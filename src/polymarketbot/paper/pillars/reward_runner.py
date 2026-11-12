"""The reward pillar's runner: accrue the measured pool share, quote, and fill.

The payout is NOT read from the optimizer's plan: it is recomputed every tick
from what is actually resting and from the measured books. The fill model that
turns measured prints into ledger entries is shared with the CTF pillar, so it
lives here next to the quotes it was written for.

The competing score in that denominator is the venue's *epoch average*, not one
snapshot: the venue samples the book 1,440 times a day and divides by the mean
of those samples, so a single reading is fed into a rolling window (one reading
per documented sample interval) and every rate this module prints carries the
number of readings behind it. Per-market accrual is journaled each tick so the
modelled number can be joined against the venue's payout report by condition and
UTC day, and maker rebates are credited from the venue's own per-wallet number
when it can be read.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

import polymarketbot.strategy.reward.decision as reward
import polymarketbot.strategy.venue.reads as venue
from polymarketbot.logs import get_logger
from polymarketbot.paper import micro as paper_micro
from polymarketbot.paper import risk as paper_risk
from polymarketbot.paper import sizing as paper_sizing
from polymarketbot.paper.actions import Resting, new_order_id, queue_tracker
from polymarketbot.paper.deskew import skew_target
from polymarketbot.strategy.books import ladder
from polymarketbot.strategy.reward.accrual import (
    REWARD_ELIGIBILITY_SECONDS,
    RewardDayAccrual,
)
from polymarketbot.strategy.reward.rebates import RebateAccountant, RebateCredit

log = get_logger(__name__)


def utc_day(ts: float) -> str:
    """The UTC epoch day (``YYYY-MM-DD``) a timestamp falls in."""
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d")


def day_accruals(engine) -> dict[str, RewardDayAccrual]:
    """The engine's per-condition UTC-day accumulators, created on first use.

    The venue pays a market per UTC day, so accrual is banked at that grain and
    only a settled day (total ``>= $1.00``) becomes cash.
    """
    accruals = getattr(engine, "_reward_accruals", None)
    if accruals is None:
        accruals = engine._reward_accruals = {}
    return accruals


def competitor_window(engine) -> reward.CompetingScoreWindow:
    """The engine's rolling competitor-score window, created on first use."""
    window = getattr(engine, "_reward_competitor", None)
    if window is None:
        window = reward.CompetingScoreWindow()
        engine._reward_competitor = window
    return window


def rebates(engine) -> RebateAccountant:
    """The engine's rebate accountant, created on first use.

    The venue source needs a CLOB client with a maker address; the paper engine
    has none, so the accountant reports ``modelled`` and says why. A live
    engine that exposes ``clob`` gets the venue's own number.
    """
    accountant = getattr(engine, "_reward_rebates", None)
    if accountant is None:
        accountant = RebateAccountant(getattr(engine, "clob", None))
        engine._reward_rebates = accountant
    return accountant


def sync_rebates(engine, now: float, day: str) -> list[RebateCredit]:
    """Credit the venue's rebate increments for ``day`` (rate-limited)."""
    accountant = rebates(engine)
    credits = accountant.sync(now, day)
    for credit in credits:
        engine.ledger.credit(
            "RewardHarvester",
            amount=credit.usd,
            condition_id=credit.condition_id,
            detail=credit.detail,
        )
    return credits


