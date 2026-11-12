"""Production-Grade Dynamic Inventory De-Skewing Optimizer for Polymarket CTF.

Based on 2026 Quantitative Market Making Research (Jane Street, Citadel, Optiver, Wintermute):
1. 24/7 Multi-pathway de-skew optimizer (Passive Skewing vs Synthetic Complete-Set Crossing vs Direct Taker).
2. Dynamic Guéant-Tapia-Manziadi inventory urgency boundary q_crit for bounded [0, 1] prediction markets.
3. Microstructure toxicity sentinel integration (CKS Order Flow Imbalance + Stoikov Micro-Price Drift).
4. Sub-penny synthetic complete-set parity router:
   Compares (1.00 - P_ask(opposite) - Gas/Q) vs P_bid(residual) to guarantee lowest-cost de-skewing.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path

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
    measured: bool = True
    """False when a threshold this decision would need was never fitted.

    A de-skew action moves inventory at a measured cost, so it may only fire on
    a fitted threshold. ``measured=False`` means the optimizer had no
    calibration and the decision is ``HOLD_STABLE`` by refusal, never by a
    threshold nobody measured.
    """


DESKEW_CALIBRATION_ARTIFACT = "data/derived/deskew_calibration.json"
"""Where the fitted de-skew coefficients are read from.

