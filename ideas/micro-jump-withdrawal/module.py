"""Pull on the jump: a two-tick move in three events is information, not drift.

The tape is event-driven, so a fast repricing is someone acting before us. The
trailing OFI mean needs a 60 s window and cannot see it; this module reacts on
the event and stays out for five events while the engine re-prices.

Reads only the DecisionContext; no clock, no tape.
"""

from __future__ import annotations

from polymarketbot.strategy.base import DecisionContext, SideActions

TICKS = 2.0
EVENTS = 3
COOLDOWN = 5


class JumpWithdrawal:
    name = "micro-jump-withdrawal"

    def __init__(self) -> None:
        self._mid: float | None = None
        self._fast: list[float] = []
        self._cooldown = 0
        self._armed = True

    def setup(self, market) -> None:
        self._mid = None
        self._fast = []
        self._cooldown = 0
        self._armed = True

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        mid = float(ctx.book.mid)
        tick = max(0.01, float(ctx.book.tick_size))
        self._fast.append(mid)
        self._fast = self._fast[-(EVENTS + 1):]

        if self._mid is None:
            self._mid = mid
        jumped = abs(mid - self._mid) >= TICKS * tick

        if jumped and self._armed:
            self._cooldown = COOLDOWN
            self._armed = False  # a second, larger move is a new jump, not an extension
            self._mid = mid
        elif not jumped:
            self._armed = True

        if len(self._fast) > 1 and self._fast[-1] != self._fast[-2]:
            self._mid = mid

        if self._cooldown > 0:
            self._cooldown -= 1
            width = max(self._fast) - min(self._fast)
            return SideActions(bid="PULL", ask="PULL", bid_scale=0.0, ask_scale=0.0,
                               reason=f"jump withdrawal: {width/tick:.1f} ticks, {self._cooldown} left")
        return SideActions(reason="jump withdrawal: quoting")


STRATEGY = JumpWithdrawal()
