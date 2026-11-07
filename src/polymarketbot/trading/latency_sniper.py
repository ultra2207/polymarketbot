"""Strategy A: High-Frequency Oracle Latency Sniping Engine (Binance Lead-Lag Taker Arm).

Capitalizes on Binance lead-lag latency vs Polymarket order book:
When Binance spot price moves rapidly (e.g. BTC moves >0.25% in trailing 500ms),
slow resting quotes linger on Polymarket's CLOB for 500ms to 1500ms.
By incorporating Polymarket's non-linear taker fee curve and VIP taker rebates
(Bronze 3% up to Obsidian 50%), this engine generates aggressive taker orders
(FAK / Fill-and-Kill) on rapid dislocations.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union

from polymarketbot.fees import taker_tier, weighted_volume
from polymarketbot.logs import get_logger

logger = get_logger("trading.latency_sniper")


# =============================================================================
# VIP Rebate Program Constants & Schedules
# =============================================================================

# Polymarket VIP Taker Rebate Tiers (live 2026-05-28)
# Normalized tier keys to rebate fractions:
VIP_REBATE_RATES: dict[str, float] = {
    "none": 0.0,
    "tier_0": 0.0,
    "0": 0.0,
    "bronze": 0.03,     # 3% rebate
    "silver": 0.08,     # 8% rebate
    "gold": 0.18,       # 18% rebate
    "platinum": 0.32,   # 32% rebate
    "diamond": 0.44,    # 44% rebate
    "obsidian": 0.50,   # 50% rebate
}


def get_vip_rebate_rate(tier: str) -> float:
    """Return the taker rebate rate discount fraction for a VIP tier."""
    norm = str(tier).strip().lower()
    if norm not in VIP_REBATE_RATES:
        raise ValueError(
            f"Unknown VIP tier '{tier}'. Supported tiers: {list(VIP_REBATE_RATES.keys())}"
        )
    return VIP_REBATE_RATES[norm]


def round_currency(value: float, dp: int = 5) -> float:
    """Quantize currency value to specified decimal places with standard half-up rounding."""
    q = Decimal(1).scaleb(-dp)
    return float(Decimal(repr(value)).quantize(q, rounding=ROUND_HALF_UP))


def calculate_taker_fee(
    shares: float,
    price: float,
    rate: float = 0.07,
    exponent: float = 1.0,
    vip_tier: str = "bronze",
    rounding_dp: int = 5,
) -> tuple[float, float, float]:
    """Calculate exact gross taker fee, VIP rebate earned, and net taker fee in USDC.

    Formula:
        fee_gross = shares * rate * (price * (1 - price)) ^ exponent
        rebate = fee_gross * rebate_rate
        fee_net = fee_gross - rebate

    Returns:
        (gross_fee_usd, rebate_earned_usd, net_fee_usd)
    """
    if shares <= 0.0 or rate <= 0.0 or price <= 0.0 or price >= 1.0:
        return 0.0, 0.0, 0.0

    p = max(0.001, min(0.999, price))
    curve = (p * (1.0 - p)) ** exponent
    gross_raw = shares * rate * curve
    rebate_rate = get_vip_rebate_rate(vip_tier)
    rebate_raw = gross_raw * rebate_rate
    net_raw = gross_raw - rebate_raw

    gross = round_currency(gross_raw, rounding_dp)
    rebate = round_currency(rebate_raw, rounding_dp)
    net = round_currency(gross - rebate, rounding_dp)
    return gross, rebate, net


# =============================================================================
# Data Models & Configurations
# =============================================================================

@dataclass
class LatencySniperConfig:
    """Configuration parameters for the High-Frequency Oracle Latency Sniper."""

    min_jump_bps: float = 25.0              # Minimum 0.25% Binance move in trailing 500ms
    max_staleness_ms: float = 1500.0        # Maximum age of Polymarket book snapshot (ms)
    vip_tier: str = "none"                  # VIP rebate tier: none, bronze, silver, gold, platinum, diamond, obsidian
    min_net_edge_bps: float = 15.0          # Net profit hurdle in bps after taker fee and VIP rebate
    max_single_snipe_usd: float = 250.0     # Maximum notional budget per snipe ticket ($)
    trailing_window_ms: float = 500.0       # Trailing lookback window for Binance velocity (ms)
    taker_fee_rate: float = 0.07            # Polymarket base taker fee rate for crypto (7%)
    taker_fee_exponent: float = 1.0         # Exponent in Polymarket fee curve
    max_spread: float = 0.10                # Maximum allowable bid-ask spread before rejecting as too wide
    min_shares: float = 1.0                 # Minimum share quantity required to snipe
    jump_sensitivity: float = 10.0          # Fallback multiplier converting spot move to binary probability shift

    def __post_init__(self) -> None:
        self.vip_tier = str(self.vip_tier).strip().lower()
        if self.vip_tier not in VIP_REBATE_RATES:
            raise ValueError(
                f"Invalid vip_tier '{self.vip_tier}'. Must be one of: {list(VIP_REBATE_RATES.keys())}"
            )
        if self.min_jump_bps <= 0.0:
            raise ValueError("min_jump_bps must be > 0.0")
        if self.max_staleness_ms <= 0.0:
            raise ValueError("max_staleness_ms must be > 0.0")
        if self.min_net_edge_bps < 0.0:
            raise ValueError("min_net_edge_bps must be >= 0.0")
        if self.max_single_snipe_usd <= 0.0:
            raise ValueError("max_single_snipe_usd must be > 0.0")

    @property
    def min_net_edge(self) -> float:
        """Net edge hurdle expressed as price difference per share (bps / 10,000)."""
        return self.min_net_edge_bps / 10_000.0


@dataclass
class PolymarketTopBook:
    """Snapshot of top-of-book quotes for a Polymarket token."""

    token_id: str
    best_bid: float
    best_bid_size: float
    best_ask: float
    best_ask_size: float
    timestamp_ms: float
    market_id: str = ""
    side_hint: str = "YES"

    @property
    def spread(self) -> float:
        """Top-of-book spread (best_ask - best_bid)."""
        return max(0.0, self.best_ask - self.best_bid)

    @property
    def mid_price(self) -> float:
        """Mid-point price between top bid and top ask."""
        return (self.best_bid + self.best_ask) / 2.0

    def age_ms(self, current_time_ms: float) -> float:
        """Age of this book snapshot relative to current time in milliseconds."""
        return max(0.0, current_time_ms - self.timestamp_ms)


@dataclass
class TakerOrder:
    """Actionable aggressive taker order generated by the latency sniper."""

    order_id: str
    token_id: str
    side: str                   # "BUY" or "SELL"
    price: float                # Crossing price (ask for BUY, bid for SELL)
    shares: float               # Execution shares
    notional_usd: float         # shares * price
    fair_price: float           # Estimated fair value after the Binance jump
    gross_edge: float           # Per-share gross edge before fees
    gross_fee: float            # Total expected gross fee ($)
    vip_rebate: float           # Total expected VIP rebate ($)
    net_fee: float              # Total expected net fee ($)
    net_edge: float             # Per-share net edge after fees and rebates
    net_edge_bps: float         # Net edge in basis points (net_edge * 10,000)
    expected_profit_usd: float  # Expected total net profit ($)
    created_at_ms: float        # Timestamp in milliseconds
    order_type: str = "FAK"     # Fill-and-Kill (aggressive taker / IOC)
    presigned_payload: Optional[Dict[str, Any]] = None
    raw_json_bytes: Optional[bytes] = None


@dataclass
class SnipeDecision:
    """Decision output from evaluating a potential sniping opportunity."""

    actionable: bool
    order: Optional[TakerOrder] = None
    rejection_reason: Optional[str] = None
    jump_bps: float = 0.0
    fair_price: float = 0.0
    target_price: float = 0.0
    net_edge: float = 0.0
    net_edge_bps: float = 0.0
    gross_fee_per_share: float = 0.0
    vip_rebate_per_share: float = 0.0
    net_fee_per_share: float = 0.0


@dataclass
class SniperTelemetry:
    """Telemetry and performance metrics for the latency sniping engine."""

    total_snipes: int = 0
    gross_profit: float = 0.0
    fees_paid: float = 0.0
    rebates_earned: float = 0.0
    net_latency_pnl: float = 0.0
    total_volume_usd: float = 0.0
    total_shares: float = 0.0
    rejected_opportunities: int = 0
    rejection_counts: dict[str, int] = field(default_factory=dict)


# =============================================================================
# Strategy A Engine Implementation
# =============================================================================

class BinanceLatencySniperEngine:
    """High-Frequency Oracle Latency Sniping Engine (Binance Lead-Lag Taker Arm).

    Monitors high-speed Binance ticker updates and detects violent price velocity moves.
    When a move exceeds `min_jump_bps` in the trailing lookback window (default 500ms),
    it examines the Polymarket top-of-book. If lingering quotes remain stale:
      - Upward Binance jump: Takes lingering stale Polymarket ASKs via aggressive BUY.
      - Downward Binance dump: Takes lingering stale Polymarket BIDs via aggressive SELL.
    Calculates exact non-linear taker fees minus VIP rebates, verifies `net_edge > min_net_edge`,
    and emits actionable FAK taker orders with full telemetry accounting.
    """

    def __init__(
        self,
        config: Optional[LatencySniperConfig] = None,
        presigned_cache: Optional[Any] = None,
    ) -> None:
        self.config = config or LatencySniperConfig()
        self.presigned_cache = presigned_cache
        self.telemetry = SniperTelemetry()
        self._binance_ticks: deque[tuple[float, float]] = deque()  # (timestamp_ms, price)
        self._executed_orders: list[TakerOrder] = []

    # -------------------------------------------------------------------------
    # Telemetry Forwarding Properties
    # -------------------------------------------------------------------------

    @property
    def total_snipes(self) -> int:
        return self.telemetry.total_snipes

    @property
    def gross_profit(self) -> float:
        return self.telemetry.gross_profit

    @property
    def fees_paid(self) -> float:
        return self.telemetry.fees_paid

    @property
    def rebates_earned(self) -> float:
        return self.telemetry.rebates_earned

    @property
    def net_latency_pnl(self) -> float:
        return self.telemetry.net_latency_pnl

    def sync_vip_tier_from_account(
        self,
        clob_client: Any,
        lookback_days: int = 30,
    ) -> str:
        """Fetch 30-day user trades from Polymarket CLOB API, calculate weighted volume, and set vip_tier."""
        try:
            trades = clob_client.get_trades()
            now_sec = time.time()
            cutoff_sec = now_sec - (lookback_days * 86400)

            total_wv = 0.0
            for t in trades:
                ts = float(t.get("timestamp", 0) or t.get("match_time", 0) or 0)
                if ts >= cutoff_sec:
                    price = float(t.get("price", 0.5))
                    size = float(t.get("size", 0.0))
                    notional = price * size
                    cat = str(t.get("category", "other"))
                    total_wv += weighted_volume(notional, price, cat)

            tier_name, rebate_rate = taker_tier(total_wv)
            tier_norm = tier_name.strip().lower()
            if tier_norm in VIP_REBATE_RATES:
                self.config.vip_tier = tier_norm
            logger.info(
                "Synchronized Polymarket account VIP Tier: {} (Rebate: {:.1f}%, 30d Weighted Vol: ${:,.2f})",
                tier_name,
                rebate_rate * 100.0,
                total_wv,
            )
            return self.config.vip_tier
        except Exception as e:
            logger.warning(
                "Failed to sync account VIP tier from Polymarket API (falling back to {}): {}",
                self.config.vip_tier,
                e,
            )
            return self.config.vip_tier

    # -------------------------------------------------------------------------
    # Binance Price Feed & Velocity Tracking
    # -------------------------------------------------------------------------

    def record_binance_price(
        self,
        price: float,
        timestamp_ms: Optional[float] = None,
    ) -> None:
        """Record an incoming Binance price tick for rolling velocity calculation."""
        if price <= 0.0:
            return
        ts = float(timestamp_ms) if timestamp_ms is not None else time.time() * 1000.0
        self._binance_ticks.append((ts, float(price)))

        # Retain history up to 5x trailing window to keep memory bounded
        cutoff = ts - (self.config.trailing_window_ms * 5.0)
        while self._binance_ticks and self._binance_ticks[0][0] < cutoff:
            self._binance_ticks.popleft()

    def get_binance_velocity_bps(
        self,
        window_ms: Optional[float] = None,
        current_time_ms: Optional[float] = None,
    ) -> float:
        """Calculate Binance spot return in basis points over recent trailing window.

        Returns:
            Basis points move: ((P_current - P_ref) / P_ref) * 10,000.0.
            Positive for upward jumps; negative for downward dumps.
        """
        if len(self._binance_ticks) < 2:
            return 0.0

        win = window_ms if window_ms is not None else self.config.trailing_window_ms
        latest_ts, latest_price = self._binance_ticks[-1]
        now_ts = current_time_ms if current_time_ms is not None else latest_ts
        target_ts = now_ts - win

        ref_price = self._binance_ticks[0][1]
        for t_ms, p in self._binance_ticks:
            if t_ms >= target_ts:
                ref_price = p
                break

        if ref_price <= 0.0:
            return 0.0

        return ((latest_price - ref_price) / ref_price) * 10_000.0

    # -------------------------------------------------------------------------
    # Pricing & Edge Evaluation
    # -------------------------------------------------------------------------

    def estimate_fair_price_post_jump(
        self,
        base_price: float,
        jump_bps: float,
    ) -> float:
        """Estimate post-jump fair binary probability when not explicitly supplied.

        Uses the configured sensitivity multiplier:
            delta_p = (jump_bps / 10,000.0) * jump_sensitivity
        Clamped within (0.001, 0.999).
        """
        shift = (jump_bps / 10_000.0) * self.config.jump_sensitivity
        post_price = base_price + shift
        return max(0.001, min(0.999, post_price))

    def evaluate_snipe(
        self,
        book: PolymarketTopBook,
        jump_bps: Optional[float] = None,
        fair_price_post_jump: Optional[float] = None,
        current_time_ms: Optional[float] = None,
    ) -> SnipeDecision:
        """Evaluate a potential latency sniping opportunity against Polymarket top-of-book.

        Steps:
        1. Check Polymarket book staleness <= max_staleness_ms.
        2. Check Polymarket top-of-book spread <= max_spread.
        3. Check Binance price jump velocity >= min_jump_bps.
        4. Check whether Polymarket book has already updated to reflect fair value.
        5. Calculate exact Polymarket non-linear taker fee and VIP tier rebate.
        6. Verify net edge = (gross_edge - net_fee) > min_net_edge.
        7. Compute optimal snipe size bounded by max_single_snipe_usd and available book depth.
        8. Construct executable FAK aggressive taker order.
        """
        now_ms = current_time_ms if current_time_ms is not None else time.time() * 1000.0

        # 1. Staleness Check
        age_ms = book.age_ms(now_ms)
        if age_ms > self.config.max_staleness_ms:
            self._record_rejection("BOOK_TOO_STALE")
            return SnipeDecision(
                actionable=False,
                rejection_reason="BOOK_TOO_STALE",
            )

        # 2. Spread Check
        if book.spread > self.config.max_spread:
            self._record_rejection("SPREAD_TOO_WIDE")
            return SnipeDecision(
                actionable=False,
                rejection_reason="SPREAD_TOO_WIDE",
            )

        # 3. Binance Velocity Jump Check
        if jump_bps is None:
            jump_bps = self.get_binance_velocity_bps(
                window_ms=self.config.trailing_window_ms,
                current_time_ms=now_ms,
            )

        if abs(jump_bps) < self.config.min_jump_bps:
            self._record_rejection("JUMP_BELOW_THRESHOLD")
            return SnipeDecision(
                actionable=False,
                rejection_reason="JUMP_BELOW_THRESHOLD",
                jump_bps=jump_bps,
            )

        # 4. Target Direction & Book Update Verification
        if jump_bps > 0:
            # UPWARD JUMP: Cross the stale ASK via BUY
            side = "BUY"
            target_price = book.best_ask
            available_size = book.best_ask_size

            if fair_price_post_jump is None:
                fair_price = self.estimate_fair_price_post_jump(target_price, jump_bps)
            else:
                fair_price = fair_price_post_jump

            # Check if book already updated or no gross edge
            if fair_price <= target_price:
                self._record_rejection("BOOK_ALREADY_UPDATED")
                return SnipeDecision(
                    actionable=False,
                    rejection_reason="BOOK_ALREADY_UPDATED",
                    jump_bps=jump_bps,
                    fair_price=fair_price,
                    target_price=target_price,
                )

            gross_edge = fair_price - target_price

        else:
            # DOWNWARD DUMP: Cross the stale BID via SELL
            side = "SELL"
            target_price = book.best_bid
            available_size = book.best_bid_size

            if fair_price_post_jump is None:
                fair_price = self.estimate_fair_price_post_jump(target_price, jump_bps)
            else:
                fair_price = fair_price_post_jump

            # Check if book already updated or no gross edge
            if target_price <= fair_price:
                self._record_rejection("BOOK_ALREADY_UPDATED")
                return SnipeDecision(
                    actionable=False,
                    rejection_reason="BOOK_ALREADY_UPDATED",
                    jump_bps=jump_bps,
                    fair_price=fair_price,
                    target_price=target_price,
                )

            gross_edge = target_price - fair_price

        # 5. Non-Linear Taker Fee and VIP Rebate Calculation per share
        # fee = rate * p * (1 - p) ^ exponent
        p_clamped = max(0.001, min(0.999, target_price))
        fee_curve = (p_clamped * (1.0 - p_clamped)) ** self.config.taker_fee_exponent
        gross_fee_per_share = self.config.taker_fee_rate * fee_curve

        rebate_rate = get_vip_rebate_rate(self.config.vip_tier)
        rebate_per_share = gross_fee_per_share * rebate_rate
        net_fee_per_share = gross_fee_per_share - rebate_per_share

        # 6. Evaluate Net Latency Edge: net_edge = gross_edge - net_fee
        net_edge = gross_edge - net_fee_per_share
        net_edge_bps = net_edge * 10_000.0

        if net_edge <= self.config.min_net_edge:
            self._record_rejection("INSUFFICIENT_NET_EDGE")
            return SnipeDecision(
                actionable=False,
                rejection_reason="INSUFFICIENT_NET_EDGE",
                jump_bps=jump_bps,
                fair_price=fair_price,
                target_price=target_price,
                net_edge=net_edge,
                net_edge_bps=net_edge_bps,
                gross_fee_per_share=gross_fee_per_share,
                vip_rebate_per_share=rebate_per_share,
                net_fee_per_share=net_fee_per_share,
            )

        # 7. Sizing & Liquidity Bound
        if target_price <= 0.0:
            self._record_rejection("INVALID_PRICE")
            return SnipeDecision(actionable=False, rejection_reason="INVALID_PRICE")

        max_shares_budget = self.config.max_single_snipe_usd / target_price
        shares = min(max_shares_budget, available_size)

        if shares < self.config.min_shares:
            self._record_rejection("INSUFFICIENT_LIQUIDITY")
            return SnipeDecision(
                actionable=False,
                rejection_reason="INSUFFICIENT_LIQUIDITY",
                jump_bps=jump_bps,
                fair_price=fair_price,
                target_price=target_price,
                net_edge=net_edge,
                net_edge_bps=net_edge_bps,
            )

        # 8. Construct Executable Taker Order (with PreSignedOrderCache lookup if available)
        notional_usd = round(shares * target_price, 4)
        gross_fee, vip_rebate, net_fee = calculate_taker_fee(
            shares=shares,
            price=target_price,
            rate=self.config.taker_fee_rate,
            exponent=self.config.taker_fee_exponent,
            vip_tier=self.config.vip_tier,
        )
        expected_profit_usd = round(net_edge * shares, 4)

        presigned_payload = None
        raw_json_bytes = None
        if self.presigned_cache is not None:
            try:
                presigned = self.presigned_cache.pop_order(
                    token_id=book.token_id,
                    side=side,
                    price=target_price,
                    size=shares,
                )
                if presigned is not None:
                    presigned_payload = presigned.payload
                    raw_json_bytes = presigned.raw_json_bytes
            except Exception as e:
                logger.debug("Pre-signed cache pop exception: {}", e)

        order = TakerOrder(
            order_id=f"snipe_{uuid.uuid4().hex[:8]}",
            token_id=book.token_id,
            side=side,
            price=target_price,
            shares=round(shares, 2),
            notional_usd=notional_usd,
            fair_price=round(fair_price, 4),
            gross_edge=round(gross_edge, 5),
            gross_fee=gross_fee,
            vip_rebate=vip_rebate,
            net_fee=net_fee,
            net_edge=round(net_edge, 5),
            net_edge_bps=round(net_edge_bps, 2),
            expected_profit_usd=expected_profit_usd,
            created_at_ms=now_ms,
            order_type="FAK",
            presigned_payload=presigned_payload,
            raw_json_bytes=raw_json_bytes,
        )

        return SnipeDecision(
            actionable=True,
            order=order,
            jump_bps=jump_bps,
            fair_price=fair_price,
            target_price=target_price,
            net_edge=net_edge,
            net_edge_bps=net_edge_bps,
            gross_fee_per_share=gross_fee_per_share,
            vip_rebate_per_share=rebate_per_share,
            net_fee_per_share=net_fee_per_share,
        )

    # -------------------------------------------------------------------------
    # Execution & Telemetry Recording
    # -------------------------------------------------------------------------

    def execute_snipe(
        self,
        decision_or_order: Union[SnipeDecision, TakerOrder],
    ) -> Optional[TakerOrder]:
        """Execute a validated snipe order and record all telemetry accounting."""
        if isinstance(decision_or_order, SnipeDecision):
            if not decision_or_order.actionable or decision_or_order.order is None:
                return None
            order = decision_or_order.order
        else:
            order = decision_or_order

        # Update Telemetry Metrics
        gross_profit = round(order.gross_edge * order.shares, 4)
        self.telemetry.total_snipes += 1
        self.telemetry.gross_profit = round(self.telemetry.gross_profit + gross_profit, 4)
        self.telemetry.fees_paid = round(self.telemetry.fees_paid + order.gross_fee, 4)
        self.telemetry.rebates_earned = round(self.telemetry.rebates_earned + order.vip_rebate, 4)
        self.telemetry.net_latency_pnl = round(
            self.telemetry.net_latency_pnl + order.expected_profit_usd, 4
        )
        self.telemetry.total_volume_usd = round(
            self.telemetry.total_volume_usd + order.notional_usd, 4
        )
        self.telemetry.total_shares = round(self.telemetry.total_shares + order.shares, 2)

        self._executed_orders.append(order)
        logger.info(
            f"[LATENCY_SNIPER] Executed {order.side} {order.shares} shs @ ${order.price:.3f} | "
            f"Gross: +${gross_profit:.4f} | NetFee: ${order.net_fee:.4f} (Rebate: ${order.vip_rebate:.4f}) | "
            f"Net PnL: +${order.expected_profit_usd:.4f} ({order.net_edge_bps:.1f} bps)"
        )
        return order

    def _record_rejection(self, reason: str) -> None:
        """Internal helper to count rejection reasons for diagnostic telemetry."""
        self.telemetry.rejected_opportunities += 1
        self.telemetry.rejection_counts[reason] = (
            self.telemetry.rejection_counts.get(reason, 0) + 1
        )
