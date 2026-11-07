"""Binance Price Anchoring, Digital Option Quoting & Lifecycle Exit Optimizer.

Implements the reverse-engineered institutional market-making architecture:
1. Real-time Binance spot price tracking with rolling momentum (1s/5s/10s velocity).
2. Continuous Black-Scholes Digital Option Pricing:
       P_fair(Up) = N(d2) = N((ln(S_t / K) - 0.5 * sigma^2 * tau) / (sigma * sqrt(tau)))
       P_fair(Down) = 1.0 - P_fair(Up)
3. Binance-Anchored Maker Quoting:
   - Rest dual bids on Up and Down with ticket sizing (default 5.0 shares).
   - Structural Pair Arbitrage constraint: P_bid(Up) + P_bid(Down) <= max_pair_cost (e.g. $0.985),
     locking in a guaranteed positive margin upon $1.00 expiry settlement.
   - Toxic Surge Sentinels: suppresses or widens resting bids when Binance spot jumps >= 1.0 bps in <= 5s.
4. Quantitative Lifecycle Exit Optimizer:
   - Closed-form comparison between:
     A) Expiration Settlement (0 fee, 0 spread loss, negligible gas).
     B) Synthetic Complete-Set Crossing & Merging.
     C) Direct CLOB Market Order Liquidation.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from polymarketbot.fees import FeeSchedule
from polymarketbot.logs import get_logger

if TYPE_CHECKING:
    from polymarketbot.trading.as_inventory_engine import (
        ASInventoryConfig,
        ASQuoteDecision,
        AvellanedaStoikovEngine,
    )

logger = get_logger("binance_anchor")


class ExitPathway(str, Enum):
    """Exit and capital reclamation pathways."""
    EXPIRATION_SETTLEMENT = "EXPIRATION_SETTLEMENT"
    COMPLETE_SET_MERGE = "COMPLETE_SET_MERGE"
    CLOB_MARKET_SELL = "CLOB_MARKET_SELL"


class HorizonRegime(str, Enum):
    """Market duration and expiry horizon classification."""
    FAST_5M = "FAST_5M"                 # <= 300s (5-minute candle)
    FAST_15M = "FAST_15M"               # <= 900s (15-minute candle)
    INTRADAY_HOURLY = "INTRADAY_HOURLY" # <= 14,400s (1h - 4h intraday)
    DAILY = "DAILY"                     # <= 86,400s (24h)
    LONG_TERM = "LONG_TERM"             # > 86,400s (multi-day / political / macro)


@dataclass
class MarketHorizonInfo:
    """Parsed horizon, expiration, and taxonomy for a Polymarket market."""
    condition_id: str
    slug: str
    question: str
    regime: HorizonRegime
    time_to_expiry_seconds: float
    end_timestamp: float
    base_asset: str
    is_fast_binary: bool
    recommended_exit_default: ExitPathway
    strike_price: Optional[float] = None


class MarketHorizonClassifier:
    """Automated market duration parser and taxonomy classifier."""

    @staticmethod
    def parse_end_timestamp(market_data: dict[str, Any], default_horizon_s: float = 3600.0) -> float:
        """Extract exact Unix expiration timestamp from market payload."""
        slug = str(market_data.get("market_slug", market_data.get("slug", "")))
        
        # 1. Check slug for fast binary timestamp (e.g. btc-updown-5m-1793701200)
        import re
        match = re.search(r"-(\d{10})(?:-|$)", slug)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

        # 2. Check end_date / end_date_iso fields
        for key in ["end_date_iso", "end_date", "endDate", "resolution_time"]:
            val = market_data.get(key)
            if not val:
                continue
            if isinstance(val, (int, float)) and val > 1_000_000_000:
                return float(val)
            if isinstance(val, str):
                try:
                    from datetime import datetime
                    clean_str = val.replace("Z", "+00:00")
                    if len(clean_str) == 10 and clean_str.count("-") == 2:
                        # Format YYYY-MM-DD -> append end of day 23:59:59 UTC
                        clean_str += "T23:59:59+00:00"
                    return datetime.fromisoformat(clean_str).timestamp()
                except Exception:
                    pass

        # 3. Check rewards_config
        rewards_cfg = market_data.get("rewards_config")
        if isinstance(rewards_cfg, list) and rewards_cfg:
            end_d = rewards_cfg[0].get("end_date")
            if end_d and isinstance(end_d, str):
                try:
                    from datetime import datetime
                    clean = end_d.replace("Z", "+00:00")
                    if len(clean) == 10:
                        clean += "T23:59:59+00:00"
                    return datetime.fromisoformat(clean).timestamp()
                except Exception:
                    pass

        # 4. Check question for long-term / annual / political markers if no explicit date
        question_text = str(market_data.get("question", "")).lower()
        if any(k in question_text for k in ["election", "senate", "house", "president", "prime minister", "governor", "2026", "2027", "2028", "super bowl", "championship", "world cup", "tax"]):
            return time.time() + (86400.0 * 90.0)  # Default 90 days for macro/political event markets

        return time.time() + default_horizon_s

    @classmethod
    def classify_market(
        cls,
        market_data: dict[str, Any],
        current_time: Optional[float] = None,
    ) -> MarketHorizonInfo:
        """Parse market dictionary and classify horizon regime, asset, and exit default."""
        import re

        now = current_time if current_time is not None else time.time()
        end_ts = cls.parse_end_timestamp(market_data)
        tau = max(0.0, end_ts - now)

        slug = str(market_data.get("market_slug", market_data.get("slug", ""))).lower()
        question = str(market_data.get("question", "")).lower()
        cond_id = str(market_data.get("condition_id", ""))

        # 1. Horizon Regime Classification (Explicit slug tags take strict precedence)
        if "-5m-" in slug:
            regime = HorizonRegime.FAST_5M
            time_horizon = min(tau, 300.0) if tau > 0 else 0.0
        elif "-15m-" in slug:
            regime = HorizonRegime.FAST_15M
            time_horizon = min(tau, 900.0) if tau > 0 else 0.0
        elif any(k in slug for k in ["-1h-", "-4h-"]):
            regime = HorizonRegime.INTRADAY_HOURLY
            time_horizon = min(tau, 14400.0) if tau > 0 else 0.0
        elif any(k in slug for k in ["-daily-", "-24h-"]):
            regime = HorizonRegime.DAILY
            time_horizon = min(tau, 86400.0) if tau > 0 else 0.0
        elif any(k in question for k in ["election", "senate", "house", "president", "prime minister", "governor", "2026", "2027", "2028", "super bowl", "championship", "world cup", "tax"]):
            regime = HorizonRegime.LONG_TERM
            time_horizon = max(tau, 86400.0 * 30.0)
        elif "up or down" in question:
            if tau <= 360.0:
                regime = HorizonRegime.FAST_5M
                time_horizon = min(tau, 300.0) if tau > 0 else 0.0
            elif tau <= 1000.0:
                regime = HorizonRegime.FAST_15M
                time_horizon = min(tau, 900.0) if tau > 0 else 0.0
            elif tau <= 14400.0:
                regime = HorizonRegime.INTRADAY_HOURLY
                time_horizon = tau
            else:
                regime = HorizonRegime.DAILY
                time_horizon = tau
        elif tau <= 14400.0:
            regime = HorizonRegime.INTRADAY_HOURLY
            time_horizon = tau
        elif tau <= 86400.0:
            regime = HorizonRegime.DAILY
            time_horizon = tau
        else:
            regime = HorizonRegime.LONG_TERM
            time_horizon = tau

        is_fast = regime in (HorizonRegime.FAST_5M, HorizonRegime.FAST_15M)

        # 2. Base Asset Detection
        combined_text = f"{slug} {question}"
        if any(k in combined_text for k in ["btc", "bitcoin"]):
            base_asset = "BTC"
        elif any(k in combined_text for k in ["eth", "ethereum"]):
            base_asset = "ETH"
        elif any(k in combined_text for k in ["sol", "solana"]):
            base_asset = "SOL"
        elif any(k in combined_text for k in ["xrp", "ripple"]):
            base_asset = "XRP"
        elif any(k in combined_text for k in ["doge", "dogecoin"]):
            base_asset = "DOGE"
        elif any(k in combined_text for k in ["bnb", "binance"]):
            base_asset = "BNB"
        else:
            base_asset = "OTHER"

        # 3. Strike Price Extraction (if static interval)
        strike_price: Optional[float] = None
        strike_match = re.search(r"above\s+([0-9,]+(?:\.[0-9]+)?)", question)
        if strike_match:
            try:
                strike_price = float(strike_match.group(1).replace(",", ""))
            except ValueError:
                pass

        # 4. Default Exit Pathway
        # For fast binaries (5m/15m), waiting for expiration eliminates 100% of CLOB taker fees and spread loss.
        # For long term (> 24h), merging complete sets eliminates capital drag.
        if is_fast:
            exit_default = ExitPathway.EXPIRATION_SETTLEMENT
        elif regime == HorizonRegime.LONG_TERM:
            exit_default = ExitPathway.COMPLETE_SET_MERGE
        else:
            exit_default = ExitPathway.EXPIRATION_SETTLEMENT

        return MarketHorizonInfo(
            condition_id=cond_id,
            slug=slug,
            question=market_data.get("question", ""),
            regime=regime,
            time_to_expiry_seconds=round(time_horizon, 1),
            end_timestamp=round(end_ts, 1),
            base_asset=base_asset,
            is_fast_binary=is_fast,
            recommended_exit_default=exit_default,
            strike_price=strike_price,
        )



@dataclass
class BinanceTick:
    """Timestamped price tick from Binance."""
    timestamp: float
    price: float


class BinanceSpotFeed:
    """Tracks Binance spot prices and computes rolling velocity/momentum."""

    def __init__(self, symbol: str = "BTCUSDT", max_history_seconds: float = 60.0) -> None:
        self.symbol = symbol.upper()
        self.max_history_seconds = max_history_seconds
        self._history: deque[BinanceTick] = deque()
        self.latest_price: float = 0.0
        self.latest_timestamp: float = 0.0

    def update_price(self, price: float, timestamp: Optional[float] = None) -> None:
        """Record a new price tick from WebSocket or REST."""
        if price <= 0.0:
            return
        ts = timestamp if timestamp is not None else time.time()
        self.latest_price = float(price)
        self.latest_timestamp = float(ts)
        self._history.append(BinanceTick(timestamp=ts, price=self.latest_price))

        cutoff = ts - self.max_history_seconds
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()

    def get_velocity_bps(self, lookback_seconds: float = 5.0) -> float:
        """Calculate price return in basis points over recent lookback window.

        Positive means price rallied; negative means dumped.
        """
        if len(self._history) < 2 or self.latest_price <= 0.0:
            return 0.0

        target_ts = self.latest_timestamp - lookback_seconds
        ref_tick = self._history[0]
        for tick in self._history:
            if tick.timestamp >= target_ts:
                ref_tick = tick
                break

        if ref_tick.price <= 0.0:
            return 0.0

        return ((self.latest_price - ref_tick.price) / ref_tick.price) * 10_000.0

    def get_realized_volatility(self, lookback_seconds: float = 60.0, min_ticks: int = 5) -> float:
        """Calculate rolling sample standard deviation of log returns ln(Pt / Pt-1).

        sigma_annual = s_dlnP * sqrt(31_557_600 / dt_avg)
        Bounded within [0.10, 2.50].
        Fallback baseline 0.55 if fewer than min_ticks ticks or zero prices.
        """
        fallback = 0.55
        if len(self._history) < min_ticks or self.latest_price <= 0.0:
            return fallback

        target_ts = self.latest_timestamp - lookback_seconds
        window_ticks = [t for t in self._history if t.timestamp >= target_ts]
        if len(window_ticks) < min_ticks:
            return fallback

        prices = [t.price for t in window_ticks]
        timestamps = [t.timestamp for t in window_ticks]

        if any(p <= 0.0 for p in prices):
            return fallback

        log_returns: list[float] = []
        dts: list[float] = []
        for i in range(1, len(prices)):
            dt = timestamps[i] - timestamps[i - 1]
            if dt > 0:
                log_returns.append(math.log(prices[i] / prices[i - 1]))
                dts.append(dt)

        if len(log_returns) < 2 or sum(dts) <= 0.0:
            return fallback

        dt_avg = sum(dts) / len(dts)
        if dt_avg <= 0.0:
            return fallback

        mean_r = sum(log_returns) / len(log_returns)
        var_r = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        s_r = math.sqrt(max(0.0, var_r))

        seconds_per_year = 31_557_600.0  # 365.25 * 86400
        sigma_annual = s_r * math.sqrt(seconds_per_year / dt_avg)

        return max(0.10, min(2.50, sigma_annual))


class BlackScholesBinaryEngine:
    """Continuous Black-Scholes Digital Cash-or-Nothing Option Pricing."""

    def __init__(self, annualized_volatility: float = 0.55) -> None:
        self.sigma = annualized_volatility
        # Volatility per square-root second: sigma / sqrt(365.25 * 86400)
        self.sigma_per_sqrt_sec = annualized_volatility / math.sqrt(365.25 * 86400.0)

    def update_volatility(self, annualized_volatility: float) -> None:
        """Update annualized volatility and recompute sigma per square-root second."""
        self.sigma = float(annualized_volatility)
        self.sigma_per_sqrt_sec = self.sigma / math.sqrt(365.25 * 86400.0)

    @staticmethod
    def norm_cdf(x: float) -> float:
        """Standard normal cumulative distribution function N(x)."""
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def price_digital_option(
        self,
        strike_k: float,
        spot_s: float,
        time_to_expiry_seconds: float,
    ) -> Tuple[float, float, float]:
        """Compute fair probability of Up (Call) and Down (Put).

        Returns:
            (p_fair_up, p_fair_down, d2)
        """
        if strike_k <= 0.0 or spot_s <= 0.0:
            return 0.50, 0.50, 0.0

        tau = max(0.0, float(time_to_expiry_seconds))

        if tau <= 0.5:
            # At expiration: deterministic outcome
            if spot_s >= strike_k:
                return 1.0, 0.0, 10.0
            else:
                return 0.0, 1.0, -10.0

        vol_term = self.sigma_per_sqrt_sec * math.sqrt(tau)
        if vol_term <= 1e-9:
            p_up = 1.0 if spot_s >= strike_k else 0.0
            return p_up, 1.0 - p_up, 0.0

        # Under risk-neutral digital option pricing: d2 = (ln(S/K) - 0.5 * sigma^2 * tau) / (sigma * sqrt(tau))
        log_moneyness = math.log(spot_s / strike_k)
        drift_adjustment = -0.5 * (self.sigma_per_sqrt_sec ** 2) * tau
        d2 = (log_moneyness + drift_adjustment) / vol_term

        p_up = max(0.001, min(0.999, self.norm_cdf(d2)))
        p_down = 1.0 - p_up

        return p_up, p_down, d2


@dataclass
class BinanceQuoteDecision:
    """Actionable quoting decision generated from Binance fair value."""
    p_fair_up: float
    p_fair_down: float
    target_bid_up: float
    target_bid_down: float
    quote_size_up: float
    quote_size_down: float
    combined_pair_cost: float
    expected_margin_cents: float
    is_toxic_surge: bool
    surge_side_suppressed: str  # "NONE", "UP", "DOWN"
    reason: str


class BinanceAnchoredQuoter:
    """Institutional Maker Quoting Engine anchored to Binance spot & strike."""

    def __init__(
        self,
        bs_engine: Optional[BlackScholesBinaryEngine] = None,
        as_engine: Optional[Any] = None,
        max_pair_cost: float = 0.985,  # Locks in >= +1.5c margin per pair
        half_spread_cents: float = 1.5,
        default_ticket_size: float = 5.0,
        surge_threshold_bps: float = 1.0,
        tick_size: float = 0.01,
    ) -> None:
        self.bs_engine = bs_engine or BlackScholesBinaryEngine()
        self.max_pair_cost = float(max_pair_cost)
        self.half_spread = float(half_spread_cents) / 100.0
        self.ticket_size = float(default_ticket_size)
        self.surge_threshold_bps = float(surge_threshold_bps)
        self.tick_size = float(tick_size)
        self._as_engine = as_engine

    @property
    def as_engine(self) -> Any:
        if self._as_engine is None:
            from polymarketbot.trading.as_inventory_engine import (
                ASInventoryConfig,
                AvellanedaStoikovEngine,
            )
            margin = max(0.005, round(1.00 - self.max_pair_cost, 4))
            self._as_engine = AvellanedaStoikovEngine(
                ASInventoryConfig(
                    target_pair_margin=margin,
                    tick_size=self.tick_size,
                    base_ticket_size=self.ticket_size,
                    volatility_annual=self.bs_engine.sigma,
                )
            )
        return self._as_engine

    def evaluate_as_quotes(
        self,
        strike_k: float,
        spot_s: float,
        tau_seconds: float,
        velocity_5s_bps: float = 0.0,
        inventory_up: float = 0.0,
        inventory_down: float = 0.0,
        annualized_volatility: Optional[float] = None,
        use_rewards: bool = True,
    ) -> Any:
        """Compute institutional Avellaneda-Stoikov & Gueant quotes anchored to Binance."""
        vol = annualized_volatility or self.bs_engine.sigma
        return self.as_engine.evaluate_from_spot_and_strike(
            strike_k=strike_k,
            spot_s=spot_s,
            tau_seconds=tau_seconds,
            inventory_yes=inventory_up,
            inventory_no=inventory_down,
            annualized_volatility=vol,
            use_rewards_optimization=use_rewards,
        )

    def evaluate_quotes(
        self,
        strike_k: float,
        spot_s: float,
        tau_seconds: float,
        velocity_5s_bps: float = 0.0,
        inventory_up: float = 0.0,
        inventory_down: float = 0.0,
        capital_available: float = 100.0,
        market_spread_cents: Optional[float] = None,
        binance_feed: Optional[BinanceSpotFeed] = None,
    ) -> BinanceQuoteDecision:
        """Compute optimal maker bids on Up and Down tokens."""
        if binance_feed is not None:
            self.bs_engine.update_volatility(binance_feed.get_realized_volatility())

        if market_spread_cents is not None:
            half_spread = max(0.5, min(3.0, market_spread_cents / 2.0)) / 100.0
            max_pair_cost = 1.00 - 2.0 * half_spread
        else:
            half_spread = self.half_spread
            max_pair_cost = self.max_pair_cost

        p_up, p_down, d2 = self.bs_engine.price_digital_option(strike_k, spot_s, tau_seconds)

        # 1. Base quotes centered around fair value
        bid_up = p_up - half_spread
        bid_down = p_down - half_spread

        # 2. Inventory skew adjustment (fade side we have excess of)
        imbalance = inventory_up - inventory_down
        if abs(imbalance) >= 5.0:
            skew_cents = min(3.0, (imbalance / 20.0)) / 100.0
            bid_up -= skew_cents * 0.5
            bid_down += skew_cents * 0.5

        # 3. Structural Pair Arbitrage Constraint: bid_up + bid_down <= max_pair_cost
        pair_cost = bid_up + bid_down
        if pair_cost > max_pair_cost:
            excess = pair_cost - max_pair_cost
            bid_up -= excess / 2.0
            bid_down -= excess / 2.0

        # 4. Tick rounding and bounding
        bid_up = max(0.01, min(0.99, round(bid_up / self.tick_size) * self.tick_size))
        bid_down = max(0.01, min(0.99, round(bid_down / self.tick_size) * self.tick_size))

        # Re-verify pair cost after rounding
        if bid_up + bid_down > max_pair_cost:
            if bid_up >= bid_down:
                bid_up = max(0.01, bid_up - self.tick_size)
            else:
                bid_down = max(0.01, bid_down - self.tick_size)

        combined_pair = round(bid_up + bid_down, 4)
        margin_cents = round((1.0 - combined_pair) * 100.0, 2)

        # 5. Toxic Surge Sentinels: Binance velocity checks
        is_toxic = False
        suppressed_side = "NONE"
        reason = f"Normal quoting: pair cost ${combined_pair:.4f} locks +{margin_cents:.1f}c margin."

        size_up = self.ticket_size
        size_down = self.ticket_size

        if velocity_5s_bps >= self.surge_threshold_bps:
            # Binance surging upward: Down bid is toxic (fast takers will dump Down)
            is_toxic = True
            suppressed_side = "DOWN"
            size_down = 0.0  # Pull Down bid
            bid_up = min(0.99, bid_up + self.tick_size)  # Up is more valuable
            reason = f"TOXIC UP-SURGE: Binance +{velocity_5s_bps:.1f} bps in 5s. Suppressed Down bid."
        elif velocity_5s_bps <= -self.surge_threshold_bps:
            # Binance dumping downward: Up bid is toxic (fast takers will dump Up)
            is_toxic = True
            suppressed_side = "UP"
            size_up = 0.0  # Pull Up bid
            bid_down = min(0.99, bid_down + self.tick_size)
            reason = f"TOXIC DOWN-SURGE: Binance {velocity_5s_bps:.1f} bps in 5s. Suppressed Up bid."

        # Capital constraint
        if capital_available < (size_up * bid_up + size_down * bid_down):
            size_up = max(0.0, math.floor(capital_available / (2.0 * max(0.01, bid_up))))
            size_down = max(0.0, math.floor(capital_available / (2.0 * max(0.01, bid_down))))

        return BinanceQuoteDecision(
            p_fair_up=round(p_up, 4),
            p_fair_down=round(p_down, 4),
            target_bid_up=round(bid_up, 2),
            target_bid_down=round(bid_down, 2),
            quote_size_up=size_up,
            quote_size_down=size_down,
            combined_pair_cost=combined_pair,
            expected_margin_cents=margin_cents,
            is_toxic_surge=is_toxic,
            surge_side_suppressed=suppressed_side,
            reason=reason,
        )


@dataclass
class ExitCostComparison:
    """Quantitative friction and opportunity cost comparison across exit pathways."""
    recommended_pathway: ExitPathway
    cost_expiration_settlement: float
    cost_complete_set_merge: float
    cost_clob_market_sell: float
    net_payout_expiration: float
    net_payout_merge: float
    net_payout_sell: float
    opportunity_cost_waiting: float
    spread_friction_avoided: float
    taker_fee_avoided: float
    rationale: str


class LifecycleExitOptimizer:
    """Evaluates the optimal lifecycle exit pathway for prediction market positions."""

    def __init__(
        self,
        gas_merge_usd: float = 0.015,
        gas_redeem_usd: float = 0.005,
        benchmark_daily_apr: float = 0.03,  # 3% daily opportunity cost benchmark
        fee_rate: float = 0.07,
        fee_exponent: float = 1.0,
        fee_schedule: Optional[FeeSchedule] = None,
    ) -> None:
        self.gas_merge_usd = gas_merge_usd
        self.gas_redeem_usd = gas_redeem_usd
        self.benchmark_per_second = benchmark_daily_apr / 86400.0
        self.fee_rate = fee_rate
        self.fee_exponent = fee_exponent
        self.fee_schedule = fee_schedule

    @staticmethod
    def calculate_taker_fee(
        price: float,
        size_shares: float,
        fee_rate: float = 0.07,
        fee_exponent: float = 1.0,
        fee_schedule: Optional[FeeSchedule] = None,
    ) -> float:
        """Polymarket dynamic taker fee: shares * rate * (p * (1 - p)) ^ exponent."""
        if fee_schedule is not None:
            rate = fee_schedule.rate
            exp = fee_schedule.exponent
        else:
            rate = fee_rate
            exp = fee_exponent
        if rate <= 0.0 or size_shares <= 0.0:
            return 0.0
        p = max(0.001, min(0.999, price))
        return size_shares * rate * ((p * (1.0 - p)) ** exp)

    def compare_exit_pathways(
        self,
        shares_up: float,
        shares_down: float,
        bid_up: float,
        bid_down: float,
        ask_up: float,
        ask_down: float,
        tau_seconds: float,
        p_win_up: float = 0.50,
        fee_rate: Optional[float] = None,
        fee_exponent: Optional[float] = None,
        fee_schedule: Optional[FeeSchedule] = None,
    ) -> ExitCostComparison:
        """Evaluate the friction and net cash payout of all 3 exit mechanics.

        Returns:
            ExitCostComparison with recommended pathway.
        """
        complete_sets = min(shares_up, shares_down)
        unbalanced_up = max(0.0, shares_up - complete_sets)
        unbalanced_down = max(0.0, shares_down - complete_sets)
        total_shares = shares_up + shares_down

        sched = fee_schedule or self.fee_schedule
        f_rate = fee_rate if fee_rate is not None else self.fee_rate
        f_exp = fee_exponent if fee_exponent is not None else self.fee_exponent

        capital_locked = (shares_up * bid_up) + (shares_down * bid_down)
        opportunity_cost = capital_locked * self.benchmark_per_second * max(0.0, tau_seconds)

        # -------------------------------------------------------------------------
        # Pathway 1: EXPIRATION SETTLEMENT (Wait tau seconds)
        # -------------------------------------------------------------------------
        # Payout: 1.00 for winning side, 0.00 for losing side
        # Zero CLOB taker fee, zero spread crossing loss; only Polygon redemption gas.
        expected_settlement_cash = (p_win_up * shares_up * 1.0) + ((1.0 - p_win_up) * shares_down * 1.0)
        cost_settlement = self.gas_redeem_usd + opportunity_cost
        net_settlement = expected_settlement_cash - self.gas_redeem_usd

        # -------------------------------------------------------------------------
        # Pathway 2: SYNTHETIC MERGE (Buy missing leg at ask + merge all on-chain)
        # -------------------------------------------------------------------------
        # To merge complete_sets immediately: costs gas_merge_usd
        # To merge unbalanced shares: must cross the ask for the opposite token
        cost_to_complete = 0.0
        taker_fee_merge = 0.0
        if unbalanced_up > 0:
            cost_to_complete = unbalanced_up * ask_down
            taker_fee_merge = self.calculate_taker_fee(ask_down, unbalanced_up, f_rate, f_exp, sched)
        elif unbalanced_down > 0:
            cost_to_complete = unbalanced_down * ask_up
            taker_fee_merge = self.calculate_taker_fee(ask_up, unbalanced_down, f_rate, f_exp, sched)

        total_sets_after_crossing = max(shares_up, shares_down)
        gross_merge_cash = total_sets_after_crossing * 1.0
        net_merge = gross_merge_cash - cost_to_complete - taker_fee_merge - self.gas_merge_usd
        cost_merge = (cost_to_complete + taker_fee_merge + self.gas_merge_usd) - (gross_merge_cash - (shares_up * bid_up + shares_down * bid_down))

        # -------------------------------------------------------------------------
        # Pathway 3: CLOB MARKET SELL (Cross bid on all shares)
        # -------------------------------------------------------------------------
        gross_sell_cash = (shares_up * bid_up) + (shares_down * bid_down)
        taker_fee_sell = (
            self.calculate_taker_fee(bid_up, shares_up, f_rate, f_exp, sched)
            + self.calculate_taker_fee(bid_down, shares_down, f_rate, f_exp, sched)
        )
        net_sell = gross_sell_cash - taker_fee_sell
        cost_sell = taker_fee_sell + ((ask_up - bid_up) * shares_up * 0.5) + ((ask_down - bid_down) * shares_down * 0.5)
        cost_sell = taker_fee_sell + ((ask_up - bid_up) * shares_up * 0.5) + ((ask_down - bid_down) * shares_down * 0.5)

        # Compute savings
        spread_avoided = max(0.0, net_settlement - net_sell)
        taker_fee_avoided = taker_fee_sell

        # -------------------------------------------------------------------------
        # Recommendation Logic
        # -------------------------------------------------------------------------
        # When tau is short (e.g. <= 900s / 15m), Expiration Settlement almost always wins
        # because opportunity cost is negligible (< 1c) whereas taker fees & spread cost 2-4%.
        # Complete Set Merge wins if complete_sets > 0 and unbalanced shares are 0.
        if complete_sets > 0 and unbalanced_up == 0 and unbalanced_down == 0 and tau_seconds > 600:
            # Clean complete sets with long horizon -> merge immediately
            recommended = ExitPathway.COMPLETE_SET_MERGE
            rationale = (
                f"Clean complete sets ({complete_sets:.1f} pairs) with long horizon ({tau_seconds:.0f}s). "
                f"Merge on-chain unlocks ${gross_merge_cash:.2f} USDC immediately without crossing spread."
            )
        elif net_settlement >= net_merge and net_settlement >= net_sell:
            recommended = ExitPathway.EXPIRATION_SETTLEMENT
            rationale = (
                f"Expiration settlement yields highest net cash (${net_settlement:.2f} vs merge ${net_merge:.2f}, "
                f"sell ${net_sell:.2f}). Waiting {tau_seconds:.0f}s costs only ${opportunity_cost:.4f} in capital drag "
                f"while saving ${spread_avoided:.2f} in CLOB spread & taker fees."
            )
        elif net_merge >= net_sell:
            recommended = ExitPathway.COMPLETE_SET_MERGE
            rationale = (
                f"Synthetic merge yields ${net_merge:.2f} vs sell ${net_sell:.2f}. "
                f"Crossing complementary leg and merging avoids full book liquidation fees."
            )
        else:
            recommended = ExitPathway.CLOB_MARKET_SELL
            rationale = f"Emergency CLOB market sell: net payout ${net_sell:.2f}."

        return ExitCostComparison(
            recommended_pathway=recommended,
            cost_expiration_settlement=round(cost_settlement, 4),
            cost_complete_set_merge=round(cost_merge, 4),
            cost_clob_market_sell=round(cost_sell, 4),
            net_payout_expiration=round(net_settlement, 2),
            net_payout_merge=round(net_merge, 2),
            net_payout_sell=round(net_sell, 2),
            opportunity_cost_waiting=round(opportunity_cost, 4),
            spread_friction_avoided=round(spread_avoided, 2),
            taker_fee_avoided=round(taker_fee_avoided, 2),
            rationale=rationale,
        )
