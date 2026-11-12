"""Order Book Microstructure Sentinel for Polymarket Liquidity Rewards.

Tracks real-time order book microstructure signals:
1. Cont-Kukanov-Stoikov Order Flow Imbalance (OFI) across top-of-book updates.
2. Stoikov Micro-Price and Micro-Price Drift relative to midpoint.
3. Order Book Depth Imbalance Ratio.
4. Microstructure Toxicity Gate to guard resting maker quotes against adverse selection.
5. Action evaluation helpers to pull or widen resting quotes on bid and ask sides.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass


def compute_dynamic_ofi_threshold(
    bid_size: float,
    ask_size: float,
    *,
    c_depth: float,
    z_score: float,
    v_ref: float,
    beta_vol: float,
    rolling_ofi_std: float = 0.0,
    volume_24h: float | None = None,
    min_threshold: float = 25.0,
    max_threshold: float = 25_000.0,
) -> float:
    """Compute dynamic Cont-Kukanov-Stoikov Order Flow Imbalance (OFI) threshold.

    ``c_depth``, ``z_score``, ``v_ref`` and ``beta_vol`` are the fitted
    coefficients of the OFI model. They have no defaults: the caller must pass
    values measured from the tape (or loaded from a fit artifact). A fabricated
    coefficient would silently become the trading decision.

    ``volume_24h`` is the measured 24-hour volume. When it is absent the volume
    scaling is skipped entirely rather than substituting a reference multiplier;
    ``v_ref``/``beta_vol`` are still required because the caller's fit is part
    of the model, not an optional refinement.
    """
    qb = max(0.0, float(bid_size))
    qa = max(0.0, float(ask_size))
    sum_q = qb + qa

    if sum_q <= 1e-6:
        d_harm = min_threshold
    else:
        d_harm = (2.0 * qb * qa) / sum_q

    structural_capacity = c_depth * d_harm
    statistical_capacity = z_score * max(0.0, float(rolling_ofi_std))
    dynamic_thresh = max(structural_capacity, statistical_capacity)

    if volume_24h is not None and volume_24h > 0.0:
        raw_psi = (float(volume_24h) / v_ref) ** beta_vol
        psi = max(0.50, min(2.50, raw_psi))
        dynamic_thresh *= psi

    return max(min_threshold, min(max_threshold, dynamic_thresh))


def compute_dynamic_drift_threshold_cents(
    mid_price: float,
    spread_cents: float,
    *,
    volatility: float,
    critical_imbalance: float = 0.80,
    volatility_ref: float = 0.02,
    min_drift_cents: float = 0.10,
    max_drift_cents: float | None = None,
) -> float:
    """Compute dynamic Stoikov Micro-Price Drift Threshold in cents.

    ``volatility`` is the engine's realised volatility, measured from the tape.
    It has no default: an invented constant would silently pin ``vol_scale`` to
    1.0 (its reference) and turn the volatility term into a no-op.
    """
    p = max(0.005, min(0.995, float(mid_price)))
    s_cents = max(0.01, float(spread_cents))
    half_spread = s_cents / 2.0

    crit_imb = max(0.50, min(0.95, float(critical_imbalance)))
    base_drift = crit_imb * half_spread

    # Wright-Fisher binary variance curvature: sqrt(4 * p * (1 - p))
    state_curvature = math.sqrt(4.0 * p * (1.0 - p))

    vol_ratio = (max(0.001, float(volatility)) / max(0.001, float(volatility_ref))) ** 0.25
    vol_scale = max(0.70, min(1.50, vol_ratio))

    thresh = base_drift * state_curvature * vol_scale

    upper_limit = 0.95 * half_spread
    if max_drift_cents is not None:
        upper_limit = min(upper_limit, float(max_drift_cents))

    floor_val = min(min_drift_cents, upper_limit * 0.90)
    return max(floor_val, min(upper_limit, thresh))


def compute_dynamic_volatility_breaker_threshold(
    mid_price: float,
    spread_cents: float,
    volatility_annual: float = 0.60,
    window_seconds: float = 60.0,
    k_jump: float = 3.29,
    min_jump_cents: float = 0.50,
    max_jump_cents: float = 15.0,
) -> float:
    """Compute dynamic Price Jump Volatility Breaker threshold Delta P_breaker^* in cents."""
    p = max(0.005, min(0.995, float(mid_price)))
    s_cents = max(0.05, float(spread_cents))
    w = max(5.0, float(window_seconds))

    sigma_sec = float(volatility_annual) / math.sqrt(365.25 * 86400.0)
    diffusive_range_cents = (float(k_jump) * sigma_sec * math.sqrt(w)) * 100.0
    curvature = math.sqrt(4.0 * p * (1.0 - p))

    dynamic_cents = s_cents + (diffusive_range_cents * curvature)
    scaled_min = max(min_jump_cents, s_cents * 1.2, 2.0 * curvature)
    return max(scaled_min, min(max_jump_cents, dynamic_cents))


def compute_dynamic_cooldown_duration(
    jump_cents_observed: float,
    breaker_threshold_cents: float,
    time_to_resolution_seconds: float = 86_400.0 * 7.0,
    base_cooldown_seconds: float = 120.0,
    severity_exponent: float = 1.25,
    horizon_exponent: float = 0.20,
    max_horizon_fraction: float = 0.05,
    min_cooldown_seconds: float = 15.0,
    max_cooldown_seconds: float = 900.0,
) -> float:
    """Compute dynamic Volatility Cooldown Duration tau_cooldown^* in seconds."""
    jump_obs = max(0.0, float(jump_cents_observed))
    thresh = max(0.01, float(breaker_threshold_cents))
    tau_rem = max(10.0, float(time_to_resolution_seconds))

    severity = max(1.0, jump_obs / thresh)
    phi_severity = severity ** severity_exponent

    tau_ref = 86_400.0
    omega_horizon = max(0.25, min(2.0, (tau_rem / tau_ref) ** horizon_exponent))

    target_cooldown = base_cooldown_seconds * phi_severity * omega_horizon

    lifetime_cap = max(min_cooldown_seconds, tau_rem * max_horizon_fraction)
    effective_max = min(max_cooldown_seconds, lifetime_cap)
    effective_min = min(min_cooldown_seconds, effective_max * 0.50)

    return max(effective_min, min(effective_max, target_cooldown))


@dataclass(frozen=True)
class MicrostructureState:
    """Snapshot of current order book microstructure state.

    ``ofi_threshold`` / ``drift_threshold_cents`` are the thresholds this
    snapshot was actually judged against, and ``measured`` records whether they
    came from a measurement (book + realised volatility, or an explicit
    override) or were unavailable. An unmeasured snapshot carries ``nan``
    thresholds: the engine never substitutes a provisional number, so callers
    can tell "flow is calm" from "flow was not judged".
    """

    mid_price: float
    micro_price: float
    ofi_10_events: float
    imbalance_ratio: float
    is_toxic: bool
    ofi_threshold: float
    drift_threshold_cents: float
    measured: bool
    drift_cents: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    toxicity_reason: str = ""  # "OFI", "DRIFT", "BOTH", "unmeasured", or ""
    timestamp: float = 0.0

    @property
    def bid_ratio(self) -> float:
        """Normalized bid size share in [0.0, 1.0]."""
        total = self.bid_size + self.ask_size
        return self.bid_size / total if total > 0 else 0.5


@dataclass(frozen=True)
class QuoteAction:
    """Evaluation action for a resting quote on a given book side."""

    side: str  # "BUY" / "BID" or "SELL" / "ASK"
    action: str  # "MAINTAIN", "WIDEN", "PULL"
    should_pull: bool
    should_widen: bool
    spread_offset_cents: float
    reason: str


class MicrostructureEngine:
    """Tracks Cont-Kukanov-Stoikov OFI and Stoikov Micro-Price dynamics.

    Implements:
    - Cont-Kukanov-Stoikov (2014) Order Flow Imbalance across top-of-book events.
    - Rolling 10-event window aggregation of OFI.
    - Stoikov (2018) Micro-Price calculation: (Q_b * P_a + Q_a * P_b) / (Q_b + Q_a).
    - Drift |P_micro - P_mid| in cents.
    - Toxicity detection gate (|rolling_ofi| > threshold or drift > threshold).
    - Adverse-selection quote protection evaluation (pull/widen bids vs asks).
    """

    def __init__(
        self,
        *,
        c_depth: float,
        z_score: float,
        v_ref: float,
        beta_vol: float,
        pull_multiplier: float,
        widen_offset_multiplier: float,
        ofi_threshold: float | None = None,
        drift_threshold_cents: float | None = None,
        window_size: int = 10,
        dynamic_thresholds: bool = True,
    ) -> None:
        """Create one flow engine.

        All six keyword arguments are required and must be fitted/measured by
        the caller. ``c_depth``/``z_score``/``v_ref``/``beta_vol`` are the OFI
        fit coefficients; ``pull_multiplier``/``widen_offset_multiplier`` are
        the measured multipliers applied to a threshold/drift once the gate is
        toxic. ``widen_offset_multiplier == 0`` is the measured "do not widen"
        policy: a toxic but non-extreme event MAINTAINS the quote. There are
        deliberately no defaults: an invented number would silently become the
        pull/widen decision.
        """
        if c_depth <= 0.0 or z_score <= 0.0 or v_ref <= 0.0 or beta_vol <= 0.0:
            raise ValueError("OFI fit coefficients must be positive")
        if pull_multiplier <= 0.0:
            raise ValueError("pull multiplier must be positive")
        if widen_offset_multiplier < 0.0:
            # Zero is a measured policy, not a missing value: the fit found
            # that widening the quote loses capture, so non-extreme toxicity
            # MAINTAINS. Negative is not a spread.
            raise ValueError("widen offset multiplier must be non-negative")
        self.c_depth = float(c_depth)
        self.z_score = float(z_score)
        self.v_ref = float(v_ref)
        self.beta_vol = float(beta_vol)
        self.ofi_threshold = ofi_threshold
        self.drift_threshold_cents = drift_threshold_cents
        self.window_size = window_size
        self.pull_multiplier = float(pull_multiplier)
        self.widen_offset_multiplier = float(widen_offset_multiplier)
        self.dynamic_thresholds = dynamic_thresholds

        self._ofi_window: deque[float] = deque(maxlen=window_size)
        self._prev_bid_price: float | None = None
        self._prev_bid_size: float | None = None
        self._prev_ask_price: float | None = None
        self._prev_ask_size: float | None = None
        self._latest_state: MicrostructureState | None = None

    @property
    def latest_state(self) -> MicrostructureState | None:
        """Return the most recent microstructure state computed."""
        return self._latest_state

    def reset(self) -> None:
        """Reset all historical window tracking and previous book states."""
        self._ofi_window.clear()
        self._prev_bid_price = None
        self._prev_bid_size = None
        self._prev_ask_price = None
        self._prev_ask_size = None
        self._latest_state = None

    def update(
        self,
        best_bid_price: float,
        best_bid_size: float,
        best_ask_price: float,
        best_ask_size: float,
        timestamp: float | None = None,
        realized_volatility: float | None = None,
    ) -> MicrostructureState:
        """Process a top-of-book update and compute the new MicrostructureState.

        Args:
            best_bid_price: Current highest bid price.
            best_bid_size: Quantity available at best bid.
            best_ask_price: Current lowest ask price.
            best_ask_size: Quantity available at best ask.
            timestamp: Event timestamp (default time.time()).
            realized_volatility: Realised volatility measured from the tape.
                Required whenever a threshold is not supplied explicitly: the
                dynamic thresholds are fitted from the book and this
                measurement, never from a constant.

        Returns:
            Updated MicrostructureState instance. ``measured`` is ``False``
            (with ``nan`` thresholds and ``is_toxic`` ``False``) when the
            thresholds could not be measured.
        """
        now = timestamp if timestamp is not None else time.time()

        # Sanitize zero or negative inputs
        bid_price = max(0.0, float(best_bid_price))
        bid_size = max(0.0, float(best_bid_size))
        ask_price = max(0.0, float(best_ask_price))
        ask_size = max(0.0, float(best_ask_size))

        # 1. Cont-Kukanov-Stoikov Order Flow Imbalance (OFI) event
        if (
            self._prev_bid_price is not None
            and self._prev_bid_size is not None
            and self._prev_ask_price is not None
            and self._prev_ask_size is not None
        ):
            # Bid delta
            if bid_price > self._prev_bid_price:
                delta_b = bid_size
            elif bid_price == self._prev_bid_price:
                delta_b = bid_size - self._prev_bid_size
            else:
                delta_b = -self._prev_bid_size

            # Ask delta
            if ask_price < self._prev_ask_price:
                delta_a = ask_size
            elif ask_price == self._prev_ask_price:
                delta_a = ask_size - self._prev_ask_size
            else:
                delta_a = -self._prev_ask_size

            ofi_event = delta_b - delta_a
        else:
            ofi_event = 0.0

        self._ofi_window.append(ofi_event)
        ofi_10_events = sum(self._ofi_window)

        # Update previous top-of-book values
        self._prev_bid_price = bid_price
        self._prev_bid_size = bid_size
        self._prev_ask_price = ask_price
        self._prev_ask_size = ask_size

        # 2. Stoikov Micro-Price & Mid-Price
        mid_price = (bid_price + ask_price) / 2.0
        total_depth = bid_size + ask_size

        if total_depth > 0:
            micro_price = (bid_size * ask_price + ask_size * bid_price) / total_depth
            imbalance_ratio = (bid_size - ask_size) / total_depth
        else:
            micro_price = mid_price
            imbalance_ratio = 0.0

        # Drift in cents: Polymarket prices are in range [0, 1] dollars.
        # If mid <= 1.0, 1.00 dollar = 100.0 cents.
        scale = 100.0 if mid_price <= 1.0 else 1.0
        drift_cents = abs(micro_price - mid_price) * scale

        # 3. Measured toxicity gate.
        # Explicit constructor thresholds are measurements (the live path fits
        # them from the tape), so they win verbatim. Otherwise thresholds are
        # computed from this book and the realised volatility. With neither
        # available the gate is unmeasured: no provisional number.
        spread_cents = abs(ask_price - bid_price) * 100.0

        if self.ofi_threshold is not None and self.drift_threshold_cents is not None:
            eff_ofi = float(self.ofi_threshold)
            eff_drift = float(self.drift_threshold_cents)
            measured = True
        elif self.dynamic_thresholds and realized_volatility is not None:
            eff_ofi = (
                float(self.ofi_threshold)
                if self.ofi_threshold is not None
                else compute_dynamic_ofi_threshold(
                    bid_size,
                    ask_size,
                    # The observed dispersion of the engine's own OFI window.
                    # Without it the statistical term is zero and the threshold
                    # collapses to a fixed depth multiple.
                    rolling_ofi_std=(
                        statistics.pstdev(self._ofi_window)
                        if len(self._ofi_window) >= 2
                        else 0.0
                    ),
                    c_depth=self.c_depth,
                    z_score=self.z_score,
                    v_ref=self.v_ref,
                    beta_vol=self.beta_vol,
                )
            )
            eff_drift = (
                float(self.drift_threshold_cents)
                if self.drift_threshold_cents is not None
                else compute_dynamic_drift_threshold_cents(
                    mid_price,
                    spread_cents,
                    volatility=realized_volatility,
                )
            )
            measured = True
        else:
            # No explicit threshold and no measured volatility: the gate is
            # unmeasured. Substituting a provisional number would be an
            # invented constant that silently becomes the decision.
            eff_ofi = float("nan")
            eff_drift = float("nan")
            measured = False

        if measured:
            ofi_triggered = abs(ofi_10_events) > eff_ofi
            drift_triggered = drift_cents > eff_drift
        else:
            ofi_triggered = False
            drift_triggered = False
        is_toxic = ofi_triggered or drift_triggered

        if not measured:
            toxicity_reason = "unmeasured"
        elif ofi_triggered and drift_triggered:
            toxicity_reason = "BOTH"
        elif ofi_triggered:
            toxicity_reason = "OFI"
        elif drift_triggered:
            toxicity_reason = "DRIFT"
        else:
            toxicity_reason = ""

        state = MicrostructureState(
            mid_price=mid_price,
            micro_price=micro_price,
            ofi_10_events=ofi_10_events,
            imbalance_ratio=imbalance_ratio,
            is_toxic=is_toxic,
            ofi_threshold=eff_ofi,
            drift_threshold_cents=eff_drift,
            measured=measured,
            drift_cents=drift_cents,
            best_bid=bid_price,
            best_ask=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            toxicity_reason=toxicity_reason,
            timestamp=now,
        )
        self._latest_state = state
        return state

    def evaluate_quote_action(
        self,
        side: str,
        state: MicrostructureState | None = None,
    ) -> QuoteAction:
        """Evaluate whether resting quotes on bid or ask should be pulled or widened.

        Args:
            side: Order side ("BUY"/"BID" or "SELL"/"ASK").
            state: Optional MicrostructureState to evaluate (defaults to latest).

        Returns:
            QuoteAction detailing whether to MAINTAIN, WIDEN, or PULL the quote.
        """
        current_state = state or self._latest_state
        norm_side = side.upper()
        is_bid = norm_side in ("BUY", "BID")
        is_ask = norm_side in ("SELL", "ASK")

        if not is_bid and not is_ask:
            raise ValueError(f"Unknown side '{side}'; must be BUY/BID or SELL/ASK.")

        if current_state is not None and not current_state.measured:
            return QuoteAction(
                side=norm_side,
                action="MAINTAIN",
                should_pull=False,
                should_widen=False,
                spread_offset_cents=0.0,
                reason=(
                    "Microstructure thresholds unmeasured: no explicit threshold and no "
                    "realised volatility; holding the resting quote rather than substituting a constant."
                ),
            )

        if current_state is None or not current_state.is_toxic:
            return QuoteAction(
                side=norm_side,
                action="MAINTAIN",
                should_pull=False,
                should_widen=False,
                spread_offset_cents=0.0,
                reason="Order book flow is balanced; non-toxic microstructure.",
            )

        # Direction of adverse toxicity
        # Positive OFI or micro_price > mid_price indicates upward buying pressure.
        # Negative OFI or micro_price < mid_price indicates downward selling pressure.
        directional_signal = (
            current_state.ofi_10_events
            if current_state.ofi_10_events != 0.0
            else (current_state.micro_price - current_state.mid_price)
        )

        # Judge extremity against the thresholds this snapshot was measured with.
        extreme_pull_threshold_ofi = current_state.ofi_threshold * self.pull_multiplier
        extreme_pull_threshold_drift = (
            current_state.drift_threshold_cents * self.pull_multiplier
        )

        is_extreme = (
            abs(current_state.ofi_10_events) >= extreme_pull_threshold_ofi
            or current_state.drift_cents >= extreme_pull_threshold_drift
        )

        if directional_signal > 0:
            # Toxic buying flow (price surging up) -> ASKs are at risk of adverse selection
            if is_ask:
                if is_extreme:
                    return QuoteAction(
                        side=norm_side,
                        action="PULL",
                        should_pull=True,
                        should_widen=True,
                        spread_offset_cents=current_state.drift_cents * 2.0,
                        reason=f"Extreme toxic buying pressure ({current_state.toxicity_reason}); pull resting asks to prevent adverse selection.",
                    )
                else:
                    if self.widen_offset_multiplier <= 0.0:
                        return QuoteAction(
                            side=norm_side,
                            action="MAINTAIN",
                            should_pull=False,
                            should_widen=False,
                            spread_offset_cents=0.0,
                            reason=(
                                "Measured widen offset is zero: maintain the "
                                "resting ask on non-extreme toxicity rather than "
                                "inventing a spread offset."
                            ),
                        )
                    # The multiplier is measured; no floor may override it.
                    offset = current_state.drift_cents * self.widen_offset_multiplier
                    return QuoteAction(
                        side=norm_side,
                        action="WIDEN",
                        should_pull=False,
                        should_widen=True,
                        spread_offset_cents=offset,
                        reason=f"Toxic buying flow detected ({current_state.toxicity_reason}); widen resting asks by {offset:.2f}c.",
                    )
            else:
                # Bids are safe from adverse selection as market moves away upwards
                return QuoteAction(
                    side=norm_side,
                    action="MAINTAIN",
                    should_pull=False,
                    should_widen=False,
                    spread_offset_cents=0.0,
                    reason="Toxic flow is upward; resting bids are safe from adverse selection.",
                )
        else:
            # Toxic selling flow (price plunging down) -> BIDs are at risk of adverse selection
            if is_bid:
                if is_extreme:
                    return QuoteAction(
                        side=norm_side,
                        action="PULL",
                        should_pull=True,
                        should_widen=True,
                        spread_offset_cents=current_state.drift_cents * 2.0,
                        reason=f"Extreme toxic selling pressure ({current_state.toxicity_reason}); pull resting bids to prevent adverse selection.",
                    )
                else:
                    if self.widen_offset_multiplier <= 0.0:
                        return QuoteAction(
                            side=norm_side,
                            action="MAINTAIN",
                            should_pull=False,
                            should_widen=False,
                            spread_offset_cents=0.0,
                            reason=(
                                "Measured widen offset is zero: maintain the "
                                "resting bid on non-extreme toxicity rather than "
                                "inventing a spread offset."
                            ),
                        )
                    # The multiplier is measured; no floor may override it.
                    offset = current_state.drift_cents * self.widen_offset_multiplier
                    return QuoteAction(
                        side=norm_side,
                        action="WIDEN",
                        should_pull=False,
                        should_widen=True,
                        spread_offset_cents=offset,
                        reason=f"Toxic selling flow detected ({current_state.toxicity_reason}); widen resting bids by {offset:.2f}c.",
                    )
            else:
                # Asks are safe from adverse selection as market moves away downwards
                return QuoteAction(
                    side=norm_side,
                    action="MAINTAIN",
                    should_pull=False,
                    should_widen=False,
                    spread_offset_cents=0.0,
                    reason="Toxic flow is downward; resting asks are safe from adverse selection.",
                )

    def should_pull_quote(
        self,
        side: str,
        state: MicrostructureState | None = None,
    ) -> bool:
        """Convenience boolean helper to check if resting quote should be pulled."""
        return self.evaluate_quote_action(side, state=state).should_pull

    def should_widen_quote(
        self,
        side: str,
        state: MicrostructureState | None = None,
    ) -> bool:
        """Convenience boolean helper to check if resting quote should be widened."""
        return self.evaluate_quote_action(side, state=state).should_widen


class MultiAssetMicrostructureSentinel:
    """Manages independent MicrostructureEngine instances across multiple assets.

    Every engine shares the caller's fitted coefficients and multipliers.
    Thresholds are optional: when absent, each engine fits them from that
    asset's own book and the realised volatility supplied per update.
    """

    def __init__(
        self,
        *,
        c_depth: float,
        z_score: float,
        v_ref: float,
        beta_vol: float,
        pull_multiplier: float,
        widen_offset_multiplier: float,
        ofi_threshold: float | None = None,
        drift_threshold_cents: float | None = None,
        window_size: int = 10,
    ) -> None:
        self.c_depth = float(c_depth)
        self.z_score = float(z_score)
        self.v_ref = float(v_ref)
        self.beta_vol = float(beta_vol)
        self.pull_multiplier = float(pull_multiplier)
        self.widen_offset_multiplier = float(widen_offset_multiplier)
        self.ofi_threshold = ofi_threshold
        self.drift_threshold_cents = drift_threshold_cents
        self.window_size = window_size
        self._engines: dict[str, MicrostructureEngine] = {}

    def get_engine(self, asset_id: str) -> MicrostructureEngine:
        """Retrieve or instantiate engine for a given asset."""
        if asset_id not in self._engines:
            self._engines[asset_id] = MicrostructureEngine(
                c_depth=self.c_depth,
                z_score=self.z_score,
                v_ref=self.v_ref,
                beta_vol=self.beta_vol,
                pull_multiplier=self.pull_multiplier,
                widen_offset_multiplier=self.widen_offset_multiplier,
                ofi_threshold=self.ofi_threshold,
                drift_threshold_cents=self.drift_threshold_cents,
                window_size=self.window_size,
            )
        return self._engines[asset_id]

    def update(
        self,
        asset_id: str,
        best_bid_price: float,
        best_bid_size: float,
        best_ask_price: float,
        best_ask_size: float,
        timestamp: float | None = None,
        realized_volatility: float | None = None,
    ) -> MicrostructureState:
        """Update top-of-book and return state for specified asset."""
        return self.get_engine(asset_id).update(
            best_bid_price=best_bid_price,
            best_bid_size=best_bid_size,
            best_ask_price=best_ask_price,
            best_ask_size=best_ask_size,
            timestamp=timestamp,
            realized_volatility=realized_volatility,
        )

    def evaluate_quote_action(
        self,
        asset_id: str,
        side: str,
        state: MicrostructureState | None = None,
    ) -> QuoteAction:
        """Evaluate quote action for given asset and side."""
        return self.get_engine(asset_id).evaluate_quote_action(side, state=state)
