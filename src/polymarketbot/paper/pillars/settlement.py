"""Settlement pillar: the oracle's verdict is the only value this module knows.

A settlement buy pays ``p_win`` per share if the oracle's winning outcome is
the one the market resolves to.  The edge of one share is therefore

    edge = p_win - ask - taker_fee(1 share, ask, schedule)

and nothing here ever reads the market's own mid: the book is the *cost* side
of that expression, never the value side.  When the fair-value model has no
independent verdict the pillar has nothing to price and returns ``None`` /
an empty leg list; the caller must not substitute a book mid.

Every function is a pure function of its arguments.  The model object passed
in carries the tape I/O and is the caller's to refresh; this module never
fetches.

Units: prices and probabilities are fractions of 1.00, shares are outcome
shares, cash, fees and edges are USDC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Optional

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.paper.venue import Market
from polymarketbot.trading.domain2.settlement_fair_value import (
    OracleVerdict,
    SettlementFairValueModel,
)

if TYPE_CHECKING:  # ``Lot`` is used for typing only; the ledger is the caller's.
    from polymarketbot.paper.ledger import Lot

log = get_logger(__name__)


def model(*, tape_refresh_sec: float = 60.0) -> SettlementFairValueModel:
    """The shared oracle model: parquet tape plus any proposals ingested live."""
    return SettlementFairValueModel(tape_refresh_sec=tape_refresh_sec)


# Comparison tolerance on the marginal edge.  A level whose edge is exactly
# zero is not funded: buying it moves cash for no expected gain.
_EDGE_EPS = 1e-12
# Bisection steps for the cash-bounded size.  The fee rounds to 5 dp, so 64
# halvings resolve the affordable size far below the smallest tradable lot.
_SOLVE_STEPS = 64


@dataclass(frozen=True)
class SettlementLeg:
    """One priced buy on one ask level of the oracle's winning token.

    ``shares`` and ``price`` are the fill (outcome shares, fraction of 1.00),
    ``fee_usd`` is the exact taker fee for that fill in USDC, and ``edge_usd``
    is ``shares * p_win - shares * price - fee_usd``, i.e. the expected profit
    of the leg in USDC if the oracle's verdict is the final payout.
    """

    condition_id: str
    token_id: str
    price: float
    shares: float
    fee_usd: float
    edge_usd: float
    p_win: float


@dataclass(frozen=True)
class EdgeInputs:
    """The decision inputs of one ask, as one auditable record.

    ``edge_per_usd`` is the ranking key the engine funds on; ``break_even_ask``
    is the ask at which ``edge_per_share`` would be zero, i.e. the price the
    market *would* have to print for this verdict to be worth buying.  Every
    field is either the oracle's verdict, the market's own fee schedule, or the
    book's ask: none of them is a guess.
    """

    condition_id: str
    token_id: str
    p_win: float
    ask: float
    fee_per_share: float
    edge_per_share: float
    edge_per_usd: float
    break_even_ask: Optional[float]
    source: str


def verdict_for(
    model: SettlementFairValueModel,
    market: Market,
    now: Optional[float] = None,
) -> Optional[OracleVerdict]:
    """The oracle's verdict for ``market``, or ``None`` when none exists.

    ``now`` is epoch seconds (defaults to the model's clock).  The invariant:
    the returned verdict always indexes a real token in ``market.token_ids``,
    so every caller may use ``market.token_ids[verdict.winning_index]``
    without a bounds check of its own.  The book is never consulted.
    """
    return verdict_for_fields(
        model,
        condition_id=market.condition_id,
        question=market.question,
        outcomes=market.outcomes,
        token_count=len(market.token_ids),
        prices=market.outcome_prices,
        closed=market.closed,
        uma_status=market.uma_status,
        category=market.category,
        now=now,
    )


def verdict_for_fields(
    model: SettlementFairValueModel,
    *,
    condition_id: str,
    question: str,
    outcomes: Iterable[str],
    token_count: int,
    prices: Iterable[float] = (),
    closed: bool = False,
    uma_status: str = "",
    category: str = "settlement",
    now: Optional[float] = None,
) -> Optional[OracleVerdict]:
    """``verdict_for`` for a market that is described only by Gamma's fields.

    A market that has stopped accepting orders cannot be represented as a
    trading ``Market`` - ``venue.parse_market`` refuses it, correctly, because
    nothing can be quoted on it - yet it is *exactly* the market a settlement
    redemption needs a verdict for.  Building a ``Market`` here would force a
    tick and a min-order size onto a row that no longer publishes a meaningful
    one, so the payout decision reads the fields it actually needs.

    Same invariants as ``verdict_for``: ``None`` when the model has no verdict,
    and a warning plus ``None`` when the verdict does not index a real token.
    """
    verdict = model.fair_value(
        condition_id=condition_id,
        question=question,
        outcomes=list(outcomes),
        prices=list(prices),
        closed=closed,
        uma_status=uma_status,
        category=category,
        now=now,
    )
    if verdict is None:
        return None
    if not 0 <= int(verdict.winning_index) < int(token_count):
        log.warning(
            f"settlement verdict for {condition_id} indexes outcome "
            f"{verdict.winning_index} but the market has {token_count} tokens"
        )
        return None
    return verdict


def _fee_schedule(market: Market) -> Optional[FeeSchedule]:
    """The market's taker schedule, or ``None`` when Gamma gave none.

    A market without a schedule has an unknown taker fee; funding it would price
    the edge off an invented rate, so the caller treats ``None`` as unfundable.
    """
    return market.fee_schedule


def _edge_per_share(p_win: float, price: float, schedule: FeeSchedule) -> float:
    """Expected USDC profit of one share bought at ``price`` and held to payout.

    ``p_win - price - taker_fee(1 share, price, schedule)``.  The single
    definition of "the marginal edge" in this module: ``fundable_legs`` stops
    the ladder on its sign and ``marginal_edge`` hands it to callers that
    screen an ask before spending an action on it.
    """
    return p_win - price - taker_fee(1.0, price, schedule)


def marginal_edge(
    market: Market,
    verdict: Optional[OracleVerdict],
    price: float,
) -> Optional[float]:
    """Per-share expected edge of a buy at ``price``, or ``None`` if unfundable.

    ``None`` - never a zero - when there is no verdict, the verdict indexes no
    token of this market, the market has no fee schedule, or ``price`` is not a
    tradable fraction: those are "cannot price this trade", not "zero edge".
    A positive result is necessary but not sufficient: only ``fundable_legs``
    can check that the level actually holds ``market.min_order`` shares, so a
    caller screening candidates on this number must still let the ladder decide
    the size.
    """
    if verdict is None:
        return None
    index = int(verdict.winning_index)
    if not 0 <= index < len(market.token_ids):
        return None
    schedule = _fee_schedule(market)
    if schedule is None:
        return None
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if not 0.0 < px < 1.0:
        return None
    return _edge_per_share(float(verdict.p_win), px, schedule)


def break_even_ask(p_win: float, schedule: FeeSchedule) -> Optional[float]:
    """The highest ask at which one share still has non-negative expected edge.

    Solves ``p_win - ask - taker_fee(1 share, ask, schedule) = 0`` for ``ask``.
    The residual is positive at ``ask = 0`` (``p_win``) and negative at
    ``ask = p_win`` (``-fee``), and on every schedule this repo has measured
    (``rate * (ask*(1-ask))**exponent`` with ``rate < 1``) it decreases
    monotonically over that interval, so bisection converges on the unique
    crossing.  It is the "is the radar alive?" number: an ask above it is a
    correct no-trade, an ask below it with no bid is a missed trade.
    """
    p = float(p_win)
    if not 0.0 < p < 1.0:
        return None
    lo, hi = 0.0, p
    if _edge_per_share(p, lo, schedule) <= 0.0:
        return None
    for _ in range(_SOLVE_STEPS):
        mid = (lo + hi) * 0.5
        if _edge_per_share(p, mid, schedule) > 0.0:
            lo = mid
        else:
            hi = mid
    return lo


def edge_inputs(
    market: Market,
    verdict: Optional[OracleVerdict],
    price: float,
) -> Optional[EdgeInputs]:
    """Every number the funding decision reads, for one ask, in one record.

    Same gate as ``marginal_edge`` (it is the same helper), but it also returns
    the fee component and the break-even ask, so a logged no-trade states the
    ask that *would* have funded - the only way to tell a correctly idle radar
    from a blind one.
    """
    if verdict is None:
        return None
    index = int(verdict.winning_index)
    if not 0 <= index < len(market.token_ids):
        return None
    schedule = _fee_schedule(market)
    if schedule is None:
        return None
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if not 0.0 < px < 1.0:
        return None
    p_win = float(verdict.p_win)
    fee = taker_fee(1.0, px, schedule)
    edge = p_win - px - fee
    return EdgeInputs(
        condition_id=market.condition_id,
        token_id=market.token_ids[index],
        p_win=p_win,
        ask=px,
        fee_per_share=fee,
        edge_per_share=edge,
        edge_per_usd=edge / px,
        break_even_ask=break_even_ask(p_win, schedule),
        source=verdict.source,
    )


def _max_affordable(price: float, schedule: FeeSchedule, budget_usd: float) -> float:
    """Largest share count whose ``shares*price + fee(shares)`` fits ``budget_usd``.

    Budget is USDC, price a fraction of 1.00, the result is outcome shares.
    The fee is monotone in shares, so bisection between zero and the fee-free
    bound ``budget/price`` is exact up to the solver's tolerance and never
    returns a size the ledger would reject for insufficient cash.
    """
    if budget_usd <= 0.0 or price <= 0.0:
        return 0.0
    hi = budget_usd / price
    if hi * price + taker_fee(hi, price, schedule) <= budget_usd:
        return hi
    lo = 0.0
    for _ in range(_SOLVE_STEPS):
        mid = (lo + hi) * 0.5
        if mid * price + taker_fee(mid, price, schedule) <= budget_usd:
            lo = mid
        else:
            hi = mid
    return lo


def fundable_legs(
    market: Market,
    verdict: Optional[OracleVerdict],
    ask_ladder: Iterable[tuple[float, float]],
    cash_usd: float,
) -> list[SettlementLeg]:
    """Buy every ask level whose marginal expected edge is strictly positive.

    ``ask_ladder`` is ``(price, shares)`` levels in any order; the levels are
    sorted by price ascending and consumed while

        p_win - price - taker_fee(1 share, price, market.fee_schedule) > 0,

    bounded only by each level's resting size and by ``cash_usd`` (the fee is
    part of the cost).  There is no fixed edge hurdle and no size cap: the
    optimizer is the sign of the marginal edge itself.

    Invariants: every leg's token is the token at ``verdict.winning_index`` in
    ``market.token_ids``; ``sum(shares*price + fee_usd) <= cash_usd``; every
    leg meets the venue's own ``min_order`` and has ``edge_usd > 0``; and the
    ladder is walked
    in price order, so once a level's marginal edge is non-positive every
    deeper level is skipped too.  Returns ``[]`` for a missing verdict, an
    unknown fee schedule, or no cash.
    """
    if verdict is None:
        return []
    index = int(verdict.winning_index)
    if not 0 <= index < len(market.token_ids):
        return []
    schedule = _fee_schedule(market)
    if schedule is None:
        return []

    p_win = float(verdict.p_win)
    token_id = market.token_ids[index]
    cash = float(cash_usd)
    if cash <= 0.0:
        return []

    levels: list[tuple[float, float]] = []
    for raw in ask_ladder:
        try:
            price, size = float(raw[0]), float(raw[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            levels.append((price, size))
    levels.sort(key=lambda level: level[0])

    legs: list[SettlementLeg] = []
    for price, size in levels:
        marginal = _edge_per_share(p_win, price, schedule)
        if marginal <= _EDGE_EPS:
            break
        if cash <= 0.0:
            break
        shares = min(size, _max_affordable(price, schedule, cash))
        if shares <= 0.0:
            break
        if shares < market.min_order:
            # The venue rejects an order below its own published minimum, so a
            # level this shallow is not tradable no matter how good its edge is.
            # A deeper level can still be fundable, so this is not a price-order
            # break.
            continue
        fee_usd = taker_fee(shares, price, schedule)
        cost = shares * price + fee_usd
        if cost > cash + 1e-9:
            # Rounding in the fee can push the bisected size a hair over; shrink
            # by whole micro-shares until the ledger's cash check would pass.
            shares = max(0.0, (cash - fee_usd) / price)
            fee_usd = taker_fee(shares, price, schedule)
            cost = shares * price + fee_usd
            if shares < market.min_order or cost > cash + 1e-9:
                break
        edge_usd = shares * p_win - cost
        if edge_usd <= 0.0:
            # The marginal edge is positive but the fee's 5 dp rounding can make
            # a sub-share leg lose money; never append a leg with no expected
            # profit, and deeper levels have strictly less edge per share.
            break
        legs.append(
            SettlementLeg(
                condition_id=market.condition_id,
                token_id=token_id,
                price=price,
                shares=shares,
                fee_usd=fee_usd,
                edge_usd=edge_usd,
                p_win=p_win,
            )
        )
        cash -= cost
    return legs


def redeemable_shares(
    lots: Iterable[Lot],
    market: Market,
    verdict: Optional[OracleVerdict],
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Split a strategy's lots into oracle winners and losers, by token.

    ``lots`` is ``Ledger.positions`` output; only lots of ``market``'s
    condition with positive shares are considered.  Returns
    ``(winners, losers)``, each a list of ``(token_id, shares)`` grouped by
    token in the market's token order.  Invariant: the two lists together hold
    exactly the lots' shares of that condition - the caller redeems the first
    at par and writes the second off at zero - so no lot is dropped or counted
    twice.  With no verdict the winner is unknown and both lists are empty.
    """
    return split_redemption(
        lots,
        condition_id=market.condition_id,
        token_ids=market.token_ids,
        verdict=verdict,
    )


