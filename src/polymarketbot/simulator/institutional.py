"""Jane Street-Style Institutional Market Making & De-Skewing Engine for Polymarket CTF.

Implements the complete quantitative institutional market making suite for binary prediction markets:
1. Pure Dual-Cash Bidding (Zero Short-Selling / Zero Locates):
   - Places BUY YES @ P_bid_yes and BUY NO @ P_bid_no using pure USDC cash collateral.
   - P_bid_no synthesizes SELL YES at (1.00 - P_bid_no) with 100% smart-contract collateral backing.
2. Zero-Deadband Continuous Reservation Pricing:
   - Evaluates r(q) = P_mid - q * gamma * sigma^2 from the very first share (zero deadband).
   - Asymmetrically skews bid half-spreads continuously across both tokens.
3. Microstructure Sentinel & Toxicity Gate:
   - Cont-Kukanov-Stoikov Order Flow Imbalance (OFI) tracking.
   - Stoikov Micro-Price Drift: P_micro = (Q_b * P_a + Q_a * P_b) / (Q_b + Q_a).
   - Instant quote fading / pulling when toxic sweeps occur.
4. Parity Arbitrage & Negative-Risk Router:
   - Exploits sub-penny parity discrepancies: P_bid_yes + P_bid_no > 1.00 (maker basis lock).
   - Taker negative risk sweep: P_ask_yes + P_ask_no < 1.00.
5. Cross-Book Emergency Synthetic Taker Cross:
   - When |q| >= q_hard or drift > 1.5c in 60s, sweeps the opposite leg at market to freeze delta.
6. Automated CTF Batch Merging:
   - Merges min(YES, NO) complete sets back into $1.00 USDC cash when sets >= threshold.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.rewards.deskew import DynamicThresholdCalculator
from polymarketbot.rewards.microstructure import (
    compute_dynamic_drift_threshold_cents,
    compute_dynamic_ofi_threshold,
)
from polymarketbot.rewards.scoring import quadratic_order_score
from polymarketbot.simulator.book import PRICE_EPSILON, SyntheticOrderBook, _norm_px
from polymarketbot.simulator.impact import MarketImpactEngine, QuoteSide
from polymarketbot.simulator.portfolio import MergeResult, Position, SimulatedPortfolio
from polymarketbot.trading.ctf import estimate_merge_cost_usd

log = get_logger("institutional.mm")


class MarketRegime(str, Enum):
    """Institutional Microstructure Market Regime."""

    BALANCED = "BALANCED"                  # Low toxicity, balanced order flow
    INFORMED_BUY = "INFORMED_BUY"          # Aggressive taker buys, positive OFI / micro drift
    INFORMED_SELL = "INFORMED_SELL"        # Aggressive taker sells, negative OFI / micro drift
    EMERGENCY_DELEVERAGE = "EMERGENCY"     # Imbalance beyond hard risk limit


@dataclass
class DualCashQuotePair:
    """Institutional Dual-Cash Resting Limit Order Pair."""

    bid_price_yes: float
    bid_size_yes: float
    bid_price_no: float
    bid_size_no: float
    synthetic_ask_yes: float               # 1.00 - bid_price_no
    synthetic_ask_no: float                # 1.00 - bid_price_yes
    collateral_committed_usd: float
    reservation_price_yes: float
    micro_price_drift_cents: float
    regime: MarketRegime
    reason: str


class JaneStreetMarketMaker:
    """Full Institutional Jane Street-Style Market Maker for Polymarket CTF."""

    def __init__(
        self,
        condition_id: str,
        initial_cash: float = 150.0,
        quote_size: float = 45.0,
        gamma: float = 0.02,                   # Risk aversion coefficient
        max_spread_cents: float = 1.0,         # Maximum allowable half-spread
        min_spread_cents: float = 0.5,         # Target tight half-spread (top of book)
        auto_merge_threshold: Optional[float] = None,    # Complete sets before batch CTF merge
        q_hard_threshold: Optional[float] = None,       # Emergency circuit breaker inventory limit
        gas_fee_merge: Optional[float] = None,          # Polygon CTF mergePositions gas fee
        ofi_toxicity_threshold: Optional[float] = None, # Cont-Kukanov-Stoikov OFI threshold
        drift_toxicity_cents: Optional[float] = None,    # Micro-price drift threshold (cents)
    ) -> None:
        self.condition_id = condition_id
        self.initial_cash = float(initial_cash)
        self.quote_size = float(quote_size)
        self.gamma = float(gamma)
        self.max_spread_cents = float(max_spread_cents)
        self.min_spread_cents = float(min_spread_cents)

        # Dynamic threshold calculator instantiation
        self.threshold_calculator = DynamicThresholdCalculator()

        # Dynamic evaluation flags
        self._dynamic_gas_fee = (gas_fee_merge is None)
        self._dynamic_q_hard = (q_hard_threshold is None)
        self._dynamic_auto_merge = (auto_merge_threshold is None)
        self._dynamic_ofi = (ofi_toxicity_threshold is None)
        self._dynamic_drift = (drift_toxicity_cents is None)

        # 1. Gas fee merge: evaluate dynamically if None
        if gas_fee_merge is None:
            self.gas_fee_merge = float(estimate_merge_cost_usd())
        else:
            self.gas_fee_merge = float(gas_fee_merge)

        # 2. Dynamic thresholds (q_hard and auto_merge) based on quote_size, initial_cash, and spread
        init_spread_cents = self.min_spread_cents * 2.0
        if self._dynamic_q_hard or self._dynamic_auto_merge:
            calib = self.threshold_calculator.calibrate_all(
                quote_size=self.quote_size,
                bankroll=self.initial_cash,
                cash_reserve=self.initial_cash,
                gamma=self.gamma,
                mid_price=0.50,
                spread_cents=init_spread_cents,
                gas_cost_usd=self.gas_fee_merge,
            )
            self.q_hard = float(q_hard_threshold) if q_hard_threshold is not None else float(calib.q_hard)
            self.auto_merge_threshold = (
                float(auto_merge_threshold) if auto_merge_threshold is not None else float(calib.auto_merge_threshold)
            )
        else:
            self.q_hard = float(q_hard_threshold)
            self.auto_merge_threshold = float(auto_merge_threshold)

        # 3. Dynamic OFI threshold
        if ofi_toxicity_threshold is None:
            self.ofi_thresh = float(compute_dynamic_ofi_threshold(bid_size=100.0, ask_size=100.0))
        else:
            self.ofi_thresh = float(ofi_toxicity_threshold)

        # 4. Dynamic drift threshold
        if drift_toxicity_cents is None:
            self.drift_thresh_cents = float(
                compute_dynamic_drift_threshold_cents(mid_price=0.50, spread_cents=init_spread_cents)
            )
        else:
            self.drift_thresh_cents = float(drift_toxicity_cents)

        # Portfolio Ledger adhering to real-world CTF collateral economics
        self.portfolio = SimulatedPortfolio(
            initial_cash=self.initial_cash,
            gas_fee_merge=self.gas_fee_merge,
            taker_fee_bps=0.0,
        )

        # Microstructure books & queues
        self.book_yes = SyntheticOrderBook(asset_id=f"{condition_id}_YES")
        self.book_no = SyntheticOrderBook(asset_id=f"{condition_id}_NO")
        self.impact_yes = MarketImpactEngine(book=self.book_yes)
        self.impact_no = MarketImpactEngine(book=self.book_no)

        # Order Flow Tracking (OFI rolling window)
        self._last_top_bid_yes: float = 0.50
        self._last_top_bid_size_yes: float = 0.0
        self._last_top_ask_yes: float = 0.51
        self._last_top_ask_size_yes: float = 0.0
        self.rolling_ofi: float = 0.0

        # Dynamic update tracking caches
        self._last_depth_bid_yes: Optional[float] = None
        self._last_depth_ask_yes: Optional[float] = None
        self._last_mid_yes: Optional[float] = None
        self._last_spread_cents: Optional[float] = None

        # Active state
        self.last_decision: Optional[DualCashQuotePair] = None
        self.total_merges: int = 0
        self.total_fills: int = 0

    # -------------------------------------------------------------------------
    # Continuous Microstructure & Pricing Engine
    # -------------------------------------------------------------------------

    def compute_stoikov_micro_price(
        self,
        best_bid: float,
        bid_size: float,
        best_ask: float,
        ask_size: float,
    ) -> float:
        """Compute volume-weighted Stoikov Micro-Price.

        Formula:
            P_micro = (Q_bid * P_ask + Q_ask * P_bid) / (Q_bid + Q_ask)
        """
        total_depth = bid_size + ask_size
        if total_depth <= PRICE_EPSILON:
            return (best_bid + best_ask) / 2.0
        return (bid_size * best_ask + ask_size * best_bid) / total_depth

    def update_order_flow_imbalance(
        self,
        new_bid: float,
        new_bid_size: float,
        new_ask: float,
        new_ask_size: float,
    ) -> float:
        """Compute Cont-Kukanov-Stoikov (2014) Order Flow Imbalance (OFI).

        Delta_OFI = I_bid * Delta_Q_bid - I_ask * Delta_Q_ask
        """
        # Bid contribution
        if new_bid > self._last_top_bid_yes:
            delta_bid = new_bid_size
        elif abs(new_bid - self._last_top_bid_yes) < 1e-4:
            delta_bid = new_bid_size - self._last_top_bid_size_yes
        else:
            delta_bid = -self._last_top_bid_size_yes

        # Ask contribution
        if new_ask < self._last_top_ask_yes:
            delta_ask = new_ask_size
        elif abs(new_ask - self._last_top_ask_yes) < 1e-4:
            delta_ask = new_ask_size - self._last_top_ask_size_yes
        else:
            delta_ask = -self._last_top_ask_size_yes

        ofi_increment = delta_bid - delta_ask
        # Exponential moving decay
        self.rolling_ofi = (self.rolling_ofi * 0.85) + ofi_increment

        self._last_top_bid_yes = new_bid
        self._last_top_bid_size_yes = new_bid_size
        self._last_top_ask_yes = new_ask
        self._last_top_ask_size_yes = new_ask_size

        return self.rolling_ofi

    def compute_continuous_reservation_price(
        self,
        mid_price: float,
        net_inventory: float,
        time_to_resolution_days: float = 7.0,
    ) -> float:
        """Compute zero-deadband Avellaneda-Stoikov reservation price r(s, q).

        Formula for bounded [0, 1] prediction markets:
            sigma^2 = p * (1 - p) / tau
            r(q) = mid_price - q * gamma * sigma^2
        """
        p = max(0.01, min(0.99, mid_price))
        tau = max(1.0 / 1440.0, time_to_resolution_days)
        variance = (p * (1.0 - p)) / tau

        # Continuous penalty from share 1
        r = mid_price - (net_inventory * self.gamma * variance)
        return round(max(0.01, min(0.99, r)), 4)

    # -------------------------------------------------------------------------
    # Core Dual-Cash Institutional Quoting Pipeline
    # -------------------------------------------------------------------------

    def generate_institutional_quotes(
        self,
        best_bid_yes: float,
        best_ask_yes: float,
        depth_bid_yes: float = 100.0,
        depth_ask_yes: float = 100.0,
        time_to_resolution_days: float = 7.0,
    ) -> DualCashQuotePair:
        """Generate institutional pure dual-cash quotes (BUY YES + BUY NO)."""
        pos = self.portfolio.get_position(self.condition_id)
        net_q = pos.shares_yes - pos.shares_no  # +q: Long YES, -q: Long NO
        abs_q = abs(net_q)

        mid_yes = (best_bid_yes + best_ask_yes) / 2.0
        spread_cents = max(0.01, (best_ask_yes - best_bid_yes) * 100.0)

        # Dynamic Threshold Updates on Order Book Depth or Mid-Price Changes
        depth_changed = (
            self._last_depth_bid_yes is None
            or self._last_depth_ask_yes is None
            or abs(depth_bid_yes - self._last_depth_bid_yes) > PRICE_EPSILON
            or abs(depth_ask_yes - self._last_depth_ask_yes) > PRICE_EPSILON
        )
        mid_or_spread_changed = (
            self._last_mid_yes is None
            or self._last_spread_cents is None
            or abs(mid_yes - self._last_mid_yes) > 1e-4
            or abs(spread_cents - self._last_spread_cents) > 1e-4
        )

        if depth_changed and self._dynamic_ofi:
            self.ofi_thresh = float(
                compute_dynamic_ofi_threshold(
                    bid_size=depth_bid_yes,
                    ask_size=depth_ask_yes,
                )
            )

        if mid_or_spread_changed:
            if self._dynamic_drift:
                self.drift_thresh_cents = float(
                    compute_dynamic_drift_threshold_cents(
                        mid_price=mid_yes,
                        spread_cents=spread_cents,
                    )
                )

            if self._dynamic_q_hard or self._dynamic_auto_merge:
                calib = self.threshold_calculator.calibrate_all(
                    quote_size=self.quote_size,
                    bankroll=self.initial_cash,
                    cash_reserve=self.portfolio.cash,
                    gamma=self.gamma,
                    mid_price=mid_yes,
                    time_to_resolution_days=time_to_resolution_days,
                    spread_cents=spread_cents,
                    gas_cost_usd=self.gas_fee_merge,
                )
                if self._dynamic_q_hard:
                    self.q_hard = float(calib.q_hard)
                if self._dynamic_auto_merge:
                    self.auto_merge_threshold = float(calib.auto_merge_threshold)

            if self._dynamic_gas_fee:
                self.gas_fee_merge = float(estimate_merge_cost_usd())
                self.portfolio.gas_fee_merge = self.gas_fee_merge

        self._last_depth_bid_yes = depth_bid_yes
        self._last_depth_ask_yes = depth_ask_yes
        self._last_mid_yes = mid_yes
        self._last_spread_cents = spread_cents

        micro_px = self.compute_stoikov_micro_price(
            best_bid=best_bid_yes,
            bid_size=depth_bid_yes,
            best_ask=best_ask_yes,
            ask_size=depth_ask_yes,
        )
        micro_drift_cents = abs(micro_px - mid_yes) * 100.0

        # Check Emergency Circuit Breaker
        if abs_q >= self.q_hard or micro_drift_cents > self.drift_thresh_cents:
            regime = MarketRegime.EMERGENCY_DELEVERAGE
        elif self.rolling_ofi > self.ofi_thresh or (micro_px > mid_yes + 0.003):
            regime = MarketRegime.INFORMED_BUY
        elif self.rolling_ofi < -self.ofi_thresh or (micro_px < mid_yes - 0.003):
            regime = MarketRegime.INFORMED_SELL
        else:
            regime = MarketRegime.BALANCED

        # Continuous reservation price
        r_yes = self.compute_continuous_reservation_price(
            mid_price=mid_yes,
            net_inventory=net_q,
            time_to_resolution_days=time_to_resolution_days,
        )

        # Baseline half-spread (in dollars)
        base_half_spread = (self.min_spread_cents / 100.0)

        # Inventory skew factor: if long YES (q > 0), fade YES bid, tighten NO bid
        # If long NO (q < 0), fade NO bid, tighten YES bid
        skew_adjustment = (net_q * 0.0005)

        # Target YES bid
        target_bid_yes = r_yes - base_half_spread - max(0.0, skew_adjustment)
        # Target YES ask (synthesized via BUY NO)
        target_ask_yes = r_yes + base_half_spread - min(0.0, skew_adjustment)

        # Enforce post-only constraints: bid < best_ask, ask > best_bid
        target_bid_yes = min(best_ask_yes - 0.001, target_bid_yes)
        target_ask_yes = max(best_bid_yes + 0.001, target_ask_yes)

        # Clamp bounds
        target_bid_yes = round(max(0.01, min(0.98, target_bid_yes)), 3)
        target_ask_yes = round(max(target_bid_yes + 0.002, min(0.99, target_ask_yes)), 3)

        # Synthesize BUY NO quote from target_ask_yes: P_no = 1.00 - P_ask_yes
        target_bid_no = round(1.00 - target_ask_yes, 3)

        # Size derivation constrained by cash
        req_collateral = (target_bid_yes * self.quote_size) + (target_bid_no * self.quote_size)
        if self.portfolio.cash < req_collateral:
            # Scale down quote size if capital constrained
            scaled_size = max(5.0, math.floor(self.portfolio.cash / (target_bid_yes + target_bid_no)))
        else:
            scaled_size = self.quote_size

        collateral_committed = round((target_bid_yes + target_bid_no) * scaled_size, 2)

        reason = (
            f"JaneStreet MM: q={net_q:+.1f}sh | r_yes=${r_yes:.3f} | "
            f"OFI={self.rolling_ofi:+.0f} | MicroDrift={micro_drift_cents:.2f}c [{regime.value}]"
        )

        quote_pair = DualCashQuotePair(
            bid_price_yes=target_bid_yes,
            bid_size_yes=scaled_size,
            bid_price_no=target_bid_no,
            bid_size_no=scaled_size,
            synthetic_ask_yes=target_ask_yes,
            synthetic_ask_no=round(1.00 - target_bid_yes, 3),
            collateral_committed_usd=collateral_committed,
            reservation_price_yes=r_yes,
            micro_price_drift_cents=micro_drift_cents,
            regime=regime,
            reason=reason,
        )
        self.last_decision = quote_pair
        return quote_pair

    # -------------------------------------------------------------------------
    # Trade Execution & Complete Set Arbitrage
    # -------------------------------------------------------------------------

    def on_maker_fill(
        self,
        token_side: str,  # "YES" or "NO"
        price: float,
        size: float,
        timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Process an executed maker fill on our dual-cash resting bids."""
        ts = timestamp or time.time()
        token_norm = token_side.upper()

        # Execute pure cash-collateralized buy fill
        tx = self.portfolio.process_fill(
            condition_id=self.condition_id,
            side="BUY",
            outcome=token_norm,
            price=price,
            size=size,
            is_maker=True,
            notes=f"JaneStreet Maker Fill: BUY {size:.1f} {token_norm} @ ${price:.3f}",
        )
        self.total_fills += 1

        pos = self.portfolio.get_position(self.condition_id)

        # Automated Batch Complete Set Merge
        merge_res: Optional[MergeResult] = None
        if pos.complete_sets >= self.auto_merge_threshold:
            merge_res = self.portfolio.merge(self.condition_id)
            if merge_res:
                self.total_merges += 1
                log.info(
                    f"[CTF BATCH MERGE] Merged {merge_res.complete_sets_merged:.1f} sets -> "
                    f"+${merge_res.net_cash_unlocked:.2f} USDC (Gain: ${merge_res.realized_gain:+.4f})"
                )

        return {
            "token_side": token_norm,
            "fill_price": price,
            "fill_size": size,
            "cash_after": self.portfolio.cash,
            "shares_yes": pos.shares_yes,
            "shares_no": pos.shares_no,
            "net_exposure": pos.net_exposure,
            "complete_sets": pos.complete_sets,
            "merge_executed": merge_res is not None,
            "realized_gain": merge_res.realized_gain if merge_res else 0.0,
        }

    def execute_emergency_synthetic_cross(
        self,
        best_ask_yes: float,
        best_ask_no: float,
    ) -> Optional[MergeResult]:
        """Emergency Circuit Breaker: Sweeps opposite leg at market to freeze delta to 0."""
        pos = self.portfolio.get_position(self.condition_id)
        net_q = pos.net_exposure

        if abs(net_q) < 5.0:
            return None

        # If long YES: buy NO at market ask to complete sets and merge
        if net_q > 0:
            target_token = "NO"
            sweep_price = best_ask_no
            sweep_size = net_q
        else:
            # If long NO: buy YES at market ask to complete sets and merge
            target_token = "YES"
            sweep_price = best_ask_yes
            sweep_size = abs(net_q)

        log.warning(
            f"[CIRCUIT BREAKER] Emergency synthetic cross engaged for q={net_q:+.1f}sh! "
            f"Taker sweep: BUY {sweep_size:.1f} {target_token} @ ${sweep_price:.3f}"
        )

        # Execute taker buy on complement token
        self.portfolio.process_fill(
            condition_id=self.condition_id,
            side="BUY",
            outcome=target_token,
            price=sweep_price,
            size=sweep_size,
            is_maker=False,
            notes=f"Emergency Circuit Breaker Cross on {target_token}",
        )

        # Immediately merge complete sets back into risk-free USDC
        merge_res = self.portfolio.merge(self.condition_id)
        if merge_res:
            self.total_merges += 1
        return merge_res
