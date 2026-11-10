"""Rigorous Microstructure Valuation of FIFO Queue Priority, CTF Gas, and Switching Frictions.

Synthesizes:
1. Maglaras-Yao (2015), Moallemi-Saglam (2013), and Cont-Kukanov-Stoikov (2014):
   Closed-form valuation of resting FIFO queue priority:
   V_queue(Q_ahead) = Size * [HalfSpread - E[AdverseSelection | fill]] * P(fill | Q_ahead) - OppCost(Q_ahead)
2. Polygon PoS CTF Gas Mechanics:
   - mergePositions: ~78,000 gas units ($0.002 - $0.015 USD at 80 Gwei)
   - splitPosition: ~76,000 - 114,000 gas units
   - CLOB Cancel (DELETE /order, DELETE /cancel-all): 0 gas (100% off-chain EIP-712 / HMAC-SHA256)
3. Synthetic Complete-Set Liquidation Parity Router:
   Closed-form minimum friction to flatten residual inventory q:
   min( q * (P_mid - P_bid), q * (P_ask_opp - (1.00 - P_mid)) + Gas_merge )
   Synthetic dominates whenever P_bid + P_ask_opp + Gas_merge/q < 1.00.
4. Total Endogenous Switching Hurdle:
   Replaces arbitrary static percentages with exact continuous economic break-even:
   Delta_Yield_net(B - A) * Tau_holding >= Cost_switch(A -> B)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from polymarketbot.fill_model import FillModel, get_fill_model
from polymarketbot.logs import get_logger

logger = get_logger("queue_valuation")

YEAR_SECONDS = 365.25 * 86_400.0


@dataclass
class QueueParameterFit:
    """Live-fitted inputs for :class:`QueuePriorityValuationModel`.

    Every parameter is an EWMA of a measured sample; no queue-model default is
    used when a measurement or a live proxy exists. The fitter consumes
    successive L2 snapshots (trade/turnover rate, queue cancellation, price
    impact, realized volatility) and the allocator's measured marginal return
    (capital hurdle). ``build_model`` returns the fitted model plus a source map
    so callers can log exactly which values were measured, proxied, or missing.
    """

    trade_rate_shares_sec: Optional[float] = None
    cancel_intensity_per_sec: Optional[float] = None
    kyle_lambda: Optional[float] = None
    volatility_annualized: Optional[float] = None
    capital_hurdle_annual: Optional[float] = None
    var_per_second: Optional[float] = None
    samples: int = 0

    def set_capital_hurdle(self, annual_rate: float) -> None:
        """Set the measured opportunity cost of capital (annualised)."""
        if annual_rate is not None and annual_rate >= 0.0:
            self.capital_hurdle_annual = float(annual_rate)

    def observe_sweep(
        self,
        *,
        dt: float,
        trade_rate_sample: Optional[float],
        cancelled_shares: float,
        previous_depth: float,
        abs_mid_change: float,
        abs_signed_flow_shares: float,
        var_per_second_sample: Optional[float],
    ) -> None:
        """Fold one full radar-sweep interval into the EWMA estimates.

        Inputs are aggregated across the universe once per sweep, not once per
        token, so the EWMA adapts over time rather than being frozen by the
        thousands of tokens in the first sweep.
        """
        if dt <= 0.0:
            return
        self.samples += 1
        alpha = 2.0 / (self.samples + 1.0)

        def ewma(old: Optional[float], new: float) -> float:
            return new if old is None else (1.0 - alpha) * old + alpha * new

        if trade_rate_sample is not None and trade_rate_sample > 0.0:
            self.trade_rate_shares_sec = ewma(self.trade_rate_shares_sec, float(trade_rate_sample))
        if previous_depth > 0.0:
            cancel = max(0.0, cancelled_shares) / previous_depth / dt
            self.cancel_intensity_per_sec = ewma(self.cancel_intensity_per_sec, cancel)
        if abs_signed_flow_shares > 0.0:
            impact_per_1000 = abs_mid_change / max(1e-9, abs_signed_flow_shares / 1000.0)
            self.kyle_lambda = ewma(self.kyle_lambda, impact_per_1000)
        if var_per_second_sample is not None and var_per_second_sample > 0.0:
            self.var_per_second = ewma(self.var_per_second, var_per_second_sample)
            self.volatility_annualized = math.sqrt(self.var_per_second * YEAR_SECONDS)

    def build_model(
        self,
        proxies: Dict[str, float],
    ) -> Tuple[Optional["QueuePriorityValuationModel"], Dict[str, Any]]:
        """Construct the model from measurements, falling back to live proxies.

        Returns ``(model, sources)``. ``model`` is ``None`` only when a required
        parameter has neither a measurement nor a live proxy, in which case the
        caller must not silently substitute a hardcoded default.
        """
        sources: Dict[str, Any] = {}
        values: Dict[str, Optional[float]] = {}

        def pick(
            name: str,
            measured: Optional[float],
            proxy: Optional[float],
            allow_zero: bool = False,
        ) -> Optional[float]:
            if measured is not None and (measured > 0.0 or (allow_zero and measured == 0.0)):
                sources[name] = "measured"
                return float(measured)
            if proxy is not None and proxy > 0.0:
                sources[name] = "live_proxy"
                return float(proxy)
            sources[name] = "unavailable"
            return None

        values["trade_arrival_rate_shares_sec"] = pick(
            "trade_arrival_rate_shares_sec",
            self.trade_rate_shares_sec,
            proxies.get("trade_rate_shares_sec"),
        )
        values["cancellation_rate_intensity"] = pick(
            "cancellation_rate_intensity",
            self.cancel_intensity_per_sec,
            proxies.get("cancel_intensity_per_sec"),
        )
        values["volatility_annualized"] = pick(
            "volatility_annualized",
            self.volatility_annualized,
            proxies.get("volatility_annualized"),
        )
        values["annual_capital_hurdle_rate"] = pick(
            "annual_capital_hurdle_rate",
            self.capital_hurdle_annual,
            proxies.get("capital_hurdle_annual"),
            allow_zero=True,
        )
        kyle = self.kyle_lambda
        if kyle is None:
            kyle = proxies.get("kyle_lambda")
            sources["kyle_lambda"] = "live_proxy" if kyle is not None else "unavailable"
        else:
            sources["kyle_lambda"] = "measured"
        values["kyle_lambda"] = kyle

        if any(v is None for v in values.values()):
            return None, {"sources": sources, "values": values, "samples": self.samples}

        model = QueuePriorityValuationModel(
            trade_arrival_rate_shares_sec=float(values["trade_arrival_rate_shares_sec"]),
            cancellation_rate_intensity=float(values["cancellation_rate_intensity"]),
            kyle_lambda=float(values["kyle_lambda"]),
            volatility_annualized=float(values["volatility_annualized"]),
            annual_capital_hurdle_rate=float(values["annual_capital_hurdle_rate"]),
        )
        return model, {"sources": sources, "values": values, "samples": self.samples}


# ============================================================================
# 1. POLYGON POS GAS & SMART CONTRACT PROFILE
# ============================================================================

@dataclass(frozen=True)
class CTFGasProfile:
    """Polygon PoS (Chain ID 137) Gas Units for Conditional Tokens Framework."""

    gas_merge_direct_min: int = 68_000       # Warm storage, binary [1, 2]
    gas_merge_direct_typical: int = 78_000   # Median observed execution
    gas_merge_direct_max: int = 95_000       # Storage clearing edge
    gas_merge_proxy_adapter: int = 142_000   # Via Safe / NegRisk adapter

    gas_split_warm: int = 76_000             # Non-zero balance update
    gas_split_typical: int = 88_000          # Average binary condition split
    gas_split_cold: int = 114_000            # Zero-to-nonzero storage writes
    gas_split_proxy: int = 155_000           # Via proxy / multicall

    gas_redeem_typical: int = 45_000         # redeemPositions, one outcome slot


@dataclass(frozen=True)
class PolygonPoSEnvironment:
    """EIP-1559 fee-market state on Polygon PoS, always measured.

    The fields have no defaults on purpose. This class previously shipped
    ``base_fee_gwei=35.0``, ``priority_fee_gwei=45.0`` and ``pol_usd_rate=0.35``
    and every caller that omitted them priced gas off those numbers. Measured
    on 2026-11-09 the all-in price was 217.54 Gwei (not 80) and POL traded
    0.08686 (not 0.35) - two wrong factors that multiplied into a third wrong
    cost. Use :meth:`measured`, or pass values you measured yourself.
    """

    base_fee_gwei: float
    priority_fee_gwei: float
    pol_usd_rate: float

    @classmethod
    def measured(
        cls,
        chain_client: Any = None,
        pol_usd_price: Optional[float] = None,
        rpc_urls: Optional[tuple[str, ...]] = None,
    ) -> "PolygonPoSEnvironment":
        """Build from live gas and POL/USD measurements; raise when unmeasurable.

        ``base_fee_gwei`` receives the *all-in* price with a zero tip, so a
        caller that already measured the effective price does not charge the
        tip twice.
        """
        from polymarketbot.strategy.onchain.ctf import gas as _gas

        reading = _gas.measure_gas(chain_client=chain_client, rpc_urls=rpc_urls)
        rate = pol_usd_price if pol_usd_price is not None else _gas.get_dynamic_pol_usd_price()
        if reading is None or rate is None:
            raise _gas.GasUnmeasured(
                f"Polygon PoS fee market unmeasured (gwei={None if reading is None else reading.gwei}, "
                f"pol_usd={rate}); refusing to build a priced environment"
            )
        return cls(
            base_fee_gwei=float(reading.gwei),
            priority_fee_gwei=0.0,
            pol_usd_rate=float(rate),
        )

    @property
    def total_gas_price_gwei(self) -> float:
        return self.base_fee_gwei + self.priority_fee_gwei

    def gas_units_to_usd(self, gas_units: int) -> float:
        """Convert gas units to exact USD cost."""
        pol_cost = gas_units * (self.total_gas_price_gwei * 1e-9)
        return pol_cost * self.pol_usd_rate


# ============================================================================
# 2. FIFO QUEUE PRIORITY VALUATION (MAGLARAS-YAO / CONT-KUKANOV-STOIKOV)
# ============================================================================

@dataclass(frozen=True)
class QueueValuationResult:
    """Valuation decomposition for resting FIFO queue priority."""

    queue_ahead: float
    quote_size: float
    half_spread_usd: float
    fill_probability: float
    expected_adverse_selection_usd: float
    opportunity_cost_usd: float
    net_queue_value_usd: float
    expected_wait_time_seconds: float


class QueuePriorityValuationModel:
    """Quantitative valuation of FIFO limit order queue priority."""

    def __init__(
        self,
        trade_arrival_rate_shares_sec: float = 25.0,  # Average fill volume rate at touch
        cancellation_rate_intensity: float = 0.05,    # Intensity of queue cancellations ahead
        kyle_lambda: float = 0.00015,                  # Kyle's lambda price impact parameter
        volatility_annualized: float = 0.45,           # Implied probability volatility
        annual_capital_hurdle_rate: float = 0.12,      # 12% cost of capital
        fill_model: Optional[FillModel] = None,        # Measured model; defaults to data/derived/fill_model.json
    ) -> None:
        self.trade_rate = max(1.0, trade_arrival_rate_shares_sec)
        self.cancel_rate = max(0.001, cancellation_rate_intensity)
        self.kyle_lambda = kyle_lambda
        self.volatility = volatility_annualized
        self.r_cost = annual_capital_hurdle_rate / (365.25 * 86_400.0)
        self.fill_model = fill_model

    def calculate_fill_probability(
        self,
        queue_ahead: Optional[float],
        quote_size: float,
        level_size_shares: Optional[float] = None,
    ) -> float:
        """Print-conditional P(fill) for a quote at a level with a measured queue.

        Feeds the measured model exactly its declared features
        (``data/derived/fill_model.json``, ``logistic.features``)::

            ratio = pre_q_ahead / pre_size_at_level
            size  = pre_size_at_level          # displayed depth at that level

        ``level_size_shares`` is the displayed depth at the quote's own price
        level; ``queue_ahead`` is the resting size ahead of the new quote at
        that price. Both are measured from the live L2 book.

        When the L2 book cannot supply the level depth (``level_size_shares``
        is ``None``, e.g. quoting inside an empty touch), the measured isotonic
        ratio buckets are used instead of extrapolating the logistic: ratio 0
        when nothing is ahead, otherwise ratio 1 because level depth >= queue
        ahead is the only bound available. GAP: there is no live per-pool level
        depth on that path, so the size feature is not evaluated.

        Only when ``queue_ahead`` itself is unknown (``None``) is the
        exponential clearance proxy used, bounded by the measured fill-rate
        support of the same file rather than hand-set 0.01/0.99 caps.

        Deliberate deviation: the numerator includes better-price depth (the
        soak's ``queue_ahead_shares`` is strict ``spread < quote``), so the
        ratio can exceed the level-local calibration; it is clamped at 1.0 and
        the measured floor P=0.2339 is a conservative bound on time-to-fill,
        not the calibrated level-local probability.
        """
        if quote_size <= 0.0:
            return 0.0
        model = self.fill_model or get_fill_model()
        if queue_ahead is None:
            proxy = math.exp(-self.cancel_rate * float(quote_size) / self.trade_rate)
            return min(model.p_ceiling, max(model.p_floor, proxy))
        ahead = max(0.0, float(queue_ahead))
        if level_size_shares is None or level_size_shares <= 0.0:
            return model.p_fill_by_ratio(0.0 if ahead <= 0.0 else 1.0)
        # The measured covariate domain requires q_ahead <= size_at_level;
        # callers whose queue measure includes better-price depth can exceed it.
        size_at_level = max(float(level_size_shares), ahead)
        return model.p_fill(ahead / size_at_level, size_at_level)

    def evaluate_queue_value(
        self,
        quote_size: float,
        queue_ahead: float,
        half_spread_cents: float,
        token_mid_price: float = 0.50,
        level_size_shares: Optional[float] = None,
    ) -> QueueValuationResult:
        """Evaluate the embedded economic option value of resting queue position.

        Deviation: ``queue_ahead`` includes better-price depth (strict
        ``spread < quote``), so the fill ratio is clamped at 1.0 and the measured
        floor P=0.2339 is a conservative time-to-fill bound, not the calibrated
        level-local probability.
        """
        if quote_size <= 0.0:
            return QueueValuationResult(
                queue_ahead=queue_ahead,
                quote_size=0.0,
                half_spread_usd=0.0,
                fill_probability=0.0,
                expected_adverse_selection_usd=0.0,
                opportunity_cost_usd=0.0,
                net_queue_value_usd=0.0,
                expected_wait_time_seconds=0.0,
            )

        half_spread_usd = half_spread_cents / 100.0
        p_fill = self.calculate_fill_probability(queue_ahead, quote_size, level_size_shares)
        # FIFO: only the shares already ahead of us wait for the trade flow.
        # GAP: data/derived/l3_queue has per-print survival/replenishment latency
        # but no derived seconds-to-clear table, so no measured depletion model.
        expected_wait_sec = max(0.0, queue_ahead) / self.trade_rate

        # Kyle's lambda adverse selection conditioned on fill. The parameter is
        # fitted as price change per 1000 shares of signed flow
        # (QueueParameterFit.observe_sweep), so the expected impact of a fill of
        # `quote_size` shares is lambda * Q / 1000 dollars per share. The
        # previous `lambda * sqrt(Q) * volatility` mixed units -- annualised
        # volatility has no place in a per-fill impact -- and with the measured
        # inputs of the 2026-11-09 soak (lambda ~ 1e-3, vol 8.5-14) it exceeded
        # the half spread on every reward quote, flooring net_edge_per_share at
        # zero. Queue priority was therefore valued at exactly $0, cancels
        # looked free, and the harvester re-quoted on every radar snapshot.
        adverse_selection_usd = self.kyle_lambda * float(quote_size) / 1000.0
        net_edge_per_share = max(0.0, half_spread_usd - adverse_selection_usd)

        gross_value = quote_size * net_edge_per_share * p_fill
        # Capital at risk is the measured mid, not a clamped stand-in: a quote
        # on a 0.02 token ties up 0.02 USDC per share, and inflating that to
        # 0.05 overstates the opportunity cost by 2.5x.
        capital_notional = quote_size * max(0.0, min(1.0, token_mid_price))
        opp_cost = capital_notional * self.r_cost * expected_wait_sec

        net_value = max(0.0, gross_value - opp_cost)

        return QueueValuationResult(
            queue_ahead=round(queue_ahead, 2),
            quote_size=round(quote_size, 2),
            half_spread_usd=round(half_spread_usd, 4),
            fill_probability=round(p_fill, 4),
            expected_adverse_selection_usd=round(adverse_selection_usd, 6),
            opportunity_cost_usd=round(opp_cost, 6),
            net_queue_value_usd=round(net_value, 4),
            expected_wait_time_seconds=round(expected_wait_sec, 2),
        )


# ============================================================================
# 3. INVENTORY LIQUIDATION SOLVER (DIRECT CROSS VS SYNTHETIC COMPLETION)
# ============================================================================

class LiquidationRoute(str, Enum):
    DIRECT_CROSS = "DIRECT_CROSS"
    SYNTHETIC_MERGE = "SYNTHETIC_MERGE"
    ZERO_INVENTORY = "ZERO_INVENTORY"


@dataclass(frozen=True)
class LiquidationFrictionResult:
    route: LiquidationRoute
    optimal_friction_usd: float
    cost_direct_usd: float
    cost_synthetic_usd: float
    synthetic_savings_usd: float
    effective_exit_price: float
    proceeds_usd: float
    is_arbitrage_discount: bool
    explanation: str


def solve_inventory_liquidation_friction(
    q: float,
    p_mid: float,
    p_bid: float,
    p_ask_opp: float,
    gas_merge_usd: float,
    taker_fee_rate: float = 0.0,
) -> LiquidationFrictionResult:
    """Solve the closed-form minimum friction for flattening residual inventory q.

    Option A: Sell q into p_bid. Cost = q * (p_mid - p_bid + fee).
    Option B: Buy q of opposite token at p_ask_opp + CTF merge.
              Cost = q * (p_ask_opp - (1.00 - p_mid) + fee) + gas_merge.
    """
    abs_q = abs(q)
    if abs_q < 1e-6:
        return LiquidationFrictionResult(
            route=LiquidationRoute.ZERO_INVENTORY,
            optimal_friction_usd=0.0,
            cost_direct_usd=0.0,
            cost_synthetic_usd=0.0,
            synthetic_savings_usd=0.0,
            effective_exit_price=p_mid,
            proceeds_usd=0.0,
            is_arbitrage_discount=False,
            explanation="Zero residual inventory.",
        )

    # Route A: Direct Crossing
    fee_direct = p_bid * taker_fee_rate
    cost_direct = abs_q * (p_mid - p_bid + fee_direct)
    proceeds_direct = abs_q * (p_bid - fee_direct)

    # Route B: Synthetic Complete-Set Completion
    p_mid_opp = 1.00 - p_mid
    fee_opp = p_ask_opp * taker_fee_rate
    cost_synthetic = abs_q * (p_ask_opp - p_mid_opp + fee_opp) + gas_merge_usd
    proceeds_synthetic = abs_q * 1.00 - abs_q * (p_ask_opp + fee_opp) - gas_merge_usd

    synthetic_savings = cost_direct - cost_synthetic
    # Synthetic Dominance Theorem: P_bid + P_ask_opp + Gas/q < 1.00
    is_discount = (p_bid + fee_direct) + (p_ask_opp + fee_opp) + (gas_merge_usd / abs_q) < 1.00

    if cost_synthetic < cost_direct:
        route = LiquidationRoute.SYNTHETIC_MERGE
        opt_cost = cost_synthetic
        proceeds = proceeds_synthetic
        exit_p = proceeds / abs_q
        expl = (
            f"Synthetic completion wins by ${synthetic_savings:.4f} "
            f"({(synthetic_savings/abs_q)*100:.2f}c/sh improvement). "
            f"Exit: ${exit_p:.4f} vs direct bid: ${p_bid:.4f}."
        )
    else:
        route = LiquidationRoute.DIRECT_CROSS
        opt_cost = cost_direct
        proceeds = proceeds_direct
        exit_p = proceeds / abs_q
        expl = (
            f"Direct bid crossing wins by ${-synthetic_savings:.4f} "
            f"({(-synthetic_savings/abs_q)*100:.2f}c/sh cheaper). "
            f"Exit: ${exit_p:.4f}."
        )

    return LiquidationFrictionResult(
        route=route,
        optimal_friction_usd=round(opt_cost, 6),
        cost_direct_usd=round(cost_direct, 6),
        cost_synthetic_usd=round(cost_synthetic, 6),
        synthetic_savings_usd=round(synthetic_savings, 6),
        effective_exit_price=round(exit_p, 6),
        proceeds_usd=round(proceeds, 6),
        is_arbitrage_discount=is_discount,
        explanation=expl,
    )


# ============================================================================
# 4. TOTAL CAPITAL MIGRATION HURDLE ENGINE
# ============================================================================

@dataclass(frozen=True)
class SwitchingCostDecomposition:
    queue_value_lost_usd: float
    liquidation_friction_usd: float
    gas_fees_usd: float
    opportunity_cost_downtime_usd: float
    total_switching_cost_usd: float
    liquidation_route: LiquidationRoute

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue_value_lost_usd": round(self.queue_value_lost_usd, 4),
            "liquidation_friction_usd": round(self.liquidation_friction_usd, 4),
            "gas_fees_usd": round(self.gas_fees_usd, 4),
            "opportunity_cost_downtime_usd": round(self.opportunity_cost_downtime_usd, 4),
            "total_switching_cost_usd": round(self.total_switching_cost_usd, 4),
            "liquidation_route": self.liquidation_route.value,
        }


@dataclass(frozen=True)
class HurdleDecision:
    should_switch: bool
    delta_yield_daily_usd: float
    required_delta_yield_daily_usd: float
    net_alpha_holding_usd: float
    switching_cost_total_usd: float
    holding_horizon_hours: float
    amortized_cost_daily_usd: float
    breakeven_holding_hours: float = 0.0
    breakeven_holding_seconds: float = 0.0
    reason: str = ""


class CapitalMigrationEngine:
    """Quantifies switching frictions and validates net-alpha hurdles."""

    def __init__(
        self,
        gas_env: Optional[PolygonPoSEnvironment] = None,
        ctf_profile: Optional[CTFGasProfile] = None,
        queue_model: Optional[QueuePriorityValuationModel] = None,
        safety_hurdle_pct: float = 0.0,
    ) -> None:
        # No default environment: an unmeasured fee market must not be invented.
        self.gas_env = gas_env
        self.ctf_profile = ctf_profile or CTFGasProfile()
        self.queue_model = queue_model or QueuePriorityValuationModel()
        self.safety_hurdle_pct = safety_hurdle_pct

    def compute_queue_loss(
        self,
        resting_bid_size: float,
        resting_ask_size: float,
        queue_ahead_bid: float,
        queue_ahead_ask: float,
        half_spread_cents: float,
        mid_price: float = 0.50,
        queue_model: Optional[QueuePriorityValuationModel] = None,
        level_size_bid: Optional[float] = None,
        level_size_ask: Optional[float] = None,
    ) -> float:
        """Value of FIFO queue priority surrendered upon cancellation.

        ``queue_model`` lets the caller inject a model fitted from measured
        microstructure instead of the constructor default. ``level_size_bid`` /
        ``level_size_ask`` are the displayed depths at the quote price levels
        (the fill model's ``pre_size_at_level``); omitting them falls back to
        the measured isotonic ratio buckets (GAP: no level depth measured).
        """
        model = queue_model or self.queue_model
        val_bid = model.evaluate_queue_value(
            quote_size=resting_bid_size,
            queue_ahead=queue_ahead_bid,
            half_spread_cents=half_spread_cents,
            token_mid_price=mid_price,
            level_size_shares=level_size_bid,
        ).net_queue_value_usd

        val_ask = model.evaluate_queue_value(
            quote_size=resting_ask_size,
            queue_ahead=queue_ahead_ask,
            half_spread_cents=half_spread_cents,
            token_mid_price=mid_price,
            level_size_shares=level_size_ask,
        ).net_queue_value_usd

        return val_bid + val_ask

    def compute_downtime_cost(
        self,
        daily_yield_a: float,
        latency_seconds: float = 4.5,
    ) -> float:
        """Forgone yield over the measured unquoted interval.

        ``latency_seconds`` is the whole interval the market is unquoted, so a
        cancel-then-place move passes both measured flights and nothing else:
        the published scoring rule has **no minimum rest time** (the venue
        samples the book once per minute and scores what rests at that instant,
        ``docs.polymarket.com/programs/liquidity-rewards``), so an order begins
        accruing as soon as it lands. The default is a placeholder; measured
        callers always supply the round trip they actually observe.
        """
        return daily_yield_a * (max(0.0, latency_seconds) / 86_400.0)

    def compute_switching_cost(
        self,
        q_a: float,
        p_mid_a: float,
        p_bid_a: float,
        p_ask_opp_a: float,
        resting_bid_size: float,
        resting_ask_size: float,
        queue_ahead_bid: float,
        queue_ahead_ask: float,
        half_spread_cents_a: float,
        daily_yield_a: float,
        has_matched_complete_sets: bool = True,
        transit_latency_seconds: float = 4.5,
        taker_fee_rate: float = 0.0,
    ) -> SwitchingCostDecomposition:
        """Compute the full economic switching friction: Cost_switch(A -> B).

        Raises :class:`GasUnmeasured` when no measured fee market is available:
        an unpriced merge must not be treated as free.
        """
        env = self.gas_env
        if env is None:
            env = PolygonPoSEnvironment.measured()
            self.gas_env = env
        merge_gas = env.gas_units_to_usd(self.ctf_profile.gas_merge_direct_typical)

        # 1. Queue Priority Loss
        q_loss = self.compute_queue_loss(
            resting_bid_size=resting_bid_size,
            resting_ask_size=resting_ask_size,
            queue_ahead_bid=queue_ahead_bid,
            queue_ahead_ask=queue_ahead_ask,
            half_spread_cents=half_spread_cents_a,
            mid_price=p_mid_a,
        )

        # 2. Inventory Liquidation Friction
        liq = solve_inventory_liquidation_friction(
            q=q_a,
            p_mid=p_mid_a,
            p_bid=p_bid_a,
            p_ask_opp=p_ask_opp_a,
            gas_merge_usd=merge_gas,
            taker_fee_rate=taker_fee_rate,
        )

        # 3. Gas Fees: off-chain cancel is $0.00; mergePositions is merge_gas
        # Avoid double counting: if synthetic merge won, merge gas is inside liq.optimal_friction_usd
        if liq.route == LiquidationRoute.SYNTHETIC_MERGE:
            gas_fees = 0.0
        elif has_matched_complete_sets:
            gas_fees = merge_gas
        else:
            gas_fees = 0.0

        # 4. Downtime Opportunity Cost
        # A migration is sequential: the old quote must pay its cancel flight
        # and the new quote pays its placement flight, and the quote accrues
        # from the moment it lands - the published rule has no minimum rest
        # time. One transit latency cannot cover the unquoted interval of a
        # cancel-then-place move.
        opp_cost = self.compute_downtime_cost(
            daily_yield_a=daily_yield_a,
            latency_seconds=2.0 * max(0.0, transit_latency_seconds),
        )

        # Baseline switching friction (network transit, order cancel/replacement latency)
        total_cost = max(0.02, q_loss + liq.optimal_friction_usd + gas_fees + opp_cost)

        return SwitchingCostDecomposition(
            queue_value_lost_usd=q_loss,
            liquidation_friction_usd=liq.optimal_friction_usd,
            gas_fees_usd=gas_fees,
            opportunity_cost_downtime_usd=opp_cost,
            total_switching_cost_usd=total_cost,
            liquidation_route=liq.route,
        )

    def evaluate_switching_hurdle(
        self,
        y_net_a_daily: float,
        y_net_b_daily: float,
        switching_cost_usd: float,
        holding_horizon_hours: float = 4.0,
    ) -> HurdleDecision:
        """Evaluate the Net Alpha condition: Delta_Yield * Tau >= Cost_switch."""
        tau_days = max(1.0 / 24.0, holding_horizon_hours / 24.0)
        delta_y = y_net_b_daily - y_net_a_daily

        amortized_cost_daily = switching_cost_usd / max(1e-4, tau_days)
        required_delta_daily = amortized_cost_daily + (self.safety_hurdle_pct * max(0.0, y_net_a_daily))

        net_alpha_holding = (delta_y * tau_days) - switching_cost_usd
        should_switch = delta_y >= required_delta_daily

        if delta_y > 0 and switching_cost_usd >= 0:
            breakeven_days = switching_cost_usd / delta_y
            breakeven_hours = breakeven_days * 24.0
            breakeven_seconds = breakeven_days * 86_400.0
        else:
            breakeven_hours = float("inf")
            breakeven_seconds = float("inf")

        if should_switch:
            reason = (
                f"POSITIVE ALPHA: Delta yield +${delta_y:.2f}/d covers switching cost ${switching_cost_usd:.4f} "
                f"in {breakeven_hours:.2f}h ({breakeven_seconds:.0f}s breakeven). "
                f"Net holding alpha over {holding_horizon_hours:.1f}h: ${net_alpha_holding:+.4f}."
            )
        else:
            reason = (
                f"HURDLE REJECTED: Delta yield +${delta_y:.2f}/d requires {breakeven_hours:.2f}h to break even on "
                f"${switching_cost_usd:.4f} switching cost (hurdle required: +${required_delta_daily:.2f}/d over {holding_horizon_hours:.1f}h)."
            )

        return HurdleDecision(
            should_switch=should_switch,
            delta_yield_daily_usd=round(delta_y, 4),
            required_delta_yield_daily_usd=round(required_delta_daily, 4),
            net_alpha_holding_usd=round(net_alpha_holding, 4),
            switching_cost_total_usd=round(switching_cost_usd, 4),
            holding_horizon_hours=holding_horizon_hours,
            amortized_cost_daily_usd=round(amortized_cost_daily, 4),
            breakeven_holding_hours=round(breakeven_hours, 4) if breakeven_hours != float("inf") else 9999.0,
            breakeven_holding_seconds=round(breakeven_seconds, 1) if breakeven_seconds != float("inf") else 999999.0,
            reason=reason,
        )
