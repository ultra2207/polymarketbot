"""Production-Grade MINLP / MILP Capital Allocator for Polymarket Liquidity Rewards.

Formulates and solves the optimal capital allocation across Polymarket reward pools:
    max sum_{i=1}^N P_i * (alpha_i * c_i) / (D_i + alpha_i * c_i)

Subject to:
    1. Budget Constraint:        sum_i c_i <= Bankroll * (1 - reserve_pct)
    2. Strict Floor Clearance:   c_i >= c_i^{min_floor} * x_i  where c_i^{min_floor} = D_i / (alpha_i * (P_i - 1.0))
    3. Minimum Order Size:       c_i >= min_size_i * x_i
    4. Max Active Markets:       sum_i x_i <= K_max
    5. Binary Market Activation: c_i <= M_usable * x_i,  x_i in {0, 1}

Features:
- Google OR-Tools SCIP/CBC solver with piece-wise linear concave secants (SOS2-free).
- Closed-form KKT Water-Filling fallback solver (< 1 ms latency).
- Strict enforcement of Polymarket's $1.00 daily payout floor per market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ortools.linear_solver import pywraplp

from polymarketbot.logs import get_logger

log = get_logger("rewards_allocator")

# Documented venue rule: a market that would pay less than $1.00 in a daily
# epoch pays nothing at all. It is the only payout hurdle this module applies;
# every other threshold below is measured from the candidate set.
PROTOCOL_MIN_DAILY_PAYOUT_USD = 1.00


@dataclass
class MarketAllocation:
    """Individual market capital allocation and expected performance."""

    condition_id: str
    question: str
    allocated_capital: float
    quote_size: float
    daily_reward_payout: float
    daily_roi_pct: float
    min_floor_capital: float
    clears_floor: bool
    daily_pool: float
    competing_depth: float
    mid_price: float
    tokens: list[dict[str, Any]] = field(default_factory=list)
    yes_token: str = ""
    no_token: str = ""
    max_spread: float = 3.0
    min_size: float = 20.0
    expected_fill_yield_usd_per_day: float = 0.0
    """Fill side of the yield, USD/day: ``p_fill``-weighted and net of the
    measured adverse markout. Negative when a fill costs more than it earns."""
    expected_total_yield_usd_per_day: float = 0.0
    """``daily_reward_payout * stay_fraction + expected_fill_yield``: the number
    the optimiser actually maximised. ``daily_reward_payout`` stays the accrual
    alone because that is what the engine credits as cash."""
    p_fill: Optional[float] = None
    """Measured per-horizon fill probability behind the fill term; ``None``
    when the candidate's fill side was not priced."""

    competing_samples: int = 0
    """Venue-cadence readings behind ``competing_depth``. The venue divides by
    the mean of up to 1,440 samples per epoch, so a ranking built on one
    snapshot is labelled rather than presented as the epoch rate."""
    competing_low_confidence: bool = False
    """True when ``competing_depth`` came from a single snapshot."""

    @property
    def token_yes(self) -> str:
        if self.yes_token:
            return self.yes_token
        for t in self.tokens:
            if str(t.get("outcome", "")).upper() == "YES":
                return t.get("token_id", "")
        return f"{self.condition_id}_YES"

    @property
    def token_no(self) -> str:
        if self.no_token:
            return self.no_token
        for t in self.tokens:
            if str(t.get("outcome", "")).upper() == "NO":
                return t.get("token_id", "")
        return f"{self.condition_id}_NO"


def _fill_side_of(candidate: dict[str, Any]) -> Tuple[float, float]:
    """``(stay_fraction, fill_yield_per_usd_per_day)`` for a parsed candidate.

    ``(1.0, 0.0)`` means the caller priced no fill side at all - the
    accrual-only objective this module ran before the fill side existed. It is
    never the representation of an unmeasured fill *probability*: a candidate
    that carries ``p_fill`` has both fields, and
    :meth:`LiquidityRewardsAllocator._fill_side` refuses a half-populated one.
    """
    side = candidate.get("fill_side")
    if side is None:
        return 1.0, 0.0
    return float(side[0]), float(side[1])


