"""Reward pillar: quote the cheaper leg inside the venue's reward band.

The venue pays a daily pool to resting size scored by
``size * ((v - |price - mid|) / v) ** 2`` inside a maximum-spread band ``v``.
This module turns a measured universe into allocator candidates, hands them to
the package optimizer, and converts the optimizer's capital into tick-valid
resting bids on the cheaper outcome token.  Nothing here invents a price: the
quote is one venue tick inside the touch, capped at the highest price that
still rests below the ask, and a market whose quote falls outside the band is
not a candidate.

Every function is a pure function of its arguments; the caller owns all I/O.

Units: prices, mids and ticks are fractions of 1.00 USDC per share; spreads
are cents; sizes are shares; ``daily_pool`` and capital are USDC per day and
USDC.
"""

from __future__ import annotations

import dataclasses
import math
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper.books import best_ask, best_bid, mid, qualifying_score
from polymarketbot.paper.venue import Market, Print
from polymarketbot.rewards.allocator import (
    PROTOCOL_MIN_DAILY_PAYOUT_USD,
    LiquidityRewardsAllocator,
    PortfolioAllocationResult,
)
from polymarketbot.rewards.scoring import single_sided_score_fraction

log = get_logger(__name__)


@dataclass(frozen=True)
class RewardQuote:
    """One resting bid the reward pillar wants to place.

    ``shares`` is tick-valid and never below the market's reward minimum size;
    ``daily_payout`` is the optimizer's estimate for the capital behind it.
    """

    condition_id: str
    question: str
    token_id: str
    price: float
    shares: float
    daily_payout: float
    competing_score: float
    max_spread_cents: float
    min_size: float
    strategy: str = "RewardHarvester"


def _cheaper_leg(
    market: Market, books: dict[str, dict[str, Any]]
) -> Optional[tuple[float, str, dict[str, Any]]]:
    """(leg mid, token_id, book) for the lower-priced leg of a binary market.

    The leg price is ``mid`` of a token below 0.5, otherwise ``1 - mid`` of the
    complementary token.  When both books price, the cheaper leg wins; when
    neither does, returns ``None``.  Units: the mid is a fraction of 1.00 USDC
    per share; the token id is the leg's CLOB token.
    """
    best: Optional[tuple[float, str, dict[str, Any]]] = None
    for index, token_id in enumerate(market.token_ids):
        book = books.get(token_id)
        if not isinstance(book, dict):
            continue
        token_mid = mid(book)
        if token_mid is None:
            continue
        if token_mid <= 0.5:
            leg_price, leg_token, leg_book = token_mid, token_id, book
        else:
            leg_token = market.token_ids[1 - index]
            leg_book = books.get(leg_token)
            leg_price = 1.0 - token_mid
        if not isinstance(leg_book, dict):
            continue
        if best is None or leg_price < best[0]:
            best = (leg_price, leg_token, leg_book)
    return best


def market_competing_score(
    market: Market, books: dict[str, dict[str, Any]]
) -> Optional[float]:
    """The market's MEASURED in-band competing score, summed over both tokens.

    Source: the venue's own L2 snapshot for **both** outcome-token books, each
    level scored with the venue's published rule ``size * ((v - |p - mid|)/v)**2``
    inside the market's ``rewards_max_spread`` band ``v`` and against that
    token's own book mid. This is the quantity the venue divides a maker's score
    by, so it is the only honest denominator for a share of the daily pool; no
    multiplier, floor or rate-derived estimate is applied anywhere.

    Both tokens matter: a market's campaign pays on the market (condition), so a
    maker and its competitors are scored across the pair. Measuring only the
    quoted leg halves the denominator and doubles our claimed share - measured
    on 40 live reward markets on 2026-11-10, the leg-only reading implied
    $772/day of payout on $838 of capital where the pair-wide reading implied
    $365/day, a 2.1x overclaim.

    Returns ``None`` - never a floor - when either book or either mid is
    missing, and also when the measured total is zero: a zero denominator makes
    the payout singular (the whole pool), and a book with no in-band depth is
    indistinguishable from a stale snapshot, so the caller must refuse the
    market instead of pricing it. A market that is not binary has no pair to
    measure and is likewise refused.
    """
    if not market.binary or market.reward_max_spread_cents <= 0.0:
        return None
    total = 0.0
    for token_id in market.token_ids:
        book = books.get(token_id)
        if not isinstance(book, dict):
            return None
        token_mid = mid(book)
        if token_mid is None:
            return None
        total += qualifying_score(
            book,
            max_spread_cents=market.reward_max_spread_cents,
            mid_price=token_mid,
        )
    return total if total > 0.0 else None


