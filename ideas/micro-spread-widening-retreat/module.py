"""Pull when the book widens without the mid moving: the makers know something.

A genuine repricing drags the mid with the spread; a liquidity withdrawal leaves
the mid where it was while the touch falls away.  The withdrawal is the visible
half of a cancel decision by makers who expect to be picked off, so a quote left
inside the now-wide spread is the cheapest thing for the informed taker to hit.
The module spends five decisions out of the book after such an episode, then
resumes.

Rule: keep one 50-observation ring of ``(spread_cents, mid)`` per asset.  If the
current spread is at least two ticks wider than the median of the prior
observations *and* the mid has not drifted more than half a tick from the oldest
buffered mid, pull both sides.  The triggering decision is the first of five
pulls; afterwards the module quotes again and re-arms only once the spread has
returned to its median, so one wide book is one window, not a permanent exit.
A widening that comes with a move is a repricing (the jump rule's mechanism),
not a withdrawal, and is ignored here.

Only ``spread_cents``, ``mid`` and ``tick_size`` are read.  The module never
looks at ``*_size_at_level`` or ``*_queue_ratio``, which are zero for the
baseline's inside-the-spread quotes, so the rule can fire on real replay data.

The module is a harness singleton: every engine in a replay shares one
``STRATEGY`` instance, so state is keyed by asset id and ``setup`` only clears
the tokens of the market it is called for.  Reads only the DecisionContext; no
clock, no tape, no engine internals.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median

from polymarketbot.strategy.base import DecisionContext, SideActions

#: Observations kept per asset: the quiet-state spread reference.
WINDOW = 50
#: Below this many observations there is no reference, so quote.
MIN_OBSERVATIONS = 20
#: A widening this many ticks over the median counts as other makers leaving.
WIDEN_TICKS = 2.0
#: Mid drift allowed between the oldest buffered mid and now, in ticks.  A mid
#: that moves is a repricing, not a withdrawal.
STATIONARY_TICKS = 0.5
#: Decisions to stay out of the book after the widening, triggering call included.
COOLDOWN_DECISIONS = 5

_EPS = 1e-9


@dataclass
class _AssetState:
    """Recent book evidence and the pull window for one asset."""

    observations: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=WINDOW)
    )
    cooldown: int = 0
    armed: bool = True


class SpreadWideningRetreat:
    """Pull both sides for five decisions after a flat-mid spread widening."""

    name = "micro-spread-widening-retreat"

    def __init__(self) -> None:
        self._state: dict[str, _AssetState] = {}

    def setup(self, market) -> None:
        # Only the tokens this setup owns may be reset: every engine shares this
        # instance during a replay.
        tokens = getattr(market, "tokens", ()) or ()
        for token in tokens:
            self._state.pop(str(token), None)

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        book = ctx.book
        spread = float(book.spread_cents)
        mid = float(book.mid)
        tick = max(float(book.tick_size), _EPS)
        tick_cents = round(tick * 100.0, 6)
        state = self._state.setdefault(str(book.asset_id), _AssetState())
        history = state.observations

        # The reference excludes the observation being judged: the first wide
        # update must be compared with the quiet book, not with a median that
        # already contains it.
        reference = median([s for s, _ in history]) if history else spread
        widened = (
            len(history) >= MIN_OBSERVATIONS
            and spread + _EPS >= reference + WIDEN_TICKS * tick_cents
        )
        drift = abs(mid - history[0][1]) if history else 0.0
        stationary = drift <= STATIONARY_TICKS * tick + _EPS

        if state.cooldown > 0:
            if widened:
                state.cooldown -= 1
                history.append((spread, mid))
                return _pull(spread, reference, state.cooldown)
            # The spread came back to its median: no reason to stay out early.
            # The not-widened branch below re-arms, so a later widening is new.
            state.cooldown = 0

        if len(history) < MIN_OBSERVATIONS:
            history.append((spread, mid))
            return SideActions(
                reason=(
                    f"widening retreat: warm-up {len(history)}/{MIN_OBSERVATIONS}"
                )
            )

        if state.cooldown == 0 and state.armed and widened and stationary:
            state.armed = False  # one window per episode; re-armed at the median
            state.cooldown = COOLDOWN_DECISIONS - 1  # this call is the first pull
            history.append((spread, mid))
            return _pull(spread, reference, state.cooldown)

        if not widened:
            state.armed = True
        history.append((spread, mid))
        return SideActions(
            reason=(
                f"widening retreat: spread {spread:.2f}c vs median {reference:.2f}c, "
                f"mid drift {drift / tick:.2f} ticks"
            )
        )


def _pull(spread: float, reference: float, remaining: int) -> SideActions:
    return SideActions(
        bid="PULL",
        ask="PULL",
        bid_scale=0.0,
        ask_scale=0.0,
        reason=(
            f"widening retreat: spread {spread:.2f}c vs median {reference:.2f}c, "
            f"{remaining} pull(s) left"
        ),
    )


STRATEGY = SpreadWideningRetreat()
