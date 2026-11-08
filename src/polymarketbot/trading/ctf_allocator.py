"""Marginal Capital Allocator for Conditional Tokens Framework (CTF) Parity Arbitrage.

Mathematical Architecture & Financial Theory:
---------------------------------------------
In CTF Minting / Merging parity arbitrage:
- Minting complete sets: 1 complete set costs exactly $1.000 USDC on-chain.
- Unwinding legs: selling into resting orderbook bids (or buying resting asks under par and merging).
- Orderbook depth is finite: resting bids/asks form discrete price ladders. Deploying more
  capital requires sweeping deeper into the book at progressively worse prices (slippage).
- Polymarket taker fees are non-linear:
      Fee(shares, price) = shares * rate * (price * (1 - price))^exponent * (1 - vip_rebate)
- Gas friction is fixed per batch: ~85,000 gas units (~$0.00125 - $0.00140).

As deployed capital C increases:
1. Gross proceeds R(C) is strictly concave (piecewise-linear concave due to descending bids).
2. Taker fees and slippage increase monotonically with depth.
3. Total Net Profit:
       Pi(C) = R(C) - C - Gas - Fees(C)
   has a strict global maximum at optimal capital C* where marginal net profit:
       dPi/dC = 0  (or dPi/dC = hurdle_rate)
4. Beyond C*, every additional dollar deployed causes strictly negative marginal profit (dPi/dC < 0),
   eroding realized net PnL!

Multi-Market Portfolio Allocation:
----------------------------------
Given N dislocated parity markets across the exchange and a capital budget B:
    max sum_{j=1}^N Pi_j(C_j)   subject to   sum C_j <= B,  0 <= C_j <= C_j*
By KKT optimality, capital is water-filled such that the marginal return:
    M_j(C_j) = dPi_j/dC_j = lambda
is equalized across all funded markets, where lambda >= 0 is the shadow price of capital.
Markets unable to amortize their fixed batch gas friction are pruned from the active set.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.trading.ctf_split_arbitrage import (
    TYPICAL_SPLIT_GAS_UNITS,
    VIP_TIER_REBATES,
    compute_leg_taker_fee,
    estimate_polygon_eip1559_gas_usd,
)

logger = get_logger("ctf_allocator")

TYPICAL_MERGE_GAS_UNITS = 78_000


class ArbitrageDirection(str, Enum):
    """Direction of the parity arbitrage execution."""

    MINT_SPLIT = "mint_split"      # Mint complete sets ($1.000 par), sell legs into bids
    MERGE_UNWIND = "merge_unwind"  # Buy legs from asks under par ($1.000), merge complete sets


class ExecutionMode(str, Enum):
    """Execution mode across legs."""

    ALL_TAKER = "all_taker"              # Sweep all legs aggressively into resting book (atomic, 0 broken-basket risk)
    SELECTIVE_MAKER = "selective_maker"  # 1 leg aggressive Taker, N-1 legs resting Maker (0% fee on maker legs)


@dataclass(slots=True, frozen=True)
class OrderBookLevel:
    """Single price-quantity level in an L2 orderbook."""

    price: float
    size: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.price <= 1.0):
            raise ValueError(f"Price out of range [0, 1]: {self.price}")
        if self.size < 0.0:
            raise ValueError(f"Size cannot be negative: {self.size}")


@dataclass
class ParityLegDepth:
    """L2 depth and pricing metadata for one outcome leg."""

    token_id: str
    condition_id: str
    title: str = ""
    side: str = "BID"                    # "BID" (for mint) or "ASK" (for merge)
    levels: list[OrderBookLevel] = field(default_factory=list)
    fee_rate: float = 0.02
    fee_exponent: float = 1.0
    fee_schedule: Optional[FeeSchedule] = None
    is_maker: bool = False               # If True, incurs 0% taker fee

    @classmethod
    def from_raw(
        cls,
        token_id: str,
        condition_id: str,
        side: str,
        raw_levels: Sequence[Any],
        title: str = "",
        fee_rate: float = 0.02,
        fee_exponent: float = 1.0,
        fee_schedule: Optional[FeeSchedule] = None,
        is_maker: bool = False,
    ) -> "ParityLegDepth":
        """Instantiate ParityLegDepth directly from raw CLOB dictionaries or tuples."""
        levels: list[OrderBookLevel] = []
        for item in raw_levels:
            try:
                if isinstance(item, (tuple, list)) and len(item) >= 2:
                    px, sz = float(item[0]), float(item[1])
                elif isinstance(item, dict):
                    px = float(item.get("price", 0.0))
                    sz = float(item.get("size", 0.0))
                elif isinstance(item, OrderBookLevel):
                    px, sz = item.price, item.size
                else:
                    continue
                if 0.0 <= px <= 1.0 and sz > 0.0:
                    levels.append(OrderBookLevel(price=px, size=sz))
            except (ValueError, TypeError):
                continue
        return cls(
            token_id=token_id,
            condition_id=condition_id,
            title=title,
            side=side.upper(),
            levels=levels,
            fee_rate=fee_rate,
            fee_exponent=fee_exponent,
            fee_schedule=fee_schedule,
            is_maker=is_maker,
        )

    def sorted_levels(self) -> list[OrderBookLevel]:
        """Returns levels sorted best to worst: descending for bids, ascending for asks."""
        if not self.levels:
            return []
        # Consolidate identical price levels
        consolidated: dict[float, float] = {}
        for lvl in self.levels:
            p = round(float(lvl.price), 6)
            s = float(lvl.size)
            consolidated[p] = consolidated.get(p, 0.0) + s

        is_bid = (self.side.upper() == "BID")
        sorted_px = sorted(consolidated.keys(), reverse=is_bid)
        return [OrderBookLevel(price=p, size=consolidated[p]) for p in sorted_px if consolidated[p] > 0]

    def get_fee_schedule(self) -> FeeSchedule:
        if self.fee_schedule is not None:
            return self.fee_schedule
        return FeeSchedule(rate=self.fee_rate, exponent=self.fee_exponent, taker_only=True)


@dataclass
class ParityTranche:
    """A discrete tranche in the combined multi-leg orderbook depth."""

    tranche_index: int
    start_shares: float
    end_shares: float
    capacity_shares: float                   # delta S = end_shares - start_shares
    leg_prices: list[float]                  # active price on each leg during this tranche
    leg_unit_fees: list[float]               # taker fee per share on each leg
    capital_per_set: float                   # capital required per complete set
    gross_proceeds_per_set: float            # gross cash received/redeemed per set
    net_proceeds_per_set: float              # proceeds net of taker fees per set
    marginal_profit_per_set: float           # dPi/dS = net_proceeds - capital
    marginal_return_per_dollar: float        # dPi/dC = marginal_profit / capital
    cumulative_shares: float
    cumulative_capital: float
    cumulative_gross_profit: float
    cumulative_net_fees: float
    cumulative_net_profit_before_gas: float


@dataclass
class SingleMarketSweepResult:
    """Complete mathematical depth-sweeping analysis for a single parity market."""

    market_id: str
    condition_id: str
    direction: ArbitrageDirection
    execution_mode: ExecutionMode
    tranches: list[ParityTranche]
    gas_cost_usd: float
    optimal_shares: float                    # S* where dPi/dS reaches 0 or hurdle rate
    optimal_capital: float                   # C* = capital to fund S*
    optimal_gross_profit: float
    optimal_net_fees: float
    optimal_rebates: float
    optimal_net_profit: float                # Pi(C*) = gross - fees - gas
    optimal_roi_bps: float
    marginal_return_at_opt: float            # dPi/dC at C*
    breakeven_shares: float                  # Minimum shares needed to cover fixed gas
    breakeven_capital: float                 # Minimum capital needed to cover fixed gas
    is_viable: bool                          # True if optimal_net_profit >= min_net_edge_usd
    taker_leg_index: Optional[int] = None
    summary: str = ""

    def evaluate_at_shares(self, shares: float) -> Tuple[float, float, float]:
        """Evaluates (capital_needed, net_profit, marginal_return_per_dollar) at given share size."""
        if shares <= 0.0 or not self.tranches:
            return 0.0, -self.gas_cost_usd, 0.0

        target_s = min(shares, self.tranches[-1].end_shares)
        total_capital = 0.0
        total_net_pnl_before_gas = 0.0
        active_marginal_return = 0.0

        for t in self.tranches:
            if target_s <= t.start_shares:
                break
            fill_in_tranche = min(target_s - t.start_shares, t.capacity_shares)
            ratio = fill_in_tranche / t.capacity_shares
            total_capital += fill_in_tranche * t.capital_per_set
            total_net_pnl_before_gas += fill_in_tranche * t.marginal_profit_per_set
            active_marginal_return = t.marginal_return_per_dollar
            if target_s <= t.end_shares:
                break

        net_profit = total_net_pnl_before_gas - self.gas_cost_usd
        return round(total_capital, 4), round(net_profit, 4), active_marginal_return

    def evaluate_at_capital(self, capital: float) -> Tuple[float, float, float]:
        """Evaluates (shares_minted, net_profit, marginal_return_per_dollar) at given capital budget."""
        if capital <= 0.0 or not self.tranches:
            return 0.0, -self.gas_cost_usd, 0.0

        rem_c = capital
        total_shares = 0.0
        total_net_pnl_before_gas = 0.0
        active_marginal_return = 0.0

        for t in self.tranches:
            tranche_cap = t.capacity_shares * t.capital_per_set
            if rem_c <= 0:
                break
            allocated_c = min(rem_c, tranche_cap)
            fill_shares = allocated_c / t.capital_per_set
            total_shares += fill_shares
            total_net_pnl_before_gas += fill_shares * t.marginal_profit_per_set
            active_marginal_return = t.marginal_return_per_dollar
            rem_c -= allocated_c

        net_profit = total_net_pnl_before_gas - self.gas_cost_usd
        return round(total_shares, 4), round(net_profit, 4), active_marginal_return


@dataclass
class MarketAllocation:
    """Optimal capital and execution allocation for one market in the portfolio."""

    market_id: str
    condition_id: str
    question: str
    direction: ArbitrageDirection
    allocated_capital: float
    allocated_shares: float
    expected_gross_profit: float
    expected_net_fees: float
    expected_gas_cost: float
    expected_net_profit: float
    expected_roi_bps: float
    marginal_return: float
    is_capacity_constrained: bool            # True if allocated capital == optimal C* (book depth limit)
    leg_execution_summary: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PortfolioAllocationResult:
    """Result of the multi-market marginal equal-utility water-filling allocation."""

    total_budget: float
    total_allocated_capital: float
    unallocated_capital: float
    total_expected_net_profit: float
    total_expected_roi_bps: float
    shadow_price_lambda: float               # Marginal return threshold equalized across active markets
    allocations: dict[str, MarketAllocation]
    candidate_count: int
    funded_count: int
    pruned_markets: list[str] = field(default_factory=list)


class MarginalCtfAllocator:
    """Optimal Marginal Capital Allocator for CTF Parity and Negative-Risk Arbitrage.

    Calculates exact depth-sweeping marginal profit functions:
        M(S) = dPi/dS
    Determines global profit-maximizing capacity C*, and distributes portfolio capital
    across N dislocated markets to equalize marginal utility and maximize net portfolio return.
    """

    def __init__(
        self,
        vip_tier: str = "bronze",
        hurdle_rate: float = 0.0,
        min_net_edge_usd: float = 0.0015,
        default_gas_price_gwei: float = 40.0,
        default_pol_usd_price: float = 0.40,
        gas_units_split: int = TYPICAL_SPLIT_GAS_UNITS,
        gas_units_merge: int = TYPICAL_MERGE_GAS_UNITS,
        max_capital_per_market: Optional[float] = None,
    ) -> None:
        self.vip_tier = vip_tier.lower()
        if self.vip_tier not in VIP_TIER_REBATES:
            raise ValueError(f"Invalid vip_tier '{vip_tier}'. Allowed: {list(VIP_TIER_REBATES.keys())}")
        self.hurdle_rate = max(0.0, hurdle_rate)
        self.min_net_edge_usd = max(0.0, min_net_edge_usd)
        self.default_gas_price_gwei = default_gas_price_gwei
        self.default_pol_usd_price = default_pol_usd_price
        self.gas_units_split = gas_units_split
        self.gas_units_merge = gas_units_merge
        self.max_capital_per_market = max_capital_per_market

    @property
    def vip_rebate_rate(self) -> float:
        return VIP_TIER_REBATES.get(self.vip_tier, 0.0)

    def compute_gas_cost_usd(
        self,
        direction: ArbitrageDirection = ArbitrageDirection.MINT_SPLIT,
        gas_price_gwei: Optional[float] = None,
        pol_usd_price: Optional[float] = None,
    ) -> float:
        units = self.gas_units_split if direction == ArbitrageDirection.MINT_SPLIT else self.gas_units_merge
        gwei = gas_price_gwei if gas_price_gwei is not None else self.default_gas_price_gwei
        px = pol_usd_price if pol_usd_price is not None else self.default_pol_usd_price
        return estimate_polygon_eip1559_gas_usd(gas_units=units, gas_price_gwei=gwei, pol_usd_price=px)

    def compute_sweep_curve(
        self,
        market_id: str,
        condition_id: str,
        legs: Sequence[ParityLegDepth],
        direction: ArbitrageDirection = ArbitrageDirection.MINT_SPLIT,
        execution_mode: ExecutionMode = ExecutionMode.ALL_TAKER,
        gas_price_gwei: Optional[float] = None,
        pol_usd_price: Optional[float] = None,
        taker_leg_index: Optional[int] = None,
    ) -> SingleMarketSweepResult:
        """Derive the piecewise-linear marginal profit function across multi-leg orderbooks.

        Synthesizes the L2 ladders of all legs into unified discrete tranches, calculating:
            Pi(S) = R_net(S) - C(S) - Gas
            M(S)  = dPi/dS
        And locates the global maximum S* where M(S*) = hurdle_rate (or 0).
        """
        k = len(legs)
        gas_cost = self.compute_gas_cost_usd(direction, gas_price_gwei, pol_usd_price)

        if k < 2:
            return SingleMarketSweepResult(
                market_id=market_id,
                condition_id=condition_id,
                direction=direction,
                execution_mode=execution_mode,
                tranches=[],
                gas_cost_usd=gas_cost,
                optimal_shares=0.0,
                optimal_capital=0.0,
                optimal_gross_profit=0.0,
                optimal_net_fees=0.0,
                optimal_rebates=0.0,
                optimal_net_profit=0.0,
                optimal_roi_bps=0.0,
                marginal_return_at_opt=0.0,
                breakeven_shares=0.0,
                breakeven_capital=0.0,
                is_viable=False,
                summary="Insufficient legs (< 2)",
            )

        # In selective maker mode, if taker_leg_index is not specified, evaluate all legs and select best
        if execution_mode == ExecutionMode.SELECTIVE_MAKER and taker_leg_index is None:
            best_res: Optional[SingleMarketSweepResult] = None
            for cand_idx in range(k):
                cand_res = self.compute_sweep_curve(
                    market_id=market_id,
                    condition_id=condition_id,
                    legs=legs,
                    direction=direction,
                    execution_mode=execution_mode,
                    gas_price_gwei=gas_price_gwei,
                    pol_usd_price=pol_usd_price,
                    taker_leg_index=cand_idx,
                )
                if best_res is None or cand_res.optimal_net_profit > best_res.optimal_net_profit:
                    best_res = cand_res
            return best_res or self._empty_result(market_id, condition_id, direction, execution_mode, gas_cost)

        # 1. Extract sorted levels for each leg
        leg_sorted_levels = [leg.sorted_levels() for leg in legs]
        if any(len(lvls) == 0 for lvls in leg_sorted_levels):
            return self._empty_result(market_id, condition_id, direction, execution_mode, gas_cost, "Empty book depth on at least one leg")

        # Compute cumulative depth per leg
        cum_depth_per_leg: list[list[float]] = []
        for lvls in leg_sorted_levels:
            cd = []
            curr = 0.0
            for lvl in lvls:
                curr += lvl.size
                cd.append(curr)
            cum_depth_per_leg.append(cd)

        max_feasible_shares = min(cd[-1] for cd in cum_depth_per_leg)
        if max_feasible_shares <= 0.0:
            return self._empty_result(market_id, condition_id, direction, execution_mode, gas_cost, "Zero liquidity available")

        # 2. Collect all discrete transition points
        crit_pts_set = {0.0, max_feasible_shares}
        for cd in cum_depth_per_leg:
            for pt in cd:
                if 0.0 < pt < max_feasible_shares:
                    crit_pts_set.add(round(pt, 6))

        sorted_pts = sorted(crit_pts_set)

        # 3. Build discrete tranches
        tranches: list[ParityTranche] = []
        cum_shares = 0.0
        cum_cap = 0.0
        cum_gross_profit = 0.0
        cum_net_fees = 0.0
        cum_net_profit = 0.0

        for idx in range(len(sorted_pts) - 1):
            s_start = sorted_pts[idx]
            s_end = sorted_pts[idx + 1]
            cap_shares = s_end - s_start
            if cap_shares <= 1e-6:
                continue

            mid_s = (s_start + s_end) / 2.0

            # Find active price and fee on each leg
            active_prices: list[float] = []
            active_fees: list[float] = []

            for leg_i in range(k):
                lvls = leg_sorted_levels[leg_i]
                cds = cum_depth_per_leg[leg_i]
                # Find which level covers mid_s
                lvl_idx = 0
                for c_i, c_val in enumerate(cds):
                    if mid_s <= c_val:
                        lvl_idx = c_i
                        break
                price = lvls[lvl_idx].price
                active_prices.append(price)

                # Determine if taker or maker
                is_leg_maker = False
                if execution_mode == ExecutionMode.SELECTIVE_MAKER:
                    is_leg_maker = (leg_i != taker_leg_index)
                elif legs[leg_i].is_maker:
                    is_leg_maker = True

                if is_leg_maker:
                    active_fees.append(0.0)
                else:
                    gross_f, reb, net_f = compute_leg_taker_fee(
                        shares=1.0,  # per-share unit fee
                        price=price,
                        fee_schedule=legs[leg_i].get_fee_schedule(),
                        vip_tier=self.vip_tier,
                    )
                    active_fees.append(net_f)

            # Financial economics of this tranche
            if direction == ArbitrageDirection.MINT_SPLIT:
                # Mint 1 set for $1.000, sell legs at bids
                cap_per_set = 1.000
                gross_payout_per_set = sum(active_prices)
                total_fees_per_set = sum(active_fees)
                net_payout_per_set = gross_payout_per_set - total_fees_per_set
                marginal_profit_per_set = net_payout_per_set - cap_per_set
                marginal_return_per_dollar = marginal_profit_per_set / cap_per_set
            else:  # MERGE_UNWIND
                # Buy legs at asks, redeem 1 set for $1.000 par
                gross_ask_cost = sum(active_prices)
                total_fees_per_set = sum(active_fees)
                cap_per_set = gross_ask_cost + total_fees_per_set
                gross_payout_per_set = 1.000
                net_payout_per_set = gross_payout_per_set
                marginal_profit_per_set = gross_payout_per_set - cap_per_set
                marginal_return_per_dollar = marginal_profit_per_set / max(1e-4, cap_per_set)

            tranche_cap = cap_shares * cap_per_set
            tranche_gross_profit = cap_shares * (gross_payout_per_set - (cap_per_set if direction == ArbitrageDirection.MINT_SPLIT else sum(active_prices)))
            tranche_fees = cap_shares * sum(active_fees)
            tranche_net_profit = cap_shares * marginal_profit_per_set

            cum_shares += cap_shares
            cum_cap += tranche_cap
            cum_gross_profit += tranche_gross_profit
            cum_net_fees += tranche_fees
            cum_net_profit += tranche_net_profit

            tranche = ParityTranche(
                tranche_index=idx,
                start_shares=round(s_start, 4),
                end_shares=round(s_end, 4),
                capacity_shares=round(cap_shares, 4),
                leg_prices=active_prices,
                leg_unit_fees=active_fees,
                capital_per_set=round(cap_per_set, 6),
                gross_proceeds_per_set=round(gross_payout_per_set, 6),
                net_proceeds_per_set=round(net_payout_per_set, 6),
                marginal_profit_per_set=round(marginal_profit_per_set, 6),
                marginal_return_per_dollar=round(marginal_return_per_dollar, 6),
                cumulative_shares=round(cum_shares, 4),
                cumulative_capital=round(cum_cap, 4),
                cumulative_gross_profit=round(cum_gross_profit, 4),
                cumulative_net_fees=round(cum_net_fees, 4),
                cumulative_net_profit_before_gas=round(cum_net_profit, 4),
            )
            tranches.append(tranche)

        if not tranches:
            return self._empty_result(market_id, condition_id, direction, execution_mode, gas_cost, "No valid tranches constructed")

        # 4. Find global optimal depth S* where marginal return >= hurdle_rate
        optimal_shares = 0.0
        optimal_capital = 0.0
        optimal_gross_profit = 0.0
        optimal_net_fees = 0.0
        optimal_net_profit_before_gas = 0.0
        marginal_at_opt = tranches[0].marginal_return_per_dollar

        for t in tranches:
            if t.marginal_return_per_dollar >= self.hurdle_rate:
                optimal_shares = t.end_shares
                optimal_capital = t.cumulative_capital
                optimal_gross_profit = t.cumulative_gross_profit
                optimal_net_fees = t.cumulative_net_fees
                optimal_net_profit_before_gas = t.cumulative_net_profit_before_gas
                marginal_at_opt = t.marginal_return_per_dollar
            else:
                break

        # Calculate net profit after gas friction
        optimal_net_profit = round(optimal_net_profit_before_gas - gas_cost, 4)

        # 5. Breakeven depth calculation (shares needed to cover fixed gas)
        breakeven_shares = 0.0
        breakeven_capital = 0.0
        if optimal_net_profit > 0:
            for t in tranches:
                if t.cumulative_net_profit_before_gas >= gas_cost:
                    # Interpolate within this tranche
                    prev_pnl = t.cumulative_net_profit_before_gas - (t.capacity_shares * t.marginal_profit_per_set)
                    needed = gas_cost - prev_pnl
                    frac = needed / max(1e-6, t.marginal_profit_per_set)
                    breakeven_shares = round(t.start_shares + frac, 4)
                    breakeven_capital = round(breakeven_shares * t.capital_per_set, 4)
                    break

        is_viable = (
            optimal_shares > 0.0
            and optimal_net_profit >= self.min_net_edge_usd
        )

        roi_bps = (
            round((optimal_net_profit / optimal_capital) * 10_000.0, 2)
            if optimal_capital > 0.0
            else 0.0
        )

        summary = (
            f"Direction: {direction.value.upper()} | Viable: {is_viable} | "
            f"S* = {optimal_shares:.1f} shs ($ {optimal_capital:.2f}) | "
            f"Net PnL: ${optimal_net_profit:.2f} ({roi_bps:.1f} bps) | "
            f"Gas: ${gas_cost:.4f} | Marginal ROI @ S*: {marginal_at_opt * 10_000:.1f} bps"
        )

        return SingleMarketSweepResult(
            market_id=market_id,
            condition_id=condition_id,
            direction=direction,
            execution_mode=execution_mode,
            tranches=tranches,
            gas_cost_usd=gas_cost,
            optimal_shares=optimal_shares,
            optimal_capital=optimal_capital,
            optimal_gross_profit=round(optimal_gross_profit, 4),
            optimal_net_fees=round(optimal_net_fees, 4),
            optimal_rebates=round(optimal_net_fees * self.vip_rebate_rate, 4),
            optimal_net_profit=optimal_net_profit,
            optimal_roi_bps=roi_bps,
            marginal_return_at_opt=marginal_at_opt,
            breakeven_shares=breakeven_shares,
            breakeven_capital=breakeven_capital,
            is_viable=is_viable,
            taker_leg_index=taker_leg_index,
            summary=summary,
        )

    def allocate_portfolio(
        self,
        candidates: Sequence[SingleMarketSweepResult],
        total_budget: float,
        max_capital_per_market: Optional[float] = None,
    ) -> PortfolioAllocationResult:
        """Equal-Marginal Water-Filling Capital Allocator across N dislocated markets.

        Allocates budget B across all viable opportunities such that:
        1. Marginal profit dPi/dC = lambda is equalized across all funded markets.
        2. No market is allocated more than its optimal depth C* (preventing negative marginal return).
        3. Markets unable to amortize their fixed batch gas friction are pruned.
        """
        market_cap_limit = max_capital_per_market or self.max_capital_per_market

        # Filter strictly viable candidates (optimal net profit >= min_net_edge_usd)
        viable_candidates = [
            c for c in candidates
            if c.is_viable and c.optimal_capital > 0.0 and c.optimal_net_profit >= self.min_net_edge_usd
        ]

        if not viable_candidates or total_budget <= 0.0:
            return PortfolioAllocationResult(
                total_budget=total_budget,
                total_allocated_capital=0.0,
                unallocated_capital=total_budget,
                total_expected_net_profit=0.0,
                total_expected_roi_bps=0.0,
                shadow_price_lambda=0.0,
                allocations={},
                candidate_count=len(candidates),
                funded_count=0,
            )

        active_set = list(viable_candidates)
        pruned_market_ids: list[str] = []

        # Iterative active-set loop: prune underfunded markets failing gas amortization
        while active_set:
            # Flatten all positive-marginal tranches from active candidates
            # Each entry: (marginal_return, market_idx, tranche_idx, tranche_obj)
            pool_tranches = []
            for m_idx, cand in enumerate(active_set):
                for t in cand.tranches:
                    if t.marginal_return_per_dollar >= self.hurdle_rate and t.start_shares < cand.optimal_shares:
                        pool_tranches.append((t.marginal_return_per_dollar, m_idx, t))

            # Sort globally descending by marginal return per dollar
            pool_tranches.sort(key=lambda x: x[0], reverse=True)

            # Greedily water-fill budget B down the ordered tranches
            rem_budget = total_budget
            market_allocations_usd: dict[int, float] = {i: 0.0 for i in range(len(active_set))}
            market_allocations_shs: dict[int, float] = {i: 0.0 for i in range(len(active_set))}
            shadow_lambda = 0.0

            for m_ret, m_idx, t in pool_tranches:
                if rem_budget <= 1e-4:
                    break

                cand = active_set[m_idx]
                curr_cap = market_allocations_usd[m_idx]

                # Market cap headroom
                max_cand_cap = cand.optimal_capital
                if market_cap_limit is not None:
                    max_cand_cap = min(max_cand_cap, market_cap_limit)

                market_headroom = max(0.0, max_cand_cap - curr_cap)
                if market_headroom <= 1e-4:
                    continue

                tranche_cap_avail = t.capacity_shares * t.capital_per_set
                alloc_usd = min(rem_budget, tranche_cap_avail, market_headroom)

                if alloc_usd > 1e-4:
                    shares_added = alloc_usd / t.capital_per_set
                    market_allocations_usd[m_idx] += alloc_usd
                    market_allocations_shs[m_idx] += shares_added
                    rem_budget -= alloc_usd
                    shadow_lambda = m_ret

            # Check gas friction and feasibility on all funded markets
            underfunded_found = False
            for m_idx, cand in enumerate(active_set):
                alloc_c = market_allocations_usd[m_idx]
                if alloc_c > 0.0:
                    shares, net_pnl, _ = cand.evaluate_at_capital(alloc_c)
                    if net_pnl < self.min_net_edge_usd:
                        # Market is underfunded and cannot clear gas friction; prune it
                        pruned_market_ids.append(cand.market_id)
                        active_set.pop(m_idx)
                        underfunded_found = True
                        break  # restart water-filling with candidate removed

            if not underfunded_found:
                # All funded markets clear gas friction and hurdle! Optimal solution reached.
                break

        # Construct final PortfolioAllocationResult
        allocations: dict[str, MarketAllocation] = {}
        total_allocated = 0.0
        total_expected_net = 0.0

        for m_idx, cand in enumerate(active_set):
            alloc_c = market_allocations_usd.get(m_idx, 0.0)
            if alloc_c <= 1e-4:
                continue

            shares, net_pnl, cur_margin = cand.evaluate_at_capital(alloc_c)
            if net_pnl <= 0.0 or shares <= 0.0:
                continue

            # Leg execution breakdown
            leg_summary = []
            for t in cand.tranches:
                if shares <= t.start_shares:
                    break
                fill = min(shares - t.start_shares, t.capacity_shares)
                leg_summary.append({
                    "tranche_index": t.tranche_index,
                    "shares": fill,
                    "prices": t.leg_prices,
                    "fees": [f * fill for f in t.leg_unit_fees],
                })
                if shares <= t.end_shares:
                    break

            gross_profit = net_pnl + cand.gas_cost_usd + sum(sum(item["fees"]) for item in leg_summary)
            net_fees = sum(sum(item["fees"]) for item in leg_summary)
            roi_bps = (net_pnl / alloc_c) * 10_000.0 if alloc_c > 0 else 0.0

            alloc_obj = MarketAllocation(
                market_id=cand.market_id,
                condition_id=cand.condition_id,
                question="",
                direction=cand.direction,
                allocated_capital=round(alloc_c, 2),
                allocated_shares=round(shares, 2),
                expected_gross_profit=round(gross_profit, 4),
                expected_net_fees=round(net_fees, 4),
                expected_gas_cost=cand.gas_cost_usd,
                expected_net_profit=round(net_pnl, 4),
                expected_roi_bps=round(roi_bps, 2),
                marginal_return=cur_margin,
                is_capacity_constrained=(alloc_c >= cand.optimal_capital - 0.01),
                leg_execution_summary=leg_summary,
            )
            allocations[cand.market_id] = alloc_obj
            total_allocated += alloc_c
            total_expected_net += net_pnl

        tot_roi = (total_expected_net / total_allocated) * 10_000.0 if total_allocated > 0 else 0.0

        return PortfolioAllocationResult(
            total_budget=total_budget,
            total_allocated_capital=round(total_allocated, 2),
            unallocated_capital=round(max(0.0, total_budget - total_allocated), 2),
            total_expected_net_profit=round(total_expected_net, 4),
            total_expected_roi_bps=round(tot_roi, 2),
            shadow_price_lambda=round(shadow_lambda, 6),
            allocations=allocations,
            candidate_count=len(candidates),
            funded_count=len(allocations),
            pruned_markets=pruned_market_ids,
        )

    def _empty_result(
        self,
        market_id: str,
        condition_id: str,
        direction: ArbitrageDirection,
        execution_mode: ExecutionMode,
        gas_cost: float,
        reason: str = "",
    ) -> SingleMarketSweepResult:
        return SingleMarketSweepResult(
            market_id=market_id,
            condition_id=condition_id,
            direction=direction,
            execution_mode=execution_mode,
            tranches=[],
            gas_cost_usd=gas_cost,
            optimal_shares=0.0,
            optimal_capital=0.0,
            optimal_gross_profit=0.0,
            optimal_net_fees=0.0,
            optimal_rebates=0.0,
            optimal_net_profit=0.0,
            optimal_roi_bps=0.0,
            marginal_return_at_opt=0.0,
            breakeven_shares=0.0,
            breakeven_capital=0.0,
            is_viable=False,
            summary=f"Non-viable: {reason}",
        )
