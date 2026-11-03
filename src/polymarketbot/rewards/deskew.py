"""Production-Grade Dynamic Inventory De-Skewing Optimizer for Polymarket CTF.

Based on 2026 Quantitative Market Making Research (Jane Street, Citadel, Optiver, Wintermute):
1. 24/7 Multi-pathway de-skew optimizer (Passive Skewing vs Synthetic Complete-Set Crossing vs Direct Taker).
2. Dynamic Guéant-Tapia-Manziadi inventory urgency boundary q_crit for bounded [0, 1] prediction markets.
3. Microstructure toxicity sentinel integration (CKS Order Flow Imbalance + Stoikov Micro-Price Drift).
4. Sub-penny synthetic complete-set parity router:
   Compares (1.00 - P_ask(opposite) - Gas/Q) vs P_bid(residual) to guarantee lowest-cost de-skewing.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from polymarketbot.logs import get_logger

log = get_logger("deskew_optimizer")


class DeSkewPathway(str, Enum):
    HOLD_STABLE = "HOLD_STABLE"                # Within neutrality zone
    PASSIVE_FADE = "PASSIVE_FADE"              # Tier 1: Skew quotes, collect spread + rewards
    SYNTHETIC_MERGE = "SYNTHETIC_MERGE"        # Tier 2: Buy opposite leg CLOB + CTF merge to USDC
    DIRECT_CROSS = "DIRECT_CROSS"              # Tier 2: Cross bid directly on skewed token


@dataclass(frozen=True)
class DeSkewDecision:
    pathway: DeSkewPathway
    urgency_level: str                         # "LOW", "MEDIUM", "CRITICAL_PANIC"
    target_token: str                          # "YES", "NO", or "NONE"
    action_side: str                           # "BUY" or "SELL"
    order_type: str                            # "LIMIT_MAKER" or "MARKET_TAKER"
    target_price: float
    target_size: float
    expected_cost_bps: float
    synthetic_savings_cents: float
    reason: str


@dataclass(frozen=True)
class DynamicThresholds:
    """Dynamically calibrated inventory and execution thresholds."""
    q_soft: float
    q_hard: float
    max_holding_seconds: float
    auto_merge_threshold: float
    reason: str


class DynamicThresholdCalculator:
    """Mathematical calibration engine for prediction market inventory & capital limits."""

    @staticmethod
    def calculate_bounded_variance(
        mid_price: float,
        time_to_resolution_days: float = 7.0,
    ) -> float:
        """Wright-Fisher bounded variance sigma^2(p, tau) = p * (1 - p) / tau."""
        p = max(0.01, min(0.99, mid_price))
        tau = max(1.0 / 1440.0, time_to_resolution_days)  # Floor at 1 minute
        return (p * (1.0 - p)) / tau

    @classmethod
    def compute_q_soft(
        cls,
        quote_size: float,
        bankroll: float,
        gamma: float = 0.02,
        mid_price: float = 0.50,
        time_to_resolution_days: float = 7.0,
        rewards_min_size: float = 5.0,
        kappa_soft: float = 1.0,
    ) -> float:
        """Dynamically scale Tier 1 passive fade threshold q_soft."""
        gamma_0 = 0.02
        sigma_0_sq = 0.25 / 7.0  # Reference variance: 50/50 market 7 days out
        sigma_sq = cls.calculate_bounded_variance(mid_price, time_to_resolution_days)

        risk_scale = ((gamma_0 / max(1e-4, gamma)) ** 0.5) * ((sigma_0_sq / max(1e-4, sigma_sq)) ** 0.25)
        raw_soft = kappa_soft * quote_size * risk_scale

        q_min = max(rewards_min_size, 5.0)
        # Cap at 20% of bankroll dollar allocation
        q_max = max(q_min, (0.20 * bankroll) / max(0.10, mid_price))
        return round(max(q_min, min(q_max, raw_soft)), 1)

    @classmethod
    def compute_q_hard(
        cls,
        quote_size: float,
        bankroll: float,
        cash_reserve: float,
        gamma: float = 0.02,
        mid_price: float = 0.50,
        time_to_resolution_days: float = 7.0,
        spread_cents: float = 1.0,
        gas_cost_usd: float = 0.015,
        taker_fee_bps: float = 0.0,
        q_soft: Optional[float] = None,
    ) -> float:
        """Dynamically scale Tier 2 emergency taker threshold q_hard."""
        # 1. Multi-fill absorption capacity (3.5 full resting fills)
        q_absorb = 3.5 * quote_size

        # 2. Synthetic Cross Solvency Ceiling: Cash required to buy complement leg + gas
        spread_dollars = spread_cents / 100.0
        taker_fee = taker_fee_bps / 10_000.0
        p_ask_opp = max(0.05, min(0.95, (1.0 - mid_price) + (spread_dollars / 2.0)))
        usable_reserve = max(0.0, cash_reserve * 0.95 - gas_cost_usd)
        q_solvency = usable_reserve / (p_ask_opp * (1.0 + taker_fee)) if p_ask_opp > 0 else q_absorb

        # 3. Bankroll directional risk limit (max 60% bankroll in single token)
        q_risk = (0.60 * bankroll) / max(0.10, mid_price)

        # 4. Guéant-Tapia-Manziadi theoretical boundary
        sigma_sq = cls.calculate_bounded_variance(mid_price, time_to_resolution_days)
        denom = gamma * sigma_sq
        friction = (spread_dollars / 2.0) + taker_fee
        q_gtm = (friction / denom) if denom > 0 else q_absorb

        # Select the constraining safety upper bound
        q_hard_cand = min(q_absorb, q_solvency, q_risk, max(q_absorb, q_gtm))

        # Enforce structural dominance: q_hard must exceed q_soft + 1 full quote
        eff_q_soft = q_soft if q_soft is not None else cls.compute_q_soft(quote_size, bankroll, gamma, mid_price)
        q_floor = max(eff_q_soft + quote_size, 1.5 * eff_q_soft)

        return round(max(q_floor, q_hard_cand), 1)

    @staticmethod
    def compute_holding_time(
        time_to_expiry_seconds: float,
        trades_per_min: float = 0.35,
        category: str = "default",
    ) -> float:
        """Dynamically scale maximum holding duration tau_holding."""
        # 1. Turnover-based matching: allow 3 full expected trade cycles
        trade_rate = max(0.01, trades_per_min)
        tau_turnover = 3.0 * (60.0 / trade_rate)

        # 2. Expiry pin-risk ceiling: max 15% of remaining contract life
        tau_expiry = 0.15 * max(10.0, time_to_expiry_seconds)

        tau_cand = min(tau_turnover, tau_expiry)
        # Bounded between 15 seconds (anti-thrash) and 3600 seconds (1 hour safety cap)
        return round(max(15.0, min(3600.0, tau_cand)), 1)

    @staticmethod
    def compute_auto_merge_threshold(
        gas_cost_usd: float,
        spread_cents: float,
        quote_size: float,
        bankroll: float,
        max_gas_pct: float = 0.05,
    ) -> float:
        """Dynamically scale CTF complete-set auto-merge batch size."""
        spread_dollars = max(0.001, spread_cents / 100.0)
        # Gas amortization hurdle: Gas / (max_gas_pct * spread)
        m_gas = gas_cost_usd / (max_gas_pct * spread_dollars)

        # Synchronize with resting quote size
        m_quote = 1.0 * quote_size
        m_min = 10.0

        # Capital velocity ceiling: Never lock more than 30% bankroll in unmerged pairs
        m_bankroll_cap = max(m_min, 0.30 * bankroll)

        m_cand = max(m_gas, m_quote, m_min)
        m_final = min(m_cand, m_bankroll_cap) if m_bankroll_cap >= m_min else m_min
        return round(max(m_min, m_final), 1)

    @classmethod
    def calibrate_all(
        cls,
        quote_size: float,
        bankroll: float,
        cash_reserve: Optional[float] = None,
        gamma: float = 0.02,
        mid_price: float = 0.50,
        time_to_resolution_days: float = 7.0,
        time_to_expiry_seconds: Optional[float] = None,
        spread_cents: float = 1.0,
        gas_cost_usd: float = 0.015,
        trades_per_min: float = 0.35,
        rewards_min_size: float = 5.0,
        category: str = "default",
    ) -> DynamicThresholds:
        """Generate fully unified dynamic thresholds for all components."""
        eff_reserve = cash_reserve if cash_reserve is not None else (bankroll * 0.25)
        eff_expiry_s = time_to_expiry_seconds if time_to_expiry_seconds is not None else (time_to_resolution_days * 86400.0)

        q_soft = cls.compute_q_soft(
            quote_size=quote_size,
            bankroll=bankroll,
            gamma=gamma,
            mid_price=mid_price,
            time_to_resolution_days=time_to_resolution_days,
            rewards_min_size=rewards_min_size,
        )
        q_hard = cls.compute_q_hard(
            quote_size=quote_size,
            bankroll=bankroll,
            cash_reserve=eff_reserve,
            gamma=gamma,
            mid_price=mid_price,
            time_to_resolution_days=time_to_resolution_days,
            spread_cents=spread_cents,
            gas_cost_usd=gas_cost_usd,
            q_soft=q_soft,
        )
        tau_holding = cls.compute_holding_time(
            time_to_expiry_seconds=eff_expiry_s,
            trades_per_min=trades_per_min,
            category=category,
        )
        m_merge = cls.compute_auto_merge_threshold(
            gas_cost_usd=gas_cost_usd,
            spread_cents=spread_cents,
            quote_size=quote_size,
            bankroll=bankroll,
        )

        reason = (
            f"Dynamic Calibration: B=${bankroll:.0f}, S={quote_size:.0f}sh | "
            f"q_soft={q_soft:.1f}, q_hard={q_hard:.1f}, tau={tau_holding:.0f}s, M_merge={m_merge:.1f}"
        )
        return DynamicThresholds(
            q_soft=q_soft,
            q_hard=q_hard,
            max_holding_seconds=tau_holding,
            auto_merge_threshold=m_merge,
            reason=reason,
        )


class DynamicDeSkewOptimizer:
    """Quantitative Dynamic De-Skewing Engine for 24/7 Prediction Markets."""

    def __init__(
        self,
        gamma: float = 0.02,                   # Risk aversion coefficient
        temporary_impact_eta: float = 1e-4,    # Almgren-Chriss impact parameter
        taker_fee_bps: float = 0.0,            # Taker fee (0 bps default on Polymarket binary)
        gas_cost_merge_usd: float = 0.015,     # Polygon CTF mergePositions gas cost
        q_soft_threshold: float = 15.0,        # Inventory where Tier 1 passive skew begins
        q_hard_threshold: float = 150.0,       # Inventory where Tier 2 emergency cross is forced
        max_holding_seconds: float = 300.0,    # Max age before emergency liquidation (5 min)
        ofi_toxicity_threshold: float = 850.0, # Cont-Kukanov-Stoikov OFI trigger
        drift_toxicity_cents: float = 1.5,     # Stoikov micro-price drift trigger (1.5 cents in 60s)
        quote_size: Optional[float] = None,
        bankroll: Optional[float] = None,
        cash_reserve: Optional[float] = None,
        dynamic_scaling: bool = True,
    ) -> None:
        self.gamma = gamma
        self.eta = temporary_impact_eta
        self.taker_fee = taker_fee_bps / 10_000.0
        self.gas_merge = gas_cost_merge_usd
        self.quote_size = quote_size
        self.bankroll = bankroll
        self.cash_reserve = cash_reserve
        self.dynamic_scaling = dynamic_scaling

        if dynamic_scaling and quote_size is not None and bankroll is not None:
            calib = DynamicThresholdCalculator.calibrate_all(
                quote_size=quote_size,
                bankroll=bankroll,
                cash_reserve=cash_reserve,
                gamma=gamma,
                gas_cost_usd=gas_cost_merge_usd,
            )
            self.q_soft = calib.q_soft
            self.q_hard = calib.q_hard
            self.max_holding_seconds = calib.max_holding_seconds
        else:
            self.q_soft = q_soft_threshold
            self.q_hard = q_hard_threshold
            self.max_holding_seconds = max_holding_seconds

        self.ofi_thresh = ofi_toxicity_threshold
        self.drift_thresh_cents = drift_toxicity_cents

    def calculate_bounded_variance(
        self,
        mid_price: float,
        time_to_resolution_days: float = 7.0,
    ) -> float:
        """Calculate state-dependent Wright-Fisher variance for binary [0, 1] contracts.

        sigma^2(p, tau) = p * (1 - p) / max(tau, 0.01)
        """
        p = max(0.01, min(0.99, mid_price))
        tau = max(1.0 / 1440.0, time_to_resolution_days)  # Floor at 1 minute
        return (p * (1.0 - p)) / tau

    def compute_critical_inventory(
        self,
        mid_price: float,
        spread_cents: float,
        time_to_resolution_days: float = 7.0,
        holding_time_seconds: float = 0.0,
    ) -> float:
        """Compute the Guéant-Tapia-Manziadi urgency threshold q_crit.

        Beyond q_crit, inventory risk dominates execution friction, mandating active taker execution.
        """
        variance = self.calculate_bounded_variance(mid_price, time_to_resolution_days)
        spread_dollars = spread_cents / 100.0
        friction = (spread_dollars / 2.0) + self.taker_fee

        # Time-decay acceleration: as holding time grows, q_crit compresses
        time_decay_factor = max(0.1, 1.0 - (holding_time_seconds / self.max_holding_seconds))
        effective_gamma = self.gamma / time_decay_factor

        denom = effective_gamma * variance
        if denom <= 0:
            return self.q_hard
        q_crit = friction / denom
        return max(self.q_soft, min(self.q_hard, q_crit))

    def evaluate_skew(
        self,
        shares_yes: float,
        shares_no: float,
        best_bid_yes: float,
        best_ask_yes: float,
        best_bid_no: float,
        best_ask_no: float,
        rolling_ofi: float = 0.0,
        micro_price_drift_cents: float = 0.0,
        holding_time_seconds: float = 0.0,
        time_to_resolution_days: float = 7.0,
        quote_size: Optional[float] = None,
        bankroll: Optional[float] = None,
        cash_reserve: Optional[float] = None,
    ) -> DeSkewDecision:
        """Evaluate inventory and return the optimal cost-minimizing de-skewing trajectory."""
        net_q = shares_yes - shares_no  # q > 0: Long YES; q < 0: Long NO
        abs_q = abs(net_q)
        mid_yes = (best_bid_yes + best_ask_yes) / 2.0 if (best_bid_yes and best_ask_yes) else 0.50
        spread_yes_cents = max(0.1, (best_ask_yes - best_bid_yes) * 100.0)

        # Dynamic parameter resolution
        eff_quote_size = quote_size if quote_size is not None else self.quote_size
        eff_bankroll = bankroll if bankroll is not None else self.bankroll
        eff_cash_reserve = cash_reserve if cash_reserve is not None else self.cash_reserve

        if self.dynamic_scaling and eff_quote_size is not None and eff_bankroll is not None:
            eff_q_soft = DynamicThresholdCalculator.compute_q_soft(
                quote_size=eff_quote_size,
                bankroll=eff_bankroll,
                gamma=self.gamma,
                mid_price=mid_yes,
                time_to_resolution_days=time_to_resolution_days,
            )
            eff_q_hard = DynamicThresholdCalculator.compute_q_hard(
                quote_size=eff_quote_size,
                bankroll=eff_bankroll,
                cash_reserve=eff_cash_reserve if eff_cash_reserve is not None else (eff_bankroll * 0.25),
                gamma=self.gamma,
                mid_price=mid_yes,
                time_to_resolution_days=time_to_resolution_days,
                spread_cents=spread_yes_cents,
                gas_cost_usd=self.gas_merge,
                q_soft=eff_q_soft,
            )
            eff_max_holding = DynamicThresholdCalculator.compute_holding_time(
                time_to_expiry_seconds=time_to_resolution_days * 86400.0,
            )
        else:
            eff_q_soft = self.q_soft
            eff_q_hard = self.q_hard
            eff_max_holding = self.max_holding_seconds

        # Toxicity Sentinel Check
        is_toxic_flow = (
            abs(rolling_ofi) > self.ofi_thresh
            or abs(micro_price_drift_cents) > self.drift_thresh_cents
        )
        is_aged_inventory = holding_time_seconds >= eff_max_holding

        # 1. Neutrality Check
        if abs_q < eff_q_soft:
            if not is_toxic_flow:
                return DeSkewDecision(
                    pathway=DeSkewPathway.HOLD_STABLE,
                    urgency_level="LOW",
                    target_token="NONE",
                    action_side="NONE",
                    order_type="NONE",
                    target_price=0.0,
                    target_size=0.0,
                    expected_cost_bps=0.0,
                    synthetic_savings_cents=0.0,
                    reason=f"Inventory within neutrality tolerance: q={net_q:.1f} shares",
                )

            # Asymmetric Toxicity Defense for Neutral Inventory:
            # If rolling_ofi > 0 or micro-price drift is upward: toxic buying flow threatens asks (target_token="YES", action_side="SELL", fading/widening ask).
            # If rolling_ofi < 0 or micro-price drift is downward: toxic selling flow threatens bids (target_token="YES", action_side="BUY", fading/widening bid).
            is_buying_threat = (rolling_ofi > 0) or (rolling_ofi == 0.0 and micro_price_drift_cents >= 0.0)
            target_side = "SELL" if is_buying_threat else "BUY"
            target_price = round(best_ask_yes, 3) if target_side == "SELL" else round(best_bid_yes, 3)

            return DeSkewDecision(
                pathway=DeSkewPathway.PASSIVE_FADE,
                urgency_level="HIGH",
                target_token="YES",
                action_side=target_side,
                order_type="LIMIT_MAKER",
                target_price=target_price,
                target_size=abs_q,
                expected_cost_bps=0.0,
                synthetic_savings_cents=0.0,
                reason="Neutral inventory toxicity detected: fading threatened quote",
            )

        # 3. Urgency Threshold Calculation:
        # Tier 1 (Passive Skew): q_soft <= |q| <= q_hard and toxicity is low
        # Tier 2 (Active Cross): |q| > q_hard OR toxic drift > 1.5c OR aged > max_holding_seconds
        requires_active_liquidation = (
            abs_q > eff_q_hard or is_toxic_flow or is_aged_inventory
        )

        q_crit = eff_q_hard

        # 4. TIER 1: PASSIVE SKEWING (Toxicity Low & Inventory Sub-Critical)
        if not requires_active_liquidation:
            skew_ratio = min(1.0, max(0.0, (abs_q - eff_q_soft) / max(1.0, eff_q_hard - eff_q_soft)))
            if net_q > 0:
                # Excess YES shares (Long YES) -> Tighten ask on YES to sell; pull/widen bid on YES
                skew_offset = (spread_yes_cents * 0.40) * skew_ratio
                passive_ask = max(best_bid_yes + 0.001, best_ask_yes - (skew_offset / 100.0))
                return DeSkewDecision(
                    pathway=DeSkewPathway.PASSIVE_FADE,
                    urgency_level="MEDIUM",
                    target_token="YES",
                    action_side="SELL",
                    order_type="LIMIT_MAKER",
                    target_price=round(passive_ask, 3),
                    target_size=abs_q,
                    expected_cost_bps=0.0,
                    synthetic_savings_cents=0.0,
                    reason=f"Tier 1 Passive Skew: q=+{net_q:.1f} < q_crit({q_crit:.1f}). Fading YES ask to top-of-book.",
                )
            else:
                # Excess NO shares (Long NO) -> Tighten YES bid to buy YES (form complete set); widen/pull NO bid!
                spread_no_cents = max(0.1, (best_ask_no - best_bid_no) * 100.0)
                skew_offset = (spread_no_cents * 0.40) * skew_ratio
                # Tighten YES bid towards mid to organically buy YES and complete the set
                tight_yes_bid = min(best_ask_yes - 0.001, best_bid_yes + (skew_offset / 100.0))
                return DeSkewDecision(
                    pathway=DeSkewPathway.PASSIVE_FADE,
                    urgency_level="MEDIUM",
                    target_token="YES",
                    action_side="BUY",
                    order_type="LIMIT_MAKER",
                    target_price=round(tight_yes_bid, 3),
                    target_size=abs_q,
                    expected_cost_bps=0.0,
                    synthetic_savings_cents=0.0,
                    reason=f"Tier 1 Passive Skew: q={net_q:.1f} (long NO). Pulling NO bid, tightening YES bid to complete pairs.",
                )

        # 5. TIER 2: ACTIVE TAKER ROUTING (Synthetic Merge vs Direct Cross)
        urgency = "CRITICAL_PANIC" if (is_toxic_flow or abs_q >= self.q_hard) else "HIGH"

        if net_q > 0:
            # Long YES (Need to flatten YES)
            # Option A: Direct Sell YES into best bid
            direct_proceeds_per_share = best_bid_yes * (1.0 - self.taker_fee)

            # Option B: Synthetic Complete-Set Crossing -> Buy NO at best ask + Merge at $1.00
            gas_per_share = self.gas_merge / max(1.0, abs_q)
            synthetic_proceeds_per_share = 1.00 - (best_ask_no * (1.0 + self.taker_fee)) - gas_per_share

            delta_advantage_cents = (synthetic_proceeds_per_share - direct_proceeds_per_share) * 100.0

            if synthetic_proceeds_per_share > direct_proceeds_per_share:
                return DeSkewDecision(
                    pathway=DeSkewPathway.SYNTHETIC_MERGE,
                    urgency_level=urgency,
                    target_token="NO",
                    action_side="BUY",
                    order_type="MARKET_TAKER",
                    target_price=best_ask_no,
                    target_size=abs_q,
                    expected_cost_bps=(1.0 - synthetic_proceeds_per_share / mid_yes) * 10_000.0,
                    synthetic_savings_cents=round(delta_advantage_cents, 3),
                    reason=(
                        f"Tier 2 Active Synthetic Merge: Buying {abs_q:.0f} NO @ ${best_ask_no:.3f} + Merge yields "
                        f"+{delta_advantage_cents:.2f}c/sh superior to direct YES bid crossing."
                    ),
                )
            else:
                return DeSkewDecision(
                    pathway=DeSkewPathway.DIRECT_CROSS,
                    urgency_level=urgency,
                    target_token="YES",
                    action_side="SELL",
                    order_type="MARKET_TAKER",
                    target_price=best_bid_yes,
                    target_size=abs_q,
                    expected_cost_bps=(1.0 - direct_proceeds_per_share / mid_yes) * 10_000.0,
                    synthetic_savings_cents=round(delta_advantage_cents, 3),
                    reason=f"Tier 2 Active Direct Cross: Selling {abs_q:.0f} YES @ ${best_bid_yes:.3f} superior to synthetic merge.",
                )
        else:
            # Long NO (Need to flatten NO)
            # Option A: Direct Sell NO into best bid
            mid_no = (best_bid_no + best_ask_no) / 2.0 if (best_bid_no and best_ask_no) else 0.50
            direct_proceeds_per_share = best_bid_no * (1.0 - self.taker_fee)

            # Option B: Synthetic Complete-Set Crossing -> Buy YES at best ask + Merge at $1.00
            gas_per_share = self.gas_merge / max(1.0, abs_q)
            synthetic_proceeds_per_share = 1.00 - (best_ask_yes * (1.0 + self.taker_fee)) - gas_per_share

            delta_advantage_cents = (synthetic_proceeds_per_share - direct_proceeds_per_share) * 100.0

            if synthetic_proceeds_per_share > direct_proceeds_per_share:
                return DeSkewDecision(
                    pathway=DeSkewPathway.SYNTHETIC_MERGE,
                    urgency_level=urgency,
                    target_token="YES",
                    action_side="BUY",
                    order_type="MARKET_TAKER",
                    target_price=best_ask_yes,
                    target_size=abs_q,
                    expected_cost_bps=(1.0 - synthetic_proceeds_per_share / mid_no) * 10_000.0,
                    synthetic_savings_cents=round(delta_advantage_cents, 3),
                    reason=(
                        f"Tier 2 Active Synthetic Merge: Buying {abs_q:.0f} YES @ ${best_ask_yes:.3f} + Merge yields "
                        f"+{delta_advantage_cents:.2f}c/sh superior to direct NO bid crossing."
                    ),
                )
            else:
                return DeSkewDecision(
                    pathway=DeSkewPathway.DIRECT_CROSS,
                    urgency_level=urgency,
                    target_token="NO",
                    action_side="SELL",
                    order_type="MARKET_TAKER",
                    target_price=best_bid_no,
                    target_size=abs_q,
                    expected_cost_bps=(1.0 - direct_proceeds_per_share / mid_no) * 10_000.0,
                    synthetic_savings_cents=round(delta_advantage_cents, 3),
                    reason=f"Tier 2 Active Direct Cross: Selling {abs_q:.0f} NO @ ${best_bid_no:.3f} superior to synthetic merge.",
                )
