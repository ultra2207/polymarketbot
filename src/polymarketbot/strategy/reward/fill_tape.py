"""What the reward fill rule saw and booked, per quote-tick.

The rule is a pure function of the tape and the resting size; this module owns
the one rolling tally the engine flushes to the log once a minute, so a
zero-fill window stays diagnosable from the log alone.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.reads import Print

log = get_logger("polymarketbot.strategy.reward.decision")


@dataclass(frozen=True)
class FillDecision:
    """The fill rule's complete breakdown for one resting bid.

    ``prints`` is every tape row the caller passed for the quoted token,
    ``crossing`` the SELL rows that printed at the quote's own price, ``above``
    and ``below`` the SELL rows that printed above and below it, ``flow_shares``
    the size of the crossing rows and ``shares`` what the rule books after the
    queue share. Units: shares, except ``queue_share`` (a fraction).
    """

    prints: int
    crossing: int
    above: int
    below: int
    flow_shares: float
    queue_share: float
    shares: float


_EMPTY_DECISION = FillDecision(0, 0, 0, 0, 0.0, 0.0, 0.0)


def fill_audit(
    quote_price: float,
    our_size: float,
    level_shares: float,
    prints: list[Print],
    tick: float,
) -> FillDecision:
    """Classify one resting bid's tape against its own price. Pure.

    Separating the classification from the booking is what makes a zero-fill
    window diagnosable: ``crossing`` (prints at our price) is the numerator the
    venue gives us, and ``below`` (prints that swept past our price) is the
    flow that proves our order was not standing where the tape printed.
    """
    prints_seen = len(prints)
    if our_size <= 0.0 or level_shares < 0.0 or tick <= 0.0:
        return dataclasses.replace(_EMPTY_DECISION, prints=prints_seen)
    denominator = level_shares + our_size
    if denominator <= 0.0:
        return dataclasses.replace(_EMPTY_DECISION, prints=prints_seen)
    queue_share = min(1.0, our_size / denominator)
    tolerance = tick * 0.5 + 1e-12
    crossing = above = below = 0
    flow = 0.0
    for trade in prints:
        if str(trade.side).upper() != "SELL":
            continue
        delta = trade.price - quote_price
        if abs(delta) <= tolerance:
            crossing += 1
            flow += trade.size
        elif delta > tolerance:
            above += 1
        else:
            below += 1
    return FillDecision(prints_seen, crossing, above, below, flow, queue_share,
                        max(0.0, flow) * queue_share)


@dataclass
class FillTape:
    """Rolling tally of what the fill rule saw, flushed to the log every minute.

    A zero-fill window is only interpretable with its inputs: the engine logs a
    fill when it books one and nothing at all when it books none, so four
    consecutive zero-fill windows could not be told apart from a broken gate.
    This tally answers it from the log alone: ``evaluations`` (quote-ticks),
    ``with_flow`` (evaluations whose token printed at all), ``prints`` seen,
    ``crossing`` prints at the quote's price, their ``flow_shares``, and the
    shares actually ``booked`` for accumulation. A window with
    ``crossing == 0`` is absent flow; a window with ``crossing > 0`` and
    ``booked < min_order`` is the gate, and the two are different bugs.
    """

    window_s: float = 60.0
    evaluations: int = 0
    with_flow: int = 0
    prints: int = 0
    crossing: int = 0
    above: int = 0
    below: int = 0
    flow_shares: float = 0.0
    booked_shares: float = 0.0
    since: float = 0.0

    def add(self, decision: FillDecision) -> None:
        self.evaluations += 1
        self.prints += decision.prints
        self.crossing += decision.crossing
        self.above += decision.above
        self.below += decision.below
        self.flow_shares += decision.flow_shares
        self.booked_shares += decision.shares
        if decision.prints > 0:
            self.with_flow += 1

    def flush(self, now: float) -> None:
        """Log and reset once a window has elapsed and something was evaluated."""
        if self.evaluations == 0 or now - self.since < self.window_s:
            return
        log.info(
            f"reward fill tape: {self.evaluations} evaluations, "
            f"{self.with_flow} with flow, {self.prints} prints seen, "
            f"{self.crossing} crossing ({self.flow_shares:.1f} sh at the quote), "
            f"{self.below} below, {self.above} above, "
            f"{self.booked_shares:.1f} sh booked for accumulation"
        )
        self.evaluations = self.with_flow = self.prints = 0
        self.crossing = self.above = self.below = 0
        self.flow_shares = self.booked_shares = 0.0
        self.since = now


_FILL_TAPE = FillTape()


def fill_tape() -> FillTape:
    """The live tally, for tests and for a caller that wants its own snapshot."""
    return _FILL_TAPE


def reset_fill_tape(now: float = 0.0) -> FillTape:
    """Clear the tally so a measurement starts from zero."""
    global _FILL_TAPE
    _FILL_TAPE = FillTape(since=now)
    return _FILL_TAPE


def measured_fill_shares(
    quote_price: float,
    our_size: float,
    level_shares: float,
    prints: list[Print],
    tick: float,
) -> float:
    """Expected shares filled on a resting bid at ``quote_price``.

    A print fills this bid only when it **printed at this bid's own price**
    (``|print.price - quote_price| <= tick / 2``): the tape records the maker
    price a taker actually got, so a SELL print at our price is a taker who hit
    the level we are standing on, and our share of that level's flow is
    ``our_size / (level_shares + our_size)``.

    A SELL print strictly *below* our price is not our fill and must not be
    counted: it means the taker reached a lower bid, so our order was not
    resting there (a bid above the best bid is hit first, so the print would
    have been at our price). The tape's ``price`` is a size-weighted average
    over the levels a print swept - the window's prints read 0.2982491384, not
    a tick multiple - so a print that only partly cleared at our level cannot
    be recognised and is missed. That is the safe direction: it can only
    under-count our fills. On a book snapshot that has gone stale - and the
    engine's book set is up to two minutes old - ``best_bid + tick`` sits above
    the live best bid most of the time, and every print below it belongs to
    somebody else. Counting those prints fabricated both fills of the
    2026-11-10 04:22-04:52 window: 20.31 shares booked at 0.321 against a tape
    that printed 20.31 at 0.298, and 20.46 booked at 0.259 against a print of
    20.46 at 0.258. Both were the full print size because the level at
    ``best_bid + tick`` is empty in 817/817 sampled markets, so the queue share
    is 1.0 and the old rule claimed 100 % of all sell flow.

    ``prints`` is the tape of the quoted token; returns 0.0 when it shows no
    sell flow at the quote.

    The caller owns the print window: it must pass the prints that arrived
    *after* this quote was placed and *before* this evaluation, and it must not
    pass the same print twice, because this function is stateless and would
    count that flow again. A tape snapshot older than the placement time is not
    a measurement of the quote's flow; it is missing data.

    Every call records its :class:`FillDecision` in :data:`_FILL_TAPE`, which
    logs the distribution once a minute; see :func:`fill_tape`. The rule itself
    stays a pure function of its arguments.

    Units: ``our_size`` and ``level_shares`` are shares resting at the quote;
    ``prints`` sizes are shares; ``tick`` is USDC per share; the result is
    shares per tape window.
    """
    decision = fill_audit(quote_price, our_size, level_shares, prints, tick)
    tape = _FILL_TAPE
    tape.add(decision)
    now = time.time()
    if tape.since <= 0.0:
        tape.since = now
    tape.flush(now)
    return decision.shares


