"""Maker rebate entitlements and the combined daily reward summary."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import ClobAPIError
from polymarketbot.strategy.venue.clob.rewards import (
    _as_float,
    _summarise_earnings,
)

logger = get_logger("clob_client")


class RebateMixin:
    """Rebate entitlement reads."""


    def get_current_rebates(self, date: Optional[str] = None) -> dict[str, Any]:
        """Read a maker's own rebated fees for one UTC day.

        ``GET /rebates/current`` (no authentication; ``date`` and a nonzero
        ``maker_address`` are required) returns an array of
        ``{date, condition_id, asset_address, maker_address, rebated_fees_usdc}``
        or ``null`` when the maker has no rebated-fee rows
        (https://docs.polymarket.com/api-reference/rebates/get-current-rebated-fees-for-a-maker).
        ``rebated_fees_usdc`` is an exact USDC decimal string.

        The response is a **per-wallet entitlement** for that market's rebate
        pool, not aggregate maker flow, so it is the authoritative number for
        accounting. ``null`` is a measured zero (the venue answered: no rows);
        an unreachable endpoint is ``source="unavailable"`` with an ``error``
        and no number - never a fabricated zero marked valid.
        """
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        empty = {
            "date": target_date,
            "rows": [],
            "by_condition": {},
            "total_usd": 0.0,
            "row_count": 0,
            "unknown_rows": [],
        }
        if self.dry_run or not self.maker_address:
            return {
                **empty,
                "source": "simulated",
                "error": "dry run or no maker address: the venue cannot be asked",
            }
        path = "/rebates/current"
        try:
            payload = self._request(
                "GET",
                path,
                params={"date": target_date, "maker_address": self.maker_address},
                auth=False,
            )
        except Exception as e:
            logger.warning(f"CLOB /rebates/current query failed: {e}")
            return {**empty, "source": "unavailable", "error": str(e)}
        if payload is None:
            # Documented "no rows" answer: the venue was asked and has nothing
            # to report for this maker and day.
            return {**empty, "source": "clob_api", "row_count": 0}
        if not isinstance(payload, list):
            return {
                **empty,
                "source": "unavailable",
                "error": (
                    f"GET {path} returned {type(payload).__name__}, "
                    "expected a JSON array or null"
                ),
            }
        rows = [row for row in payload if isinstance(row, dict)]
        by_condition: dict[str, float] = {}
        unknown: list[dict[str, Any]] = []
        total = 0.0
        for row in rows:
            amount = _as_float(row.get("rebated_fees_usdc"))
            if amount is None:
                unknown.append(row)
                continue
            key = str(row.get("condition_id") or row.get("asset_address") or "unknown")
            by_condition[key] = by_condition.get(key, 0.0) + amount
            total += amount
        return {
            "date": target_date,
            "rows": rows,
            "by_condition": by_condition,
            "total_usd": total,
            "row_count": len(rows),
            "unknown_rows": unknown,
            "source": "clob_api",
        }

    def get_daily_rewards(self, date: Optional[str] = None) -> dict[str, Any]:
        """Query one day's reward earnings from ``GET /rewards/user``.

        The endpoint is paginated (``{limit, count, next_cursor, data}`` with
        ``next_cursor == "LTE="`` on the last page) and returns per-market rows
        ``{date, condition_id, asset_address, maker_address, earnings,
        asset_rate}``. USD value per row is ``earnings * asset_rate``; a missing
        rate is treated as 1.0 only for a known USDC asset, otherwise the row is
        excluded from the USD sum and reported in ``unknown_earnings``.

        ``daily_rebates_usd`` is the wallet's own rebated fees for the same day
        from ``GET /rebates/current`` (a per-wallet entitlement, not aggregate
        maker flow); ``rebate_source`` says whether that number came from the
        venue or was unreadable. Rewards and rebates are separate programs, so
        ``daily_combined_usd`` is their sum.

        Raises nothing: on failure it returns zeros with ``source="unavailable"``
        and an ``error`` string (C2-style: never a fabricated zero marked valid).
        """
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.dry_run or not self.maker_address:
            return {
                "date": target_date,
                "daily_rewards_usd": 0.0,
                "daily_rebates_usd": 0.0,
                "daily_combined_usd": 0.0,
                "market_breakdown": [],
                "source": "simulated",
            }

        path = "/rewards/user"
        try:
            rows = self._paginate(
                path,
                {
                    "date": target_date,
                    "signature_type": self.signature_type,
                    "maker_address": self.maker_address,
                },
                max_pages=50,
            )
        except ClobAPIError as e:
            logger.warning(f"CLOB /rewards/user query failed: {e}")
            return {
                "date": target_date,
                "daily_rewards_usd": 0.0,
                "daily_rebates_usd": 0.0,
                "daily_combined_usd": 0.0,
                "market_breakdown": [],
                "source": "unavailable",
                "error": str(e),
            }

        total_usd, breakdown, unknown = _summarise_earnings(rows)
        rebates = self.get_current_rebates(target_date)
        rebate_usd = float(rebates.get("total_usd") or 0.0)
        return {
            "date": target_date,
            "daily_rewards_usd": total_usd,
            "daily_rebates_usd": rebate_usd,
            "daily_combined_usd": total_usd + rebate_usd,
            "market_breakdown": list(breakdown.values()),
            "rebate_breakdown": rebates.get("by_condition") or {},
            "rebate_source": rebates["source"],
            "rebate_error": rebates.get("error", ""),
            "source": "clob_api",
            "row_count": len(rows),
            "unknown_earnings": unknown,
        }
