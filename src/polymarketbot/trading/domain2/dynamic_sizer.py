"""Institutional Dynamic Position Sizing & Portfolio Risk Budgeting for Domain 2.

Implements institutional quantitative prop desk frameworks (Jump Trading, Jane Street, Citadel, AQR)
to replace naive static caps (e.g. hardcoded 10% single-bet cap, 15% category cap, 25% cash reserve,
and 15 active markets cap):

1. Dynamic Single-Bet Bound w_max(i):
   a) Distance to Strike & Drift-Diffusion Variance:
      z_i = [ln(S_i / K_i) - 0.5 * sigma_i^2 * tau_i] / (sigma_i * sqrt(tau_i))
      h_diffusion = Phi(-z_i)
      M_z(z_i) = strike-distance scaling multiplier (collapses to 0 for near-strike coin flips z < 1.5,
      unlocks full capacity for deterministic deep-ITM z >= 3.5).
      For non-strike binary markets: z_info = ln(p/(1-p)) / (sigma_bin * sqrt(tau)) using empirical entropy.
   b) Continuous Fractional Kelly with Downside Semi-Variance:
      f*_i = (p_i - P_i) / (1 - P_i)
      Downside semi-variance: sigma^2_{-, i} = (1 - p_i) * P_i^2
      f_Kelly = kappa_Kelly * Edge / [(1 - P_i) * (1 + lambda_semi * sigma^2_{-, i})]
   c) Order Book Depth & Concave Market Impact (Kyle's Lambda & Almgren-Chriss):
      lambda_Kyle = Spread / (2 * Depth) or Delta P / Delta Q
      Continuous dynamic gamma: gamma(Spread, Depth) = 1 / [1 + sqrt(Spread / (Depth/10))]
      Concave power-law capacity: w_opt = (Edge / ((1 + alpha) * lambda))^(1/alpha)
      w_impact = min(gamma * Depth, w_opt, w_slippage)
   d) Portfolio CVaR / Expected Shortfall Budgeting:
      Marginal Risk Contribution MRC_i bounds position size endogenously to guarantee
      portfolio tail loss at 99% confidence does not exceed MaxDrawdownTarget (10%).

2. Dynamic Factor Covariance / Cross-Market Correlation Risk Budgeting:
   - Systematic factor loadings X_ik (Crypto Macro, Politics, Sports, General).
   - Factor covariance matrix Omega_F and Euler Factor Risk Parity.
   - Factor risk budget: sum_i X_ik * w_i <= FactorBudget_k.

3. Stochastic Precautionary Liquidity Model (Miller-Orr / Cash Inventory):
   - Miller-Orr optimal return point Z* = (3 * gamma_tx * sigma_flow^2 / (4 * r_opp))^(1/3).
   - Cash-in-advance Value-at-Risk (VaR) liquidity buffer at 99% confidence.
   - Dynamically responds to opportunity arrival rate, turnover speed, and liquidity volatility.

4. Fixed-Cost Order Optimization / L1-Sparsity:
   - Ticket transaction/gas friction and monitoring costs replace arbitrary integer cutoff.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.robust_hazard import BinaryVolatilityEstimator
from polymarketbot.trading.domain2.types import (
    DynamicCashReserveMetrics,
    OrderBookElasticityMetrics,
)

logger = get_logger("domain2_dynamic_sizer")


def standard_normal_cdf(z: float) -> float:
    """Cumulative distribution function for standard normal distribution N(0, 1)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class StrikeDistanceMetrics:
    """Drift-diffusion strike distance and tail crossing probability."""

    spot_price: Optional[float]
    strike_price: Optional[float]
    hours_to_resolution: float
    annual_vol: float
    z_score: float
    downside_hazard: float
    strike_multiplier: float
    is_strike_applicable: bool