def reward_candidates(
    markets: list[Market],
    books: dict[str, dict[str, Any]],
    tape_prints: list[Print],
) -> list[dict[str, Any]]:
    """Allocator candidate dicts for every market quoting inside the band.

    A market qualifies when it pays a daily pool, publishes a positive maximum
    spread and tick, is binary, and at least one token book has a measured mid.
    The cheaper leg is quoted at ``min(best_bid + tick, best_ask - tick)``, the
    highest price at which the bid still *rests*: on a book one tick wide those
    are the same price as the ask, and a bid at the ask is marketable. The
    candidate is dropped when the leg book lacks a bid or ask, when the book is
    locked or crossed (no resting price exists), when that quote leaves the
    price domain, when ``|quote - leg mid|`` exceeds ``max_spread - tick`` in
    cents, or when the venue would pay a one-sided quote nothing at all (leg
    mid below the documented two-sided band, see
    :func:`polymarketbot.rewards.scoring.single_sided_score_fraction`).

    This pillar rests a **single** bid, so the venue scores it one-sided:
    ``Q_min = alpha * shares / c`` inside the band and ``0`` outside it. The
    candidate therefore carries the measured score it will actually be paid,
    ``score_per_usd = score_fraction / quote_price`` (venue score units per
    USDC of resting capital), rather than the raw ``alpha`` the allocator would
    otherwise read as a dollar-denominated score.

    ``competing_score`` is the market's **measured** in-band competing score,
    summed over both outcome-token books (see :func:`market_competing_score`) -
    an upper bound on the competitors' ``Q_min``, because every competitor's own
    two-sided penalty can only shrink it (``sum Q_min <= sum raw``). Using the
    upper bound understates our share, which is the safe direction. A market
    whose books or mids are missing, or whose measured total is zero, is refused
    here rather than priced at a floor: the denominator is the claim's only
    evidence.

    ``lot_price`` is the price our own order rests at, so the optimizer funds
    the venue's minimum *share* lot at the quote rather than at the mid.

    ``tape_prints`` is the measured tape snapshot accompanying the books; the
    quote itself is a pure function of the book and market fields.

    Units: ``daily_pool`` is USDC per day, ``competing_score`` is the venue's
    share-weighted score, ``max_spread`` is cents, ``min_size`` is shares,
    ``real_mid``, ``quote_price``, ``lot_price`` and ``tick`` are USDC per
    share, ``score_per_usd`` is score units per USDC.
    """
    candidates: list[dict[str, Any]] = []
    for market in markets:
        if market.daily_reward <= 0.0 or market.reward_max_spread_cents <= 0.0:
            continue
        if market.tick <= 0.0 or not market.binary:
            continue
        # A rewarded market always publishes the share size that qualifies for
        # rewards. When the venue omits it, the quote cannot be scored, so the
        # market is not tradable here; a null is not a zero-size qualification.
        if market.reward_min_size <= 0.0:
            continue
        leg = _cheaper_leg(market, books)
        if leg is None:
            continue
        leg_mid, token_id, book = leg
        bid, ask = best_bid(book), best_ask(book)
        if bid is None or ask is None:
            continue
        # A bid priced at or above the ask is marketable: it would cross on
        # landing, so it can neither rest for the pool nor be filled by a later
        # SELL print. `best_bid + tick` equals the ask whenever the book is one
        # tick wide, and those are exactly the highest-scoring markets, so the
        # allocator funds them first. Measured on 40 live reward candidates,
        # 16 (40 %) priced at the ask; over 4,229 tape prints those quotes saw
        # 0.22 crossing prints per 30-minute window, and the highest price that
        # still rests (`ask - tick`) saw 0.81 - 4x the flow, on a book where
        # the level actually has depth (16/40) so the queue share is not 1.0.
        quote_price = min(bid + market.tick, ask - market.tick)
        # A locked or crossed book publishes no price a bid can rest at.
        if quote_price < bid - 1e-12:
            continue
        if not 0.0 < quote_price < 1.0:
            continue
        band_cents = market.reward_max_spread_cents - market.tick * 100.0
        if abs(quote_price - leg_mid) * 100.0 > band_cents:
            continue
        # One bid is one-sided liquidity: outside the documented two-sided
        # midpoint band the venue pays it nothing, so quoting there is pure
        # inventory risk. Measured on 2,921 live candidates, 51 % price below
        # the band and the allocator was funding them.
        score_fraction = single_sided_score_fraction(leg_mid)
        if score_fraction <= 0.0:
            continue
        score = market_competing_score(market, books)
        if score is None:
            continue
        candidates.append(
            {
                "condition_id": market.condition_id,
                "question": market.question,
                "daily_pool": market.daily_reward,
                "competing_score": score,
                "max_spread": market.reward_max_spread_cents,
                "min_size": max(market.reward_min_size, market.min_order),
                "real_mid": leg_mid,
                # The score multiplier belongs to the distance *this* quote will
                # actually rest at, so it is measured here, per candidate, and
                # never defaulted by the allocator.
                "spread_offset_cents": abs(quote_price - leg_mid) * 100.0,
                # Venue score earned per USDC of resting capital: shares per
                # dollar (1 / price) times the one-sided payout fraction.
                "score_per_usd": score_fraction / quote_price,
                "tokens": [
                    {"outcome": outcome, "token_id": token}
                    for outcome, token in zip(market.outcomes, market.token_ids)
                ],
                "quote_price": quote_price,
                "quote_token": token_id,
                "quote_side": "bid",
                "tick": market.tick,
                "lot_price": quote_price,
            }
        )
    return candidates


