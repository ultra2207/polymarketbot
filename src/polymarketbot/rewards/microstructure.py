"""Order Book Microstructure Sentinel for Polymarket Liquidity Rewards.

Tracks real-time order book microstructure signals:
1. Cont-Kukanov-Stoikov Order Flow Imbalance (OFI) across top-of-book updates.
2. Stoikov Micro-Price and Micro-Price Drift relative to midpoint.
3. Order Book Depth Imbalance Ratio.
4. Microstructure Toxicity Gate to guard resting maker quotes against adverse selection.
5. Action evaluation helpers to pull or widen resting quotes on bid and ask sides.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional, Sequence


@dataclass(frozen=True)
class MicrostructureState:
    """Snapshot of current order book microstructure state."""

    mid_price: float
    micro_price: float
    ofi_10_events: float
    imbalance_ratio: float
    is_toxic: bool
    drift_cents: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    toxicity_reason: str = ""  # "OFI", "DRIFT", "BOTH", or ""
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
        ofi_threshold: float = 850.0,
        drift_threshold_cents: float = 0.45,
        window_size: int = 10,
        pull_multiplier: float = 1.5,
        widen_offset_multiplier: float = 1.5,
    ) -> None:
        self.ofi_threshold = ofi_threshold
        self.drift_threshold_cents = drift_threshold_cents
        self.window_size = window_size
        self.pull_multiplier = pull_multiplier
        self.widen_offset_multiplier = widen_offset_multiplier

        self._ofi_window: deque[float] = deque(maxlen=window_size)
        self._prev_bid_price: Optional[float] = None
        self._prev_bid_size: Optional[float] = None
        self._prev_ask_price: Optional[float] = None
        self._prev_ask_size: Optional[float] = None
        self._latest_state: Optional[MicrostructureState] = None

    @property
    def latest_state(self) -> Optional[MicrostructureState]:
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
        timestamp: Optional[float] = None,
    ) -> MicrostructureState:
        """Process a top-of-book update and compute the new MicrostructureState.

        Args:
            best_bid_price: Current highest bid price.
            best_bid_size: Quantity available at best bid.
            best_ask_price: Current lowest ask price.
            best_ask_size: Quantity available at best ask.
            timestamp: Event timestamp (default time.time()).

        Returns:
            Updated MicrostructureState instance.
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

        # 3. Toxicity Gate
        ofi_triggered = abs(ofi_10_events) > self.ofi_threshold
        drift_triggered = drift_cents > self.drift_threshold_cents
        is_toxic = ofi_triggered or drift_triggered

        if ofi_triggered and drift_triggered:
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
        state: Optional[MicrostructureState] = None,
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

        extreme_pull_threshold_ofi = self.ofi_threshold * self.pull_multiplier
        extreme_pull_threshold_drift = self.drift_threshold_cents * self.pull_multiplier

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
                    offset = max(
                        current_state.drift_cents * self.widen_offset_multiplier,
                        0.50,
                    )
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
                    offset = max(
                        current_state.drift_cents * self.widen_offset_multiplier,
                        0.50,
                    )
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
        state: Optional[MicrostructureState] = None,
    ) -> bool:
        """Convenience boolean helper to check if resting quote should be pulled."""
        return self.evaluate_quote_action(side, state=state).should_pull

    def should_widen_quote(
        self,
        side: str,
        state: Optional[MicrostructureState] = None,
    ) -> bool:
        """Convenience boolean helper to check if resting quote should be widened."""
        return self.evaluate_quote_action(side, state=state).should_widen


class MultiAssetMicrostructureSentinel:
    """Manages independent MicrostructureEngine instances across multiple assets."""

    def __init__(
        self,
        ofi_threshold: float = 850.0,
        drift_threshold_cents: float = 0.45,
        window_size: int = 10,
    ) -> None:
        self.ofi_threshold = ofi_threshold
        self.drift_threshold_cents = drift_threshold_cents
        self.window_size = window_size
        self._engines: Dict[str, MicrostructureEngine] = {}

    def get_engine(self, asset_id: str) -> MicrostructureEngine:
        """Retrieve or instantiate engine for a given asset."""
        if asset_id not in self._engines:
            self._engines[asset_id] = MicrostructureEngine(
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
        timestamp: Optional[float] = None,
    ) -> MicrostructureState:
        """Update top-of-book and return state for specified asset."""
        return self.get_engine(asset_id).update(
            best_bid_price=best_bid_price,
            best_bid_size=best_bid_size,
            best_ask_price=best_ask_price,
            best_ask_size=best_ask_size,
            timestamp=timestamp,
        )

    def evaluate_quote_action(
        self,
        asset_id: str,
        side: str,
    ) -> QuoteAction:
        """Evaluate quote action for given asset and side."""
        return self.get_engine(asset_id).evaluate_quote_action(side)
