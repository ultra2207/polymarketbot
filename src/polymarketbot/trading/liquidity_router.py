"""Inter-Domain Dynamic Liquidity Router.

Coordinates dynamic capital routing between:
- Domain 1: Continuous High-Frequency Passive Market Maker & Binance Lead-Lag Alpha Engine
- Domain 2: UMA Settlement Discount & Parity Arbitrage Burst Harvester

Domain 1 holds ~85-90% of total bankroll for continuous quoting and yield capture.
When Domain 2 identifies high-conviction UMA settlement proposals with ARR >= hurdle,
the router instantaneously loans unencumbered capital from Domain 1 to Domain 2.
Upon on-chain CTF redemption, principal and profit are immediately swept back
into Domain 1's active quoting pool to compound continuously without cash drag.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.types import DiscountOpportunity

logger = get_logger("liquidity_router")


class StrategyName(str, Enum):
    """Canonical institutional nomenclature for Polymarket trading engines."""

    REWARD_HARVESTER = "RewardHarvester"
    """Continuous quoting, spread harvesting & 1440-epoch quadratic liquidity rewards (Former Domain 1)."""

    SETTLEMENT_HARVESTER = "SettlementHarvester"
    """Pre-settlement discount & resolution cycle burst harvester (Former Domain 2)."""

    BASKET_PARITY = "BasketParity"
    """Cross-spread synthetic basket arbitrage - Permanently gated at $0 capital (Former Domain 3)."""

    LATENCY_SNIPER = "LatencySniper"
    """High-frequency Binance USD-M futures lead-lag jump taker sniper (Former Strategy A)."""

    CTF_SPLITTER = "CtfSplitter"
    """On-chain complete-set minting (CTF.splitPosition) & selective cross-split arbitrage (Former Strategy C)."""

    CAPITAL_ROUTER = "CapitalRouter"
    """Dynamic inter-strategy treasury controller and liquidity loan manager."""


@dataclass
class LiquidityRouterConfig:
    """Configuration governing inter-domain capital migration and margin limits."""

    min_domain1_reserve_pct: float = 0.20
    """Minimum buffer (20%) reserved for Domain 1 open order margin and quoting buffer."""

    min_settlement_arr_hurdle: float = 150.0
    """Minimum 150% Annualized Return Rate (ARR) required for Domain 2 to request liquidity."""

    max_single_settlement_alloc_pct: float = 0.15
    """Maximum 15% of total portfolio bankroll deployable into a single UMA market."""

    settlement_repatriation_timeout_hours: float = 72.0
    """Maximum expected lockup window before an active settlement loan is flagged as overdue."""

    max_total_settlement_alloc_pct: float = 0.80
    """Maximum aggregate allocation (80%) of portfolio bankroll across all active Domain 2 loans."""


@dataclass
class ActiveSettlementLoan:
    """Record of an active liquidity loan dispatched to Domain 2."""

    condition_id: str
    allocated_usd: float
    timestamp: float
    expected_arr: float
    asset_id: str = ""
    repatriated: bool = False
    returned_usd: float = 0.0
    profit_usd: float = 0.0
    repatriated_timestamp: Optional[float] = None


@runtime_checkable
class CashInventoryManager(Protocol):
    """Interface defining cash and inventory operations for Domain 1."""

    def get_cash(self) -> float:
        """Available free cash balance in USD."""
        ...

    def get_locked_cash(self) -> float:
        """Cash locked in resting orders in USD."""
        ...

    def get_equity(self) -> float:
        """Total mark-to-market equity in USD."""
        ...

    def loan_capital(self, amount: float) -> bool:
        """Debit cash to loan to Domain 2."""
        ...

    def repatriate_capital(self, returned_usd: float, profit_usd: float) -> None:
        """Credit repatriated principal and profit from Domain 2 settlement."""
        ...


class SimpleCashInventoryManager:
    """Concrete reference implementation of CashInventoryManager for Domain 1."""

    def __init__(
        self,
        initial_cash: float = 1000.0,
        locked_cash: float = 0.0,
        inventory_value: float = 0.0,
    ) -> None:
        self.cash: float = float(initial_cash)
        self.locked_cash: float = float(locked_cash)
        self.inventory_value: float = float(inventory_value)
        self.realized_pnl: float = 0.0

    def get_cash(self) -> float:
        return self.cash

    def get_locked_cash(self) -> float:
        return self.locked_cash

    def get_equity(self) -> float:
        return self.cash + self.locked_cash + self.inventory_value

    def loan_capital(self, amount: float) -> bool:
        amt = float(amount)
        if amt <= 0.0 or amt > self.cash:
            return False
        self.cash -= amt
        return True

    def repatriate_capital(self, returned_usd: float, profit_usd: float) -> None:
        self.cash += float(returned_usd) + float(profit_usd)
        self.realized_pnl += float(profit_usd)


@dataclass
class StrategyCapacityModel:
    """Mathematical capacity and diminishing-returns model for an institutional trading strategy.

    Marginal compounding APY decays with capital deployment C according to:
        MarginalAPY(C) = base_marginal_apy / (1.0 + (C / capacity_scale_usd) ** decay_exponent)
    until C reaches max_capacity_usd (where MarginalAPY drops to 0.0).
    """

    name: str
    base_marginal_apy: float
    """Compounding APY (%) for the very first dollar deployed (C -> 0)."""

    capacity_scale_usd: float
    """Capital scale (USD) at which marginal APY decays to half of its base rate."""

    decay_exponent: float = 1.0
    """Steepness exponent of marginal yield decay as capital scales."""

    max_capacity_usd: float = float("inf")
    """Hard absolute ceiling of market capacity. Marginal yield becomes 0.0 above this."""

    min_allocation_usd: float = 0.0
    """Minimum operational capital threshold required to activate the strategy."""

    is_active: bool = True
    """Whether the strategy is actively enabled in the optimization pool."""

    def marginal_apy(self, allocated_usd: float) -> float:
        """Compute the marginal compounding APY (%) for the next increment of capital."""
        if not self.is_active:
            return 0.0
        c = max(0.0, float(allocated_usd))
        if c >= self.max_capacity_usd:
            return 0.0
        scale = max(1.0, float(self.capacity_scale_usd))
        decay = 1.0 + (c / scale) ** self.decay_exponent
        return max(0.0, self.base_marginal_apy / decay)

    def cumulative_annual_profit_usd(self, allocated_usd: float, steps: int = 100) -> float:
        """Numerically integrate marginal yield to compute total expected annual profit in USD:

        Profit(C) = Integral_0^C (MarginalAPY(c) / 100.0) dc
        """
        c = max(0.0, float(allocated_usd))
        if c <= 0.0 or not self.is_active:
            return 0.0
        capped_c = min(c, self.max_capacity_usd)
        n = max(10, steps)
        dc = capped_c / float(n)
        # Trapezoidal rule for smooth convex decay curve
        total = 0.5 * (self.marginal_apy(0.0) + self.marginal_apy(capped_c))
        for i in range(1, n):
            total += self.marginal_apy(i * dc)
        return (total * dc) / 100.0

    def effective_apy(self, allocated_usd: float) -> float:
        """Compute the blended/effective annual percentage yield (%) across the allocated capital."""
        c = max(0.0, float(allocated_usd))
        if c <= 0.0:
            return self.base_marginal_apy if self.is_active else 0.0
        profit = self.cumulative_annual_profit_usd(c)
        return (profit / c) * 100.0


@dataclass
class AllocationSlice:
    """Individual portfolio allocation slice assigned to a single strategy."""

    strategy: str
    allocated_usd: float
    allocation_pct: float
    marginal_apy: float
    cumulative_annual_profit_usd: float
    effective_apy: float


@dataclass
class MetaAllocationPlan:
    """Global multi-strategy capital allocation plan generated by the MetaOptimizer."""

    total_capital_usd: float
    allocations: Dict[str, AllocationSlice]
    blended_marginal_apy: float
    total_annual_profit_usd: float
    blended_effective_apy: float
    compounded_arr_pct: float
    daily_profit_usd: float
    monthly_profit_usd: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert allocation plan to serializable dictionary."""
        return {
            "total_capital_usd": round(self.total_capital_usd, 2),
            "allocations": {
                k: {
                    "allocated_usd": round(v.allocated_usd, 2),
                    "allocation_pct": round(v.allocation_pct, 2),
                    "marginal_apy": round(v.marginal_apy, 2),
                    "cumulative_annual_profit_usd": round(v.cumulative_annual_profit_usd, 2),
                    "effective_apy": round(v.effective_apy, 2),
                }
                for k, v in self.allocations.items()
            },
            "blended_marginal_apy": round(self.blended_marginal_apy, 2),
            "total_annual_profit_usd": round(self.total_annual_profit_usd, 2),
            "blended_effective_apy": round(self.blended_effective_apy, 2),
            "compounded_arr_pct": round(self.compounded_arr_pct, 2),
            "daily_profit_usd": round(self.daily_profit_usd, 2),
            "monthly_profit_usd": round(self.monthly_profit_usd, 2),
            "timestamp": self.timestamp,
        }


