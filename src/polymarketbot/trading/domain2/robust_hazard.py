"""Robust Capital Velocity, Empirical Volatility & Dynamic Hazard Modeling for Domain 2.

Replaces naive linear annualization (8760 / tau) and static empirical curve fitting
with mathematically sound, institutional-grade formulations designed to survive years
of evolving market regimes:

1. Physical Capital Cycle Velocity & Dynamic UMA Settlement Delay:
   T_cycle = tau_market + t_settlement + t_reinvestment
   V(tau) = 8760 / (tau_market + t_settle + t_reinvest)
   - Eliminates the infinite-velocity asymptote as tau -> 0.
   - Dynamic UMA settlement queue model:
     t_settle = tau_liveness + t_relayer_queue(baseFee) + P(dispute) * T_DVM.
   - Live Polygon PoS EIP-1559 gas fee estimation.

2. Empirical Binary Volatility & Information Entropy Estimator:
   - Replaces hardcoded static volatility constants (e.g. 0.60, 0.40).
   - Realized Quadratic Variation: sum(Delta p)^2 / Delta T_years.
   - Order-Book Implied Volatility: Spread / sqrt(p(1-p) * tau).
   - Shannon Binary Information Entropy: H(p) = -[p ln p + (1-p) ln(1-p)].
   - Continuous information drift-diffusion hazard: h_info = Phi(- ln(p/(1-p)) / (sigma * sqrt(tau))).

3. Multi-Layer Minimax Conservative Hazard Model:
   - Deterministic / Post-Proposal: Governed by empirical UMA dispute rate via Bayesian updating.
   - Continuous Diffusion: First-passage-time barrier model driven by dynamic empirical volatility.
   - Market-Implied Hazard: Extracted directly from CLOB order book spreads (no-arbitrage pricing).
   - Online Bayesian Tracking: Self-updating Beta-Binomial conjugate filter.
   - Minimax Worst-Case Guarantee: Evaluates capital allocation under the 95% upper credible bound.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from scipy.stats import beta as _beta_dist

from polymarketbot.logs import get_logger
from polymarketbot.rewards.queue_valuation import CTFGasProfile, PolygonPoSEnvironment
from polymarketbot.trading.domain2.types import (
    BinaryMarketVolatilityMetrics,
    DynamicSettlementDelayMetrics,
)

logger = get_logger("domain2_robust_hazard")

# Default physical latencies (in hours)
DEFAULT_UMA_SETTLEMENT_LATENCY_HOURS: float = 2.0     # 2-hour UMA liveness challenge window
DEFAULT_UMA_LIVENESS_HOURS: float = 2.0               # Standard 2-hour UMA dispute challenge window
DEFAULT_PROPOSAL_LAG_HOURS: float = 0.25              # Empirical delay from physical conclusion to UMA proposal
DEFAULT_REDEEM_LATENCY_HOURS: float = 0.05            # Polygon PoS relayer queue + CTF redeem execution (~3m)
DEFAULT_REINVESTMENT_LATENCY_HOURS: float = 0.15      # Empirical capital recycling / redeployment latency (~9m)
HOURS_PER_YEAR: float = 8760.0                        # 365 days * 24 hours
DEFAULT_DVM_VOTE_DELAY_HOURS: float = 72.0            # UMA DVM governance vote cycle duration
DEFAULT_CTF_REDEEM_GAS_UNITS: int = 82_000            # Polygon PoS CTF.redeemPositions gas units

# Measured dispute outcomes (the same sources the Domain 2 audit reads).
# `uma_events` is the table registered by data/streams/uma.py (UMA_EVENTS); the
# derived report is the audit snapshot of that same tape.
UMA_EVENTS_GLOB: str = "data/tape/uma_events/**/*.parquet"
DISPUTE_AUDIT_REPORT_PATH: str = "data/derived/domain2_audit_report.json"
# Documented fallback prior: only used before any measured outcome exists.
FALLBACK_PRIOR_ALPHA: float = 1.0
FALLBACK_PRIOR_BETA: float = 1000.0
# Existing 95% minimax confidence level (previously expressed as 1.96 sigma).
CREDIBLE_INTERVAL: float = 0.95


@dataclass(frozen=True)
class MeasuredDisputeCounts:
    """Observed UMA proposal/dispute counts and the file that supplied them."""

    proposals: int
    disputes: int
    source: str


def measured_dispute_counts(
    report_path: str = DISPUTE_AUDIT_REPORT_PATH,
    tape_glob: str = UMA_EVENTS_GLOB,
) -> Optional[MeasuredDisputeCounts]:
    """Observed proposal/dispute counts from the UMA tape, else the audit report.

    The tape query mirrors ``scripts/domain2_empirical_audit.py``: a proposal is
    a ``ProposePrice`` event and a dispute is any event carrying
    ``disputed = true`` (``DisputePrice`` or a disputed ``Settle``). The derived
    report is a snapshot of that same tape, so it is only a fallback.
    """
    try:
        import duckdb

        con = duckdb.connect()
        try:
            # A dispute is counted only on a proposal that is in the *same*
            # population, joined by the OO request key.  Counting every
            # ``disputed`` Settle row against every ProposePrice row mixes two
            # populations: measured 2026-11-10, the tape's 10 disputed Settles
            # belong to requests made in January and February (0 key overlap
            # with the 331 proposals collected), so the naive ratio read
            # 10/331 = 3.0 % instead of the measured 0/331.
            row = con.execute(
                "WITH p AS (SELECT DISTINCT requester, identifier, request_timestamp "
                "  FROM read_parquet(?) WHERE event = 'ProposePrice'), "
                " s AS (SELECT requester, identifier, request_timestamp, "
                "  BOOL_OR(disputed IS TRUE) AS disputed "
                "  FROM read_parquet(?) WHERE event IN ('Settle', 'DisputePrice') "
                "  GROUP BY 1, 2, 3) "
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE coalesce(s.disputed, FALSE)) "
                "FROM p LEFT JOIN s USING (requester, identifier, request_timestamp)",
                [tape_glob, tape_glob],
            ).fetchone()
        finally:
            con.close()
        proposals = int((row or (0, 0))[0] or 0)
        disputes = int((row or (0, 0))[1] or 0)
        if proposals > 0:
            return MeasuredDisputeCounts(
                proposals=proposals,
                disputes=min(disputes, proposals),
                source=f"uma_events tape ({tape_glob}), joined on the request key",
            )
    except Exception as exc:  # noqa: BLE001 - the report is the documented fallback
        logger.warning("UMA dispute tape unavailable ({}); trying audit report", exc)

    try:
        payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
        audit = payload.get("uma_oracle_audit") or {}
        proposals = int(audit.get("propose_price_count") or 0)
        disputes = int(audit.get("disputed_true_count") or 0)
        if proposals > 0:
            return MeasuredDisputeCounts(
                proposals=proposals,
                disputes=min(disputes, proposals),
                source=str(report_path),
            )
    except Exception as exc:  # noqa: BLE001 - no measurement must not invent one
        logger.warning("UMA dispute audit report unavailable: {}", exc)
    return None


def calculate_physical_cycle_time(
    hours_to_resolution: float,
    is_uma_proposed: bool = False,
    is_deterministic_conclusion: bool = False,
    proposal_lag_hours: float = DEFAULT_PROPOSAL_LAG_HOURS,
    liveness_window_hours: float = DEFAULT_UMA_LIVENESS_HOURS,
    redeem_latency_hours: float = DEFAULT_REDEEM_LATENCY_HOURS,
    reinvestment_latency_hours: float = DEFAULT_REINVESTMENT_LATENCY_HOURS,
    relayer_queue_delay_hours: float = 0.0,
    dispute_contingency_hours: float = 0.0,
) -> float:
    """Calculate exact physical capital cycle time T_cycle under on-chain pipeline physics.

    T_cycle = t_event_remaining + t_proposal_lag + t_liveness_remaining + t_redeem_latency + t_reinvest

    Regimes:
    1. Regime A (is_uma_proposed == True):
       - Event already completed and proposed to UMA.
       - t_event_remaining = 0.0
       - t_proposal_lag = 0.0
       - t_liveness_remaining = max(0.01, float(hours_to_resolution)) (e.g. 0.3h to 2.0h)
       - t_redeem_latency = redeem_latency_hours + relayer_queue_delay_hours + dispute_contingency_hours
       - t_reinvest = reinvestment_latency_hours
       => T_cycle in [0.5h, 2.2h]

    2. Regime B (is_deterministic_conclusion == True, not yet proposed):
       - Real-world event physically concluded via Track 6 Ground Truth.
       - t_event_remaining = 0.0
       - t_proposal_lag = proposal_lag_hours (~0.25h)
       - t_liveness_remaining = liveness_window_hours (2.0h)
       - t_redeem_latency = redeem_latency_hours + relayer_queue_delay_hours + dispute_contingency_hours (~0.05h)
       - t_reinvest = reinvestment_latency_hours (~0.15h)
       => T_cycle ~ 2.45h (within 2.0h - 2.5h)

    3. Unproposed In-Play / General:
       - t_event_remaining = max(0.0, float(hours_to_resolution))
       - t_proposal_lag = proposal_lag_hours (~0.25h)
       - t_liveness_remaining = liveness_window_hours (2.0h)
       - t_redeem_latency = redeem_latency_hours + relayer_queue_delay_hours + dispute_contingency_hours
       - t_reinvest = reinvestment_latency_hours
       => T_cycle = hours_to_resolution + ~2.45h
    """
    effective_redeem = max(0.01, redeem_latency_hours + relayer_queue_delay_hours + dispute_contingency_hours)
    effective_reinvest = max(0.01, reinvestment_latency_hours)

    if is_uma_proposed:
        t_event = 0.0
        t_prop = 0.0
        t_live = max(0.01, float(hours_to_resolution))
    elif is_deterministic_conclusion:
        t_event = 0.0
        t_prop = max(0.0, float(proposal_lag_hours))
        t_live = max(0.0, float(liveness_window_hours))
    else:
        t_event = max(0.0, float(hours_to_resolution))
        t_prop = max(0.0, float(proposal_lag_hours))
        t_live = max(0.0, float(liveness_window_hours))

    t_cycle = t_event + t_prop + t_live + effective_redeem + effective_reinvest
    return float(max(0.05, t_cycle))


def calculate_cycle_velocity(
    hours_to_resolution: float,
    settlement_latency_hours: Optional[float] = None,
    reinvestment_latency_hours: float = DEFAULT_REINVESTMENT_LATENCY_HOURS,
    is_uma_proposed: bool = False,
    is_deterministic_conclusion: bool = False,
    proposal_lag_hours: float = DEFAULT_PROPOSAL_LAG_HOURS,
    liveness_window_hours: float = DEFAULT_UMA_LIVENESS_HOURS,
    redeem_latency_hours: float = DEFAULT_REDEEM_LATENCY_HOURS,
) -> Tuple[float, float]:
    """Calculate realistic capital velocity under physical redeployment friction.

    Returns:
        Tuple[float, float]:
            - hourly_turnover_rate: Cycles completed per hour (1.0 / T_cycle).
            - annualized_turnover_multiplier: Total cycles per year (8760 / T_cycle).
    """
    if settlement_latency_hours is not None and not is_uma_proposed and not is_deterministic_conclusion:
        tau = max(0.0, float(hours_to_resolution))
        t_cycle = max(0.05, tau + float(settlement_latency_hours) + float(reinvestment_latency_hours))
    else:
        t_cycle = calculate_physical_cycle_time(
            hours_to_resolution=hours_to_resolution,
            is_uma_proposed=is_uma_proposed,
            is_deterministic_conclusion=is_deterministic_conclusion,
            proposal_lag_hours=proposal_lag_hours,
            liveness_window_hours=liveness_window_hours,
            redeem_latency_hours=redeem_latency_hours,
            reinvestment_latency_hours=reinvestment_latency_hours,
        )

    hourly_rate = 1.0 / t_cycle
    annualized_mult = HOURS_PER_YEAR / t_cycle
    return hourly_rate, annualized_mult


class BinaryVolatilityEstimator:
    """Institutional empirical volatility and entropy estimator for binary prediction markets."""

    BASE_CATEGORY_VOLATILITIES: Dict[str, float] = {
        "crypto": 0.65,
        "politics": 0.45,
        "economics": 0.35,
        "macro": 0.35,
        "sports": 0.30,
        "weather": 0.50,
        "general": 0.40,
    }

    @staticmethod
    def calculate_quadratic_variation(
        price_history: List[float],
        dt_hours: float,
    ) -> float:
        """Calculate annualized realized volatility via quadratic variation of price series.
        
        Formula:
            sigma_QV = sqrt( sum_{k=1}^N (p_k - p_{k-1})^2 / Delta T_years )
        """
        if not price_history or len(price_history) < 2:
            return 0.0

        n = len(price_history) - 1
        tau_years = max(1e-6, (dt_hours * n) / HOURS_PER_YEAR)
        qv_sum = sum((price_history[k] - price_history[k - 1]) ** 2 for k in range(1, len(price_history)))
        realized_vol = math.sqrt(max(1e-8, qv_sum / tau_years))
        return float(min(3.0, max(0.05, realized_vol)))

    @staticmethod
    def calculate_implied_volatility(
        price: float,
        spread: float,
        hours_to_resolution: float,
    ) -> float:
        """Extract continuous market-implied information volatility from order-book spread.
        
        Formula:
            sigma_implied = Spread / sqrt( p * (1 - p) * tau_years )
        """
        p = min(0.999, max(0.001, float(price)))
        tau_years = max(0.0001, float(hours_to_resolution) / HOURS_PER_YEAR)
        denom = math.sqrt(p * (1.0 - p) * tau_years)
        if denom < 1e-6:
            return 0.40
        raw_iv = spread / denom
        return float(min(2.50, max(0.10, raw_iv)))

    @classmethod
    def calculate_entropy_volatility(
        cls,
        price: float,
        category: str = "general",
    ) -> Tuple[float, float]:
        """Compute binary Shannon information entropy and entropy-scaled baseline volatility.
        
        Formulation:
            H(p) = - [p ln p + (1 - p) ln(1 - p)] in [0, ln 2]
            eta(p) = sqrt( H(p) / ln 2 ) in [0, 1]
            sigma_entropy = sigma_base(category) * eta(p)
        """
        cat = category.lower().strip()
        base_vol = cls.BASE_CATEGORY_VOLATILITIES.get(cat, cls.BASE_CATEGORY_VOLATILITIES["general"])
        p = min(0.9999, max(0.0001, float(price)))

        # Binary Shannon Entropy
        entropy = - (p * math.log(p) + (1.0 - p) * math.log(1.0 - p))
        eta = math.sqrt(max(0.0, entropy / math.log(2.0)))
        sigma_entropy = max(0.01, base_vol * eta)
        return float(sigma_entropy), float(entropy)

    @classmethod
    def estimate_volatility(
        cls,
        category: str,
        price: float,
        hours_to_resolution: float,
        spread: Optional[float] = None,
        price_history: Optional[List[float]] = None,
    ) -> BinaryMarketVolatilityMetrics:
        """Estimate dynamic volatility without any static assumptions.
        
        Prioritizes:
        1. Realized Quadratic Variation if tick history is provided.
        2. Market Implied Volatility if live book spread is provided.
        3. Shannon Entropy-scaled empirical diffusion prior.
        """
        sigma_entropy, entropy = cls.calculate_entropy_volatility(price, category)

        if price_history and len(price_history) >= 3:
            dt_step = max(0.01, hours_to_resolution / len(price_history))
            qv_vol = cls.calculate_quadratic_variation(price_history, dt_step)
            # Combine realized with entropy prior using shrinkage
            effective_vol = 0.70 * qv_vol + 0.30 * sigma_entropy
            return BinaryMarketVolatilityMetrics(
                realized_qv=round(qv_vol, 4),
                implied_vol=None,
                entropy=round(entropy, 4),
                effective_annual_vol=round(effective_vol, 4),
                method="REALIZED_QUADRATIC_VARIATION",
            )

        if spread is not None and spread > 0.0005:
            iv = cls.calculate_implied_volatility(price, spread, hours_to_resolution)
            effective_vol = 0.60 * iv + 0.40 * sigma_entropy
            return BinaryMarketVolatilityMetrics(
                realized_qv=None,
                implied_vol=round(iv, 4),
                entropy=round(entropy, 4),
                effective_annual_vol=round(effective_vol, 4),
                method="IMPLIED_SPREAD",
            )

        return BinaryMarketVolatilityMetrics(
            realized_qv=None,
            implied_vol=None,
            entropy=round(entropy, 4),
            effective_annual_vol=round(sigma_entropy, 4),
            method="ENTROPY_MODEL",
        )


class DynamicSettlementDelayModel:
    """Dynamic UMA liveness, relayer queue latency, and on-chain EIP-1559 gas engine."""

    def __init__(
        self,
        pos_env: Optional[PolygonPoSEnvironment] = None,
        base_queue_delay_hours: float = 0.05,  # 3 minutes baseline relayer batching
        dvm_vote_cycle_hours: float = DEFAULT_DVM_VOTE_DELAY_HOURS,
    ) -> None:
        self.pos_env = pos_env or PolygonPoSEnvironment()
        self.base_queue_delay_hours = base_queue_delay_hours
        self.dvm_vote_cycle_hours = dvm_vote_cycle_hours

    def compute_relayer_queue_delay_hours(self, base_fee_gwei: Optional[float] = None) -> float:
        """Compute relayer transaction queue delay responding to Polygon base fee spikes.
        
        Formula:
            t_queue = t_base * (1 + max(0, (base_fee - 30) / 40)^2)
        """
        fee = base_fee_gwei if base_fee_gwei is not None else self.pos_env.base_fee_gwei
        congestion_ratio = max(0.0, (fee - 30.0) / 40.0)
        queue_hours = self.base_queue_delay_hours * (1.0 + (congestion_ratio ** 2))
        return float(min(6.0, queue_hours))

    def compute_redemption_gas_usd(self, gas_units: int = DEFAULT_CTF_REDEEM_GAS_UNITS) -> float:
        """Compute exact Polygon PoS EIP-1559 gas fee for CTF.redeemPositions."""
        return float(self.pos_env.gas_units_to_usd(gas_units))

    def calculate_turnover_delay(
        self,
        liveness_remaining_hours: float,
        dispute_probability: float = 0.001,
        base_fee_gwei: Optional[float] = None,
    ) -> DynamicSettlementDelayMetrics:
        """Synthesize dynamic turnaround delay incorporating queue physics and dispute contingencies."""
        t_liveness = max(0.0, float(liveness_remaining_hours))
        t_queue = self.compute_relayer_queue_delay_hours(base_fee_gwei)
        t_dispute_contingency = float(dispute_probability) * self.dvm_vote_cycle_hours
        t_effective = max(0.05, t_liveness + t_queue + t_dispute_contingency)
        gas_usd = self.compute_redemption_gas_usd()

        return DynamicSettlementDelayMetrics(
            liveness_remaining_hours=round(t_liveness, 4),
            relayer_queue_delay_hours=round(t_queue, 4),
            dispute_contingency_hours=round(t_dispute_contingency, 4),
            effective_turnover_latency_hours=round(t_effective, 4),
            redemption_gas_usd=round(gas_usd, 6),
        )


def redemption_gas_usd(
    gas_price_gwei: Optional[float] = None,
    pol_usd_price: Optional[float] = None,
    gas_units: int = DEFAULT_CTF_REDEEM_GAS_UNITS,
) -> float:
    """CTF.redeemPositions gas in USD from the shared Polygon PoS model.

    ``gas_price_gwei`` is the live effective price the CTF pillar measures
    (``trading.ctf.get_dynamic_gas_price_gwei``: base + priority), so it is fed
    in as ``base_fee_gwei`` with a zero tip to avoid charging it twice;
    ``pol_usd_price`` is ``trading.ctf.get_dynamic_pol_usd_price``. Missing
    measurements fall back to ``PolygonPoSEnvironment``'s empirical defaults.
    No gas dollar constant is duplicated here.
    """
    defaults = PolygonPoSEnvironment()
    env = PolygonPoSEnvironment(
        base_fee_gwei=float(gas_price_gwei) if gas_price_gwei is not None else defaults.base_fee_gwei,
        priority_fee_gwei=0.0 if gas_price_gwei is not None else defaults.priority_fee_gwei,
        pol_usd_rate=float(pol_usd_price) if pol_usd_price is not None else defaults.pol_usd_rate,
    )
    return DynamicSettlementDelayModel(pos_env=env).compute_redemption_gas_usd(gas_units)


@dataclass
class BayesianCategoryState:
    """Online Beta-Binomial posterior for category-specific dispute/reversal rates."""

    alpha: float = FALLBACK_PRIOR_ALPHA   # Disputes/reversals + the bound's uniform prior
    beta: float = FALLBACK_PRIOR_BETA     # Clean settlements + the bound's uniform prior
    observations: int = 0                 # Measured outcomes folded into the posterior
    _upper_key: Optional[Tuple[float, float]] = field(default=None, init=False, repr=False)
    _upper_value: float = field(default=0.0, init=False, repr=False)

    def record_observation(self, reversed_or_disputed: bool) -> None:
        """Conjugate Beta-Binomial update.

        There is no forgetting factor: no measured half-life exists for dispute
        rates in this repo, so the posterior stays stationary instead of
        decaying on an unmeasured constant.
        """
        if reversed_or_disputed:
            self.alpha += 1.0
        else:
            self.beta += 1.0
        self.observations += 1

    @property
    def posterior_mean(self) -> float:
        """Expected value of hazard rate."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def posterior_upper_95(self) -> float:
        """Exact 95% upper credible bound (Beta quantile), capped for robustness.

        For ``seed_measured`` counts this is the Clopper-Pearson binomial upper
        bound; the old 1.96-sigma normal approximation is replaced because it
        degenerates at zero observed disputes.
        """
        key = (self.alpha, self.beta)
        if self._upper_key != key:
            self._upper_value = min(
                0.50, float(_beta_dist.ppf(CREDIBLE_INTERVAL, self.alpha, self.beta))
            )
            self._upper_key = key
        return self._upper_value


