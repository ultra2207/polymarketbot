"""Robust Capital Velocity & Dynamic Hazard Modeling for Domain 2.

Replaces naive linear annualization (8760 / tau) and static empirical curve fitting
with mathematically sound, institutional-grade formulations designed to survive years
of evolving market regimes:

1. Physical Capital Cycle Velocity:
   T_cycle = tau_market + t_settlement + t_reinvestment
   V(tau) = 8760 / (tau_market + t_settle + t_reinvest)
   - Eliminates the infinite-velocity asymptote as tau -> 0.
   - Accounts for real-world UMA resolution challenge lockup (2.0h) and capital queuing.

2. Multi-Layer Dynamic Hazard Modeling:
   - Deterministic / Post-Proposal: Governed by empirical UMA dispute rate via Bayesian updating.
   - Continuous Diffusion (Crypto): First-passage-time barrier model driven by underlying volatility.
   - Market-Implied Hazard: Extracted directly from CLOB order book spreads (no-arbitrage pricing).
   - Online Bayesian Tracking: Self-updating Beta-Binomial conjugate filter that automatically adapts
     as dispute rates or market efficiency evolve over time.
   - Minimax Worst-Case Guarantee: Evaluates capital allocation under the 95% upper credible bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from polymarketbot.logs import get_logger

logger = get_logger("domain2_robust_hazard")

# Default physical latencies (in hours)
DEFAULT_UMA_SETTLEMENT_LATENCY_HOURS: float = 2.0     # 2-hour UMA liveness challenge window
DEFAULT_REINVESTMENT_LATENCY_HOURS: float = 1.0       # Average capital queue/idle search time
HOURS_PER_YEAR: float = 8760.0                        # 365 days * 24 hours


def calculate_cycle_velocity(
    hours_to_resolution: float,
    settlement_latency_hours: float = DEFAULT_UMA_SETTLEMENT_LATENCY_HOURS,
    reinvestment_latency_hours: float = DEFAULT_REINVESTMENT_LATENCY_HOURS,
) -> Tuple[float, float]:
    """Calculate realistic capital velocity under physical redeployment friction.

    Returns:
        Tuple[float, float]:
            - hourly_turnover_rate: Cycles completed per hour (1.0 / T_cycle).
            - annualized_turnover_multiplier: Total cycles per year (8760 / T_cycle).
    """
    tau = max(0.01, float(hours_to_resolution))
    t_settle = max(0.0, float(settlement_latency_hours))
    t_reinvest = max(0.0, float(reinvestment_latency_hours))

    # Total physical capital cycle time
    t_cycle = tau + t_settle + t_reinvest

    hourly_rate = 1.0 / t_cycle
    annualized_mult = HOURS_PER_YEAR / t_cycle
    return hourly_rate, annualized_mult


@dataclass
class BayesianCategoryState:
    """Online Beta-Binomial conjugate tracker for category-specific dispute/reversal rates."""

    alpha: float = 1.0   # Prior pseudocount for disputes/reversals
    beta: float = 1000.0 # Prior pseudocount for clean settlements (baseline 0.1% dispute rate)
    decay_gamma: float = 0.998  # Exponential forgetting factor for non-stationary market drift

    def record_observation(self, reversed_or_disputed: bool) -> None:
        """Update posterior beliefs with exponential memory discounting."""
        self.alpha = self.decay_gamma * self.alpha + (1.0 if reversed_or_disputed else 0.0)
        self.beta = self.decay_gamma * self.beta + (0.0 if reversed_or_disputed else 1.0)

    @property
    def posterior_mean(self) -> float:
        """Expected value of hazard rate."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def posterior_upper_95(self) -> float:
        """Conservative 95% upper credible bound for Minimax robust allocation."""
        mean = self.posterior_mean
        var = (self.alpha * self.beta) / (((self.alpha + self.beta) ** 2) * (self.alpha + self.beta + 1.0))
        std_err = math.sqrt(max(1e-9, var))
        return min(0.50, mean + 1.96 * std_err)


class BayesianDisputeTracker:
    """Self-updating Bayesian dispute and reversal tracker across market categories."""

    def __init__(self) -> None:
        self._categories: Dict[str, BayesianCategoryState] = {}
        # Global fallback state
        self._global_state = BayesianCategoryState(alpha=1.0, beta=1000.0)

    def get_state(self, category: str) -> BayesianCategoryState:
        cat = category.lower().strip()
        if cat not in self._categories:
            self._categories[cat] = BayesianCategoryState()
        return self._categories[cat]

    def record_outcome(self, category: str, reversed_or_disputed: bool) -> None:
        """Record live settlement resolution for continuous learning."""
        cat_state = self.get_state(category)
        cat_state.record_observation(reversed_or_disputed)
        self._global_state.record_observation(reversed_or_disputed)
        logger.info(
            "Bayesian tracker updated for {}: mean={:.4%}, upper95={:.4%}",
            category, cat_state.posterior_mean, cat_state.posterior_upper_95,
        )

    def get_credible_upper_hazard(self, category: str) -> float:
        """Return 95% conservative upper bound for category hazard."""
        cat = category.lower().strip()
        if cat in self._categories:
            return self._categories[cat].posterior_upper_95
        return self._global_state.posterior_upper_95