def allocate(
    candidates: list[dict[str, Any]], bankroll_usd: float
) -> PortfolioAllocationResult:
    """Run the package optimizer over ``candidates`` with ``bankroll_usd``.

    Returns the optimizer's statement unchanged: the reserve policy, the MILP
    and its fallbacks are the allocator's, not this pillar's.  Units:
    ``bankroll_usd`` and every returned capital/payout figure are USDC;
    ``candidates`` carry the units documented on :func:`reward_candidates`.
    """
    return LiquidityRewardsAllocator().optimize_allocation_milp(
        candidates, bankroll_usd=bankroll_usd
    )


@dataclass(frozen=True)
class AccrualShare:
    """What one resting bid is actually paid, from measured books.

    ``usd_per_day`` is the quote's share of the market's published pool:
    ``pool * our_score / (measured_competing_score + our_score)``, with
    ``our_score = alpha(shares, |price - leg_mid|) * shares * 1/c`` (``c`` = the
    venue's one-sided penalty) and ``competing_score`` the pair-wide in-band
    score from :func:`market_competing_score`. ``qualifies`` is False when the
    quote earns the venue nothing at all (size below the market's reward
    minimum, price outside the band, or a leg mid outside the two-sided band);
    the payout is then a measured zero rather than an omission. Units: USD/day,
    shares, score units, USD/day.
    """

    usd_per_day: float
    share: float
    our_score: float
    competing_score: float
    pool_usd_per_day: float
    qualifies: bool


