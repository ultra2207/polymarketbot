"""The OR-Tools MILP for the reward allocator, and its piecewise linearisation.

The payout is concave in capital, so each market's curve is approximated by
``num_segments`` secant segments above its activation lot; the binary activates
the market and the budget is the single coupling constraint. ``solve_milp``
returns the funded ``(market, capital)`` pairs, or ``None`` when no solver is
available or the solve does not converge - the caller then runs the exact KKT
water-fill instead.
"""

from __future__ import annotations

from typing import Any, Optional

from ortools.linear_solver import pywraplp

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.marginal import _fill_side_of

log = get_logger("rewards_allocator")


def solve_milp(
    parsed_candidates: list[dict[str, Any]],
    usable_capital: float,
    num_segments: int,
    max_active_markets: Optional[int],
) -> Optional[list[tuple[dict[str, Any], float]]]:
    """Solve the allocation MILP; ``None`` means "fall back to water-filling"."""
    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        log.warning("SCIP solver unavailable, falling back to CBC.")
        solver = pywraplp.Solver.CreateSolver("CBC")
    if not solver:
        return None

    n = len(parsed_candidates)
    x_vars = {}  # Binary: market active
    c_vars = {}  # Continuous: total capital
    seg_vars = {}  # Continuous: capital in piecewise segment k

    for i, m in enumerate(parsed_candidates):
        x_vars[i] = solver.BoolVar(f"x_{i}")
        c_vars[i] = solver.NumVar(0.0, usable_capital, f"c_{i}")

        # Link capital to binary activation
        solver.Add(c_vars[i] >= m["c_min"] * x_vars[i])
        solver.Add(c_vars[i] <= usable_capital * x_vars[i])

        # Piecewise linear discretization of concave payout function R_i(c)
        # R(c) = P * (alpha * c) / (D + alpha * c)
        c_upper = usable_capital
        c_step = (c_upper - m["c_min"]) / max(1, num_segments)

        seg_vars[i] = []
        for k in range(num_segments):
            seg_cap = solver.NumVar(0.0, c_step, f"seg_{i}_{k}")
            seg_vars[i].append(seg_cap)

        # Sum of segment capitals plus base must equal c_vars[i] when active
        solver.Add(c_vars[i] == m["c_min"] * x_vars[i] + sum(seg_vars[i]))

    # Budget constraint
    solver.Add(sum(c_vars[i] for i in range(n)) <= usable_capital)

    # Cardinality constraint
    if max_active_markets is not None and max_active_markets > 0:
        solver.Add(sum(x_vars[i] for i in range(n)) <= max_active_markets)

    # Objective Function: Maximize sum of piecewise linear payouts
    objective = solver.Objective()
    objective.SetMaximization()

    for i, m in enumerate(parsed_candidates):
        P = m["P"]
        D = m["D"]
        alpha = m["alpha"]
        # Expected total yield: the pool share is earned only while the bid
        # rests in-band and unfilled, so it is discounted by the measured
        # ``stay_fraction``; the filled part earns the measured fill yield,
        # which is linear in capital and enters the base and every slope.
        stay, fill = _fill_side_of(m)

        # Base payout at c_min
        base_payout = P * (alpha * m["c_min"]) / (D + alpha * m["c_min"])
        objective.SetCoefficient(x_vars[i], base_payout * stay + fill * m["c_min"])

        c_step = (usable_capital - m["c_min"]) / max(1, num_segments)
        for k in range(num_segments):
            c_start = m["c_min"] + k * c_step
            c_end = c_start + c_step
            r_start = P * (alpha * c_start) / (D + alpha * c_start)
            r_end = P * (alpha * c_end) / (D + alpha * c_end)
            slope = (r_end - r_start) / max(1e-6, c_step)
            objective.SetCoefficient(seg_vars[i][k], slope * stay + fill)

    status = solver.Solve()

    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        log.warning("MILP solver did not converge to optimal solution, using water-filling fallback.")
        return None

    return [
        (m, c_vars[i].solution_value())
        for i, m in enumerate(parsed_candidates)
        if x_vars[i].solution_value() > 0.5
    ]