def get_default_strategy_models() -> List[StrategyCapacityModel]:
    """Empirically calibrated capacity models derived from the 45 GB Polymarket tape and rewards distribution.

    Parameters:
    - RewardHarvester: $33.8M/yr exchange rewards across 8,000 qualifying markets; high initial marginal yield, decays as book depth saturates.
    - SettlementHarvester: $140M-$390M/day resolving volume, $2M-$6M/day high-conviction UMA proposal flow; linear capacity up to $6M.
    - CtfSplitter: Complete-set minting (CTF.splitPosition) cross-spread arbitrage; vast liquidity scale up to $10M.
    - LatencySniper: Benched due to artificial 250ms taker delay leaving only shallow retail quotes ($21.48 median).
    """
    return [
        StrategyCapacityModel(
            name=StrategyName.REWARD_HARVESTER.value,
            base_marginal_apy=19111.0,
            capacity_scale_usd=40_000.0,
            decay_exponent=1.15,
            max_capacity_usd=3_000_000.0,
            is_active=True,
        ),
        StrategyCapacityModel(
            name=StrategyName.SETTLEMENT_HARVESTER.value,
            base_marginal_apy=1277.0,
            capacity_scale_usd=2_500_000.0,
            decay_exponent=0.95,
            max_capacity_usd=6_000_000.0,
            is_active=True,
        ),
        StrategyCapacityModel(
            name=StrategyName.CTF_SPLITTER.value,
            base_marginal_apy=581.0,
            capacity_scale_usd=6_000_000.0,
            decay_exponent=0.85,
            max_capacity_usd=10_000_000.0,
            is_active=True,
        ),
        StrategyCapacityModel(
            name=StrategyName.LATENCY_SNIPER.value,
            base_marginal_apy=23.9,
            capacity_scale_usd=2_000.0,
            decay_exponent=1.5,
            max_capacity_usd=5_000.0,
            is_active=False,  # Benched: 250ms speed bump eliminates maker misquotes
        ),
    ]