def split_redemption(
    lots: Iterable[Lot],
    *,
    condition_id: str,
    token_ids: Iterable[str],
    verdict: Optional[OracleVerdict],
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """``redeemable_shares`` from Gamma's raw fields, for a closed market.

    A market is only redeemable once it is ``closed``, and ``venue.parse_market``
    deliberately returns ``None`` for exactly those rows (it is the *trading*
    gate: ``acceptingOrders == False``).  The measured consequence, before this
    entry point existed, was that ``_reconcile_held`` never reached its redeem
    branch at all: it required ``market["closed"]`` and a parsed ``Market``,
    which are mutually exclusive, so an oracle win was marked at par forever and
    never converted to cash.

    Same invariants as ``redeemable_shares``; ``token_ids`` is the market's
    ``clobTokenIds`` order, which is the slot order the verdict indexes.
    """
    if verdict is None:
        return [], []
    tokens = tuple(token_ids)
    index = int(verdict.winning_index)
    if not 0 <= index < len(tokens):
        return [], []
    winning_token = tokens[index]

    winners: dict[str, float] = {}
    losers: dict[str, float] = {}
    for lot in lots:
        if lot.condition_id != condition_id:
            continue
        shares = float(lot.shares)
        if shares <= 0.0:
            continue
        bucket = winners if lot.token_id == winning_token else losers
        bucket[lot.token_id] = bucket.get(lot.token_id, 0.0) + shares

    order = {token: i for i, token in enumerate(tokens)}

    def grouped(bucket: dict[str, float]) -> list[tuple[str, float]]:
        return sorted(
            bucket.items(),
            key=lambda item: (order.get(item[0], len(order)), item[0]),
        )

    return grouped(winners), grouped(losers)


def par_from_verdict(verdict: Optional[OracleVerdict]) -> float:
    """Protocol value per share of the oracle's winning token, in USDC.

    A final payout pays exactly 1.00 per share; an undisputed UMA proposal is
    worth its dispute-adjusted ``p_win``.  The result is clamped to [0, 1] and
    is 0.0 when there is no verdict: with no independent value the pillar must
    not claim par, and must never fall back to a book mark.

    0.0 for "no verdict" is not a coerced unknown - it is the caller's signal to
    leave the lot's mark untouched, and ``Ledger.set_par`` is only called when
    a verdict exists at all.
    """
    if verdict is None:
        return 0.0
    if verdict.source == "gamma_payout":
        value = 1.0
    else:
        value = float(verdict.p_win)
    return min(1.0, max(0.0, value))


def mark_value(market: Market, verdict: Optional[OracleVerdict]) -> float:
    """``par_from_verdict``, refusing a verdict that indexes no token here.

    ``None`` and an out-of-range verdict both mark at 0.0, so a mismatched
    verdict can never be booked at par.
    """
    if verdict is None:
        return 0.0
    index = int(verdict.winning_index)
    if not 0 <= index < len(market.token_ids):
        return 0.0
    return par_from_verdict(verdict)
