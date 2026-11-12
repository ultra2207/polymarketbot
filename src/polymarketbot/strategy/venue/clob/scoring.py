"""Reward-scoring telemetry endpoints."""

from __future__ import annotations

from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import ClobAPIError

logger = get_logger("clob_client")


class ScoringMixin:
    """Order scoring and market reward configuration."""


    def get_order_scoring(self, order_id: str) -> dict[str, Any]:
        """Query live qualifying maker reward scoring for one order.

        Queries CLOB ``GET /order-scoring?order_id=<id>``. Returns ``scoring`` as
        True/False when the exchange answered; on failure it returns
        ``scoring=None`` plus an ``error`` string (never a bare False, which would
        look like "answered: not scoring").
        """
        if not order_id:
            return {"order_id": "", "scoring": False, "dry_run": self.dry_run}

        if self.dry_run:
            is_mock = order_id in self._mock_orders
            return {
                "order_id": order_id,
                "scoring": is_mock,
                "dry_run": True,
                "raw_response": {"scoring": is_mock, "mock": True},
            }

        path = "/order-scoring"
        try:
            data = self._request("GET", path, params={"order_id": order_id})
            if not isinstance(data, dict):
                raise ClobAPIError(
                    f"GET {path} returned a non-object body",
                    status_code=200,
                    path=path,
                )
            return {
                "order_id": order_id,
                "scoring": bool(data.get("scoring", False)),
                "dry_run": False,
                "raw_response": data,
            }
        except ClobAPIError as e:
            logger.error(f"Failed to query order scoring for {order_id}: {e}")
            return {
                "order_id": order_id,
                "scoring": None,
                "dry_run": False,
                "error": str(e),
                "raw_response": {"status_code": e.status_code},
            }

    def get_market_reward_config(self, condition_id_or_market_id: str) -> dict[str, Any] | None:
        """Query live official reward parameters from CLOB ``/rewards/user/markets``.

        Returns the config dict, or None when the market is absent from the
        response. A transport failure is logged and also returns None (the
        caller cannot distinguish it), so treat None as "unknown", not "no rewards".
        """
        if not condition_id_or_market_id:
            return None

        key = str(condition_id_or_market_id).lower()
        path = "/rewards/user/markets"

        try:
            data = self._request("GET", path, auth=self.credentials.is_configured)
            markets = data.get("data", []) if isinstance(data, dict) else []
            for m in markets:
                cid = str(m.get("condition_id", "")).lower()
                mid = str(m.get("market_id", "")).lower()
                if key in (cid, mid):
                    cfg = m.get("rewards_config", [{}])[0] if m.get("rewards_config") else {}
                    return {
                        "condition_id": m.get("condition_id"),
                        "market_id": m.get("market_id"),
                        "question": m.get("question"),
                        "rate_per_day": float(cfg.get("rate_per_day", 0.0) or 0.0),
                        "rewards_max_spread": float(m.get("rewards_max_spread", 0.0) or 0.0),
                        "rewards_min_size": float(m.get("rewards_min_size", 0.0) or 0.0),
                        "market_competitiveness": float(m.get("market_competitiveness", 0.0) or 0.0),
                        "earning_percentage": float(m.get("earning_percentage", 0.0) or 0.0),
                        "earnings": m.get("earnings", []),
                    }
        except (ClobAPIError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"Failed to fetch market reward config: {e}")

        return None
