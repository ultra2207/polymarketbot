"""Endogenous cash reserve for unmerged inventory, from measured inputs."""

from __future__ import annotations

import math
from typing import Any, Optional

from polymarketbot.logs import get_logger

log = get_logger("rewards_allocator")


def calculate_endogenous_reserve(
    allocations: list[dict[str, Any]],
    bankroll_usd: float,
    base_gas_buffer_usd: Optional[float] = None,
    jump_shock_factor: float = 0.20,
    ctf_client: Optional[Any] = None,
) -> float:
    """Endogenous cash reserve for unmerged inventory, from measured inputs.

    The reserve is the sum of (a) the measured Polygon merge-gas cost for the
    current position count, (b) a tail term for one instantaneous sweep of the
    largest exposure, and (c) a diversification term that scales with the
    exposure vector's L2 norm. ``jump_shock_factor`` and the L2 weight are
    unfitted risk parameters, not protocol constants: callers that have a
    measured fill-shock distribution must pass them.
    """
    if bankroll_usd <= 0.0:
        return 0.0

    if base_gas_buffer_usd is not None:
        dynamic_gas_buffer = float(base_gas_buffer_usd)
        per_market_buffer = 0.35
    else:
        try:
            if ctf_client is not None and hasattr(ctf_client, "estimate_merge_cost_usd"):
                gas_cost_merge = ctf_client.estimate_merge_cost_usd()
            else:
                from polymarketbot.strategy.onchain.ctf import estimate_merge_cost_usd
                gas_cost_merge = estimate_merge_cost_usd()
        except Exception:
            # An unmeasured gas cost is not zero and not a guess: reserve
            # nothing for gas and say so, rather than inventing a price.
            log.warning(
                "endogenous reserve: merge gas cost unmeasured; gas buffer held at 0.0"
            )
            gas_cost_merge = 0.0
        # Dynamic gas buffer: ~100 merge operations base headroom + 25 per-market merge operations
        dynamic_gas_buffer = max(0.20, round(gas_cost_merge * 100.0, 2))
        per_market_buffer = max(0.05, round(gas_cost_merge * 25.0, 2))

    if not allocations:
        return round(dynamic_gas_buffer, 2)

    h_values = []
    total_gas_buffer = dynamic_gas_buffer

    for a in allocations:
        # A missing capital or mid is unknown, never a default exposure.
        cap = a.get("capital", a.get("allocated_capital"))
        mid = a.get("mid", a.get("mid_price"))
        if cap is None or mid is None:
            continue
        cap = float(cap)
        mid = float(mid)
        if not 0.0 < mid < 1.0 or cap <= 0.0:
            continue
        h_i = cap * max(mid, 1.0 - mid)
        h_values.append(h_i)
        total_gas_buffer += per_market_buffer

    if not h_values:
        return round(total_gas_buffer, 2)

    # Unified Portfolio Exposure via Central Limit Theorem:
    # 1. Stochastic replenishment demand scales as sqrt(sum(h_i^2)) * sigma_fill
    #    When K=1, sqrt(h_1^2) = h_1 directly.
    # 2. Jump shock: risk of instantaneous unhedged fill on the largest single market
    # 3. Polygon CTF merge gas buffer (scales dynamically with K and live gas pricing)
    diversified_stochastic_demand = math.sqrt(sum(h**2 for h in h_values)) * 0.15
    tail_single_sweep = max(h_values) * jump_shock_factor
    optimal_reserve = diversified_stochastic_demand + tail_single_sweep + total_gas_buffer

    # Physical safety bounds:
    # Floor: minimum gas buffer so smart contract calls never fail.
    # Ceiling: the whole exposure vector could not fill at once and need more
    # cash than ``sum(h_i)`` - the notional of every resting position filled
    # simultaneously - so reserving beyond that is pure cash drag. The old
    # ceiling was ``bankroll * 0.35``, a hand-set cash-drag preference with no
    # measurement behind it; this bound is computed from the same allocation
    # vector the reserve is sized from, so it never binds for a diversified
    # book and still caps a concentrated one at its own exposure.
    max_simultaneous_demand = sum(h_values)
    bounded_reserve = max(
        total_gas_buffer, min(max_simultaneous_demand, optimal_reserve)
    )
    if optimal_reserve > max_simultaneous_demand:
        log.info(
            f"endogenous reserve capped by total exposure: "
            f"optimal={optimal_reserve:.2f} > sum(h_i)={max_simultaneous_demand:.2f} "
            f"(bankroll={bankroll_usd:.2f})"
        )
    return round(bounded_reserve, 2)
