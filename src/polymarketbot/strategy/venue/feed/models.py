"""Feed value objects and raw-frame parsing helpers."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


def _parse_float(val: Any, default: float = 0.0) -> float:
    """Safely parse float from arbitrary JSON input."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _parse_opt_float(val: Any) -> float | None:
    """Parse an optional float, preserving the difference between missing and 0."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _pick(primary: Any, secondary: Any, *keys: str, default: Any = None) -> Any:
    """Read the first present, non-None field from primary then secondary.

    Polymarket frames arrive both flat and wrapped in ``payload``; values such as
    ``size: 0`` are meaningful (level removal) so presence — not truthiness —
    decides which source wins.
    """
    for source in (primary, secondary):
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
    return default


@dataclass(frozen=True)
class OrderBookLevel:
    """A single price level in an L2 order book."""

    price: float
    size: float

    def to_dict(self) -> dict[str, float]:
        return {"price": self.price, "size": self.size}


@dataclass
class OrderBookSnapshot:
    """L2 Order book snapshot with calculated market metrics."""

    asset_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    best_bid: float = 0.0
    best_ask: float = 1.0
    # ``mid``/``spread`` are only defined for a two-sided book. ``None`` means
    # this snapshot cannot be priced: an empty book used to report a 0.50 mid
    # and a zero spread, which the reward scorer reads as a real, maximally
    # scoring quote at the middle of a market that has no quotes at all.
    mid: float | None = None
    spread: float | None = None
    spread_cents: float | None = None
    tick_size: float = 0.01
    timestamp: float = field(default_factory=time.time)
    hash: str | None = None
    source: str = "ws"  # "ws" or "rest"

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "bids": [b.to_dict() for b in self.bids],
            "asks": [a.to_dict() for a in self.asks],
            "best_bid": round(self.best_bid, 4),
            "best_ask": round(self.best_ask, 4),
            "mid": None if self.mid is None else round(self.mid, 4),
            "spread": None if self.spread is None else round(self.spread, 4),
            "spread_cents": (
                None if self.spread_cents is None else round(self.spread_cents, 2)
            ),
            "tick_size": self.tick_size,
            "timestamp": self.timestamp,
            "hash": self.hash,
            "source": self.source,
        }


@dataclass(frozen=True)
class MarketTrade:
    """Public trade execution event from Polymarket tape."""

    asset_id: str
    price: float
    size: float
    side: str  # "BUY" or "SELL" (taker aggressor side)
    timestamp: float = field(default_factory=time.time)
    trade_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = "ws"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "asset_id": self.asset_id,
            "price": round(self.price, 4),
            "size": round(self.size, 4),
            "side": self.side.upper(),
            "timestamp": self.timestamp,
            "source": self.source,
        }


def parse_levels(raw_levels: Any, reverse_sort: bool = False) -> list[OrderBookLevel]:
    """Parse bids or asks into sorted OrderBookLevel list.

    Bids should be reverse_sort=True (highest price first).
    Asks should be reverse_sort=False (lowest price first).
    """
    levels: list[OrderBookLevel] = []
    if not isinstance(raw_levels, list):
        return levels

    for item in raw_levels:
        p: float = 0.0
        s: float = 0.0
        if isinstance(item, dict):
            p = _parse_float(item.get("price"))
            s = _parse_float(item.get("size"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            p = _parse_float(item[0])
            s = _parse_float(item[1])
        if 0.0 < p < 1.0 and s > 0.0:
            levels.append(OrderBookLevel(price=round(p, 6), size=round(s, 6)))

    levels.sort(key=lambda lvl: lvl.price, reverse=reverse_sort)
    return levels


def build_book_snapshot(
    asset_id: str,
    raw_bids: Any,
    raw_asks: Any,
    tick_size: float = 0.01,
    book_hash: str | None = None,
    timestamp: float | None = None,
    source: str = "ws",
) -> OrderBookSnapshot:
    """Construct a validated, sorted OrderBookSnapshot from raw bid/ask data."""
    bids = parse_levels(raw_bids, reverse_sort=True)
    asks = parse_levels(raw_asks, reverse_sort=False)

    best_bid = bids[0].price if bids else 0.0
    best_ask = asks[0].price if asks else 1.0

    # A mid needs both sides. One-sided and empty books report ``None`` so a
    # caller prices a real quote or refuses; there is no stand-in mid.
    if best_bid > 0.0 and best_ask < 1.0 and best_ask >= best_bid:
        mid: float | None = round((best_bid + best_ask) / 2.0, 4)
        spread: float | None = round(best_ask - best_bid, 4)
    else:
        mid = None
        spread = None

    spread_cents: float | None = None if spread is None else round(spread * 100.0, 2)
    ts = timestamp if timestamp is not None else time.time()

    return OrderBookSnapshot(
        asset_id=asset_id,
        bids=bids,
        asks=asks,
        best_bid=round(best_bid, 4),
        best_ask=round(best_ask, 4),
        mid=mid,
        spread=spread,
        spread_cents=spread_cents,
        tick_size=tick_size,
        timestamp=ts,
        hash=book_hash,
        source=source,
    )


BookCallback = Callable[[OrderBookSnapshot], None | Awaitable[None]]
TradeCallback = Callable[[MarketTrade], None | Awaitable[None]]
