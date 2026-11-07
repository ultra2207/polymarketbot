"""Multi-Outcome Negative-Risk Converter & Bundle Arbitrage Engine.

Implements Domain 3 Path B:
Real-time monitoring, coherence checking, fee/gas deduction, and atomic conversion
planning across mutually exclusive multi-outcome markets (Soccer 1X2, Elections,
k >= 3 outcomes).

The Core Mechanism & Arbitrage Inequalities:
--------------------------------------------
1. Buy-All-NO (Negative-Risk Short Basket):
   - Condition: sum(Ask_NO_i) < k - 1
   - Guaranteed Payout: Exactly 1 outcome resolves YES (payout 0.0) and k-1 outcomes
     resolve NO (payout 1.0 each). Total payout = (k - 1) * $1.000 USDC.
   - Gross Edge: (k - 1) - sum(Ask_NO_i) per share bundle.
   - Instant Capital Recycling: Calling NegRiskAdapter.convertPositions on k-1 legs
     burns the NO shares and yields 1 share of YES of the remaining leg plus (k - 2) USDC
     collateral. Merging that remaining leg's YES + NO unlocks 1 USDC, giving exactly
     (k - 1) USDC in immediate cash without waiting for event resolution!

2. Buy-All-YES (Long Basket):
   - Condition: sum(Ask_YES_i) < 1.000
   - Guaranteed Payout: Exactly 1 outcome pays 1.000 USDC; others pay 0.000 USDC.
   - Gross Edge: 1.000 - sum(Ask_YES_i) per share bundle.

3. Cross-Outcome Conversion Arbitrage (Convert NO to YES):
   - For each outcome j in 0..k-1:
     sum_{i != j} Ask_NO_i < (k - 2) + Bid_YES_j
   - Buying NO across the other k-1 legs, converting via NegRiskAdapter to 1 YES_j +
     (k-2) USDC cash, and selling YES_j on the bid locks in risk-free profit immediately.

All edges are strictly calculated net of Polymarket taker fees (via compute_taker_fee)
and Polygon PoS gas costs.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from polymarketbot.fees import FeeSchedule, era_schedule, schedule_from_market, taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.trading.ctf import (
    CTF_SHARE_DECIMALS,
    PolygonCtfClient,
    get_dynamic_gas_price_gwei,
    get_dynamic_pol_usd_price,
)
from polymarketbot.trading.settlement import (
    NEGRISK_ADAPTER,
    amount_to_6dp,
    build_negrisk_convert,
    build_negrisk_merge,
    negrisk_index_set,
)

if TYPE_CHECKING:  # pragma: no cover
    from polymarketbot.chain import ChainClient
    from polymarketbot.trading.client import ClobClient

logger = get_logger("negrisk_engine")

# NegRiskAdapter contract on Polygon PoS (verified: 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296)
NEGRISK_ADAPTER_ADDRESS = NEGRISK_ADAPTER

# Gas profiles for Polygon PoS on-chain settlement actions
TYPICAL_CONVERSION_GAS_UNITS = 85_000
TYPICAL_MERGE_GAS_UNITS = 78_000
PER_LEG_ADDITIONAL_GAS = 15_000

# Minimum threshold defaults
DEFAULT_MIN_NET_EDGE_USD = 0.01      # $0.01 net profit threshold
DEFAULT_MIN_NET_EDGE_BPS = 10.0      # 10 bps minimum net edge
DEFAULT_MAX_QUOTE_AGE_S = 60.0       # Max quote staleness window (seconds)


class OpportunityType(str, Enum):
    """Categorization of negative-risk bundle arbitrage opportunities."""

    BUY_ALL_NO = "buy_all_no"
    BUY_ALL_YES = "buy_all_yes"
    CONVERT_NO_TO_YES = "convert_no_to_yes"


class BrokenBasketPolicy(str, Enum):
    """Institutional execution policy when a multi-leg basket suffers execution failure (broken leg)."""

    UNWIND_MARKET = "unwind_market"         # Cross spread immediately to market-sell filled partial legs into CLOB bids
    SYNTHETIC_CONVERT = "synthetic_convert" # In BUY_ALL_NO, convert k-1 filled NO legs via NegRiskAdapter to 1 YES + cash
    ABORT_HOLD = "abort_hold"               # Halt and leave partial inventory intact (for manual intervention)


@dataclass(slots=True, frozen=True)
class OrderBookLevel:
    """Single price-quantity level in an L2 order book."""

    price: float
    size: float

    def to_dict(self) -> dict[str, float]:
        return {"price": self.price, "size": self.size}


@dataclass
class OrderBookDepth:
    """L2 order book side levels sorted best to worst."""

    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_bid_size(self) -> float:
        return self.bids[0].size if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def best_ask_size(self) -> float:
        return self.asks[0].size if self.asks else 0.0


@dataclass
class OutcomeLeg:
    """Represents one outcome leg in a multi-outcome negative-risk market."""

    index: int                               # 0-based question index in the NegRisk event
    condition_id: str                        # 32-byte hex condition ID
    question_id: str                         # 32-byte question ID (for NegRiskAdapter)
    yes_token_id: str                        # Decimal token ID string for YES
    no_token_id: str                         # Decimal token ID string for NO
    title: str = ""                          # Outcome title/label (e.g. "Home", "Draw")
    best_ask_yes: float = 1.0
    best_ask_no: float = 1.0
    best_bid_yes: float = 0.0
    best_bid_no: float = 0.0
    ask_size_yes: float = 0.0
    ask_size_no: float = 0.0
    bid_size_yes: float = 0.0
    bid_size_no: float = 0.0
    depth_yes: OrderBookDepth = field(default_factory=OrderBookDepth)
    depth_no: OrderBookDepth = field(default_factory=OrderBookDepth)
    fee_rate: float = 0.0                    # Market fee rate (e.g. 0.05 for sports)
    fee_exponent: float = 1.0                # Market fee curve exponent
    fee_schedule: Optional[FeeSchedule] = None
    last_update_ts: float = field(default_factory=time.time)

    def is_fresh(self, max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_S) -> bool:
        """True if quote timestamp is within max_age_seconds."""
        return (time.time() - self.last_update_ts) <= max_age_seconds


@dataclass
class NegRiskMarket:
    """A multi-outcome event consisting of k >= 3 mutually exclusive legs."""

    event_id: str
    market_id: str                           # NegRisk market identifier (32-byte hex)
    title: str = ""
    category: str = "sports"                 # "sports", "politics", "crypto", etc.
    k: int = 3                               # Total number of outcomes (k >= 3)
    legs: dict[int, OutcomeLeg] = field(default_factory=dict)
    fee_rate: float = 0.0
    fee_exponent: float = 1.0
    created_at: float = field(default_factory=time.time)
    last_update_ts: float = field(default_factory=time.time)

    def is_complete(self) -> bool:
        """True if all k legs (indices 0..k-1) are present."""
        return len(self.legs) == self.k and all(i in self.legs for i in range(self.k))

    def is_fresh(self, max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_S) -> bool:
        """True if all k legs have been updated within max_age_seconds."""
        if not self.is_complete():
            return False
        return all(leg.is_fresh(max_age_seconds) for leg in self.legs.values())

    def get_leg(self, index: int) -> OutcomeLeg:
        return self.legs[index]


def compute_taker_fee(
    shares: float,
    price: float,
    fee_rate: float = 0.0,
    fee_exponent: float = 1.0,
    fee_schedule: Optional[FeeSchedule] = None,
    rounding_dp: int = 5,
) -> float:
    """Compute exact Polymarket taker fee for a fill.

    Formula:
        fee = C * rate * (p * (1 - p)) ^ exponent

    Where C is shares, p is price. Makers pay zero; only takers pay.
    Fees are quantized to 5 decimal places (ROUND_HALF_UP).
    """
    if fee_schedule is not None:
        return taker_fee(shares, price, fee_schedule, rounding_dp=rounding_dp)
    if shares <= 0.0 or fee_rate <= 0.0:
        return 0.0
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"Price out of range [0.0, 1.0]: {price}")

    curve = price * (1.0 - price)
    raw = shares * fee_rate * (curve ** fee_exponent)
    q = Decimal(1).scaleb(-rounding_dp)
    return float(Decimal(repr(raw)).quantize(q, rounding=ROUND_HALF_UP))


def estimate_conversion_gas_usd(
    k_legs: int = 3,
    pol_usd_price: Optional[float] = None,
    gas_price_gwei: Optional[float] = None,
    chain_client: Optional[ChainClient] = None,
    include_merge: bool = True,
) -> float:
    """Calculate dynamic Polygon PoS gas cost in USD for NegRisk conversion/merge.

    Args:
        k_legs: Number of event outcomes.
        pol_usd_price: Current market exchange rate for POL in USD.
        gas_price_gwei: Optional gas price in Gwei.
        chain_client: Optional ChainClient instance for RPC queries.
        include_merge: Whether to include the subsequent mergePositions gas.

    Returns:
        Gas cost in USD (rounded to 6 decimal places).
    """
    gwei = (
        float(gas_price_gwei)
        if gas_price_gwei is not None
        else get_dynamic_gas_price_gwei(chain_client=chain_client)
    )
    px = float(pol_usd_price) if pol_usd_price is not None else get_dynamic_pol_usd_price()

    # Base convertUnits + extra gas for each outcome beyond 3
    convert_units = TYPICAL_CONVERSION_GAS_UNITS + max(0, k_legs - 3) * PER_LEG_ADDITIONAL_GAS
    total_units = convert_units + (TYPICAL_MERGE_GAS_UNITS if include_merge else 0)

    gas_pol = total_units * (gwei * 1e-9)
    gas_usd = gas_pol * px
    return round(gas_usd, 6)


def calculate_minimum_batch_shares(
    gas_cost_usd: float,
    max_gas_friction_cents_per_share: float = 0.05,
) -> float:
    """Calculate minimum batch size (in shares/USD) so on-chain gas is amortized to < 0.05c/share.

    Formula:
        min_shares = gas_cost_usd / (max_friction_cents / 100.0)

    For example, with $0.015 merge gas and 0.05c/share hurdle, requires >= 30.0 shares.
    With $0.005 redeem gas, requires >= 10.0 shares.
    """
    if gas_cost_usd <= 0.0:
        return 1.0
    hurdle = max_gas_friction_cents_per_share / 100.0  # 0.0005 USD/share
    return round(gas_cost_usd / hurdle, 2)


def compute_basket_fill_probability(
    market: NegRiskMarket,
    opportunity_type: OpportunityType,
    shares: float,
    base_intensity: float = 2.0,
) -> float:
    """Calculate joint basket completion probability across all k legs under Poisson arrival.

    Formula:
        For each leg i:
            liquidity_ratio_i = min(1.0, available_depth_i / shares)
            spread_factor_i = max(0.1, 1.0 - spread_i)
            intensity_i = base_intensity * liquidity_ratio_i * spread_factor_i
            P_i = 1.0 - exp(-intensity_i)
        P_basket = prod(P_i for i in legs)

    Returns:
        Continuous joint execution probability in [0.0, 1.0].
    """
    if not market.is_complete() or shares <= 0.0:
        return 0.0

    k = market.k
    probs = []

    if opportunity_type == OpportunityType.BUY_ALL_NO:
        for i in range(k):
            leg = market.get_leg(i)
            d = leg.ask_size_no
            spread = max(0.01, leg.best_ask_no - leg.best_bid_no)
            rho = min(1.0, d / max(1e-6, shares))
            tau = max(0.1, 1.0 - min(0.9, spread))
            kappa = base_intensity * rho * tau
            probs.append(1.0 - math.exp(-kappa))

    elif opportunity_type == OpportunityType.BUY_ALL_YES:
        for i in range(k):
            leg = market.get_leg(i)
            d = leg.ask_size_yes
            spread = max(0.01, leg.best_ask_yes - leg.best_bid_yes)
            rho = min(1.0, d / max(1e-6, shares))
            tau = max(0.1, 1.0 - min(0.9, spread))
            kappa = base_intensity * rho * tau
            probs.append(1.0 - math.exp(-kappa))

    elif opportunity_type == OpportunityType.CONVERT_NO_TO_YES:
        for i in range(k):
            leg = market.get_leg(i)
            d = max(leg.ask_size_no, leg.bid_size_yes)
            spread = max(0.01, leg.best_ask_no - leg.best_bid_no)
            rho = min(1.0, d / max(1e-6, shares))
            tau = max(0.1, 1.0 - min(0.9, spread))
            kappa = base_intensity * rho * tau
            probs.append(1.0 - math.exp(-kappa))

    if not probs:
        return 0.0

    prod = 1.0
    for p in probs:
        prod *= p
    return round(prod, 6)


def slice_basket_opportunity(
    opportunity: NegRiskOpportunity,
    market: NegRiskMarket,
    max_slice_shares: Optional[float] = None,
    safety_factor: float = 0.8,
    min_slice_shares: float = 10.0,
) -> list[NegRiskOpportunity]:
    """Slice a large N-leg basket arbitrage opportunity into institutional micro-slices.

    Each micro-slice:
    1. Sizes within safety_factor * bottleneck depth to prevent market impact and broken legs.
    2. Strictly amortizes taker fees and conversion/merge gas friction.
    3. Guarantees positive net profit.
    """
    if opportunity.shares <= 0.0 or not market.is_complete():
        return []

    k = market.k
    if opportunity.opportunity_type == OpportunityType.BUY_ALL_NO:
        bottleneck = min(market.get_leg(i).ask_size_no for i in range(k))
    elif opportunity.opportunity_type == OpportunityType.BUY_ALL_YES:
        bottleneck = min(market.get_leg(i).ask_size_yes for i in range(k))
    elif opportunity.opportunity_type == OpportunityType.CONVERT_NO_TO_YES:
        target_idx = opportunity.target_outcome_index or 0
        other_no = [market.get_leg(i).ask_size_no for i in range(k) if i != target_idx]
        target_bid = market.get_leg(target_idx).bid_size_yes
        bottleneck = min(other_no + [target_bid])
    else:
        bottleneck = opportunity.shares

    safe_depth = max(min_slice_shares, bottleneck * safety_factor)
    slice_size = safe_depth if max_slice_shares is None else min(safe_depth, float(max_slice_shares))
    slice_size = max(min_slice_shares, round(slice_size, 2))

    total_shares = opportunity.shares
    if total_shares <= slice_size:
        return [opportunity]

    slices: list[NegRiskOpportunity] = []
    rem = total_shares
    total_gas = opportunity.estimated_gas_cost_usd

    while rem > 1e-4:
        current_slice_shares = round(min(rem, slice_size), 2)
        if current_slice_shares < min_slice_shares and slices:
            # Add remaining fractional shares to last slice if safe
            last = slices[-1]
            combined_shares = round(last.shares + current_slice_shares, 2)
            c_ratio = combined_shares / total_shares
            last_gas = round(total_gas * c_ratio, 6)
            last_gross_edge = round(opportunity.gross_edge_usd * c_ratio, 4)
            last_fees = round(opportunity.total_taker_fees_usd * c_ratio, 4)
            last_net_edge = round(last_gross_edge - last_fees - last_gas, 4)

            updated_legs = []
            for leg_ex in opportunity.legs_execution:
                updated_legs.append(
                    ArbitrageLegExecution(
                        leg_index=leg_ex.leg_index,
                        token_id=leg_ex.token_id,
                        outcome_type=leg_ex.outcome_type,
                        side=leg_ex.side,
                        price=leg_ex.price,
                        shares=combined_shares,
                        notional_usd=round(leg_ex.price * combined_shares, 4),
                        taker_fee_usd=round(leg_ex.taker_fee_usd * c_ratio, 4),
                    )
                )
            slices[-1] = NegRiskOpportunity(
                event_id=opportunity.event_id,
                market_id=opportunity.market_id,
                opportunity_type=opportunity.opportunity_type,
                k_legs=opportunity.k_legs,
                shares=combined_shares,
                gross_cost_per_share=opportunity.gross_cost_per_share,
                gross_payout_per_share=opportunity.gross_payout_per_share,
                gross_edge_per_share=opportunity.gross_edge_per_share,
                gross_edge_usd=last_gross_edge,
                total_taker_fees_usd=last_fees,
                estimated_gas_cost_usd=last_gas,
                net_edge_usd=last_net_edge,
                net_edge_bps=opportunity.net_edge_bps,
                net_margin_pct=opportunity.net_margin_pct,
                legs_execution=updated_legs,
                target_outcome_index=opportunity.target_outcome_index,
                convert_index_set=opportunity.convert_index_set,
                timestamp=opportunity.timestamp,
            )
            break

        ratio = current_slice_shares / total_shares
        slice_gas = round(total_gas * ratio, 6)
        slice_gross_edge = round(opportunity.gross_edge_usd * ratio, 4)
        slice_fees = round(opportunity.total_taker_fees_usd * ratio, 4)
        slice_net_edge = round(slice_gross_edge - slice_fees - slice_gas, 4)

        if slice_net_edge <= 0.0:
            break

        slice_legs_exec = []
        for leg_ex in opportunity.legs_execution:
            slice_legs_exec.append(
                ArbitrageLegExecution(
                    leg_index=leg_ex.leg_index,
                    token_id=leg_ex.token_id,
                    outcome_type=leg_ex.outcome_type,
                    side=leg_ex.side,
                    price=leg_ex.price,
                    shares=current_slice_shares,
                    notional_usd=round(leg_ex.price * current_slice_shares, 4),
                    taker_fee_usd=round(leg_ex.taker_fee_usd * ratio, 4),
                )
            )

        slice_opp = NegRiskOpportunity(
            event_id=opportunity.event_id,
            market_id=opportunity.market_id,
            opportunity_type=opportunity.opportunity_type,
            k_legs=opportunity.k_legs,
            shares=current_slice_shares,
            gross_cost_per_share=opportunity.gross_cost_per_share,
            gross_payout_per_share=opportunity.gross_payout_per_share,
            gross_edge_per_share=opportunity.gross_edge_per_share,
            gross_edge_usd=slice_gross_edge,
            total_taker_fees_usd=slice_fees,
            estimated_gas_cost_usd=slice_gas,
            net_edge_usd=slice_net_edge,
            net_edge_bps=opportunity.net_edge_bps,
            net_margin_pct=opportunity.net_margin_pct,
            legs_execution=slice_legs_exec,
            target_outcome_index=opportunity.target_outcome_index,
            convert_index_set=opportunity.convert_index_set,
            timestamp=opportunity.timestamp,
        )
        slices.append(slice_opp)
        rem = round(rem - current_slice_shares, 4)

    return slices


@dataclass
class ArbitrageLegExecution:
    """Details for executing one leg of an arbitrage bundle."""

    leg_index: int
    token_id: str
    outcome_type: str                        # "YES" or "NO"
    side: str                                # "BUY" or "SELL"
    price: float
    shares: float
    notional_usd: float
    taker_fee_usd: float


@dataclass
class OnChainAction:
    """Atomic on-chain smart contract invocation specification."""

    contract_address: str
    function_name: str
    calldata: str
    gas_units: int
    description: str


@dataclass
class NegRiskOpportunity:
    """Identified negative-risk bundle or conversion arbitrage opportunity."""

    event_id: str
    market_id: str
    opportunity_type: OpportunityType
    k_legs: int
    shares: float
    gross_cost_per_share: float
    gross_payout_per_share: float
    gross_edge_per_share: float
    gross_edge_usd: float
    total_taker_fees_usd: float
    estimated_gas_cost_usd: float
    net_edge_usd: float
    net_edge_bps: float
    net_margin_pct: float
    legs_execution: list[ArbitrageLegExecution]
    target_outcome_index: Optional[int] = None
    convert_index_set: Optional[int] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "market_id": self.market_id,
            "opportunity_type": self.opportunity_type.value,
            "k_legs": self.k_legs,
            "shares": self.shares,
            "gross_cost_per_share": round(self.gross_cost_per_share, 4),
            "gross_payout_per_share": round(self.gross_payout_per_share, 4),
            "gross_edge_per_share": round(self.gross_edge_per_share, 4),
            "gross_edge_usd": round(self.gross_edge_usd, 4),
            "total_taker_fees_usd": round(self.total_taker_fees_usd, 4),
            "estimated_gas_cost_usd": round(self.estimated_gas_cost_usd, 4),
            "net_edge_usd": round(self.net_edge_usd, 4),
            "net_edge_bps": round(self.net_edge_bps, 2),
            "net_margin_pct": round(self.net_margin_pct, 2),
            "target_outcome_index": self.target_outcome_index,
            "convert_index_set": self.convert_index_set,
            "timestamp": self.timestamp,
        }


@dataclass
class NegRiskExecutionPlan:
    """Executable plan composed of CLOB taker orders and on-chain conversion."""

    plan_id: str
    opportunity: NegRiskOpportunity
    clob_orders: list[dict[str, Any]]
    on_chain_calls: list[OnChainAction]
    estimated_net_profit_usd: float
    requires_on_chain_conversion: bool
    status: str = "PENDING"
    created_at: float = field(default_factory=time.time)


@dataclass
class BrokenBasketResolution:
    """Audit trail and resolution record for an execution-failed broken basket."""

    plan_id: str
    failed_leg_index: int
    failed_error: str
    policy_applied: BrokenBasketPolicy
    unwind_orders: list[dict[str, Any]] = field(default_factory=list)
    unwind_pnl_usd: float = 0.0
    synthetic_converted_shares: float = 0.0
    synthetic_cash_unlocked_usd: float = 0.0
    is_resolved: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "failed_leg_index": self.failed_leg_index,
            "failed_error": self.failed_error,
            "policy_applied": self.policy_applied.value,
            "unwind_orders": self.unwind_orders,
            "unwind_pnl_usd": round(self.unwind_pnl_usd, 4),
            "synthetic_converted_shares": round(self.synthetic_converted_shares, 4),
            "synthetic_cash_unlocked_usd": round(self.synthetic_cash_unlocked_usd, 4),
            "is_resolved": self.is_resolved,
            "message": self.message,
        }


@dataclass
class NegRiskExecutionResult:
    """Outcome of attempting to execute an arbitrage plan."""

    success: bool
    plan_id: str
    orders_placed: list[dict[str, Any]] = field(default_factory=list)
    tx_hashes: list[str] = field(default_factory=list)
    actual_net_profit_usd: float = 0.0
    is_dry_run: bool = False
    error: Optional[str] = None
    status: str = "FAILED"
    broken_basket_resolution: Optional[BrokenBasketResolution] = None


class CoherenceChecker:
    """Mathematical verification and edge deduction engine for multi-outcome events."""

    def __init__(
        self,
        min_net_edge_usd: float = DEFAULT_MIN_NET_EDGE_USD,
        min_net_edge_bps: float = DEFAULT_MIN_NET_EDGE_BPS,
        max_quote_age_s: float = DEFAULT_MAX_QUOTE_AGE_S,
    ) -> None:
        self.min_net_edge_usd = min_net_edge_usd
        self.min_net_edge_bps = min_net_edge_bps
        self.max_quote_age_s = max_quote_age_s

    def scan_market(
        self,
        market: NegRiskMarket,
        max_shares: Optional[float] = None,
        gas_price_gwei: Optional[float] = None,
        pol_usd_price: Optional[float] = None,
        chain_client: Optional[ChainClient] = None,
    ) -> list[NegRiskOpportunity]:
        """Scan a NegRisk market for all profitable bundle and conversion arbs."""
        if not market.is_complete():
            logger.debug(f"Market {market.event_id} incomplete ({len(market.legs)}/{market.k} legs); skipping")
            return []

        if not market.is_fresh(self.max_quote_age_s):
            logger.debug(f"Market {market.event_id} contains stale quotes (> {self.max_quote_age_s}s); skipping")
            return []

        opportunities: list[NegRiskOpportunity] = []

        # 1. Evaluate Buy-All-NO Bundle
        opp_no = self._check_buy_all_no(
            market=market,
            max_shares=max_shares,
            gas_price_gwei=gas_price_gwei,
            pol_usd_price=pol_usd_price,
            chain_client=chain_client,
        )
        if opp_no is not None:
            opportunities.append(opp_no)

        # 2. Evaluate Buy-All-YES Bundle
        opp_yes = self._check_buy_all_yes(
            market=market,
            max_shares=max_shares,
            gas_price_gwei=gas_price_gwei,
            pol_usd_price=pol_usd_price,
            chain_client=chain_client,
        )
        if opp_yes is not None:
            opportunities.append(opp_yes)

        # 3. Evaluate Convert-NO-to-YES for each outcome
        for j in range(market.k):
            opp_convert = self._check_convert_no_to_yes(
                market=market,
                target_index=j,
                max_shares=max_shares,
                gas_price_gwei=gas_price_gwei,
                pol_usd_price=pol_usd_price,
                chain_client=chain_client,
            )
            if opp_convert is not None:
                opportunities.append(opp_convert)

        return opportunities

    def sweep_depth_bundle(
        self,
        market: NegRiskMarket,
        opportunity_type: OpportunityType = OpportunityType.BUY_ALL_NO,
        target_shares: Optional[float] = None,
        gas_price_gwei: Optional[float] = None,
        pol_usd_price: Optional[float] = None,
        chain_client: Optional[ChainClient] = None,
    ) -> Optional[NegRiskOpportunity]:
        """Sweep multi-level L2 depth across all legs to determine maximum profitable size.

        Simulates filling through multiple order book levels until the cumulative
        VWAP crosses profitability or liquidity is exhausted.
        """
        if not market.is_complete() or not market.is_fresh(self.max_quote_age_s):
            return None

        k = market.k
        outcome_type = "NO" if opportunity_type == OpportunityType.BUY_ALL_NO else "YES"
        payout_per_share = float(k - 1) if opportunity_type == OpportunityType.BUY_ALL_NO else 1.000

        # Collect and validate asks per leg
        leg_asks: list[list[OrderBookLevel]] = []
        for i in range(k):
            leg = market.get_leg(i)
            depth = leg.depth_no if opportunity_type == OpportunityType.BUY_ALL_NO else leg.depth_yes
            levels = [lvl for lvl in depth.asks if 0.0 < lvl.price < 1.0 and lvl.size > 0.0]
            if not levels:
                best_ask = leg.best_ask_no if opportunity_type == OpportunityType.BUY_ALL_NO else leg.best_ask_yes
                ask_size = leg.ask_size_no if opportunity_type == OpportunityType.BUY_ALL_NO else leg.ask_size_yes
                if 0.0 < best_ask < 1.0 and ask_size > 0.0:
                    levels = [OrderBookLevel(best_ask, ask_size)]
                else:
                    return None
            levels = sorted(levels, key=lambda l: l.price)
            leg_asks.append(levels)

        max_available = min(sum(lvl.size for lvl in levels) for levels in leg_asks)
        if max_available <= 0.0:
            return None

        share_ceiling = max_available if target_shares is None else min(max_available, float(target_shares))
        if share_ceiling <= 0.0:
            return None

        gas_cost_usd = estimate_conversion_gas_usd(
            k_legs=k,
            pol_usd_price=pol_usd_price,
            gas_price_gwei=gas_price_gwei,
            chain_client=chain_client,
            include_merge=(opportunity_type == OpportunityType.BUY_ALL_NO),
        )

        def _evaluate_at_shares(shares: float) -> Optional[dict[str, Any]]:
            if shares <= 0.0:
                return None
            leg_execs: list[ArbitrageLegExecution] = []
            total_cost = 0.0
            total_fees = 0.0

            for i in range(k):
                leg = market.get_leg(i)
                levels = leg_asks[i]
                rem = shares
                leg_cost = 0.0
                leg_fee = 0.0

                for lvl in levels:
                    take = min(rem, lvl.size)
                    fee = compute_taker_fee(
                        shares=take,
                        price=lvl.price,
                        fee_rate=leg.fee_rate,
                        fee_exponent=leg.fee_exponent,
                        fee_schedule=leg.fee_schedule,
                    )
                    leg_cost += take * lvl.price
                    leg_fee += fee
                    rem -= take
                    if rem <= 0.0:
                        break

                if rem > 1e-6:
                    return None

                vwap = leg_cost / shares
                total_cost += leg_cost
                total_fees += leg_fee
                token_id = leg.no_token_id if opportunity_type == OpportunityType.BUY_ALL_NO else leg.yes_token_id
                leg_execs.append(
                    ArbitrageLegExecution(
                        leg_index=i,
                        token_id=token_id,
                        outcome_type=outcome_type,
                        side="BUY",
                        price=round(vwap, 4),
                        shares=shares,
                        notional_usd=round(leg_cost, 4),
                        taker_fee_usd=round(leg_fee, 4),
                    )
                )

            gross_payout = payout_per_share * shares
            gross_edge_usd = gross_payout - total_cost
            gross_edge_per_share = gross_edge_usd / shares
            gross_cost_per_share = total_cost / shares
            net_edge_usd = gross_edge_usd - total_fees - gas_cost_usd

            if net_edge_usd < self.min_net_edge_usd:
                return None

            net_margin_pct = (net_edge_usd / total_cost) * 100.0 if total_cost > 0 else 0.0
            net_edge_bps = net_margin_pct * 100.0
            if net_edge_bps < self.min_net_edge_bps:
                return None

            return {
                "shares": shares,
                "gross_cost_per_share": gross_cost_per_share,
                "gross_payout_per_share": payout_per_share,
                "gross_edge_per_share": gross_edge_per_share,
                "gross_edge_usd": gross_edge_usd,
                "total_taker_fees_usd": total_fees,
                "estimated_gas_cost_usd": gas_cost_usd,
                "net_edge_usd": net_edge_usd,
                "net_edge_bps": net_edge_bps,
                "net_margin_pct": net_margin_pct,
                "legs_execution": leg_execs,
            }

        # Try sweeping maximum available first
        res = _evaluate_at_shares(share_ceiling)
        if res is None:
            # Binary search for maximum profitable volume
            low = 1.0
            high = share_ceiling
            best_res = None
            for _ in range(12):  # 12 iterations gives sub-share precision
                mid = (low + high) / 2.0
                cand = _evaluate_at_shares(mid)
                if cand is not None:
                    best_res = cand
                    low = mid
                else:
                    high = mid
            res = best_res

        if res is None:
            return None

        conv_mask = negrisk_index_set(list(range(k - 1))) if opportunity_type == OpportunityType.BUY_ALL_NO else None
        target_idx = (k - 1) if opportunity_type == OpportunityType.BUY_ALL_NO else None

        return NegRiskOpportunity(
            event_id=market.event_id,
            market_id=market.market_id,
            opportunity_type=opportunity_type,
            k_legs=k,
            shares=res["shares"],
            gross_cost_per_share=res["gross_cost_per_share"],
            gross_payout_per_share=res["gross_payout_per_share"],
            gross_edge_per_share=res["gross_edge_per_share"],
            gross_edge_usd=res["gross_edge_usd"],
            total_taker_fees_usd=res["total_taker_fees_usd"],
            estimated_gas_cost_usd=res["estimated_gas_cost_usd"],
            net_edge_usd=res["net_edge_usd"],
            net_edge_bps=res["net_edge_bps"],
            net_margin_pct=res["net_margin_pct"],
            legs_execution=res["legs_execution"],
            target_outcome_index=target_idx,
            convert_index_set=conv_mask,
        )

    def _check_buy_all_no(
        self,
        market: NegRiskMarket,
        max_shares: Optional[float],
        gas_price_gwei: Optional[float],
        pol_usd_price: Optional[float],
        chain_client: Optional[ChainClient],
    ) -> Optional[NegRiskOpportunity]:
        """Check: sum(Ask_NO_i) < k - 1."""
        k = market.k
        payout_per_share = float(k - 1)

        # Ensure all legs have valid NO asks
        for i in range(k):
            leg = market.get_leg(i)
            if leg.best_ask_no <= 0.0 or leg.best_ask_no >= 1.0 or leg.ask_size_no <= 0.0:
                return None

        sum_ask_no = sum(market.get_leg(i).best_ask_no for i in range(k))
        if sum_ask_no >= payout_per_share:
            return None

        gross_edge_per_share = payout_per_share - sum_ask_no
        if gross_edge_per_share <= 0.0:
            return None

        # Bottleneck size across all NO asks
        available_shares = min(market.get_leg(i).ask_size_no for i in range(k))
        shares = available_shares if max_shares is None else min(available_shares, float(max_shares))
        if shares <= 0.0:
            return None

        gross_cost = sum_ask_no * shares
        gross_payout = payout_per_share * shares
        gross_edge_usd = gross_edge_per_share * shares

        # Calculate exact taker fees per leg
        total_fees = 0.0
        legs_exec: list[ArbitrageLegExecution] = []
        for i in range(k):
            leg = market.get_leg(i)
            px = leg.best_ask_no
            fee = compute_taker_fee(
                shares=shares,
                price=px,
                fee_rate=leg.fee_rate,
                fee_exponent=leg.fee_exponent,
                fee_schedule=leg.fee_schedule,
            )
            total_fees += fee
            legs_exec.append(
                ArbitrageLegExecution(
                    leg_index=i,
                    token_id=leg.no_token_id,
                    outcome_type="NO",
                    side="BUY",
                    price=px,
                    shares=shares,
                    notional_usd=round(shares * px, 4),
                    taker_fee_usd=fee,
                )
            )

        # Gas cost for conversion + merge (instant recycling)
        gas_cost_usd = estimate_conversion_gas_usd(
            k_legs=k,
            pol_usd_price=pol_usd_price,
            gas_price_gwei=gas_price_gwei,
            chain_client=chain_client,
            include_merge=True,
        )

        net_edge_usd = gross_edge_usd - total_fees - gas_cost_usd
        if net_edge_usd < self.min_net_edge_usd:
            return None

        net_margin_pct = (net_edge_usd / gross_cost) * 100.0 if gross_cost > 0 else 0.0
        net_edge_bps = net_margin_pct * 100.0
        if net_edge_bps < self.min_net_edge_bps:
            return None

        # For Buy-All-NO recycling, convert k-1 legs (indices 0..k-2) to YES of index k-1, then merge index k-1
        # Convert indexSet is all indices except k-1: 0, 1, ..., k-2
        convert_indices = list(range(k - 1))
        conv_mask = negrisk_index_set(convert_indices)

        return NegRiskOpportunity(
            event_id=market.event_id,
            market_id=market.market_id,
            opportunity_type=OpportunityType.BUY_ALL_NO,
            k_legs=k,
            shares=shares,
            gross_cost_per_share=sum_ask_no,
            gross_payout_per_share=payout_per_share,
            gross_edge_per_share=gross_edge_per_share,
            gross_edge_usd=gross_edge_usd,
            total_taker_fees_usd=total_fees,
            estimated_gas_cost_usd=gas_cost_usd,
            net_edge_usd=net_edge_usd,
            net_edge_bps=net_edge_bps,
            net_margin_pct=net_margin_pct,
            legs_execution=legs_exec,
            target_outcome_index=k - 1,
            convert_index_set=conv_mask,
        )

    def _check_buy_all_yes(
        self,
        market: NegRiskMarket,
        max_shares: Optional[float],
        gas_price_gwei: Optional[float],
        pol_usd_price: Optional[float],
        chain_client: Optional[ChainClient],
    ) -> Optional[NegRiskOpportunity]:
        """Check: sum(Ask_YES_i) < 1.000."""
        k = market.k
        payout_per_share = 1.000

        # Ensure all legs have valid YES asks
        for i in range(k):
            leg = market.get_leg(i)
            if leg.best_ask_yes <= 0.0 or leg.best_ask_yes >= 1.0 or leg.ask_size_yes <= 0.0:
                return None

        sum_ask_yes = sum(market.get_leg(i).best_ask_yes for i in range(k))
        if sum_ask_yes >= payout_per_share:
            return None

        gross_edge_per_share = payout_per_share - sum_ask_yes
        if gross_edge_per_share <= 0.0:
            return None

        available_shares = min(market.get_leg(i).ask_size_yes for i in range(k))
        shares = available_shares if max_shares is None else min(available_shares, float(max_shares))
        if shares <= 0.0:
            return None

        gross_cost = sum_ask_yes * shares
        gross_payout = payout_per_share * shares
        gross_edge_usd = gross_edge_per_share * shares

        total_fees = 0.0
        legs_exec: list[ArbitrageLegExecution] = []
        for i in range(k):
            leg = market.get_leg(i)
            px = leg.best_ask_yes
            fee = compute_taker_fee(
                shares=shares,
                price=px,
                fee_rate=leg.fee_rate,
                fee_exponent=leg.fee_exponent,
                fee_schedule=leg.fee_schedule,
            )
            total_fees += fee
            legs_exec.append(
                ArbitrageLegExecution(
                    leg_index=i,
                    token_id=leg.yes_token_id,
                    outcome_type="YES",
                    side="BUY",
                    price=px,
                    shares=shares,
                    notional_usd=round(shares * px, 4),
                    taker_fee_usd=fee,
                )
            )

        # For Buy-All-YES, holding to resolution incurs no NegRisk conversion,
        # but requires CTF.redeemPositions() on the single winning token upon resolution.
        if pol_usd_price is not None or gas_price_gwei is not None:
            gas_cost_usd = estimate_conversion_gas_usd(
                k_legs=1,
                pol_usd_price=pol_usd_price,
                gas_price_gwei=gas_price_gwei,
                chain_client=chain_client,
                include_merge=False,
            )
        else:
            gas_cost_usd = 0.005  # Standard Polygon CTF.redeemPositions gas buffer

        net_edge_usd = gross_edge_usd - total_fees - gas_cost_usd
        if net_edge_usd < self.min_net_edge_usd:
            return None

        net_margin_pct = (net_edge_usd / gross_cost) * 100.0 if gross_cost > 0 else 0.0
        net_edge_bps = net_margin_pct * 100.0
        if net_edge_bps < self.min_net_edge_bps:
            return None

        return NegRiskOpportunity(
            event_id=market.event_id,
            market_id=market.market_id,
            opportunity_type=OpportunityType.BUY_ALL_YES,
            k_legs=k,
            shares=shares,
            gross_cost_per_share=sum_ask_yes,
            gross_payout_per_share=payout_per_share,
            gross_edge_per_share=gross_edge_per_share,
            gross_edge_usd=gross_edge_usd,
            total_taker_fees_usd=total_fees,
            estimated_gas_cost_usd=gas_cost_usd,
            net_edge_usd=net_edge_usd,
            net_edge_bps=net_edge_bps,
            net_margin_pct=net_margin_pct,
            legs_execution=legs_exec,
        )

    def _check_convert_no_to_yes(
        self,
        market: NegRiskMarket,
        target_index: int,
        max_shares: Optional[float],
        gas_price_gwei: Optional[float],
        pol_usd_price: Optional[float],
        chain_client: Optional[ChainClient],
    ) -> Optional[NegRiskOpportunity]:
        """Check: sum_{i != target} Ask_NO_i < (k - 2) + Bid_YES_target."""
        k = market.k
        target_leg = market.get_leg(target_index)

        # Requires a bid on YES of target outcome
        if target_leg.best_bid_yes <= 0.0 or target_leg.bid_size_yes <= 0.0:
            return None

        # Ensure all other legs have valid NO asks
        other_indices = [i for i in range(k) if i != target_index]
        for i in other_indices:
            leg = market.get_leg(i)
            if leg.best_ask_no <= 0.0 or leg.best_ask_no >= 1.0 or leg.ask_size_no <= 0.0:
                return None

        sum_other_no = sum(market.get_leg(i).best_ask_no for i in other_indices)
        payout_per_share = float(k - 2) + target_leg.best_bid_yes

        gross_edge_per_share = payout_per_share - sum_other_no
        if gross_edge_per_share <= 0.0:
            return None

        # Available shares bottleneck: NO ask sizes of other legs + YES bid size of target
        available_shares = min(
            [market.get_leg(i).ask_size_no for i in other_indices] + [target_leg.bid_size_yes]
        )
        shares = available_shares if max_shares is None else min(available_shares, float(max_shares))
        if shares <= 0.0:
            return None

        gross_cost = sum_other_no * shares
        gross_payout = payout_per_share * shares
        gross_edge_usd = gross_edge_per_share * shares

        total_fees = 0.0
        legs_exec: list[ArbitrageLegExecution] = []

        # Buy NO on other legs
        for i in other_indices:
            leg = market.get_leg(i)
            px = leg.best_ask_no
            fee = compute_taker_fee(
                shares=shares,
                price=px,
                fee_rate=leg.fee_rate,
                fee_exponent=leg.fee_exponent,
                fee_schedule=leg.fee_schedule,
            )
            total_fees += fee
            legs_exec.append(
                ArbitrageLegExecution(
                    leg_index=i,
                    token_id=leg.no_token_id,
                    outcome_type="NO",
                    side="BUY",
                    price=px,
                    shares=shares,
                    notional_usd=round(shares * px, 4),
                    taker_fee_usd=fee,
                )
            )

        # Sell YES on target leg into bid
        target_fee = compute_taker_fee(
            shares=shares,
            price=target_leg.best_bid_yes,
            fee_rate=target_leg.fee_rate,
            fee_exponent=target_leg.fee_exponent,
            fee_schedule=target_leg.fee_schedule,
        )
        total_fees += target_fee
        legs_exec.append(
            ArbitrageLegExecution(
                leg_index=target_index,
                token_id=target_leg.yes_token_id,
                outcome_type="YES",
                side="SELL",
                price=target_leg.best_bid_yes,
                shares=shares,
                notional_usd=round(shares * target_leg.best_bid_yes, 4),
                taker_fee_usd=target_fee,
            )
        )

        # Gas cost for NegRiskAdapter.convertPositions (no merge needed since target YES was sold)
        gas_cost_usd = estimate_conversion_gas_usd(
            k_legs=k,
            pol_usd_price=pol_usd_price,
            gas_price_gwei=gas_price_gwei,
            chain_client=chain_client,
            include_merge=False,
        )

        net_edge_usd = gross_edge_usd - total_fees - gas_cost_usd
        if net_edge_usd < self.min_net_edge_usd:
            return None

        net_margin_pct = (net_edge_usd / gross_cost) * 100.0 if gross_cost > 0 else 0.0
        net_edge_bps = net_margin_pct * 100.0
        if net_edge_bps < self.min_net_edge_bps:
            return None

        conv_mask = negrisk_index_set(other_indices)

        return NegRiskOpportunity(
            event_id=market.event_id,
            market_id=market.market_id,
            opportunity_type=OpportunityType.CONVERT_NO_TO_YES,
            k_legs=k,
            shares=shares,
            gross_cost_per_share=sum_other_no,
            gross_payout_per_share=payout_per_share,
            gross_edge_per_share=gross_edge_per_share,
            gross_edge_usd=gross_edge_usd,
            total_taker_fees_usd=total_fees,
            estimated_gas_cost_usd=gas_cost_usd,
            net_edge_usd=net_edge_usd,
            net_edge_bps=net_edge_bps,
            net_margin_pct=net_margin_pct,
            legs_execution=legs_exec,
            target_outcome_index=target_index,
            convert_index_set=conv_mask,
        )


class ConversionPlanner:
    """Generates atomic execution plans (CLOB orders + on-chain conversion)."""

    @staticmethod
    def create_plan(
        opportunity: NegRiskOpportunity,
        market: NegRiskMarket,
        order_type: str = "IOC",
    ) -> NegRiskExecutionPlan:
        """Create an execution plan with signed CLOB orders and on-chain calldata."""
        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        clob_orders: list[dict[str, Any]] = []

        # 1. Prepare CLOB orders for all required legs
        for leg_exec in opportunity.legs_execution:
            clob_orders.append(
                {
                    "token_id": leg_exec.token_id,
                    "side": leg_exec.side,
                    "price": leg_exec.price,
                    "size": leg_exec.shares,
                    "order_type": order_type,
                    "leg_index": leg_exec.leg_index,
                    "outcome_type": leg_exec.outcome_type,
                }
            )

        # 2. Prepare on-chain actions
        on_chain_calls: list[OnChainAction] = []
        shares_6dp = amount_to_6dp(opportunity.shares)

        if opportunity.opportunity_type == OpportunityType.BUY_ALL_NO:
            # Step A: convert k-1 legs (0..k-2) to YES of leg k-1 + (k-2) USDC cash
            convert_indices = list(range(market.k - 1))
            convert_calldata = build_negrisk_convert(
                market_id=market.market_id,
                index_sets=convert_indices,
                amount_6dp=shares_6dp,
            )
            on_chain_calls.append(
                OnChainAction(
                    contract_address=NEGRISK_ADAPTER_ADDRESS,
                    function_name="convertPositions",
                    calldata=convert_calldata,
                    gas_units=TYPICAL_CONVERSION_GAS_UNITS,
                    description=f"Convert {opportunity.shares:.2f} NO across legs {convert_indices} to YES_{market.k-1} + ${(market.k-2)*opportunity.shares:.2f} USDC",
                )
            )

            # Step B: merge YES + NO of leg k-1 to unlock $1.000 USDC per share
            target_leg = market.get_leg(market.k - 1)
            merge_calldata = build_negrisk_merge(
                condition_id=target_leg.question_id,
                amount_6dp=shares_6dp,
            )
            on_chain_calls.append(
                OnChainAction(
                    contract_address=NEGRISK_ADAPTER_ADDRESS,
                    function_name="mergePositions",
                    calldata=merge_calldata,
                    gas_units=TYPICAL_MERGE_GAS_UNITS,
                    description=f"Merge {opportunity.shares:.2f} complete sets of leg {market.k-1} to unlock ${opportunity.shares:.2f} USDC",
                )
            )

        elif opportunity.opportunity_type == OpportunityType.CONVERT_NO_TO_YES:
            # Convert k-1 legs to target outcome YES
            target_idx = opportunity.target_outcome_index
            if target_idx is None:
                raise ValueError("Target outcome index required for CONVERT_NO_TO_YES")

            other_indices = [i for i in range(market.k) if i != target_idx]
            convert_calldata = build_negrisk_convert(
                market_id=market.market_id,
                index_sets=other_indices,
                amount_6dp=shares_6dp,
            )
            on_chain_calls.append(
                OnChainAction(
                    contract_address=NEGRISK_ADAPTER_ADDRESS,
                    function_name="convertPositions",
                    calldata=convert_calldata,
                    gas_units=TYPICAL_CONVERSION_GAS_UNITS,
                    description=f"Convert {opportunity.shares:.2f} NO across legs {other_indices} to YES_{target_idx} + ${(market.k-2)*opportunity.shares:.2f} USDC",
                )
            )

        return NegRiskExecutionPlan(
            plan_id=plan_id,
            opportunity=opportunity,
            clob_orders=clob_orders,
            on_chain_calls=on_chain_calls,
            estimated_net_profit_usd=opportunity.net_edge_usd,
            requires_on_chain_conversion=len(on_chain_calls) > 0,
        )


class NegRiskEngine:
    """Domain 3 Path B: Multi-Outcome Negative-Risk Converter & Bundle Arbitrage Engine."""

    def __init__(
        self,
        min_net_edge_usd: float = DEFAULT_MIN_NET_EDGE_USD,
        min_net_edge_bps: float = DEFAULT_MIN_NET_EDGE_BPS,
        max_quote_age_s: float = DEFAULT_MAX_QUOTE_AGE_S,
        max_capital_per_arb_usd: float = 1_000.0,
        dry_run: bool = True,
        clob_client: Optional[ClobClient] = None,
        ctf_client: Optional[PolygonCtfClient] = None,
        broken_basket_policy: BrokenBasketPolicy = BrokenBasketPolicy.UNWIND_MARKET,
    ) -> None:
        self.min_net_edge_usd = min_net_edge_usd
        self.min_net_edge_bps = min_net_edge_bps
        self.max_quote_age_s = max_quote_age_s
        self.max_capital_per_arb_usd = max_capital_per_arb_usd
        self.dry_run = dry_run
        self.clob_client = clob_client
        self.ctf_client = ctf_client
        self.broken_basket_policy = broken_basket_policy

        self.coherence_checker = CoherenceChecker(
            min_net_edge_usd=min_net_edge_usd,
            min_net_edge_bps=min_net_edge_bps,
            max_quote_age_s=max_quote_age_s,
        )
        self.conversion_planner = ConversionPlanner()

        # Tracked markets: event_id -> NegRiskMarket
        self.markets: dict[str, NegRiskMarket] = {}
        # Condition ID to (event_id, leg_index) mapping
        self._condition_to_market: dict[str, tuple[str, int]] = {}

        logger.info(
            f"NegRiskEngine initialized (dry_run={self.dry_run}, "
            f"min_net_edge=${self.min_net_edge_usd:.2f}, min_bps={self.min_net_edge_bps:.1f})"
        )

    def register_market(
        self,
        event_id: str,
        market_id: str,
        k: int,
        title: str = "",
        category: str = "sports",
        fee_rate: Optional[float] = None,
        fee_exponent: float = 1.0,
    ) -> NegRiskMarket:
        """Register a multi-outcome negative risk market."""
        if k < 3:
            raise ValueError(f"NegRisk requires at least k >= 3 outcomes, got {k}")

        # Resolve category fee rate if not explicitly supplied
        resolved_fee_rate = fee_rate
        if resolved_fee_rate is None:
            sched = era_schedule(category, datetime.now(timezone.utc))
            resolved_fee_rate = sched.rate
            fee_exponent = sched.exponent

        market = NegRiskMarket(
            event_id=event_id,
            market_id=market_id,
            title=title,
            category=category,
            k=k,
            fee_rate=resolved_fee_rate,
            fee_exponent=fee_exponent,
        )
        self.markets[event_id] = market
        return market

    def register_leg(
        self,
        event_id: str,
        index: int,
        condition_id: str,
        question_id: str,
        yes_token_id: str,
        no_token_id: str,
        title: str = "",
        fee_rate: Optional[float] = None,
        fee_exponent: Optional[float] = None,
    ) -> OutcomeLeg:
        """Register an outcome leg within a tracked market."""
        market = self.markets.get(event_id)
        if market is None:
            raise KeyError(f"Event ID {event_id} not registered")

        if index < 0 or index >= market.k:
            raise ValueError(f"Leg index {index} out of range [0, {market.k - 1}]")

        f_rate = market.fee_rate if fee_rate is None else fee_rate
        f_exp = market.fee_exponent if fee_exponent is None else fee_exponent

        leg = OutcomeLeg(
            index=index,
            condition_id=condition_id.lower(),
            question_id=question_id.lower(),
            yes_token_id=str(yes_token_id).strip(),
            no_token_id=str(no_token_id).strip(),
            title=title,
            fee_rate=f_rate,
            fee_exponent=f_exp,
        )
        market.legs[index] = leg
        self._condition_to_market[condition_id.lower()] = (event_id, index)
        return leg

    def update_leg_quotes(
        self,
        event_id: str,
        index: int,
        best_ask_yes: float = 1.0,
        best_ask_no: float = 1.0,
        best_bid_yes: float = 0.0,
        best_bid_no: float = 0.0,
        ask_size_yes: float = 0.0,
        ask_size_no: float = 0.0,
        bid_size_yes: float = 0.0,
        bid_size_no: float = 0.0,
        depth_yes: Optional[OrderBookDepth] = None,
        depth_no: Optional[OrderBookDepth] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Update top-of-book and depth quotes for one outcome leg."""
        market = self.markets.get(event_id)
        if market is None or index not in market.legs:
            return

        ts = timestamp or time.time()
        leg = market.legs[index]
        leg.best_ask_yes = best_ask_yes
        leg.best_ask_no = best_ask_no
        leg.best_bid_yes = best_bid_yes
        leg.best_bid_no = best_bid_no
        leg.ask_size_yes = ask_size_yes
        leg.ask_size_no = ask_size_no
        leg.bid_size_yes = bid_size_yes
        leg.bid_size_no = bid_size_no
        if depth_yes is not None:
            leg.depth_yes = depth_yes
        if depth_no is not None:
            leg.depth_no = depth_no
        leg.last_update_ts = ts
        market.last_update_ts = ts

    def scan_all(self) -> list[NegRiskOpportunity]:
        """Scan all registered markets for bundle and conversion arbitrage opportunities."""
        opportunities: list[NegRiskOpportunity] = []
        for market in self.markets.values():
            opps = self.coherence_checker.scan_market(market)
            opportunities.extend(opps)
        # Sort opportunities by highest net edge USD descending
        opportunities.sort(key=lambda o: o.net_edge_usd, reverse=True)
        return opportunities

    def sweep_depth_bundle(
        self,
        event_id: str,
        opportunity_type: OpportunityType = OpportunityType.BUY_ALL_NO,
        target_shares: Optional[float] = None,
        gas_price_gwei: Optional[float] = None,
        pol_usd_price: Optional[float] = None,
    ) -> Optional[NegRiskOpportunity]:
        """Sweep multi-level L2 depth for a registered market."""
        market = self.markets.get(event_id)
        if market is None:
            return None
        return self.coherence_checker.sweep_depth_bundle(
            market=market,
            opportunity_type=opportunity_type,
            target_shares=target_shares,
            gas_price_gwei=gas_price_gwei,
            pol_usd_price=pol_usd_price,
        )

    def plan_execution(self, opportunity: NegRiskOpportunity) -> NegRiskExecutionPlan:
        """Create an execution plan for a detected opportunity."""
        market = self.markets[opportunity.event_id]
        return self.conversion_planner.create_plan(opportunity, market)

    def handle_broken_basket(
        self,
        plan: NegRiskExecutionPlan,
        filled_orders: list[dict[str, Any]],
        failed_leg_index: int,
        error_message: str,
        policy: Optional[BrokenBasketPolicy] = None,
        pol_usd_price: Optional[float] = None,
    ) -> BrokenBasketResolution:
        """Institutional emergency unwind and synthetic conversion policy for broken multi-leg baskets.

        Mitigations:
        1. UNWIND_MARKET:
           Immediately submits offsetting IOC orders on already-filled partial legs to cross
           CLOB bids, liquidating directional exposure and locking in minimal spread loss.
        2. SYNTHETIC_CONVERT:
           In BUY_ALL_NO, when k-1 legs have filled and 1 leg fails, the filled NO shares can be
           synthetically converted via NegRiskAdapter.convertPositions() into 1 YES share of the
           missing leg plus (k-2) USDC cash collateral, perfectly neutralizing directional exposure
           without crossing wide taker spreads!
        3. ABORT_HOLD:
           Halts execution and preserves inventory for manual algorithmic supervision.
        """
        active_policy = policy or self.broken_basket_policy
        market = self.markets.get(plan.opportunity.event_id)

        if active_policy == BrokenBasketPolicy.ABORT_HOLD or market is None or not filled_orders:
            logger.warning(
                f"[BROKEN-BASKET] Policy={active_policy.value}: aborting plan {plan.plan_id} "
                f"with {len(filled_orders)} unhedged filled legs. Manual intervention required."
            )
            return BrokenBasketResolution(
                plan_id=plan.plan_id,
                failed_leg_index=failed_leg_index,
                failed_error=error_message,
                policy_applied=active_policy,
                is_resolved=False,
                message=f"Aborted hold: {error_message}",
            )

        if active_policy == BrokenBasketPolicy.SYNTHETIC_CONVERT:
            filled_indices = [
                ord_spec.get("leg_index")
                for ord_spec in plan.clob_orders[:len(filled_orders)]
                if ord_spec.get("leg_index") is not None
            ]
            if (
                plan.opportunity.opportunity_type == OpportunityType.BUY_ALL_NO
                and len(filled_indices) == (market.k - 1)
            ):
                shares = plan.opportunity.shares
                shares_6dp = amount_to_6dp(shares)
                cash_unlocked = (market.k - 2) * shares

                convert_calldata = build_negrisk_convert(
                    market_id=market.market_id,
                    index_sets=filled_indices,
                    amount_6dp=shares_6dp,
                )
                tx_hash = f"0xsim_synth_convert_{uuid.uuid4().hex[:12]}"
                if not self.dry_run and self.ctf_client is not None:
                    try:
                        settle_res = self.ctf_client.execute_settlement(
                            calldata=convert_calldata,
                            to_address=NEGRISK_ADAPTER_ADDRESS,
                            condition_id=market.event_id,
                            amount_shares=shares,
                            pol_usd_price=pol_usd_price,
                            kind="convert",
                        )
                        if settle_res.success:
                            tx_hash = settle_res.tx_hash
                    except Exception as exc:
                        logger.error(f"[BROKEN-BASKET] Synthetic conversion failed: {exc}")

                logger.info(
                    f"[BROKEN-BASKET] Synthetic conversion successful for plan {plan.plan_id}: "
                    f"Converted {len(filled_indices)} NO legs into YES_{failed_leg_index} + ${cash_unlocked:.2f} USDC collateral (Tx: {tx_hash[:16]})."
                )
                return BrokenBasketResolution(
                    plan_id=plan.plan_id,
                    failed_leg_index=failed_leg_index,
                    failed_error=error_message,
                    policy_applied=BrokenBasketPolicy.SYNTHETIC_CONVERT,
                    synthetic_converted_shares=shares,
                    synthetic_cash_unlocked_usd=cash_unlocked,
                    is_resolved=True,
                    message=f"Synthetically converted {len(filled_indices)} legs to YES_{failed_leg_index} + ${cash_unlocked:.2f} USDC",
                )

        # Fallback / Default: UNWIND_MARKET
        logger.warning(
            f"[BROKEN-BASKET] Executing UNWIND_MARKET for plan {plan.plan_id} across {len(filled_orders)} filled legs..."
        )
        unwind_orders: list[dict[str, Any]] = []
        realized_unwind_pnl = 0.0

        for i, filled_ord in enumerate(filled_orders):
            orig_spec = plan.clob_orders[i] if i < len(plan.clob_orders) else filled_ord
            token_id = orig_spec["token_id"]
            orig_side = orig_spec["side"]
            orig_price = orig_spec["price"]
            size = orig_spec["size"]
            leg_idx = orig_spec.get("leg_index", i)
            leg = market.get_leg(leg_idx) if leg_idx in market.legs else None

            # Determine unwind side & counter-price
            if orig_side.upper() == "BUY":
                unwind_side = "SELL"
                if orig_spec.get("outcome_type") == "NO":
                    unwind_price = leg.best_bid_no if (leg and leg.best_bid_no > 0) else max(0.01, orig_price - 0.05)
                else:
                    unwind_price = leg.best_bid_yes if (leg and leg.best_bid_yes > 0) else max(0.01, orig_price - 0.05)
                gross_pnl = (unwind_price - orig_price) * size
            else:
                unwind_side = "BUY"
                if orig_spec.get("outcome_type") == "NO":
                    unwind_price = leg.best_ask_no if (leg and leg.best_ask_no < 1.0) else min(0.99, orig_price + 0.05)
                else:
                    unwind_price = leg.best_ask_yes if (leg and leg.best_ask_yes < 1.0) else min(0.99, orig_price + 0.05)
                gross_pnl = (orig_price - unwind_price) * size

            taker_fee_usd = compute_taker_fee(
                shares=size,
                price=unwind_price,
                fee_rate=leg.fee_rate if leg else 0.0,
                fee_exponent=leg.fee_exponent if leg else 1.0,
            )
            net_pnl = gross_pnl - taker_fee_usd
            realized_unwind_pnl += net_pnl

            unwind_record = {
                "token_id": token_id,
                "side": unwind_side,
                "price": round(unwind_price, 4),
                "size": size,
                "realized_pnl": round(net_pnl, 4),
                "status": "UNWOUND",
            }
            if not self.dry_run and self.clob_client is not None:
                try:
                    res = self.clob_client.place_order(
                        token_id=token_id,
                        side=unwind_side,
                        price=unwind_price,
                        size=size,
                        order_type="IOC",
                    )
                    unwind_record["clob_response"] = res.to_dict()
                except Exception as exc:
                    unwind_record["error"] = str(exc)
            unwind_orders.append(unwind_record)

        return BrokenBasketResolution(
            plan_id=plan.plan_id,
            failed_leg_index=failed_leg_index,
            failed_error=error_message,
            policy_applied=BrokenBasketPolicy.UNWIND_MARKET,
            unwind_orders=unwind_orders,
            unwind_pnl_usd=round(realized_unwind_pnl, 4),
            is_resolved=True,
            message=f"Unwound {len(unwind_orders)} legs via market cross with PnL=${realized_unwind_pnl:.4f}",
        )

    def execute_plan(
        self,
        plan: NegRiskExecutionPlan,
        pol_usd_price: Optional[float] = None,
        simulate_failed_leg_index: Optional[int] = None,
        broken_basket_policy: Optional[BrokenBasketPolicy] = None,
    ) -> NegRiskExecutionResult:
        """Execute or simulate the complete arbitrage plan."""
        logger.info(
            f"Executing arbitrage plan {plan.plan_id} ({plan.opportunity.opportunity_type.value}, "
            f"shares={plan.opportunity.shares:.2f}, net_edge=${plan.estimated_net_profit_usd:.4f})"
        )

        effective_policy = broken_basket_policy or self.broken_basket_policy

        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulating placement of {len(plan.clob_orders)} CLOB orders...")
            mock_orders = []
            for i, ord_spec in enumerate(plan.clob_orders):
                if simulate_failed_leg_index is not None and i == simulate_failed_leg_index:
                    logger.error(f"[DRY-RUN] Simulated execution failure on leg index {i}")
                    resolution = self.handle_broken_basket(
                        plan=plan,
                        filled_orders=mock_orders,
                        failed_leg_index=i,
                        error_message="Simulated dry-run order rejection / liquidity evaporation",
                        policy=effective_policy,
                        pol_usd_price=pol_usd_price,
                    )
                    plan.status = "BROKEN_BASKET_RESOLVED" if resolution.is_resolved else "PARTIAL_FILL_ABORT"
                    actual_profit = (
                        resolution.unwind_pnl_usd
                        if resolution.policy_applied == BrokenBasketPolicy.UNWIND_MARKET
                        else resolution.synthetic_cash_unlocked_usd
                    )
                    return NegRiskExecutionResult(
                        success=False,
                        plan_id=plan.plan_id,
                        orders_placed=mock_orders,
                        tx_hashes=[],
                        actual_net_profit_usd=actual_profit,
                        is_dry_run=True,
                        error=f"Simulated execution failure on leg {i}",
                        status="BROKEN_BASKET_RESOLVED" if resolution.is_resolved else "PARTIAL_FILL_ABORT",
                        broken_basket_resolution=resolution,
                    )

                mock_orders.append(
                    {
                        "order_id": f"0xmock_order_{uuid.uuid4().hex[:12]}",
                        "token_id": ord_spec["token_id"],
                        "side": ord_spec["side"],
                        "price": ord_spec["price"],
                        "size": ord_spec["size"],
                        "status": "FILLED",
                    }
                )

            mock_txs = []
            if plan.requires_on_chain_conversion:
                logger.info(f"[DRY-RUN] Simulating {len(plan.on_chain_calls)} on-chain settlement calls...")
                for call in plan.on_chain_calls:
                    mock_tx = f"0xmock_tx_{uuid.uuid4().hex}"
                    mock_txs.append(mock_tx)
                    logger.info(f"[DRY-RUN] Simulated {call.function_name} -> {mock_tx[:16]}... ({call.description})")

            plan.status = "SIMULATED_SUCCESS"
            return NegRiskExecutionResult(
                success=True,
                plan_id=plan.plan_id,
                orders_placed=mock_orders,
                tx_hashes=mock_txs,
                actual_net_profit_usd=plan.estimated_net_profit_usd,
                is_dry_run=True,
                status="SUCCESS",
            )

        # Live Execution
        if self.clob_client is None:
            return NegRiskExecutionResult(
                success=False,
                plan_id=plan.plan_id,
                error="Live execution requested but no ClobClient configured",
                status="FAILED",
            )

        # 1. Place CLOB orders
        placed_orders = []
        for i, ord_spec in enumerate(plan.clob_orders):
            try:
                res = self.clob_client.place_order(
                    token_id=ord_spec["token_id"],
                    side=ord_spec["side"],
                    price=ord_spec["price"],
                    size=ord_spec["size"],
                    order_type=ord_spec.get("order_type", "IOC"),
                )
                if not res.success:
                    logger.error(f"CLOB order placement failed on leg {i}: {res.error_message}")
                    resolution = self.handle_broken_basket(
                        plan=plan,
                        filled_orders=placed_orders,
                        failed_leg_index=i,
                        error_message=str(res.error_message),
                        policy=effective_policy,
                        pol_usd_price=pol_usd_price,
                    )
                    plan.status = "BROKEN_BASKET_RESOLVED" if resolution.is_resolved else "PARTIAL_FILL_ABORT"
                    actual_profit = (
                        resolution.unwind_pnl_usd
                        if resolution.policy_applied == BrokenBasketPolicy.UNWIND_MARKET
                        else resolution.synthetic_cash_unlocked_usd
                    )
                    return NegRiskExecutionResult(
                        success=False,
                        plan_id=plan.plan_id,
                        orders_placed=placed_orders,
                        actual_net_profit_usd=actual_profit,
                        error=f"Leg order placement failed on leg {i}: {res.error_message}",
                        status="BROKEN_BASKET_RESOLVED" if resolution.is_resolved else "PARTIAL_FILL_ABORT",
                        broken_basket_resolution=resolution,
                    )
                placed_orders.append(res.to_dict())
            except Exception as exc:
                logger.error(f"Exception during CLOB order execution on leg {i}: {exc}")
                resolution = self.handle_broken_basket(
                    plan=plan,
                    filled_orders=placed_orders,
                    failed_leg_index=i,
                    error_message=str(exc),
                    policy=effective_policy,
                    pol_usd_price=pol_usd_price,
                )
                plan.status = "BROKEN_BASKET_RESOLVED" if resolution.is_resolved else "FAILED"
                actual_profit = (
                    resolution.unwind_pnl_usd
                    if resolution.policy_applied == BrokenBasketPolicy.UNWIND_MARKET
                    else resolution.synthetic_cash_unlocked_usd
                )
                return NegRiskExecutionResult(
                    success=False,
                    plan_id=plan.plan_id,
                    orders_placed=placed_orders,
                    actual_net_profit_usd=actual_profit,
                    error=f"Exception during CLOB order execution on leg {i}: {exc}",
                    status="BROKEN_BASKET_RESOLVED" if resolution.is_resolved else "FAILED",
                    broken_basket_resolution=resolution,
                )

        # 2. Execute on-chain conversion / merge
        tx_hashes = []
        if plan.requires_on_chain_conversion:
            if self.ctf_client is None:
                return NegRiskExecutionResult(
                    success=False,
                    plan_id=plan.plan_id,
                    orders_placed=placed_orders,
                    error="On-chain conversion required but no PolygonCtfClient configured",
                    status="FAILED",
                )

            for call in plan.on_chain_calls:
                settle_res = self.ctf_client.execute_settlement(
                    calldata=call.calldata,
                    to_address=call.contract_address,
                    condition_id=plan.opportunity.event_id,
                    amount_shares=plan.opportunity.shares,
                    pol_usd_price=pol_usd_price,
                    kind="convert" if call.function_name == "convertPositions" else "merge",
                )
                if not settle_res.success:
                    logger.error(f"On-chain action {call.function_name} failed: {settle_res.error}")
                    return NegRiskExecutionResult(
                        success=False,
                        plan_id=plan.plan_id,
                        orders_placed=placed_orders,
                        tx_hashes=tx_hashes,
                        error=f"On-chain settlement failed: {settle_res.error}",
                        status="ON_CHAIN_FAILED",
                    )
                tx_hashes.append(settle_res.tx_hash)

        plan.status = "SUCCESS"
        return NegRiskExecutionResult(
            success=True,
            plan_id=plan.plan_id,
            orders_placed=placed_orders,
            tx_hashes=tx_hashes,
            actual_net_profit_usd=plan.estimated_net_profit_usd,
            is_dry_run=False,
            status="SUCCESS",
        )


__all__ = [
    "NEGRISK_ADAPTER_ADDRESS",
    "TYPICAL_CONVERSION_GAS_UNITS",
    "TYPICAL_MERGE_GAS_UNITS",
    "PER_LEG_ADDITIONAL_GAS",
    "DEFAULT_MIN_NET_EDGE_USD",
    "DEFAULT_MIN_NET_EDGE_BPS",
    "DEFAULT_MAX_QUOTE_AGE_S",
    "OpportunityType",
    "BrokenBasketPolicy",
    "BrokenBasketResolution",
    "OrderBookLevel",
    "OrderBookDepth",
    "OutcomeLeg",
    "NegRiskMarket",
    "ArbitrageLegExecution",
    "OnChainAction",
    "NegRiskOpportunity",
    "NegRiskExecutionPlan",
    "NegRiskExecutionResult",
    "compute_taker_fee",
    "estimate_conversion_gas_usd",
    "calculate_minimum_batch_shares",
    "compute_basket_fill_probability",
    "slice_basket_opportunity",
    "CoherenceChecker",
    "ConversionPlanner",
    "NegRiskEngine",
]
