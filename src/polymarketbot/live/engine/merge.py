"""Three-phase CTF merge: claim, unlocked settlement I/O, and apply."""

from __future__ import annotations

import time
from dataclasses import dataclass

from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.ctf import MergeExecutionResult
from polymarketbot.strategy.onchain.settlement import (
    NEGRISK_ADAPTER,
    ROUTER,
    SettlementRoute,
    amount_to_6dp,
    build_negrisk_merge,
    build_router_merge,
    verify_route,
)

logger = get_logger("real_money_engine")


@dataclass
class _CtfMergePreflight:
    """Merge state snapshotted under the engine lock before settlement I/O."""

    available_sets: float
    sets_to_merge: float
    now: float
    addr: str
    dry_run: bool


@dataclass
class _CtfMergeOutcome:
    """Receipt plus post-merge chain re-read produced by the unlocked I/O phase."""

    result: MergeExecutionResult
    apply: bool
    chain_yes: float | None = None
    chain_no: float | None = None
    balance_read_error: BaseException | None = None


class MergeMixin:
    """Single-flight CTF complete-set merges."""

    def trigger_ctf_merge(self, amount_sets: float | None = None) -> MergeExecutionResult | None:
        """Merge complete sets only when real on-chain settlement is possible.

        The inventory and cash mutation is driven exclusively by a confirmed
        merge receipt plus its actual gas cost. Every refusal path leaves state
        untouched.

        Three phases keep ``self._lock`` off every I/O call: claim (snapshot
        state and the per-condition single-flight marker, lock held), execute
        (pending-settlement DB read, route lookup, chain preflight, broadcast
        and receipt parsing, lock released), apply (re-acquire the lock, mutate,
        persist, drop the marker).
        """
        preflight = self._claim_ctf_merge(amount_sets)
        if preflight is None:
            return None
        try:
            outcome = self._execute_ctf_merge(preflight)
        except BaseException:
            self._release_ctf_merge_marker()
            raise
        if outcome is None:
            self._release_ctf_merge_marker()
            return None
        return self._apply_ctf_merge(preflight, outcome)

    def _release_ctf_merge_marker(self) -> None:
        """Drop this condition's in-flight marker (idempotent, lock-safe)."""
        with self._lock:
            self._settlement_inflight.discard(self.condition_id)

    def run_pending_ctf_merge(self) -> MergeExecutionResult | None:
        """Run a merge a fill requested, with the engine lock released.

        ``_apply_fill`` books fills under ``self._lock`` and cannot itself
        broadcast: phase 2 waits on an RPC round trip and a block. The flag it
        sets is consumed here, after the caller's lock scope ends.
        """
        if not getattr(self, "_merge_requested", False):
            return None
        self._merge_requested = False
        return self.trigger_ctf_merge()

    def _claim_ctf_merge(
        self, amount_sets: float | None
    ) -> _CtfMergePreflight | None:
        """Phase 1: under the lock, snapshot merge state and claim the marker.

        Nothing here touches SQLite, the chain or the venue: every blocking call
        runs in :meth:`_execute_ctf_merge` with the lock released. The marker is
        claimed last, so a local refusal never has to release it.
        """
        with self._lock:
            if self.condition_id in self._settlement_inflight:
                logger.debug(
                    f"CTF merge skipped: a settlement for {self.condition_id[:14]} "
                    "is already in flight"
                )
                return None

            if not self.dry_run and not bool(
                getattr(self.ctf_client, "is_settlement_enabled", False)
            ):
                logger.error(
                    "CTF merge refused: live on-chain settlement is disabled for this client"
                )
                return None

            pos = self.get_position(self.condition_id)
            available_sets = min(pos.shares_yes, pos.shares_no)
            if available_sets <= 0.0:
                logger.debug(
                    f"CTF merge skipped: no complete sets held "
                    f"(YES: {pos.shares_yes:.2f}, NO: {pos.shares_no:.2f})"
                )
                return None

            if amount_sets is not None:
                sets_to_merge = min(float(amount_sets), available_sets)
                if float(amount_sets) > available_sets:
                    logger.warning(
                        f"Requested CTF merge amount ({amount_sets:.2f}) exceeds available complete sets "
                        f"({available_sets:.2f}). Clamping to {available_sets:.2f}."
                    )
            else:
                sets_to_merge = available_sets

            cfg = self.risk_manager.config
            risk_min_notional = float(getattr(cfg, "min_merge_notional_usd", 0.0) or 0.0)
            min_sets = max(float(self.auto_merge_threshold), risk_min_notional)
            if sets_to_merge < min_sets:
                logger.debug(
                    f"CTF merge skipped: {sets_to_merge:.2f} sets below min threshold {min_sets:.2f}"
                )
                return None

            now = time.time()
            if (now - self._last_merge_ts) < self.merge_cooldown_seconds:
                logger.debug(
                    f"CTF merge skipped: cooldown {self.merge_cooldown_seconds:.0f}s not elapsed"
                )
                return None

            self._settlement_inflight.add(self.condition_id)
            return _CtfMergePreflight(
                available_sets=available_sets,
                sets_to_merge=sets_to_merge,
                now=now,
                addr=str(getattr(self.clob_client, "maker_address", "") or ""),
                dry_run=bool(self.dry_run),
            )

    def _execute_ctf_merge(
        self, preflight: _CtfMergePreflight
    ) -> _CtfMergeOutcome | None:
        """Phase 2: every settlement I/O call, with no engine lock held.

        Returns None for a refusal (already logged), otherwise the receipt plus
        any post-merge chain re-read for phase 3 to apply under the lock.
        """
        # One settlement in flight per condition. A PENDING row means a signed
        # transaction exists whose outcome is unknown; merging again would
        # spend the same complete sets twice.
        pending_rows = self._pending_settlement_rows()
        if pending_rows is None:
            logger.error(
                "CTF merge refused: cannot prove that no settlement is already pending"
            )
            return None
        if pending_rows:
            oldest = pending_rows[0]
            logger.error(
                f"CTF merge refused: settlement tx {oldest.get('tx_hash')} "
                f"({oldest.get('kind')} {oldest.get('amount')}) for {self.condition_id[:14]} "
                f"is still PENDING; resolve it by hash before merging again"
            )
            return None

        addr = preflight.addr
        sets_to_merge = preflight.sets_to_merge
        route = SettlementRoute.CTF_MERGE
        meta: dict | None = None
        if not preflight.dry_run:
            if not addr:
                logger.error(
                    "Settlement aborted: maker address is unknown, cannot verify the ledger"
                )
                return None
            route, meta = self._resolve_settlement_route()
            if route is SettlementRoute.UNSUPPORTED:
                logger.error(
                    "Settlement refused: market has no supported merge route "
                    "(version/neg-risk metadata missing); refusing to broadcast blind"
                )
                return None
            if route is SettlementRoute.CTF_MERGE:
                try:
                    chain_yes = float(self.ctf_client.get_position_balance(self.asset_id_yes, addr))
                    chain_no = float(self.ctf_client.get_position_balance(self.asset_id_no, addr))
                except (RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
                    logger.error(f"Settlement aborted: on-chain token balances are unverifiable: {e}")
                    return None
                if chain_yes + 1e-9 < sets_to_merge or chain_no + 1e-9 < sets_to_merge:
                    logger.error(
                        f"Settlement aborted: chain balances YES={chain_yes:.6f} NO={chain_no:.6f} "
                        f"cannot back {sets_to_merge:.6f} sets"
                    )
                    return None

        if route is SettlementRoute.CTF_MERGE:
            res = self.ctf_client.merge_positions(
                condition_id=self.condition_id,
                amount_shares=sets_to_merge,
                token_ids=(self.asset_id_yes, self.asset_id_no),
            )
        else:
            ok, reason = verify_route(
                route,
                self.condition_id,
                addr,
                self.ctf_client.chain_client,
                meta=meta,
            )
            if not ok:
                logger.error(f"Settlement refused ({route.value}): {reason}")
                return None
            try:
                amount_6dp = amount_to_6dp(sets_to_merge)
            except ValueError as e:
                logger.error(f"Settlement aborted: invalid amount {sets_to_merge!r}: {e}")
                return None
            try:
                if route is SettlementRoute.ROUTER_MERGE:
                    calldata = build_router_merge(self.condition_id, amount_6dp)
                    target = ROUTER
                elif route is SettlementRoute.NEGRISK_MERGE:
                    calldata = build_negrisk_merge(self.condition_id, amount_6dp)
                    target = NEGRISK_ADAPTER
                else:
                    logger.error(f"Settlement aborted: unhandled route {route}")
                    return None
            except ValueError as e:
                logger.error(
                    f"Settlement aborted: cannot encode {route.value} calldata "
                    f"for {self.condition_id}: {e}"
                )
                return None
            logger.info(
                f"[{route.value}] Broadcasting merge of {sets_to_merge:.6f} sets "
                f"({amount_6dp} base units) to {target[:10]}..."
            )
            res = self.ctf_client.execute_settlement(
                calldata,
                to_address=target,
                condition_id=self.condition_id,
                amount_shares=sets_to_merge,
            )
        if not res.success:
            if getattr(res, "status", "failed") == "pending":
                logger.warning(
                    f"CTF mergePositions pending for {self.condition_id[:14]}: "
                    f"{res.error or 'no receipt yet'}; the signed tx {res.tx_hash} is stored "
                    "and will be resolved by hash. No retry, no state change."
                )
            else:
                logger.critical(
                    f"CTF mergePositions failed for {self.condition_id[:14]}: {res.error or 'unknown error'}; "
                    "no inventory or cash change applied"
                )
            return _CtfMergeOutcome(result=res, apply=False)

        # Post-merge chain re-read stays in the unlocked phase; phase 3 applies
        # whatever it returned and falls back to the local decrement on failure.
        chain_yes: float | None = None
        chain_no: float | None = None
        balance_read_error: BaseException | None = None
        if not preflight.dry_run and route is SettlementRoute.CTF_MERGE:
            try:
                chain_yes = float(
                    self.ctf_client.get_position_balance(self.asset_id_yes, addr)
                )
                chain_no = float(
                    self.ctf_client.get_position_balance(self.asset_id_no, addr)
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
                balance_read_error = e
        return _CtfMergeOutcome(
            result=res,
            apply=True,
            chain_yes=chain_yes,
            chain_no=chain_no,
            balance_read_error=balance_read_error,
        )
