"""SQLite/JSON ledger flushes and dashboard telemetry."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from polymarketbot.logs import get_logger

logger = get_logger("real_money_engine")


class LedgerMixin:
    """Ledger persistence and telemetry."""

    def _order_version_safe(self) -> int:
        """Cached CLOB order version without forcing a network call in dry-run."""
        raw = getattr(self.clob_client, "_order_version", None)
        if raw is None:
            if self.dry_run:
                return 2
            raw = getattr(self.clob_client, "order_version", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def flush_ledger(self) -> None:
        """Write atomic state snapshot to SQLite and JSON ledger."""
        cash_val, locked_val, inventory_val = self._accounting_snapshot()
        equity_val = cash_val + locked_val + inventory_val
        pos = self.get_position(self.condition_id)
        total_pnl_val = equity_val - self.initial_equity

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO portfolio_snapshots (
                timestamp, cash, market_value, equity, unrealized_pnl, realized_pnl,
                total_pnl, total_rewards, total_gas_fees, total_merges, total_splits, total_volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                cash_val,
                inventory_val,
                equity_val,
                inventory_val,
                pos.realized_pnl,
                total_pnl_val,
                self.total_rewards_credited,
                self.total_gas_fees,
                self.total_merges,
                self.total_splits,
                self.total_volume_traded_usd,
            ),
        )

        cur.execute(
            """
            INSERT INTO positions (
                timestamp, condition_id, shares_yes, shares_no, avg_cost_yes,
                avg_cost_no, net_exposure, complete_sets, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                self.condition_id,
                pos.shares_yes,
                pos.shares_no,
                pos.avg_cost_yes,
                pos.avg_cost_no,
                pos.net_exposure,
                pos.complete_sets,
                pos.realized_pnl,
            ),
        )
        self._conn.commit()

        ledger_data = {
            "updated_at": time.time(),
            "condition_id": self.condition_id,
            "mode": "DRY_RUN" if self.dry_run else "REAL_MONEY",
            "portfolio": {
                "cash": cash_val,
                "free_cash": cash_val,
                "locked_cash": locked_val,
                "initial_equity": self.initial_equity,
                "total_wallet_balance": getattr(self, "total_wallet_balance", cash_val),
                "budget_pct": getattr(self, "budget_pct", 1.0),
                "trading_bankroll": getattr(self, "trading_bankroll", self.initial_cash),
                "pol_balance": self.pol_balance,
                "equity": equity_val,
                "unrealized_pnl": inventory_val,
                "realized_pnl": pos.realized_pnl,
                "total_pnl": total_pnl_val,
                "total_rewards_credited": self.total_rewards_credited,
                "modelled_rewards": self.total_rewards_credited,
                "actual_rewards": self.actual_rewards_usd,
                "actual_rewards_usd": self.actual_rewards_usd,
                "actual_rebates_usd": self.actual_rebates_usd,
                "actual_daily_rewards_usd": self.actual_daily_rewards_usd,
                "reward_source": self.reward_source,
                "fees_paid": self.fees_paid_usd,
                "order_version": self._order_version_safe(),
                "tick_size": self.tick_size,
                "total_gas_fees": self.total_gas_fees,
                "total_merges": self.total_merges,
                "total_splits": self.total_splits,
            },
            "position": asdict(pos),
            "active_quotes": list(self.active_quotes.values()),
            "last_deskew": asdict(self.latest_deskew_decision) if self.latest_deskew_decision else None,
            "epoch_index": self.epoch_index,
            "recent_fills": [f.to_dict() for f in self.fills_history[-10:]],
            "recent_merges": [asdict(m) for m in self.merges_history[-10:]],
        }

        tmp_path = Path(f"{self.ledger_path}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(ledger_data, f, indent=2)
            os.replace(tmp_path, self.ledger_path)
        except Exception as e:
            logger.warning(f"Failed to write JSON ledger atomically: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def get_telemetry(self) -> dict[str, Any]:
        """Comprehensive telemetry dictionary for terminal dashboard rendering."""
        cash_val = self.get_cash()
        equity_val = self.get_equity()
        pos = self.get_position(self.condition_id)
        locked_val = self.get_locked_cash()
        inventory_val = equity_val - cash_val - locked_val

        v_queue_total = self.compute_resting_queue_value()
        reserve_ratio_pct = (cash_val / max(1e-4, equity_val)) * 100.0
        daily_yield = self.get_current_daily_yield()
        init_cash = max(1.0, self.initial_cash)
        nominal_apr = (daily_yield / init_cash) * 365.25 * 100.0
        r_daily = max(0.0, daily_yield / init_cash)
        try:
            compounding_apr = (
                (math.pow(1.0 + r_daily, 365.25) - 1.0) * 100.0
                if (0.0 < r_daily <= 5.0)
                else (99999.9 if r_daily > 5.0 else 0.0)
            )
        except (OverflowError, ValueError):
            compounding_apr = 99999.9
        total_wb = cash_val + locked_val + inventory_val
        b_pct = getattr(self, "budget_pct", 1.0) * 100.0
        t_bankroll = getattr(self, "trading_bankroll", self.initial_cash)
        book_stale = not (self.has_fresh_book("YES") and self.has_fresh_book("NO"))

        return {
            "timestamp": time.time(),
            "condition_id": self.condition_id,
            "mode": "DRY_RUN" if self.dry_run else "REAL_MONEY",
            "equity": round(equity_val, 2),
            "cash": round(cash_val, 2),
            "free_cash": round(cash_val, 2),
            "locked_cash": round(locked_val, 2),
            "initial_equity": round(self.initial_equity, 2),
            "total_wallet_balance": round(total_wb, 2),
            "budget_pct": round(b_pct, 1),
            "trading_bankroll": round(t_bankroll, 2),
            "pol_balance": round(self.pol_balance, 4),
            "market_value": round(inventory_val, 2),
            "realized_pnl": round(pos.realized_pnl, 4),
            "unrealized_pnl": round(inventory_val, 4),
            "total_pnl": round(equity_val - self.initial_equity, 4),
            "shares_yes": round(pos.shares_yes, 2),
            "shares_no": round(pos.shares_no, 2),
            "net_exposure": round(pos.net_exposure, 2),
            "complete_sets": round(pos.complete_sets, 2),
            "avg_cost_yes": round(pos.avg_cost_yes, 4),
            "avg_cost_no": round(pos.avg_cost_no, 4),
            "active_quotes": list(self.active_quotes.values()),
            "fills_count": len(self.fills_history),
            "recent_fills": [f.to_dict() for f in self.fills_history[-5:]],
            "total_merges": self.total_merges,
            "total_splits": self.total_splits,
            "total_gas_fees": round(self.total_gas_fees, 4),
            "total_rewards": round(self.total_rewards_credited, 4),
            "theoretical_rewards_usd": round(self.total_rewards_credited, 4),
            "modelled_rewards": round(self.total_rewards_credited, 4),
            "actual_rewards": round(self.actual_rewards_usd, 4),
            "actual_rewards_usd": round(self.actual_rewards_usd, 4),
            "actual_rebates_usd": round(self.actual_rebates_usd, 4),
            "actual_daily_rewards_usd": round(self.actual_daily_rewards_usd, 4),
            "actual_total_payout_usd": round(self.actual_rewards_usd + self.actual_rebates_usd, 4),
            "reward_source": self.reward_source,
            "verified_scoring_orders": dict(self.verified_scoring_orders),
            "total_volume_usd": round(self.total_volume_traded_usd, 2),
            "fees_paid": round(self.fees_paid_usd, 6),
            "epoch_index": self.epoch_index,
            "projected_daily_usd": float(
                self._last_reward_epoch.get("projected_daily_usd", 0.0) or 0.0
            ),
            "accrued_daily_usd": float(
                self._last_reward_epoch.get("accrued_daily_usd", 0.0) or 0.0
            ),
            "latest_deskew": asdict(self.latest_deskew_decision) if self.latest_deskew_decision else None,
            "reserve_cash_usd": round(cash_val, 2),
            "reserve_ratio_pct": round(reserve_ratio_pct, 1),
            "resting_queue_value_usd": round(v_queue_total, 4),
            "daily_yield_usd": round(daily_yield, 2),
            "nominal_apr_pct": round(nominal_apr, 1),
            "compounding_apr_pct": round(compounding_apr, 1),
            "migration_count": len(self.migration_history),
            "trading_enabled": self.trading_enabled,
            "market_closed": self.market_closed,
            "halt_reason": self.halt_reason,
            "book_stale": book_stale,
            "order_version": self._order_version_safe(),
            "tick_size": self.tick_size,
        }
