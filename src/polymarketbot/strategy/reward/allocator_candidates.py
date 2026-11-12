"""Candidate building for the reward allocator: filter, parse, prune.

A candidate dict is turned into the solver's market record here - the measured
pool, the measured competing denominator, the score per dollar, the activation
lot and the fill side - so the MILP and the KKT fallback solve the same set.
The dominance prune that follows is part of the same screen: a market whose
marginal yield is below the optimum's shadow price cannot be active, so it is
dropped with the bound logged.
"""

from __future__ import annotations

import math
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.marginal import (
    _fill_side_of,
    calculate_alpha,
    calculate_min_floor_capital,
    fill_side,
    marginal_total_yield,
)

log = get_logger("rewards_allocator")


def activation_lot_capital(cand: dict[str, Any]) -> float | None:
    """USD capital for the venue's binding minimum order, one or two legs.

    ``capital_per_share`` is the capital one share of the plan commits: the
    matched pair's cost on the two-sided live path, the single leg's quote
    price on the one-sided paper path. A caller that does not state it gets
    $1.00, the definitional upper bound for a binary pair, so legacy callers
    keep the conservative pair semantics.

    The venue publishes ``orderMinSize`` as a USDC notional per order; the
    rewards campaign publishes ``min_size`` as a share count. The binding
    count is the larger of the two at the cheap leg's rest price:

    ``q = max(min_size, ceil(order_min_usd / min(p, 1 - p)))``

    A matched binary pair redeems for exactly $1.00 and its two bids sum to at
    most $1.00, so the pair's capital is ``q`` dollars. Funding only
    ``min_size * price`` under-budgets the second leg. A candidate that cannot
    state its minimum or price returns ``None`` (missing is not zero).
    """
    try:
        min_sz = float(cand.get("min_size", 0.0))
        mid = float(cand.get("real_mid", cand.get("mid_yes", 0.0)))
        lot_price = float(cand.get("lot_price", mid))
        order_min = float(cand.get("order_min_usd") or 0.0)
        capital_per_share = float(cand.get("capital_per_share") or 1.0)
    except (TypeError, ValueError):
        return None
    if (
        min_sz <= 0.0
        or not 0.0 < mid < 1.0
        or not 0.0 < lot_price < 1.0
        or not 0.0 < capital_per_share <= 1.0
    ):
        return None
    shares = min_sz
    if order_min > 0.0:
        cheap = min(lot_price, 1.0 - lot_price)
        if cheap <= 0.0:
            return None
        shares = max(shares, float(math.ceil(order_min / cheap)))
    return round(shares * capital_per_share, 2)


