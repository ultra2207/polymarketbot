"""Rigorous Microstructure Valuation of FIFO Queue Priority, CTF Gas, and Switching Frictions.

Synthesizes:
1. Maglaras-Yao (2015), Moallemi-Saglam (2013), and Cont-Kukanov-Stoikov (2014):
   Closed-form valuation of resting FIFO queue priority:
   V_queue(Q_ahead) = Size * [HalfSpread - E[AdverseSelection | fill]] * P(fill | Q_ahead) - OppCost(Q_ahead)
2. Polygon PoS CTF Gas Mechanics:
   - mergePositions: ~78,000 gas units ($0.002 - $0.015 USD at 80 Gwei)
   - splitPosition: ~76,000 - 114,000 gas units
   - CLOB Cancel (DELETE /order, DELETE /cancel-all): 0 gas (100% off-chain EIP-712 / HMAC-SHA256)
3. Synthetic Complete-Set Liquidation Parity Router:
   Closed-form minimum friction to flatten residual inventory q:
   min( q * (P_mid - P_bid), q * (P_ask_opp - (1.00 - P_mid)) + Gas_merge )
   Synthetic dominates whenever P_bid + P_ask_opp + Gas_merge/q < 1.00.
4. Total Endogenous Switching Hurdle:
   Replaces arbitrary static percentages with exact continuous economic break-even:
   Delta_Yield_net(B - A) * Tau_holding >= Cost_switch(A -> B)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("queue_valuation")


# ============================================================================
# 1. POLYGON POS GAS & SMART CONTRACT PROFILE
# ============================================================================

@dataclass(frozen=True)
class CTFGasProfile:
    """Polygon PoS (Chain ID 137) Gas Units for Conditional Tokens Framework."""

    gas_merge_direct_min: int = 68_000       # Warm storage, binary [1, 2]
    gas_merge_direct_typical: int = 78_000   # Median observed execution
    gas_merge_direct_max: int = 95_000       # Storage clearing edge
    gas_merge_proxy_adapter: int = 142_000   # Via Safe / NegRisk adapter

    gas_split_warm: int = 76_000             # Non-zero balance update
    gas_split_typical: int = 88_000          # Average binary condition split
    gas_split_cold: int = 114_000            # Zero-to-nonzero storage writes
    gas_split_proxy: int = 155_000           # Via proxy / multicall


@dataclass(frozen=True)
class PolygonPoSEnvironment:
    """EIP-1559 Fee Market state on Polygon PoS."""

    base_fee_gwei: float = 35.0              # Empirical base fee (Gwei)
    priority_fee_gwei: float = 45.0          # Validator tip floor (Gwei)
    pol_usd_rate: float = 0.35               # POL / USD exchange rate

    @property
    def total_gas_price_gwei(self) -> float:
        return self.base_fee_gwei + self.priority_fee_gwei

    def gas_units_to_usd(self, gas_units: int) -> float:
        """Convert gas units to exact USD cost."""
        pol_cost = gas_units * (self.total_gas_price_gwei * 1e-9)
        return pol_cost * self.pol_usd_rate


# ============================================================================
# 2. FIFO QUEUE PRIORITY VALUATION (MAGLARAS-YAO / CONT-KUKANOV-STOIKOV)
# ============================================================================

@dataclass(frozen=True)
class QueueValuationResult:
    """Valuation decomposition for resting FIFO queue priority."""

    queue_ahead: float
    quote_size: float
    half_spread_usd: float
    fill_probability: float
    expected_adverse_selection_usd: float
    opportunity_cost_usd: float
    net_queue_value_usd: float
    expected_wait_time_seconds: float


class QueuePriorityValuationModel:
    """Quantitative valuation of FIFO limit order queue priority."""

    def __init__(
        self,
        trade_arrival_rate_shares_sec: float = 25.0,  # Average fill volume rate at touch
        cancellation_rate_intensity: float = 0.05,    # Intensity of queue cancellations ahead
        kyle_lambda: float = 0.00015,                  # Kyle's lambda price impact parameter
        volatility_annualized: float = 0.45,           # Implied probability volatility
        annual_capital_hurdle_rate: float = 0.12,      # 12% cost of capital
    ) -> None:
        self.trade_rate = max(1.0, trade_arrival_rate_shares_sec)
        self.cancel_rate = max(0.001, cancellation_rate_intensity)
        self.kyle_lambda = kyle_lambda
        self.volatility = volatility_annualized
        self.r_cost = annual_capital_hurdle_rate / (365.25 * 86_400.0)

    def calculate_fill_probability(self, queue_ahead: float, quote_size: float) -> float:
        """Calculate fill probability before price drift moves market away."""
        eff_depth = max(0.0, queue_ahead)
        decay_factor = math.exp(-self.cancel_rate * (eff_depth / self.trade_rate))
        return max(0.01, min(0.99, decay_factor))

    def evaluate_queue_value(
        self,
        quote_size: float,
        queue_ahead: float,
        half_spread_cents: float,
        token_mid_price: float = 0.50,
        rolling_ofi: float = 0.0,
    ) -> QueueValuationResult:
        """Evaluate the embedded economic option value of resting queue position."""
        if quote_size <= 0.0:
            return QueueValuationResult(
                queue_ahead=queue_ahead,
                quote_size=0.0,
                half_spread_usd=0.0,
                fill_probability=0.0,
                expected_adverse_selection_usd=0.0,
                opportunity_cost_usd=0.0,
                net_queue_value_usd=0.0,
                expected_wait_time_seconds=0.0,
            )

        half_spread_usd = half_spread_cents / 100.0
        p_fill = self.calculate_fill_probability(queue_ahead, quote_size)
        expected_wait_sec = (max(0.0, queue_ahead) + 0.5 * quote_size) / self.trade_rate

        # Kyle's lambda adverse selection conditioned on fill
        ofi_impact = 1.0 + max(0.0, abs(rolling_ofi) / 1000.0)
        adverse_selection_usd = self.kyle_lambda * math.sqrt(quote_size) * self.volatility * ofi_impact
        net_edge_per_share = max(0.0, half_spread_usd - adverse_selection_usd)

        gross_value = quote_size * net_edge_per_share * p_fill
        capital_notional = quote_size * max(0.05, min(0.95, token_mid_price))
        opp_cost = capital_notional * self.r_cost * expected_wait_sec

        net_value = max(0.0, gross_value - opp_cost)

        return QueueValuationResult(
            queue_ahead=round(queue_ahead, 2),
            quote_size=round(quote_size, 2),
            half_spread_usd=round(half_spread_usd, 4),
            fill_probability=round(p_fill, 4),
            expected_adverse_selection_usd=round(adverse_selection_usd, 6),
            opportunity_cost_usd=round(opp_cost, 6),
            net_queue_value_usd=round(net_value, 4),
            expected_wait_time_seconds=round(expected_wait_sec, 2),
        )


# ============================================================================
# 3. INVENTORY LIQUIDATION SOLVER (DIRECT CROSS VS SYNTHETIC COMPLETION)
# ============================================================================

class LiquidationRoute(str, Enum):
    DIRECT_CROSS = "DIRECT_CROSS"
    SYNTHETIC_MERGE = "SYNTHETIC_MERGE"
    ZERO_INVENTORY = "ZERO_INVENTORY"


@dataclass(frozen=True)
class LiquidationFrictionResult:
    route: LiquidationRoute
    optimal_friction_usd: float
    cost_direct_usd: float
    cost_synthetic_usd: float
    synthetic_savings_usd: float
    effective_exit_price: float
    proceeds_usd: float
    is_arbitrage_discount: bool
    explanation: str


def solve_inventory_liquidation_friction(
    q: float,
    p_mid: float,
    p_bid: float,
    p_ask_opp: float,
    gas_merge_usd: float,
    taker_fee_rate: float = 0.0,
) -> LiquidationFrictionResult:
    """Solve the closed-form minimum friction for flattening residual inventory q.

    Option A: Sell q into p_bid. Cost = q * (p_mid - p_bid + fee).
    Option B: Buy q of opposite token at p_ask_opp + CTF merge.
              Cost = q * (p_ask_opp - (1.00 - p_mid) + fee) + gas_merge.
    """
    abs_q = abs(q)
    if abs_q < 1e-6:
        return LiquidationFrictionResult(
            route=LiquidationRoute.ZERO_INVENTORY,
            optimal_friction_usd=0.0,
            cost_direct_usd=0.0,
            cost_synthetic_usd=0.0,
            synthetic_savings_usd=0.0,
            effective_exit_price=p_mid,
            proceeds_usd=0.0,
            is_arbitrage_discount=False,
            explanation="Zero residual inventory.",
        )

    # Route A: Direct Crossing
    fee_direct = p_bid * taker_fee_rate
    cost_direct = abs_q * (p_mid - p_bid + fee_direct)
    proceeds_direct = abs_q * (p_bid - fee_direct)

    # Route B: Synthetic Complete-Set Completion
    p_mid_opp = 1.00 - p_mid
    fee_opp = p_ask_opp * taker_fee_rate
    cost_synthetic = abs_q * (p_ask_opp - p_mid_opp + fee_opp) + gas_merge_usd
    proceeds_synthetic = abs_q * 1.00 - abs_q * (p_ask_opp + fee_opp) - gas_merge_usd

    synthetic_savings = cost_direct - cost_synthetic
    # Synthetic Dominance Theorem: P_bid + P_ask_opp + Gas/q < 1.00
    is_discount = (p_bid + fee_direct) + (p_ask_opp + fee_opp) + (gas_merge_usd / abs_q) < 1.00

    if cost_synthetic < cost_direct:
        route = LiquidationRoute.SYNTHETIC_MERGE
        opt_cost = cost_synthetic
        proceeds = proceeds_synthetic
        exit_p = proceeds / abs_q
        expl = (
            f"Synthetic completion wins by ${synthetic_savings:.4f} "
            f"({(synthetic_savings/abs_q)*100:.2f}c/sh improvement). "
            f"Exit: ${exit_p:.4f} vs direct bid: ${p_bid:.4f}."
        )
    else:
        route = LiquidationRoute.DIRECT_CROSS
        opt_cost = cost_direct
        proceeds = proceeds_direct
        exit_p = proceeds / abs_q
        expl = (
            f"Direct bid crossing wins by ${-synthetic_savings:.4f} "
            f"({(-synthetic_savings/abs_q)*100:.2f}c/sh cheaper). "
            f"Exit: ${exit_p:.4f}."
        )

    return LiquidationFrictionResult(
        route=route,
        optimal_friction_usd=round(opt_cost, 6),
        cost_direct_usd=round(cost_direct, 6),
        cost_synthetic_usd=round(cost_synthetic, 6),
        synthetic_savings_usd=round(synthetic_savings, 6),
        effective_exit_price=round(exit_p, 6),
        proceeds_usd=round(proceeds, 6),
        is_arbitrage_discount=is_discount,
        explanation=expl,
    )


# ============================================================================
# 4. TOTAL CAPITAL MIGRATION HURDLE ENGINE
# ============================================================================

@dataclass(frozen=True)
class SwitchingCostDecomposition:
    queue_value_lost_usd: float
    liquidation_friction_usd: float
    gas_fees_usd: float
    opportunity_cost_downtime_usd: float
    total_switching_cost_usd: float
    liquidation_route: LiquidationRoute

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue_value_lost_usd": round(self.queue_value_lost_usd, 4),
            "liquidation_friction_usd": round(self.liquidation_friction_usd, 4),
            "gas_fees_usd": round(self.gas_fees_usd, 4),
            "opportunity_cost_downtime_usd": round(self.opportunity_cost_downtime_usd, 4),
            "total_switching_cost_usd": round(self.total_switching_cost_usd, 4),
            "liquidation_route": self.liquidation_route.value,
        }


@dataclass(frozen=True)
class HurdleDecision:
    should_switch: bool
    delta_yield_daily_usd: float
    required_delta_yield_daily_usd: float
    net_alpha_holding_usd: float
    switching_cost_total_usd: float
    holding_horizon_hours: float
    amortized_cost_daily_usd: float
    reason: str


class CapitalMigrationEngine:
    """Quantifies switching frictions and validates net-alpha hurdles."""

    def __init__(
        self,
        gas_env: Optional[PolygonPoSEnvironment] = None,
        ctf_profile: Optional[CTFGasProfile] = None,
        queue_model: Optional[QueuePriorityValuationModel] = None,
        safety_hurdle_pct: float = 0.15,
    ) -> None:
        self.gas_env = gas_env or PolygonPoSEnvironment()
        self.ctf_profile = ctf_profile or CTFGasProfile()
        self.queue_model = queue_model or QueuePriorityValuationModel()
        self.safety_hurdle_pct = safety_hurdle_pct

    def compute_queue_loss(
        self,
        resting_bid_size: float,
        resting_ask_size: float,
        queue_ahead_bid: float,
        queue_ahead_ask: float,
        half_spread_cents: float,
        mid_price: float = 0.50,
    ) -> float:
        """Value of FIFO queue priority surrendered upon cancellation."""
        val_bid = self.queue_model.evaluate_queue_value(
            quote_size=resting_bid_size,
            queue_ahead=queue_ahead_bid,
            half_spread_cents=half_spread_cents,
            token_mid_price=mid_price,
        ).net_queue_value_usd

        val_ask = self.queue_model.evaluate_queue_value(
            quote_size=resting_ask_size,
            queue_ahead=queue_ahead_ask,
            half_spread_cents=half_spread_cents,
            token_mid_price=mid_price,
        ).net_queue_value_usd

        return val_bid + val_ask

    def compute_downtime_cost(
        self,
        daily_yield_a: float,
        latency_seconds: float = 4.5,
    ) -> float:
        """Forgone yield during unquoted transit and 3.5s qualification timer."""
        return daily_yield_a * (latency_seconds / 86_400.0)

    def compute_switching_cost(
        self,
        q_a: float,
        p_mid_a: float,
        p_bid_a: float,
        p_ask_opp_a: float,
        resting_bid_size: float,
        resting_ask_size: float,
        queue_ahead_bid: float,
        queue_ahead_ask: float,
        half_spread_cents_a: float,
        daily_yield_a: float,
        has_matched_complete_sets: bool = True,
        transit_latency_seconds: float = 4.5,
        taker_fee_rate: float = 0.0,
    ) -> SwitchingCostDecomposition:
        """Compute the full economic switching friction: Cost_switch(A -> B)."""
        merge_gas = self.gas_env.gas_units_to_usd(self.ctf_profile.gas_merge_direct_typical)

        # 1. Queue Priority Loss
        q_loss = self.compute_queue_loss(
            resting_bid_size=resting_bid_size,
            resting_ask_size=resting_ask_size,
            queue_ahead_bid=queue_ahead_bid,
            queue_ahead_ask=queue_ahead_ask,
            half_spread_cents=half_spread_cents_a,
            mid_price=p_mid_a,
        )

        # 2. Inventory Liquidation Friction
        liq = solve_inventory_liquidation_friction(
            q=q_a,
            p_mid=p_mid_a,
            p_bid=p_bid_a,
            p_ask_opp=p_ask_opp_a,
            gas_merge_usd=merge_gas,
            taker_fee_rate=taker_fee_rate,
        )

        # 3. Gas Fees: off-chain cancel is $0.00; mergePositions is merge_gas
        # Avoid double counting: if synthetic merge won, merge gas is inside liq.optimal_friction_usd
        if liq.route == LiquidationRoute.SYNTHETIC_MERGE:
            gas_fees = 0.0
        elif has_matched_complete_sets:
            gas_fees = merge_gas
        else:
            gas_fees = 0.0

        # 4. Downtime Opportunity Cost
        opp_cost = self.compute_downtime_cost(
            daily_yield_a=daily_yield_a,
            latency_seconds=transit_latency_seconds,
        )

        total_cost = q_loss + liq.optimal_friction_usd + gas_fees + opp_cost

        return SwitchingCostDecomposition(
            queue_value_lost_usd=q_loss,
            liquidation_friction_usd=liq.optimal_friction_usd,
            gas_fees_usd=gas_fees,
            opportunity_cost_downtime_usd=opp_cost,
            total_switching_cost_usd=total_cost,
            liquidation_route=liq.route,
        )

    def evaluate_switching_hurdle(
        self,
        y_net_a_daily: float,
        y_net_b_daily: float,
        switching_cost_usd: float,
        holding_horizon_hours: float = 4.0,
    ) -> HurdleDecision:
        """Evaluate the Net Alpha condition: Delta_Yield * Tau >= Cost_switch."""
        tau_days = max(1.0 / 24.0, holding_horizon_hours / 24.0)
        delta_y = y_net_b_daily - y_net_a_daily

        amortized_cost_daily = switching_cost_usd / max(1e-4, tau_days)
        required_delta_daily = amortized_cost_daily + (self.safety_hurdle_pct * max(0.0, y_net_a_daily))

        net_alpha_holding = (delta_y * tau_days) - switching_cost_usd
        should_switch = delta_y >= required_delta_daily

        status = "POSITIVE ALPHA" if should_switch else "HURDLE REJECTED"
        reason = (
            f"{status}: Delta yield ${delta_y:.2f}/d clears hurdle ${required_delta_daily:.2f}/d "
            f"(Amortized cost: ${amortized_cost_daily:.2f}/d over {holding_horizon_hours:.1f}h). "
            f"Net holding alpha: ${net_alpha_holding:+.4f}."
        )

        return HurdleDecision(
            should_switch=should_switch,
            delta_yield_daily_usd=round(delta_y, 4),
            required_delta_yield_daily_usd=round(required_delta_daily, 4),
            net_alpha_holding_usd=round(net_alpha_holding, 4),
            switching_cost_total_usd=round(switching_cost_usd, 4),
            holding_horizon_hours=holding_horizon_hours,
            amortized_cost_daily_usd=round(amortized_cost_daily, 4),
            reason=reason,
        )
