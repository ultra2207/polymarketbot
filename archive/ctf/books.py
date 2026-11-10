"""Order-book math copied from ``polymarketbot.paper.books``.

The archived CTF pillar is self-contained: it may not import ``polymarketbot.paper``
(that package is live and moves on its own schedule).  These four helpers are a
verbatim copy of the pure functions the pillar uses - ``best_bid``, ``best_ask``,
``shares_at`` and ``ladder`` - plus the level iterator they share.  They are
pure functions of a ``{"bids": [...], "asks": [...]}`` snapshot: no I/O, no
state, no clock, so there is nothing to drift.

Levels arrive as ``{"price": str, "size": str}`` from the CLOB. Sizes are
shares. Nothing here invents a price or a size: a missing side returns
``None`` and the caller must treat it as unpriced.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

Level = tuple[float, float]  # (price, shares)


def _levels(raw: Any) -> Iterable[Level]:
    if not isinstance(raw, (list, tuple)):
        return
    for level in raw:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            yield price, size


def best_bid(book: dict[str, Any]) -> Optional[float]:
    prices = [price for price, _ in _levels(book.get("bids"))]
    return max(prices) if prices else None


def best_ask(book: dict[str, Any]) -> Optional[float]:
    prices = [price for price, _ in _levels(book.get("asks"))]
    return min(prices) if prices else None


def mid(book: dict[str, Any]) -> Optional[float]:
    bid, ask = best_bid(book), best_ask(book)
    if bid is None or ask is None or bid >= ask:
        return None
    return (bid + ask) * 0.5


def shares_at(book: dict[str, Any], *, bid: bool, price: float) -> float:
    side = book.get("bids") if bid else book.get("asks")
    return sum(size for level_px, size in _levels(side) if abs(level_px - price) <= 1e-9)


def ladder(book: dict[str, Any], *, asks: bool) -> list[Level]:
    levels = list(_levels(book.get("asks") if asks else book.get("bids")))
    return sorted(levels, key=lambda item: item[0], reverse=not asks)


def walk_cost(levels: list[Level], shares: float) -> Optional[tuple[float, float]]:
    """(cost, filled_shares) for buying/selling ``shares`` down/up the ladder."""
    remaining = float(shares)
    cost = 0.0
    filled = 0.0
    for price, size in levels:
        take = min(size, remaining)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled <= 1e-9:
        return None
    return cost, filled
