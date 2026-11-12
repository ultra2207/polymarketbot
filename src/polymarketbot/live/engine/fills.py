"""Fill application, booking and per-fill measurement."""

from __future__ import annotations

import time
import uuid
from typing import Any

from polymarketbot.live.engine.markout import RealMoneyFillRecord
from polymarketbot.logs import get_logger

logger = get_logger("real_money_engine")


class FillMixin:
    """Book confirmed fills into cash, inventory and measurement tables."""

    def _apply_fill(
        self,
        tok: str,
        side: str,
        fill_sz: float,
        fill_px: float,
        fee_usd: float = 0.0,
        ts: float | None = None,
        is_taker: bool = False,
        maker_rebate_usd: float = 0.0,
    ) -> None:
        """Apply a confirmed fill to cash, inventory, cost basis, and realized PnL.

        A resting maker BUY already reserved its collateral at placement when
        the endpoint reports free cash, so its fill only moves the reserve.
        Maker fills on fee-charging markets earn a positive volume share rebate.
        """
        tok = str(tok).upper()
        side = str(side).upper()
        size = float(fill_sz)
        price = float(fill_px)
        fee = float(fee_usd)
        rebate = float(maker_rebate_usd)
        if size <= 0.0 or not (0.0 < price < 1.0):
            return
        if tok not in ("YES", "NO") or side not in ("BUY", "SELL"):
            return
        if ts is None:
            ts = time.time()

        with self._lock:
            shares_attr = "shares_yes" if tok == "YES" else "shares_no"
            avg_attr = "avg_cost_yes" if tok == "YES" else "avg_cost_no"
            shares = float(getattr(self, shares_attr))
            avg = float(getattr(self, avg_attr))
            realized = rebate
            if rebate > 0.0:
                self.cash += rebate
                self.actual_rebates_usd += rebate
            allow_short = bool(getattr(self.risk_manager.config, "allow_short", False))

            if side == "BUY":
                if shares >= 0.0:
                    new_shares = shares + size
                    if not (self._cost_basis_unknown and shares > 0.0):
                        avg = (shares * avg + size * price) / new_shares
                else:
                    cover = min(size, -shares)
                    realized += (avg - price) * cover
                    if size + shares >= -1e-12:
                        new_shares = size + shares
                        avg = price if new_shares > 1e-12 else 0.0
                    else:
                        new_shares = shares + size
                if self.dry_run or self._locked_already_in_balance is True:
                    self.cash -= size * price + fee
                else:
                    self.cash -= fee
            else:
                if shares > 1e-12:
                    sold = min(size, shares)
                    gain = 0.0 if self._cost_basis_unknown else (price - avg) * sold
                    realized += gain
                    remaining = shares - sold
                    if size > sold + 1e-9:
                        extra = size - sold
                        if not allow_short:
                            logger.critical(
                                f"SELL {size:.6f} {tok} exceeds owned {shares:.6f}; "
                                f"clamping to {sold:.6f} (shorting disabled)"
                            )
                            if size > 0.0:
                                fee *= sold / size
                            size = sold
                            extra = 0.0
                        else:
                            remaining = -extra
                            avg = price
                    if remaining <= 1e-12:
                        remaining = 0.0
                        avg = 0.0
                    new_shares = remaining
                elif shares < 0.0:
                    short_sz = -shares
                    total = short_sz + size
                    avg = (short_sz * avg + size * price) / total
                    new_shares = -total
                else:
                    if not allow_short:
                        logger.critical(
                            f"SELL {size:.6f} {tok} with zero owned shares rejected "
                            "(shorting disabled)"
                        )
                        return
                    new_shares = -size
                    avg = price
                if size <= 0.0:
                    return
                self.cash += size * price - fee

            setattr(self, shares_attr, new_shares)
            setattr(self, avg_attr, round(float(avg), 10))
            self._track_inventory_age(ts)
            self.realized_pnl += realized
            self.fees_paid_usd += fee
            self.total_volume_traded_usd += size * price

            asset_id = self.asset_id_yes if tok == "YES" else self.asset_id_no
            self.risk_manager.record_fill(
                asset_id=asset_id,
                condition_id=self.condition_id,
                side=side,
                price=price,
                size=size,
                timestamp=ts,
            )
            self.micro_risk.on_fill(asset_id, side, price, size, ts)

            if (
                self._cost_basis_unknown
                and self.shares_yes <= 1e-12
                and self.shares_no <= 1e-12
            ):
                self._cost_basis_unknown = False

            if not self.dry_run:
                cfg = self.risk_manager.config
                min_sets = max(
                    float(self.auto_merge_threshold),
                    float(getattr(cfg, "min_merge_notional_usd", 0.0) or 0.0),
                )
                if min(self.shares_yes, self.shares_no) >= min_sets:
                    # Deferred, not called here: this runs under ``self._lock``
                    # (a fill is booked from the order/feed callback), and the
                    # merge's broadcast + receipt wait must not hold the lock.
                    # ``run_pending_ctf_merge`` executes it after release.
                    self._merge_requested = True

    def _fill_mode(self) -> str:
        return "DRY_RUN" if self.dry_run else "LIVE"

    def _record_fill_observation(
        self,
        *,
        fill_key: str,
        ts: float,
        tok: str,
        side: str,
        price: float,
        size: float,
        is_maker: bool,
        order_id: str,
        fee_usd: float,
        maker_rebate_usd: float = 0.0,
        realized_pnl: float,
        note: str,
    ) -> None:
        """Persist one fill plus its post-fill marks (measurement only)."""
        mid = self._fresh_mid(tok)
        bid_at_fill: float | None = None
        ask_at_fill: float | None = None
        if mid is not None:
            raw_bid, raw_ask = self.get_best_bid_ask(tok)
            bid_at_fill = float(raw_bid) if 0.0 < float(raw_bid) < 1.0 else None
            ask_at_fill = float(raw_ask) if 0.0 < float(raw_ask) < 1.0 else None
            if bid_at_fill is None or ask_at_fill is None:
                mid = None
                bid_at_fill = None
                ask_at_fill = None
        self._insert_yield_fill_row(
            {
                "fill_key": fill_key,
                "ts": float(ts),
                "condition_id": self.condition_id,
                "token_side": tok,
                "side": side,
                "price": float(price),
                "size": float(size),
                "is_maker": 1 if is_maker else 0,
                "order_id": str(order_id or ""),
                "fee_usd": float(fee_usd),
                "maker_rebate_usd": float(maker_rebate_usd),
                "mid_at_fill": mid,
                "best_bid_at_fill": bid_at_fill,
                "best_ask_at_fill": ask_at_fill,
                "markout_30s": None,
                "markout_5m": None,
                "markout_30m": None,
                "realized_pnl": float(realized_pnl),
                "mode": self._fill_mode(),
                "note": note,
                # Columns present only on a legacy fills table.
                "fill_id": fill_key,
                "timestamp": float(ts),
                "liquidity_type": "maker" if is_maker else "taker",
                "slippage_cents": 0.0,
                "queue_depleted": 0,
                "post_fill_drift": 0.0,
                "cash_after": float(self.cash),
                "notes": note,
            }
        )
        # Horizons are queued even when the book is stale now: the due-time read
        # decides, and an unmeasurable horizon simply stays NULL.
        self._markout_tracker.record(fill_key, tok, float(price), side, now=float(ts))

    def _book_exchange_fill(
        self,
        qdata: dict[str, Any],
        tok: str,
        side: str,
        size: float,
        price: float,
        is_taker: bool,
        ts: float,
        note: str,
    ) -> RealMoneyFillRecord:
        fee = self._taker_fee(size, price) if is_taker else 0.0
        rebate = 0.0 if is_taker else self._maker_rebate(size, price)
        record = RealMoneyFillRecord(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=self.condition_id,
            token_side=tok,
            side=side,
            fill_price=price,
            fill_size=size,
            is_maker=not is_taker,
            order_id=str(qdata.get("order_id", "") or ""),
            notes=f"Live CLOB fill reconciled ({note})",
            maker_rebate_usd=rebate,
        )
        self.fills_history.append(record)
        shares_attr = "shares_yes" if str(tok).upper() == "YES" else "shares_no"
        shares_before = float(getattr(self, shares_attr, 0.0) or 0.0)
        realized_before = float(self.realized_pnl)
        self._apply_fill(
            tok, side, size, price, fee_usd=fee, ts=ts, is_taker=is_taker, maker_rebate_usd=rebate
        )
        realized_delta = float(self.realized_pnl) - realized_before
        shares_after = float(getattr(self, shares_attr, 0.0) or 0.0)

        order_id = record.order_id
        # The wash-trade guard indexes resting makers by their full size. A
        # partial fill that is never reported leaves the guard believing the
        # whole order still rests, which then blocks the engine's legitimate
        # re-quote at that price. Report the remaining size.
        guard = getattr(getattr(self, "clob_client", None), "self_trade_guard", None)
        if guard is not None and order_id and not is_taker:
            remaining = max(0.0, float(qdata.get("size", 0.0) or 0.0) - float(size))
            try:
                guard.update_maker_order(order_id, remaining)
            except Exception as exc:  # noqa: BLE001 - guard bookkeeping must not lose a fill
                logger.warning(f"STP guard size update failed for {order_id}: {exc}")
        key_prefix = order_id or record.fill_id
        fill_key = f"{key_prefix}:{float(ts):.3f}:{float(price):.6f}:{float(size):.6f}"
        self._record_fill_observation(
            fill_key=fill_key,
            ts=ts,
            tok=str(tok).upper(),
            side=str(side).upper(),
            price=price,
            size=size,
            is_maker=not is_taker,
            order_id=order_id,
            fee_usd=fee,
            maker_rebate_usd=rebate,
            realized_pnl=realized_delta,
            note=record.notes,
        )

        if is_taker:
            reference = float(
                qdata.get("decision_target_price", qdata.get("price", price)) or price
            )
            self._insert_rebalance_row(
                pathway="TIER2_TAKER",
                ts=ts,
                crossed_cents=abs(float(price) - reference) * 100.0,
                taker_fee_usd=float(fee),
                gas_usd=0.0,
                inventory_before=shares_before,
                inventory_after=shares_after,
                note=f"tier2 taker fill ({note})",
            )
        return record