class RobustHazardModel:
    """Institutional-grade multi-layer hazard and capital velocity model for Domain 2."""

    def __init__(
        self,
        dispute_tracker: Optional[BayesianDisputeTracker] = None,
        settlement_latency_hours: float = DEFAULT_UMA_SETTLEMENT_LATENCY_HOURS,
        reinvestment_latency_hours: float = DEFAULT_REINVESTMENT_LATENCY_HOURS,
    ) -> None:
        self.dispute_tracker = dispute_tracker or BayesianDisputeTracker()
        self.settlement_latency = float(settlement_latency_hours)
        self.reinvestment_latency = float(reinvestment_latency_hours)

    def calculate_diffusion_fpt_hazard(
        self,
        spot: float,
        strike: float,
        vol_annual: float = 0.60,
        hours_to_resolution: float = 1.0,
    ) -> float:
        """Calculate First Passage Time (FPT) barrier hazard for continuous asset strikes (e.g. BTC).

        Uses standard drift-neutral diffusion:
            d2 = [ln(S / K) - 0.5 * sigma^2 * tau] / (sigma * sqrt(tau))
            P(S_tau < K) = Phi(-d2)
        """
        tau_years = max(0.0001, hours_to_resolution / HOURS_PER_YEAR)
        if spot <= strike:
            return 0.50  # Already at or below strike

        vol_tau = vol_annual * math.sqrt(tau_years)
        if vol_tau < 1e-6:
            return 0.0001 if spot > strike else 0.50

        d2 = (math.log(spot / strike) - 0.5 * (vol_annual ** 2) * tau_years) / vol_tau
        # Cumulative standard normal approximation
        cdf_neg_d2 = 0.5 * (1.0 + math.erf(-d2 / math.sqrt(2.0)))
        return float(min(0.50, max(0.0001, cdf_neg_d2)))

    def calculate_market_implied_hazard(
        self,
        price: float,
        hours_to_resolution: float,
        typical_liquidity_spread: float = 0.015,
        annual_risk_free_rate: float = 0.05,
    ) -> float:
        """Extract implied reversal hazard directly from the order book discount.

        Total Market Discount = Time Value + Liquidity Spread + Implied Hazard.
        """
        p = max(0.50, min(0.999, price))
        total_discount = 1.0 - p
        tau_years = hours_to_resolution / HOURS_PER_YEAR
        time_value = annual_risk_free_rate * tau_years
        net_discount = total_discount - time_value - typical_liquidity_spread
        return float(max(0.0001, min(0.50, net_discount)))

    def evaluate_candidate(
        self,
        category: str,
        price: float,
        hours_to_resolution: float,
        is_uma_proposed: bool = False,
        spot_price: Optional[float] = None,
        strike_price: Optional[float] = None,
        underlying_vol_annual: float = 0.60,
    ) -> Tuple[float, float, float]:
        """Compute robust conservative hazard, capital velocity, and win probability.

        Returns:
            Tuple[float, float, float]:
                - p_win: Conservative win probability (1.0 - h_robust).
                - h_robust: Minimax worst-case hazard bound.
                - hourly_velocity: Effective cycles per hour (1.0 / T_cycle).
        """
        hourly_vel, _ = calculate_cycle_velocity(
            hours_to_resolution=hours_to_resolution,
            settlement_latency_hours=self.settlement_latency,
            reinvestment_latency_hours=self.reinvestment_latency,
        )

        cat = category.lower().strip()
        bayesian_upper = self.dispute_tracker.get_credible_upper_hazard(cat)

        # 1. Structural Layer
        if is_uma_proposed or hours_to_resolution <= 0.0:
            # Deterministic post-facto market: event has concluded.
            # Price discount is settlement basis carry (yield), NOT reversal risk.
            # Only exposed to UMA oracle dispute risk bounded by Bayesian upper bound.
            structural_hazard = bayesian_upper
            implied_hazard = bayesian_upper
        elif cat == "crypto" and spot_price is not None and strike_price is not None:
            # First-passage time diffusion for continuous crypto asset
            structural_hazard = self.calculate_diffusion_fpt_hazard(
                spot=spot_price,
                strike=strike_price,
                vol_annual=underlying_vol_annual,
                hours_to_resolution=hours_to_resolution,
            )
            implied_hazard = self.calculate_market_implied_hazard(
                price=price,
                hours_to_resolution=hours_to_resolution,
            )
        elif cat == "sports" and hours_to_resolution <= 1.0:
            # Near-terminal sports game with decided score
            structural_hazard = max(0.0010, bayesian_upper)
            implied_hazard = bayesian_upper
        else:
            # General parametric prior with square-root diffusion scaling
            structural_hazard = max(0.0005, 0.0010 * math.sqrt(max(0.1, hours_to_resolution)))
            implied_hazard = self.calculate_market_implied_hazard(
                price=price,
                hours_to_resolution=hours_to_resolution,
            )

        # 3. Minimax Conservative Upper Bound (Worst-case protection)
        # We take the maximum across structural, implied, and Bayesian upper bounds
        h_robust = max(structural_hazard, implied_hazard, bayesian_upper)
        h_robust = min(0.50, max(0.0002, h_robust))
        p_win = 1.0 - h_robust

        return p_win, h_robust, hourly_vel
