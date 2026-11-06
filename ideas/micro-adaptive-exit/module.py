"""Withdraw from a market that has stopped paying for itself.

Rewards are paid for resting; adverse selection is the price. Which of the two
dominates is market-specific and only visible in our own fills, so the rule is
adaptive: after a minimum number of fills, a market whose realised P&L is
negative is abandoned. Everything else keeps quoting.

The rule is deliberately blunt - one number, one threshold - because the
alternative (a model of markout per market) needs data this context does not
carry.
"""

from __future__ import annotations

from polymarketbot.strategy.base import DecisionContext, SideActions

MIN_FILLS = 8
EXIT_PNL = 0.0
READMIT_AFTER = 60  # decisions; kept as a constant so a withdrawn market is not permanent


class AdaptiveExit:
    name = "micro-adaptive-exit"

    def __init__(self) -> None:
        self._withdrawn = False
        self._cooldown = 0

    def setup(self, market) -> None:
        self._withdrawn = False
        self._cooldown = 0

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        if self._withdrawn:
            self._cooldown -= 1
            if self._cooldown > 0:
                return SideActions(
                    bid="PULL", ask="PULL", bid_scale=0.0, ask_scale=0.0,
                    reason=f"adaptive exit: withdrawn, {self._cooldown} left",
                )
            self._withdrawn = False

        if int(ctx.fills_count) >= MIN_FILLS and float(ctx.realized_pnl) < EXIT_PNL:
            self._withdrawn = True
            self._cooldown = READMIT_AFTER
            return SideActions(
                bid="PULL", ask="PULL", bid_scale=0.0, ask_scale=0.0,
                reason=(
                    f"adaptive exit: pnl {float(ctx.realized_pnl):+.2f} over "
                    f"{int(ctx.fills_count)} fills"
                ),
            )
        return SideActions(
            reason=f"adaptive exit: pnl {float(ctx.realized_pnl):+.2f} over {int(ctx.fills_count)} fills"
        )


STRATEGY = AdaptiveExit()