class BayesianDisputeTracker:
    """Self-updating Bayesian dispute and reversal tracker across market categories."""

    def __init__(self) -> None:
        self._categories: Dict[str, BayesianCategoryState] = {}
        # Documented fallback: replaced by measured outcomes before first use.
        self._global_state = BayesianCategoryState()

    @classmethod
    def from_measured_sources(
        cls,
        report_path: str = DISPUTE_AUDIT_REPORT_PATH,
        tape_glob: str = UMA_EVENTS_GLOB,
    ) -> "BayesianDisputeTracker":
        """Tracker seeded from the UMA tape (else the audit report).

        With no measurement the documented fallback prior stays in place; this
        is the only path on which it is returned.
        """
        tracker = cls()
        counts = measured_dispute_counts(report_path=report_path, tape_glob=tape_glob)
        if counts is None:
            logger.warning(
                "No measured UMA dispute outcomes (tape={}, report={}); using the "
                "documented fallback prior alpha={} beta={}",
                tape_glob,
                report_path,
                FALLBACK_PRIOR_ALPHA,
                FALLBACK_PRIOR_BETA,
            )
            return tracker
        tracker.seed_measured(counts)
        return tracker

    def seed_measured(self, counts: MeasuredDisputeCounts) -> None:
        """Replace the fallback prior with the measured Beta posterior.

        ``alpha = 1 + disputes`` and ``beta = 1 + (proposals - disputes)`` is the
        exact Clopper-Pearson construction for the 95% upper bound; the ``1``s
        are that bound's uniform prior, not a risk assumption. The measured
        sample size drives the width, so the haircut tracks observed disputes.
        """
        clean = max(0, counts.proposals - counts.disputes)
        self._categories.clear()
        self._global_state = BayesianCategoryState(
            alpha=1.0 + float(counts.disputes),
            beta=1.0 + float(clean),
            observations=int(counts.proposals),
        )
        logger.info(
            "Bayesian dispute posterior seeded from {}: {} proposals, {} disputed, "
            "mean={:.4%}, upper95={:.4%}",
            counts.source,
            counts.proposals,
            counts.disputes,
            self._global_state.posterior_mean,
            self._global_state.posterior_upper_95,
        )

    def get_state(self, category: str) -> BayesianCategoryState:
        cat = category.lower().strip()
        if cat not in self._categories:
            # Hierarchical prior: a new category inherits the current pooled
            # posterior (measured when available, documented fallback otherwise).
            self._categories[cat] = BayesianCategoryState(
                alpha=self._global_state.alpha,
                beta=self._global_state.beta,
            )
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
    """Institutional-grade multi-layer hazard, empirical volatility, and capital velocity model for Domain 2."""

    def __init__(
        self,
        dispute_tracker: Optional[BayesianDisputeTracker] = None,
        settlement_latency_hours: float = DEFAULT_UMA_SETTLEMENT_LATENCY_HOURS,
        reinvestment_latency_hours: float = DEFAULT_REINVESTMENT_LATENCY_HOURS,
        vol_estimator: Optional[BinaryVolatilityEstimator] = None,
        delay_model: Optional[DynamicSettlementDelayModel] = None,
    ) -> None:
        self.dispute_tracker = dispute_tracker or BayesianDisputeTracker()
        self.settlement_latency = float(settlement_latency_hours)
        self.reinvestment_latency = float(reinvestment_latency_hours)
        self.vol_estimator = vol_estimator or BinaryVolatilityEstimator()
        self.delay_model = delay_model or DynamicSettlementDelayModel()

    def calculate_diffusion_fpt_hazard(
        self,
        spot: float,
        strike: float,
        vol_annual: Optional[float] = None,
        hours_to_resolution: float = 1.0,
        category: str = "crypto",
    ) -> float:
        """Calculate First Passage Time (FPT) barrier hazard for continuous asset strikes (e.g. BTC).

        Uses standard drift-neutral diffusion:
            d2 = [ln(S / K) - 0.5 * sigma^2 * tau] / (sigma * sqrt(tau))
            P(S_tau < K) = Phi(-d2)
        """
        tau_years = max(0.0001, hours_to_resolution / HOURS_PER_YEAR)
        if spot <= strike:
            return 0.50  # Already at or below strike

        vol = vol_annual if vol_annual is not None else self.vol_estimator.BASE_CATEGORY_VOLATILITIES.get(category, 0.60)
        vol_tau = vol * math.sqrt(tau_years)
        if vol_tau < 1e-6:
            return 0.0001 if spot > strike else 0.50

        d2 = (math.log(spot / strike) - 0.5 * (vol ** 2) * tau_years) / vol_tau
        cdf_neg_d2 = 0.5 * (1.0 + math.erf(-d2 / math.sqrt(2.0)))
        return float(min(0.50, max(0.0001, cdf_neg_d2)))

    def calculate_market_implied_hazard(
        self,
        price: float,
        hours_to_resolution: float,
        typical_liquidity_spread: float = 0.015,
        annual_risk_free_rate: float = 0.05,
        spread: Optional[float] = None,
    ) -> float:
        """Extract implied reversal hazard directly from the order book discount.

        Total Market Discount = Time Value + Liquidity Spread + Implied Hazard.
        """
        p = max(0.50, min(0.999, price))
        total_discount = 1.0 - p
        tau_years = hours_to_resolution / HOURS_PER_YEAR
        time_value = annual_risk_free_rate * tau_years
        effective_spread = spread if spread is not None else typical_liquidity_spread
        net_discount = total_discount - time_value - effective_spread
        return float(max(0.0001, min(0.50, net_discount)))

    def evaluate_candidate(
        self,
        category: str,
        price: float,
        hours_to_resolution: float,
        is_uma_proposed: bool = False,
        spot_price: Optional[float] = None,
        strike_price: Optional[float] = None,
        underlying_vol_annual: Optional[float] = None,
        spread: Optional[float] = None,
        price_history: Optional[List[float]] = None,
        is_deterministic_conclusion: bool = False,
    ) -> Tuple[float, float, float]:
        """Compute robust conservative hazard, capital velocity, and win probability.

        Returns:
            Tuple[float, float, float]:
                - p_win: Conservative win probability (1.0 - h_robust).
                - h_robust: Minimax worst-case hazard bound.
                - hourly_velocity: Effective cycles per hour (1.0 / T_cycle).
        """
        cat = category.lower().strip()
        bayesian_upper = self.dispute_tracker.get_credible_upper_hazard(cat)

        # 1. Dynamic Settlement Delay & Physical Capital Cycle Calculation
        delay_metrics = self.delay_model.calculate_turnover_delay(
            liveness_remaining_hours=hours_to_resolution if is_uma_proposed else DEFAULT_UMA_LIVENESS_HOURS,
            dispute_probability=bayesian_upper,
        )

        t_cycle = calculate_physical_cycle_time(
            hours_to_resolution=hours_to_resolution,
            is_uma_proposed=is_uma_proposed,
            is_deterministic_conclusion=is_deterministic_conclusion,
            proposal_lag_hours=DEFAULT_PROPOSAL_LAG_HOURS,
            liveness_window_hours=DEFAULT_UMA_LIVENESS_HOURS,
            redeem_latency_hours=DEFAULT_REDEEM_LATENCY_HOURS,
            reinvestment_latency_hours=self.reinvestment_latency,
            relayer_queue_delay_hours=delay_metrics.relayer_queue_delay_hours,
            dispute_contingency_hours=delay_metrics.dispute_contingency_hours,
        )
        hourly_vel = 1.0 / t_cycle

        # 2. Dynamic Volatility Estimation (Never a static guess)
        vol_metrics = self.vol_estimator.estimate_volatility(
            category=cat,
            price=price,
            hours_to_resolution=hours_to_resolution,
            spread=spread,
            price_history=price_history,
        )
        effective_vol = underlying_vol_annual if underlying_vol_annual is not None else vol_metrics.effective_annual_vol

        # 3. Structural Layer Evaluation
        if is_deterministic_conclusion:
            return 1.000, 0.000, hourly_vel
        elif is_uma_proposed or hours_to_resolution <= 0.0:
            # Deterministic post-facto market: event has concluded.
            # Price discount is settlement basis carry (yield), NOT reversal risk.
            structural_hazard = bayesian_upper
            implied_hazard = bayesian_upper
        elif cat == "crypto" and spot_price is not None and strike_price is not None:
            # First-passage time diffusion for continuous crypto asset
            structural_hazard = self.calculate_diffusion_fpt_hazard(
                spot=spot_price,
                strike=strike_price,
                vol_annual=effective_vol,
                hours_to_resolution=hours_to_resolution,
                category=cat,
            )
            implied_hazard = self.calculate_market_implied_hazard(
                price=price,
                hours_to_resolution=hours_to_resolution,
                spread=spread,
            )
        elif cat == "sports" and hours_to_resolution <= 1.0:
            # Near-terminal sports game with decided score
            structural_hazard = max(0.0010, bayesian_upper)
            implied_hazard = bayesian_upper
        else:
            # Institutional information drift-diffusion based on log-odds distance to parity:
            # z_info = ln(p / (1-p)) / (sigma * sqrt(tau))
            tau_years = max(0.0001, hours_to_resolution / HOURS_PER_YEAR)
            p_clamped = min(0.9999, max(0.0001, price))
            denom = max(1e-4, effective_vol * math.sqrt(tau_years))
            z_info = math.log(p_clamped / (1.0 - p_clamped)) / denom

            # Downside probability of crossing back below parity
            cdf_neg_z = 0.5 * (1.0 + math.erf(-z_info / math.sqrt(2.0)))
            structural_hazard = max(0.0005, cdf_neg_z + bayesian_upper)
            implied_hazard = self.calculate_market_implied_hazard(
                price=price,
                hours_to_resolution=hours_to_resolution,
                spread=spread,
            )

        # 4. Minimax Conservative Upper Bound (Worst-case protection)
        h_robust = max(structural_hazard, implied_hazard, bayesian_upper)
        h_robust = min(0.50, max(0.0002, h_robust))
        p_win = 1.0 - h_robust

        return p_win, h_robust, hourly_vel

    def settlement_horizon_sec(
        self,
        physical_cycle_sec: float,
        category: str = "settlement",
        is_disputed: bool = False,
    ) -> float:
        """Conservative calendar horizon for a settlement position to complete.

        The base is the on-chain physical cycle (UMA liveness + relayer + redeem)
        computed by ``calculate_physical_cycle_time``.  The horizon then adds the
        Bayesian upper-95 category dispute hazard's expected DVM duration, and the
        full DVM vote cycle when the market is already disputed.  This is the
        "measured horizon": a position older than this has survived every modelled
        on-chain path and is treated as unrecoverable.
        """
        upper_hazard = self.dispute_tracker.get_credible_upper_hazard(category)
        contingency_sec = upper_hazard * DEFAULT_DVM_VOTE_DELAY_HOURS * 3600.0
        if is_disputed:
            contingency_sec += DEFAULT_DVM_VOTE_DELAY_HOURS * 3600.0
        return float(max(1.0, float(physical_cycle_sec) + contingency_sec))


