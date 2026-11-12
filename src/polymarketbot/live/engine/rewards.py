"""Ground-truth reward synchronization and modelled reward handling."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob import ClobAPIError

logger = get_logger("real_money_engine")


class RewardMixin:
    """Venue reward telemetry and modelled reward credit."""

    def sync_rewards(self, force: bool = False) -> None:
        """Synchronize ground-truth reward telemetry (throttled to once per 60 s)."""
        now = time.time()
        if not force and (now - self._last_reward_sync) < 60.0:
            return
        self._last_reward_sync = now
        try:
            rew_data = self.clob_client.get_total_rewards()
            if rew_data:
                self.actual_rewards_usd = float(rew_data.get("reward_income_usd", 0.0) or 0.0)
                venue_rebates = float(rew_data.get("maker_rebates_usd", 0.0) or 0.0)
                modelled_rebates = float(self.actual_rebates_usd)
                if abs(venue_rebates - modelled_rebates) > 1e-9:
                    # Cash already carries the per-fill modelled credits. This
                    # line is the evidence needed to calibrate that model
                    # against the venue's own total; it must not silently
                    # overwrite the two figures as if they were the same.
                    logger.info(
                        f"Rebate reconciliation: venue ${venue_rebates:.4f} vs "
                        f"modelled ${modelled_rebates:.4f} "
                        f"(delta ${venue_rebates - modelled_rebates:+.4f}); "
                        "cash carries the modelled credit"
                    )
                self.actual_rebates_usd = venue_rebates
                self.reward_source = str(rew_data.get("source", self.reward_source))

            daily_data = self.clob_client.get_daily_rewards()
            if daily_data:
                source = str(daily_data.get("source", "") or "")
                if source and source != "unavailable":
                    self.reward_source = source
                self.actual_daily_rewards_usd = float(
                    daily_data.get("daily_combined_usd", 0.0) or 0.0
                )
                # Only a real venue answer is persisted; an unavailable or
                # simulated response writes nothing (never a fabricated zero).
                if source not in ("", "unavailable", "simulated"):
                    self._record_reward_payouts(daily_data, source)

            # Check scoring status for resting quotes
            for qid, qdata in list(self.active_quotes.items()):
                order_id = qdata.get("order_id")
                if order_id:
                    score_res = self.clob_client.get_order_scoring(order_id)
                    is_scoring = bool(score_res.get("scoring", False))
                    qdata["scoring"] = is_scoring
                    self.verified_scoring_orders[order_id] = is_scoring
        except (ClobAPIError, sqlite3.Error, KeyError, TypeError, ValueError, OSError) as e:
            logger.debug(f"Failed to sync ground-truth rewards: {e}")

    def _record_reward_payouts(self, daily_data: dict[str, Any], source: str) -> None:
        """Persist venue-reported per-market daily payouts (idempotent)."""
        breakdown = daily_data.get("market_breakdown")
        if not isinstance(breakdown, list):
            return
        date = str(daily_data.get("date") or time.strftime("%Y-%m-%d", time.gmtime()))
        observed_ts = time.time()
        for row in breakdown:
            if not isinstance(row, dict):
                continue
            condition = row.get("condition_id") or row.get("asset_address")
            usd = row.get("earned_usd")
            if usd is None:
                usd = row.get("earnings_usd")
            if not condition or usd is None:
                continue
            try:
                usd_value = float(usd)
            except (TypeError, ValueError):
                continue
            self._insert_reward_payout(
                date=date,
                condition_id=str(condition),
                usd=usd_value,
                source=source,
                observed_ts=observed_ts,
            )

    def credit_reward(self, amount: float, condition_id: str, note: str = "") -> None:
        """Credit modelled rewards to simulated cash only.

        In live mode the modelled epoch payout is telemetry: crediting it would
        mask trading losses in the daily-loss and drawdown breakers.
        """
        amt = float(amount)
        if self.dry_run:
            self.cash += amt
            return
        logger.info(
            f"[REWARD TELEMETRY] Modelled reward ${amt:.6f} for {condition_id[:12]} "
            f"recorded only, not credited to live cash ({note})"
        )
