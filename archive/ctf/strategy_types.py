"""Strategy interface types, copied out of ``polymarketbot.strategy.base``.

``trading/complement_quoter.py`` implemented the idea-replay harness protocol
(``StrategyModule``).  That harness was deleted with the archived strategy
package, and archive code may not reach back into live packages for types it
only annotates with, so the frozen context dataclasses and the runtime-checkable
protocol are copied here verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

ACTIONS = ("QUOTE", "SHRINK", "PULL")


class StrategyError(RuntimeError):
    """A module broke the protocol; the harness rejects it, it does not guess."""


@dataclass(frozen=True)
class SideActions:
    """Per-side instruction for one decision step."""

    bid: str = "QUOTE"
    ask: str = "QUOTE"
    bid_scale: float = 1.0
    ask_scale: float = 1.0
    reason: str = ""
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None

    def __post_init__(self) -> None:
        for side, action in (("bid", self.bid), ("ask", self.ask)):
            if action not in ACTIONS:
                raise StrategyError(f"{side} action {action!r} not in {ACTIONS}")
        for name, scale in (("bid_scale", self.bid_scale), ("ask_scale", self.ask_scale)):
            if not 0.0 <= float(scale) <= 1.0:
                raise StrategyError(f"{name}={scale!r} outside [0, 1]")
        for name, price in (("bid_price", self.bid_price), ("ask_price", self.ask_price)):
            if price is not None and not 0.0 < float(price) < 1.0:
                raise StrategyError(f"{name}={price!r} outside (0, 1)")


@dataclass(frozen=True)
class MarketView:
    """Static market facts a module may read at setup."""

    condition_id: str
    question: str
    rate_per_day: float
    rewards_min_size: float
    rewards_max_spread: float
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class BookContext:
    """Top of book after one update."""

    condition_id: str
    asset_id: str
    outcome: str
    mid: float
    best_bid: float
    best_ask: float
    tick_size: float
    spread_cents: float
    competing_score: float = 0.0


@dataclass(frozen=True)
class TradeContext:
    """One print on our market."""

    asset_id: str
    price: float
    size: float
    side: str
    seconds_elapsed: float


@dataclass(frozen=True)
class DecisionContext:
    """Everything a module needs to decide a side, and nothing else."""

    book: BookContext
    seconds_elapsed: float
    inventory_shares: float
    max_inventory_shares: float
    bid_queue_ratio: float
    ask_queue_ratio: float
    bid_size_at_level: float
    ask_size_at_level: float
    ofi: Optional[float] = None
    realized_pnl: float = 0.0
    fills_count: int = 0
    quote_size: float = 0.0
    bid_score: float = 0.0
    ask_score: float = 0.0
    rate_per_day: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)


@runtime_checkable
class StrategyModule(Protocol):
    name: str

    def setup(self, market: MarketView) -> None: ...

    def on_book(self, ctx: BookContext) -> None: ...

    def on_trade(self, ctx: TradeContext) -> None: ...

    def decide(self, ctx: DecisionContext) -> SideActions: ...