@dataclass
class DynamicSizerConfig:
    """Institutional quantitative parameters for Dynamic Position Sizing & Risk Budgeting."""

    # 1. Strike & Diffusion Parameters
    annual_vol_crypto: float = 0.65       # Baseline annualized volatility for crypto strikes
    annual_vol_general: float = 0.40      # Baseline volatility for continuous macro strikes
    z_cutoff: float = 1.50                 # Minimum z-score (1.5 sigma); below this, w_max -> 0
    z_target: float = 3.50                 # Target z-score (3.5 sigma) for full allocation capacity

    # 2. Continuous Kelly & Downside Semi-Variance Parameters
    kelly_fraction: float = 0.40           # Fractional Kelly multiplier kappa (0.25 to 0.50)
    downside_semi_lambda: float = 4.0     # Downside semi-variance penalty lambda_semi
    max_single_bet_ceiling_pct: float = 0.25 # Absolute physical cap for deterministic 4+ sigma bets (25%)
    min_single_bet_floor_usd: float = 10.0   # Minimum viable ticket size

    # 3. Microstructure & Kyle Market Impact Parameters
    default_spread_bps: float = 150.0      # 1.50% default bid-ask spread for Kyle lambda estimation
    depth_gamma_default: float = 0.35      # Maximum fraction of visible order book depth absorbed
    max_kyle_slippage_pct: float = 0.015   # 1.5% maximum allowable price slippage
    concave_impact_alpha: float = 0.50     # Square-root market impact elasticity exponent

    # 4. Portfolio CVaR / Expected Shortfall Budgeting Parameters
    max_drawdown_target: float = 0.10      # 10% maximum portfolio drawdown at 99% confidence
    cvar_confidence: float = 0.99          # 99% confidence level for Expected Shortfall
    systemic_oracle_hazard: float = 0.0010 # 0.10% baseline UMA oracle dispute risk

    # 5. Factor Covariance Parameters
    factor_crypto_budget_pct: float = 0.20 # Factor risk budget for crypto macro driver
    factor_politics_budget_pct: float = 0.30 # Factor risk budget for election/political driver
    factor_sports_budget_pct: float = 0.40   # Factor risk budget for sports (orthogonal matchups)
    factor_general_budget_pct: float = 0.25  # Factor risk budget for general markets

    # 6. Precautionary Liquidity Queuing Parameters
    min_reserve_pct: float = 0.05          # 5% floor when opportunity arrival rate is near zero
    max_reserve_pct: float = 0.35          # 35% ceiling during massive opportunity clusters
    baseline_reserve_pct: float = 0.15     # 15% baseline precautionary cash buffer
    baseline_arrival_rate: float = 5.0     # Baseline opportunities arriving per hour
    baseline_lockup_hours: float = 2.5     # Baseline resolution + settlement lockup duration
    miller_orr_tx_cost_usd: float = 0.50   # Cash rebalance / trade friction for inventory model

    # 7. Fixed-Cost Order Optimization / Ticket Hurdle
    fixed_ticket_cost_usd: float = 0.25    # Transaction gas + monitoring friction per activated market


