"""Stack strategy modules and test the combination, not just its members.

A combination may only *tighten* the quote: each member votes, the composite
takes the most conservative action per side (`PULL` > `SHRINK` > `QUOTE`), the
smallest scale, and the price from the highest-priority member that set one.
That rule is what makes a stack auditable - adding a module can never increase
exposure, so a combination that beats its best member is a real interaction and
not a leverage artefact.

    composite = CompositeStrategy([load_strategy("a"), load_strategy("b")])
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    StrategyError,
    TradeContext,
)

_ORDER = {"QUOTE": 0, "SHRINK": 1, "PULL": 2}


class CompositeStrategy:
    """One quote from several modules; the most conservative wins."""

    def __init__(self, modules: Sequence[Any]) -> None:
        if not modules:
            raise StrategyError("composite needs at least one module")
        self.modules = list(modules)
        self.name = "composite:" + "+".join(getattr(m, "name", "?") for m in self.modules)

    # Every lifecycle hook is broadcast, so a member's state is as complete as
    # it would be on its own.
    def setup(self, market: MarketView) -> None:
        for module in self.modules:
            module.setup(market)

    def on_book(self, ctx: BookContext) -> None:
        for module in self.modules:
            module.on_book(ctx)

    def on_trade(self, ctx: TradeContext) -> None:
        for module in self.modules:
            module.on_trade(ctx)

    def decide(self, ctx: DecisionContext) -> SideActions:
        actions = [module.decide(ctx) for module in self.modules]
        return combine(actions, [getattr(m, "name", "?") for m in self.modules])


def combine(actions: Iterable[SideActions], names: Iterable[str] | None = None) -> SideActions:
    """Most conservative action per side, smallest scale, first price wins."""
    actions = list(actions)
    labels = list(names) if names is not None else [""] * len(actions)
    if not actions:
        return SideActions()

    def pick(side: str) -> tuple[str, float, float | None, str]:
        action = "QUOTE"
        scale = 1.0
        price: float | None = None
        reason = ""
        for act, label in zip(actions, labels):
            this_action = getattr(act, side)
            this_scale = float(getattr(act, f"{side}_scale"))
            if _ORDER[this_action] > _ORDER[action]:
                action = this_action
                reason = f"{label}:{this_action}"
            if this_action == "SHRINK":
                scale = min(scale, this_scale)
            elif this_action == "PULL":
                scale = 0.0
            if price is None:
                candidate = getattr(act, f"{side}_price")
                if candidate is not None:
                    price = float(candidate)
        if action == "QUOTE":
            scale = min(
                [1.0] + [float(getattr(a, f"{side}_scale")) for a in actions if getattr(a, side) == "QUOTE"]
            )
        return action, scale, price, reason

    bid, bid_scale, bid_price, bid_reason = pick("bid")
    ask, ask_scale, ask_price, ask_reason = pick("ask")
    return SideActions(
        bid=bid,
        ask=ask,
        bid_scale=bid_scale,
        ask_scale=ask_scale,
        bid_price=bid_price,
        ask_price=ask_price,
        reason=" | ".join(r for r in (bid_reason, ask_reason) if r),
    )
