"""Built-in strategy modules: the two baselines every idea must beat.

``reactionary`` quotes both sides unconditionally — the "do nothing clever"
control. ``queue_aware_ofi`` is the current production candidate: it keeps both
sides at the front of the queue and shrinks the threatened side at the back.
Reproducing both on the same slice is the harness's own acceptance test.
"""

from __future__ import annotations

from polymarketbot.paper_trading.orchestrator import (
    OrchestratorConfig,
    QueueSnapshot,
    QuoteOrchestrator,
)
from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)


class Reactionary:
    """Quote both sides, always. The control arm."""

    name = "reactionary"

    def setup(self, market: MarketView) -> None:
        return None

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        return SideActions(reason="reactionary: both sides")


class QueueAwareOFI:
    """The queue-aware orchestrator, exposed as a module."""

    name = "queue_aware_ofi"

    def __init__(self) -> None:
        self._orchestrator = QuoteOrchestrator(
            OrchestratorConfig(enabled=True, policy="queue_aware", signal="ofi")
        )

    def setup(self, market: MarketView) -> None:
        return None

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        queue = QueueSnapshot(
            bid_queue_ratio=ctx.bid_queue_ratio,
            bid_size_at_level=ctx.bid_size_at_level,
            ask_queue_ratio=ctx.ask_queue_ratio,
            ask_size_at_level=ctx.ask_size_at_level,
        )
        decision = self._orchestrator.decide(ctx.ofi, queue)
        return SideActions(
            bid=decision.bid.action,
            ask=decision.ask.action,
            bid_scale=decision.bid.size_scale,
            ask_scale=decision.ask.size_scale,
            reason=decision.bid.reason,
        )


BUILTINS = {
    "reactionary": Reactionary,
    "queue_aware_ofi": QueueAwareOFI,
}