def parse_candidates(
    candidates: list[dict[str, Any]], usable_capital: float, floor_target: float
) -> tuple[list[dict[str, Any]], int]:
    """The solver's market records that can be funded, and the unmeasured count.

    ``unmeasured`` counts candidates refused for a missing or non-positive pool
    or competing denominator, so the caller can state why the set is empty.
    """
    parsed_candidates: list[dict[str, Any]] = []
    unmeasured = 0
    for cand in candidates:
        # The pool and the competing score are measurements: a missing one
        # is missing data, not zero. Reading a missing competing score as
        # 0.0 told the optimizer there was no competition and paid the whole
        # pool on any size, so an unmeasured market is refused here.
        raw_pool = cand.get("daily_pool")
        raw_competing = cand.get("competing_score")
        if raw_pool is None or raw_competing is None:
            unmeasured += 1
            continue
        p = float(raw_pool)
        d = float(raw_competing)
        if not math.isfinite(p) or not math.isfinite(d) or p <= 0.0 or d <= 0.0:
            unmeasured += 1
            continue
        max_spr = float(cand.get("max_spread", 0.0))
        min_sz = float(cand.get("min_size", 0.0))
        mid = float(cand.get("real_mid", cand.get("mid_yes", 0.0)))

        # Alpha is a property of *our* resting distance from the mid, so it
        # must come from the candidate's measured quote, never from a
        # default offset. A candidate that cannot state its distance is not
        # scoreable and is dropped (missing is not zero).
        offset = cand.get("spread_offset_cents")
        if offset is None:
            quote_price = cand.get("quote_price")
            if quote_price is None or not 0.0 < mid < 1.0:
                continue
            offset = abs(float(quote_price) - mid) * 100.0
        alpha = calculate_alpha(max_spr, float(offset))
        if alpha <= 0.0 or not 0.0 < mid < 1.0 or min_sz <= 0.0:
            continue
        # The venue scores *shares* weighted by alpha; the optimizer's unit
        # is USD. ``score_per_usd`` converts one into the other, including
        # any payout fraction the caller has already measured (the reward
        # pillar's one-sided penalty). A caller that states neither quotes
        # at the mid on both sides, for which one USD is one share and the
        # raw alpha is the score.
        score_per_usd = float(cand.get("score_per_usd", 1.0))
        if score_per_usd <= 0.0:
            continue
        eff_alpha = alpha * score_per_usd
        # A pool that cannot clear the floor even with the whole bankroll is
        # not fundable; a market with no competing score is the opposite
        # case (we would take the pool) and stays in.
        if p <= floor_target:
            continue

        c_floor = calculate_min_floor_capital(p, d, eff_alpha, floor_target=floor_target)

        # The binding minimum is the larger of the rewards share count and
        # the venue's per-order notional, and BOTH legs carry it. The
        # activation lot is the matched pair's capital, not one leg's.
        lot_price = float(cand.get("lot_price", mid))
        if not 0.0 < lot_price < 1.0:
            continue
        # One share of the plan commits either the matched pair's cost
        # (two-sided live) or the single leg's quote price (one-sided paper).
        # Sources publish it explicitly; the fallbacks keep legacy callers on
        # the definitional $1.00 pair bound rather than the leg mid.
        try:
            capital_per_share = float(
                cand.get("capital_per_share")
                or cand.get("pair_cost")
                or cand.get("quote_price")
                or 1.0
            )
        except (TypeError, ValueError):
            continue
        if not 0.0 < capital_per_share <= 1.0:
            continue
        lot_capital = activation_lot_capital(
            {
                **cand,
                "min_size": min_sz,
                "real_mid": mid,
                "lot_price": lot_price,
                "capital_per_share": capital_per_share,
            }
        )
        if lot_capital is None:
            continue
        c_min = max(lot_capital, c_floor)
        if not math.isfinite(c_min) or c_min > usable_capital:
            continue

        parsed_candidates.append({
            "cand": cand,
            "P": p,
            "D": d,
            "alpha": eff_alpha,
            "c_min": c_min,
            "c_floor": c_floor,
            "min_size": min_sz,
            "mid": mid,
            "capital_per_share": capital_per_share,
            "fill_side": fill_side(cand),
        })
    unpriced = sum(1 for m in parsed_candidates if m["fill_side"] is None)
    if unpriced:
        # The accrual half alone is what this module priced before the fill
        # side existed: an empty book's tiny ``D`` then wins the pool share
        # with no regard for the fact that nothing trades there. Saying so
        # is the difference between a study and an allocation.
        log.info(
            f"allocator: {unpriced}/{len(parsed_candidates)} candidate(s) carry no "
            "measured fill side; accrual-only objective (measured share, unpriced fills)"
        )

    thin = [
        m for m in parsed_candidates
        if bool(m["cand"].get("competing_low_confidence"))
    ]
    if thin:
        # The venue divides by the epoch average of up to 1,440 samples; a
        # ranking built on one snapshot is a point estimate and is labelled
        # as such rather than reported as the epoch rate.
        log.info(
            f"allocator: {len(thin)}/{len(parsed_candidates)} candidate(s) rank on a "
            "LOW-CONFIDENCE competing score (one snapshot, not an epoch average); "
            "the estimate firms up as the venue-cadence window fills"
        )

    if unmeasured:
        log.info(
            f"allocator: refused {unmeasured}/{len(candidates)} candidates whose "
            "daily pool or competing score was unmeasured (a missing denominator "
            "is never read as zero)"
        )
    dropped = len(candidates) - len(parsed_candidates) - unmeasured
    if dropped:
        log.info(
            f"allocator: dropped {dropped}/{len(candidates)} candidates that could "
            "not be scored or funded at this capital (quote distance, alpha, "
            "activation lot, or the $1.00/day floor)"
        )
    return parsed_candidates, unmeasured


def prune_dominated(
    parsed_candidates: list[dict[str, Any]], usable_capital: float
) -> tuple[list[dict[str, Any]], float | None]:
    """Drop candidates that cannot be active, with the shadow-price bound logged.

    Returns the surviving candidates ranked by marginal yield, and the shadow
    yield floor used to prune them (``None`` when nothing was dropped).
    """
    # Sound dominance prune, with a logged bound. For a separable concave
    # payout the optimum's shadow price is at least the marginal yield of
    # the k_max-th ranked market, where k_max bounds the number of markets
    # whose activation lot can fit in the budget. Anything ranked below that
    # has a marginal yield under the shadow price and cannot be active, so
    # dropping it loses exactly nothing while shrinking the MILP.
    for m in parsed_candidates:
        stay, fill = _fill_side_of(m)
        m["marginal_at_floor"] = marginal_total_yield(
            m["c_min"], m["P"], m["D"], m["alpha"], stay, fill
        )
    shadow_floor: float | None = None
    k_max = 0
    spent = 0.0
    for m in sorted(parsed_candidates, key=lambda item: item["c_min"]):
        if spent + m["c_min"] > usable_capital:
            break
        spent += m["c_min"]
        k_max += 1
    ranked = sorted(parsed_candidates, key=lambda item: item["marginal_at_floor"], reverse=True)
    if 0 < k_max < len(ranked):
        shadow_floor = ranked[k_max - 1]["marginal_at_floor"]
        kept = [m for m in ranked if m["marginal_at_floor"] >= shadow_floor]
        log.info(
            f"allocator prune: {len(ranked)} -> {len(kept)} candidates "
            f"(k_max={k_max} lots of >= ${min(m['c_min'] for m in kept):.2f}, "
            f"shadow yield floor {shadow_floor:.8f}/USD/day, dropped yield < floor)"
        )
        return kept, shadow_floor
    return ranked, shadow_floor
