"""Operator-facing market helpers used by the supervision loop."""

from __future__ import annotations

from typing import Any

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
    ids: list[str] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_id = order.get("id") or order.get("order_id") or order.get("orderId")
        ids.append(str(order_id) if order_id else "<no id in payload>")
    return ", ".join(ids) if ids else "none listed by the venue"


def _compute_competing_score(
    session: requests.Session,
    token_yes: str,
    max_spread_cents: float,
    token_no: str | None = None,
) -> float | None:
    """The venue's in-band score for the pair, or ``None`` when unmeasured.

    The pool is divided against **both** outcome-token books, so a one-sided
    reading is not the denominator the payout formula uses. A missing second
    book is missing data, never a zero contribution.
    """
    try:
        score, _, _ = get_real_l2_score(session, token_yes, max_spread_cents)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning(f"L2 competing-score refresh failed: {exc}")
        return None
    if score is None:
        return None
    total = float(score)
    if token_no:
        try:
            score_no, _, _ = get_real_l2_score(session, token_no, max_spread_cents)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log.warning(f"L2 competing-score refresh failed for the NO book: {exc}")
            return None
        if score_no is None:
            return None
        total += float(score_no)
    return total if total > 0 else None
