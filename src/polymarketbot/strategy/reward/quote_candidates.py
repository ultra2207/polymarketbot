"""Allocator candidates for the paper trader's matched reward pairs.

The paper trader quotes a **pair**: one bid on each outcome token of a binary
market, at the same share count and the same measured distance from each mid.
That is the neutral quoting the live engine places (``live/quoting.py``,
``_execute_neutral_maker_quoting``) and exactly how it sizes the pair
(``live/supervisor/sizing.py``, ``matched_pair_quote_size``): one share count
funded by the pair's combined cost. This module prices the pair - both leg
prices, the venue's pair-level ``Q_min``, the measured fill side - so the
allocator funds matched pairs rather than a directional one-leg book.
"""

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
from polymarketbot.strategy.reward.scoring import (
    OrderQuote,
    compute_book_side_scores,
    compute_minimum_score,
)
from polymarketbot.strategy.venue.reads import Market, Print

log = get_logger("polymarketbot.strategy.reward.decision")


def _pair_legs(
    market: Market, books: dict[str, dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Both legs of the matched pair, priced to rest inside the venue band.

    Each leg's bid is ``min(best_bid + tick, best_ask - tick)``: the highest
    price that still *rests* below the ask, so it can neither cross on landing
    nor be filled by a print at its own price. A locked or crossed book
    publishes no mid, so it has no measurable distance and the market is
    refused. Units: prices are USDC per share, distances are cents.
    """
    legs: list[dict[str, Any]] = []
    for index, token_id in enumerate(market.token_ids[:2]):
        book = books.get(token_id)
        if not isinstance(book, dict):
            return None
        bid, ask = best_bid(book), best_ask(book)
        if bid is None or ask is None:
            return None
        leg_mid = mid(book)
        if leg_mid is None:
            # Locked/crossed: no resting price between the touch and the ask.
            return None
        price = min(bid + market.tick, ask - market.tick)
        if price < bid - 1e-12 or not 0.0 < price < 1.0:
            return None
        distance_cents = abs(price - leg_mid) * 100.0
        band_cents = market.reward_max_spread_cents - market.tick * 100.0
        if distance_cents > band_cents:
            return None
        legs.append(
            {
                "outcome": "YES" if index == 0 else "NO",
                "token_id": token_id,
                "book": book,
                "mid": leg_mid,
                "price": price,
                "distance_cents": distance_cents,
            }
        )
    return legs if len(legs) == 2 else None


def _pair_min_score(
    legs: list[dict[str, Any]], mid_yes: float, band_cents: float, shares: float
) -> float:
    """The venue's ``Q_min`` for this pair at ``shares`` per leg.

    This is the live engine's own scoring path (``compute_quote_scores``): the
    YES bid contributes to ``Q_one``, the NO bid to ``Q_two``, and the
    two-sided rule pays ``min`` when both legs are scored. A lone leg falls
    back to ``Q_one / c`` inside the band, exactly as the venue does.
    """
    q_one = q_two = 0.0
    for leg in legs:
        quote = OrderQuote(
            side="BUY",
            outcome=leg["outcome"],
            price=float(leg["price"]),
            size=float(shares),
        )
        leg_mid_yes = mid_yes if leg["outcome"] == "YES" else 1.0 - float(leg["mid"])
        one, two = compute_book_side_scores(
            [quote],
            mid_price_yes=leg_mid_yes,
            max_spread_cents=band_cents,
            min_size=0.0,
        )
        q_one += one
        q_two += two
    return compute_minimum_score(q_one=q_one, q_two=q_two, midpoint=mid_yes)


def reward_candidates(
    markets: list[Market],
    books: dict[str, dict[str, Any]],
    tape_prints: list[Print],
    fill_horizon_s: float | None = None,
    competitor: CompetingScoreWindow | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Allocator candidates for every market the trader can quote as a pair.

    A market qualifies when it pays a daily pool, publishes a positive maximum
    spread, minimum size and tick, is binary, and **both** outcome books have a
    measured bid, ask and mid. Both legs are quoted at a single share count,
    each at the highest price that still rests inside the band. The candidate
    is dropped when either leg leaves the price domain, when a leg's book is
    locked or crossed, or when either leg's distance exceeds
    ``max_spread - tick``.

    **One candidate is one matched pair.** ``capital_per_share`` /
    ``quote_price`` are the pair's combined bid cost, so the allocator's
    capital buys that many shares *per leg*; ``score_per_usd`` converts
    dollars of pair capital into the venue's pair-level ``Q_min``, computed
    once with :func:`_pair_min_score` - never the sum of two independent
    one-sided claims. ``legs`` carries each leg's token, price, mid, measured
    fill side and distance.

    ``competing_score`` is the market's measured in-band competing score, summed
    over both outcome-token books (an upper bound on the competitors' ``Q_min``,
    because every competitor's own two-sided penalty can only shrink it). A
    market whose books or mids are missing, or whose measured total is zero, is
    refused rather than priced at a floor: the denominator is the claim's only
    evidence.

    ``competitor`` (with the sample clock ``now``) turns that single snapshot
    into the venue's epoch-average estimate: every candidate is observed into
    the rolling window, and ``competing_score`` is the window mean, with
    ``competing_samples``, ``competing_variance`` and
    ``competing_low_confidence`` stating how much evidence stands behind it.

    The fill side is measured for both legs. The pair's ``stay_fraction`` is the
    probability neither leg fills (the accrual only survives while both rest);
    ``fill_yield_usd_per_usd_per_day`` is the legs' measured fill yield weighted
    by the capital each commits, per dollar of pair capital.

    Units: ``daily_pool`` is USDC per day, ``competing_score`` is the venue's
    share-weighted score, ``max_spread`` is cents, ``min_size`` is shares,
    ``real_mid``, ``quote_price``, ``pair_cost`` and ``tick`` are USDC per share,
    ``score_per_usd`` is the pair ``Q_min`` per USDC of pair capital.
    """
    candidates: list[dict[str, Any]] = []
    unpriced = 0
    for market in markets:
        if market.daily_reward <= 0.0 or market.reward_max_spread_cents <= 0.0:
            continue
        if market.tick <= 0.0 or not market.binary:
            continue
        # A rewarded market always publishes the share size that qualifies for
        # rewards. When the venue omits it, the pair cannot be scored, so the
        # market is not tradable here; a null is not a zero-size qualification.
        if market.reward_min_size <= 0.0:
            continue
        legs = _pair_legs(market, books)
        if legs is None:
            continue
        mid_yes = float(legs[0]["mid"])
        pair_cost = float(legs[0]["price"]) + float(legs[1]["price"])
        # One share of the pair redeems for $1.00, so a pair that costs more
        # cannot be funded as a matched lot.
        if not 0.0 < pair_cost <= 1.0:
            continue
        band_cents = float(market.reward_max_spread_cents)
        binding_distance = max(leg["distance_cents"] for leg in legs)
        q_min_per_share = _pair_min_score(legs, mid_yes, band_cents, 1.0)
        if q_min_per_share <= 0.0:
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
        fill_yields: list[MarketFillYield] = []
        if fill_horizon_s is not None:
            # With a stated quote horizon the fill side is mandatory for both
            # legs: a pair priced on one leg's fill probability claims half a
            # measurement, so the market is refused instead.
            for leg in legs:
                fill_yield = expected_fill_yield(
                    tick=market.tick,
                    min_flow_shares=min_size,
                    horizon_s=float(fill_horizon_s),
                    quote_price=float(leg["price"]),
                    leg_mid=float(leg["mid"]),
                )
                if fill_yield is None:
                    unpriced += 1
                    fill_yields = []
                    break
                fill_yields.append(fill_yield)
            if not fill_yields:
                continue
            for leg, fill_yield in zip(legs, fill_yields):
                leg["p_fill"] = fill_yield.p_fill
                leg["stay_fraction"] = fill_yield.stay_fraction
                leg["fill_yield_usd_per_usd_per_day"] = (
                    fill_yield.fill_yield_usd_per_usd_per_day
                )
        # The pair's raw alpha is the leg the optimizer sees: the worse leg's
        # quadratic multiplier. ``score_per_usd`` then scales that alpha to the
        # pair Q_min per dollar of pair capital, so
        # ``alpha * score_per_usd * capital`` is exactly ``Q_min(shares)``.
        alpha = ((band_cents - binding_distance) / band_cents) ** 2
        if alpha <= 0.0:
            continue
        score_per_usd = (q_min_per_share / pair_cost) / alpha
        stay_fraction = 1.0
        fill_yield_usd = 0.0
        for leg in legs:
            fill_yield_usd += (
                float(leg.get("fill_yield_usd_per_usd_per_day", 0.0))
                * float(leg["price"])
                / pair_cost
            )
        if fill_yields:
            for fill_yield in fill_yields:
                stay_fraction *= fill_yield.stay_fraction
        cheap_leg = min(legs, key=lambda leg: float(leg["price"]))
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
                "real_mid": mid_yes,
                # The pair's binding (worse) leg distance: the distance the
                # optimizer's alpha is measured at.
                "spread_offset_cents": binding_distance,
                # Pair Q_min per USDC of pair capital. The allocator multiplies
                # this by ``calculate_alpha(spread_offset_cents)``.
                "score_per_usd": score_per_usd,
                "tokens": [
                    {"outcome": outcome, "token_id": token}
                    for outcome, token in zip(market.outcomes, market.token_ids)
                ],
                # One candidate is one matched pair: this is the combined cost
                # of one share on each leg, and the capital one pair-share
                # commits.
                "quote_price": pair_cost,
                "pair_cost": pair_cost,
                "capital_per_share": pair_cost,
                "quote_token": cheap_leg["token_id"],
                "quote_side": "bid",
                "tick": market.tick,
                "lot_price": float(cheap_leg["price"]),
                "legs": [
                    {
                        key: leg[key]
                        for key in (
                            "outcome",
                            "token_id",
                            "price",
                            "mid",
                            "distance_cents",
                            "p_fill",
                            "stay_fraction",
                            "fill_yield_usd_per_usd_per_day",
                        )
                        if key in leg
                    }
                    for leg in legs
                ],
                # Expected fill side of the pair, both legs measured on the L2
                # tape: the accrual only survives while both bids rest.
                "p_fill": None if not fill_yields else 1.0 - stay_fraction,
                "stay_fraction": stay_fraction,
                "fill_yield_usd_per_usd_per_day": fill_yield_usd,
            }
        )
    if unpriced:
        log.info(
            f"reward candidates: refused {unpriced} market(s) whose fill probability "
            f"is unmeasured at a {fill_horizon_s:.0f}s horizon for their tick and "
            f"required flow (a matched pair priced without it claims an accrual it "
            f"may not keep)"
        )
    return candidates