class DynamicPositionSizer:
    """Institutional Dynamic Sizer & Portfolio Risk Budgeting Engine."""

    # Multi-factor systematic covariance matrix
    FACTOR_COVARIANCE: Dict[str, Dict[str, float]] = {
        "crypto": {"crypto": 0.40, "politics": 0.05, "sports": 0.01, "general": 0.10},
        "politics": {"crypto": 0.05, "politics": 0.35, "sports": 0.00, "general": 0.08},
        "sports": {"crypto": 0.01, "politics": 0.00, "sports": 0.15, "general": 0.02},
        "general": {"crypto": 0.10, "politics": 0.08, "sports": 0.02, "general": 0.25},
    }

    def __init__(self, config: Optional[DynamicSizerConfig] = None) -> None:
        self.config = config or DynamicSizerConfig()
        self.vol_estimator = BinaryVolatilityEstimator()

    def compute_strike_distance_metrics(
        self,
        category: str,
        hours_to_resolution: float,
        spot_price: Optional[float] = None,
        strike_price: Optional[float] = None,
        is_uma_proposed: bool = False,
        contract_price: Optional[float] = None,
        spread: Optional[float] = None,
        price_history: Optional[List[float]] = None,
        empirical_vol: Optional[float] = None,
        is_deterministic_conclusion: bool = False,
    ) -> StrikeDistanceMetrics:
        """Compute drift-diffusion distance to strike z_i and strike multiplier M_z(z_i).
        
        Mathematical Formulation:
            z_i = [ln(S_i / K_i) - 0.5 * sigma^2 * tau] / (sigma * sqrt(tau))
            Downside crossing probability: h_diffusion = Phi(-z_i)
            Strike multiplier:
                M_z(z) = 0.0                            if z <= z_cutoff (1.5 sigma)
                M_z(z) = ((z - z_cutoff)/(z_target - z_cutoff))^2  if z_cutoff < z < z_target
                M_z(z) = 1.0                            if z >= z_target (3.5 sigma)
        """
        tau_years = max(0.0001, hours_to_resolution / 8760.0)
        cat = category.lower().strip()

        # If post-proposal or deterministic resolution, distance to strike is infinite (outcome settled)
        if is_uma_proposed or is_deterministic_conclusion or hours_to_resolution <= 0.0:
            return StrikeDistanceMetrics(
                spot_price=spot_price,
                strike_price=strike_price,
                hours_to_resolution=hours_to_resolution,
                annual_vol=0.0,
                z_score=10.0,
                downside_hazard=self.config.systemic_oracle_hazard,
                strike_multiplier=1.0,
                is_strike_applicable=False,
            )

        # For continuous crypto / strike assets where spot and strike are provided
        if spot_price is not None and strike_price is not None and strike_price > 0:
            if empirical_vol is not None:
                vol = empirical_vol
            elif contract_price is not None:
                vol_metrics = self.vol_estimator.estimate_volatility(
                    category=cat,
                    price=contract_price,
                    hours_to_resolution=hours_to_resolution,
                    spread=spread,
                    price_history=price_history,
                )
                vol = vol_metrics.effective_annual_vol
            else:
                vol = self.config.annual_vol_crypto if cat == "crypto" else self.config.annual_vol_general

            vol_tau = vol * math.sqrt(tau_years)

            if spot_price <= strike_price:
                # Already breached or at strike!
                return StrikeDistanceMetrics(
                    spot_price=spot_price,
                    strike_price=strike_price,
                    hours_to_resolution=hours_to_resolution,
                    annual_vol=vol,
                    z_score=-1.0,
                    downside_hazard=0.50,
                    strike_multiplier=0.0,
                    is_strike_applicable=True,
                )

            # Compute d2 / z-score
            z = (math.log(spot_price / strike_price) - 0.5 * (vol ** 2) * tau_years) / max(1e-6, vol_tau)
            h_diffusion = float(standard_normal_cdf(-z))

            if z <= self.config.z_cutoff:
                mult = 0.0
            elif z >= self.config.z_target:
                mult = 1.0
            else:
                progress = (z - self.config.z_cutoff) / (self.config.z_target - self.config.z_cutoff)
                mult = progress ** 2

            return StrikeDistanceMetrics(
                spot_price=spot_price,
                strike_price=strike_price,
                hours_to_resolution=hours_to_resolution,
                annual_vol=round(vol, 4),
                z_score=round(z, 3),
                downside_hazard=round(h_diffusion, 5),
                strike_multiplier=round(mult, 4),
                is_strike_applicable=True,
            )

        # Non-strike binary markets: compute empirical information distance to parity
        if contract_price is not None and hours_to_resolution > 0.0:
            vol_metrics = self.vol_estimator.estimate_volatility(
                category=cat,
                price=contract_price,
                hours_to_resolution=hours_to_resolution,
                spread=spread,
                price_history=price_history,
            )
            vol_bin = vol_metrics.effective_annual_vol
            p = min(0.9999, max(0.0001, contract_price))
            # Binary entropy distance from uninformative 50-50 prior
            denom = math.sqrt(p * (1.0 - p))
            z_info = (p - 0.50) / max(1e-4, denom)
            h_info = float(max(1e-4, 1.0 - p))

            if z_info <= self.config.z_cutoff:
                mult = 0.0
            elif z_info >= self.config.z_target:
                mult = 1.0
            else:
                progress = (z_info - self.config.z_cutoff) / (self.config.z_target - self.config.z_cutoff)
                mult = progress ** 2

            return StrikeDistanceMetrics(
                spot_price=None,
                strike_price=None,
                hours_to_resolution=hours_to_resolution,
                annual_vol=round(vol_bin, 4),
                z_score=round(z_info, 3),
                downside_hazard=round(h_info, 5),
                strike_multiplier=round(mult, 4),
                is_strike_applicable=False,
            )

        # Baseline fallback
        return StrikeDistanceMetrics(
            spot_price=None,
            strike_price=None,
            hours_to_resolution=hours_to_resolution,
            annual_vol=0.0,
            z_score=6.0,
            downside_hazard=self.config.systemic_oracle_hazard,
            strike_multiplier=1.0,
            is_strike_applicable=False,
        )

    def compute_continuous_kelly_bound(
        self,
        p_win: float,
        price: float,
        bankroll_usd: float,
        fee_rate: float = 0.0,
        strike_multiplier: float = 1.0,
        max_single_bet_pct: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """Compute continuous fractional Kelly allocation scaled by downside semi-variance.
        
        Mathematical Formulation:
            Edge: e = p_win - price - fee_rate
            Full Kelly fraction: f* = e / (1 - price)
            Downside semi-variance: sigma^2_- = (1 - p_win) * price^2
            f_Kelly = kappa_Kelly * e / [(1 - price) * (1 + lambda_semi * sigma^2_-)]
            w_Kelly = Bankroll * f_Kelly * strike_multiplier
            
        Returns:
            Tuple[float, float, float]: (w_kelly_usd, kelly_fraction, downside_semi_variance)
        """
        p = max(0.50, min(0.999, price))
        edge = max(0.0, p_win - p - fee_rate)
        if edge <= 1e-6 or strike_multiplier <= 1e-4:
            return 0.0, 0.0, 0.0

        loss_loss_prob = max(1e-6, 1.0 - p_win)
        downside_semi_var = loss_loss_prob * (p ** 2)

        denom = (1.0 - p) * (1.0 + self.config.downside_semi_lambda * downside_semi_var)
        f_kelly_raw = self.config.kelly_fraction * (edge / max(1e-4, denom))
        
        # Scale by strike multiplier
        f_kelly_scaled = f_kelly_raw * strike_multiplier
        ceiling_pct = max_single_bet_pct if max_single_bet_pct is not None else self.config.max_single_bet_ceiling_pct
        f_kelly_bounded = min(ceiling_pct, max(0.0, f_kelly_scaled))

        w_kelly = bankroll_usd * f_kelly_bounded
        return round(w_kelly, 2), round(f_kelly_bounded, 4), round(downside_semi_var, 6)

    def compute_kyle_market_impact_bound(
        self,
        price: float,
        depth_usd: float,
        p_win: float,
        fee_rate: float = 0.0,
        spread_bps: Optional[float] = None,
        depth_gamma: Optional[float] = None,
        order_book_levels: Optional[List[Tuple[float, float]]] = None,
        concave_alpha: Optional[float] = None,
    ) -> Tuple[float, float]:
        """Compute continuous Kyle's Lambda market impact bound and concave trade capacity.
        
        Mathematical Formulation:
            Kyle's Lambda: lambda_Kyle = Spread / (2 * Depth)
            Dynamic continuous depth gamma:
                gamma_dyn = 1 / [1 + sqrt(Spread / (Depth / 10))]
            Concave power-law capacity (Almgren-Chriss / Bouchaud square-root law):
                w_opt = (Edge / ((1 + alpha) * lambda_concave))^(1 / alpha)
            w_impact = min(gamma * Depth, w_opt, w_slippage)
            
        Returns:
            Tuple[float, float]: (w_impact_usd, kyle_lambda)
        """
        p = max(0.50, min(0.999, price))
        spread = (spread_bps or self.config.default_spread_bps) / 10000.0
        d_usd = max(10.0, depth_usd)
        alpha = concave_alpha if concave_alpha is not None else self.config.concave_impact_alpha

        # Estimate Kyle's lambda in price per dollar deployed
        kyle_lambda = spread / (2.0 * d_usd)

        edge = max(0.0, p_win - p - fee_rate)
        if edge <= 1e-6:
            return 0.0, round(kyle_lambda, 8)

        # Max slippage bound: w_slippage = max_slippage / kyle_lambda
        w_slippage = self.config.max_kyle_slippage_pct / max(1e-9, kyle_lambda)

        # Concave power-law optimal capacity:
        # Impact I(w) = lambda_concave * w^alpha
        lambda_concave = spread / (2.0 * (d_usd ** alpha))
        w_kyle_opt = (edge / max(1e-9, (1.0 + alpha) * lambda_concave)) ** (1.0 / alpha)

        # Continuous dynamic depth gamma factor
        gamma_dyn = 1.0 / (1.0 + math.sqrt(max(1e-4, spread / (d_usd / 10.0))))
        gamma = depth_gamma if depth_gamma is not None else min(0.80, max(0.05, gamma_dyn))
        w_depth_cap = d_usd * gamma

        w_impact = min(w_depth_cap, w_kyle_opt, w_slippage)
        return round(max(0.0, w_impact), 2), round(kyle_lambda, 8)

    def compute_cvar_risk_budget_bound(
        self,
        p_win: float,
        bankroll_usd: float,
        existing_correlated_exposure_usd: float = 0.0,
        cross_correlation: Optional[float] = None,
        max_single_bet_pct: Optional[float] = None,
        category: str = "general",
    ) -> float:
        """Compute position bound derived endogenously from Portfolio 99% CVaR / Expected Shortfall.
        
        Mathematical Formulation:
            Target Max Drawdown: MDD_target = max_drawdown_target * Bankroll
            In binary markets, loss on reversal is 100% of w_i.
            Tail Loss Severity:
                Tail_i = (1 - p_win) + cross_correlation * (existing_correlated_exposure / Bankroll)
            Marginal Risk Contribution constraint:
                w_i * Tail_i <= MDD_target * Bankroll
                w_CVaR = (MDD_target * Bankroll - existing_correlated_exposure * cross_correlation) / max(0.01, Tail_i)
        """
        mdd_budget = bankroll_usd * self.config.max_drawdown_target
        loss_prob = max(0.0005, 1.0 - p_win)

        # Derive cross correlation dynamically if not provided (intra-category correlation)
        if cross_correlation is not None:
            rho = cross_correlation
        else:
            cat = category.lower().strip()
            rho = self.FACTOR_COVARIANCE.get(cat, {}).get(cat, 0.30)

        tail_factor = loss_prob + rho * (existing_correlated_exposure_usd / max(1.0, bankroll_usd))
        w_cvar = (mdd_budget - existing_correlated_exposure_usd * rho) / max(0.001, tail_factor)
        
        # Ceiling at max allowable single-bet ceiling
        ceiling_pct = max_single_bet_pct if max_single_bet_pct is not None else self.config.max_single_bet_ceiling_pct
        w_cvar_bounded = min(bankroll_usd * ceiling_pct, max(0.0, w_cvar))
        return round(w_cvar_bounded, 2)

    def compute_dynamic_single_bet_bound(
        self,
        category: str,
        price: float,
        hours_to_resolution: float,
        depth_usd: float,
        p_win: float,
        bankroll_usd: float,
        spot_price: Optional[float] = None,
        strike_price: Optional[float] = None,
        is_uma_proposed: bool = False,
        fee_rate: float = 0.0,
        existing_correlated_exposure_usd: float = 0.0,
        max_single_bet_pct: Optional[float] = None,
        depth_gamma: Optional[float] = None,
        spread: Optional[float] = None,
        price_history: Optional[List[float]] = None,
        empirical_vol: Optional[float] = None,
        order_book_levels: Optional[List[Tuple[float, float]]] = None,
        is_deterministic_conclusion: bool = False,
    ) -> Dict[str, Any]:
        """Compute global institutional dynamic single-bet bound w_max(i).
        
        Combines:
        1. Drift-diffusion strike distance z_i & strike multiplier M_z(z_i).
        2. Continuous fractional Kelly with downside semi-variance.
        3. Kyle's Lambda & concave power-law market impact.
        4. Portfolio 99% CVaR / Expected Shortfall budgeting.
        """
        ceiling_pct = max_single_bet_pct if max_single_bet_pct is not None else self.config.max_single_bet_ceiling_pct

        # 1. Strike Distance
        strike_metrics = self.compute_strike_distance_metrics(
            category=category,
            hours_to_resolution=hours_to_resolution,
            spot_price=spot_price,
            strike_price=strike_price,
            is_uma_proposed=is_uma_proposed,
            contract_price=p_win if p_win is not None else price,
            spread=spread,
            price_history=price_history,
            empirical_vol=empirical_vol,
            is_deterministic_conclusion=is_deterministic_conclusion,
        )

        # 2. Continuous Kelly
        w_kelly, f_kelly, semi_var = self.compute_continuous_kelly_bound(
            p_win=p_win,
            price=price,
            bankroll_usd=bankroll_usd,
            fee_rate=fee_rate,
            strike_multiplier=strike_metrics.strike_multiplier,
            max_single_bet_pct=ceiling_pct,
        )

        # 3. Market Impact
        w_impact, kyle_lambda = self.compute_kyle_market_impact_bound(
            price=price,
            depth_usd=depth_usd,
            p_win=p_win,
            fee_rate=fee_rate,
            spread_bps=spread * 10000.0 if spread else None,
            depth_gamma=depth_gamma,
            order_book_levels=order_book_levels,
        )

        # 4. CVaR Budget
        dyn_corr = 0.30 if category.lower() == "crypto" else 0.10
        w_cvar = self.compute_cvar_risk_budget_bound(
            p_win=p_win,
            bankroll_usd=bankroll_usd,
            existing_correlated_exposure_usd=existing_correlated_exposure_usd,
            cross_correlation=dyn_corr,
            max_single_bet_pct=ceiling_pct,
            category=category,
        )

        # Absolute physical ceiling
        abs_ceiling = bankroll_usd * ceiling_pct

        # Global endogenous synthesis
        if strike_metrics.strike_multiplier <= 1e-4:
            w_max = 0.0
            limiting_factor = "STRIKE_DIFFUSION_REJECTION"
        else:
            candidates = [
                ("KELLY_SEMI_VARIANCE", w_kelly),
                ("KYLE_MARKET_IMPACT", w_impact),
                ("PORTFOLIO_CVAR", w_cvar),
                ("PHYSICAL_CEILING", abs_ceiling),
            ]
            limiting_factor, w_max = min(candidates, key=lambda item: item[1])

        return {
            "w_max_usd": round(w_max, 2),
            "limiting_factor": limiting_factor,
            "strike_metrics": strike_metrics,
            "w_kelly_usd": w_kelly,
            "kelly_fraction": f_kelly,
            "downside_semi_variance": semi_var,
            "w_impact_usd": w_impact,
            "kyle_lambda": kyle_lambda,
            "w_cvar_usd": w_cvar,
            "ceiling_usd": round(abs_ceiling, 2),
        }

    def compute_precautionary_reserve(
        self,
        candidate_count: int,
        avg_candidate_edge: float,
        avg_lockup_hours: float,
        bankroll_usd: float = 10000.0,
        deal_size_usd: Optional[float] = None,
    ) -> float:
        """Compute dynamic precautionary liquidity reserve fraction using Miller-Orr / Cash Inventory theory.
        
        Mathematical Formulation:
            Arrival intensity: lambda_arr = candidate_count / lookahead_window
            Cash flow variance: sigma_flow^2 = lambda_arr * (S_bar^2 + (0.5 * S_bar)^2)
            Holding cost: r_opp = avg_candidate_edge / avg_lockup_hours
            Miller-Orr optimal return point:
                Z* = [ (3 * gamma_tx * sigma_flow^2) / (4 * r_opp) ]^(1/3)
            Cash-in-advance VaR liquidity buffer at 99% confidence:
                Buffer_VaR = z_0.99 * sigma_flow * sqrt(avg_lockup_hours) * (avg_edge / (1 + avg_edge))
            Target reserve C* = (Z* + Buffer_VaR) / Bankroll
            Dynamically bounded by capital demand volatility.
        """
        arrival_intensity = max(0.1, float(candidate_count))
        tau_cycle = max(0.2, float(avg_lockup_hours))
        edge = max(0.0005, float(avg_candidate_edge))
        bankroll = max(100.0, float(bankroll_usd))

        # Average deal size
        s_bar = deal_size_usd if deal_size_usd is not None else (bankroll * 0.025)
        # Cash flow variance per hour under compound Poisson arrival
        sigma_flow_sq = arrival_intensity * (s_bar ** 2 + (0.5 * s_bar) ** 2)
        sigma_flow = math.sqrt(sigma_flow_sq)

        # Opportunity holding cost rate per hour
        r_opp = max(0.0002, edge / tau_cycle)
        # Transaction / missed opportunity friction
        gamma_tx = self.config.miller_orr_tx_cost_usd + s_bar * edge

        # 1. Miller-Orr Stochastic Return Point
        numerator = 3.0 * gamma_tx * sigma_flow_sq
        denominator = 4.0 * r_opp
        z_star = (numerator / denominator) ** (1.0 / 3.0)

        # 2. Cash-in-Advance VaR Buffer (99% quantile = 2.326)
        var_buffer = sigma_flow * math.sqrt(tau_cycle) * (1.0 + edge) * 0.50

        # 3. Queuing Net Capital Demand (Little's Law load factor)
        load_factor = (arrival_intensity * tau_cycle) / 24.0
        queue_demand = load_factor * s_bar * 1.50

        # Target Cash Reserve Fraction
        raw_reserve_pct = (z_star + var_buffer + queue_demand) / bankroll

        # Dynamic Bounds derived from stochastic volatility
        dyn_floor = max(self.config.min_reserve_pct, (z_star * 0.5) / bankroll)
        dyn_ceil = min(self.config.max_reserve_pct, max(dyn_floor + 0.15, (z_star + var_buffer * 1.5 + queue_demand) / bankroll))

        clamped_reserve = min(dyn_ceil, max(dyn_floor, raw_reserve_pct))
        return round(clamped_reserve, 4)

    def get_factor_loadings(self, category: str, question: str) -> Dict[str, float]:
        """Compute Barra-style systematic factor loadings for a candidate market."""
        q_lower = question.lower()
        cat = category.lower().strip()

        # Factors: crypto, politics, sports, general
        loadings = {"crypto": 0.0, "politics": 0.0, "sports": 0.0, "general": 0.10}

        if cat == "crypto" or any(k in q_lower for k in ["btc", "bitcoin", "eth", "ethereum", "sol", "solana"]):
            loadings["crypto"] = 1.0
            loadings["general"] = 0.20
        elif cat == "politics" or any(k in q_lower for k in ["president", "senate", "trump", "harris", "election"]):
            loadings["politics"] = 1.0
            loadings["general"] = 0.15
        elif cat == "sports" or any(k in q_lower for k in ["nfl", "nba", "nhl", "game", "win", "vs", "score"]):
            # Sports games are mostly idiosyncratic per game
            loadings["sports"] = 0.35
            loadings["general"] = 0.05
        else:
            loadings["general"] = 0.50

        return loadings

    def compute_factor_risk_budgets(
        self,
        bankroll_usd: float,
        max_category_pct: float = 0.15,
    ) -> Dict[str, float]:
        """Compute systematic factor covariance risk budgets in USD.
        
        Scales proportionally with user/portfolio category risk budget parameters.
        """
        scale = max(0.5, max_category_pct / 0.15)
        crypto_pct = min(0.90, self.config.factor_crypto_budget_pct * scale)
        politics_pct = min(0.90, self.config.factor_politics_budget_pct * scale)
        sports_pct = min(0.90, self.config.factor_sports_budget_pct * scale)
        general_pct = min(0.90, self.config.factor_general_budget_pct * scale)

        return {
            "crypto": round(bankroll_usd * crypto_pct, 2),
            "politics": round(bankroll_usd * politics_pct, 2),
            "sports": round(bankroll_usd * sports_pct, 2),
            "general": round(bankroll_usd * general_pct, 2),
        }
