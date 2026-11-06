"""Substitute the sibling token instead of dropping a pair side.

The venue scores a condition, not a token: Q1 pairs YES bids with NO asks and
Q2 pairs YES asks with NO bids, and the epoch pays ``min(Q1, Q2)`` (with the
c = 3 single-sided factor inside the mid band).  So when one token's side is
protected, pulling it does not remove "half" the reward - it removes a whole
term of a minimum, while the economically identical exposure on the sibling
token is untouched.  When one quote has nothing to lose, the same score is
available by standing at full size on that side; when both ends of a pair are
protected, at least one of them must stay quoted, otherwise the pair pays
two-thirds of what it already earns.

The module is called once per token and never sees the sibling book.  It reads
two prepared signals, which are constant ``1.0``/``0.0`` style flags per asset:

``sibling_scale``
    ``1.0`` when the sibling token is fully quoted, ``0.0`` when it is pulled.
    Missing means ``1.0`` - no prepared signal is not evidence of a pull.
``sibling_threat``
    ``> 0`` when the sibling token is under its own protective rule.  Missing
    means ``0`` - the module must degrade to the baseline, not guess.

Rule, per side (the two sides are decided independently, each with its own
``bid_score``/``ask_score``):

1. Sibling pulled *and* this side's own score is zero: QUOTE at full scale.
   Nobody else is carrying this side of the pair, so full size is the only way
   the Q term survives, and there is no earned score to protect.
2. Sibling threatened *and* this side is threatened: QUOTE at full scale.
   The sibling is already shrinking the mirrored leg; shrinking this one too is
   the one case that makes both pair ends small, which the Q_min rule punishes
   twice over.
3. Sibling threatened and this side is not: SHRINK to ``0.01``.  The engine
   floors any positive size at ``rewards_min_size``, so the quote stays
   reward-eligible while the redundant exposure on this token comes off.  The
   sibling keeps its threatened leg at full (rule 2 applied there), so the pair
   is never one-sided.
4. Otherwise: QUOTE at full scale.  No sibling threat means there is nothing to
   mirror and nothing to gain by reducing size.

"This side is threatened" is read from the context, not from another signal:
the side the order-flow imbalance points at (``ofi > 0`` threatens the ask,
``ofi < 0`` threatens the bid), or a side whose queue ratio is at least 0.75.
The queue test is included for live books, but the baseline quotes rest inside
the spread, where the ratio is zero, so in a replay the OFI sign is the live
input; ``*_size_at_level`` and ``*_queue_ratio`` are never required to fire.

The module never PULLs.  A pull zeroes a side and, under the venue's
normalisation, costs two-thirds of that side's score; ``0.01`` keeps the quote
eligible at the minimum size, which is the whole point of this wave.  It reads
only the ``DecisionContext`` (signals, scores, OFI, queue ratios); no clock, no
tape, no engine internals.
"""

from __future__ import annotations

from polymarketbot.strategy.base import DecisionContext, SideActions

#: Scale that keeps a side alive at ``rewards_min_size`` (the engine floors it).
SHRINK_SCALE = 0.01
#: Fully quoted side.
FULL_SCALE = 1.0
#: Queue ratio at which the measured fill markout turns negative at the back.
QUEUE_THREAT_RATIO = 0.75
#: OFI magnitude above which the sign names the threatened side.  The baseline
#: gate uses ``0.0``; a decision-level mean smaller than a share is noise but
#: still directional, and the guard only preserves reward, never adds risk.
OFI_THREAT = 0.0

_EPS = 1e-9


def _side_threatened(side: str, ctx: DecisionContext) -> bool:
    """Whether this token's own book names ``side`` as the threatened one."""
    ratio = ctx.ask_queue_ratio if side == "ask" else ctx.bid_queue_ratio
    if float(ratio) >= QUEUE_THREAT_RATIO - _EPS:
        return True
    ofi = ctx.ofi
    if ofi is None or abs(float(ofi)) <= OFI_THREAT:
        return False
    return float(ofi) > 0.0 if side == "ask" else float(ofi) < 0.0


class CrossTokenSubstitution:
    """Keep at least one end of a Q pair quoted when the sibling is protected."""

    name = "micro-cross-token-substitution"

    def setup(self, market) -> None:
        # Stateless by design: the decision is a pure function of the context,
        # so a shared STRATEGY instance across replay engines cannot leak state.
        return None

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        signals = ctx.signals or {}
        # A missing signal degrades to "sibling quoted, not threatened": the
        # baseline, never a guess that would shrink a side on absent data.
        sibling_scale = float(signals.get("sibling_scale", FULL_SCALE))
        sibling_threat = float(signals.get("sibling_threat", 0.0)) > 0.0
        sibling_pulled = sibling_scale <= _EPS

        bid = self._side("bid", float(ctx.bid_score), ctx, sibling_pulled, sibling_threat)
        ask = self._side("ask", float(ctx.ask_score), ctx, sibling_pulled, sibling_threat)
        return SideActions(
            bid=bid[0],
            ask=ask[0],
            bid_scale=bid[1],
            ask_scale=ask[1],
            reason=f"cross-token substitution: {bid[2]}; {ask[2]}",
        )

    @staticmethod
    def _side(
        side: str,
        score: float,
        ctx: DecisionContext,
        sibling_pulled: bool,
        sibling_threat: bool,
    ) -> tuple[str, float, str]:
        if sibling_pulled and score <= _EPS:
            return (
                "QUOTE",
                FULL_SCALE,
                f"{side} full (sibling pulled, own score 0; carry the pair)",
            )
        if sibling_threat and _side_threatened(side, ctx):
            return (
                "QUOTE",
                FULL_SCALE,
                f"{side} full (sibling and own {side} threatened; never shrink both)",
            )
        if sibling_threat:
            return (
                "SHRINK",
                SHRINK_SCALE,
                f"{side} at floor (sibling threatened; keep reward-eligible)",
            )
        return "QUOTE", FULL_SCALE, f"{side} full (no sibling threat)"


STRATEGY = CrossTokenSubstitution()