class MetaOptimizerCapitalRouter:
    """Greedy Equi-Marginal Water-Filling Meta-Optimizer.

    Dynamically allocates portfolio capital across all active trading strategies.
    In accordance with the Equi-Marginal Principle:
    1. Capital is routed incrementally to whichever strategy currently delivers the highest
       marginal compounding APY (#1).
    2. As capital flows in, diminishing marginal returns cause its marginal yield to decay.
    3. The exact instant its marginal APY decays to match the #2 strategy, the optimizer
       routes capital to the new #1 (or splits between both along the equi-marginal frontier).
    4. This continues until 100% of capital is deployed, guaranteeing mathematical optimality
       and maximizing total portfolio compounding ARR.
    """

    def __init__(
        self,
        strategy_models: Optional[List[StrategyCapacityModel]] = None,
        router: Optional[Any] = None,
    ) -> None:
        models = strategy_models if strategy_models is not None else get_default_strategy_models()
        self._models: Dict[str, StrategyCapacityModel] = {m.name: m for m in models}
        self.router = router
        logger.info(
            "MetaOptimizerCapitalRouter initialized with {} strategies: {}",
            len(self._models),
            list(self._models.keys()),
        )

    def register_strategy(self, model: StrategyCapacityModel) -> None:
        """Register or update a strategy capacity model."""
        self._models[model.name] = model
        logger.info(
            "Registered strategy model: {} (base APY: {:.1f}%, active: {})",
            model.name,
            model.base_marginal_apy,
            model.is_active,
        )

    def activate_strategy(self, name: str) -> None:
        """Activate a previously benched or registered strategy."""
        if name in self._models:
            self._models[name].is_active = True
            logger.info("Activated strategy: {}", name)

    def bench_strategy(self, name: str) -> None:
        """Bench a strategy so it receives 0.0 capital allocation."""
        if name in self._models:
            self._models[name].is_active = False
            logger.info("Benched strategy: {}", name)

    def get_strategy_model(self, name: str) -> Optional[StrategyCapacityModel]:
        """Retrieve strategy capacity model by name."""
        return self._models.get(name)

    def optimize(
        self,
        total_capital_usd: float,
        steps: int = 2000,
    ) -> MetaAllocationPlan:
        """Execute greedy equi-marginal water-filling optimization.

        In accordance with the Equi-Marginal Principle:
        - At each increment, capital is routed to the strategy with the highest marginal compounding APY (#1).
        - As capital is added, diminishing marginal returns decay that strategy's marginal yield.
        - The instant its marginal yield decays to match the #2 strategy, the router routes to the new #1
          (or splits along the equi-marginal frontier), mathematically maximizing total portfolio profit.

        Args:
            total_capital_usd: Total portfolio capital to allocate (USD).
            steps: Number of discrete capital increments for numerical water-filling.

        Returns:
            MetaAllocationPlan with strategy slices and aggregate portfolio yield.
        """
        total_cap = max(0.0, float(total_capital_usd))
        active_models = [m for m in self._models.values() if m.is_active]

        if total_cap <= 0.0 or not active_models:
            empty_slices = {
                name: AllocationSlice(
                    strategy=name,
                    allocated_usd=0.0,
                    allocation_pct=0.0,
                    marginal_apy=m.marginal_apy(0.0),
                    cumulative_annual_profit_usd=0.0,
                    effective_apy=m.effective_apy(0.0),
                )
                for name, m in self._models.items()
            }
            return MetaAllocationPlan(
                total_capital_usd=total_cap,
                allocations=empty_slices,
                blended_marginal_apy=0.0,
                total_annual_profit_usd=0.0,
                blended_effective_apy=0.0,
                compounded_arr_pct=0.0,
                daily_profit_usd=0.0,
                monthly_profit_usd=0.0,
            )

        # 1. Greedy Incremental Water-Filling along Equi-Marginal Frontier
        allocated: Dict[str, float] = {m.name: 0.0 for m in active_models}
        step_count = max(100, int(steps))
        delta_usd = total_cap / float(step_count)

        for _ in range(step_count):
            # Compute current marginal compounding APY for the next dollar of capital
            current_yields = {
                m.name: m.marginal_apy(allocated[m.name])
                for m in active_models
            }

            # Find current #1 strategy
            best_strat, best_yield = max(current_yields.items(), key=lambda item: item[1])

            if best_yield <= 0.0:
                # All active strategies have exhausted their capacity caps
                # Fallback: allocate remaining delta to highest-capacity active strategy
                highest_cap_strat = max(active_models, key=lambda m: m.max_capacity_usd).name
                allocated[highest_cap_strat] += delta_usd
            else:
                allocated[best_strat] += delta_usd

        # 2. Build Slices & Aggregate Profit
        slices: Dict[str, AllocationSlice] = {}
        total_profit = 0.0

        for name, m in self._models.items():
            alloc_usd = round(allocated.get(name, 0.0), 2)
            pct = (alloc_usd / total_cap) * 100.0 if total_cap > 0 else 0.0
            m_apy = m.marginal_apy(alloc_usd)
            profit_usd = m.cumulative_annual_profit_usd(alloc_usd)
            eff_apy = m.effective_apy(alloc_usd)
            total_profit += profit_usd

            slices[name] = AllocationSlice(
                strategy=name,
                allocated_usd=alloc_usd,
                allocation_pct=round(pct, 2),
                marginal_apy=round(m_apy, 2),
                cumulative_annual_profit_usd=round(profit_usd, 2),
                effective_apy=round(eff_apy, 2),
            )

        blended_effective_apy = (total_profit / total_cap) * 100.0 if total_cap > 0 else 0.0

        # Weighted marginal APY along the active equi-marginal frontier
        active_allocs = [s for s in slices.values() if s.allocated_usd > 0.0]
        if active_allocs:
            sum_alloc = sum(s.allocated_usd for s in active_allocs)
            blended_marginal_apy = sum(s.marginal_apy * s.allocated_usd for s in active_allocs) / sum_alloc
        else:
            blended_marginal_apy = 0.0

        # Compounding ARR: daily compounding over 365 calendar days
        daily_rate = (blended_effective_apy / 100.0) / 365.0
        if daily_rate < 50.0:
            compounded_arr_pct = ((1.0 + daily_rate) ** 365.0 - 1.0) * 100.0
        else:
            compounded_arr_pct = float("inf")

        return MetaAllocationPlan(
            total_capital_usd=round(total_cap, 2),
            allocations=slices,
            blended_marginal_apy=round(blended_marginal_apy, 2),
            total_annual_profit_usd=round(total_profit, 2),
            blended_effective_apy=round(blended_effective_apy, 2),
            compounded_arr_pct=round(compounded_arr_pct, 2),
            daily_profit_usd=round(total_profit / 365.0, 2),
            monthly_profit_usd=round(total_profit / 12.0, 2),
        )

    def rebalance_delta(
        self,
        current_allocations: Dict[str, float],
        total_equity: float,
        min_rebalance_pct: float = 0.02,
    ) -> Dict[str, float]:
        """Compute capital adjustment delta per strategy to reach optimal equi-marginal allocation.

        Filters out adjustments smaller than min_rebalance_pct * total_equity.
        """
        plan = self.optimize(total_equity)
        threshold_usd = total_equity * min_rebalance_pct
        deltas: Dict[str, float] = {}

        for name, slice_ in plan.allocations.items():
            current = float(current_allocations.get(name, 0.0))
            target = slice_.allocated_usd
            delta = target - current
            if abs(delta) >= threshold_usd:
                deltas[name] = round(delta, 2)
            else:
                deltas[name] = 0.0

        return deltas