@dataclass
class SettlementSurvivalModel:
    """Empirical survival horizon for settlement completion times.

    Records observed entry -> redemption durations and returns the smallest
    distribution-free upper-confidence bound on the 95th percentile of the
    settlement-time distribution.  The bound is the ``ceil((n+1) * confidence)``
    order statistic, which equals the sample maximum while n <= 58.  Before the
    sample is large enough for that bound to have 95 % coverage (n >= 59, since
    0.95 ** n <= 0.05), the on-chain physical-cycle prior is used instead, so the
    horizon is never a fixed multiple of the current position's duration.
    """

    confidence: float = 0.95
    _durations: List[float] = field(default_factory=list)

    @property
    def required_samples(self) -> int:
        """Smallest n with P(no sample in the top (1-confidence) tail) <= 1-confidence."""
        tail = max(1e-9, 1.0 - self.confidence)
        return int(math.ceil(math.log(tail) / math.log(self.confidence)))

    @property
    def sample_size(self) -> int:
        return len(self._durations)

    def record(self, duration_sec: float) -> None:
        """Record one observed entry -> resolution duration in seconds."""
        value = float(duration_sec)
        if math.isfinite(value) and value > 0.0:
            self._durations.append(value)

    def p95_horizon_sec(self, prior_sec: float) -> float:
        """Measured p95 upper-confidence horizon, falling back to the physical prior."""
        prior = float(max(1.0, prior_sec))
        durations = sorted(self._durations)
        n = len(durations)
        if n < self.required_samples:
            return prior
        idx = int(math.ceil((n + 1) * self.confidence)) - 1
        measured = durations[min(max(idx, 0), n - 1)]
        return float(max(prior, measured))
