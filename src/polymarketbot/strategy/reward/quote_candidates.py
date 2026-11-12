"""Allocator candidates for every reward market quoting inside the band."""

from __future__ import annotations

from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.books import best_ask, best_bid, mid
from polymarketbot.strategy.reward.competition import (
    CompetingEstimate,
    CompetingScoreWindow,
    market_competing_detail,
)
from polymarketbot.strategy.reward.fill_measurement import (
    MarketFillYield,
    expected_fill_yield,
)
from polymarketbot.strategy.reward.scoring import single_sided_score_fraction
from polymarketbot.strategy.venue.reads import Market, Print

log = get_logger("polymarketbot.strategy.reward.decision")


def _cheaper_leg(
    market: Market, books: dict[str, dict[str, Any]]
) -> tuple[float, str, dict[str, Any]] | None:
    """(leg mid, token_id, book) for the lower-priced leg of a binary market.

    The leg price is ``mid`` of a token below 0.5, otherwise ``1 - mid`` of the
    complementary token.  When both books price, the cheaper leg wins; when
    neither does, returns ``None``.  Units: the mid is a fraction of 1.00 USDC
    per share; the token id is the leg's CLOB token.
    """
    best: tuple[float, str, dict[str, Any]] | None = None
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




def reward_candidates(
    markets: list[Market],
    books: dict[str, dict[str, Any]],
    tape_prints: list[Print],
    fill_horizon_s: float | None = None,
    competitor: CompetingScoreWindow | None = None,
    now: float | None = None,
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
    :func:`polymarketbot.strategy.reward.scoring.single_sided_score_fraction`).

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
    upper bound understates our share, which is the safe direction; the
    candidate carries ``venue_denominator_low`` and ``denominator_bias_high``
    so a caller can state the bracket instead of hiding it. A market whose
    books or mids are missing, or whose measured total is zero, is refused here
    rather than priced at a floor: the denominator is the claim's only evidence.

    ``competitor`` (with the sample clock ``now``) turns that single snapshot
    into the venue's epoch-average estimate: every candidate is observed into
    the rolling window, and ``competing_score`` is the window **mean**, with
    ``competing_samples``, ``competing_variance`` and
    ``competing_low_confidence`` stating how much evidence stands behind it. A
    one-sample window is reported as low-confidence, never as the epoch rate.
    Without a window the candidate carries one snapshot and is likewise marked
    low-confidence.

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
    unpriced = 0
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
        detail = market_competing_detail(market, books)
        if detail is None:
            continue
        estimate: CompetingEstimate | None = None
        if competitor is not None:
            if now is None:
                raise ValueError(
                    "reward_candidates needs the sample clock `now` when a "
                    "competitor window is supplied: a timestamp invented here "
                    "would place the reading in the wrong epoch"
                )
            estimate = competitor.observe(market.condition_id, detail.total, float(now))
        score = detail.total if estimate is None else estimate.mean
        samples = 1 if estimate is None else estimate.samples
        low_confidence = True if estimate is None else estimate.low_confidence
        min_size = max(market.reward_min_size, market.min_order)
        fill_yield: MarketFillYield | None = None
        if fill_horizon_s is not None:
            # With a stated quote horizon the fill side is mandatory: a bid
            # whose chance of filling is unmeasured cannot be priced against
            # the accrual it gives up when it fills, so the market is refused
            # rather than valued on half a measurement.
            fill_yield = expected_fill_yield(
                tick=market.tick,
                min_flow_shares=min_size,
                horizon_s=float(fill_horizon_s),
                quote_price=quote_price,
                leg_mid=leg_mid,
            )
            if fill_yield is None:
                unpriced += 1
                continue
        candidates.append(
            {
                "condition_id": market.condition_id,
                "question": market.question,
                "daily_pool": market.daily_reward,
                "competing_score": score,
                # Provenance of the denominator: how many venue-cadence
                # readings the mean came from, their variance, and whether a
                # single snapshot is all the evidence there is.
                "competing_snapshot": detail.total,
                "competing_samples": samples,
                "competing_variance": None if estimate is None else estimate.variance,
                "competing_low_confidence": low_confidence,
                # Q_one+Q_two is an upper bound on the venue's sum Q_min:
                # ours overstates by at most `denominator_bias_high`x.
                "venue_denominator_low": detail.venue_denominator_low,
                "denominator_bias_high": detail.bias_factor_high,
                "max_spread": market.reward_max_spread_cents,
                "min_size": min_size,
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
                # One-sided pillar: one share commits its own quote price.
                "capital_per_share": quote_price,
                "quote_token": token_id,
                "quote_side": "bid",
                "tick": market.tick,
                "lot_price": quote_price,
                # Expected fill side, both halves measured on the L2 tape. The
                # optimiser maximises accrual + fill yield, so a book that never
                # trades can no longer win on its thin competing score alone:
                # the share it claims is only earned while the bid rests
                # in-band and unfilled.
                "p_fill": None if fill_yield is None else fill_yield.p_fill,
                "stay_fraction": 1.0 if fill_yield is None else fill_yield.stay_fraction,
                "fill_yield_usd_per_usd_per_day": (
                    0.0 if fill_yield is None else fill_yield.fill_yield_usd_per_usd_per_day
                ),
            }
        )
    if unpriced:
        log.info(
            f"reward candidates: refused {unpriced} market(s) whose fill probability "
            f"is unmeasured at a {fill_horizon_s:.0f}s horizon for their tick and "
            f"required flow (a resting bid priced without it claims an accrual it "
            f"may not keep)"
        )
    return candidates


