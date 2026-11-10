"""Operator-facing market helpers used by the supervision loop."""

from __future__ import annotations

from typing import Any, List, Optional

import requests

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.scanner import get_real_l2_score

log = get_logger("real_money_trader")


def _format_open_order_ids(orders: Any) -> str:
    """Name the venue's open orders for the operator, or say why they are unknown."""
    if orders is None:
        return "unavailable (open-order read failed)"
    if not isinstance(orders, list):
        return f"unavailable (unexpected payload {type(orders).__name__})"
    ids: List[str] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_id = order.get("id") or order.get("order_id") or order.get("orderId")
        ids.append(str(order_id) if order_id else "<no id in payload>")
    return ", ".join(ids) if ids else "none listed by the venue"


def _compute_competing_score(
    session: requests.Session, token_yes: str, max_spread_cents: float
) -> Optional[float]:
    try:
        score, _, _ = get_real_l2_score(session, token_yes, max_spread_cents)
    except Exception as exc:
        log.warning(f"L2 competing-score refresh failed: {exc}")
        return None
    return score if score is not None and score > 0 else None
