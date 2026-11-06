"""The control for every participation rule: same duty cycle, no information.

If standing down when the touch is hostile earns money, then standing down at
random for the same fraction of decisions must not. Without this control, a
positive result from any participation gate cannot be told apart from the plain
fact that quoting less on a market with adverse markout is worth something.

The duty cycle is fixed at the rate the gates were observed to fire (about one
decision in eight), so the two are compared at equal exposure.
"""

from __future__ import annotations

import random

from polymarketbot.strategy.base import DecisionContext, SideActions

#: Fraction of decisions spent out of the market, matched to the measured
#: firing rate of the participation gates.
STAND_DOWN_FRACTION = 0.125
PULL_FOR_DECISIONS = 3


class RandomParticipationControl:
    name = "micro-random-participation-control"

    def __init__(self, seed: int = 7) -> None:
        self._rng = random.Random(seed)
        self._left = 0

    def setup(self, market) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_book(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_trade(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        if self._left > 0:
            self._left -= 1
            return SideActions(bid="PULL", ask="PULL", reason="random stand-down")
        if self._rng.random() < STAND_DOWN_FRACTION:
            self._left = PULL_FOR_DECISIONS - 1
            return SideActions(bid="PULL", ask="PULL", reason="random stand-down")
        return SideActions(bid="QUOTE", ask="QUOTE", reason="random quote")


STRATEGY = RandomParticipationControl()