def measured_accrual(
    *,
    pool_usd_per_day: Optional[float],
    price: float,
    shares: float,
    min_size: Optional[float],
    max_spread_cents: Optional[float],
    leg_book: Optional[dict[str, Any]],
    other_book: Optional[dict[str, Any]],
) -> Optional[AccrualShare]:
    """The payout a resting bid earns per day, from its own measured books.

    This is the accrual the engine credits as cash, so every input is a
    measurement and nothing has a fallback: the pool is the venue's published
    daily rate for the market, the band and the minimum size are the venue's
    published campaign parameters, ``price``/``shares`` are what is actually
    resting, and the mid and competing score come from the current L2 snapshot
    of *both* outcome-token books. Returns ``None`` - and the caller must then
    credit nothing - when any of those is missing or when the measured
    competing score is zero (a singular denominator), because a payout without
    a measured denominator is a guess.

    Deliberately different from :func:`quotes_from_allocation`: that one prices
    a *plan* from the optimizer, whose payout was computed once for the planned
    capital; this one prices what is on the book **now**, so a partially filled,
    stale or over-sized resting bid accrues what it actually earns.
    """
    if pool_usd_per_day is None or pool_usd_per_day <= 0.0:
        return None
    if min_size is None or min_size <= 0.0:
        return None
    if max_spread_cents is None or max_spread_cents <= 0.0:
        return None
    if not isinstance(leg_book, dict) or not isinstance(other_book, dict):
        return None
    leg_mid = mid(leg_book)
    other_mid = mid(other_book)
    if leg_mid is None or other_mid is None:
        return None
    competing = qualifying_score(
        leg_book, max_spread_cents=max_spread_cents, mid_price=leg_mid
    ) + qualifying_score(
        other_book, max_spread_cents=max_spread_cents, mid_price=other_mid
    )
    if competing <= 0.0:
        return None
    if shares <= 0.0 or shares < min_size or not 0.0 < price < 1.0:
        return AccrualShare(0.0, 0.0, 0.0, competing, pool_usd_per_day, False)
    band = float(max_spread_cents)
    distance = abs(price - leg_mid) * 100.0
    if distance >= band:
        # Outside the band the venue scores nothing: a measured zero payout.
        return AccrualShare(0.0, 0.0, 0.0, competing, pool_usd_per_day, False)
    fraction = single_sided_score_fraction(leg_mid)
    if fraction <= 0.0:
        return AccrualShare(0.0, 0.0, 0.0, competing, pool_usd_per_day, False)
    our_score = ((band - distance) / band) ** 2 * float(shares) * fraction
    denom = competing + our_score
    share = our_score / denom if denom > 0.0 else 0.0
    usd_per_day = pool_usd_per_day * share
    # Documented venue floor: a market whose daily payout is below $1.00 pays
    # nothing, so the quote's share of it is a measured zero, not a small cash
    # amount the engine would otherwise bank.
    if usd_per_day < PROTOCOL_MIN_DAILY_PAYOUT_USD:
        return AccrualShare(0.0, share, our_score, competing, pool_usd_per_day, False)
    return AccrualShare(
        usd_per_day=usd_per_day,
        share=share,
        our_score=our_score,
        competing_score=competing,
        pool_usd_per_day=pool_usd_per_day,
        qualifies=True,
    )


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


def quotes_from_allocation(
    result: PortfolioAllocationResult,
    candidates: list[dict[str, Any]],
    books: dict[str, dict[str, Any]],
) -> list[RewardQuote]:
    """Convert allocated capital into tick-valid resting bids.

    ``shares = allocated_capital / quote_price`` floored to whole shares, the
    venue's minimum-order unit; a quote is dropped when it carries no capital,
    prices outside (0, 1), or floors below the market's ``min_size`` shares.
    ``books`` is the snapshot the candidates were built from and is accepted so
    callers pass one book set through the pipeline; sizing needs only the
    candidate's quote price.

    Units: capital is USDC, ``price`` is USDC per share, ``shares`` is shares,
    ``daily_payout`` is USDC per day, ``max_spread_cents`` is cents,
    ``min_size`` is shares.
    """
    by_condition = {candidate["condition_id"]: candidate for candidate in candidates}
    quotes: list[RewardQuote] = []
    for allocation in result.allocations:
        candidate = by_condition.get(allocation.condition_id)
        if candidate is None:
            continue
        if allocation.allocated_capital <= 0.0:
            continue
        price = float(candidate["quote_price"])
        if not 0.0 < price < 1.0:
            continue
        tick = float(candidate["tick"])
        if tick <= 0.0:
            continue
        raw_shares = allocation.allocated_capital / price
        # The price tick quantises *prices*, not sizes; shares are whole units
        # because the venue's minimum order is published as a share count.
        shares = float(math.floor(raw_shares + 1e-9))
        min_size = float(candidate["min_size"])
        if min_size <= 0.0 or shares < min_size:
            continue
        quotes.append(
            RewardQuote(
                condition_id=str(candidate["condition_id"]),
                question=str(candidate["question"]),
                token_id=str(candidate["quote_token"]),
                price=price,
                shares=shares,
                daily_payout=float(allocation.daily_reward_payout),
                competing_score=float(candidate["competing_score"]),
                max_spread_cents=float(candidate["max_spread"]),
                min_size=min_size,
            )
        )
    return quotes