class InterDomainLiquidityRouter:
    """Production Liquidity Router coordinating capital between Domain 1 and Domain 2."""

    def __init__(
        self,
        domain1_engine: Optional[Any] = None,
        domain2_harvester: Optional[Any] = None,
        config: Optional[LiquidityRouterConfig] = None,
    ) -> None:
        self.config = config or LiquidityRouterConfig()
        self.domain1_engine = domain1_engine
        self.domain2_harvester = domain2_harvester

        # Active & completed loans
        self._active_loans: Dict[str, ActiveSettlementLoan] = {}
        self._completed_loans: List[ActiveSettlementLoan] = []

        # Lifetime counters
        self.total_rotated_capital: float = 0.0
        self.total_repatriated_capital: float = 0.0
        self.total_profit_earned: float = 0.0
        self.total_loans_count: int = 0
        self.start_timestamp: float = time.time()

        # Meta-Optimizer Equi-Marginal Engine
        self.meta_optimizer: MetaOptimizerCapitalRouter = MetaOptimizerCapitalRouter(router=self)

        logger.info(
            "InterDomainLiquidityRouter initialized (min_reserve={:.0%}, ARR_hurdle={:.0f}%, max_single_alloc={:.0%})",
            self.config.min_domain1_reserve_pct,
            self.config.min_settlement_arr_hurdle,
            self.config.max_single_settlement_alloc_pct,
        )

    def register_domain1(self, engine: Any) -> None:
        """Register or update Domain 1 (RewardHarvester) trading engine / cash manager."""
        self.domain1_engine = engine
        logger.info("RewardHarvester (Domain 1) registered with CapitalRouter")

    def register_reward_harvester(self, engine: Any) -> None:
        """Register or update RewardHarvester (Domain 1) trading engine."""
        self.register_domain1(engine)

    def register_domain2(self, harvester: Any) -> None:
        """Register or update Domain 2 (SettlementHarvester) settlement harvester."""
        self.domain2_harvester = harvester
        logger.info("SettlementHarvester (Domain 2) registered with CapitalRouter")

    def register_settlement_harvester(self, harvester: Any) -> None:
        """Register or update SettlementHarvester (Domain 2) settlement harvester."""
        self.register_domain2(harvester)

    def compute_opportunity_arr(self, opportunity: DiscountOpportunity) -> float:
        """Compute the Annualized Return Rate (ARR) in percent for an evaluated discount opportunity."""
        # Check if opportunity already carries an explicit expected_arr attribute
        if hasattr(opportunity, "expected_arr") and getattr(opportunity, "expected_arr") is not None:
            arr_val = float(getattr(opportunity, "expected_arr"))
            # Normalize decimal ratio to percent if <= 10.0
            if 0.0 < arr_val <= 10.0:
                arr_val *= 100.0
            return arr_val

        hours = max(0.01, float(opportunity.hours_to_liveness_expiry))
        price = opportunity.recommended_bid_price if opportunity.recommended_bid_price > 0 else opportunity.best_bid

        # If price is unavailable or 0, fallback to discount_bps
        if price <= 0.0:
            if hasattr(opportunity, "discount_bps") and opportunity.discount_bps > 0:
                return (opportunity.discount_bps / 10000.0) * (8760.0 / hours) * 100.0
            return 0.0

        proposed = opportunity.proposed_price if opportunity.proposed_price > 0 else 1.0
        margin = (proposed - price) / price
        if margin <= 0.0:
            return 0.0

        arr_pct = margin * (8760.0 / hours) * 100.0
        return arr_pct

    def _get_domain1_equity(self) -> float:
        """Calculate total mark-to-market equity in Domain 1."""
        if self.domain1_engine is None:
            return 0.0
        if hasattr(self.domain1_engine, "get_equity"):
            try:
                return float(self.domain1_engine.get_equity())
            except Exception as e:
                logger.debug("Failed calling get_equity() on domain1: {}", e)
        if hasattr(self.domain1_engine, "cash"):
            return float(self.domain1_engine.cash)
        return 0.0

    def _get_domain1_free_cash(self) -> float:
        """Get Domain 1 unencumbered free cash balance (excluding open order margin)."""
        if self.domain1_engine is None:
            return 0.0
        if hasattr(self.domain1_engine, "get_cash"):
            try:
                return float(self.domain1_engine.get_cash())
            except Exception as e:
                logger.debug("Failed calling get_cash() on domain1: {}", e)
        if hasattr(self.domain1_engine, "cash"):
            return float(self.domain1_engine.cash)
        return 0.0

    def _get_domain2_equity(self) -> float:
        """Calculate total mark-to-market equity and inventory in Domain 2."""
        if self.domain2_harvester is not None:
            if hasattr(self.domain2_harvester, "get_domain2_local_equity"):
                try:
                    return float(self.domain2_harvester.get_domain2_local_equity())
                except Exception as e:
                    logger.debug("Failed calling get_domain2_local_equity() on domain2: {}", e)
            if hasattr(self.domain2_harvester, "available_cash"):
                cash = float(self.domain2_harvester.available_cash)
                pos_val = 0.0
                if hasattr(self.domain2_harvester, "_positions"):
                    pos_val = sum(
                        p.shares * p.entry_price
                        for p in self.domain2_harvester._positions.values()
                        if not p.is_redeemed
                    )
                return cash + pos_val
        # Standalone or test mode: active loans count as Domain 2 asset
        return sum(l.allocated_usd for l in self._active_loans.values() if not l.repatriated)

    def get_total_portfolio_equity(self) -> float:
        """Sum of Domain 1 cash + open positions + Domain 2 cash + locked settlement inventory."""
        d1_eq = self._get_domain1_equity()
        d2_eq = self._get_domain2_equity()

        # If domain2 is attached, d2_eq includes both available cash and active positions.
        # If domain2 is not attached, d2_eq counts the active loan principal as receivables.
        total_equity = d1_eq + d2_eq
        return round(total_equity, 4)

    def request_settlement_capital(
        self,
        opportunity: DiscountOpportunity,
        requested_usd: float,
    ) -> float:
        """Evaluate and grant an instantaneous liquidity loan to Domain 2 for a settlement discount.

        Checks:
        1. ARR qualification: must satisfy min_settlement_arr_hurdle (e.g. >= 150% ARR).
        2. Single-bet cap: single market allocation <= max_single_settlement_alloc_pct of portfolio bankroll.
        3. Domain 1 reserve: Domain 1 unencumbered cash must remain above min_domain1_reserve_pct buffer.
        4. Aggregate cap: total active settlement loans <= max_total_settlement_alloc_pct.

        Returns:
            Approved allocation in USD (0.0 if rejected).
        """
        cid = str(opportunity.condition_id).lower()
        req = float(requested_usd)
        if req <= 0.0:
            return 0.0

        # 1. ARR Hurdle Check
        arr = self.compute_opportunity_arr(opportunity)
        if arr < self.config.min_settlement_arr_hurdle:
            logger.info(
                "Capital request rejected for {}: ARR {:.1f}% < hurdle {:.1f}%",
                cid[:14], arr, self.config.min_settlement_arr_hurdle,
            )
            return 0.0

        # 2. Portfolio Equity & Sizing Caps
        total_equity = self.get_total_portfolio_equity()
        if total_equity <= 0.0:
            logger.warning("Capital request rejected: total portfolio equity is <= 0")
            return 0.0

        # Max single settlement market cap
        max_single_alloc = total_equity * self.config.max_single_settlement_alloc_pct
        existing_loan = self._active_loans[cid].allocated_usd if cid in self._active_loans else 0.0
        remaining_market_room = max(0.0, max_single_alloc - existing_loan)
        if remaining_market_room <= 0.0:
            logger.info(
                "Capital request rejected for {}: single market cap ${:.2f} reached (existing: ${:.2f})",
                cid[:14], max_single_alloc, existing_loan,
            )
            return 0.0

        # 3. Domain 1 Unencumbered Cash & Reserve Buffer
        d1_free_cash = self._get_domain1_free_cash()
        d1_reserve_buffer = total_equity * self.config.min_domain1_reserve_pct
        unencumbered_cash = max(0.0, d1_free_cash - d1_reserve_buffer)
        if unencumbered_cash <= 0.0:
            logger.info(
                "Capital request rejected for {}: Domain 1 unencumbered cash is $0.00 (free cash ${:.2f} <= reserve buffer ${:.2f})",
                cid[:14], d1_free_cash, d1_reserve_buffer,
            )
            return 0.0

        # 4. Aggregate Settlement Cap Check
        total_active_loans = sum(l.allocated_usd for l in self._active_loans.values())
        max_aggregate_alloc = total_equity * self.config.max_total_settlement_alloc_pct
        remaining_aggregate_room = max(0.0, max_aggregate_alloc - total_active_loans)
        if remaining_aggregate_room <= 0.0:
            logger.info(
                "Capital request rejected for {}: aggregate settlement cap ${:.2f} reached",
                cid[:14], max_aggregate_alloc,
            )
            return 0.0

        # 5. Determine Approved Allocation Size
        approved = min(req, remaining_market_room, unencumbered_cash, remaining_aggregate_room)
        approved = round(approved, 4)
        if approved <= 0.0:
            return 0.0

        # 6. Execute Transfer: Debit Domain 1
        transfer_success = False
        if self.domain1_engine is not None:
            if hasattr(self.domain1_engine, "loan_capital"):
                transfer_success = bool(self.domain1_engine.loan_capital(approved))
            elif hasattr(self.domain1_engine, "cash") and self.domain1_engine.cash >= approved:
                self.domain1_engine.cash -= approved
                if hasattr(self.domain1_engine, "risk_manager") and hasattr(self.domain1_engine.risk_manager, "portfolio_state"):
                    self.domain1_engine.risk_manager.portfolio_state.current_cash_usd = self.domain1_engine.cash
                transfer_success = True
        else:
            # Standalone router operation without attached engine
            transfer_success = True

        if not transfer_success:
            logger.warning("Domain 1 failed to execute loan transfer of ${:.2f}", approved)
            return 0.0

        # 7. Credit Domain 2
        if self.domain2_harvester is not None:
            if hasattr(self.domain2_harvester, "receive_routed_capital"):
                self.domain2_harvester.receive_routed_capital(approved)
            elif hasattr(self.domain2_harvester, "available_cash"):
                self.domain2_harvester.available_cash += approved

        # 8. Record Active Loan
        if cid in self._active_loans:
            self._active_loans[cid].allocated_usd += approved
        else:
            self._active_loans[cid] = ActiveSettlementLoan(
                condition_id=cid,
                allocated_usd=approved,
                timestamp=time.time(),
                expected_arr=arr,
                asset_id=getattr(opportunity, "winning_asset_id", ""),
            )

        self.total_rotated_capital += approved
        self.total_loans_count += 1

        logger.info(
            "Approved loan for {}: ${:.2f} USDC (ARR: {:.1f}%, Single Cap: ${:.2f}, Reserve Buffer: ${:.2f})",
            cid[:14], approved, arr, max_single_alloc, d1_reserve_buffer,
        )
        return approved

    def release_settlement_capital(
        self,
        condition_id: str,
        returned_usd: float,
        profit_usd: float,
    ) -> None:
        """Called when Domain 2 redeems via CTF; immediately sweeps principal + profit back to Domain 1.

        Ensures capital immediately compounds inside Domain 1's active quoting pool without cash drag.
        """
        cid = str(condition_id).lower()
        now = time.time()
        ret = float(returned_usd)
        prof = float(profit_usd)

        loan = self._active_loans.pop(cid, None)
        if loan is not None:
            # Handle both conventions and partial fills:
            # Convention A: returned_usd is total payout proceeds (principal + profit)
            if abs(ret - (loan.allocated_usd + prof)) < 1e-4:
                total_credit = ret
                principal = ret - prof
                unspent = 0.0
            elif abs(ret - loan.allocated_usd) < 1e-4:
                # Full loan principal returned
                total_credit = ret + prof
                principal = ret
                unspent = 0.0
            else:
                # Partial fill redemption: returned_usd is the redeemed position cost basis
                # Any unspent portion of the allocated loan held in Domain 2 is swept back
                unspent = max(0.0, loan.allocated_usd - ret)
                if self.domain2_harvester is not None and hasattr(self.domain2_harvester, "available_cash"):
                    unspent = min(float(self.domain2_harvester.available_cash), unspent)
                total_credit = ret + prof + unspent
                principal = ret + unspent

            loan.repatriated = True
            loan.returned_usd = principal
            loan.profit_usd = prof
            loan.repatriated_timestamp = now
            self._completed_loans.append(loan)
        else:
            total_credit = ret + prof
            principal = ret
            unspent = 0.0

        # Credit Domain 1
        if self.domain1_engine is not None:
            if hasattr(self.domain1_engine, "repatriate_capital"):
                self.domain1_engine.repatriate_capital(principal, prof)
            elif hasattr(self.domain1_engine, "cash"):
                self.domain1_engine.cash += total_credit
                if hasattr(self.domain1_engine, "risk_manager") and hasattr(self.domain1_engine.risk_manager, "portfolio_state"):
                    self.domain1_engine.risk_manager.portfolio_state.current_cash_usd = self.domain1_engine.cash
                if hasattr(self.domain1_engine, "realized_pnl"):
                    self.domain1_engine.realized_pnl += prof

        # Debit Domain 2 (if available_cash holds the redeemed funds)
        if self.domain2_harvester is not None:
            if hasattr(self.domain2_harvester, "repatriate_routed_capital"):
                self.domain2_harvester.repatriate_routed_capital(total_credit)
            elif hasattr(self.domain2_harvester, "available_cash"):
                self.domain2_harvester.available_cash = max(
                    0.0, self.domain2_harvester.available_cash - total_credit
                )

        # Update telemetry counters
        self.total_repatriated_capital += principal
        self.total_profit_earned += prof

        logger.info(
            "Repatriated settlement capital for {}: principal ${:.2f} + profit ${:.2f} -> Domain 1 quoting pool (${:.2f} total)",
            cid[:14], principal, prof, total_credit,
        )

    def get_timed_out_loans(self) -> List[ActiveSettlementLoan]:
        """Return list of active loans exceeding settlement_repatriation_timeout_hours."""
        now = time.time()
        timeout_seconds = self.config.settlement_repatriation_timeout_hours * 3600.0
        return [
            loan
            for loan in self._active_loans.values()
            if (now - loan.timestamp) > timeout_seconds
        ]

    def get_telemetry(self) -> Dict[str, Any]:
        """Report allocation ratio, active loans, lifetime capital rotated, and incremental ARR boost."""
        total_equity = self.get_total_portfolio_equity()
        d1_equity = self._get_domain1_equity()
        d2_equity = self._get_domain2_equity()

        d1_pct = round(d1_equity / max(1e-6, total_equity), 4)
        d2_pct = round(d2_equity / max(1e-6, total_equity), 4)

        now = time.time()
        active_loans_usd = sum(l.allocated_usd for l in self._active_loans.values())
        active_loans_list = [
            {
                "condition_id": l.condition_id,
                "allocated_usd": round(l.allocated_usd, 2),
                "expected_arr": round(l.expected_arr, 2),
                "age_hours": round((now - l.timestamp) / 3600.0, 2),
                "is_timed_out": ((now - l.timestamp) / 3600.0) > self.config.settlement_repatriation_timeout_hours,
            }
            for l in self._active_loans.values()
        ]

        # Calculate incremental ARR boost from dynamic routing
        if self._completed_loans:
            total_loan_volume = sum(l.allocated_usd for l in self._completed_loans)
            weighted_arr = (
                sum(l.expected_arr * l.allocated_usd for l in self._completed_loans)
                / max(1.0, total_loan_volume)
            )
            # Duration-weighted profit return
            durations = [
                max(0.01, (l.repatriated_timestamp - l.timestamp) / 3600.0)
                for l in self._completed_loans
                if l.repatriated_timestamp is not None
            ]
            avg_duration_hours = sum(durations) / max(1, len(durations)) if durations else 24.0
            incremental_arr_boost = (
                (self.total_profit_earned / max(1.0, total_equity))
                * (8760.0 / avg_duration_hours)
                * 100.0
            )
        elif self._active_loans:
            total_loan_volume = sum(l.allocated_usd for l in self._active_loans.values())
            weighted_arr = (
                sum(l.expected_arr * l.allocated_usd for l in self._active_loans.values())
                / max(1.0, total_loan_volume)
            )
            incremental_arr_boost = weighted_arr * (total_loan_volume / max(1.0, total_equity))
        else:
            weighted_arr = 0.0
            incremental_arr_boost = 0.0

        optimal_plan = self.compute_meta_optimal_allocation(total_equity, steps=500)

        return {
            "total_portfolio_equity": round(total_equity, 2),
            "domain1_equity": round(d1_equity, 2),
            "domain2_equity": round(d2_equity, 2),
            "allocation_ratio": {
                "domain1_pct": d1_pct,
                "domain2_pct": d2_pct,
            },
            "domain1_unencumbered_cash": round(
                max(0.0, self._get_domain1_free_cash() - (total_equity * self.config.min_domain1_reserve_pct)), 2
            ),
            "domain1_reserve_buffer_usd": round(total_equity * self.config.min_domain1_reserve_pct, 2),
            "active_loans_count": len(self._active_loans),
            "active_loans_usd": round(active_loans_usd, 2),
            "active_loans": active_loans_list,
            "timed_out_loans_count": len(self.get_timed_out_loans()),
            "lifetime_capital_rotated_usd": round(self.total_rotated_capital, 2),
            "lifetime_capital_repatriated_usd": round(self.total_repatriated_capital, 2),
            "lifetime_profit_usd": round(self.total_profit_earned, 2),
            "lifetime_loans_count": self.total_loans_count,
            "weighted_settlement_arr_pct": round(weighted_arr, 2),
            "incremental_arr_boost_pct": round(incremental_arr_boost, 2),
            "meta_optimal_plan": optimal_plan.to_dict(),
        }

    def compute_meta_optimal_allocation(
        self,
        total_capital_usd: Optional[float] = None,
        steps: int = 2000,
    ) -> MetaAllocationPlan:
        """Compute globally optimal multi-strategy allocation using the equi-marginal meta-optimizer.

        Args:
            total_capital_usd: Total capital to allocate. Defaults to total portfolio equity.
            steps: Numerical integration steps for water-filling.

        Returns:
            MetaAllocationPlan detailing optimal strategy allocations and compounded yield.
        """
        capital = total_capital_usd if total_capital_usd is not None else self.get_total_portfolio_equity()
        return self.meta_optimizer.optimize(capital, steps=steps)


