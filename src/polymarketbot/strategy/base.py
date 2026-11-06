"""Strategy module protocol for the idea-replay factory.

A strategy is pure: it sees a context built from the tape replay (book, queue,
signals, own inventory) and returns per-side actions. It never does I/O, never
reads the clock, and never touches another module's state. That purity is what
lets hundreds of ideas be tested by one harness and compared on the same slice.

Modules live under ``ideas/<slug>/module.py`` and expose ``STRATEGY``. Two
built-ins live here so the harness can reproduce the known baselines.
"""

from __future__ import annotations

import importlib
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
    #: Optional price overrides. A maker's main decision is *where* to stand, so
    #: a module may join an existing level instead of improving the spread. The
    #: engine still rounds to the tick, enforces post-only and applies risk.
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
    #: Reward-relevant state of the book: competitors' band score and the score
    #: our own resting quotes currently earn. A reward-priced rule needs both to
    #: compare the dollar value of quoting against the markout it risks.
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


def load_strategy(name: str) -> StrategyModule:
    """Resolve a strategy name to a module instance.

    ``builtin:<name>`` or a bare built-in name, otherwise ``ideas.<slug>.module``.
    """
    from polymarketbot.strategy import builtin

    key = str(name).strip()
    if key.startswith("builtin:"):
        key = key.split(":", 1)[1]
    if key in builtin.BUILTINS:
        return builtin.BUILTINS[key]()

    slug = key.split(".", 1)[0]
    try:
        module = importlib.import_module(f"ideas.{slug}.module")
    except ModuleNotFoundError as exc:
        raise StrategyError(f"no strategy named {name!r} (built-ins: {sorted(builtin.BUILTINS)})") from exc
    strategy = getattr(module, "STRATEGY", None)
    if strategy is None:
        raise StrategyError(f"ideas.{slug}.module has no STRATEGY instance")
    if not isinstance(strategy, StrategyModule):
        raise StrategyError(f"ideas.{slug}.module.STRATEGY does not implement the protocol")
    return _fresh(strategy)


def _fresh(strategy: StrategyModule) -> StrategyModule:
    """A per-market instance, so one module's buffer never spans two markets.

    Modules ship a module-level ``STRATEGY`` for discovery. A replay runs one
    engine per market, and sharing that one instance would mix their state; the
    factory is called per market and must be stateless at construction.
    """
    try:
        return type(strategy)()
    except Exception:  # noqa: BLE001 - a module that needs constructor args keeps its instance
        return strategy