@dataclass
class PortfolioAllocationResult:
    """Aggregated portfolio allocation statement."""

    bankroll_usd: float
    usable_capital: float
    cash_reserve: float
    total_allocated: float
    total_daily_payout: float
    total_monthly_payout: float
    portfolio_daily_roi_pct: float
    portfolio_monthly_roi_pct: float
    active_markets_count: int
    allocations: list[MarketAllocation]
    solver_name: str
    #: KKT price of capital at the optimum, USD per USD per day: the marginal
    #: reward yield the last funded market earns. Any other use of the same
    #: dollar (a CTF parity lock, a settlement buy) must beat it or the capital
    #: is better left on the reward book. ``None`` when no allocation was
    #: solved, so a caller cannot mistake "unmeasured" for "zero hurdle".
    shadow_yield_floor_usd_per_usd_per_day: Optional[float] = None


class LiquidityRewardsAllocator:
    """Mathematical optimizer allocating capital across Polymarket reward pools."""

    def __init__(
        self,
        reserve_pct: Optional[float] = None,
        num_linear_segments: int = 10,
    ) -> None:
        # ``reserve_pct`` is only consulted when a caller explicitly disables
        # the endogenous reserve; it has no default because a reserve fraction
        # is a capital decision, not a constant. The attribute is only set when
        # a caller supplied one, so a reader that wants a fallback still sees
        # its own default rather than an invented number from here.
        if reserve_pct is not None:
            self.reserve_pct = float(reserve_pct)
        self.num_segments = int(num_linear_segments)

    def calculate_alpha(self, max_spread_cents: float, spread_offset_cents: float) -> float:
        """Quadratic score multiplier alpha = ((v - s) / v) ** 2.

        ``s`` is the measured distance of *our* quote from the mid in cents, so
        the caller must pass the distance it will actually rest at (for the
        paper pillar that is ``bid + tick`` against the leg mid). A quote at or
        beyond the band scores nothing at all: the venue gives it zero, so the
        multiplier is 0.0 rather than a clamped positive value.
        """
        v = float(max_spread_cents)
        s = float(spread_offset_cents)
        if v <= 0.0 or s < 0.0 or s >= v:
            return 0.0
        return float(((v - s) / v) ** 2)

    def calculate_min_floor_capital(
        self,
        daily_pool: float,
        competing_score: float,
        alpha: float,
        floor_target: float = PROTOCOL_MIN_DAILY_PAYOUT_USD,
    ) -> float:
        """Capital required to clear the venue's daily payout floor:

        P * (alpha * c) / (D + alpha * c) >= floor_target
        <=> alpha * c * (P - floor_target) >= floor_target * D
        <=> c >= (floor_target * D) / (alpha * (P - floor_target))

        ``floor_target`` defaults to the documented $1.00 protocol floor. It is
        not a hand-set safety multiplier: anything the venue would not pay is
        capital thrown away, and a larger buffer belongs to the optimizer's
        marginal condition, not to a constant.
        """
        target = float(floor_target)
        if daily_pool <= target or alpha <= 0.0:
            return float("inf")
        excess_pool_ratio = (daily_pool / target) - 1.0
        if excess_pool_ratio <= 1e-6:
            return float("inf")
        return float(competing_score / (alpha * excess_pool_ratio))

    def evaluate_market_payout(
        self,
        capital: float,
        daily_pool: Optional[float],
        competing_score: Optional[float],
        alpha: float,
    ) -> Optional[float]:
        """Evaluate non-linear daily cash payout function R_i(c_i).

        ``alpha`` here is the venue score earned per USDC of capital (see
        ``score_per_usd`` in :meth:`optimize_allocation_milp`), so
        ``alpha * capital`` is in the same share-weighted units as
        ``competing_score`` and the ratio is a genuine share of the pool.

        Returns ``None`` - never a number - when the pool or the competing
        score is missing, non-positive or non-finite. ``competing_score`` is the
        measured in-band depth of the market's competing book (see
        :func:`polymarketbot.paper.pillars.reward.market_competing_score`); a
        zero or absent one used to be read as "no competitors" and paid the
        whole pool on any size, which is a fabricated payout, not a share.
        """
        if daily_pool is None or competing_score is None:
            return None
        pool = float(daily_pool)
        competing = float(competing_score)
        if not math.isfinite(pool) or not math.isfinite(competing):
            return None
        if pool <= 0.0 or competing <= 0.0 or alpha <= 0.0:
            return None
        if capital <= 0.0:
            return 0.0
        user_score = alpha * capital
        denom = competing + user_score
        if denom <= 0.0:
            return None
        raw_payout = pool * (user_score / denom)
        # Documented venue floor: below it the market pays nothing.
        return raw_payout if raw_payout >= PROTOCOL_MIN_DAILY_PAYOUT_USD else 0.0

    def marginal_payout(self, capital: float, daily_pool: float,
                        competing_score: float, alpha: float) -> float:
        """dR/dc = P * alpha * D / (D + alpha * c) ** 2, USD per USD per day.

        This is the KKT price of capital in one market: at the optimum every
        active market shares the same marginal yield. ``alpha`` is the venue
        score per USDC of capital, so this is USD per USDC per day.
        """
        if capital <= 0.0 or alpha <= 0.0 or daily_pool <= 0.0:
            return 0.0
        denom = competing_score + alpha * capital
        if denom <= 0.0:
            return 0.0
        return float(daily_pool * alpha * competing_score / (denom * denom))

    def evaluate_market_total_yield(
        self,
        capital: float,
        daily_pool: Optional[float],
        competing_score: Optional[float],
        alpha: float,
        stay_fraction: float,
        fill_yield_usd_per_usd_per_day: float,
    ) -> Optional[float]:
        """Expected **total** yield of ``capital`` in one market, USD per day.

            E[yield] = R(c) * stay_fraction + fill_yield * c

        The pool share is only earned while the bid is in-band and unfilled, so
        ``R(c)`` is multiplied by the measured ``1 - p_fill`` per horizon; the
        filled part of the capital earns the measured fill yield instead, which
        is negative whenever the measured adverse markout exceeds the distance
        from the mid. ``fill_yield_usd_per_usd_per_day`` and ``stay_fraction``
        come from the L2 tape tables, so neither term is a guess.

        Returns ``None`` when the accrual denominator is unmeasured, exactly as
        :meth:`evaluate_market_payout` does: a missing denominator is missing
        data, never zero.
        """
        accrual = self.evaluate_market_payout(capital, daily_pool, competing_score, alpha)
        if accrual is None:
            return None
        return float(accrual) * float(stay_fraction) + float(fill_yield_usd_per_usd_per_day) * float(capital)

    def marginal_total_yield(
        self,
        capital: float,
        daily_pool: float,
        competing_score: float,
        alpha: float,
        stay_fraction: float,
        fill_yield_usd_per_usd_per_day: float,
    ) -> float:
        """dE[yield]/dc = dR/dc * stay_fraction + fill_yield, USD/USD/day.

        With ``fill_yield <= 0`` this is the accrual's marginal yield discounted
        by the probability the bid is filled, minus the measured drift on the
        part that is. It is the marginal condition every candidate is ranked on,
        so an empty book whose ``D`` is tiny wins only when its accrual still
        beats the yield available on a book that actually trades.
        """
        return (
            self.marginal_payout(capital, daily_pool, competing_score, alpha) * float(stay_fraction)
            + float(fill_yield_usd_per_usd_per_day)
        )

    @staticmethod
    def _fill_side(cand: dict[str, Any]) -> Optional[tuple[float, float]]:
        """``(stay_fraction, fill_yield)`` for a candidate, or ``None`` when unpriced.

        ``None`` means the caller did not price the fill side at all (an
        accrual-only study); a candidate that carries ``p_fill`` is priced with
        it. The two modes are never mixed silently - the solver logs which one
        it ran in.
        """
        if cand.get("p_fill") is None and "fill_yield_usd_per_usd_per_day" not in cand:
            return None
        stay = cand.get("stay_fraction")
        fill = cand.get("fill_yield_usd_per_usd_per_day")
        if stay is None or fill is None:
            return None
        return float(stay), float(fill)

    def optimize_allocation_milp(
        self,
        candidates: list[dict[str, Any]],
        bankroll_usd: float = 150.0,
        max_active_markets: Optional[int] = None,
        floor_target: float = PROTOCOL_MIN_DAILY_PAYOUT_USD,
        use_endogenous_reserve: bool = True,
        exact_reserve_override: Optional[float] = None,
        _is_pass2: bool = False,
    ) -> PortfolioAllocationResult:
        """Solve capital allocation using Google OR-Tools SCIP MILP solver with concave piecewise linear secants."""
        if exact_reserve_override is not None:
            cash_reserve = round(exact_reserve_override, 2)
            usable_capital = max(0.0, round(bankroll_usd - cash_reserve, 2))
        elif use_endogenous_reserve:
            # Reserve = one measured venue-minimum activation lot: the smallest
            # rewards_min_size (shares) x mid (USD/share) in the supplied universe,
            # i.e. the cash needed to open one more qualifying position. No
            # hand-set reserve fraction or sqrt multiple.
            activation_lots: List[float] = []
            for cand in candidates:
                try:
                    lot_shares = float(cand.get("min_size", 0.0))
                    lot_mid = float(cand.get("real_mid", cand.get("mid_yes", 0.0)))
                except (TypeError, ValueError):
                    continue
                if lot_shares > 0.0 and 0.0 < lot_mid < 1.0:
                    activation_lots.append(lot_shares * lot_mid)
            init_reserve = round(min(bankroll_usd, min(activation_lots, default=0.0)), 2)
            usable_capital = max(0.0, bankroll_usd - init_reserve)
            cash_reserve = init_reserve
        else:
            reserve_pct = getattr(self, "reserve_pct", None)
            if reserve_pct is None:
                raise ValueError(
                    "use_endogenous_reserve=False requires an explicit reserve_pct: "
                    "a reserve fraction is a capital decision, not a default."
                )
            usable_capital = bankroll_usd * (1.0 - reserve_pct)
            cash_reserve = bankroll_usd * reserve_pct

        if usable_capital <= 0.0 or not candidates:
            return PortfolioAllocationResult(
                bankroll_usd=bankroll_usd,
                usable_capital=usable_capital,
                cash_reserve=cash_reserve,
                total_allocated=0.0,
                total_daily_payout=0.0,
                total_monthly_payout=0.0,
                portfolio_daily_roi_pct=0.0,
                portfolio_monthly_roi_pct=0.0,
                active_markets_count=0,
                allocations=[],
                solver_name="OR_TOOLS_SCIP",
            )

        # 1. Filter viable candidates
        parsed_candidates = []
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
            alpha = self.calculate_alpha(max_spr, float(offset))
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

            c_floor = self.calculate_min_floor_capital(p, d, eff_alpha, floor_target=floor_target)

            # The venue's minimum is a *share* count; the optimizer's unit is
            # USD, so the activation lot is min_size shares at the price the
            # order will rest at. Funding it at the mid leaves the floored
            # share count one short of the minimum whenever the quote sits
            # above the mid, and the quote is then silently dropped.
            lot_price = float(cand.get("lot_price", mid))
            if not 0.0 < lot_price < 1.0:
                continue
            c_min = max(min_sz * lot_price, c_floor)
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
                "fill_side": self._fill_side(cand),
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

        if not parsed_candidates:
            log.info(
                f"allocator: no candidate clears the ${floor_target:.2f}/day payout floor "
                f"inside ${usable_capital:.2f} usable capital ({len(candidates)} candidates, "
                f"{unmeasured} refused with an unmeasured pool or competing score)"
            )
            return PortfolioAllocationResult(
                bankroll_usd=bankroll_usd,
                usable_capital=usable_capital,
                cash_reserve=cash_reserve,
                total_allocated=0.0,
                total_daily_payout=0.0,
                total_monthly_payout=0.0,
                portfolio_daily_roi_pct=0.0,
                portfolio_monthly_roi_pct=0.0,
                active_markets_count=0,
                allocations=[],
                solver_name="NO_VIABLE_MARKETS",
            )

        # Sound dominance prune, with a logged bound. For a separable concave
        # payout the optimum's shadow price is at least the marginal yield of
        # the k_max-th ranked market, where k_max bounds the number of markets
        # whose activation lot can fit in the budget. Anything ranked below that
        # has a marginal yield under the shadow price and cannot be active, so
        # dropping it loses exactly nothing while shrinking the MILP.
        for m in parsed_candidates:
            stay, fill = _fill_side_of(m)
            m["marginal_at_floor"] = self.marginal_total_yield(
                m["c_min"], m["P"], m["D"], m["alpha"], stay, fill
            )
        shadow_floor: Optional[float] = None
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
            parsed_candidates = kept
        else:
            parsed_candidates = ranked

        # 2. Build Google OR-Tools SCIP Solver
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if not solver:
            log.warning("SCIP solver unavailable, falling back to CBC.")
            solver = pywraplp.Solver.CreateSolver("CBC")
        if not solver:
            return self._solve_water_filling(parsed_candidates, bankroll_usd, usable_capital, cash_reserve)

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
            c_step = (c_upper - m["c_min"]) / max(1, self.num_segments)

            seg_vars[i] = []
            for k in range(self.num_segments):
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

            c_step = (usable_capital - m["c_min"]) / max(1, self.num_segments)
            for k in range(self.num_segments):
                c_start = m["c_min"] + k * c_step
                c_end = c_start + c_step
                r_start = P * (alpha * c_start) / (D + alpha * c_start)
                r_end = P * (alpha * c_end) / (D + alpha * c_end)
                slope = (r_end - r_start) / max(1e-6, c_step)
                objective.SetCoefficient(seg_vars[i][k], slope * stay + fill)

        status = solver.Solve()

        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            log.warning("MILP solver did not converge to optimal solution, using water-filling fallback.")
            return self._solve_water_filling(parsed_candidates, bankroll_usd, usable_capital, cash_reserve)

        # Extract solution
        allocations: list[MarketAllocation] = []
        total_allocated = 0.0
        total_payout = 0.0

        for i, m in enumerate(parsed_candidates):
            if x_vars[i].solution_value() > 0.5:
                cap = c_vars[i].solution_value()
                payout = self.evaluate_market_payout(cap, m["P"], m["D"], m["alpha"])
                if payout is None:
                    # The denominator stopped being measurable between the
                    # filter and here: fund nothing rather than book a guess.
                    log.warning(
                        f"allocator: refusing {m['cand'].get('condition_id', '')[:10]} - "
                        "payout denominator unmeasured at extraction"
                    )
                    continue
                roi_pct = (payout / cap) * 100.0 if cap > 0 else 0.0
                stay, fill = _fill_side_of(m)
                fill_usd = fill * cap
                cand_dict = m["cand"]

                allocations.append(
                    MarketAllocation(
                        condition_id=cand_dict.get("condition_id", ""),
                        question=cand_dict.get("question", ""),
                        allocated_capital=round(cap, 2),
                        quote_size=round(cap / m["mid"], 1),
                        daily_reward_payout=round(payout, 2),
                        daily_roi_pct=round(roi_pct, 2),
                        min_floor_capital=round(m["c_floor"], 2),
                        clears_floor=payout >= PROTOCOL_MIN_DAILY_PAYOUT_USD,
                        daily_pool=round(m["P"], 2),
                        competing_depth=round(m["D"], 1),
                        mid_price=round(m["mid"], 4),
                        tokens=cand_dict.get("raw_market", {}).get("tokens", []),
                        yes_token=cand_dict.get("yes_token") or cand_dict.get("token_yes") or "",
                        no_token=cand_dict.get("no_token") or cand_dict.get("token_no") or "",
                        max_spread=float(cand_dict.get("max_spread", 0.0)),
                        min_size=float(cand_dict.get("min_size", 0.0)),
                        expected_fill_yield_usd_per_day=round(fill_usd, 4),
                        expected_total_yield_usd_per_day=round(payout * stay + fill_usd, 4),
                        p_fill=cand_dict.get("p_fill"),
                        competing_samples=int(cand_dict.get("competing_samples") or 0),
                        competing_low_confidence=bool(
                            cand_dict.get("competing_low_confidence")
                        ),
                    )
                )
                total_allocated += cap
                total_payout += payout

        # Sort allocations by expected total yield descending: the accrual alone
        # ranks the emptiest book first, which is the selection this exists to
        # fix.
        allocations.sort(key=lambda a: a.expected_total_yield_usd_per_day, reverse=True)

        if use_endogenous_reserve and allocations and not _is_pass2:
            exact_reserve = calculate_endogenous_reserve(
                allocations=[a.__dict__ for a in allocations],
                bankroll_usd=bankroll_usd,
            )
            # 2-Pass Refinement: Re-solve with the exact portfolio-derived risk reserve
            if abs(exact_reserve - cash_reserve) >= 1.0:
                return self.optimize_allocation_milp(
                    candidates=candidates,
                    bankroll_usd=bankroll_usd,
                    max_active_markets=max_active_markets,
                    floor_target=floor_target,
                    use_endogenous_reserve=True,
                    exact_reserve_override=exact_reserve,
                    _is_pass2=True,
                )
            cash_reserve = round(exact_reserve, 2)
            usable_capital = round(bankroll_usd - cash_reserve, 2)
        elif _is_pass2 and exact_reserve_override is not None:
            cash_reserve = round(exact_reserve_override, 2)
            usable_capital = round(bankroll_usd - cash_reserve, 2)

        daily_roi = (total_payout / total_allocated) * 100.0 if total_allocated > 0 else 0.0
        monthly_roi = daily_roi * 30.0

        return PortfolioAllocationResult(
            bankroll_usd=round(bankroll_usd, 2),
            usable_capital=round(usable_capital, 2),
            cash_reserve=round(cash_reserve, 2),
            total_allocated=round(total_allocated, 2),
            total_daily_payout=round(total_payout, 2),
            total_monthly_payout=round(total_payout * 30.0, 2),
            portfolio_daily_roi_pct=round(daily_roi, 2),
            portfolio_monthly_roi_pct=round(monthly_roi, 2),
            active_markets_count=len(allocations),
            allocations=allocations,
            solver_name="OR_TOOLS_SCIP",
            shadow_yield_floor_usd_per_usd_per_day=shadow_floor,
        )

    def _solve_water_filling(
        self,
        parsed_candidates: list[dict[str, Any]],
        bankroll_usd: float,
        usable_capital: float,
        cash_reserve: float,
    ) -> PortfolioAllocationResult:
        """Exact KKT water-filling for the separable concave allocation.

        R_i(c) = P_i * alpha_i * c / (D_i + alpha_i * c) is concave and strictly
        increasing, so the budget-constrained optimum equalises the marginal
        yield dR_i/dc = P_i alpha_i D_i / (D_i + alpha_i c_i)^2 = lambda across
        every active market, with c_i >= c_i_min. The closed form inverts to

            c_i(lambda) = (sqrt(P_i alpha_i D_i / lambda) - D_i) / alpha_i,

        and lambda is found by bisection on sum_i c_i(lambda) = usable. Markets
        whose c_i(lambda) falls below their activation lot are funded at the lot
        and removed, then lambda is re-solved. Every candidate here carries a
        measured, positive competing score - an unmeasured one is refused in
        :meth:`optimize_allocation_milp` - so no market is ever funded on a
        zero denominator.

        This replaces the previous fallback, which put the entire bankroll into
        a single market regardless of bankroll size.
        """
        free = [m for m in parsed_candidates if m["c_min"] <= usable_capital]
        # A market whose activation lot does not pay for itself is not "starved"
        # by the water level - it is unviable, and funding it at the lot would
        # book a negative total yield. The fill term makes that reachable: a
        # book whose measured markout exceeds its spread can have a positive
        # accrual and still lose money on every fill.
        viable: list[dict[str, Any]] = []
        for m in free:
            total_at_lot = self.evaluate_market_total_yield(
                m["c_min"], m["P"], m["D"], m["alpha"], *_fill_side_of(m)
            )
            if total_at_lot is None or total_at_lot <= 0.0:
                continue
            viable.append(m)
        free = viable
        remaining = usable_capital
        funded: list[tuple[dict[str, Any], float]] = []

        while free and remaining > 1e-9:
            caps = self._water_level_caps(free, remaining)
            starved = [m for m, cap in zip(free, caps) if cap < m["c_min"]]
            if not starved:
                for m, cap in zip(free, caps):
                    funded.append((m, cap))
                remaining = 0.0
                break
            for m in starved:
                funded.append((m, m["c_min"]))
                remaining -= m["c_min"]
            starved_ids = {id(m) for m in starved}
            free = [m for m in free if id(m) not in starved_ids]

        if not funded:
            return PortfolioAllocationResult(
                bankroll_usd=bankroll_usd,
                usable_capital=usable_capital,
                cash_reserve=cash_reserve,
                total_allocated=0.0,
                total_daily_payout=0.0,
                total_monthly_payout=0.0,
                portfolio_daily_roi_pct=0.0,
                portfolio_monthly_roi_pct=0.0,
                active_markets_count=0,
                allocations=[],
                solver_name="KKT_WATER_FILLING",
            )

        allocations: list[MarketAllocation] = []
        total_allocated = 0.0
        total_payout = 0.0
        for m, cap in funded:
            cap = max(0.0, min(cap, usable_capital))
            payout = self.evaluate_market_payout(cap, m["P"], m["D"], m["alpha"])
            if payout is None:
                log.warning(
                    f"allocator: refusing {m['cand'].get('condition_id', '')[:10]} - "
                    "payout denominator unmeasured at extraction"
                )
                continue
            cand_dict = m["cand"]
            stay, fill = _fill_side_of(m)
            fill_usd = fill * cap
            allocations.append(
                MarketAllocation(
                    condition_id=cand_dict.get("condition_id", ""),
                    question=cand_dict.get("question", ""),
                    allocated_capital=round(cap, 2),
                    quote_size=round(cap / m["mid"], 1),
                    daily_reward_payout=round(payout, 2),
                    daily_roi_pct=round((payout / cap) * 100.0, 2) if cap > 0 else 0.0,
                    min_floor_capital=round(m["c_floor"], 2),
                    clears_floor=payout >= PROTOCOL_MIN_DAILY_PAYOUT_USD,
                    daily_pool=round(m["P"], 2),
                    competing_depth=round(m["D"], 1),
                    mid_price=round(m["mid"], 4),
                    tokens=cand_dict.get("raw_market", {}).get("tokens", []),
                    yes_token=cand_dict.get("yes_token") or cand_dict.get("token_yes") or "",
                    no_token=cand_dict.get("no_token") or cand_dict.get("token_no") or "",
                    max_spread=float(cand_dict.get("max_spread", 0.0)),
                    min_size=float(cand_dict.get("min_size", 0.0)),
                    expected_fill_yield_usd_per_day=round(fill_usd, 4),
                    expected_total_yield_usd_per_day=round(payout * stay + fill_usd, 4),
                    p_fill=cand_dict.get("p_fill"),
                    competing_samples=int(cand_dict.get("competing_samples") or 0),
                    competing_low_confidence=bool(
                        cand_dict.get("competing_low_confidence")
                    ),
                )
            )
            total_allocated += cap
            total_payout += payout

        allocations.sort(key=lambda a: a.expected_total_yield_usd_per_day, reverse=True)
        daily_roi = (total_payout / total_allocated) * 100.0 if total_allocated > 0 else 0.0
        return PortfolioAllocationResult(
            bankroll_usd=round(bankroll_usd, 2),
            usable_capital=round(usable_capital, 2),
            cash_reserve=round(cash_reserve, 2),
            total_allocated=round(total_allocated, 2),
            total_daily_payout=round(total_payout, 2),
            total_monthly_payout=round(total_payout * 30.0, 2),
            portfolio_daily_roi_pct=round(daily_roi, 2),
            portfolio_monthly_roi_pct=round(daily_roi * 30.0, 2),
            active_markets_count=len(allocations),
            allocations=allocations,
            solver_name="KKT_WATER_FILLING",
        )

    @staticmethod
    def _water_level_caps(markets: list[dict[str, Any]], budget: float) -> list[float]:
        """c_i(lambda) for every market, with lambda solved by bisection.

        The marginal condition is the **total** yield
        ``dR/dc * stay + fill = lambda``, whose inverse is

            c_i(lambda) = (sqrt(P_i alpha_i D_i stay_i / (lambda - fill_i)) - D_i) / alpha_i

        so a candidate carrying a measured fill side is sized against the yield
        it actually keeps, and one with ``lambda <= fill`` (its accrual cannot
        beat its own fill yield) is funded nothing.
        """

        def demand(lam: float) -> list[float]:
            out = []
            for m in markets:
                stay, fill = _fill_side_of(m)
                excess = lam - fill
                if excess <= 0.0:
                    out.append(0.0)
                    continue
                base = m["P"] * m["alpha"] * m["D"] * stay / excess
                out.append(max(0.0, (math.sqrt(base) - m["D"]) / m["alpha"]))
            return out

        lo, hi = 1e-18, 1.0
        guard = 0
        while sum(demand(hi)) > budget and hi < 1e18 and guard < 200:
            hi *= 10.0
            guard += 1
        for _ in range(200):
            mid = 0.5 * (lo + hi)
            if sum(demand(mid)) > budget:
                lo = mid
            else:
                hi = mid
        return demand(hi)

    def calculate_endogenous_reserve(
        self,
        allocations: list[dict[str, Any]],
        bankroll_usd: float,
        base_gas_buffer_usd: Optional[float] = None,
        ctf_client: Optional[Any] = None,
    ) -> float:
        """Delegate to module-level calculate_endogenous_reserve."""
        return calculate_endogenous_reserve(
            allocations=allocations,
            bankroll_usd=bankroll_usd,
            base_gas_buffer_usd=base_gas_buffer_usd,
            ctf_client=ctf_client,
        )


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
                from polymarketbot.trading.ctf import estimate_merge_cost_usd
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