# Canonical Institutional Alias
CapitalRouter = InterDomainLiquidityRouter


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Meta-Optimizer Capital Router Equi-Marginal Allocator")
    parser.add_argument("--capital", type=float, default=None, help="Specific capital amount (USD) to allocate")
    parser.add_argument("--steps", type=int, default=2000, help="Number of water-filling increments")
    parser.add_argument("--json", action="store_true", help="Output raw JSON plan")
    args = parser.parse_args()

    optimizer = MetaOptimizerCapitalRouter()

    test_tiers = [1_000.0, 10_000.0, 100_000.0, 1_000_000.0, 10_000_000.0] if args.capital is None else [args.capital]

    for cap in test_tiers:
        plan = optimizer.optimize(cap, steps=args.steps)
        if args.json:
            print(json.dumps(plan.to_dict(), indent=2))
        else:
            print("\n" + "=" * 80)
            print(f"META-OPTIMIZER ALLOCATION: ${plan.total_capital_usd:,.2f} USD")
            print("=" * 80)
            print(f"{'Strategy':<22} {'Allocated USD':>14} {'Share':>8} {'Marginal APY':>14} {'Effective APY':>15} {'Annual Profit':>15}")
            print("-" * 80)
            for strat_name, s in plan.allocations.items():
                print(
                    f"{strat_name:<22} "
                    f"${s.allocated_usd:>13,.2f} "
                    f"{s.allocation_pct:>7.1f}% "
                    f"{s.marginal_apy:>13.2f}% "
                    f"{s.effective_apy:>14.2f}% "
                    f"${s.cumulative_annual_profit_usd:>14,.2f}"
                )
            print("-" * 80)
            print(
                f"{'PORTFOLIO TOTAL':<22} "
                f"${plan.total_capital_usd:>13,.2f} "
                f"{'100.0%':>8} "
                f"{plan.blended_marginal_apy:>13.2f}% "
                f"{plan.blended_effective_apy:>14.2f}% "
                f"${plan.total_annual_profit_usd:>14,.2f}"
            )
            print(f"  * Daily Run-Rate Profit : ${plan.daily_profit_usd:,.2f} / day")
            print(f"  * Monthly Run-Rate      : ${plan.monthly_profit_usd:,.2f} / month")
            print(f"  * Compounded ARR (365d) : {plan.compounded_arr_pct:,.2f}% ARR")

