"""Institutional Dynamic Inventory-Skewed Market Making Engine for Polymarket Binaries.

Implements the classical Avellaneda-Stoikov (2008) and Guéant-Tapia-Manziadi (2012)
optimal market making framework, specialized for Polymarket binary options and quadratic
liquidity rewards.

Key Theoretical Foundations:
1. Classical Avellaneda-Stoikov (2008) & Guéant-Tapia-Manziadi (2012):
   - Continuous reservation price:
       r(s, q, tau) = s - q * gamma * sigma^2 * tau
     where:
       s     = fair binary value / spot probability in [0, 1]
       q     = net inventory (q_YES - q_NO)
       gamma = absolute risk aversion parameter (CARA utility u(x) = -exp(-gamma * x))
       sigma = volatility per square root second
       tau   = remaining time horizon to expiry (seconds)
   - Guéant-Tapia-Manziadi closed-form optimal half-spreads (exponential arrival intensity lambda(delta) = A * exp(-kappa * delta)):
       delta_0 = (1 / gamma) * ln(1 + gamma / kappa)
       delta^b(q) = delta_0 + ((2q + 1) / 2) * gamma * sigma^2 * tau
       delta^a(q) = delta_0 - ((2q - 1) / 2) * gamma * sigma^2 * tau
   - Directional Skewing Mechanics:
     When long YES (q > 0):
       delta^b widens  -> P_bid(YES) drops lower to avoid buying additional YES
       delta^a tightens -> P_ask(YES) drops closer to mid to attract takers and offload YES
     Simultaneously on the complementary NO token (where q_NO,net = -q):
       P_bid(NO) tightens (rises) -> attracts sellers of NO, accumulating NO to complete sets!
       P_ask(NO) widens (rises)  -> avoids selling NO.
     Every quote pushes net inventory back towards balanced neutrality (q_net = 0).

2. Binary Complete-Set Adaptation & Pair Arbitrage Bound:
   - In binary prediction markets, 1 YES + 1 NO = $1.00 at expiry.
   - Holding balanced pairs (q_YES = q_NO) carries zero directional price risk.
   - Structural Pair Arbitrage Constraint:
       P_bid(YES) + P_bid(NO) <= 1.00 - target_pair_margin (e.g. $0.985 for >= 1.5c margin)
       P_ask(YES) + P_ask(NO) >= 1.00 + target_pair_margin (e.g. $1.015)
     guaranteeing strictly positive settlement or synthetic merge profits.

3. Polymarket Quadratic Liquidity Rewards Co-Objective:
   - Maker quotes within qualifying spread v earn reward score:
       S(delta, size) = (1 - delta / v)^2 * size   for 0 <= delta <= v, else 0
   - Joint Objective Function:
       Maximize: Expected Fill Spread PnL + Liquidity Rewards Yield
                 - Inventory Variance Penalty - Adverse Selection Risk
   - Maintains balanced two-sided quotes (Q_one ~= Q_two) to avoid Polymarket's
     c = 3.0 single-sided penalty inside [0.10, 0.90] and zero score outside.

4. Dynamic Size Skewing:
   - Quotes size scales inversely with directional inventory exposure:
       size_ask(YES) = base * (1 + tanh(q / q_scale))
       size_bid(YES) = base * (1 - tanh(q / q_scale))
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("as_inventory_engine")

SECONDS_PER_YEAR = 31_557_600.0  # 365.25 * 86,400


class InventoryRegime(str, Enum):
    """Classification of inventory imbalance urgency."""
    BALANCED = "BALANCED"                  # |q| <= 2 shares
    MILD_SKEW = "MILD_SKEW"                # 2 < |q| <= 10 shares
    HEAVY_SKEW = "HEAVY_SKEW"              # 10 < |q| <= 25 shares
    CRITICAL_DEFENSE = "CRITICAL_DEFENSE"  # |q| > 25 shares


class QuoteSide(str, Enum):
    """Side of order book."""
    BID = "BID"
    ASK = "ASK"


@dataclass
class ASInventoryConfig:
    """Configuration parameters for Avellaneda-Stoikov Inventory Quoting Engine."""
    gamma: float = 0.05                    # Absolute risk aversion parameter gamma
    kappa: float = 50.0                    # Order arrival intensity decay parameter kappa (~2c half-spread)
    order_arrival_intensity_A: float = 1.5 # Baseline order arrival rate A (orders/sec)
    volatility_annual: float = 0.55        # Default annualized volatility sigma
    target_pair_margin: float = 0.015      # Minimum locked-in pair margin ($0.015 = 1.5c)
    reward_weight_rho: float = 0.005       # Liquidity rewards economic weight in utility function
    max_qualifying_spread_v: float = 0.03  # Polymarket max qualifying spread threshold (3c)
    adverse_selection_alpha: float = 0.0005 # Adverse selection cost scaling factor
    tick_size: float = 0.01                # CLOB price tick size (e.g. 1c)
    min_price: float = 0.01                # Minimum allowable limit price
    max_price: float = 0.99                # Maximum allowable limit price
    base_ticket_size: float = 5.0          # Base maker order ticket size in shares
    min_ticket_size: float = 1.0           # Minimum ticket size floor
    max_ticket_size: float = 50.0          # Maximum ticket size ceiling
    skew_scale_q: float = 20.0             # Inventory scaling constant for dynamic sizing
    rewards_single_sided_c: float = 3.0    # Polymarket single-sided penalty factor c inside [0.10, 0.90]


@dataclass(frozen=True)
class QuotePair:
    """Maker quote pair (bid and ask) for a single token (YES or NO)."""
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    delta_bid: float                       # Half-spread of bid from mid
    delta_ask: float                       # Half-spread of ask from mid
    reward_score_bid: float                # Quadratic reward score for bid
    reward_score_ask: float                # Quadratic reward score for ask


@dataclass(frozen=True)
class RewardScoreBreakdown:
    """Detailed liquidity reward scoring metrics across YES and NO order books."""
    q_one: float                           # Q_one score (YES bid + NO ask)
    q_two: float                           # Q_two score (YES ask + NO bid)
    q_min: float                           # Official qualifying score after single-sided penalty
    is_two_sided_qualified: bool
    penalty_applied: bool


@dataclass(frozen=True)
class ASQuoteDecision:
    """Actionable institutional maker quoting decision for a binary market."""
    fair_price_yes: float
    fair_price_no: float
    reservation_price_yes: float
    reservation_price_no: float
    net_inventory: float                   # q_net = q_YES - q_NO
    yes_quote: QuotePair
    no_quote: QuotePair
    pair_bid_cost: float                   # P_bid(YES) + P_bid(NO)
    pair_bid_margin_cents: float           # (1.00 - pair_bid_cost) * 100
    pair_ask_cost: float                   # P_ask(YES) + P_ask(NO)
    pair_ask_margin_cents: float           # (pair_ask_cost - 1.00) * 100
    reward_breakdown: RewardScoreBreakdown
    is_pair_arbitrage_guaranteed: bool
    inventory_regime: InventoryRegime
    reason: str


class DynamicASCalibrator:
    """Dynamic calibration of Avellaneda-Stoikov parameters (gamma, kappa, A).

    Replaces static/hardcoded parameters with state-of-the-art institutional prop firm methods.
    """

    @staticmethod
    def calibrate_gamma(
        capital_usd: float,
        target_sharpe: float = 2.0,
        volatility_annual: float = 0.55,
        risk_scale: float = 25.0,
    ) -> float:
        """Dynamically scales absolute risk aversion gamma with portfolio capital and target Sharpe.

        Formula:
            gamma = (risk_scale / max(10.0, capital)) * (target_sharpe / max(0.10, volatility_annual))
        """
        c = max(10.0, float(capital_usd))
        vol = max(0.10, float(volatility_annual))
        sr = max(0.1, float(target_sharpe))
        gamma = (risk_scale / c) * (sr / vol)
        return max(0.001, min(1.0, gamma))

    @staticmethod
    def calibrate_kappa_from_depth(
        spread: float,
        l1_size: float = 100.0,
        l2_size: float = 50.0,
        tick_size: float = 0.01,
    ) -> float:
        """Dynamically calibrates liquidity decay parameter kappa from order book depth profile.

        In LOB depth models, depth density decays as Q(delta) ~ exp(-kappa * delta).
        kappa = ln(Q_1 / Q_2) / delta_distance
        """
        q1 = max(1.0, float(l1_size))
        q2 = max(1.0, float(l2_size))
        s = max(tick_size, float(spread))

        if q1 > q2:
            kappa_from_decay = math.log(q1 / q2) / tick_size
        else:
            kappa_from_decay = 2.0 / s

        kappa_from_spread = 2.0 / s
        kappa = 0.5 * (kappa_from_decay + kappa_from_spread)
        return max(5.0, min(200.0, kappa))

    @staticmethod
    def calibrate_intensity_A(
        trade_rate_per_sec: float,
        fill_ratio_at_touch: float = 0.5,
    ) -> float:
        """Dynamically estimates baseline arrival intensity A at touch from trade velocity."""
        rate = max(0.01, float(trade_rate_per_sec))
        fill_p = max(0.1, min(1.0, float(fill_ratio_at_touch)))
        a = rate / fill_p
        return max(0.1, min(20.0, a))

    @staticmethod
    def calibrate_from_order_arrivals(
        deltas: list[float],
        fill_counts: list[int],
        total_time_seconds: float,
    ) -> Tuple[float, float]:
        """Calibrates (A, kappa) via log-linear regression: ln(lambda) = ln(A) - kappa * delta.

        Args:
            deltas: List of half-spread distances from mid.
            fill_counts: Observed execution counts at each distance.
            total_time_seconds: Duration of observation window.

        Returns:
            Tuple of (estimated_A, estimated_kappa).
        """
        if len(deltas) < 2 or len(deltas) != len(fill_counts) or total_time_seconds <= 0.0:
            return 1.5, 50.0

        valid_points = []
        for d, c in zip(deltas, fill_counts):
            if c > 0:
                lam = float(c) / total_time_seconds
                valid_points.append((d, math.log(lam)))

        if len(valid_points) < 2:
            return 1.5, 50.0

        n = len(valid_points)
        sum_x = sum(p[0] for p in valid_points)
        sum_y = sum(p[1] for p in valid_points)
        sum_xx = sum(p[0] ** 2 for p in valid_points)
        sum_xy = sum(p[0] * p[1] for p in valid_points)

        denom = n * sum_xx - sum_x ** 2
        if abs(denom) <= 1e-9:
            return 1.5, 50.0

        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n

        kappa = max(5.0, min(200.0, -slope))
        a = max(0.1, min(50.0, math.exp(min(10.0, intercept))))
        return a, kappa


class AvellanedaStoikovEngine:
    """Avellaneda-Stoikov & Guéant-Tapia-Manziadi Dynamic Inventory Market Making Engine.

    Fully adapts continuous stochastic control market making to Polymarket binary options,
    enforcing structural pair arbitrage bounds and optimizing quadratic liquidity rewards.
    """

    def __init__(self, config: Optional[ASInventoryConfig] = None) -> None:
        self.config = config or ASInventoryConfig()
        self.calibrator = DynamicASCalibrator()

    # =========================================================================
    # 1. Classical Avellaneda-Stoikov & Guéant-Tapia-Manziadi Analytical Core
    # =========================================================================

    def calculate_sigma_per_sqrt_sec(self, annualized_volatility: Optional[float] = None) -> float:
        """Calculate volatility per square root second from annualized volatility."""
        vol = annualized_volatility if annualized_volatility is not None else self.config.volatility_annual
        return max(1e-6, vol / math.sqrt(SECONDS_PER_YEAR))

    def calculate_reservation_price(
        self,
        fair_price: float,
        net_inventory_q: float,
        tau_seconds: float,
        sigma_per_sqrt_sec: Optional[float] = None,
        gamma: Optional[float] = None,
    ) -> float:
        """Compute the continuous Avellaneda-Stoikov reservation (indifference) price.

        Formula:
            r(s, q, tau) = s - q * gamma * sigma^2 * tau

        Args:
            fair_price: Fair binary probability / spot price s in (0, 1).
            net_inventory_q: Net inventory q = q_YES - q_NO.
            tau_seconds: Time to expiry horizon tau in seconds.
            sigma_per_sqrt_sec: Volatility per square root second.
            gamma: Absolute risk aversion parameter.

        Returns:
            Reservation price r(s, q, tau).
        """
        g = gamma if gamma is not None else self.config.gamma
        sig_sec = sigma_per_sqrt_sec if sigma_per_sqrt_sec is not None else self.calculate_sigma_per_sqrt_sec()
        tau = max(0.01, float(tau_seconds))

        variance_term = (sig_sec ** 2) * tau
        reservation = fair_price - (net_inventory_q * g * variance_term)
        return max(self.config.min_price, min(self.config.max_price, reservation))

    def calculate_gtm_half_spreads(
        self,
        net_inventory_q: float,
        tau_seconds: float,
        sigma_per_sqrt_sec: Optional[float] = None,
        gamma: Optional[float] = None,
        kappa: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """Compute Guéant-Tapia-Manziadi (2012) closed-form optimal half-spreads.

        Formulas:
            delta_0 = (1 / gamma) * ln(1 + gamma / kappa)
            delta^b(q) = delta_0 + ((2q + 1) / 2) * gamma * sigma^2 * tau
            delta^a(q) = delta_0 - ((2q - 1) / 2) * gamma * sigma^2 * tau

        When q > 0:
            delta^b widens (drops bid lower to avoid buying more)
            delta^a tightens (moves ask closer to mid to induce takers to buy)

        Returns:
            Tuple of (delta_bid, delta_ask, delta_0).
        """
        g = gamma if gamma is not None else self.config.gamma
        k = kappa if kappa is not None else self.config.kappa
        sig_sec = sigma_per_sqrt_sec if sigma_per_sqrt_sec is not None else self.calculate_sigma_per_sqrt_sec()
        tau = max(0.01, float(tau_seconds))

        # Baseline spread delta_0 with robust L'Hopital limit for small gamma
        if g <= 1e-7:
            delta_0 = 1.0 / max(1e-6, k)
        else:
            delta_0 = (1.0 / g) * math.log(1.0 + (g / max(1e-6, k)))

        variance_term = (sig_sec ** 2) * tau
        skew_term_bid = 0.5 * (2.0 * net_inventory_q + 1.0) * g * variance_term
        skew_term_ask = 0.5 * (2.0 * net_inventory_q - 1.0) * g * variance_term

        delta_bid = delta_0 + skew_term_bid
        delta_ask = delta_0 - skew_term_ask

        # Floor half-spreads to prevent crossed or degenerate quotes
        min_half_spread = 0.5 * self.config.tick_size
        delta_bid = max(min_half_spread, delta_bid)
        delta_ask = max(min_half_spread, delta_ask)

        return delta_bid, delta_ask, delta_0

    def calculate_raw_as_quotes(
        self,
        fair_price: float,
        net_inventory_q: float,
        tau_seconds: float,
        sigma_per_sqrt_sec: Optional[float] = None,
        gamma: Optional[float] = None,
        kappa: Optional[float] = None,
    ) -> Tuple[float, float, float, float]:
        """Calculate continuous Avellaneda-Stoikov bid and ask prices from fair value.

        Returns:
            (raw_bid, raw_ask, delta_bid, delta_ask)
        """
        delta_bid, delta_ask, _ = self.calculate_gtm_half_spreads(
            net_inventory_q=net_inventory_q,
            tau_seconds=tau_seconds,
            sigma_per_sqrt_sec=sigma_per_sqrt_sec,
            gamma=gamma,
            kappa=kappa,
        )
        raw_bid = fair_price - delta_bid
        raw_ask = fair_price + delta_ask

        # Ensure ask strictly greater than bid
        if raw_ask <= raw_bid:
            raw_ask = raw_bid + self.config.tick_size

        return raw_bid, raw_ask, delta_bid, delta_ask

    # =========================================================================
    # 2. Polymarket Quadratic Liquidity Rewards Co-Objective
    # =========================================================================

    @staticmethod
    def calculate_quadratic_reward_score(
        delta_spread: float,
        max_qualifying_spread_v: float,
        size: float = 1.0,
    ) -> float:
        """Calculate Polymarket quadratic liquidity reward score for an order.

        S(delta, size) = ((v - delta) / v)^2 * size   if 0 <= delta <= v, else 0.
        """
        if delta_spread < 0.0 or delta_spread > max_qualifying_spread_v or max_qualifying_spread_v <= 0.0:
            return 0.0
        ratio = (max_qualifying_spread_v - delta_spread) / max_qualifying_spread_v
        return (ratio ** 2) * max(0.0, size)

    def evaluate_joint_utility(
        self,
        delta: float,
        side: QuoteSide,
        net_inventory_q: float,
        tau_seconds: float,
        sigma_per_sqrt_sec: Optional[float] = None,
        quote_size: float = 1.0,
    ) -> float:
        """Evaluate the joint continuous objective function for quoting half-spread delta.

        Objective:
            U(delta) = lambda(delta) * [ delta - c_adv - delta_R(q) ] + rho * S(delta)

        where:
            lambda(delta) = A * exp(-kappa * delta)
            delta_R(q) = ((2q + 1) / 2) * gamma * sigma^2 * tau   for BID
            delta_R(q) = -((2q - 1) / 2) * gamma * sigma^2 * tau  for ASK
            S(delta)   = (1 - delta / v)^2 * quote_size
        """
        if delta <= 0.0:
            return -1e9

        g = self.config.gamma
        k = self.config.kappa
        a = self.config.order_arrival_intensity_A
        rho = self.config.reward_weight_rho
        v = self.config.max_qualifying_spread_v
        c_adv = self.config.adverse_selection_alpha
        sig_sec = sigma_per_sqrt_sec if sigma_per_sqrt_sec is not None else self.calculate_sigma_per_sqrt_sec()
        tau = max(0.01, float(tau_seconds))

        var_term = (sig_sec ** 2) * tau
        if side == QuoteSide.BID:
            inv_penalty = 0.5 * (2.0 * net_inventory_q + 1.0) * g * var_term
        else:
            inv_penalty = -0.5 * (2.0 * net_inventory_q - 1.0) * g * var_term

        # 1. Guéant-Tapia-Manziadi expected utility rate per unit time
        fill_intensity = a * math.exp(-k * delta)
        net_spread = delta - c_adv - inv_penalty

        if abs(g) <= 1e-7:
            pnl_utility = net_spread
        else:
            # Robust CARA utility: (1 - exp(-gamma * net_spread)) / gamma
            exp_term = math.exp(max(-20.0, min(20.0, -g * net_spread)))
            pnl_utility = (1.0 - exp_term) / g

        expected_fill_utility = fill_intensity * pnl_utility * quote_size

        # 2. Liquidity rewards yield rate
        reward_score = self.calculate_quadratic_reward_score(delta, v, size=quote_size)
        reward_yield = rho * reward_score

        return expected_fill_utility + reward_yield

    def optimize_spread_with_rewards(
        self,
        side: QuoteSide,
        net_inventory_q: float,
        tau_seconds: float,
        sigma_per_sqrt_sec: Optional[float] = None,
        quote_size: float = 1.0,
    ) -> float:
        """Find the optimal half-spread maximizing the joint execution + rewards utility.

        Evaluates reward-harvesting candidates inside [min_spread, v] and unconstrained
        candidates outside v, choosing the global utility-maximizing quote spread.
        """
        # Baseline AS-GTM half-spread
        delta_bid_as, delta_ask_as, _ = self.calculate_gtm_half_spreads(
            net_inventory_q=net_inventory_q,
            tau_seconds=tau_seconds,
            sigma_per_sqrt_sec=sigma_per_sqrt_sec,
        )
        anchor_delta = delta_bid_as if side == QuoteSide.BID else delta_ask_as

        min_spread = 0.5 * self.config.tick_size
        v = self.config.max_qualifying_spread_v

        def obj(delta_val: float) -> float:
            return self.evaluate_joint_utility(
                delta=delta_val,
                side=side,
                net_inventory_q=net_inventory_q,
                tau_seconds=tau_seconds,
                sigma_per_sqrt_sec=sigma_per_sqrt_sec,
                quote_size=quote_size,
            )

        # 1. Optimize inside reward qualifying zone [min_spread, min(v, anchor_delta)]
        upper_bound_in = min(v, max(min_spread * 2.0, anchor_delta))
        gr = (math.sqrt(5.0) - 1.0) / 2.0
        a = min_spread
        b = upper_bound_in

        c = b - gr * (b - a)
        d = a + gr * (b - a)
        f_c = obj(c)
        f_d = obj(d)

        for _ in range(16):
            if f_c > f_d:
                b = d
                d = c
                f_d = f_c
                c = b - gr * (b - a)
                f_c = obj(c)
            else:
                a = c
                c = d
                f_c = f_d
                d = a + gr * (b - a)
                f_d = obj(d)

        best_delta_in = 0.5 * (a + b)
        util_in = obj(best_delta_in)

        # 2. Compare against anchor / unconstrained spread if anchor > v
        if anchor_delta > v:
            util_anchor = obj(anchor_delta)
            if util_anchor > util_in:
                return max(min_spread, anchor_delta)

        return max(min_spread, best_delta_in)

    # =========================================================================
    # 3. Dynamic Size Skewing & Inventory Sizing
    # =========================================================================

    def calculate_dynamic_quote_sizes(
        self,
        net_inventory_q: float,
    ) -> Tuple[float, float, float, float]:
        """Compute inventory-skewed ticket sizes for YES and NO quotes.

        When net inventory q > 0 (excess YES):
            - YES bid size contracts (reduce YES accumulation)
            - YES ask size expands (increase YES offloading)
            - NO bid size expands (increase NO accumulation to complete sets!)
            - NO ask size contracts (reduce NO selling)

        Returns:
            Tuple of (size_bid_yes, size_ask_yes, size_bid_no, size_ask_no)
        """
        base = self.config.base_ticket_size
        scale = max(1.0, self.config.skew_scale_q)
        q_ratio = net_inventory_q / scale

        # Hyperbolic tangent smooth dampener in [-1.0, +1.0]
        tanh_factor = math.tanh(q_ratio)

        # YES sizes
        size_bid_yes = base * max(0.20, 1.0 - tanh_factor)
        size_ask_yes = base * min(2.50, 1.0 + tanh_factor)

        # Complementary NO sizes (where q_NO,net = -q)
        size_bid_no = base * min(2.50, 1.0 + tanh_factor)
        size_ask_no = base * max(0.20, 1.0 - tanh_factor)

        # Clamp within config boundaries
        c_min = self.config.min_ticket_size
        c_max = self.config.max_ticket_size

        return (
            round(max(c_min, min(c_max, size_bid_yes)), 1),
            round(max(c_min, min(c_max, size_ask_yes)), 1),
            round(max(c_min, min(c_max, size_bid_no)), 1),
            round(max(c_min, min(c_max, size_ask_no)), 1),
        )

    # =========================================================================
    # 4. Binary Complete-Set Adaptation & Pair Arbitrage Bound Enforcer
    # =========================================================================

    def enforce_complete_set_arbitrage_bounds(
        self,
        bid_yes: float,
        bid_no: float,
        ask_yes: float,
        ask_no: float,
        net_inventory_q: float,
        target_pair_margin: Optional[float] = None,
    ) -> Tuple[float, float, float, float]:
        """Enforce strict structural pair arbitrage bounds and discrete tick rounding.

        Guarantees:
            1. Dual Bid Cost: P_bid(YES) + P_bid(NO) <= 1.00 - target_pair_margin (e.g. 0.985)
            2. Dual Ask Cost: P_ask(YES) + P_ask(NO) >= 1.00 + target_pair_margin (e.g. 1.015)
            3. Price bounds in [min_price, max_price]
            4. Strict tick snapping
        """
        tick = self.config.tick_size
        margin = target_pair_margin if target_pair_margin is not None else self.config.target_pair_margin
        max_pair_bid = round(1.00 - margin, 4)
        min_pair_ask = round(1.00 + margin, 4)

        # Snap to tick grid
        b_yes = round(round(bid_yes / tick) * tick, 4)
        b_no = round(round(bid_no / tick) * tick, 4)
        a_yes = round(round(ask_yes / tick) * tick, 4)
        a_no = round(round(ask_no / tick) * tick, 4)

        # 1. Enforce Dual Bid Constraint: P_bid(YES) + P_bid(NO) <= max_pair_bid
        if round(b_yes + b_no, 4) > max_pair_bid:
            excess = round((b_yes + b_no) - max_pair_bid, 4)
            # Prioritize shaving the side that would worsen inventory
            if net_inventory_q > 0:
                # Excess YES: shave YES bid first
                shave_yes = min(b_yes - self.config.min_price, excess)
                b_yes = round(b_yes - shave_yes, 4)
                remaining = round(excess - shave_yes, 4)
                if remaining > 0:
                    b_no = round(b_no - remaining, 4)
            elif net_inventory_q < 0:
                # Excess NO: shave NO bid first
                shave_no = min(b_no - self.config.min_price, excess)
                b_no = round(b_no - shave_no, 4)
                remaining = round(excess - shave_no, 4)
                if remaining > 0:
                    b_yes = round(b_yes - remaining, 4)
            else:
                # Symmetric inventory: shave equally
                half_excess = round(round((excess / 2.0) / tick) * tick, 4)
                b_yes = round(b_yes - half_excess, 4)
                b_no = round(b_no - (excess - half_excess), 4)

        # Re-check after shaving
        while round(b_yes + b_no, 4) > max_pair_bid:
            if net_inventory_q >= 0 and b_yes > self.config.min_price:
                b_yes = round(b_yes - tick, 4)
            elif b_no > self.config.min_price:
                b_no = round(b_no - tick, 4)
            else:
                b_yes = round(b_yes - tick, 4)

        # 2. Enforce Dual Ask Constraint: P_ask(YES) + P_ask(NO) >= min_pair_ask
        if round(a_yes + a_no, 4) < min_pair_ask:
            deficit = round(min_pair_ask - (a_yes + a_no), 4)
            if net_inventory_q > 0:
                # Excess YES: raise NO ask first (preserve cheap YES ask to offload YES)
                raise_no = min(self.config.max_price - a_no, deficit)
                a_no = round(a_no + raise_no, 4)
                remaining = round(deficit - raise_no, 4)
                if remaining > 0:
                    a_yes = round(a_yes + remaining, 4)
            elif net_inventory_q < 0:
                # Excess NO: raise YES ask first
                raise_yes = min(self.config.max_price - a_yes, deficit)
                a_yes = round(a_yes + raise_yes, 4)
                remaining = round(deficit - raise_yes, 4)
                if remaining > 0:
                    a_no = round(a_no + remaining, 4)
            else:
                half_deficit = round(round((deficit / 2.0) / tick) * tick, 4)
                a_yes = round(a_yes + half_deficit, 4)
                a_no = round(a_no + (deficit - half_deficit), 4)

        while round(a_yes + a_no, 4) < min_pair_ask:
            if net_inventory_q <= 0 and a_yes < self.config.max_price:
                a_yes = round(a_yes + tick, 4)
            elif a_no < self.config.max_price:
                a_no = round(a_no + tick, 4)
            else:
                a_yes = round(a_yes + tick, 4)

        # 3. Final sanity checks: bid < ask on each token
        if a_yes <= b_yes:
            a_yes = round(b_yes + tick, 4)
        if a_no <= b_no:
            a_no = round(b_no + tick, 4)

        # 4. Clamp strictly within min_price and max_price
        b_yes = max(self.config.min_price, min(self.config.max_price, b_yes))
        b_no = max(self.config.min_price, min(self.config.max_price, b_no))
        a_yes = max(self.config.min_price, min(self.config.max_price, a_yes))
        a_no = max(self.config.min_price, min(self.config.max_price, a_no))

        return b_yes, b_no, a_yes, a_no

    # =========================================================================
    # 5. Full Evaluation Pipeline
    # =========================================================================

    def evaluate_market_quotes(
        self,
        fair_price_yes: float,
        inventory_yes: float = 0.0,
        inventory_no: float = 0.0,
        tau_seconds: float = 300.0,
        annualized_volatility: Optional[float] = None,
        use_rewards_optimization: bool = True,
        gamma: Optional[float] = None,
        kappa: Optional[float] = None,
        intensity_A: Optional[float] = None,
        target_pair_margin: Optional[float] = None,
    ) -> ASQuoteDecision:
        """Compute the full institutional Avellaneda-Stoikov & Guéant quoting decision.

        Supports dynamic risk aversion gamma, order arrival intensity decay kappa,
        and dynamic complete-set arbitrage margin bounds.

        Args:
            fair_price_yes: Fair binary probability of YES in (0, 1).
            inventory_yes: Quantity of YES shares held.
            inventory_no: Quantity of NO shares held.
            tau_seconds: Remaining time horizon in seconds.
            annualized_volatility: Volatility parameter (default from config).
            use_rewards_optimization: Whether to incorporate quadratic rewards co-objective.
            gamma: Optional dynamically calibrated CARA risk aversion parameter.
            kappa: Optional dynamically calibrated order arrival decay parameter.
            intensity_A: Optional dynamically calibrated arrival intensity at touch.
            target_pair_margin: Optional dynamically calibrated minimum locked pair margin.

        Returns:
            ASQuoteDecision with complete quotes, reservation prices, scores, and arbitrage guarantees.
        """
        # Clamp inputs
        p_yes = max(self.config.min_price, min(self.config.max_price, float(fair_price_yes)))
        p_no = round(1.0 - p_yes, 4)
        tau = max(0.1, float(tau_seconds))
        sig_sec = self.calculate_sigma_per_sqrt_sec(annualized_volatility)
        effective_gamma = gamma if gamma is not None else self.config.gamma
        effective_kappa = kappa if kappa is not None else self.config.kappa

        # Net directional inventory
        q_net = float(inventory_yes - inventory_no)

        # Classify inventory regime
        abs_q = abs(q_net)
        if abs_q <= 2.0:
            regime = InventoryRegime.BALANCED
        elif abs_q <= 10.0:
            regime = InventoryRegime.MILD_SKEW
        elif abs_q <= 25.0:
            regime = InventoryRegime.HEAVY_SKEW
        else:
            regime = InventoryRegime.CRITICAL_DEFENSE

        # 1. Continuous Reservation Prices
        r_yes = self.calculate_reservation_price(
            fair_price=p_yes,
            net_inventory_q=q_net,
            tau_seconds=tau,
            sigma_per_sqrt_sec=sig_sec,
            gamma=effective_gamma,
        )
        # For NO token, net inventory is -q_net
        r_no = self.calculate_reservation_price(
            fair_price=p_no,
            net_inventory_q=-q_net,
            tau_seconds=tau,
            sigma_per_sqrt_sec=sig_sec,
            gamma=effective_gamma,
        )

        # 2. Dynamic Ticket Sizes
        size_b_yes, size_a_yes, size_b_no, size_a_no = self.calculate_dynamic_quote_sizes(q_net)

        # 3. Compute Half-Spreads
        if use_rewards_optimization and self.config.reward_weight_rho > 0.0:
            delta_b_yes = self.optimize_spread_with_rewards(QuoteSide.BID, q_net, tau, sig_sec, size_b_yes)
            delta_a_yes = self.optimize_spread_with_rewards(QuoteSide.ASK, q_net, tau, sig_sec, size_a_yes)
            delta_b_no = self.optimize_spread_with_rewards(QuoteSide.BID, -q_net, tau, sig_sec, size_b_no)
            delta_a_no = self.optimize_spread_with_rewards(QuoteSide.ASK, -q_net, tau, sig_sec, size_a_no)
        else:
            delta_b_yes, delta_a_yes, _ = self.calculate_gtm_half_spreads(
                net_inventory_q=q_net,
                tau_seconds=tau,
                sigma_per_sqrt_sec=sig_sec,
                gamma=effective_gamma,
                kappa=effective_kappa,
            )
            delta_b_no, delta_a_no, _ = self.calculate_gtm_half_spreads(
                net_inventory_q=-q_net,
                tau_seconds=tau,
                sigma_per_sqrt_sec=sig_sec,
                gamma=effective_gamma,
                kappa=effective_kappa,
            )

        # Continuous prices
        raw_b_yes = p_yes - delta_b_yes
        raw_a_yes = p_yes + delta_a_yes
        raw_b_no = p_no - delta_b_no
        raw_a_no = p_no + delta_a_no

        # 4. Enforce Complete-Set Arbitrage Bounds & Tick Grid Snapping
        effective_margin = target_pair_margin if target_pair_margin is not None else self.config.target_pair_margin
        b_yes, b_no, a_yes, a_no = self.enforce_complete_set_arbitrage_bounds(
            bid_yes=raw_b_yes,
            bid_no=raw_b_no,
            ask_yes=raw_a_yes,
            ask_no=raw_a_no,
            net_inventory_q=q_net,
            target_pair_margin=effective_margin,
        )

        # Recalculate discrete post-snap half-spreads
        eff_delta_b_yes = round(p_yes - b_yes, 4)
        eff_delta_a_yes = round(a_yes - p_yes, 4)
        eff_delta_b_no = round(p_no - b_no, 4)
        eff_delta_a_no = round(a_no - p_no, 4)

        # 5. Polymarket Liquidity Rewards Scoring
        v = self.config.max_qualifying_spread_v
        score_b_yes = self.calculate_quadratic_reward_score(eff_delta_b_yes, v, size_b_yes)
        score_a_yes = self.calculate_quadratic_reward_score(eff_delta_a_yes, v, size_a_yes)
        score_b_no = self.calculate_quadratic_reward_score(eff_delta_b_no, v, size_b_no)
        score_a_no = self.calculate_quadratic_reward_score(eff_delta_a_no, v, size_a_no)

        # Q_one = YES bid + NO ask; Q_two = YES ask + NO bid
        q_one = round(score_b_yes + score_a_no, 4)
        q_two = round(score_a_yes + score_b_no, 4)

        # Official Polymarket single-sided penalty rules:
        c_penalty = self.config.rewards_single_sided_c
        if 0.10 <= p_yes <= 0.90:
            q_min = round(max(min(q_one, q_two), max(q_one, q_two) / c_penalty), 4)
            penalty_applied = (min(q_one, q_two) < max(q_one, q_two) / c_penalty) and (max(q_one, q_two) > 0.0)
        else:
            q_min = round(min(q_one, q_two), 4)
            penalty_applied = (q_one != q_two)

        is_two_sided_qual = (q_one > 0.0 and q_two > 0.0)

        reward_breakdown = RewardScoreBreakdown(
            q_one=q_one,
            q_two=q_two,
            q_min=q_min,
            is_two_sided_qualified=is_two_sided_qual,
            penalty_applied=penalty_applied,
        )

        yes_quote = QuotePair(
            bid_price=b_yes,
            ask_price=a_yes,
            bid_size=size_b_yes,
            ask_size=size_a_yes,
            delta_bid=eff_delta_b_yes,
            delta_ask=eff_delta_a_yes,
            reward_score_bid=round(score_b_yes, 4),
            reward_score_ask=round(score_a_yes, 4),
        )

        no_quote = QuotePair(
            bid_price=b_no,
            ask_price=a_no,
            bid_size=size_b_no,
            ask_size=size_a_no,
            delta_bid=eff_delta_b_no,
            delta_ask=eff_delta_a_no,
            reward_score_bid=round(score_b_no, 4),
            reward_score_ask=round(score_a_no, 4),
        )

        pair_bid_cost = round(b_yes + b_no, 4)
        pair_bid_margin = round((1.00 - pair_bid_cost) * 100.0, 2)
        pair_ask_cost = round(a_yes + a_no, 4)
        pair_ask_margin = round((pair_ask_cost - 1.00) * 100.0, 2)

        is_arb_guaranteed = (pair_bid_cost <= round(1.00 - effective_margin, 4))

        # Diagnostic reason
        skew_desc = f"q_net={q_net:+.1f} ({regime.value})"
        reason = (
            f"AS-GTM quotes: YES [{b_yes:.2f} / {a_yes:.2f}], NO [{b_no:.2f} / {a_no:.2f}] | "
            f"{skew_desc} | Pair bid cost ${pair_bid_cost:.4f} (+{pair_bid_margin:.1f}c margin) | "
            f"Rewards Q_min={q_min:.2f}"
        )

        return ASQuoteDecision(
            fair_price_yes=round(p_yes, 4),
            fair_price_no=round(p_no, 4),
            reservation_price_yes=round(r_yes, 4),
            reservation_price_no=round(r_no, 4),
            net_inventory=q_net,
            yes_quote=yes_quote,
            no_quote=no_quote,
            pair_bid_cost=pair_bid_cost,
            pair_bid_margin_cents=pair_bid_margin,
            pair_ask_cost=pair_ask_cost,
            pair_ask_margin_cents=pair_ask_margin,
            reward_breakdown=reward_breakdown,
            is_pair_arbitrage_guaranteed=is_arb_guaranteed,
            inventory_regime=regime,
            reason=reason,
        )

    def evaluate_from_spot_and_strike(
        self,
        strike_k: float,
        spot_s: float,
        tau_seconds: float,
        inventory_yes: float = 0.0,
        inventory_no: float = 0.0,
        annualized_volatility: Optional[float] = None,
        use_rewards_optimization: bool = True,
        gamma: Optional[float] = None,
        kappa: Optional[float] = None,
        intensity_A: Optional[float] = None,
        target_pair_margin: Optional[float] = None,
    ) -> ASQuoteDecision:
        """Evaluate AS quotes directly from underlying crypto spot and strike using digital BS.

        Computes fair binary probability via risk-neutral Black-Scholes digital call formula:
            d2 = (ln(S / K) - 0.5 * sigma^2 * tau) / (sigma * sqrt(tau))
            P(YES) = N(d2)
        """
        vol = annualized_volatility if annualized_volatility is not None else self.config.volatility_annual
        sig_sec = self.calculate_sigma_per_sqrt_sec(vol)
        tau = max(0.1, float(tau_seconds))

        if strike_k <= 0.0 or spot_s <= 0.0:
            fair_yes = 0.50
        elif tau <= 0.5:
            fair_yes = 1.0 if spot_s >= strike_k else 0.0
        else:
            vol_term = sig_sec * math.sqrt(tau)
            if vol_term <= 1e-9:
                fair_yes = 1.0 if spot_s >= strike_k else 0.0
            else:
                log_moneyness = math.log(spot_s / strike_k)
                drift_adj = -0.5 * (sig_sec ** 2) * tau
                d2 = (log_moneyness + drift_adj) / vol_term
                fair_yes = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))

        return self.evaluate_market_quotes(
            fair_price_yes=fair_yes,
            inventory_yes=inventory_yes,
            inventory_no=inventory_no,
            tau_seconds=tau,
            annualized_volatility=vol,
            use_rewards_optimization=use_rewards_optimization,
            gamma=gamma,
            kappa=kappa,
            intensity_A=intensity_A,
            target_pair_margin=target_pair_margin,
        )
