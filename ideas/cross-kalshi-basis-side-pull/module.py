"""Kalshi basis threatens one side of the Polymarket quote.

The basis is `logit(Kalshi YES) - logit(PM YES)`. Kalshi above Polymarket means
Polymarket has to reprice up, so our resting ask is the side at risk; Kalshi below
means the bid is. The module shrinks (never pulls: a one-sided book forfeits
two-thirds of its score under the venue's normalisation) the threatened side
when the basis clears the threshold and is fresh.

Reads only the context. The signal is prepared from the tape by
`prepare.sql` -> `signals.parquet` and joined as-of by the harness, so the module
cannot see a basis that had not been observed yet.
"""

from __future__ import annotations

from polymarketbot.strategy.base import DecisionContext, SideActions

# The spec proposed +/-3 logit points. Measured on 6,183 as-of rows over two
# hours, |basis| has std 0.30 and never exceeds 1.72, so 3.0 is unreachable and
# the module could never fire. 0.5 is ~1.7 sigma of that distribution; the
# calibration window and the test slices overlap, so this is a hypothesis to
# hold out on another day, not a tuned parameter.
THRESHOLD = 0.5
MAX_AGE_S = 20.0
SHRINK = 0.01


class KalshiBasisSidePull:
    name = "cross-kalshi-basis-side-pull"

    def setup(self, market) -> None:
        return None

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        basis = ctx.signals.get("basis")
        age = ctx.signals.get("pm_age_s")
        if basis is None:
            return SideActions(reason="basis: no signal")
        if age is not None and float(age) > MAX_AGE_S:
            return SideActions(reason=f"basis: stale ({float(age):.0f}s)")
        # A NO quote moves opposite to the condition's YES probability.
        value = -float(basis) if str(ctx.book.outcome).strip().upper() == "NO" else float(basis)
        if abs(value) < THRESHOLD:
            return SideActions(reason=f"basis: {value:+.2f} inside +/-{THRESHOLD}")
        threatened = "ask" if value > 0 else "bid"
        return SideActions(
            bid="SHRINK" if threatened == "bid" else "QUOTE",
            ask="SHRINK" if threatened == "ask" else "QUOTE",
            bid_scale=SHRINK if threatened == "bid" else 1.0,
            ask_scale=SHRINK if threatened == "ask" else 1.0,
            reason=f"basis: {value:+.2f} threatens the {threatened}",
        )


STRATEGY = KalshiBasisSidePull()