def markout_cost_usd(
    shares: float, markout_cents_per_share: Optional[float]
) -> float:
    """Adverse-move cost of ``shares`` filled at the quote, in USDC.

    The paper engine already books the *realised* adverse move: a fill enters
    ``Ledger`` at the quote price and :meth:`Engine._mark` re-values every lot
    at the live best bid on each tick, so a drop after the fill lands in equity
    immediately. A booking-side markout term on top of that would count the
    same move twice, and this window produced no fill to book either way.

    What is missing is the *ex-ante* term: the quote-selection rule prices a
    fill at the touch and ignores that the touching taker is informed. That
    term needs a measured markout, and there is no defensible constant for it
    here: cycle 2 measured -0.209 c/share (-0.585 %) over **2** fills. On a
    prediction market the per-fill dispersion is the same order as the mean, so
    the standard error at n=2 is of the same size as the estimate and no fit is
    possible.

    The 2026-11-10 12:24 tape can now supply the *sample* - 258 SELL prints at
    the price the pillar would rest at across 40 live reward candidates, 246 of
    them with a later print to mark against - but not the *horizon*: marking
    each fill with the next print of the same token realises a median gap of
    1,710 s against a nominal 60 s, and the estimate it produces is +0.311 c
    +/- 0.133 c (t = +2.33), the opposite sign to the earlier 2-fill reading.
    The missing input is therefore not prints but a persisted best-bid/mid
    series at a fixed offset from the fill: this soak writes equity snapshots
    only, so a fixed-Delta markout cannot be computed from it. The parameter
    stays ``None`` until that series exists; ``None`` -> 0.0 with no invented
    default, and the caller is expected to log that the fill is priced without
    one.

    Units: ``shares`` is shares, ``markout_cents_per_share`` is USDC cents per
    share (negative for an adverse move the pillar pays), result is USDC.
    """
    if markout_cents_per_share is None or shares <= 0.0:
        return 0.0
    return float(shares) * float(markout_cents_per_share) / 100.0


