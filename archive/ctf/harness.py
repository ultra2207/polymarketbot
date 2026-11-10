"""Engine-free harness pieces the archived CTF runner needs.

The archived runner (``archive.ctf.runner``) is the old paper-engine pillar
loop.  Three of its inputs used to come from ``polymarketbot.paper``:

* the quote record ``Resting`` and the ``pending_for`` guard
  (``polymarketbot.paper.actions``),
* the market/print views it annotates with (``polymarketbot.paper.venue``),
* the measured fill rule that books fills of a resting maker quote
  (``polymarketbot.paper.pillars.reward.measured_fill_shares`` through
  ``reward_runner.apply_resting_fills``).

Archive code may not import ``polymarketbot.paper``, so those definitions are
copied here.  ``Market``/``Print``/``Resting`` are field-for-field copies of the
paper dataclasses; ``measured_fill_shares`` is the same fill rule (a SELL print
fills a resting bid only when it printed at that bid's own price, weighted by
our share of the queue at the level); ``apply_resting_fills`` is the same
accrual loop.  The only omission is the paper pillar's rolling log tally, which
is telemetry for the live reward pillar and has no reader here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from polymarketbot.fees import FeeSchedule, maker_fee_rebate
from polymarketbot.logs import get_logger

from .books import shares_at

log = get_logger("archive.ctf")


@dataclass(frozen=True)
class Market:
    """Static market facts, copied from ``polymarketbot.paper.venue.Market``."""

    condition_id: str
    question: str
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    tick: float
    min_order: float
    daily_reward: float
    reward_max_spread_cents: float
    reward_min_size: float
    fee_schedule: Optional[FeeSchedule]
    neg_risk: bool
    closed: bool
    uma_status: str
    outcome_prices: tuple[float, ...] = ()
    category: str = "other"

    @property
    def binary(self) -> bool:
        return len(self.token_ids) == 2


@dataclass
class Print:
    token_id: str
    condition_id: str
    side: str  # taker side: BUY or SELL
    price: float
    size: float
    ts: float


@dataclass
class Resting:
    """One resting maker quote, copied from ``polymarketbot.paper.actions``."""

    strategy: str
    condition_id: str
    token_id: str
    price: float
    shares: float
    min_order: float
    placed: float
    cancel_at: Optional[float] = None
    tick: float = 0.01
    daily_payout: float = 0.0
    """Optimizer payout estimate in USDC/day for the capital behind this quote."""
    seen_ts: float = 0.0
    """Newest print already consumed by the fill model; stops re-counting."""
    accumulated: float = 0.0
    """Sub-min-order fill flow carried until it can clear the venue minimum."""
    initial_shares: float = 0.0
    """Size when placed; accrual scales with what is still resting."""
    max_age_s: float = 0.0
    """Lifetime the quote's EV was measured over; 0.0 = unbounded."""


def pending_for(engine: Any, condition_id: str) -> bool:
    """True when the landing book already holds an action for this condition."""
    return any(action.payload.get("condition_id") == condition_id
               for action in engine.actions.pending())


def measured_fill_shares(
    quote_price: float,
    our_size: float,
    level_shares: float,
    prints: list[Print],
    tick: float,
) -> float:
    """Expected shares filled on a resting bid at ``quote_price``.

    Copied rule (``polymarketbot.paper.pillars.reward.measured_fill_shares``): a
    print fills this bid only when it printed at the bid's own price
    (``|print.price - quote_price| <= tick / 2``), with our share of that
    level's flow being ``our_size / (level_shares + our_size)``.  A SELL print
    strictly below our price is somebody else's fill and is not counted; a BUY
    print cannot fill a resting bid.
    """
    if our_size <= 0.0 or level_shares < 0.0 or tick <= 0.0:
        return 0.0
    denominator = level_shares + our_size
    if denominator <= 0.0:
        return 0.0
    queue_share = min(1.0, our_size / denominator)
    tolerance = tick * 0.5 + 1e-12
    flow = 0.0
    for trade in prints:
        if str(trade.side).upper() != "SELL":
            continue
        if abs(trade.price - quote_price) <= tolerance:
            flow += trade.size
    return max(0.0, flow) * queue_share


def apply_resting_fills(
    engine: Any,
    resting: dict[str, Resting],
    strategy: str,
    books: dict[str, dict],
    prints: list[Print],
    now: float,
) -> None:
    """Book fills of ``strategy``'s resting bids from measured tape prints.

    Copied from ``polymarketbot.paper.pillars.reward_runner.apply_resting_fills``:
    a quote past its measured lifetime is pulled, a cancelled quote leaves after
    the round trip, and a fill is booked only once the accumulated crossing flow
    reaches the venue minimum order.
    """
    for token_id, quote in list(resting.items()):
        if now < quote.placed:
            continue
        if quote.max_age_s and now > quote.placed + quote.max_age_s:
            resting.pop(token_id, None)
            continue
        if quote.cancel_at is not None and now >= quote.cancel_at + engine._rtt():
            resting.pop(token_id, None)
            continue
        market_prints = [p for p in prints
                         if p.token_id == token_id and p.ts > quote.seen_ts]
        if market_prints:
            quote.seen_ts = max(p.ts for p in market_prints)
        book = books.get(token_id) or {}
        level_shares = shares_at(book, bid=True, price=quote.price)
        fill = measured_fill_shares(quote.price, quote.shares, level_shares,
                                    market_prints, quote.tick)
        # Measured prints are 1-5 shares; discarding sub-lot flow guarantees
        # no fill against a 20-200 share minimum. Carry it instead.
        quote.accumulated += max(0.0, fill)
        fill = min(quote.accumulated, quote.shares)
        if fill < quote.min_order:
            continue
        market = engine.by_condition.get(quote.condition_id)
        schedule = market.fee_schedule if market else None
        if not engine.ledger.buy(
            strategy, condition_id=quote.condition_id, token_id=token_id,
            shares=fill, price=quote.price, tag="reward_fill",
        ):
            continue
        quote.shares -= fill
        quote.accumulated = max(0.0, quote.accumulated - fill)
        engine._fills[strategy] += 1
        if schedule is not None:
            rebate = maker_fee_rebate(quote.price, fill, schedule)
            if rebate > 0.0:
                engine.ledger.credit(strategy, amount=rebate, detail="maker_rebate")
        log.info(f"ctf fill {fill:.1f} @ {quote.price:.4f} on {token_id[:12]}")
