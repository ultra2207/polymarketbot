"""Settlement routing, merge application, split refusal and settlement replay."""

from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple

from polymarketbot.gamma import GammaClient
from polymarketbot.live.engine.merge import _CtfMergeOutcome, _CtfMergePreflight
from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.ctf import MergeExecutionResult
from polymarketbot.strategy.onchain.pending import (
    REVERTED as SETTLEMENT_REVERTED,
)
from polymarketbot.strategy.onchain.pending import (
    SUCCESS as SETTLEMENT_SUCCESS,
)
from polymarketbot.strategy.onchain.pending import (
    PendingSettlementStore,
)
from polymarketbot.strategy.onchain.settlement import (
    SettlementRoute,
    classify_merge_route,
)

logger = get_logger("real_money_engine")


class OnchainMixin:
    """On-chain settlement bookkeeping for the live engine."""

    def _pending_settlement_rows(self) -> Optional[List[dict]]:
        """Unresolved settlement rows for this condition; None when unreadable."""
        if self._conn is None:
            return None
        try:
            return PendingSettlementStore(self._conn).pending_for(self.condition_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger is a refusal
            logger.error(f"Pending settlement lookup failed: {exc}")
            return None

    def reconcile_pending_settlements(self) -> dict[str, int]:
        """Replay every stored settlement hash against the chain. Read-only.

        A settlement that timed out locally may have been mined anyway. Each
        stored hash is looked up by hash, so the transaction is never re-signed
        and no state is mutated here: the row moves to SUCCESS/REVERTED when a
        receipt exists and stays PENDING otherwise. Returns counts for the
        startup log.
        """
        if self._conn is None:
            return {"checked": 0, "resolved": 0, "pending": 0}
        store = PendingSettlementStore(self._conn)
        rows = store.unresolved()
        resolved = 0
        chain = getattr(self.ctf_client, "chain_client", None)
        receipt_lookup = getattr(chain, "get_transaction_receipt", None)

        for row in rows:
            tx_hash = str(row.get("tx_hash") or "")
            if not tx_hash or receipt_lookup is None:
                continue
            try:
                receipt = receipt_lookup(tx_hash)
            except Exception as exc:  # noqa: BLE001 - unknown stays pending
                logger.warning(f"Settlement receipt lookup for {tx_hash} failed: {exc}")
                continue
            if not isinstance(receipt, dict) or not receipt:
                continue
            status = receipt.get("status")
            try:
                mined = status is not None and int(str(status), 16) == 1
            except (TypeError, ValueError):
                mined = False
            store.resolve(tx_hash, SETTLEMENT_SUCCESS if mined else SETTLEMENT_REVERTED, receipt)
            resolved += 1
            logger.info(
                f"Settlement {tx_hash} ({row.get('kind')} {row.get('amount')}) "
                f"reconciled on chain: {'mined' if mined else 'reverted'}"
            )

        if rows:
            logger.info(
                f"Pending settlements: {len(rows)} checked, {resolved} resolved, "
                f"{len(rows) - resolved} still pending"
            )
        return {"checked": len(rows), "resolved": resolved, "pending": len(rows) - resolved}

    def update_dynamic_gas_and_threshold(self) -> Tuple[float, float]:
        """Update dynamic gas fee and optimal merge threshold from live conditions.

        An unmeasurable gas price does not move the threshold: the last measured
        pair stays in force rather than being replaced by a guess.
        """
        gas_cost = self.ctf_client.estimate_merge_cost_usd()
        if gas_cost is None:
            logger.warning(
                "gas unmeasured this cycle; keeping the last measured merge gas "
                f"${float(getattr(self, 'gas_fee_merge', 0.0)):.6f} and threshold "
                f"{float(getattr(self, 'auto_merge_threshold', 0.0)):.2f}"
            )
            return (
                float(getattr(self, "gas_fee_merge", 0.0)),
                float(getattr(self, "auto_merge_threshold", 0.0)),
            )
        threshold = self.ctf_client.calculate_optimal_merge_threshold(gas_cost_usd=gas_cost)
        self.gas_fee_merge = gas_cost
        self.auto_merge_threshold = threshold
        return gas_cost, threshold

    def _resolve_settlement_route(self) -> tuple[SettlementRoute, Optional[dict]]:
        """Classify the market's settlement ledger once, from Gamma metadata.

        A lookup failure is *not* cached: a transient Gamma outage must not
        permanently pin a live engine to "unsupported". Only a supported route
        is cached.
        """
        cached = getattr(self, "_settlement_route_cache", None)
        if cached is not None:
            return cached
        condition = str(self.condition_id or "")
        if not (
            len(condition) == 66
            and condition.startswith("0x")
            and all(ch in "0123456789abcdefABCDEF" for ch in condition[2:])
        ):
            # A malformed condition id cannot exist on-chain (and is what the
            # unit tests use). Keep the legacy CTF path without a network call.
            return SettlementRoute.CTF_MERGE, None
        try:
            meta = GammaClient().market_meta(self.condition_id)
        except Exception as exc:  # noqa: BLE001 - the balance preflight is the guard
            logger.warning(
                f"Settlement route lookup failed ({exc}); defaulting to v1 CTF, "
                "which the on-chain balance preflight will refuse if wrong"
            )
            return SettlementRoute.CTF_MERGE, None
        if meta is None:
            # Gamma could not confirm the ledger (unknown condition or a
            # transient outage). Fall back to the legacy CTF path: its on-chain
            # balance preflight refuses when the positions actually live on the
            # Router or NegRisk ledger, so this cannot merge the wrong ledger.
            logger.warning(
                f"Gamma returned no metadata for {self.condition_id[:14]}; "
                "settling as v1 CTF (balance preflight still applies)"
            )
            return SettlementRoute.CTF_MERGE, None
        route = classify_merge_route(meta)
        if route is not SettlementRoute.UNSUPPORTED:
            self._settlement_route_cache = (route, meta)
        return route, meta

    def _apply_ctf_merge(
        self, preflight: _CtfMergePreflight, outcome: _CtfMergeOutcome
    ) -> MergeExecutionResult:
        """Phase 3: re-acquire the lock, apply the confirmed result, drop the marker."""
        with self._lock:
            try:
                res = outcome.result
                if not outcome.apply:
                    return res

                gas_usd = res.gas_fee_usd
                if gas_usd is None:
                    logger.error(
                        "merge confirmed on chain but its gas cost is unmeasured "
                        "(POL/USD unavailable); leaving it unapplied so the same "
                        "transaction is replayed by hash instead of booking a "
                        "guessed cash figure"
                    )
                    return res

                sets_to_merge = preflight.sets_to_merge
                now = preflight.now
                self._last_merge_ts = now
                self.shares_yes = max(0.0, self.shares_yes - sets_to_merge)
                self.shares_no = max(0.0, self.shares_no - sets_to_merge)
                self.cash += float(res.usdc_unlocked) - float(gas_usd)
                self.total_gas_fees += float(gas_usd)
                self.total_merges += 1
                self.merges_history.append(res)

                if outcome.chain_yes is not None:
                    self.shares_yes = outcome.chain_yes
                if outcome.chain_no is not None:
                    self.shares_no = outcome.chain_no
                if outcome.balance_read_error is not None:
                    logger.warning(
                        f"Post-merge on-chain balance re-read failed ({outcome.balance_read_error}); "
                        f"keeping the local decrement (YES {self.shares_yes:.4f}, NO {self.shares_no:.4f})"
                    )

                # Persist to SQLite
                cur = self._conn.cursor()
                cur.execute(
                    """
                    INSERT INTO ctf_merges (
                        timestamp, condition_id, complete_sets, usdc_gross, gas_fee,
                        net_cash, realized_gain, cash_after
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        self.condition_id,
                        sets_to_merge,
                        res.usdc_unlocked,
                        gas_usd,
                        res.usdc_unlocked - gas_usd,
                        0.0,
                        self.cash,
                    ),
                )
                self._conn.commit()
                self.flush_ledger()

                # Measurement: one row per confirmed merge, keyed by tx hash so a
                # retried call cannot double-count it. Never raises into settlement.
                try:
                    tx_hash = str(getattr(res, "tx_hash", "") or "")
                    dedupe_key = (
                        f"tx:{tx_hash}" if tx_hash else f"at:{self.condition_id}:{now:.3f}"
                    )
                    note = (
                        f"merge key={dedupe_key} sets={sets_to_merge:.4f} "
                        f"{'dry-run' if preflight.dry_run else 'live'}"
                    )
                    existing = self._conn.execute(
                        "SELECT 1 FROM rebalances WHERE pathway = 'MERGE' AND note = ? LIMIT 1",
                        (note,),
                    ).fetchone()
                    if existing is None:
                        self._insert_rebalance_row(
                            pathway="MERGE",
                            crossed_cents=0.0,
                            taker_fee_usd=0.0,
                            gas_usd=float(gas_usd),
                            inventory_before=float(preflight.available_sets),
                            inventory_after=max(
                                0.0, float(preflight.available_sets) - sets_to_merge
                            ),
                            note=note,
                        )
                except Exception as e:  # measurement only; never raises into settlement
                    logger.warning(f"Merge cost measurement failed: {e}")

                return res
            finally:
                self._settlement_inflight.discard(self.condition_id)

    def trigger_ctf_split(self, amount_usdc: float) -> dict[str, Any]:
        """Split collateral into YES+NO. Only simulated: no on-chain split exists here."""
        if not self.dry_run:
            message = (
                "CTF split is not implemented for live settlement; refusing to mutate live "
                "inventory or cash"
            )
            logger.error(message)
            return {
                "success": False,
                "error": message,
                "amount_split": 0.0,
                "cash_after": self.cash,
            }

        amt = float(amount_usdc)
        self.cash -= amt
        self.shares_yes += amt
        self.shares_no += amt
        self.total_splits += 1

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO ctf_splits (timestamp, condition_id, amount_split, cash_after)
            VALUES (?, ?, ?, ?)
            """,
            (time.time(), self.condition_id, amt, self.cash),
        )
        self._conn.commit()
        self.flush_ledger()
        return {"success": True, "amount_split": amt, "cash_after": self.cash}