def commit_quotes(
    ideal: list[RewardQuote],
    cash_usd: float,
    resting: Mapping[str, tuple[float, float]],
) -> list[RewardQuote]:
    """The subset of ``ideal`` the pillar can afford, given what already rests.

    ``allocate`` is handed the whole balance every tick and re-derives sizes,
    so on its own it prices a *fresh* book. The engine, however, keeps a
    resting bid whose price has not moved instead of cancel-and-replacing it,
    which means the cash behind an earlier tick's quote is still committed
    while this tick's plan spends the same dollars again. The 2026-11-10
    10:10-10:40 window measured the result: 48 reward bids resting $1,445.65
    while cash was $1,000.51, i.e. 44 % more notional on the book than the
    pillar could settle. No fill occurred, so nothing was refused; had the
    tape filled that book, ``Ledger.buy`` would have refused the overflow and
    silently dropped fills the pillar had already decided to take.

    This function is the reserve ledger. Quotes are accepted in descending
    marginal yield (``daily_payout`` per dollar of notional) until the cash is
    spent, and a quote whose notional does not fit is **not** returned: the
    engine cancels any resting bid it is not asked to keep, so an unaffordable
    stale bid is retired instead of being allowed to over-commit.

    A resting bid is accepted at the size it is already resting at, not at the
    freshly optimised size, because the engine keeps the existing order when
    the price is unchanged; counting the new size while the old one rests is
    exactly the double-spend this function exists to prevent. The residual
    (which can only be smaller than the fresh plan) is deliberately not
    redistributed: re-sizing a kept order needs a cancel/replace, and holding
    a smaller quote for one more tick costs a fraction of one tick's accrual.

    ``ideal`` is the optimizer's statement for this tick (see
    :func:`quotes_from_allocation`), ``cash_usd`` the pillar's spendable cash,
    ``resting`` maps token id to ``(price, shares)`` of the bids currently on
    the book and not cancelled. Returns quotes whose notional sums to at most
    ``cash_usd``. Units: USDC, shares, USDC per share.
    """
    cash = max(0.0, float(cash_usd))
    if cash <= 0.0 or not ideal:
        return []
    resting = resting or {}
    ordered = sorted(
        ideal,
        key=lambda quote: quote.daily_payout / max(quote.price * quote.shares, 1e-9),
        reverse=True,
    )
    committed: list[RewardQuote] = []
    remaining = cash
    already = 0
    for quote in ordered:
        entry = resting.get(quote.token_id)
        # A resting bid only counts as kept when it prices where this tick's
        # plan does; a moved price is a cancel, and a cancelled order releases
        # its cash, so it must not be charged against this budget.
        kept = None
        if entry is not None:
            price, shares = float(entry[0]), float(entry[1])
            if shares >= quote.min_size and abs(price - quote.price) <= 1e-9:
                kept = shares
        notional = (kept if kept is not None else quote.shares) * quote.price
        if notional > remaining:
            continue
        remaining -= notional
        already += kept is not None
        committed.append(quote if kept is None else dataclasses.replace(quote, shares=kept))
    # Logged every tick, not only on a shortfall: ``already resting`` versus
    # ``new`` is the plan's own stability, and the engine cancels every resting
    # bid this plan omits. The 11:48-12:19 window issued >=156 cancels and >=191
    # placements over 110 ticks on a 32.7-quote book (3.15 quotes of churn per
    # tick); it cost 0.09-0.13 % of the window's $9.09 accrual, so churn was not
    # what kept the book at zero fills.
    log.info(
        f"reward reserve: kept {len(committed)}/{len(ordered)} quotes, "
        f"${cash - remaining:,.2f} committed of ${cash:,.2f} cash "
        f"({len(ordered) - len(committed)} unaffordable; "
        f"{already} already resting, {len(committed) - already} new)"
    )
    return committed

def plan_quotes(
    candidates: list[dict[str, Any]],
    cash_usd: float,
    resting: Optional[Mapping[str, tuple[float, float]]] = None,
    state: Optional[dict[str, Any]] = None,
) -> list[RewardQuote]:
    """The pillar's tick decision: optimise, then commit within cash.

    ``allocate`` answers "where would this cash go if the book were empty";
    :func:`commit_quotes` answers "and what may rest given the cash already
    committed". Splitting the two keeps the optimizer a pure function of its
    inputs and makes the cash invariant enforce-able on the real allocator
    output rather than on a hand-scaled budget. See :func:`commit_quotes` for
    the measurement behind the reserve ledger.

    ``state``, when given, receives the optimum's KKT price of capital
    (``"shadow_yield"``, USD per USD per day). The engine publishes it as the
    opportunity-cost hurdle every other pillar must beat to use the same
    dollar; ``None`` means no allocation was solved, which the other pillars
    treat as missing data rather than as a zero hurdle.
    """
    cash = max(0.0, float(cash_usd))
    if cash <= 0.0 or not candidates:
        if state is not None:
            state["shadow_yield"] = None
        return []
    allocation = allocate(candidates, cash)
    if state is not None:
        state["shadow_yield"] = getattr(
            allocation, "shadow_yield_floor_usd_per_usd_per_day", None
        )
    return commit_quotes(
        quotes_from_allocation(allocation, candidates, {}),
        cash,
        resting or {},
    )