def run_reward(engine, books: dict[str, dict], prints: list[venue.Print]) -> None:
    now = time.time()
    day = utc_day(now)
    dt = max(0.0, now - engine._reward_accrual_ts)
    engine._reward_accrual_ts = now
    # Rebates accrue continuously on the venue's side; poll its running daily
    # total (rate-limited inside the accountant) and credit the increment. This
    # runs before the accrual log and the fill stage, so both report and price
    # the source this tick actually used. When the endpoint cannot be read the
    # accountant switches to the modelled per-fill number and says why.
    sync_rebates(engine, now, day)
    if dt > 0.0:
        # Resting liquidity earns the pool's daily payout pro rata. The
        # payout is NOT read from the optimizer's plan: it is recomputed
        # every tick from what is actually resting and from the measured
        # books - the pair's venue Q_min across the YES and NO bids against
        # the pair-wide measured competing score of both outcome tokens,
        # averaged over the window of venue-cadence samples. A market whose
        # denominator cannot be measured earns nothing and is counted, so a
        # window can never book a share of a pool it never measured.
        accrued = 0.0
        settled = 0.0
        priced = refused = unqualified = 0
        pools = 0.0
        notional = 0.0
        competing: list[float] = []
        sample_counts: list[int] = []
        sample_sds: list[float] = []
        low_confidence = 0
        top: list[tuple[float, str, float, float]] = []
        # Day accrual is banked per (condition, UTC day) and becomes cash only
        # when the day settles at rollover against the venue's $1.00 minimum -
        # never by flooring the instantaneous rate.
        accruals = day_accruals(engine)
        # One aggregate rate/anchor per condition this tick: a condition that
        # holds more than one quote must not have its interval counted twice.
        rates: dict[str, float] = {}
        anchors: dict[str, float] = {}
        resting_conditions: set[str] = set()
        window_scores = competitor_window(engine)
        # One candidate per market is one matched pair: both legs rest at one
        # share count, so the venue pays one pair-level Q_min and this set must
        # have the same size as ``priced``. Grouping by condition is what stops
        # the two legs being summed into two one-sided claims against one pool.
        priced_markets: set[str] = set()
        resting_by_condition: dict[str, list[Resting]] = {}
        for quote in engine.reward_quotes.values():
            if quote.cancel_at is not None or quote.shares <= 0.0:
                continue
            resting_conditions.add(quote.condition_id)
            resting_by_condition.setdefault(quote.condition_id, []).append(quote)
        for condition_id, pair_quotes in resting_by_condition.items():
            market = engine.by_condition.get(condition_id)
            if market is None or not market.binary or len(market.token_ids) != 2:
                refused += 1
                continue
            by_token = {quote.token_id: quote for quote in pair_quotes}
            if not set(by_token) <= set(market.token_ids):
                refused += 1
                continue
            # Observe the pair-wide snapshot into the window first: the venue's
            # denominator is the epoch mean, and this reading is the only
            # evidence the estimator has.
            estimate: reward.CompetingEstimate | None = None
            detail = reward.market_competing_detail(market, books)
            if detail is not None:
                estimate = window_scores.observe(condition_id, detail.total, now)
            share = reward.measured_pair_accrual(
                pool_usd_per_day=market.daily_reward,
                max_spread_cents=market.reward_max_spread_cents,
                min_size=min(quote.min_order for quote in pair_quotes),
                yes_book=books.get(market.token_ids[0]),
                no_book=books.get(market.token_ids[1]),
                # The venue scores the pair once: YES bid in Q_one, NO bid in
                # Q_two, combined by the two-sided rule. A half-filled pair
                # falls back to the venue's own one-sided Q_one / c.
                resting=tuple(
                    reward.PairLeg(
                        outcome="YES" if token_id == market.token_ids[0] else "NO",
                        price=quote.price,
                        shares=quote.shares,
                    )
                    for token_id, quote in by_token.items()
                ),
                competing=estimate,
                # The paper trader quotes into a simulated book: the venue L2
                # snapshot it reads never contains the order, so the measured
                # competitor total is clean and our score is added once. A
                # caller reading the venue's own book with its order resting
                # there states True, or it divides our claim by itself twice.
                books_include_our_order=False,
            )
            if share is None:
                refused += 1
                continue
            priced += 1
            priced_markets.add(condition_id)
            pools += share.pool_usd_per_day
            notional += sum(quote.shares * quote.price for quote in pair_quotes)
            competing.append(share.competing_score)
            sample_counts.append(share.competing_samples)
            if share.competing_variance:
                sample_sds.append(math.sqrt(share.competing_variance))
            if share.low_confidence:
                low_confidence += 1
            if not share.qualifies:
                unqualified += 1
                continue
            # The venue only counts an order that has rested at least 3.5 s.
            # The pair accrues only once *both* legs are eligible, so the
            # anchor is the later landing: charging the pair rate while one leg
            # is still inside the pre-eligibility interval would claim a
            # two-sided score for a one-sided book.
            eligible_from = max(
                float(quote.placed) + REWARD_ELIGIBILITY_SECONDS
                for quote in pair_quotes
            )
            if now > eligible_from:
                rates[condition_id] = share.usd_per_day
                anchors[condition_id] = eligible_from
            top.append((share.usd_per_day, condition_id,
                        share.competing_score, share.share))
        # Record every resting condition, including the measured zeros: an
        # interval with no eligible rate must still advance the accumulator's
        # clock or the next rate would be charged for it. ``journal`` keeps the
        # tick's increment per (UTC day, condition), because an interval that
        # crosses midnight belongs to two venue payout periods.
        today = datetime.fromtimestamp(now, UTC).date()
        journal: dict[tuple[str, str], float] = {}
        for condition_id in resting_conditions:
            account = accruals.get(condition_id)
            if account is None:
                account = accruals[condition_id] = RewardDayAccrual(condition_id)
            before = {d: account.pending_utc_day(d) for d in account.pending_days()}
            rate = rates.get(condition_id, 0.0)
            if rate > 0.0:
                account.record(anchors[condition_id], 0.0)
                account.record(now, rate)
            else:
                account.record(now, 0.0)
            for accrual_day in set(before) | set(account.pending_days()):
                delta = account.pending_utc_day(accrual_day) - before.get(accrual_day, 0.0)
                if delta == 0.0:
                    continue
                key = (accrual_day.isoformat(), condition_id)
                journal[key] = journal.get(key, 0.0) + delta
                accrued += delta
            if condition_id in priced_markets:
                # Journal every priced market each tick, including a measured
                # zero: a market that was priced and earned nothing is
                # evidence, and a missing row would be indistinguishable from a
                # market never priced.
                journal.setdefault((day, condition_id), 0.0)
        # Settle every completed UTC day. The $1.00 minimum is applied to the
        # day's total here, at rollover, and nowhere else.
        for condition_id, account in accruals.items():
            for accrual_day in account.pending_days():
                if accrual_day >= today:
                    continue
                usd = account.settle_utc_day(accrual_day)
                if usd <= 0.0:
                    continue
                settled += usd
                engine.ledger.credit(
                    "RewardHarvester",
                    amount=usd,
                    condition_id=condition_id,
                    detail="reward_accrual",
                )
        for (journal_day, condition_id), usd in journal.items():
            engine.ledger.record_reward_accrual(
                day=journal_day,
                condition_id=condition_id,
                accrued_usd=usd,
                priced_markets=len(priced_markets),
                ts=now,
            )
        # Window bookkeeping: the implied rate and the equity it produced
        # must be integrated over the *same* interval, or the two numbers
        # cannot be compared at all. Measured 2026-11-10 window 9: an
        # instantaneous reading swung $104 -> $472.91/day (49.37 %/day)
        # between ticks while the equity delta over the same 30 minutes was
        # +$5.95 (~$286/day) - a 1.65x "disagreement" that was purely the
        # difference between a point estimate and a time average.
        window = engine._reward_window
        if window is None:
            window = engine._reward_window = {
                "since": now, "accrued": 0.0, "settled": 0.0,
                "notional_seconds": 0.0,
                "equity0": engine.ledger.equity().get("RewardHarvester", 0.0),
            }
        window["accrued"] += accrued
        window["settled"] = window.get("settled", 0.0) + settled
        window["notional_seconds"] += notional * dt
        if now - engine._reward_log_ts >= 60.0:
            engine._reward_log_ts = now
            elapsed = max(1e-9, now - float(window["since"]))
            window_accrued = float(window["accrued"])
            window_settled = float(window.get("settled", 0.0))
            window_implied = window_accrued * 86_400.0 / elapsed
            window_notional = float(window["notional_seconds"]) / elapsed
            equity_now = engine.ledger.equity().get("RewardHarvester", 0.0)
            equity_delta = equity_now - float(window["equity0"])
            equity_rate = equity_delta * 86_400.0 / elapsed
            venue_pool = sum(m.daily_reward for m in engine.markets)
            rewarded_count = sum(1 for m in engine.markets if m.daily_reward > 0)
            depth = sorted(competing)
            median_d = depth[len(depth) // 2] if depth else 0.0
            counts = sorted(sample_counts)
            median_n = counts[len(counts) // 2] if counts else 0
            sds = sorted(sample_sds)
            median_sd = sds[len(sds) // 2] if sds else None
            # The claim must be auditable against the venue's own budget:
            # share of the measured program, share of the pools we cover,
            # and the largest single claim with the denominator behind it.
            biggest = sorted(top, reverse=True)[:3]
            top_txt = " ".join(
                f"{cid[:8]}=${usd:,.2f}/day(D={d:,.1f} shares,share={share * 100.0:.1f}%)"
                for usd, cid, d, share in biggest
            ) or "none"
            # Two known biases, stated every window rather than buried:
            # the competitor total is Q_one+Q_two (the venue sums Q_min, so it
            # overstates competition by at most 1+c and understates our share),
            # and the spread is measured from the raw L2 mid because the
            # venue's "size-cutoff-adjusted midpoint" is named but never
            # defined. Our own claim is now the pair's Q_min, the venue's own
            # number, so only the competitor side carries the bound.
            bias_high = 1.0 + reward.SINGLE_SIDED_PENALTY
            log.info(
                f"reward accrual: {priced} matched pairs over "
                f"{len(priced_markets)} markets priced, {unqualified} "
                f"not qualifying, {refused} refused (unmeasured books or "
                f"denominator) | pending ${window_accrued:,.4f} over "
                f"{elapsed:.0f}s = ${window_implied:,.2f}/day on a time-average "
                f"${window_notional:,.2f} resting "
                f"({window_implied / window_notional * 100.0 if window_notional > 0 else 0.0:.2f}%/day); "
                f"settled ${window_settled:,.4f} at UTC rollover ($1.00 daily "
                f"floor applied per market); "
                f"equity moved ${equity_delta:+,.4f} = ${equity_rate:,.2f}/day "
                f"({100.0 * equity_rate / window_implied - 100.0 if window_implied > 0 else 0.0:+.1f}% "
                f"vs pending, same window) | ${window_implied / venue_pool * 100.0 if venue_pool > 0 else 0.0:.3f}% of the "
                f"${venue_pool:,.0f}/day venue pool measured over {rewarded_count} "
                f"rewarded markets and "
                f"{window_implied / pools * 100.0 if pools > 0 else 0.0:.1f}% of the "
                f"${pools:,.0f}/day of pools it covers | measured pair-wide "
                f"competing score (epoch-average estimate): median {median_d:,.0f}, "
                f"median n={median_n} sample(s), "
                f"median sd={'n/a' if median_sd is None else f'{median_sd:,.1f}'}, "
                f"{low_confidence}/{priced} "
                f"low-confidence | denominator bias: competitor total "
                f"(Q_one+Q_two) overstates venue sum(Q_min) by 1.0-{bias_high:.1f}x "
                f"-> our pair share understated the same factor (conservative) | "
                f"spread bias: measured from the raw L2 mid, venue uses the "
                f"size-cutoff-adjusted mid (adjustment "
                f"not published; direction unknown) | rebates: {rebates(engine).describe()} "
                f"| top claims: {top_txt}"
            )
            engine._reward_window = {
                "since": now, "accrued": 0.0, "notional_seconds": 0.0,
                "equity0": equity_now,
            }
    if paper_risk.blocks_fills(engine):
        # A permanent breaker cancels the resting maker quotes; replaying the
        # tape into them after the halt would book fills the live venue would
        # not have. The cancellation still travels the measured round trip, so
        # the fill pass drains the retired orders here and no replacement is
        # planned until an operator resumes.
        paper_risk.cancel_quotes(engine, None, "risk breaker active")
        apply_resting_fills(
            engine, engine.reward_quotes, "RewardHarvester", books, prints, now
        )
        return
    apply_resting_fills(engine, engine.reward_quotes, "RewardHarvester", books, prints, now)
    rewarded = [m for m in engine.markets if m.daily_reward > 0]
    # The horizon the pillar claims its yield over is one engine interval:
    # the plan is re-solved against fresh books every tick, so a quote that
    # survives is re-justified each tick at the same per-interval
    # arithmetic. That is the horizon the measured P(fill) is read at.
    candidates = reward.reward_candidates(
        rewarded,
        books,
        prints,
        fill_horizon_s=engine.interval,
        competitor=competitor_window(engine),
        now=now,
    )
    # The allocator answers "where would cash go on an empty book"; the
    # engine must also respect the notional already resting. Handing it the
    # bare balance each tick let resting reward quotes reach 1445.65 on a
    # 1000.51 cash balance (measured 10:33:48 this window), so a
    # simultaneous fill set would be refused by the ledger after the
    # decision to take it had already been made.
    shadow_state: dict = {}
    wanted = {
        q.token_id: q
        for q in reward.plan_quotes(
            candidates,
            max(0.0, engine.ledger.cash("RewardHarvester")),
            {
                token_id: (quote.price, quote.shares)
                for token_id, quote in engine.reward_quotes.items()
                if quote.cancel_at is None and quote.shares > 0.0
            },
            state=shadow_state,
        )
    }
    # The optimum's KKT price of capital: the hurdle every other pillar
    # must clear before it spends the same dollar. None = not solved, which
    # the CTF gate treats as missing data and refuses on.
    engine._shadow_yield = shadow_state.get("shadow_yield")
    # The shared per-order cap must price the plan about to be validated: live
    # derives it from its measured sizer size, and paper's measurement is the
    # planner's own leg size. Observed from the plan (never from resting state
    # and never from a quote the gate refuses) before the placement loop, so
    # every leg below goes through ``validate_quote`` under this tick's size.
    paper_sizing.record(engine, wanted.values(), now=now)
    for token_id, quote in wanted.items():
        current = engine.reward_quotes.get(token_id)
        # A passive fade re-prices this leg; the pair is no longer the symmetric
        # bid pair the planner priced, so the deskew price is what rests.
        skew = skew_target(engine, token_id)
        price = float(quote.price) if skew is None else float(skew.price)
        # An order is gone once it has fully filled (``shares == 0``) or has
        # been cancelled. The fill path leaves a fully-filled quote in place
        # with ``cancel_at is None``, so treating merely "present at the same
        # price" as still resting silenced that market for the rest of the
        # run: the planner wanted a quote and the engine answered with the
        # zero share remainder. Re-place whenever the order is gone, on the
        # same cash/inventory gate the planner already applied.
        gone = (
            current is None
            or current.shares <= 0.0
            or current.cancel_at is not None
        )
        # The shared risk gate runs before the "still resting" short-circuit:
        # breakers, the per-asset cooldown, the order/position/notional caps and
        # the cash headroom all apply to a quote that is already resting too.
        allowed, reason, approved_size, approved_price = paper_risk.quote_denial(
            engine,
            condition_id=quote.condition_id,
            token_id=token_id,
            price=price,
            shares=float(quote.shares),
            books=books,
            tick_size=float(engine.by_condition[quote.condition_id].tick),
            now=now,
        )
        if not allowed:
            log.warning(f"reward quote refused for {token_id[:12]}: {reason}")
            if current is not None and not gone:
                current.cancel_at = now
                engine.actions.submit(
                    "reward_cancel", "RewardHarvester", {"token_id": token_id}
                )
            continue
        price = float(approved_price)
        shares = float(approved_size)
        if shares + 1e-9 < float(quote.min_size):
            # A risk-clamped order below the venue minimum can never fill or
            # score. Refuse it rather than rest a quote that only looks active.
            log.warning(
                f"reward quote suppressed for {token_id[:12]}: risk-approved size "
                f"{shares:.2f} is below the venue minimum {quote.min_size:.2f}"
            )
            if current is not None and not gone:
                current.cancel_at = now
                engine.actions.submit(
                    "reward_cancel", "RewardHarvester", {"token_id": token_id}
                )
            continue
        if shares + 1e-9 < float(quote.shares):
            log.info(
                f"reward quote {token_id[:12]} clamped by risk: "
                f"{quote.shares:.2f} -> {shares:.2f} shares"
            )
        if (
            current is not None
            and not gone
            and abs(current.price - price) < 1e-9
            and abs(current.shares - shares) < 1e-9
        ):
            current.daily_payout = quote.daily_payout
            continue
        if current is not None and not gone and current.cancel_at is None:
            current.cancel_at = now
            engine.actions.submit("reward_cancel", "RewardHarvester",
                                  {"token_id": token_id})
        landing = now + engine._rtt()
        resting = Resting("RewardHarvester", quote.condition_id, token_id,
                          price, shares, quote.min_size, landing,
                          tick=engine.by_condition[quote.condition_id].tick,
                          daily_payout=quote.daily_payout, seen_ts=landing,
                          initial_shares=shares)
        engine.actions.submit("reward_quote", "RewardHarvester",
                              {"token_id": token_id, "resting": resting})
        if skew is not None and abs(price - float(quote.price)) > 1e-9:
            log.info(
                f"reward quote {token_id[:12]} placed at de-skew "
                f"{skew.pathway} ${price:.3f} (planner ${quote.price:.3f}): "
                f"{skew.reason}"
            )
    if not wanted and engine._reserved(engine.reward_quotes) > 0.0:
        # Cash is already committed to resting bids: hold them rather than
        # cancelling and re-quoting the same liquidity every tick.
        return
    for token_id, current in list(engine.reward_quotes.items()):
        if token_id in wanted:
            continue
        if current.cancel_at is None:
            current.cancel_at = now
            engine.actions.submit("reward_cancel", "RewardHarvester",
                                  {"token_id": token_id})
        if current.cancel_at is not None and now >= current.cancel_at + engine._rtt():
            engine.reward_quotes.pop(token_id, None)
            if current.order_id:
                queue_tracker(engine).drop(token_id, current.order_id)
            # The market is no longer quoted here, so its estimator window has
            # no reader: drop it rather than letting the dict grow with every
            # market the pillar ever touched.
            competitor_window(engine).forget(current.condition_id)


def _with_own_order(
    levels: list[tuple[float, float]], price: float, size: float
) -> list[tuple[float, float]]:
    """The ladder the venue would publish while our order rests.

    The tracker's ``on_book`` contract is the venue's aggregated depth, which
    includes our own order: ``ahead = min(prev, observed - size)``. The paper
    engine's simulated book does not contain the virtual order, so the fill
    pass adds it at our price before every snapshot. Without this the first
    snapshot would read the size *ahead* of us as the whole level and clear it
    (``observed - size``), putting the order at the front of a queue it is
    still behind.
    """
    if size <= 0.0:
        return levels
    merged: list[tuple[float, float]] = []
    placed = False
    for level_price, level_size in levels:
        if abs(level_price - price) <= 1e-12:
            merged.append((level_price, level_size + size))
            placed = True
        else:
            merged.append((level_price, level_size))
    if not placed:
        merged.append((price, size))
    return merged


def _fifo_fill(quote: Resting, progress) -> float:
    """Shares this pass consumed from the resting order, from the tracker.

    FIFO consumption is cumulative: prints at our price first clear the size
    that was ahead of us when we landed, and only the overflow fills us. A
    print strictly through our price fills the whole remaining order. Both
    facts are carried by :class:`~polymarketbot.risk.contracts.QueueProgress`,
    so the fill is ``consumed - already booked`` and never a pro-rata share of
    the level.
    """
    if progress is None or not progress.filled:
        return 0.0
    size = quote.initial_shares or quote.shares
    if progress.traded_at_level <= progress.initial_ahead:
        # No printed volume at our level, yet the order is marked filled: a
        # print went through our price. The rest of the order is gone.
        consumed = size
    else:
        consumed = min(size, progress.traded_at_level - progress.initial_ahead)
    return max(0.0, consumed - quote.booked_fill)


def apply_resting_fills(
    engine,
    resting: dict[str, Resting],
    strategy: str,
    books: dict[str, dict],
    prints: list[venue.Print],
    now: float,
) -> None:
    tracker = queue_tracker(engine)
    if paper_risk.blocks_fills(engine):
        # No maker fill may book after a permanent breaker: the live venue is
        # cancelling these orders. Drop the retired records so a later resume
        # cannot replay tape that arrived during the halt into them; a taker
        # exit already in flight is a separate, explicit landing.
        for token_id, quote in list(resting.items()):
            if quote.order_id:
                tracker.drop(token_id, quote.order_id)
                paper_micro.cancel(engine, token_id, quote.order_id)
            resting.pop(token_id, None)
        return
    ordered_prints = sorted(prints, key=lambda p: (p.ts, getattr(p, "seq", 0)))
    for token_id, quote in list(resting.items()):
        if now < quote.placed:
            continue
        if quote.max_age_s and now > quote.placed + quote.max_age_s:
            # The EV that justified this quote was measured over exactly
            # this window; past it the quote rests on an unmeasured
            # probability.
            if quote.order_id:
                tracker.drop(token_id, quote.order_id)
                paper_micro.cancel(engine, token_id, quote.order_id)
            resting.pop(token_id, None)
            continue
        if quote.cancel_at is not None and now >= quote.cancel_at + engine._rtt():
            if quote.order_id:
                tracker.drop(token_id, quote.order_id)
                paper_micro.cancel(engine, token_id, quote.order_id)
            resting.pop(token_id, None)
            continue
        book = books.get(token_id) or {}
        bids = ladder(book, asks=False)
        asks = ladder(book, asks=True)
        if not quote.order_id:
            # A quote that reached this pass without landing (a resumed ledger
            # or a caller that injected it) is registered at the first book it
            # is observed against. That is the only snapshot available, and it
            # is stated as the entry book rather than silently assumed empty.
            quote.order_id = new_order_id(strategy, token_id)
            quote.initial_shares = quote.initial_shares or quote.shares
        if tracker.progress(quote.order_id) is None:
            tracker.on_entry(
                quote.order_id, token_id, "BUY", quote.price, quote.shares,
                max(quote.placed, quote.seen_ts), bids, asks,
            )
        # One snapshot per tick: size that appears at our price after entry is
        # behind us, which the tracker enforces by clamping ``ahead``. Our own
        # resting size is added because the venue's published level includes
        # it and the simulated book does not.
        tracker.on_book(
            token_id,
            _with_own_order(bids, quote.price, quote.shares),
            asks,
            now,
        )
        market_prints = [
            p for p in ordered_prints
            if p.token_id == token_id and p.ts > quote.seen_ts
        ]
        for printed in market_prints:
            tracker.on_trade(
                token_id, printed.price, printed.size, printed.side, printed.ts
            )
        if market_prints:
            quote.seen_ts = market_prints[-1].ts
        raw = _fifo_fill(quote, tracker.progress(quote.order_id))
        # The tracker's consumption is cumulative, so it is booked here even
        # when the lot is below the venue minimum; dropping it would double
        # count the same shares on the next pass. Measured prints are 1-5
        # shares, so the sub-lot carry is what lets a 20-200 share minimum
        # ever clear.
        if raw > 0.0:
            paper_micro.fill(engine, token_id, quote.price, raw, now)
        quote.booked_fill += raw
        quote.accumulated += raw
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
        # Rebate: the venue's own per-wallet number when it can be read, the
        # modelled fees.maker_fee_rebate only when it cannot. The accountant
        # decides and records which source produced the credit, so the journal
        # (and reconcile) can split venue money from modelled money.
        credit = rebates(engine).modelled_credit(
            condition_id=quote.condition_id,
            price=quote.price,
            shares=fill,
            schedule=schedule,
        )
        if credit.usd > 0.0:
            engine.ledger.credit(
                strategy,
                amount=credit.usd,
                condition_id=quote.condition_id,
                detail=credit.detail,
            )
        log.info(f"reward fill {fill:.1f} @ {quote.price:.4f} on {token_id[:12]}")
