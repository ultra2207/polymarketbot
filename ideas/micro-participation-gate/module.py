"""Stand down when the touch is hostile, on the union of three measured conditions.

Each of the three detectors below was measured separately on the liquid universe
and each returned a positive but individually insignificant effect (t of 1.0-1.7).
They are not independent mechanisms: all three are evidence that the flow about
to take our quote is aggressive rather than passive, so the gate is the union
rather than a vote. Standing down on any one is the cautious reading.

- a burst of prints inside a few seconds is a parent order being sliced,
- a print that eats half its level is aggression that usually continues,
- a level that shrinks and never refills is a taker who is still going.
"""

from __future__ import annotations

from collections import deque

from polymarketbot.strategy.base import DecisionContext, SideActions, TradeContext

#: Burst detector: prints inside this window define a burst.
BURST_WINDOW_SECONDS = 3.0
BURST_PRINTS = 4
COOLDOWN_MULTIPLE = 4.0
MAX_COOLDOWN_SECONDS = 120.0

#: Sweep detector: fraction of the resting level a print must consume.
CONSUMED_FRACTION = 0.5
SWEEP_COOLDOWN = 10

#: Absorption detector.
REFILL_FRACTION = 0.6
EMPTYING_STREAK = 3


class ParticipationGate:
    name = "micro-participation-gate"

    def __init__(self) -> None:
        self._prints: deque[float] = deque()
        self._until = 0.0
        self._sweep_left = 0
        self._prev = {"bid": 0.0, "ask": 0.0}
        self._peak = {"bid": 0.0, "ask": 0.0}
        self._streak = {"bid": 0, "ask": 0}
        self._absorbed = {"bid": True, "ask": True}

    def setup(self, market) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_book(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        now = float(ctx.seconds_elapsed)
        self._prints.append(now)
        while self._prints and now - self._prints[0] > BURST_WINDOW_SECONDS:
            self._prints.popleft()
        if len(self._prints) >= BURST_PRINTS:
            span = max(1e-6, now - self._prints[0])
            self._until = max(self._until, now + min(MAX_COOLDOWN_SECONDS, COOLDOWN_MULTIPLE * span))

        # Consumption is judged against the level this print hit.
        resting = "bid" if str(ctx.side).upper() == "SELL" else "ask"
        level = self._prev[resting]
        if level > 0.0 and float(ctx.size) >= CONSUMED_FRACTION * level:
            self._sweep_left = SWEEP_COOLDOWN

    def _update(self, side: str, level: float) -> None:
        prev = self._prev[side]
        if level > prev:
            eaten = self._peak[side] - prev
            if eaten > 0 and (level - prev) >= REFILL_FRACTION * eaten:
                self._streak[side] = 0
                self._absorbed[side] = True
        elif level < prev:
            self._streak[side] += 1
            if self._streak[side] >= EMPTYING_STREAK:
                self._absorbed[side] = False
        else:
            self._streak[side] = 0
        self._prev[side] = level
        self._peak[side] = max(self._peak[side], level)

    def decide(self, ctx: DecisionContext) -> SideActions:
        self._update("bid", float(ctx.bid_size_at_level))
        self._update("ask", float(ctx.ask_size_at_level))

        now = float(ctx.seconds_elapsed)
        if self._sweep_left > 0:
            self._sweep_left -= 1
        if now < self._until:
            return SideActions(
                bid="PULL", ask="PULL",
                reason=f"print burst, {self._until - now:.1f}s left",
            )
        if self._sweep_left > 0:
            return SideActions(
                bid="SHRINK", ask="SHRINK", bid_scale=0.01, ask_scale=0.01,
                reason=f"sweep, {self._sweep_left} decisions left",
            )
        bid = "QUOTE" if self._absorbed["bid"] else "PULL"
        ask = "QUOTE" if self._absorbed["ask"] else "PULL"
        return SideActions(bid=bid, ask=ask, reason="absorbed flow")


STRATEGY = ParticipationGate()
