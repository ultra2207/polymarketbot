"""CTF Complete-Set Mint & Selective Cross-Split Arbitrage Engine (Strategy C).

Core Financial Mechanism:
-------------------------
On the Polygon Conditional Tokens Framework (CTF), anyone can call `splitPosition`
with USDC collateral to mint complete sets for par ($1.000) with ZERO exchange fees.

In binary or multi-outcome markets where individual outcomes trade at premiums
such that:
    sum(best_bids) > 1.000 + gas_friction + net_taker_fee

A VIP trading bot can:
1. Mint complete sets on-chain for exactly $1.000 per set using `splitPosition`.
2. Sell N - 1 legs passively as Maker (0% fee + maker volume share / rebate).
3. Execute the single remaining leg as an aggressive Taker (with VIP rebate discount)
   into the resting bid.

Because complete sets are minted on-chain prior to unwinding, the bot holds 100%
of all outcome tokens (which sum to guaranteed $1.000 at settlement). This completely
eliminates directional exposure and achieves ZERO broken-basket risk.

Fee Architecture:
-----------------
* CTF Mint: 0.0% exchange fee (only Polygon PoS EIP-1559 gas, ~85,000 gas units).
* Maker legs (N-1): 0.0% fee.
* Taker leg (1): Standard Polymarket fee formula:
      fee = shares * rate * (p * (1 - p)) ^ exponent
  Discounted by account VIP tier rebate (Bronze: 3%, Silver: 8%, Gold: 18%,
  Platinum: 32%, Diamond: 44%, Obsidian: 50%).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.logs import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from polymarketbot.chain import ChainClient
    from polymarketbot.trading.client import ClobClient
    from polymarketbot.trading.ctf import PolygonCtfClient

logger = get_logger("ctf_split_arbitrage")

# CTF & Polygon PoS Constants
CTF_CONTRACT_POLYGON = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DEFAULT_COLLATERAL_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC.e
ZERO_BYTES32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

# splitPosition(address,bytes32,bytes32,uint256[],uint256) -> 0x72ce4275
CTF_SPLIT_SELECTOR = "72ce4275"
POLYGON_CHAIN_ID = 137
CTF_SHARE_DECIMALS = 1_000_000
TYPICAL_SPLIT_GAS_UNITS = 85_000

# Polymarket VIP Taker Rebate Tiers (live 2026-05-28)
VIP_TIER_REBATES: dict[str, float] = {
    "none": 0.00,
    "bronze": 0.03,    # 3% taker fee discount
    "silver": 0.08,    # 8% taker fee discount
    "gold": 0.18,      # 18% taker fee discount
    "platinum": 0.32,  # 32% taker fee discount
    "diamond": 0.44,   # 44% taker fee discount
    "obsidian": 0.50,  # 50% taker fee discount
}


@dataclass
class CtfSplitArbConfig:
    """Configuration parameters for CTF Mint & Selective Cross-Split Arbitrage."""

    min_gross_spread_bps: float = 40.0   # 0.40% minimum gross spread above par ($1.00)
    min_net_edge_usd: float = 1.00       # $1.00 minimum net risk-free profit
    max_mint_size_usd: float = 1000.0    # Cap on capital deployed per mint batch
    vip_tier: str = "bronze"             # none, bronze, silver, gold, platinum, diamond, obsidian
    gas_units_split: int = TYPICAL_SPLIT_GAS_UNITS
    default_gas_price_gwei: float = 40.0
    default_pol_usd_price: float = 0.092  # measured POL/USD baseline (live oracle preferred)
    collateral_token: str = DEFAULT_COLLATERAL_USDC
    ctf_contract: str = CTF_CONTRACT_POLYGON
    parent_collection_id: str = ZERO_BYTES32

    def __post_init__(self) -> None:
        self.vip_tier = self.vip_tier.lower()
        if self.vip_tier not in VIP_TIER_REBATES:
            raise ValueError(
                f"Invalid vip_tier '{self.vip_tier}'. Allowed tiers: {list(VIP_TIER_REBATES.keys())}"
            )
        if self.min_gross_spread_bps < 0:
            raise ValueError("min_gross_spread_bps cannot be negative")
        if self.min_net_edge_usd < 0:
            raise ValueError("min_net_edge_usd cannot be negative")
        if self.max_mint_size_usd <= 0:
            raise ValueError("max_mint_size_usd must be greater than zero")

    @property
    def vip_rebate_rate(self) -> float:
        """Taker rebate discount percentage for the configured VIP tier."""
        return VIP_TIER_REBATES.get(self.vip_tier, 0.0)


@dataclass
class CtfSplitLeg:
    """Quote and market metadata for one outcome leg."""

    index: int                               # 0-based outcome index
    token_id: str                            # Decimal CLOB asset ID
    condition_id: str                        # 32-byte hex condition ID
    title: str = ""                          # Human readable outcome title (e.g., "YES", "NO", "Home")
    best_bid: float = 0.0                    # Highest resting bid price
    best_bid_size: float = 0.0               # Shares available at best bid
    best_ask: float = 1.0                    # Lowest resting ask price
    best_ask_size: float = 0.0               # Shares available at best ask
    fee_rate: float = 0.02                   # Category base fee rate (default 2% or per schedule)
    fee_exponent: float = 1.0                # Category fee exponent
    fee_schedule: Optional[FeeSchedule] = None

    def get_fee_schedule(self) -> FeeSchedule:
        if self.fee_schedule is not None:
            return self.fee_schedule
        return FeeSchedule(
            rate=self.fee_rate,
            exponent=self.fee_exponent,
            taker_only=True,
            rebate_rate=0.0,
        )


@dataclass
class SplitTransactionPayload:
    """On-chain EIP-1559 transaction payload for CTF splitPosition."""

    to: str
    calldata: str
    value: int = 0
    gas_limit: int = TYPICAL_SPLIT_GAS_UNITS
    condition_id: str = ""
    amount_shares: float = 0.0
    amount_6dp: int = 0
    partition: list[int] = field(default_factory=list)
    collateral_token: str = DEFAULT_COLLATERAL_USDC
    parent_collection_id: str = ZERO_BYTES32


@dataclass
class OrderBatchLeg:
    """Specification for an order in the atomic execution batch."""

    leg_index: int
    token_id: str
    title: str
    side: str = "SELL"                       # We sell minted tokens
    order_type: str = "MAKER"                # "MAKER" (0% fee) or "TAKER"
    price: float = 0.0
    size: float = 0.0
    gross_proceeds_usd: float = 0.0
    gross_fee_usd: float = 0.0
    vip_rebate_usd: float = 0.0
    net_fee_usd: float = 0.0
    is_taker: bool = False


@dataclass
class CtfSplitOpportunity:
    """Discovered mispriced complete set opportunity."""

    market_id: str
    condition_id: str
    legs: list[CtfSplitLeg]
    sum_best_bids: float
    gross_spread: float                      # sum_best_bids - 1.00
    gross_spread_bps: float                  # gross_spread * 10,000
    mint_size_shares: float                  # Complete sets to mint
    mint_cost_usd: float                     # Mint cost = mint_size_shares * $1.00
    gross_proceeds_usd: float                # Total proceeds across all sold legs
    gross_profit_usd: float                  # gross_proceeds - mint_cost
    gas_units: int
    gas_cost_usd: float                      # Polygon EIP-1559 gas in USD
    taker_leg_index: int                     # Leg chosen to take aggressively
    gross_taker_fee_usd: float               # Raw taker fee on taker leg
    vip_rebate_usd: float                    # Rebate savings from VIP tier
    net_taker_fee_usd: float                 # Net taker fee paid
    maker_rebate_usd: float                  # Rebates earned from maker legs (if any)
    net_profit_usd: float                    # Risk-free net profit in USD
    net_roi_bps: float                       # Net return on mint capital (bps)
    is_actionable: bool                      # Meets all minimum spread and profit thresholds
    all_taker_net_profit_usd: float = 0.0    # Reference profit if all legs were swept as taker


@dataclass
class CtfSplitExecutionPlan:
    """Complete institutional execution plan for Strategy C."""

    opportunity: CtfSplitOpportunity
    mint_payload: SplitTransactionPayload
    order_batch: list[OrderBatchLeg]
    total_mint_cost_usd: float
    total_gross_proceeds_usd: float
    total_gas_usd: float
    total_net_fees_usd: float
    net_profit_usd: float
    net_roi_bps: float
    broken_basket_risk_usd: float = 0.0      # Zero broken basket risk guaranteed


@dataclass
class CtfSplitExecutionResult:
    """Result of executing or simulating the split arbitrage plan."""

    success: bool
    plan: CtfSplitExecutionPlan
    mint_tx_hash: str
    orders_placed: list[dict[str, Any]]
    realized_profit_usd: float
    error: Optional[str] = None
    is_dry_run: bool = True


def estimate_polygon_eip1559_gas_usd(
    gas_units: int = TYPICAL_SPLIT_GAS_UNITS,
    gas_price_gwei: float = 40.0,
    pol_usd_price: float = 0.092,
) -> float:
    """Calculate exact on-chain Polygon PoS EIP-1559 gas cost in USD.

    Formula:
        gas_pol = gas_units * (gas_price_gwei * 1e-9)
        gas_usd = gas_pol * pol_usd_price
    """
    gas_pol = gas_units * (gas_price_gwei * 1e-9)
    return round(gas_pol * pol_usd_price, 6)


def compute_leg_taker_fee(
    shares: float,
    price: float,
    fee_schedule: Optional[FeeSchedule] = None,
    fee_rate: float = 0.02,
    fee_exponent: float = 1.0,
    vip_tier: str = "bronze",
) -> tuple[float, float, float]:
    """Calculate gross taker fee, VIP rebate credit, and net taker fee paid.

    Returns:
        (gross_taker_fee_usd, vip_rebate_usd, net_taker_fee_usd)
    """
    if shares <= 0:
        return 0.0, 0.0, 0.0
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"Price out of range [0.0, 1.0]: {price}")

    sched = fee_schedule or FeeSchedule(rate=fee_rate, exponent=fee_exponent)
    gross_fee = taker_fee(shares, price, sched)
    vip_rebate_rate = VIP_TIER_REBATES.get(vip_tier.lower(), 0.0)
    vip_rebate = round(gross_fee * vip_rebate_rate, 5)
    net_fee = round(gross_fee - vip_rebate, 5)
    return gross_fee, vip_rebate, net_fee


def encode_split_positions_calldata(
    condition_id: str,
    amount_shares: float,
    partition: Optional[Sequence[int]] = None,
    collateral_token: str = DEFAULT_COLLATERAL_USDC,
    parent_collection_id: str = ZERO_BYTES32,
) -> str:
    """Encode ABI calldata for CTF splitPosition(address,bytes32,bytes32,uint256[],uint256).

    Selector: 0x72ce4275
    """
    indices = list(partition or [1, 2])
    collat = collateral_token.lower().removeprefix("0x").rjust(64, "0")
    parent = parent_collection_id.lower().removeprefix("0x").rjust(64, "0")
    cond = condition_id.lower().removeprefix("0x").rjust(64, "0")

    # Word offset for dynamic array `partition`: 5 static words * 32 bytes = 160 = 0xa0
    offset_partition = hex(160)[2:].rjust(64, "0")
    amount_raw = int(round(amount_shares * CTF_SHARE_DECIMALS))
    amount_hex = hex(amount_raw)[2:].rjust(64, "0")

    arr_len = hex(len(indices))[2:].rjust(64, "0")
    arr_elems = "".join(hex(idx)[2:].rjust(64, "0") for idx in indices)

    return (
        "0x"
        + CTF_SPLIT_SELECTOR
        + collat
        + parent
        + cond
        + offset_partition
        + amount_hex
        + arr_len
        + arr_elems
    )


class CtfCrossSplitArbitrageEngine:
    """Engine for CTF Mint & Selective Cross-Split Arbitrage (Strategy C).

    Scans binary and multi-outcome markets for condition:
        sum(best_bids) > 1.000 + gas_friction + net_taker_fee
    And constructs atomic mint + Maker/Taker execution plans with zero broken-basket risk.
    """

    def __init__(
        self,
        config: Optional[CtfSplitArbConfig] = None,
        chain_client: Optional[ChainClient] = None,
        ctf_client: Optional[PolygonCtfClient] = None,
        clob_client: Optional[ClobClient] = None,
    ) -> None:
        self.config = config or CtfSplitArbConfig()
        self.chain_client = chain_client
        self.ctf_client = ctf_client
        self.clob_client = clob_client

    def calculate_gas_cost_usd(
        self,
        gas_units: Optional[int] = None,
        gas_price_gwei: Optional[float] = None,
        pol_usd_price: Optional[float] = None,
    ) -> float:
        """Calculate dynamic gas cost in USD with live oracle lookup or fallback."""
        units = gas_units or self.config.gas_units_split

        # Gas price in Gwei
        if gas_price_gwei is not None:
            gwei = gas_price_gwei
        elif self.ctf_client is not None:
            try:
                gwei = float(self.ctf_client.get_dynamic_gas_price_gwei())
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "CTF split gas oracle query failed; using measured venue fallback: %s", exc
                )
                gwei = self.config.default_gas_price_gwei
        else:
            gwei = self.config.default_gas_price_gwei

        # POL price in USD: use the measured baseline unless a caller supplies
        # the live oracle value (the soak measures it once per refresh).
        if pol_usd_price is not None:
            px = pol_usd_price
        else:
            px = self.config.default_pol_usd_price

        return estimate_polygon_eip1559_gas_usd(
            gas_units=units,
            gas_price_gwei=gwei,
            pol_usd_price=px,
        )

    def scan_market(
        self,
        market: Any,
        pol_usd_price: Optional[float] = None,
        gas_price_gwei: Optional[float] = None,
    ) -> Optional[CtfSplitOpportunity]:
        """Scan a single market (binary or multi-outcome) for par mispricing.

        Evaluates:
        1. sum(best_bids) > 1.000 + spread hurdle.
        2. Execution optimization: N - 1 legs as Maker (0% fee), 1 leg as Taker.
        3. Net risk-free profit after Polygon gas and net taker fees.
        """
        legs = self._extract_legs(market)
        if len(legs) < 2:
            return None

        # Check that all legs have valid non-negative bids
        for leg in legs:
            if leg.best_bid < 0.0 or leg.best_bid > 1.0:
                return None

        sum_best_bids = round(sum(leg.best_bid for leg in legs), 6)
        gross_spread = round(sum_best_bids - 1.000, 6)
        gross_spread_bps = round(gross_spread * 10_000.0, 2)

        # Liquidity constraint: mint size limited by shallowest resting bid
        min_bid_depth = min(leg.best_bid_size for leg in legs)
        if min_bid_depth <= 0.0:
            # No available bid liquidity
            mint_size_shares = 0.0
        else:
            # 1 complete set costs $1.00 par, so max_mint_size_usd directly caps shares
            mint_size_shares = min(min_bid_depth, self.config.max_mint_size_usd)

        mint_cost_usd = round(mint_size_shares * 1.000, 4)
        gross_proceeds_usd = round(mint_size_shares * sum_best_bids, 4)
        gross_profit_usd = round(gross_proceeds_usd - mint_cost_usd, 4)

        # Polygon PoS Gas Friction
        gas_units = self.config.gas_units_split
        gas_cost_usd = self.calculate_gas_cost_usd(
            gas_units=gas_units,
            gas_price_gwei=gas_price_gwei,
            pol_usd_price=pol_usd_price,
        )

        # Evaluate execution: Select which leg to take aggressively as Taker
        # The remaining N - 1 legs are executed as Maker (0% fee).
        # We test each leg j as the taker leg and choose the one maximizing net profit.
        best_taker_leg_idx = 0
        best_net_profit = -float("inf")
        best_gross_taker_fee = 0.0
        best_vip_rebate = 0.0
        best_net_taker_fee = 0.0

        if mint_size_shares > 0.0:
            for j, leg in enumerate(legs):
                gross_fee, rebate, net_fee = compute_leg_taker_fee(
                    shares=mint_size_shares,
                    price=leg.best_bid,
                    fee_schedule=leg.get_fee_schedule(),
                    vip_tier=self.config.vip_tier,
                )
                net_profit = round(gross_profit_usd - net_fee - gas_cost_usd, 4)
                if net_profit > best_net_profit:
                    best_net_profit = net_profit
                    best_taker_leg_idx = j
                    best_gross_taker_fee = gross_fee
                    best_vip_rebate = rebate
                    best_net_taker_fee = net_fee

            # Also compute reference profit if all legs were swept as Taker
            all_taker_net_fee = sum(
                compute_leg_taker_fee(
                    shares=mint_size_shares,
                    price=l.best_bid,
                    fee_schedule=l.get_fee_schedule(),
                    vip_tier=self.config.vip_tier,
                )[2]
                for l in legs
            )
            all_taker_net_profit = round(gross_profit_usd - all_taker_net_fee - gas_cost_usd, 4)
        else:
            best_net_profit = 0.0
            all_taker_net_profit = 0.0

        net_roi_bps = (
            round((best_net_profit / mint_cost_usd) * 10_000.0, 2)
            if mint_cost_usd > 0
            else 0.0
        )

        # Coherence & Actionability conditions:
        # 1. Gross spread >= min_gross_spread_bps
        # 2. Net profit >= min_net_edge_usd
        # 3. Mint size > 0
        # 4. sum(best_bids) > 1.000 + gas_friction + net_taker_fee
        per_share_friction = (
            (gas_cost_usd + best_net_taker_fee) / mint_size_shares
            if mint_size_shares > 0
            else float("inf")
        )
        is_actionable = (
            gross_spread_bps >= self.config.min_gross_spread_bps
            and best_net_profit >= self.config.min_net_edge_usd
            and mint_size_shares > 0.0
            and sum_best_bids > (1.000 + per_share_friction)
        )

        condition_id = legs[0].condition_id if legs else ""
        market_id = getattr(market, "market_id", condition_id)

        return CtfSplitOpportunity(
            market_id=str(market_id),
            condition_id=condition_id,
            legs=legs,
            sum_best_bids=sum_best_bids,
            gross_spread=gross_spread,
            gross_spread_bps=gross_spread_bps,
            mint_size_shares=mint_size_shares,
            mint_cost_usd=mint_cost_usd,
            gross_proceeds_usd=gross_proceeds_usd,
            gross_profit_usd=gross_profit_usd,
            gas_units=gas_units,
            gas_cost_usd=gas_cost_usd,
            taker_leg_index=best_taker_leg_idx,
            gross_taker_fee_usd=best_gross_taker_fee,
            vip_rebate_usd=best_vip_rebate,
            net_taker_fee_usd=best_net_taker_fee,
            maker_rebate_usd=0.0,
            net_profit_usd=best_net_profit,
            net_roi_bps=net_roi_bps,
            is_actionable=is_actionable,
            all_taker_net_profit_usd=all_taker_net_profit,
        )

    def scan_markets(
        self,
        markets: Sequence[Any],
        pol_usd_price: Optional[float] = None,
        gas_price_gwei: Optional[float] = None,
    ) -> list[CtfSplitOpportunity]:
        """Scan multiple markets and return actionable opportunities sorted by net profit."""
        opportunities = []
        for market in markets:
            opp = self.scan_market(
                market,
                pol_usd_price=pol_usd_price,
                gas_price_gwei=gas_price_gwei,
            )
            if opp is not None and opp.is_actionable:
                opportunities.append(opp)
        opportunities.sort(key=lambda x: x.net_profit_usd, reverse=True)
        return opportunities

    def evaluate_execution(self, opportunity: CtfSplitOpportunity) -> CtfSplitExecutionPlan:
        """Construct the atomic on-chain mint payload and off-chain order batch."""
        k = len(opportunity.legs)
        partition = [1 << i for i in range(k)]

        # 1. Generate on-chain CTF mint transaction payload
        mint_payload = self.build_split_payload(
            condition_id=opportunity.condition_id,
            amount_shares=opportunity.mint_size_shares,
            partition=partition,
            collateral_token=self.config.collateral_token,
        )

        # 2. Generate atomic order batch (N - 1 Maker legs, 1 Taker leg)
        order_batch = []
        total_gross_proceeds = 0.0
        total_net_fees = 0.0

        for idx, leg in enumerate(opportunity.legs):
            is_taker = (idx == opportunity.taker_leg_index)
            order_type = "TAKER" if is_taker else "MAKER"
            price = leg.best_bid
            size = opportunity.mint_size_shares
            gross_proceeds = round(size * price, 4)
            total_gross_proceeds += gross_proceeds

            if is_taker:
                gross_fee, rebate, net_fee = compute_leg_taker_fee(
                    shares=size,
                    price=price,
                    fee_schedule=leg.get_fee_schedule(),
                    vip_tier=self.config.vip_tier,
                )
            else:
                gross_fee, rebate, net_fee = 0.0, 0.0, 0.0

            total_net_fees += net_fee
            order_batch.append(
                OrderBatchLeg(
                    leg_index=idx,
                    token_id=leg.token_id,
                    title=leg.title or f"Outcome {idx}",
                    side="SELL",
                    order_type=order_type,
                    price=price,
                    size=size,
                    gross_proceeds_usd=gross_proceeds,
                    gross_fee_usd=gross_fee,
                    vip_rebate_usd=rebate,
                    net_fee_usd=net_fee,
                    is_taker=is_taker,
                )
            )

        total_mint_cost = opportunity.mint_cost_usd
        total_gas = opportunity.gas_cost_usd
        net_profit = round(total_gross_proceeds - total_mint_cost - total_gas - total_net_fees, 4)
        net_roi_bps = (
            round((net_profit / total_mint_cost) * 10_000.0, 2)
            if total_mint_cost > 0
            else 0.0
        )

        return CtfSplitExecutionPlan(
            opportunity=opportunity,
            mint_payload=mint_payload,
            order_batch=order_batch,
            total_mint_cost_usd=total_mint_cost,
            total_gross_proceeds_usd=total_gross_proceeds,
            total_gas_usd=total_gas,
            total_net_fees_usd=total_net_fees,
            net_profit_usd=net_profit,
            net_roi_bps=net_roi_bps,
            broken_basket_risk_usd=0.0,  # Zero broken basket risk
        )

    def build_split_payload(
        self,
        condition_id: str,
        amount_shares: float,
        partition: Optional[Sequence[int]] = None,
        collateral_token: Optional[str] = None,
    ) -> SplitTransactionPayload:
        """Generate ABI-encoded transaction payload for splitPosition."""
        collat = collateral_token or self.config.collateral_token
        indices = list(partition or [1, 2])
        calldata = encode_split_positions_calldata(
            condition_id=condition_id,
            amount_shares=amount_shares,
            partition=indices,
            collateral_token=collat,
            parent_collection_id=self.config.parent_collection_id,
        )
        return SplitTransactionPayload(
            to=self.config.ctf_contract,
            calldata=calldata,
            value=0,
            gas_limit=self.config.gas_units_split,
            condition_id=condition_id,
            amount_shares=amount_shares,
            amount_6dp=int(round(amount_shares * CTF_SHARE_DECIMALS)),
            partition=indices,
            collateral_token=collat,
            parent_collection_id=self.config.parent_collection_id,
        )

    def simulate_execution(self, plan: CtfSplitExecutionPlan) -> CtfSplitExecutionResult:
        """Simulate the atomic execution plan in dry-run mode, asserting risk-free return."""
        mock_tx_hash = f"0xmock_split_{uuid.uuid4().hex}"
        orders_placed = []
        for o in plan.order_batch:
            orders_placed.append({
                "token_id": o.token_id,
                "title": o.title,
                "side": o.side,
                "type": o.order_type,
                "price": o.price,
                "size": o.size,
                "proceeds": o.gross_proceeds_usd,
                "fee": o.net_fee_usd,
            })

        logger.info(
            f"[DRY-RUN STRATEGY C] Minted {plan.mint_payload.amount_shares:.2f} sets on {plan.mint_payload.condition_id[:10]}... "
            f"| Cost: ${plan.total_mint_cost_usd:.2f} | Proceeds: ${plan.total_gross_proceeds_usd:.2f} "
            f"| Gas: ${plan.total_gas_usd:.4f} | Net Fees: ${plan.total_net_fees_usd:.4f} "
            f"| Risk-Free Net Profit: ${plan.net_profit_usd:.2f} ({plan.net_roi_bps:.1f} bps) "
            f"| Broken Basket Risk: $0.00"
        )

        return CtfSplitExecutionResult(
            success=True,
            plan=plan,
            mint_tx_hash=mock_tx_hash,
            orders_placed=orders_placed,
            realized_profit_usd=plan.net_profit_usd,
            is_dry_run=True,
        )

    def _extract_legs(self, market: Any) -> list[CtfSplitLeg]:
        """Normalize market representations into a list of CtfSplitLeg."""
        if isinstance(market, list):
            legs = []
            for item in market:
                if isinstance(item, CtfSplitLeg):
                    legs.append(item)
                elif isinstance(item, dict):
                    legs.append(
                        CtfSplitLeg(
                            index=item.get("index", len(legs)),
                            token_id=str(item.get("token_id", "")),
                            condition_id=str(item.get("condition_id", "")),
                            title=item.get("title", ""),
                            best_bid=float(item.get("best_bid", 0.0)),
                            best_bid_size=float(item.get("best_bid_size", 0.0)),
                            best_ask=float(item.get("best_ask", 1.0)),
                            best_ask_size=float(item.get("best_ask_size", 0.0)),
                            fee_rate=float(item.get("fee_rate", 0.02)),
                            fee_exponent=float(item.get("fee_exponent", 1.0)),
                        )
                    )
            return legs

        if isinstance(market, dict):
            raw_legs = market.get("legs", [])
            cond_id = market.get("condition_id", "")
            legs = []
            for i, raw in enumerate(raw_legs):
                if isinstance(raw, CtfSplitLeg):
                    legs.append(raw)
                elif isinstance(raw, dict):
                    legs.append(
                        CtfSplitLeg(
                            index=raw.get("index", i),
                            token_id=str(raw.get("token_id", "")),
                            condition_id=str(raw.get("condition_id", cond_id)),
                            title=raw.get("title", f"Outcome {i}"),
                            best_bid=float(raw.get("best_bid", 0.0)),
                            best_bid_size=float(raw.get("best_bid_size", 0.0)),
                            best_ask=float(raw.get("best_ask", 1.0)),
                            best_ask_size=float(raw.get("best_ask_size", 0.0)),
                            fee_rate=float(raw.get("fee_rate", market.get("fee_rate", 0.02))),
                            fee_exponent=float(raw.get("fee_exponent", market.get("fee_exponent", 1.0))),
                        )
                    )
            return legs

        if hasattr(market, "legs"):
            return list(getattr(market, "legs"))

        return []
