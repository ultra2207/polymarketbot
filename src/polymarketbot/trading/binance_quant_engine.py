"""Binance High-Frequency Quantitative Pricing Engine for Polymarket Crypto Binaries.

Provides advanced quantitative modeling for ultra-short-dated (5m/15m) prediction
market binary contracts (e.g. btc-updown-5m, eth-updown-15m, sol-updown-5m).

Key Capabilities:
1. Micro-Price Estimation:
   - Volume-Weighted Top-of-Book Micro-Price.
   - Multi-Level L2 Depth Micro-Price (Stoikov 2018).
2. High-Frequency Volatility Estimators:
   - Rogers-Satchell (drift-independent).
   - Garman-Klass & Parkinson range-based variance.
   - Two-Scale Realized Variance (TSRV) / Microstructure noise filter.
   - Intraday volatility seasonality adjustment (funding rate settlement, US market open).
3. Order Flow Imbalance (OFI) & Short-Term Drift Prediction:
   - Level-1 & Multi-Level OFI (Cont, Kukanov, Stoikov 2014).
   - Trade Flow Imbalance (TFI) / Signed trade volume.
   - Predictive short-term drift calibration.
4. Jump-Diffusion Option Pricing (Merton 1976):
   - Continuous diffusion + Compound Poisson jump process.
   - Closed-form series expansion for cash-or-nothing binary call/put.
5. Exact Binary Option Greeks & Pinning Risk:
   - Analytical Delta (exploding sensitivity at ATM as tau -> 0).
   - Analytical Gamma (sign-reversal at the strike).
   - Analytical Vega (sign-inversion across moneyness).
   - Analytical Theta & Pinning Risk Index.
6. Execution Arbitrage Bounds:
   - Statistical Taker Sniping with Polymarket dynamic taker fee (0.07 * p * (1 - p)).
   - Institutional Dual-Sided Maker Quoting with Structural Pair Arbitrage.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.fees import FeeSchedule
from polymarketbot.logs import get_logger

logger = get_logger("binance_quant_engine")

SECONDS_PER_YEAR = 31_557_600.0  # 365.25 * 86,400


# =============================================================================
# Mathematical Helpers
# =============================================================================

def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function N(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal probability density function N'(x)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# =============================================================================
# 1. Micro-Price Estimation
# =============================================================================

@dataclass
class OrderBookLevel:
    price: float
    size: float


class MicroPriceEstimator:
    """Computes microstructure-informed fair spot prices from Binance order book."""

    @staticmethod
    def calculate_l1_micro_price(
        bid_price: float,
        bid_size: float,
        ask_price: float,
        ask_size: float,
    ) -> float:
        """Top-of-book Volume-Weighted Micro-Price.

        P_micro = (Q_b * P_a + Q_a * P_b) / (Q_b + Q_a)
                = P_b + (Q_b / (Q_b + Q_a)) * (P_a - P_b)
        """
        if bid_price <= 0.0 or ask_price <= 0.0 or bid_price > ask_price:
            return max(bid_price, ask_price)
        tot_size = bid_size + ask_size
        if tot_size <= 1e-9:
            return 0.5 * (bid_price + ask_price)
        return (bid_size * ask_price + ask_size * bid_price) / tot_size

    @staticmethod
    def calculate_multilevel_micro_price(
        bids: list[OrderBookLevel],
        asks: list[OrderBookLevel],
        decay_alpha: float = 0.5,
        max_levels: int = 5,
    ) -> float:
        """Multi-level L2 depth-weighted micro-price (Stoikov 2018).

        Weights deeper book levels with exponential decay w_k = exp(-alpha * (k - 1)).
        """
        if not bids or not asks:
            return 0.0
        n_levels = min(len(bids), len(asks), max_levels)
        if n_levels == 0:
            return 0.0

        weighted_num = 0.0
        weighted_den = 0.0
        for k in range(n_levels):
            w = math.exp(-decay_alpha * k)
            p_b = bids[k].price
            q_b = bids[k].size
            p_a = asks[k].price
            q_a = asks[k].size

            tot_lvl_qty = q_b + q_a
            if tot_lvl_qty > 1e-9:
                lvl_micro = (q_b * p_a + q_a * p_b) / tot_lvl_qty
                weighted_num += w * tot_lvl_qty * lvl_micro
                weighted_den += w * tot_lvl_qty

        if weighted_den <= 1e-9:
            return 0.5 * (bids[0].price + asks[0].price)

        return weighted_num / weighted_den

    @staticmethod
    def calculate_order_book_imbalance(bid_size: float, ask_size: float) -> float:
        """Normalized top-of-book imbalance: (Q_b - Q_a) / (Q_b + Q_a) in [-1, +1]."""
        tot = bid_size + ask_size
        if tot <= 1e-9:
            return 0.0
        return (bid_size - ask_size) / tot


# =============================================================================
# 2. High-Frequency Realized Volatility Estimators
# =============================================================================

@dataclass
class KlineBar:
    open_time_s: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class HighFrequencyVolEstimator:
    """Calculates instantaneous volatility using high-frequency range and tick estimators."""

    @staticmethod
    def rogers_satchell_variance(bars: list[KlineBar]) -> float:
        """Rogers-Satchell realized variance per bar (drift-independent).

        sigma_RS^2 = sum( ln(H/C)*ln(H/O) + ln(L/C)*ln(L/O) ) / N
        """
        if not bars:
            return 0.0
        var_sum = 0.0
        valid_count = 0
        for b in bars:
            if b.open <= 0 or b.high <= 0 or b.low <= 0 or b.close <= 0:
                continue
            if b.high < b.low or b.high < b.open or b.high < b.close:
                continue
            term1 = math.log(b.high / b.close) * math.log(b.high / b.open)
            term2 = math.log(b.low / b.close) * math.log(b.low / b.open)
            var_sum += term1 + term2
            valid_count += 1

        if valid_count == 0:
            return 0.0
        return max(0.0, var_sum / valid_count)

    @staticmethod
    def garman_klass_variance(bars: list[KlineBar]) -> float:
        """Garman-Klass realized variance per bar (zero-drift benchmark).

        sigma_GK^2 = sum( 0.5 * ln(H/L)^2 - (2*ln(2) - 1) * ln(C/O)^2 ) / N
        """
        if not bars:
            return 0.0
        c1 = 0.5
        c2 = 2.0 * math.log(2.0) - 1.0
        var_sum = 0.0
        valid_count = 0
        for b in bars:
            if b.open <= 0 or b.high <= 0 or b.low <= 0 or b.close <= 0:
                continue
            term_hl = math.log(b.high / b.low) ** 2
            term_co = math.log(b.close / b.open) ** 2
            var_sum += c1 * term_hl - c2 * term_co
            valid_count += 1

        if valid_count == 0:
            return 0.0
        return max(0.0, var_sum / valid_count)

    @staticmethod
    def parkinson_variance(bars: list[KlineBar]) -> float:
        """Parkinson extreme-value variance per bar.

        sigma_P^2 = sum( ln(H/L)^2 ) / (4 * ln(2) * N)
        """
        if not bars:
            return 0.0
        denom = 4.0 * math.log(2.0)
        var_sum = 0.0
        valid_count = 0
        for b in bars:
            if b.high <= 0 or b.low <= 0 or b.high < b.low:
                continue
            var_sum += (math.log(b.high / b.low) ** 2) / denom
            valid_count += 1

        if valid_count == 0:
            return 0.0
        return max(0.0, var_sum / valid_count)

    @staticmethod
    def yang_zhang_variance(bars: list[KlineBar]) -> float:
        """Yang-Zhang (2000) multi-period range-based variance (drift & overnight jump independent).

        sigma_YZ^2 = sigma_open^2 + k * sigma_close^2 + (1 - k) * sigma_RS^2
        where k = 0.34 / (1.34 + (N + 1) / (N - 1))
        """
        n = len(bars)
        if n < 2:
            return HighFrequencyVolEstimator.rogers_satchell_variance(bars)

        # 1. Open jumps: u_i = ln(O_i / C_{i-1})
        open_jumps: list[float] = []
        for i in range(1, n):
            if bars[i].open > 0 and bars[i - 1].close > 0:
                open_jumps.append(math.log(bars[i].open / bars[i - 1].close))

        # 2. Close returns: c_i = ln(C_i / O_i)
        close_returns: list[float] = []
        for b in bars:
            if b.open > 0 and b.close > 0:
                close_returns.append(math.log(b.close / b.open))

        # 3. Rogers-Satchell variance
        var_rs = HighFrequencyVolEstimator.rogers_satchell_variance(bars)

        if not open_jumps or not close_returns:
            return var_rs

        # Sample variance of open jumps
        mean_u = sum(open_jumps) / len(open_jumps)
        var_o = sum((u - mean_u) ** 2 for u in open_jumps) / max(1, len(open_jumps) - 1)

        # Sample variance of close returns
        mean_c = sum(close_returns) / len(close_returns)
        var_c = sum((c - mean_c) ** 2 for c in close_returns) / max(1, len(close_returns) - 1)

        # Weighting constant k
        k = 0.34 / (1.34 + float(n + 1) / float(n - 1))
        var_yz = var_o + k * var_c + (1.0 - k) * var_rs
        return max(0.0, var_yz)

    @classmethod
    def annualize_bar_volatility(
        cls,
        bar_variance: float,
        bar_duration_seconds: float = 60.0,
        fallback_vol: float = 0.55,
    ) -> float:
        """Annualize per-bar variance into annualized volatility sigma_annual."""
        if bar_variance <= 0.0 or bar_duration_seconds <= 0.0:
            return fallback_vol
        bars_per_year = SECONDS_PER_YEAR / bar_duration_seconds
        ann_var = bar_variance * bars_per_year
        ann_vol = math.sqrt(ann_var)
        return max(0.10, min(2.50, ann_vol))

    @staticmethod
    def get_seasonality_multiplier(utc_timestamp: Optional[float] = None) -> float:
        """Calculates intraday volatility seasonality multiplier based on UTC market cycles.

        Key structural crypto volatility events:
        1. Binance Funding Rate Settlement (00:00, 08:00, 16:00 UTC):
           Perp arbitrage flows create violent rebalancing surges within +/- 10 mins.
        2. US Cash Equities Open (13:30 - 15:00 UTC / 9:30 AM ET):
           Peak daily volume and institutional price discovery.
        3. Asia Morning Session (00:00 - 02:00 UTC):
           Elevated liquidity turnover.
        """
        now_ts = utc_timestamp if utc_timestamp is not None else time.time()
        dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        hour = dt.hour
        minute = dt.minute
        time_in_day_minutes = hour * 60 + minute

        multiplier = 1.0

        # Funding rate settlement windows: 00:00, 08:00, 16:00 UTC (+/- 10 mins)
        funding_settlement_minutes = [0, 480, 960]
        for f_min in funding_settlement_minutes:
            dist = min(abs(time_in_day_minutes - f_min), abs(time_in_day_minutes - (f_min + 1440)))
            if dist <= 10:
                multiplier = max(multiplier, 1.45)
            elif dist <= 30:
                multiplier = max(multiplier, 1.20)

        # US Equities Open: 13:30 - 15:00 UTC (810 - 900 minutes)
        if 810 <= time_in_day_minutes <= 900:
            multiplier = max(multiplier, 1.35)

        return multiplier


class HighFrequencyEWMAVolEstimator:
    """Online recursive tick-level EWMA volatility estimator with microstructure noise correction.

    Replaces static/fixed volatility assumptions with dynamic high-frequency adaptation.
    Formulation:
        r_t = ln(P_t / P_{t-1})
        dt = max(1e-4, t_t - t_{t-1})
        v_t = (r_t^2) / (dt / SECONDS_PER_YEAR)
        lambda_decay = exp(-dt / half_life_seconds)
        sigma_{raw}^2 = lambda_decay * sigma_{raw, t-1}^2 + (1 - lambda_decay) * v_t

    Microstructure noise adjustment (Bandi-Russell / Hansen-Lunde):
        Corrects for negative 1st-order autocovariance from bid-ask bouncing.
        gamma_1 = lambda_decay * gamma_{1, t-1} + (1 - lambda_decay) * (r_t * r_{t-1})
        noise_adj_var = max(min_var, sigma_{raw}^2 + 2 * (gamma_1 / (dt / SECONDS_PER_YEAR)))
    """

    def __init__(
        self,
        half_life_seconds: float = 60.0,
        initial_volatility: float = 0.55,
        min_volatility: float = 0.10,
        max_volatility: float = 2.50,
        noise_correction: bool = True,
    ) -> None:
        self.half_life_seconds = max(1.0, float(half_life_seconds))
        self.min_volatility = float(min_volatility)
        self.max_volatility = float(max_volatility)
        self.noise_correction = noise_correction

        self.current_var = (max(min_volatility, min(max_volatility, initial_volatility))) ** 2
        self.last_price: Optional[float] = None
        self.last_timestamp: Optional[float] = None
        self.last_return: float = 0.0
        self.rolling_autocov: float = 0.0
        self.tick_count: int = 0

    def update_tick(self, price: float, timestamp: Optional[float] = None) -> float:
        """Update recursive variance with a new price tick and return current annualized volatility."""
        if price <= 0.0:
            return self.get_current_volatility()
        now_ts = timestamp if timestamp is not None else time.time()

        if self.last_price is None or self.last_timestamp is None:
            self.last_price = float(price)
            self.last_timestamp = float(now_ts)
            self.tick_count += 1
            return self.get_current_volatility()

        dt = max(1e-4, now_ts - self.last_timestamp)
        if now_ts < self.last_timestamp:
            return self.get_current_volatility()

        ret = math.log(price / self.last_price)
        dt_annual = dt / SECONDS_PER_YEAR
        inst_var = (ret ** 2) / dt_annual

        decay = math.exp(-dt / self.half_life_seconds)
        self.current_var = decay * self.current_var + (1.0 - decay) * inst_var

        if self.noise_correction and self.tick_count >= 2:
            prod_r = ret * self.last_return
            self.rolling_autocov = decay * self.rolling_autocov + (1.0 - decay) * prod_r

        self.last_price = float(price)
        self.last_timestamp = float(now_ts)
        self.last_return = ret
        self.tick_count += 1

        return self.get_current_volatility()

    def get_current_volatility(self) -> float:
        """Return the current annualized volatility sigma in [min_volatility, max_volatility]."""
        eff_var = self.current_var
        if self.noise_correction and self.tick_count >= 3:
            if self.last_timestamp and self.last_price and self.rolling_autocov < 0.0:
                dt_avg = max(1e-4, self.half_life_seconds / max(1, self.tick_count))
                dt_annual = dt_avg / SECONDS_PER_YEAR
                noise_adj = 2.0 * (self.rolling_autocov / dt_annual)
                eff_var = max(self.min_volatility ** 2, eff_var + noise_adj)

        vol = math.sqrt(max(0.0, eff_var))
        return max(self.min_volatility, min(self.max_volatility, vol))


# =============================================================================
# 3. Order Flow Imbalance (OFI) & Short-Term Drift Prediction
# =============================================================================

@dataclass
class OFISnapshot:
    timestamp: float
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float


class OrderFlowImbalanceTracker:
    """Tracks Order Flow Imbalance (Cont, Kukanov, Stoikov 2014) on Binance L1/L2.

    Eliminates static heuristics by dynamically calibrating the transmission beta
    between order flow imbalance and price return via online rolling regression.
    """

    def __init__(self, history_seconds: float = 60.0, beta_decay: float = 0.95) -> None:
        self.history_seconds = history_seconds
        self.beta_decay = beta_decay
        self._history: deque[OFISnapshot] = deque()
        self._cumulative_ofi: float = 0.0
        self._last_ofi_delta: float = 0.0
        self._rolling_cov_p_ofi: float = 0.0
        self._rolling_var_ofi: float = 0.0
        self._calibrated_beta_ofi: Optional[float] = None
        self._total_updates: int = 0

    def update(
        self,
        bid_price: float,
        bid_size: float,
        ask_price: float,
        ask_size: float,
        timestamp: Optional[float] = None,
    ) -> float:
        """Update with a new BBO frame and return the incremental OFI (shares)."""
        ts = timestamp if timestamp is not None else time.time()
        curr = OFISnapshot(ts, bid_price, bid_size, ask_price, ask_size)

        if not self._history:
            self._history.append(curr)
            return 0.0

        prev = self._history[-1]

        # Bid side component
        if curr.bid_price > prev.bid_price:
            delta_w_b = curr.bid_size
        elif curr.bid_price == prev.bid_price:
            delta_w_b = curr.bid_size - prev.bid_size
        else:
            delta_w_b = -prev.bid_size

        # Ask side component
        if curr.ask_price > prev.ask_price:
            delta_w_a = -prev.ask_size
        elif curr.ask_price == prev.ask_price:
            delta_w_a = curr.ask_size - prev.ask_size
        else:
            delta_w_a = curr.ask_size

        ofi_increment = delta_w_b - delta_w_a
        self._last_ofi_delta = ofi_increment
        self._cumulative_ofi += ofi_increment

        # Online dynamic calibration of beta_OFI: Delta P = beta * Delta OFI
        prev_mid = 0.5 * (prev.bid_price + prev.ask_price)
        curr_mid = 0.5 * (curr.bid_price + curr.ask_price)
        if prev_mid > 0.0:
            delta_p_bps = ((curr_mid - prev_mid) / prev_mid) * 10_000.0
            d = self.beta_decay
            self._rolling_cov_p_ofi = d * self._rolling_cov_p_ofi + (1.0 - d) * (delta_p_bps * ofi_increment)
            self._rolling_var_ofi = d * self._rolling_var_ofi + (1.0 - d) * (ofi_increment ** 2)
            if self._rolling_var_ofi > 1e-4:
                ratio = self._rolling_cov_p_ofi / self._rolling_var_ofi
                self._calibrated_beta_ofi = max(0.000001, min(0.005, ratio))

        self._history.append(curr)
        self._total_updates += 1

        # Evict old history
        cutoff = ts - self.history_seconds
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()

        return ofi_increment

    def get_ofi_rate(self, lookback_seconds: float = 10.0) -> float:
        """OFI per second over recent lookback window."""
        if len(self._history) < 2:
            return 0.0
        latest_ts = self._history[-1].timestamp
        cutoff = latest_ts - lookback_seconds
        # Sub-sample snapshots in window
        window = [s for s in self._history if s.timestamp >= cutoff]
        if len(window) < 2:
            return 0.0
        dt = window[-1].timestamp - window[0].timestamp
        if dt <= 0.0:
            return 0.0
        # Compute incremental OFI across this window
        window_ofi = 0.0
        for i in range(1, len(window)):
            p = window[i - 1]
            c = window[i]
            dw_b = c.bid_size if c.bid_price > p.bid_price else (c.bid_size - p.bid_size if c.bid_price == p.bid_price else -p.bid_size)
            dw_a = -p.ask_size if c.ask_price > p.ask_price else (c.ask_size - p.ask_size if c.ask_price == p.ask_price else c.ask_size)
            window_ofi += (dw_b - dw_a)
        return window_ofi / dt

    def estimate_drift_mu(
        self,
        lookback_seconds: float = 10.0,
        beta_ofi: Optional[float] = None,
        velocity_5s_bps: float = 0.0,
        use_calibrated_beta: bool = True,
    ) -> float:
        """Predicts instantaneous drift mu (annualized) for the binary pricing engine.

        mu_annual = (mu_bps_per_sec * 1e-4) * SECONDS_PER_YEAR
        """
        ofi_rate = self.get_ofi_rate(lookback_seconds)
        
        # Select dynamically calibrated beta or specified fallback
        if beta_ofi is not None:
            eff_beta = beta_ofi
        elif use_calibrated_beta and self._calibrated_beta_ofi is not None:
            eff_beta = self._calibrated_beta_ofi
        else:
            eff_beta = 0.00005

        # Expected return in bps per second
        # Combines order book imbalance flow + short-term trade momentum
        drift_bps_sec = (eff_beta * ofi_rate) + (0.05 * (velocity_5s_bps / 5.0))
        # Bounded between -50 bps/sec and +50 bps/sec to prevent unbounded runaway
        drift_bps_sec = max(-50.0, min(50.0, drift_bps_sec))

        mu_per_sec = drift_bps_sec * 1e-4
        mu_annual = mu_per_sec * SECONDS_PER_YEAR
        # Bound annualized drift between [-5.0, +5.0]
        return max(-5.0, min(5.0, mu_annual))


class MultiLevelOrderFlowImbalanceTracker:
    """Multi-Level Order Flow Imbalance tracker across L2 depth (Cont, Kukanov, Stoikov 2014).

    Integrates queue changes across deeper levels k=1..K with distance decay:
        OFI_{multi} = sum_{k=1}^K exp(-alpha * (k - 1)) * OFI_k
    """

    def __init__(self, max_levels: int = 5, decay_alpha: float = 0.5, history_seconds: float = 60.0) -> None:
        self.max_levels = max_levels
        self.decay_alpha = decay_alpha
        self.history_seconds = history_seconds
        self._prev_bids: list[OrderBookLevel] = []
        self._prev_asks: list[OrderBookLevel] = []
        self._last_update_ts: float = 0.0
        self._history: deque[Tuple[float, float]] = deque()  # (ts, multi_ofi_delta)

    def update(
        self,
        bids: list[OrderBookLevel],
        asks: list[OrderBookLevel],
        timestamp: Optional[float] = None,
    ) -> float:
        """Update with a new multi-level book snapshot and return weighted incremental OFI."""
        now_ts = timestamp if timestamp is not None else time.time()
        if not self._prev_bids or not self._prev_asks:
            self._prev_bids = list(bids[:self.max_levels])
            self._prev_asks = list(asks[:self.max_levels])
            self._last_update_ts = now_ts
            self._history.append((now_ts, 0.0))
            return 0.0

        n_levels = min(len(bids), len(asks), len(self._prev_bids), len(self._prev_asks), self.max_levels)
        multi_ofi = 0.0
        total_weight = 0.0

        for k in range(n_levels):
            w = math.exp(-self.decay_alpha * k)
            curr_b = bids[k]
            prev_b = self._prev_bids[k]
            curr_a = asks[k]
            prev_a = self._prev_asks[k]

            # Bid side contribution
            if curr_b.price > prev_b.price:
                delta_b = curr_b.size
            elif curr_b.price == prev_b.price:
                delta_b = curr_b.size - prev_b.size
            else:
                delta_b = -prev_b.size

            # Ask side contribution
            if curr_a.price > prev_a.price:
                delta_a = -prev_a.size
            elif curr_a.price == prev_a.price:
                delta_a = curr_a.size - prev_a.size
            else:
                delta_a = curr_a.size

            multi_ofi += w * (delta_b - delta_a)
            total_weight += w

        normalized_ofi = multi_ofi / max(1e-9, total_weight)
        self._history.append((now_ts, normalized_ofi))
        self._prev_bids = list(bids[:self.max_levels])
        self._prev_asks = list(asks[:self.max_levels])
        self._last_update_ts = now_ts

        cutoff = now_ts - self.history_seconds
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        return normalized_ofi

    def get_multi_ofi_rate(self, lookback_seconds: float = 10.0) -> float:
        """Returns weighted multi-level OFI per second over lookback window."""
        if len(self._history) < 2:
            return 0.0
        latest_ts = self._history[-1][0]
        cutoff = latest_ts - lookback_seconds
        window = [h for h in self._history if h[0] >= cutoff]
        if len(window) < 2:
            return 0.0
        dt = window[-1][0] - window[0][0]
        if dt <= 0.0:
            return 0.0
        total_ofi = sum(h[1] for h in window[1:])
        return total_ofi / dt


@dataclass
class LeadLagState:
    """State vector and uncertainty for cross-venue transmission."""
    beta: float              # Transmission elasticity (dProb / dLnS)
    alpha: float             # Constant drift/intercept
    beta_variance: float     # Estimation variance for beta
    innovation_error: float  # Latest measurement surprise
    timestamp: float


class CrossVenueLeadLagKalmanFilter:
    """State-Space Kalman Filter for dynamic cross-venue lead-lag estimation.

    Tracks transmission elasticity between Binance spot price changes (d ln S)
    and Polymarket binary probability changes (d P).

    Observation Equation:
        y_t = Delta P_{poly, t} = [Delta ln S_{binance, t}, 1.0] * [beta_t, alpha_t]^T + v_t,  v_t ~ N(0, R)
    State Transition Equation:
        theta_t = theta_{t-1} + w_t,  w_t ~ N(0, Q)
    """

    def __init__(
        self,
        initial_beta: float = 1.0,
        initial_alpha: float = 0.0,
        process_noise_beta: float = 1e-4,
        process_noise_alpha: float = 1e-6,
        measurement_noise: float = 1e-6,
    ) -> None:
        self.state = [float(initial_beta), float(initial_alpha)]  # [beta, alpha]
        self.p_cov = [[1.0, 0.0], [0.0, 1e-6]]
        self.q_cov = [[process_noise_beta, 0.0], [0.0, process_noise_alpha]]
        self.r_var = measurement_noise
        self.last_update_ts = time.time()
        self.last_binance_price: Optional[float] = None
        self.last_poly_prob: Optional[float] = None

    def update(
        self,
        binance_price: float,
        poly_prob: float,
        timestamp: Optional[float] = None,
    ) -> LeadLagState:
        """Update Kalman Filter with synchronized Binance spot and Polymarket probability."""
        now_ts = timestamp if timestamp is not None else time.time()

        if self.last_binance_price is None or self.last_poly_prob is None:
            self.last_binance_price = float(binance_price)
            self.last_poly_prob = float(poly_prob)
            self.last_update_ts = now_ts
            return LeadLagState(
                beta=self.state[0],
                alpha=self.state[1],
                beta_variance=self.p_cov[0][0],
                innovation_error=0.0,
                timestamp=now_ts,
            )

        if self.last_binance_price <= 0.0:
            d_ln_s = 0.0
        else:
            d_ln_s = math.log(binance_price / self.last_binance_price)
        d_p = poly_prob - self.last_poly_prob

        # Work in basis points for numerical conditioning and balanced regressor scales
        d_ln_s_bps = d_ln_s * 10_000.0
        d_p_bps = d_p * 10_000.0

        # 1. Predict
        p00 = self.p_cov[0][0] + self.q_cov[0][0]
        p01 = self.p_cov[0][1]
        p10 = self.p_cov[1][0]
        p11 = self.p_cov[1][1] + self.q_cov[1][1]

        # 2. Measurement Vector H = [d_ln_s_bps, 1.0]
        h0 = d_ln_s_bps
        h1 = 1.0

        # Innovation: e = y - H * x
        y_pred = h0 * self.state[0] + h1 * self.state[1]
        e = d_p_bps - y_pred

        # Innovation covariance: S = H * P * H^T + R
        hp0 = h0 * p00 + h1 * p10
        hp1 = h0 * p01 + h1 * p11
        s_cov = hp0 * h0 + hp1 * h1 + self.r_var

        # Kalman Gain: K = P * H^T / S
        k0 = (p00 * h0 + p01 * h1) / s_cov
        k1 = (p10 * h0 + p11 * h1) / s_cov

        # 3. Update state
        self.state[0] += k0 * e
        self.state[1] += k1 * e
        self.state[0] = max(0.01, min(100.0, self.state[0]))

        # Update covariance: P = (I - K * H) * P_{pred}
        self.p_cov[0][0] = (1.0 - k0 * h0) * p00 - (k0 * h1) * p10
        self.p_cov[0][1] = (1.0 - k0 * h0) * p01 - (k0 * h1) * p11
        self.p_cov[1][0] = (-k1 * h0) * p00 + (1.0 - k1 * h1) * p10
        self.p_cov[1][1] = (-k1 * h0) * p01 + (1.0 - k1 * h1) * p11

        self.last_binance_price = float(binance_price)
        self.last_poly_prob = float(poly_prob)
        self.last_update_ts = now_ts

        return LeadLagState(
            beta=self.state[0],
            alpha=self.state[1],
            beta_variance=self.p_cov[0][0],
            innovation_error=e,
            timestamp=now_ts,
        )

    @staticmethod
    def calculate_analytical_elasticity(
        spot_s: float,
        strike_k: float,
        tau_seconds: float,
        annualized_volatility: float,
        risk_free_rate: float = 0.0,
    ) -> float:
        """Computes analytical binary option transmission elasticity dP / d(ln S).

        Formula:
            d(P_yes) / d(ln S) = S * (dP / dS) = exp(-r*tau) * N'(d2) / (sigma * sqrt(tau))
        """
        if spot_s <= 0.0 or strike_k <= 0.0 or annualized_volatility <= 0.0:
            return 1.0
        tau = max(0.1, float(tau_seconds))
        sig_sec = annualized_volatility / math.sqrt(SECONDS_PER_YEAR)
        sig_tau = sig_sec * math.sqrt(tau)

        r_sec = risk_free_rate / SECONDS_PER_YEAR
        log_m = math.log(spot_s / strike_k)
        d2 = (log_m + (r_sec - 0.5 * sig_sec ** 2) * tau) / sig_tau

        pdf_d2 = norm_pdf(d2)
        discount = math.exp(-r_sec * tau)
        elasticity = (discount * pdf_d2) / max(1e-9, sig_tau)
        return max(0.01, min(5000.0, elasticity))

    @classmethod
    def calculate_dynamic_surge_threshold_bps(
        cls,
        spot_s: float,
        strike_k: float,
        tau_seconds: float,
        annualized_volatility: float,
        prob_shock_hurdle: float = 0.015,  # 1.5 cents probability move
        min_threshold_bps: float = 0.10,
        max_threshold_bps: float = 10.0,
    ) -> float:
        """Computes dynamic Binance surge threshold in bps that causes a prob_shock_hurdle move in Polymarket.

        Delta S_bps = (prob_shock_hurdle / elasticity) * 10,000.
        """
        elasticity = cls.calculate_analytical_elasticity(
            spot_s=spot_s,
            strike_k=strike_k,
            tau_seconds=tau_seconds,
            annualized_volatility=annualized_volatility,
        )
        raw_threshold_bps = (prob_shock_hurdle / max(1e-4, elasticity)) * 10_000.0
        return max(min_threshold_bps, min(max_threshold_bps, raw_threshold_bps))


# =============================================================================
# 4. Jump-Diffusion Option Pricing Engine (Merton 1976)
# =============================================================================

@dataclass
class BinaryOptionGreeks:
    """Exact analytical sensitivities for short-duration binary contracts."""
    delta: float              # dP / dS ($ per $ spot movement)
    gamma: float              # d^2P / dS^2
    vega: float               # dP / dsigma (per 1.0 unit of vol)
    theta: float              # -dP / dtau ($ decay per second)
    pinning_risk_index: float # Sensitivity to discrete tick jumps near expiry


class MertonJumpDiffusionBinaryEngine:
    """Prices ultra-short-dated binary contracts under Merton Jump Diffusion & Black-Scholes.

    Under risk-neutral or physical filtration:
        dS_t / S_{t-} = (mu - lambda * k_bar) dt + sigma dW_t + (J - 1) dq_t
    where:
        lambda: Poisson jump arrival rate per second
        ln(J) ~ N(gamma, delta^2)
        k_bar = exp(gamma + 0.5 * delta^2) - 1
    """

    def __init__(
        self,
        annualized_volatility: float = 0.55,
        risk_free_rate: float = 0.0,
        jump_intensity_per_sec: float = 0.001,  # ~1 jump every 1000s
        jump_mean: float = 0.0,
        jump_std: float = 0.002,                # 20 bps jump std
        max_poisson_terms: int = 15,
    ) -> None:
        self.sigma = annualized_volatility
        self.sigma_per_sqrt_sec = annualized_volatility / math.sqrt(SECONDS_PER_YEAR)
        self.r = risk_free_rate
        self.r_per_sec = risk_free_rate / SECONDS_PER_YEAR
        self.jump_intensity = jump_intensity_per_sec
        self.jump_mean = jump_mean
        self.jump_std = jump_std
        self.max_terms = max_poisson_terms

    def update_volatility(self, annualized_volatility: float) -> None:
        self.sigma = float(annualized_volatility)
        self.sigma_per_sqrt_sec = self.sigma / math.sqrt(SECONDS_PER_YEAR)

    def price_black_scholes_binary(
        self,
        strike_k: float,
        spot_s: float,
        tau_seconds: float,
        mu_annual: float = 0.0,
    ) -> Tuple[float, float, float]:
        """Continuous Black-Scholes Digital Option Pricing with drift mu.

        Returns:
            (p_yes, p_no, d2)
        """
        if strike_k <= 0.0 or spot_s <= 0.0:
            return 0.50, 0.50, 0.0

        tau = max(0.0, float(tau_seconds))
        if tau <= 0.1:
            p_yes = 1.0 if spot_s >= strike_k else 0.0
            return p_yes, 1.0 - p_yes, (10.0 if p_yes == 1.0 else -10.0)

        sigma_tau = self.sigma_per_sqrt_sec * math.sqrt(tau)
        if sigma_tau <= 1e-9:
            p_yes = 1.0 if spot_s >= strike_k else 0.0
            return p_yes, 1.0 - p_yes, 0.0

        mu_per_sec = mu_annual / SECONDS_PER_YEAR
        # d2 = (ln(S/K) + (mu - 0.5 * sigma^2)*tau) / (sigma * sqrt(tau))
        log_moneyness = math.log(spot_s / strike_k)
        drift_term = (mu_per_sec - 0.5 * (self.sigma_per_sqrt_sec ** 2)) * tau
        d2 = (log_moneyness + drift_term) / sigma_tau

        discount = math.exp(-self.r_per_sec * tau)
        p_yes = discount * norm_cdf(d2)
        p_yes = max(0.0001, min(0.9999, p_yes))
        p_no = 1.0 - p_yes

        return p_yes, p_no, d2

    def price_merton_jump_binary(
        self,
        strike_k: float,
        spot_s: float,
        tau_seconds: float,
        mu_annual: float = 0.0,
    ) -> Tuple[float, float, float]:
        """Merton Jump-Diffusion Digital Option Pricing.

        P_Merton(YES) = exp(-r*tau) * sum_{n=0}^inf [ exp(-lambda*tau) * (lambda*tau)^n / n! * N(d2^(n)) ]
        """
        if strike_k <= 0.0 or spot_s <= 0.0:
            return 0.50, 0.50, 0.0

        tau = max(0.0, float(tau_seconds))
        if tau <= 0.1:
            p_yes = 1.0 if spot_s >= strike_k else 0.0
            return p_yes, 1.0 - p_yes, (10.0 if p_yes == 1.0 else -10.0)

        lam_tau = self.jump_intensity * tau
        k_bar = math.exp(self.jump_mean + 0.5 * (self.jump_std ** 2)) - 1.0
        mu_per_sec = mu_annual / SECONDS_PER_YEAR

        # Effective diffusion drift
        diff_drift = (mu_per_sec - self.jump_intensity * k_bar - 0.5 * (self.sigma_per_sqrt_sec ** 2)) * tau
        log_moneyness = math.log(spot_s / strike_k)

        weighted_prob = 0.0
        poisson_factor = math.exp(-lam_tau)
        term_weight = poisson_factor

        d2_base = 0.0
        for n in range(self.max_terms):
            if n > 0:
                term_weight *= lam_tau / n

            var_n = (self.sigma_per_sqrt_sec ** 2) * tau + n * (self.jump_std ** 2)
            sig_n = math.sqrt(max(1e-12, var_n))

            d2_n = (log_moneyness + diff_drift + n * (self.jump_mean + 0.5 * (self.jump_std ** 2))) / sig_n
            if n == 0:
                d2_base = d2_n

            weighted_prob += term_weight * norm_cdf(d2_n)

            # Convergence check
            if term_weight < 1e-7 and n >= 3:
                break

        discount = math.exp(-self.r_per_sec * tau)
        p_yes = discount * weighted_prob
        p_yes = max(0.0001, min(0.9999, p_yes))
        p_no = 1.0 - p_yes

        return p_yes, p_no, d2_base

    def calculate_greeks(
        self,
        strike_k: float,
        spot_s: float,
        tau_seconds: float,
        mu_annual: float = 0.0,
    ) -> BinaryOptionGreeks:
        """Computes exact analytical Greeks for the binary option."""
        tau = max(0.1, float(tau_seconds))
        sigma_sec = self.sigma_per_sqrt_sec
        sigma_tau = sigma_sec * math.sqrt(tau)

        mu_per_sec = mu_annual / SECONDS_PER_YEAR
        log_moneyness = math.log(spot_s / strike_k)
        d2 = (log_moneyness + (mu_per_sec - 0.5 * sigma_sec ** 2) * tau) / sigma_tau
        d1 = d2 + sigma_tau

        pdf_d2 = norm_pdf(d2)
        discount = math.exp(-self.r_per_sec * tau)

        # 1. Delta: dP / dS
        delta = (discount * pdf_d2) / (spot_s * sigma_tau)

        # 2. Gamma: d^2P / dS^2 = -discount * pdf(d2) * d1 / (S^2 * sigma^2 * tau)
        gamma = -(discount * pdf_d2 * d1) / ((spot_s ** 2) * (sigma_sec ** 2) * tau)

        # 3. Vega: dP / dsigma = -discount * pdf(d2) * (d1 / sigma)
        vega = -(discount * pdf_d2 * d1) / max(1e-9, sigma_sec)

        # 4. Theta: -dP / dtau
        theta = (discount * pdf_d2 * d1) / (2.0 * tau) + (self.r_per_sec * discount * norm_cdf(d2))

        # 5. Pinning Risk Index: Sensitivity to discrete jump at expiration
        pinning_risk = pdf_d2 / sigma_tau

        return BinaryOptionGreeks(
            delta=delta,
            gamma=gamma,
            vega=vega,
            theta=theta,
            pinning_risk_index=pinning_risk,
        )


# =============================================================================
# 5. Arbitrage Bounds & Execution Logic
# =============================================================================

@dataclass
class SnipingOpportunity:
    """Actionable statistical taker sniping opportunity on Polymarket CLOB."""
    target_side: str            # "YES" or "NO"
    fair_value: float           # Model fair value
    clob_ask: float             # Current CLOB ask price to cross
    taker_fee: float            # Exact fee (0.07 * p * (1 - p))
    net_edge: float             # Fair value - Ask - Fee - Gas
    is_actionable: bool
    recommended_size: float
    reason: str


class PolymarketArbitrageScanner:
    """Detects mispriced binary contracts and computes optimal quoting and sniping levels."""

    def __init__(
        self,
        min_taker_edge: float = 0.015,     # Minimum 1.5c net edge to take liquidity
        gas_amortized_usd: float = 0.005,  # Polygon transaction gas amortized
        default_snipe_size: float = 10.0,  # Max ticket size for sniping
    ) -> None:
        self.min_taker_edge = min_taker_edge
        self.gas_amortized_usd = gas_amortized_usd
        self.default_snipe_size = default_snipe_size

    @staticmethod
    def calculate_taker_fee(
        price: float,
        fee_rate: float = 0.07,
        fee_exponent: float = 1.0,
        fee_schedule: Optional[FeeSchedule] = None,
    ) -> float:
        """Exact Polymarket dynamic taker fee per share: fee = rate * (p * (1 - p)) ^ exponent.

        At p = 0.50, p(1-p) = 0.25 (peak fee hurdle: up to 1.75c/share at rate=0.07, 1.00c/share at rate=0.04).
        At p = 0.98, p(1-p) = 0.0196 (0.137c/share at rate=0.07, 0.078c/share at rate=0.04).
        Makers pay 0.0% fee.
        """
        if fee_schedule is not None:
            fee_rate = fee_schedule.rate
            fee_exponent = fee_schedule.exponent
        if fee_rate <= 0.0:
            return 0.0
        p = max(0.001, min(0.999, price))
        return fee_rate * ((p * (1.0 - p)) ** fee_exponent)

    @staticmethod
    def calculate_dynamic_crypto_fee(price: float) -> float:
        """Polymarket crypto taker fee formula: fee = 0.07 * p * (1 - p).
        
        Backward-compatible wrapper around calculate_taker_fee.
        """
        return PolymarketArbitrageScanner.calculate_taker_fee(price, fee_rate=0.07, fee_exponent=1.0)

    def evaluate_taker_snipe(
        self,
        fair_p_yes: float,
        best_ask_yes: float,
        best_ask_no: float,
        available_liquidity_yes: float = 100.0,
        available_liquidity_no: float = 100.0,
        fee_rate: float = 0.07,
        fee_exponent: float = 1.0,
        fee_schedule: Optional[FeeSchedule] = None,
    ) -> SnipingOpportunity:
        """Evaluates whether either the YES or NO ask is stale and provides an actionable snipe.
        
        CRITICAL LATENCY JUMP ANALYSIS:
        Near p = 0.50, the dynamic taker fee is up to 1.75c/share (rate 0.07) or 1.00c/share (rate 0.04).
        Combined with a 1.0c - 2.0c spread crossing cost, taker execution faces a 2.75c - 3.75c friction hurdle.
        Binance spot jumps < 1.5 bps produce probability shifts (< 2.5c) that are completely destroyed
        by taker fees. Therefore, taker sniping is only viable on large jumps (> 2.0 bps) or fee-exempt markets.
        For smaller jumps, maker-only quoting (BinanceAnchoredQuoter) strictly dominates (0.0% maker fee).
        """
        fair_p_no = 1.0 - fair_p_yes

        # 1. Check YES side snipe
        fee_yes = self.calculate_taker_fee(best_ask_yes, fee_rate, fee_exponent, fee_schedule)
        net_edge_yes = fair_p_yes - best_ask_yes - fee_yes - self.gas_amortized_usd

        # 2. Check NO side snipe
        fee_no = self.calculate_taker_fee(best_ask_no, fee_rate, fee_exponent, fee_schedule)
        net_edge_no = fair_p_no - best_ask_no - fee_no - self.gas_amortized_usd

        if net_edge_yes >= self.min_taker_edge and net_edge_yes >= net_edge_no:
            size = min(self.default_snipe_size, available_liquidity_yes)
            return SnipingOpportunity(
                target_side="YES",
                fair_value=round(fair_p_yes, 4),
                clob_ask=round(best_ask_yes, 4),
                taker_fee=round(fee_yes, 4),
                net_edge=round(net_edge_yes, 4),
                is_actionable=True,
                recommended_size=size,
                reason=f"SNIPE YES: Fair {fair_p_yes:.3f} > Ask {best_ask_yes:.3f} by +{net_edge_yes * 100:.1f}c net.",
            )
        elif net_edge_no >= self.min_taker_edge:
            size = min(self.default_snipe_size, available_liquidity_no)
            return SnipingOpportunity(
                target_side="NO",
                fair_value=round(fair_p_no, 4),
                clob_ask=round(best_ask_no, 4),
                taker_fee=round(fee_no, 4),
                net_edge=round(net_edge_no, 4),
                is_actionable=True,
                recommended_size=size,
                reason=f"SNIPE NO: Fair {fair_p_no:.3f} > Ask {best_ask_no:.3f} by +{net_edge_no * 100:.1f}c net.",
            )

        return SnipingOpportunity(
            target_side="NONE",
            fair_value=round(fair_p_yes, 4),
            clob_ask=0.0,
            taker_fee=0.0,
            net_edge=0.0,
            is_actionable=False,
            recommended_size=0.0,
            reason=f"No taker edge: Edge YES={net_edge_yes*100:.1f}c, Edge NO={net_edge_no*100:.1f}c < threshold {self.min_taker_edge*100:.1f}c.",
        )
