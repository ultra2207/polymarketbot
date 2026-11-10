"""The reward pillar's runner: accrue the measured pool share, quote, and fill.

The payout is NOT read from the optimizer's plan: it is recomputed every tick
from what is actually resting and from the measured books. The fill model that
turns measured prints into ledger entries is shared with the CTF pillar, so it
lives here next to the quotes it was written for.
"""

from __future__ import annotations

import time

from polymarketbot.logs import get_logger
from polymarketbot.paper import venue
from polymarketbot.paper.actions import Resting
from polymarketbot.paper.books import shares_at
from polymarketbot.paper.pillars import reward

log = get_logger(__name__)


def run_reward(engine, books: dict[str, dict], prints: list[venue.Print]) -> None:
    now = time.time()
    dt = max(0.0, now - engine._reward_accrual_ts)
    engine._reward_accrual_ts = now
    if dt > 0.0:
        # Resting liquidity earns the pool's daily payout pro rata. The
        # payout is NOT read from the optimizer's plan: it is recomputed
        # every tick from what is actually resting and from the measured
        # books - our in-band score on the leg against the pair-wide
        # measured competing score of both outcome tokens. A quote whose
        # denominator cannot be measured earns nothing and is counted, so a
        # window can never book a share of a pool it never measured.
        accrued = 0.0
        priced = refused = unqualified = 0
        pools = 0.0
        notional = 0.0
        competing: list[float] = []
        top: list[tuple[float, str, float, float]] = []
        # One quote per market: the pillar quotes the cheaper leg only, so
        # this set must have the same size as ``priced``. Logged so a market
        # paying its pool twice would be visible in a single line.
        priced_markets: set[str] = set()
        for quote in engine.reward_quotes.values():
            if quote.cancel_at is not None or quote.shares <= 0.0:
                continue
            market = engine.by_condition.get(quote.condition_id)
            if market is None or not market.binary:
                refused += 1
                continue
            others = [t for t in market.token_ids if t != quote.token_id]
            if len(others) != 1:
                refused += 1
                continue
            share = reward.measured_accrual(
                pool_usd_per_day=market.daily_reward,
                price=quote.price,
                shares=quote.shares,
                min_size=quote.min_order,
                max_spread_cents=market.reward_max_spread_cents,
                leg_book=books.get(quote.token_id),
                other_book=books.get(others[0]),
            )
            if share is None:
                refused += 1
                continue
            priced += 1
            priced_markets.add(quote.condition_id)
            pools += share.pool_usd_per_day
            notional += quote.shares * quote.price
            competing.append(share.competing_score)
            if not share.qualifies:
                unqualified += 1
                continue
            accrued += share.usd_per_day * dt / 86_400.0
            top.append((share.usd_per_day, quote.condition_id,
                        share.competing_score, share.share))
        if accrued > 0.0:
            engine.ledger.credit("RewardHarvester", amount=accrued,
                                 detail="reward_accrual")
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
                "since": now, "accrued": 0.0, "notional_seconds": 0.0,
                "equity0": engine.ledger.equity().get("RewardHarvester", 0.0),
            }
        window["accrued"] += accrued
        window["notional_seconds"] += notional * dt
        if now - engine._reward_log_ts >= 60.0:
            engine._reward_log_ts = now
            elapsed = max(1e-9, now - float(window["since"]))
            window_accrued = float(window["accrued"])
            window_implied = window_accrued * 86_400.0 / elapsed
            window_notional = float(window["notional_seconds"]) / elapsed
            equity_now = engine.ledger.equity().get("RewardHarvester", 0.0)
            equity_delta = equity_now - float(window["equity0"])
            equity_rate = equity_delta * 86_400.0 / elapsed
            venue_pool = sum(m.daily_reward for m in engine.markets)
            rewarded_count = sum(1 for m in engine.markets if m.daily_reward > 0)
            depth = sorted(competing)
            median_d = depth[len(depth) // 2] if depth else 0.0
            # The claim must be auditable against the venue's own budget:
            # share of the measured program, share of the pools we cover,
            # and the largest single claim with the denominator behind it.
            biggest = sorted(top, reverse=True)[:3]
            top_txt = " ".join(
                f"{cid[:8]}=${usd:,.2f}/day(D={d:,.1f} shares,share={share * 100.0:.1f}%)"
                for usd, cid, d, share in biggest
            ) or "none"
            log.info(
                f"reward accrual: {priced} quotes over {len(priced_markets)} "
                f"markets priced, {unqualified} "
                f"not qualifying, {refused} refused (unmeasured books or "
                f"denominator) | credited ${window_accrued:,.4f} over "
                f"{elapsed:.0f}s = ${window_implied:,.2f}/day on a time-average "
                f"${window_notional:,.2f} resting "
                f"({window_implied / window_notional * 100.0 if window_notional > 0 else 0.0:.2f}%/day); "
                f"equity moved ${equity_delta:+,.4f} = ${equity_rate:,.2f}/day "
                f"({100.0 * equity_rate / window_implied - 100.0 if window_implied > 0 else 0.0:+.1f}% "
                f"vs credited, same window) | ${window_implied / venue_pool * 100.0 if venue_pool > 0 else 0.0:.3f}% of the "
                f"${venue_pool:,.0f}/day venue pool measured over {rewarded_count} "
                f"rewarded markets and "
                f"{window_implied / pools * 100.0 if pools > 0 else 0.0:.1f}% of the "
                f"${pools:,.0f}/day of pools it covers | measured pair-wide "
                f"competing score: median {median_d:,.0f} | top claims: {top_txt}"
            )
            engine._reward_window = {
                "since": now, "accrued": 0.0, "notional_seconds": 0.0,
                "equity0": equity_now,
            }
    apply_resting_fills(engine, engine.reward_quotes, "RewardHarvester", books, prints, now)
    rewarded = [m for m in engine.markets if m.daily_reward > 0]
    # The horizon the pillar claims its yield over is one engine interval:
    # the plan is re-solved against fresh books every tick, so a quote that
    # survives is re-justified each tick at the same per-interval
    # arithmetic. That is the horizon the measured P(fill) is read at.
    candidates = reward.reward_candidates(
        rewarded, books, prints, fill_horizon_s=engine.interval
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
    for token_id, quote in wanted.items():
        current = engine.reward_quotes.get(token_id)
        if current and abs(current.price - quote.price) < 1e-9:
            current.daily_payout = quote.daily_payout
            continue
        if current is not None and current.cancel_at is None:
            current.cancel_at = now
            engine.actions.submit("reward_cancel", "RewardHarvester",
                                  {"token_id": token_id})
        landing = now + engine._rtt()
        resting = Resting("RewardHarvester", quote.condition_id, token_id,
                          quote.price, quote.shares, quote.min_size, landing,
                          tick=engine.by_condition[quote.condition_id].tick,
                          daily_payout=quote.daily_payout, seen_ts=landing,
                          initial_shares=quote.shares)
        engine.actions.submit("reward_quote", "RewardHarvester",
                              {"token_id": token_id, "resting": resting})
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


def apply_resting_fills(
    engine,
    resting: dict[str, Resting],
    strategy: str,
    books: dict[str, dict],
    prints: list[venue.Print],
    now: float,
) -> None:
    for token_id, quote in list(resting.items()):
        if now < quote.placed:
            continue
        if quote.max_age_s and now > quote.placed + quote.max_age_s:
            # The EV that justified this quote was measured over exactly
            # this window; past it the quote rests on an unmeasured
            # probability.
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
        fill = reward.measured_fill_shares(quote.price, quote.shares, level_shares,
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
            rebate = reward.maker_rebate(quote.price, fill, schedule)
            if rebate > 0.0:
                engine.ledger.credit(strategy, amount=rebate, detail="maker_rebate")
        log.info(f"reward fill {fill:.1f} @ {quote.price:.4f} on {token_id[:12]}")