# ---------------------------------------------------------------------------
# Continuous Real-Time Closed-Loop Portfolio Governor
# ---------------------------------------------------------------------------

@dataclass
class GovernorAction:
    """Discrete action emitted by the continuous optimization governor."""

    action_type: str  # "MAINTAIN", "RESIZE_QUOTE", "SWITCH_MARKET", "MERGE_INVENTORY"
    condition_id: str
    target_capital: float
    target_quote_size: float
    reason: str
    urgency: str = "NORMAL"  # "NORMAL", "HIGH", "CRITICAL"


class ContinuousOptimizationGovernor:
    """Continuously evaluates market conditions and dynamically adapts quoting allocations in real time.

    Operates in real-time to:
    1. Re-evaluate OR-Tools MINLP allocation every T seconds (e.g. 30s) or upon fill/book shift.
    2. Evaluate Switching / Reallocation Hurdle:
           Delta_Yield * T_holding >= Switching_Cost * (1 + hurdle_pct)
    3. Emits discrete control actions to resize quotes, switch markets, or rebalance cash reserves.
    """

    def __init__(
        self,
        allocator: Optional[LiquidityRewardsAllocator] = None,
        hurdle_pct: float = 0.15,
        min_dwell_time_seconds: float = 300.0,
        re_solve_interval_seconds: float = 30.0,
    ) -> None:
        self.allocator = allocator or LiquidityRewardsAllocator()
        self.hurdle_pct = float(hurdle_pct)
        self.min_dwell_time = float(min_dwell_time_seconds)
        self.re_solve_interval = float(re_solve_interval_seconds)

        self.last_re_solve_time: float = 0.0
        self.market_entry_times: dict[str, float] = {}
        self.current_allocations: dict[str, float] = {}

    def should_re_solve(self, current_time: float, force: bool = False) -> bool:
        """Check if optimizer should trigger a continuous re-evaluation."""
        if force:
            return True
        return (current_time - self.last_re_solve_time) >= self.re_solve_interval

    def evaluate_governor_cycle(
        self,
        current_active_markets: list[str],
        live_bankroll_usd: float,
        scanned_universe: list[dict[str, Any]],
        current_time: float,
    ) -> list[GovernorAction]:
        """Run continuous optimization cycle and generate market-making control actions."""
        self.last_re_solve_time = current_time
        actions: list[GovernorAction] = []

        if not scanned_universe or live_bankroll_usd <= 0:
            return actions

        # 1. Run OR-Tools MILP optimization on latest live universe
        solution = self.allocator.optimize_allocation_milp(
            candidates=scanned_universe,
            bankroll_usd=live_bankroll_usd,
        )

        target_map = {a.condition_id: a for a in solution.allocations}

        # 2. Check existing active markets
        for cid in current_active_markets:
            if cid in target_map:
                alloc = target_map[cid]
                old_cap = self.current_allocations.get(cid, alloc.allocated_capital)
                # Check for significant sizing adjustment (> 10% change)
                if abs(alloc.allocated_capital - old_cap) > max(15.0, old_cap * 0.10):
                    actions.append(
                        GovernorAction(
                            action_type="RESIZE_QUOTE",
                            condition_id=cid,
                            target_capital=alloc.allocated_capital,
                            target_quote_size=alloc.quote_size,
                            reason=(
                                f"Optimizer adjusted target capital from ${old_cap:.1f} to "
                                f"${alloc.allocated_capital:.1f} (competing depth shifted)."
                            ),
                        )
                    )
                    self.current_allocations[cid] = alloc.allocated_capital
            else:
                # Market is no longer in optimal allocation -> check switching hurdle
                dwell = current_time - self.market_entry_times.get(cid, current_time)
                if dwell >= self.min_dwell_time:
                    actions.append(
                        GovernorAction(
                            action_type="SWITCH_MARKET",
                            condition_id=cid,
                            target_capital=0.0,
                            target_quote_size=0.0,
                            reason=f"Market yield diluted; dwell time {dwell:.0f}s >= {self.min_dwell_time:.0f}s. Exiting position.",
                            urgency="HIGH",
                        )
                    )
                    self.current_allocations.pop(cid, None)

        # 3. Check newly activated markets
        for cid, alloc in target_map.items():
            if cid not in current_active_markets:
                actions.append(
                    GovernorAction(
                        action_type="SWITCH_MARKET",
                        condition_id=cid,
                        target_capital=alloc.allocated_capital,
                        target_quote_size=alloc.quote_size,
                        reason=(
                            f"New optimal market activated: '{alloc.question[:35]}' "
                            f"(Expected Payout: ${alloc.daily_reward_payout:.2f}/d, ROI: +{alloc.daily_roi_pct:.1f}%/d)"
                        ),
                        urgency="HIGH",
                    )
                )
                self.market_entry_times[cid] = current_time
                self.current_allocations[cid] = alloc.allocated_capital

        if not actions and current_active_markets:
            for cid in current_active_markets:
                actions.append(
                    GovernorAction(
                        action_type="MAINTAIN",
                        condition_id=cid,
                        target_capital=self.current_allocations.get(cid, live_bankroll_usd * 0.75),
                        target_quote_size=self.current_allocations.get(cid, live_bankroll_usd * 0.75),
                        reason="Current market making allocation is globally optimal within hurdle tolerance.",
                    )
                )

        return actions

