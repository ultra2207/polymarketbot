"""Lead-Lag Sentinel (Cross-Venue Microstructure Alpha).

Monitors microstructural leads and lags between Kalshi and Polymarket:
1. Kalshi Macro Sweeps (CPI, FOMC, Jobs):
   - Kalshi leads in CFTC-regulated macro binary events.
   - When high OFI or price jump > 1.0c occurs on Kalshi, emits an instantaneous
     FADE_QUOTE or PULL_QUOTE signal targeting the Polymarket engine.
2. Polymarket Crypto / Geopolitics Sweeps:
   - Polymarket leads in crypto (BTC, ETH) and geopolitical betting markets.
   - When sweeps occur on Polymarket, emits FADE_QUOTE or PULL_QUOTE signal
     targeting the Kalshi engine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from polymarketbot.cross_venue.bus import TOPIC_LEAD_LAG_SIGNALS, CrossVenueBus
from polymarketbot.cross_venue.models import (
    LeadLagSignal,
    MarketCategory,
    SignalAction,
    SignalUrgency,
    Venue,
)
from polymarketbot.logs import get_logger

logger = get_logger("lead_lag_sentinel")

# Macro identifiers that trigger Kalshi -> Polymarket lead signals
MACRO_KEYWORDS = {
    "CPI", "FOMC", "FED", "JOBS", "NFP", "UNEMPLOYMENT", "INFLATION",
    "RATE", "INTEREST", "GDP", "POWELL", "PAYROLL"
}

# Crypto & Geopolitics identifiers that trigger Polymarket -> Kalshi lead signals
CRYPTO_KEYWORDS = {"BTC", "ETH", "SOL", "BITCOIN", "ETHEREUM", "CRYPTO"}
GEOPOLITICS_KEYWORDS = {
    "ELECTION", "PRESIDENT", "CONGRESS", "SENATE", "TARIFF", "WAR",
    "TAIWAN", "CEASEFIRE", "TRUMP", "HARRIS"
}

DEFAULT_PRICE_JUMP_THRESHOLD = 0.010  # 1.0c price jump
DEFAULT_PRICE_PULL_THRESHOLD = 0.025  # 2.5c aggressive sweep (pull quote)
DEFAULT_OFI_FADE_THRESHOLD = 50.0     # Order Flow Imbalance threshold
DEFAULT_OFI_PULL_THRESHOLD = 150.0    # Massive sweep OFI threshold


@dataclass
class MarketQuoteState:
    """Historical quote and OFI state for a symbol."""

    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    last_trade_price: float = 0.0
    cumulative_ofi: float = 0.0
    last_update_ns: int = 0


@dataclass
class LinkedPair:
    """Registered cross-venue pair linking Polymarket and Kalshi."""

    poly_token_id: str
    kalshi_ticker: str
    category: MarketCategory
    poly_name: str = ""
    kalshi_name: str = ""


class LeadLagSentinel:
    """Microsecond Lead-Lag Sentinel engine."""

    def __init__(
        self,
        bus: CrossVenueBus,
        price_jump_threshold: float = DEFAULT_PRICE_JUMP_THRESHOLD,
        price_pull_threshold: float = DEFAULT_PRICE_PULL_THRESHOLD,
        ofi_fade_threshold: float = DEFAULT_OFI_FADE_THRESHOLD,
        ofi_pull_threshold: float = DEFAULT_OFI_PULL_THRESHOLD,
    ) -> None:
        self.bus = bus
        self.price_jump_threshold = price_jump_threshold
        self.price_pull_threshold = price_pull_threshold
        self.ofi_fade_threshold = ofi_fade_threshold
        self.ofi_pull_threshold = ofi_pull_threshold

        self._pairs_by_poly: Dict[str, LinkedPair] = {}
        self._pairs_by_kalshi: Dict[str, LinkedPair] = {}
        self._states: Dict[str, MarketQuoteState] = {}
        self._emitted_signals: List[LeadLagSignal] = []

    def register_pair(
        self,
        poly_token_id: str,
        kalshi_ticker: str,
        category: Optional[MarketCategory] = None,
        poly_name: str = "",
        kalshi_name: str = "",
    ) -> LinkedPair:
        """Register a linked synthetic pair for lead-lag surveillance."""
        cat = category or self.detect_category(f"{kalshi_ticker} {poly_name}")
        pair = LinkedPair(
            poly_token_id=poly_token_id,
            kalshi_ticker=kalshi_ticker,
            category=cat,
            poly_name=poly_name,
            kalshi_name=kalshi_name,
        )
        self._pairs_by_poly[poly_token_id] = pair
        self._pairs_by_kalshi[kalshi_ticker] = pair
        logger.info(f"Registered cross-venue pair: {poly_token_id} <-> {kalshi_ticker} (category={cat.value})")
        return pair

    @staticmethod
    def detect_category(identifier: str) -> MarketCategory:
        """Heuristically classify category based on ticker / market names."""
        upper = identifier.upper()
        if any(k in upper for k in MACRO_KEYWORDS):
            return MarketCategory.MACRO
        if any(k in upper for k in CRYPTO_KEYWORDS):
            return MarketCategory.CRYPTO
        if any(k in upper for k in GEOPOLITICS_KEYWORDS):
            return MarketCategory.GEOPOLITICS
        return MarketCategory.OTHER

    def _compute_ofi(
        self,
        prev: MarketQuoteState,
        new_bid: float,
        new_ask: float,
        new_bid_size: float,
        new_ask_size: float,
    ) -> float:
        """Compute Order Flow Imbalance (OFI) from top-of-book delta."""
        # Bid side OFI component
        if new_bid > prev.bid:
            delta_bid = new_bid_size
        elif new_bid == prev.bid:
            delta_bid = new_bid_size - prev.bid_size
        else:
            delta_bid = -prev.bid_size

        # Ask side OFI component
        if new_ask < prev.ask:
            delta_ask = new_ask_size
        elif new_ask == prev.ask:
            delta_ask = new_ask_size - prev.ask_size
        else:
            delta_ask = -prev.ask_size

        return delta_bid - delta_ask

    def on_kalshi_quote(
        self,
        ticker: str,
        yes_bid: float,
        yes_ask: float,
        yes_bid_size: float = 0.0,
        yes_ask_size: float = 0.0,
        timestamp_ns: Optional[int] = None,
    ) -> Optional[LeadLagSignal]:
        """Process Kalshi BBO quote update.

        For Macro markets (CPI, FOMC, Jobs):
        When price jump > 1.0c or high OFI occurs on Kalshi, emits an instantaneous
        FADE_QUOTE or PULL_QUOTE signal targeting Polymarket engine.
        """
        now_ns = timestamp_ns or time.perf_counter_ns()
        pair = self._pairs_by_kalshi.get(ticker)
        if not pair:
            return None

        # Only Kalshi -> Polymarket lead for Macro (or Other configured) markets
        if pair.category not in (MarketCategory.MACRO, MarketCategory.OTHER):
            return None

        key = f"kalshi:{ticker}"
        prev = self._states.get(key)
        mid = (yes_bid + yes_ask) / 2.0

        if prev is None:
            self._states[key] = MarketQuoteState(
                bid=yes_bid,
                ask=yes_ask,
                mid=mid,
                bid_size=yes_bid_size,
                ask_size=yes_ask_size,
                last_update_ns=now_ns,
            )
            return None

        # Calculate price jump and OFI
        price_jump = mid - prev.mid
        ofi = self._compute_ofi(prev, yes_bid, yes_ask, yes_bid_size, yes_ask_size)

        # Update state
        prev.bid = yes_bid
        prev.ask = yes_ask
        prev.mid = mid
        prev.bid_size = yes_bid_size
        prev.ask_size = yes_ask_size
        prev.cumulative_ofi += ofi
        prev.last_update_ns = now_ns

        # Evaluate trigger
        abs_jump = abs(price_jump)
        abs_ofi = abs(ofi)

        signal: Optional[LeadLagSignal] = None
        if abs_jump >= self.price_jump_threshold or abs_ofi >= self.ofi_fade_threshold:
            # Check if urgency justifies PULL_QUOTE vs FADE_QUOTE
            if abs_jump >= self.price_pull_threshold or abs_ofi >= self.ofi_pull_threshold:
                action = SignalAction.PULL_QUOTE
                urgency = SignalUrgency.CRITICAL
                reason = f"Kalshi macro sweep: jump={price_jump*100:.1f}c, OFI={ofi:.0f} (pull resting quotes)"
            else:
                action = SignalAction.FADE_QUOTE
                urgency = SignalUrgency.HIGH
                reason = f"Kalshi macro shift: jump={price_jump*100:.1f}c, OFI={ofi:.0f} (fade quote)"

            signal = LeadLagSignal(
                source_venue=Venue.KALSHI,
                target_venue=Venue.POLYMARKET,
                symbol_source=ticker,
                symbol_target=pair.poly_token_id,
                action=action,
                price_jump=price_jump,
                ofi=ofi,
                source_price=mid,
                category=pair.category,
                urgency=urgency,
                reason=reason,
                timestamp_ns=now_ns,
            )

            self._emitted_signals.append(signal)
            self.bus.dispatch(TOPIC_LEAD_LAG_SIGNALS, signal)
            logger.warning(f"SENTINEL DISPATCH [Kalshi -> Poly]: {signal.action.value} on {pair.poly_token_id} reason='{reason}'")

        return signal

    def on_polymarket_quote(
        self,
        token_id: str,
        bid: float,
        ask: float,
        bid_size: float = 0.0,
        ask_size: float = 0.0,
        timestamp_ns: Optional[int] = None,
    ) -> Optional[LeadLagSignal]:
        """Process Polymarket orderbook update.

        For Crypto and Geopolitics markets:
        When price jump > 1.0c or high OFI occurs on Polymarket, emits an instantaneous
        FADE_QUOTE or PULL_QUOTE signal targeting Kalshi engine.
        """
        now_ns = timestamp_ns or time.perf_counter_ns()
        pair = self._pairs_by_poly.get(token_id)
        if not pair:
            return None

        # Polymarket leads on Crypto and Geopolitics
        if pair.category not in (MarketCategory.CRYPTO, MarketCategory.GEOPOLITICS):
            return None

        key = f"poly:{token_id}"
        prev = self._states.get(key)
        mid = (bid + ask) / 2.0

        if prev is None:
            self._states[key] = MarketQuoteState(
                bid=bid,
                ask=ask,
                mid=mid,
                bid_size=bid_size,
                ask_size=ask_size,
                last_update_ns=now_ns,
            )
            return None

        price_jump = mid - prev.mid
        ofi = self._compute_ofi(prev, bid, ask, bid_size, ask_size)

        prev.bid = bid
        prev.ask = ask
        prev.mid = mid
        prev.bid_size = bid_size
        prev.ask_size = ask_size
        prev.cumulative_ofi += ofi
        prev.last_update_ns = now_ns

        abs_jump = abs(price_jump)
        abs_ofi = abs(ofi)

        signal: Optional[LeadLagSignal] = None
        if abs_jump >= self.price_jump_threshold or abs_ofi >= self.ofi_fade_threshold:
            if abs_jump >= self.price_pull_threshold or abs_ofi >= self.ofi_pull_threshold:
                action = SignalAction.PULL_QUOTE
                urgency = SignalUrgency.CRITICAL
                reason = f"Polymarket {pair.category.value} sweep: jump={price_jump*100:.1f}c, OFI={ofi:.0f} (pull resting quotes)"
            else:
                action = SignalAction.FADE_QUOTE
                urgency = SignalUrgency.HIGH
                reason = f"Polymarket {pair.category.value} shift: jump={price_jump*100:.1f}c, OFI={ofi:.0f} (fade quotes)"

            signal = LeadLagSignal(
                source_venue=Venue.POLYMARKET,
                target_venue=Venue.KALSHI,
                symbol_source=token_id,
                symbol_target=pair.kalshi_ticker,
                action=action,
                price_jump=price_jump,
                ofi=ofi,
                source_price=mid,
                category=pair.category,
                urgency=urgency,
                reason=reason,
                timestamp_ns=now_ns,
            )

            self._emitted_signals.append(signal)
            self.bus.dispatch(TOPIC_LEAD_LAG_SIGNALS, signal)
            logger.warning(f"SENTINEL DISPATCH [Poly -> Kalshi]: {signal.action.value} on {pair.kalshi_ticker} reason='{reason}'")

        return signal

    def get_emitted_signals(self) -> List[LeadLagSignal]:
        """Return audit trail of emitted signals."""
        return list(self._emitted_signals)