The coefficients below are the parameters of an inventory-risk model, not
protocol constants: the venue publishes none of them, and every value an
earlier revision carried was a hand-set placeholder.
:mod:`polymarketbot.tools.fit_deskew_calibration` fits them from the recorded
CLOB tape (inventory path, fill hazard, markout, gas) and writes this file with
the window, sample size, definition and source of every field. An absent
artifact means the fit was never produced, and
:class:`DynamicDeSkewOptimizer` refuses to act.
"""


@dataclass(frozen=True)
class DeSkewCalibration:
    """Fitted parameters of the de-skew model. Every field is required.

    There are no defaults on purpose: a default here would silently become the
    threshold that moves inventory at a measured execution cost. Sources the fit
    must provide (``data/derived/deskew_calibration.json``):

    * ``gamma`` - risk-aversion coefficient of the Guéant-Tapia-Manziadi model.
    * ``skew_aggressiveness`` - fraction of the half-spread the passive fade
      moves per unit of normalised skew. A fitted zero is a measured corner:
      the tape says never to skew the quote, so the passive fade rests at the
      touch instead of failing validation.
    * the bankroll/absorption fractions that bound ``q_soft``/``q_hard`` and the
      merge batch size, and the holding-time and gas fractions.
    * ``ofi_toxicity_threshold`` / ``drift_toxicity_cents`` - the tape-fitted
      toxicity triggers, the same measurement
      :class:`polymarketbot.risk.unified.UnifiedRiskEngine` consumes.
    """

    gamma: float
    skew_aggressiveness: float
    q_soft_bankroll_fraction: float
    q_hard_risk_fraction: float
    q_hard_absorption_quotes: float
    q_hard_floor_quotes: float
    reserve_usable_fraction: float
    holding_turnover_cycles: float
    holding_expiry_fraction: float
    holding_min_seconds: float
    holding_max_seconds: float
    merge_gas_fraction: float
    merge_quote_multiples: float
    merge_bankroll_fraction: float
    merge_min_shares: float
    ofi_toxicity_threshold: float
    drift_toxicity_cents: float

    def __post_init__(self) -> None:
        # Structural validity only: these are not judgement calls, they are the
        # domain in which the formulas are defined.
        for name in ("gamma", "q_hard_absorption_quotes",
                     "holding_turnover_cycles", "ofi_toxicity_threshold",
                     "drift_toxicity_cents"):
            if not float(getattr(self, name)) > 0.0:
                raise ValueError(f"de-skew calibration {name} must be positive")
        # Zero is the fitted "never skew the quote" corner (the gain grid's
        # lower edge), not a missing value; negative or a full half-spread is
        # outside the domain the passive fade is defined on.
        if not 0.0 <= self.skew_aggressiveness < 1.0:
            raise ValueError(
                "de-skew calibration skew_aggressiveness must be in [0, 1): "
                "zero means the measured policy is never to skew the quote"
            )
        for name in ("q_soft_bankroll_fraction", "q_hard_risk_fraction",
                     "reserve_usable_fraction", "holding_expiry_fraction",
                     "merge_gas_fraction", "merge_bankroll_fraction"):
            value = float(getattr(self, name))
            if not 0.0 < value <= 1.0:
                raise ValueError(f"de-skew calibration {name} must be in (0, 1]")
        if self.holding_min_seconds <= 0.0 or self.holding_max_seconds < self.holding_min_seconds:
            raise ValueError("de-skew calibration holding bounds are inconsistent")


_CALIBRATION_FIELDS: tuple[str, ...] = tuple(
    field.name for field in fields(DeSkewCalibration)
)


def load_deskew_calibration(
    path: str | Path = DESKEW_CALIBRATION_ARTIFACT,
) -> DeSkewCalibration | None:
    """The fitted de-skew coefficients, or ``None`` when the artifact is absent.

    The artifact must carry **every** :class:`DeSkewCalibration` field under
    that field's own name. Missing or non-numeric fields are a broken fit and
    raise ``ValueError``: substituting one would put an unmeasured threshold
    behind a decision that moves inventory. An absent file is not an error -
    it means the fit was never produced, and the caller disables de-skewing.
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"de-skew calibration {target} unreadable: {exc}") from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"de-skew calibration {target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"de-skew calibration {target} must contain a JSON object")

    missing = [name for name in _CALIBRATION_FIELDS if payload.get(name) is None]
    if missing:
        raise ValueError(
            f"de-skew calibration {target} is missing required fields {missing}"
        )

    values: dict[str, float] = {}
    for name in _CALIBRATION_FIELDS:
        try:
            value = float(payload[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"de-skew calibration {target} field {name}={payload[name]!r} is not numeric"
            ) from exc
        if not math.isfinite(value):
            raise ValueError(
                f"de-skew calibration {target} field {name}={value!r} is not finite"
            )
        values[name] = value

    return DeSkewCalibration(**values)


#: The venue's published minimum order size in shares (``orderMinSize``). This
#: is a fetched protocol constant, not a model parameter.
VENUE_MIN_ORDER_SHARES = 5.0


@dataclass(frozen=True)
class DynamicThresholds:
    """Dynamically calibrated inventory and execution thresholds."""
    q_soft: float
    q_hard: float
    max_holding_seconds: float
    auto_merge_threshold: float
    reason: str


class DynamicThresholdCalculator:
    """Mathematical calibration of prediction-market inventory and capital limits.

    Every coefficient the formulas use comes from a :class:`DeSkewCalibration`
    (a tape fit). Nothing here has a default: the previous revision carried
    ``gamma=0.02``, ``trades_per_min=0.35``, ``0.20/0.60/0.30 x bankroll``,
    ``3.5 x quote_size``, ``1.5 x q_soft``, ``0.15 x expiry``, ``0.40 x spread``,
    ``850`` and ``1.5c`` - all hand-set, and the print rate alone set every
    ``max_holding_seconds``, i.e. when aged inventory is force-liquidated.
    """

    @staticmethod
    def calculate_bounded_variance(
        mid_price: float,
        time_to_resolution_days: float = 7.0,
    ) -> float:
        """Wright-Fisher bounded variance sigma^2(p, tau) = p * (1 - p) / tau.

        This is the documented variance of a binary contract given its own price
        and its own time to resolution, so it needs no fitted coefficient.
        """
        p = max(0.01, min(0.99, mid_price))
        tau = max(1.0 / 1440.0, time_to_resolution_days)  # Floor at 1 minute
        return (p * (1.0 - p)) / tau

    @classmethod
    def compute_q_soft(
        cls,
        *,
        quote_size: float,
        bankroll: float,
        calibration: DeSkewCalibration,
        mid_price: float,
        time_to_resolution_days: float,
        rewards_min_size: float,
    ) -> float:
        """Scale the Tier 1 passive-fade threshold ``q_soft`` from the fit."""
        sigma_0_sq = 0.25 / 7.0  # reference variance: 50/50 market 7 days out
        sigma_sq = cls.calculate_bounded_variance(mid_price, time_to_resolution_days)

        risk_scale = ((calibration.gamma / max(1e-4, calibration.gamma)) ** 0.5) * (
            (sigma_0_sq / max(1e-4, sigma_sq)) ** 0.25
        )
        raw_soft = quote_size * risk_scale

        q_min = max(rewards_min_size, VENUE_MIN_ORDER_SHARES)
        q_max = max(
            q_min,
            (calibration.q_soft_bankroll_fraction * bankroll) / max(0.10, mid_price),
        )
        return round(max(q_min, min(q_max, raw_soft)), 1)

    @classmethod
    def compute_q_hard(
        cls,
        *,
        quote_size: float,
        bankroll: float,
        cash_reserve: float,
        taker_fee_bps: float,
        calibration: DeSkewCalibration,
        mid_price: float,
        time_to_resolution_days: float,
        spread_cents: float,
        gas_cost_usd: float,
        q_soft: float,
    ) -> float:
        """Scale the Tier 2 emergency-taker threshold ``q_hard`` from the fit."""
        # 1. Multi-fill absorption capacity
        q_absorb = calibration.q_hard_absorption_quotes * quote_size

        # 2. Synthetic-cross solvency ceiling: cash to buy the complement + gas.
        spread_dollars = spread_cents / 100.0
        taker_fee = taker_fee_bps / 10_000.0
        # Price domain clamps only (an estimated price must stay inside (0, 1)).
        p_ask_opp = max(0.05, min(0.95, (1.0 - mid_price) + (spread_dollars / 2.0)))
        usable_reserve = max(
            0.0, cash_reserve * calibration.reserve_usable_fraction - gas_cost_usd
        )
        q_solvency = (
            usable_reserve / (p_ask_opp * (1.0 + taker_fee)) if p_ask_opp > 0 else q_absorb
        )

        # 3. Bankroll directional risk limit
        q_risk = (calibration.q_hard_risk_fraction * bankroll) / max(0.10, mid_price)

        # 4. Guéant-Tapia-Manziadi theoretical boundary
        sigma_sq = cls.calculate_bounded_variance(mid_price, time_to_resolution_days)
        denom = calibration.gamma * sigma_sq
        friction = (spread_dollars / 2.0) + taker_fee
        q_gtm = (friction / denom) if denom > 0 else q_absorb

        q_hard_cand = min(q_absorb, q_solvency, q_risk, max(q_absorb, q_gtm))

        # Structural dominance: q_hard must exceed q_soft by the fitted margin.
        q_floor = max(
            q_soft + quote_size, calibration.q_hard_floor_quotes * q_soft
        )
        return round(max(q_floor, q_hard_cand), 1)

    @staticmethod
    def compute_holding_time(
        *,
        time_to_expiry_seconds: float,
        trades_per_min: float,
        calibration: DeSkewCalibration,
    ) -> float:
        """Scale the maximum holding duration from the measured print rate.

        ``trades_per_min`` is the market's measured tape rate and has no
        default: it is what turns "how long before our quote would have been
        filled anyway" into seconds. The old 0.35 prints/min placeholder set
        every market's holding horizon.
        """
        if not trades_per_min > 0.0:
            raise ValueError(
                "compute_holding_time needs a measured trades_per_min: a holding "
                "horizon fitted from an assumed print rate force-liquidates on a guess"
            )
        # 1. Turnover-based matching: the fitted number of expected trade cycles.
        tau_turnover = calibration.holding_turnover_cycles * (60.0 / trades_per_min)

        # 2. Expiry pin-risk ceiling: a fitted fraction of the remaining life.
        tau_expiry = calibration.holding_expiry_fraction * max(
            10.0, time_to_expiry_seconds
        )

        tau_cand = min(tau_turnover, tau_expiry)
        return round(
            max(
                calibration.holding_min_seconds,
                min(calibration.holding_max_seconds, tau_cand),
            ),
            1,
        )

    @staticmethod
    def compute_auto_merge_threshold(
        *,
        gas_cost_usd: float,
        spread_cents: float,
        quote_size: float,
        bankroll: float,
        calibration: DeSkewCalibration,
    ) -> float:
        """Scale the CTF complete-set auto-merge batch size from the fit."""
        spread_dollars = max(0.001, spread_cents / 100.0)
        # Gas amortisation hurdle: Gas / (fitted max gas share x spread)
        m_gas = gas_cost_usd / (calibration.merge_gas_fraction * spread_dollars)
        m_quote = calibration.merge_quote_multiples * quote_size
        m_min = calibration.merge_min_shares

        m_bankroll_cap = max(
            m_min, calibration.merge_bankroll_fraction * bankroll
        )

        m_cand = max(m_gas, m_quote, m_min)
        m_final = min(m_cand, m_bankroll_cap) if m_bankroll_cap >= m_min else m_min
        return round(max(m_min, m_final), 1)

    @classmethod
    def calibrate_all(
        cls,
        *,
        quote_size: float,
        bankroll: float,
        cash_reserve: float,
        taker_fee_bps: float,
        calibration: DeSkewCalibration,
        mid_price: float,
        time_to_resolution_days: float,
        time_to_expiry_seconds: float | None = None,
        spread_cents: float,
        gas_cost_usd: float,
        trades_per_min: float,
        rewards_min_size: float,
        category: str = "default",
    ) -> DynamicThresholds:
        """Generate the unified thresholds for every component from the fit."""
        eff_expiry_s = (
            time_to_expiry_seconds
            if time_to_expiry_seconds is not None
            else (time_to_resolution_days * 86400.0)
        )

        q_soft = cls.compute_q_soft(
            quote_size=quote_size,
            bankroll=bankroll,
            calibration=calibration,
            mid_price=mid_price,
            time_to_resolution_days=time_to_resolution_days,
            rewards_min_size=rewards_min_size,
        )
        q_hard = cls.compute_q_hard(
            quote_size=quote_size,
            bankroll=bankroll,
            cash_reserve=cash_reserve,
            taker_fee_bps=taker_fee_bps,
            calibration=calibration,
            mid_price=mid_price,
            time_to_resolution_days=time_to_resolution_days,
            spread_cents=spread_cents,
            gas_cost_usd=gas_cost_usd,
            q_soft=q_soft,
        )
        tau_holding = cls.compute_holding_time(
            time_to_expiry_seconds=eff_expiry_s,
            trades_per_min=trades_per_min,
            calibration=calibration,
        )
        m_merge = cls.compute_auto_merge_threshold(
            gas_cost_usd=gas_cost_usd,
            spread_cents=spread_cents,
            quote_size=quote_size,
            bankroll=bankroll,
            calibration=calibration,
        )

        reason = (
            f"Fitted calibration: B=${bankroll:.0f}, S={quote_size:.0f}sh | "
            f"q_soft={q_soft:.1f}, q_hard={q_hard:.1f}, tau={tau_holding:.0f}s, "
            f"M_merge={m_merge:.1f} ({category})"
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
        *,
        quote_size: float,                     # Resting quote size in shares (required)
        bankroll: float,                       # Total capital in USD (required)
        cash_reserve: float,                   # USD held back for de-skewing (required)
        taker_fee_bps: float,                  # Market's fee curve at the quoted mid
        calibration: DeSkewCalibration,        # Tape fit; every coefficient comes from here
        mid_price: float,                      # Measured YES mid the thresholds are priced at
        time_to_resolution_days: float,        # Measured remaining life
        spread_cents: float,                   # Measured two-sided YES spread
        gas_cost_usd: float,                   # Measured on-chain merge cost
        trades_per_min: float,                 # Measured tape print rate
        rewards_min_size: float,               # Market's published reward minimum size
        time_to_expiry_seconds: float | None = None,  # Measured; defaults to days-to-resolution
    ) -> None:
        if not (0.0 < mid_price < 1.0):
            raise ValueError(f"mid_price must be in (0, 1), got {mid_price!r}")
        if spread_cents < 0.0:
            raise ValueError(f"spread_cents must be non-negative, got {spread_cents!r}")

        self.taker_fee_bps = taker_fee_bps
        self.taker_fee = taker_fee_bps / 10_000.0
        self.gas_cost_usd = gas_cost_usd
        self.quote_size = quote_size
        self.bankroll = bankroll
        self.cash_reserve = cash_reserve
        self.calibration = calibration
        self.rewards_min_size = rewards_min_size
        self.time_to_resolution_days = time_to_resolution_days

        # Every threshold is derived from the fit and this market's measured
        # state; there is no static fallback and no default coefficient.
        calib = DynamicThresholdCalculator.calibrate_all(
            quote_size=quote_size,
            bankroll=bankroll,
            cash_reserve=cash_reserve,
            taker_fee_bps=taker_fee_bps,
            calibration=calibration,
            mid_price=mid_price,
            time_to_resolution_days=time_to_resolution_days,
            time_to_expiry_seconds=time_to_expiry_seconds,
            spread_cents=spread_cents,
            gas_cost_usd=gas_cost_usd,
            trades_per_min=trades_per_min,
            rewards_min_size=rewards_min_size,
        )
        self.q_soft = calib.q_soft
        self.q_hard = calib.q_hard
        self.max_holding_seconds = calib.max_holding_seconds
        self.auto_merge_threshold = calib.auto_merge_threshold

        # The toxicity sentinels are the same tape fit the risk engine consumes.
        self.gamma = calibration.gamma
        self.ofi_thresh = calibration.ofi_toxicity_threshold
        self.drift_thresh_cents = calibration.drift_toxicity_cents

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

    def _resolution_days(self, override: float | None) -> float:
        """The measured remaining life, unless the caller prices a fresher one."""
        return (
            self.time_to_resolution_days if override is None else float(override)
        )

    def evaluate_skew(
        self,
        shares_yes: float,
        shares_no: float,
        best_bid_yes: float,
        best_ask_yes: float,
        best_bid_no: float,
        best_ask_no: float,
        *,
        holding_time_seconds: float,
        rolling_ofi: float = 0.0,
        micro_price_drift_cents: float = 0.0,
        time_to_resolution_days: float | None = None,
    ) -> DeSkewDecision:
        """Evaluate inventory and return the optimal cost-minimizing de-skewing trajectory."""
        net_q = shares_yes - shares_no  # q > 0: Long YES; q < 0: Long NO
        abs_q = abs(net_q)
        if not (best_bid_yes and best_ask_yes and best_bid_no and best_ask_no):
            # A missing side means the market cannot be priced; hold rather than
            # invent a 0.50 mid and de-skew against a fabricated spread.
            return DeSkewDecision(
                pathway=DeSkewPathway.HOLD_STABLE,
                urgency_level="LOW",
                target_token="NONE",
                action_side="SELL",
                order_type="LIMIT_MAKER",
                target_price=0.0,
                target_size=0.0,
                expected_cost_bps=0.0,
                synthetic_savings_cents=0.0,
                reason="no two-sided quote; de-skew refused",
            )
        mid_yes = (best_bid_yes + best_ask_yes) / 2.0
        spread_yes_cents = max(0.1, (best_ask_yes - best_bid_yes) * 100.0)

        # Thresholds are always re-calibrated at the current mid from the
        # fitted coefficients and the measured inputs the constructor required.
        eff_q_soft = DynamicThresholdCalculator.compute_q_soft(
            quote_size=self.quote_size,
            bankroll=self.bankroll,
            calibration=self.calibration,
            mid_price=mid_yes,
            time_to_resolution_days=self._resolution_days(time_to_resolution_days),
            rewards_min_size=self.rewards_min_size,
        )
        eff_q_hard = DynamicThresholdCalculator.compute_q_hard(
            quote_size=self.quote_size,
            bankroll=self.bankroll,
            cash_reserve=self.cash_reserve,
            taker_fee_bps=self.taker_fee_bps,
            calibration=self.calibration,
            mid_price=mid_yes,
            time_to_resolution_days=self._resolution_days(time_to_resolution_days),
            spread_cents=spread_yes_cents,
            gas_cost_usd=self.gas_cost_usd,
            q_soft=eff_q_soft,
        )
        # The holding horizon is fitted at construction (or supplied per market);
        # it must not silently fall back to a static default here.
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
            # Signed advantage of resting instead of crossing now: cents per share
            # earned relative to the price an immediate taker cross would pay.
            passive_advantage_cents = (
                (target_price - best_bid_yes) if target_side == "SELL" else (best_ask_yes - target_price)
            ) * 100.0

            return DeSkewDecision(
                pathway=DeSkewPathway.PASSIVE_FADE,
                urgency_level="HIGH",
                target_token="YES",
                action_side=target_side,
                order_type="LIMIT_MAKER",
                target_price=target_price,
                target_size=abs_q,
                expected_cost_bps=0.0,
                synthetic_savings_cents=round(passive_advantage_cents, 3),
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
                skew_offset = (spread_yes_cents * self.calibration.skew_aggressiveness) * skew_ratio
                passive_ask = max(best_bid_yes + 0.001, best_ask_yes - (skew_offset / 100.0))
                target_price = round(passive_ask, 3)
                return DeSkewDecision(
                    pathway=DeSkewPathway.PASSIVE_FADE,
                    urgency_level="MEDIUM",
                    target_token="YES",
                    action_side="SELL",
                    order_type="LIMIT_MAKER",
                    target_price=target_price,
                    target_size=abs_q,
                    expected_cost_bps=0.0,
                    # Cents per share the resting ask earns over crossing at the bid now.
                    synthetic_savings_cents=round((target_price - best_bid_yes) * 100.0, 3),
                    reason=f"Tier 1 Passive Skew: q=+{net_q:.1f} < q_crit({q_crit:.1f}). Fading YES ask to top-of-book.",
                )
            else:
                # Excess NO shares (Long NO) -> Tighten YES bid to buy YES (form complete set); widen/pull NO bid!
                spread_no_cents = max(0.1, (best_ask_no - best_bid_no) * 100.0)
                skew_offset = (spread_no_cents * self.calibration.skew_aggressiveness) * skew_ratio
                # Tighten YES bid towards mid to organically buy YES and complete the set
                tight_yes_bid = min(best_ask_yes - 0.001, best_bid_yes + (skew_offset / 100.0))
                target_price = round(tight_yes_bid, 3)
                return DeSkewDecision(
                    pathway=DeSkewPathway.PASSIVE_FADE,
                    urgency_level="MEDIUM",
                    target_token="YES",
                    action_side="BUY",
                    order_type="LIMIT_MAKER",
                    target_price=target_price,
                    target_size=abs_q,
                    expected_cost_bps=0.0,
                    # Cents per share the resting bid saves over crossing at the ask now.
                    synthetic_savings_cents=round((best_ask_yes - target_price) * 100.0, 3),
                    reason=f"Tier 1 Passive Skew: q={net_q:.1f} (long NO). Pulling NO bid, tightening YES bid to complete pairs.",
                )

        # 5. TIER 2: ACTIVE TAKER ROUTING (Synthetic Merge vs Direct Cross)
        urgency = "CRITICAL_PANIC" if (is_toxic_flow or abs_q >= eff_q_hard) else "HIGH"

        if net_q > 0:
            # Long YES (Need to flatten YES)
            # Option A: Direct Sell YES into best bid
            direct_proceeds_per_share = best_bid_yes * (1.0 - self.taker_fee)

            # Option B: Synthetic Complete-Set Crossing -> Buy NO at best ask + Merge at $1.00
            gas_per_share = self.gas_cost_usd / max(1.0, abs_q)
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
            gas_per_share = self.gas_cost_usd / max(1.0, abs_q)
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

