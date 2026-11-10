"""Reward earnings reads (per-day totals)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import USDC_ASSET_ADDRESSES

logger = get_logger("clob_client")


def _as_float(value: Any) -> Optional[float]:
    """Parse a JSON number/string; None when absent or malformed."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _earnings_usd(row: dict[str, Any]) -> Optional[float]:
    """USD value of one reward row: ``earnings * asset_rate``.

    A missing rate is 1.0 only for a known USDC asset; otherwise the row cannot
    be valued and returns None (the caller excludes it from the USD sum).
    """
    earnings = _as_float(row.get("earnings"))
    if earnings is None:
        return None
    rate = _as_float(row.get("asset_rate"))
    if rate is None:
        address = str(row.get("asset_address") or "").strip().lower()
        if address not in USDC_ASSET_ADDRESSES:
            return None
        rate = 1.0
    return earnings * rate


def _summarise_earnings(
    rows: list[dict[str, Any]],
) -> tuple[float, dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Sum reward rows in USD and group them per condition.

    Returns ``(total_usd, breakdown_by_condition, unknown_rows)``.
    """
    total = 0.0
    breakdown: dict[str, dict[str, Any]] = {}
    unknown: list[dict[str, Any]] = []
    for row in rows:
        usd = _earnings_usd(row)
        if usd is None:
            unknown.append(row)
            continue
        total += usd
        key = str(row.get("condition_id") or row.get("asset_address") or "unknown")
        entry = breakdown.get(key)
        if entry is None:
            entry = {
                "condition_id": row.get("condition_id"),
                "asset_address": row.get("asset_address"),
                "earned_usd": 0.0,
                "earnings": 0.0,
            }
            breakdown[key] = entry
        entry["earned_usd"] += usd
        entry["earnings"] += _as_float(row.get("earnings")) or 0.0
    return total, breakdown, unknown


class RewardMixin:
    """Per-day reward totals and their Data API fallback."""


    def get_rewards_for_date(self, date: Optional[str] = None) -> dict[str, Any]:
        """Read ``GET /rewards/user/total`` for one UTC day, per asset.

        Returns the row array (``{date, asset_address, maker_address, earnings,
        native_earnings?, sponsored_earnings?, asset_rate}``) plus the USD total
        and the per-asset breakdown. A missing ``asset_rate`` is 1.0 only for a
        known USDC asset; any other row without a rate is excluded from the USD
        sum and reported in ``unknown_earnings``. Raises nothing: on failure the
        result is ``source="unavailable"`` with an ``error`` and no number.
        """
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.dry_run or not self.maker_address:
            return {
                "date": target_date,
                "rows": [],
                "total_usd": 0.0,
                "by_asset": {},
                "by_condition": {},
                "unknown_earnings": [],
                "row_count": 0,
                "source": "simulated",
            }
        path = "/rewards/user/total"
        try:
            payload = self._request(
                "GET",
                path,
                params={
                    "date": target_date,
                    "signature_type": self.signature_type,
                    "maker_address": self.maker_address,
                },
            )
        except Exception as e:
            logger.debug(f"CLOB {path} query: {e}")
            return {
                "date": target_date,
                "rows": [],
                "total_usd": 0.0,
                "by_asset": {},
                "by_condition": {},
                "unknown_earnings": [],
                "row_count": 0,
                "source": "unavailable",
                "error": str(e),
            }
        if not isinstance(payload, list):
            return {
                "date": target_date,
                "rows": [],
                "total_usd": 0.0,
                "by_asset": {},
                "by_condition": {},
                "unknown_earnings": [],
                "row_count": 0,
                "source": "unavailable",
                "error": (
                    f"GET {path} returned {type(payload).__name__}, "
                    "expected a JSON array"
                ),
            }
        rows = [row for row in payload if isinstance(row, dict)]
        total_usd, breakdown, unknown = _summarise_earnings(rows)
        by_asset: dict[str, float] = {}
        for row in rows:
            usd = _earnings_usd(row)
            if usd is None:
                continue
            key = str(row.get("asset_address") or "unknown")
            by_asset[key] = by_asset.get(key, 0.0) + usd
        return {
            "date": target_date,
            "rows": rows,
            "total_usd": total_usd,
            "by_asset": by_asset,
            "by_condition": breakdown,
            "unknown_earnings": unknown,
            "row_count": len(rows),
            "source": "clob_api",
        }

    def get_total_rewards(self) -> dict[str, Any]:
        """Query the account's **today** reward earnings from CLOB.

        ``GET /rewards/user/total`` is a per-day endpoint (``date`` required,
        ``signature_type`` and ``maker_address`` for proxy accounts) and returns
        a JSON array of ``{date, asset_address, maker_address, earnings,
        native_earnings?, sponsored_earnings?, asset_rate}``. The headline
        values are today's USD total and are flagged ``source="clob_api_daily"``
        so nobody mistakes them for lifetime earnings.

        On failure it falls back to Data API v2 lifetime stats
        (``source="polymarket_data_api"``); if that also fails it returns zeros
        with ``source="unavailable"`` and an ``error`` string.
        """
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.dry_run or not self.maker_address:
            return {
                "total_rewards_usd": 0.0,
                "reward_income_usd": 0.0,
                "maker_rebates_usd": 0.0,
                "yield_income_usd": 0.0,
                "payout_count": 0,
                "source": "simulated",
                "recent_payouts": [],
            }

        errors: list[str] = []

        # 1. CLOB per-day totals (reference:
        # https://docs.polymarket.com/api-reference/rewards/get-total-earnings-for-user-by-date)
        if self.credentials.is_configured:
            earnings = self.get_rewards_for_date(target_date)
            if earnings["source"] == "clob_api":
                # Rewards and rebates are separate per-wallet programs with
                # separate endpoints. Rebates used to be reported here as a
                # hardcoded 0.0, which said "you earned no rebate" for every
                # wallet that did; they are read from /rebates/current and the
                # source of that number travels with it.
                rebates = self.get_current_rebates(target_date)
                return {
                    "total_rewards_usd": earnings["total_usd"],
                    "reward_income_usd": earnings["total_usd"],
                    "maker_rebates_usd": float(rebates.get("total_usd") or 0.0),
                    "yield_income_usd": 0.0,
                    "payout_count": earnings["row_count"],
                    "date": target_date,
                    "source": "clob_api_daily",
                    "rebate_source": rebates["source"],
                    "rebate_error": rebates.get("error", ""),
                    "asset_earnings": [
                        {
                            "asset_address": row.get("asset_address"),
                            "earnings": _as_float(row.get("earnings")),
                            "asset_rate": _as_float(row.get("asset_rate")),
                            "usd": _earnings_usd(row),
                        }
                        for row in earnings["rows"]
                    ],
                    "unknown_earnings": earnings["unknown_earnings"],
                    "recent_payouts": [],
                }
            errors.append(earnings.get("error") or "rewards/user/total unavailable")
            logger.debug(f"CLOB /rewards/user/total query: {earnings.get('error')}")

        # 2. Data API v2 lifetime fallback (NOT today; identified by `source`).
        try:
            data_client = DataAPIClient(session=self.session)
            stats = data_client.user_stats(self.maker_address)
            all_time = stats.get("all_time_pnl", {}) if isinstance(stats, dict) else {}
            reward_inc = float(all_time.get("reward_income", 0.0) or 0.0)
            maker_reb = float(all_time.get("maker_rebate", 0.0) or 0.0)
            yield_inc = float(all_time.get("yield_income", 0.0) or 0.0)

            payouts = []
            try:
                act = data_client.activity(
                    user=self.maker_address,
                    types=["REWARD", "MAKER_REBATE"],
                    max_pages=1,
                )
                payouts = act[:20] if act else []
            except Exception as e:
                errors.append(f"activity: {e}")

            return {
                "total_rewards_usd": reward_inc + maker_reb,
                "reward_income_usd": reward_inc,
                "maker_rebates_usd": maker_reb,
                "yield_income_usd": yield_inc,
                "payout_count": len(payouts),
                "source": "polymarket_data_api",
                "recent_payouts": payouts,
            }
        except Exception as e:
            errors.append(str(e))
            logger.warning(f"Failed to fetch authentic reward balance from Data API: {e}")

        return {
            "total_rewards_usd": 0.0,
            "reward_income_usd": 0.0,
            "maker_rebates_usd": 0.0,
            "yield_income_usd": 0.0,
            "payout_count": 0,
            "source": "unavailable",
            "recent_payouts": [],
            "error": "; ".join(errors) if errors else "rewards unavailable",
        }
