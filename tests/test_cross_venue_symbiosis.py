"""Tests for Polymarket & Kalshi Symbiosis.

Verifies:
1. Lead-lag signal dispatch and quote pull triggers (< 1ms IPC latency).
2. Synthetic complete set locking via maker quoting across venues (zero maker fees).
3. Rejection of taker crossing when spread dislocation <= 1.5c (preventing 7% quadratic taker fee bleed).
4. Kalshi REST v2 client post-only limit orders and Order Groups circuit breaker.
5. Kalshi dynamic opportunity scanner with microstructure filters and $1.00 incentive floor.
"""

from __future__ import annotations

import asyncio
import time
import pytest

from polymarketbot.cross_venue.bus import (
    TOPIC_ALL,
    TOPIC_HEDGE_REQUESTS,
    TOPIC_LEAD_LAG_SIGNALS,
    CrossVenueBus,
)
from polymarketbot.cross_venue.hedger import (
    MIN_TAKER_DISLOCATION_HURDLE,
    PassiveCrossVenueHedger,
)
from polymarketbot.cross_venue.models import (
    CrossVenueHedgeRequest,
    HedgeExecutionMode,
    HedgeStatus,
    LeadLagSignal,
    MarketCategory,
    SignalAction,
    SignalUrgency,
    SyntheticPairState,
    Venue,
)
from polymarketbot.cross_venue.sentinel import (
    LeadLagSentinel,
)
from polymarketbot.kalshi.client import (
    DEMO_BASE_URL,
    PROD_BASE_URL,
    KalshiClient,
    KalshiOrderError,
)
from polymarketbot.kalshi.scanner import (
    KalshiOpportunityScanner,
)


# =====================================================================
# 1. High-Speed IPC Bus & Sub-Millisecond Dispatch Tests
# =====================================================================

def test_bus_ring_buffer_and_sub_millisecond_dispatch():
    """Verify in-memory IPC event dispatch latency is strictly under 1.0 ms."""
    bus = CrossVenueBus(buffer_capacity=1024)
    q = bus.subscribe(TOPIC_LEAD_LAG_SIGNALS)

    latencies_ms = []
    num_events = 200

    for i in range(num_events):
        sig = LeadLagSignal(
            source_venue=Venue.KALSHI,
            target_venue=Venue.POLYMARKET,
            symbol_source="FED-RATE-MAR26",
            symbol_target="poly_token_fed",
            action=SignalAction.FADE_QUOTE,
            price_jump=0.015,
            ofi=75.0,
            source_price=0.55,
        )
        lat_ms = bus.dispatch(TOPIC_LEAD_LAG_SIGNALS, sig)
        latencies_ms.append(lat_ms)

    # Sub-millisecond verification (< 1.0 ms)
    max_lat = max(latencies_ms)
    avg_lat = sum(latencies_ms) / len(latencies_ms)
    assert max_lat < 1.0, f"Max IPC dispatch latency {max_lat:.4f}ms exceeded 1.0ms limit"
    assert avg_lat < 0.2, f"Average IPC dispatch latency {avg_lat:.4f}ms exceeded 0.2ms"

    # Queue received all events without loss
    assert q.qsize() == num_events

    # Ring buffer depth and query
    recent = bus.get_recent(limit=10, topic=TOPIC_LEAD_LAG_SIGNALS)
    assert len(recent) == 10

    metrics = bus.get_metrics()
    assert metrics["total_dispatches"] == num_events
    assert metrics["sub_millisecond_compliant"] is True


def test_bus_zero_delay_callback_handler():
    """Verify instantaneous callback execution for latency-critical signals."""
    bus = CrossVenueBus()
    received_signals = []

    def fast_handler(topic: str, event: Any):
        received_signals.append((topic, event))

    bus.register_handler(TOPIC_LEAD_LAG_SIGNALS, fast_handler)

    test_sig = LeadLagSignal(
        source_venue=Venue.KALSHI,
        target_venue=Venue.POLYMARKET,
        symbol_source="CPI-MAR26",
        symbol_target="poly_cpi",
        action=SignalAction.PULL_QUOTE,
        price_jump=0.03,
        ofi=200.0,
        source_price=0.62,
    )
    bus.dispatch(TOPIC_LEAD_LAG_SIGNALS, test_sig)

    assert len(received_signals) == 1
    assert received_signals[0][0] == TOPIC_LEAD_LAG_SIGNALS
    assert received_signals[0][1].action == SignalAction.PULL_QUOTE


# =====================================================================
# 2. Lead-Lag Sentinel Tests (Macro Kalshi & Crypto Polymarket Sweeps)
# =====================================================================

def test_sentinel_kalshi_macro_sweep_triggers_quote_pull():
    """Verify Kalshi macro sweep (price jump > 1.0c / high OFI) emits PULL_QUOTE to Polymarket."""
    bus = CrossVenueBus()
    sentinel = LeadLagSentinel(
        bus=bus,
        price_jump_threshold=0.010,  # 1.0c
        price_pull_threshold=0.025,  # 2.5c
        ofi_fade_threshold=50.0,
        ofi_pull_threshold=150.0,
    )

    # Register macro pair
    sentinel.register_pair(
        poly_token_id="poly_cpi_token",
        kalshi_ticker="CPI-MAR26-B3.0",
        category=MarketCategory.MACRO,
    )

    # Initial quote (baseline)
    sig0 = sentinel.on_kalshi_quote(
        ticker="CPI-MAR26-B3.0",
        yes_bid=0.45,
        yes_ask=0.47,
        yes_bid_size=30,
        yes_ask_size=30,
    )
    assert sig0 is None  # First quote establishes baseline

    # Macro sweep 1: Moderate shift (jump = 1.5c > 1.0c threshold, OFI = 70 < 150) -> FADE_QUOTE
    sig1 = sentinel.on_kalshi_quote(
        ticker="CPI-MAR26-B3.0",
        yes_bid=0.465,
        yes_ask=0.485,
        yes_bid_size=40,
        yes_ask_size=35,
    )
    assert sig1 is not None
    assert sig1.action == SignalAction.FADE_QUOTE
    assert sig1.target_venue == Venue.POLYMARKET
    assert sig1.symbol_target == "poly_token_cpi_token" or sig1.symbol_target == "poly_cpi_token"
    assert round(sig1.price_jump, 3) == 0.015

    # Macro sweep 2: Aggressive sweep (jump = 3.0c > 2.5c pull threshold) -> PULL_QUOTE
    sig2 = sentinel.on_kalshi_quote(
        ticker="CPI-MAR26-B3.0",
        yes_bid=0.495,
        yes_ask=0.515,
        yes_bid_size=300,
        yes_ask_size=20,
    )
    assert sig2 is not None
    assert sig2.action == SignalAction.PULL_QUOTE
    assert sig2.urgency == SignalUrgency.CRITICAL
    assert sig2.target_venue == Venue.POLYMARKET
    assert "pull resting quotes" in sig2.reason


def test_sentinel_polymarket_crypto_sweep_triggers_kalshi_signal():
    """Verify Polymarket crypto sweep emits signal targeting Kalshi engine."""
    bus = CrossVenueBus()
    sentinel = LeadLagSentinel(bus=bus)

    # Register crypto pair
    sentinel.register_pair(
        poly_token_id="poly_btc_token_100k",
        kalshi_ticker="KXBTCD-26MAR-100K",
        category=MarketCategory.CRYPTO,
    )

    # Initial baseline
    sentinel.on_polymarket_quote(
        token_id="poly_btc_token_100k",
        bid=0.60,
        ask=0.62,
        bid_size=500,
        ask_size=500,
    )

    # Crypto surge on Polymarket: price jumps by 2.0c (0.02)
    sig = sentinel.on_polymarket_quote(
        token_id="poly_btc_token_100k",
        bid=0.62,
        ask=0.64,
        bid_size=800,
        ask_size=200,
    )
    assert sig is not None
    assert sig.source_venue == Venue.POLYMARKET
    assert sig.target_venue == Venue.KALSHI
    assert sig.symbol_target == "KXBTCD-26MAR-100K"
    assert sig.action in (SignalAction.FADE_QUOTE, SignalAction.PULL_QUOTE)


# =====================================================================
# 3. Passive Cross-Venue Hedger Tests (Synthetic Complete Sets & Maker Quoting)
# =====================================================================

def test_synthetic_complete_set_locking_via_maker_quoting():
    """Verify Polymarket long YES fill instructs Kalshi maker NO quote @ (1.00 - P_poly) with 0% maker fee."""
    bus = CrossVenueBus()
    kalshi = KalshiClient(mock_mode=True)
    hedger = PassiveCrossVenueHedger(bus=bus, kalshi_client=kalshi)

    poly_token = "token_fed_cut"
    kalshi_ticker = "FED-DEC26-CUT"

    # Polymarket fills long YES: 50 contracts @ $0.42
    p_poly = 0.42
    fill_size = 50.0

    hedge_req = hedger.hedge_polymarket_fill(
        poly_token_id=poly_token,
        kalshi_ticker=kalshi_ticker,
        side="YES",
        fill_price=p_poly,
        fill_size=fill_size,
        execution_mode=HedgeExecutionMode.MAKER_POST_ONLY,
    )

    # Verify hedge request properties
    assert hedge_req.status == HedgeStatus.RESTING
    assert hedge_req.target_side == "NO"
    # Target price on Kalshi is exactly 1.00 - 0.42 = 0.58 (58 cents)
    assert hedge_req.target_price == 0.58
    assert hedge_req.post_only is True

    # Verify Kalshi client received a resting post_only limit order
    assert len(kalshi.recorded_requests) == 1
    kalshi_req = kalshi.recorded_requests[0]
    assert kalshi_req["endpoint"] == "create_order"
    assert kalshi_req["payload"]["ticker"] == kalshi_ticker
    assert kalshi_req["payload"]["side"] == "no"
    assert kalshi_req["payload"]["no_price"] == 58  # 58 cents
    assert kalshi_req["payload"]["post_only"] is True

    # Now simulate resting maker order fill on Kalshi
    pair_state = hedger.on_kalshi_hedge_filled(
        hedge_id=hedge_req.hedge_id,
        fill_price=0.58,
        fill_size=50.0,
    )

    assert pair_state is not None
    # 50 Poly YES + 50 Kalshi NO = 50 Complete Sets
    assert pair_state.total_locked_sets == 50.0
    # Net delta is zero: perfectly hedged!
    assert pair_state.net_delta == 0.0
    assert pair_state.is_fully_hedged is True

    # Settlement payout: 50 * $1.00 = $50.00. Cost: 50 * (0.42 + 0.58) = $50.00 -> 0 risk
    assert pair_state.locked_profit == 0.0


def test_synthetic_pair_arbitrage_locked_profit():
    """Verify locked risk-free arbitrage profit when total purchase price < 1.00."""
    bus = CrossVenueBus()
    kalshi = KalshiClient(mock_mode=True)
    hedger = PassiveCrossVenueHedger(bus=bus, kalshi_client=kalshi)

    # Polymarket buys YES @ $0.38
    req = hedger.hedge_polymarket_fill(
        poly_token_id="poly_token_1",
        kalshi_ticker="K-TICKER-1",
        side="YES",
        fill_price=0.38,
        fill_size=100.0,
        execution_mode=HedgeExecutionMode.MAKER_POST_ONLY,
    )

    # Kalshi maker order fills NO @ $0.60 (60 cents)
    # Total cost per set = 0.38 + 0.60 = 0.98 ($0.02 locked profit per set!)
    state = hedger.on_kalshi_hedge_filled(req.hedge_id, fill_price=0.60, fill_size=100.0)

    assert state.total_locked_sets == 100.0
    # 100 sets * ($1.00 - $0.98) = $2.00 locked guaranteed profit
    assert state.locked_profit == 2.00
    assert state.is_fully_hedged is True


# =====================================================================
# 4. Taker Crossing Rejection Tests (<= 1.5c Hurdle Rule)
# =====================================================================

def test_rejection_of_taker_crossing_when_dislocation_below_hurdle():
    """Verify rejection of taker crossing when spread dislocation <= 1.5c (0.015).

    Protects against Kalshi's 7% quadratic taker fee turning trades into negative EV.
    """
    bus = CrossVenueBus()
    kalshi = KalshiClient(mock_mode=True)
    hedger = PassiveCrossVenueHedger(
        bus=bus,
        kalshi_client=kalshi,
        min_taker_dislocation=MIN_TAKER_DISLOCATION_HURDLE,  # 0.015 (1.5c)
    )

    # Case A: Spread dislocation is 1.0c (0.010 <= 0.015) -> MUST REJECT
    req_rejected_1 = hedger.hedge_polymarket_fill(
        poly_token_id="poly_token",
        kalshi_ticker="KALSHI_TICKER",
        side="YES",
        fill_price=0.50,
        fill_size=25.0,
        execution_mode=HedgeExecutionMode.TAKER_CROSS,
        spread_dislocation=0.010,  # 1.0c
    )

    assert req_rejected_1.status == HedgeStatus.REJECTED
    assert "Taker crossing rejected" in req_rejected_1.rejection_reason
    assert "1.50c hurdle" in req_rejected_1.rejection_reason
    assert "7% quadratic taker fee" in req_rejected_1.rejection_reason
    # Kalshi client must not have placed any order
    assert len(kalshi.recorded_requests) == 0

    # Case B: Spread dislocation is exactly 1.5c (0.015 <= 0.015) -> MUST REJECT
    req_rejected_2 = hedger.hedge_polymarket_fill(
        poly_token_id="poly_token",
        kalshi_ticker="KALSHI_TICKER",
        side="YES",
        fill_price=0.50,
        fill_size=25.0,
        execution_mode=HedgeExecutionMode.TAKER_CROSS,
        spread_dislocation=0.015,  # 1.5c
    )
    assert req_rejected_2.status == HedgeStatus.REJECTED
    assert len(kalshi.recorded_requests) == 0

    # Case C: Passive maker execution is ALWAYS accepted (0% maker fee on Kalshi)
    req_maker = hedger.hedge_polymarket_fill(
        poly_token_id="poly_token",
        kalshi_ticker="KALSHI_TICKER",
        side="YES",
        fill_price=0.50,
        fill_size=25.0,
        execution_mode=HedgeExecutionMode.MAKER_POST_ONLY,
        spread_dislocation=0.010,
    )
    assert req_maker.status == HedgeStatus.RESTING
    assert req_maker.post_only is True
    assert len(kalshi.recorded_requests) == 1


# =====================================================================
# 5. Kalshi REST v2 Client Tests
# =====================================================================

def test_kalshi_client_endpoints_and_circuit_breakers():
    """Verify Kalshi REST v2 client order groups, post-only rules, and cancellations."""
    client = KalshiClient(demo=True, mock_mode=True)
    assert client.base_url == DEMO_BASE_URL

    # Create Order Group with contracts limit circuit breaker
    grp_resp = client.create_order_group(contracts_limit=100)
    grp_id = grp_resp["order_group"]["order_group_id"]
    assert grp_resp["order_group"]["status"] == "active"

    # Place compliant post-only order
    order_resp = client.create_order(
        ticker="INX-CPI-MAR26",
        action="buy",
        side="yes",
        count=40,
        yes_price=52,
        post_only=True,
        order_group_id=grp_id,
    )
    order_id = order_resp["order"]["order_id"]
    assert order_resp["order"]["status"] == "resting"
    assert order_resp["order"]["post_only"] is True

    # Cancel single order
    cancel_resp = client.cancel_order(order_id)
    assert cancel_resp["order"]["status"] == "canceled"

    # Circuit breaker trigger: trip order group
    trip_resp = client.cancel_order_group(grp_id)
    assert "cancelled_orders" in trip_resp

    # Subsequent order on tripped group is rejected
    with pytest.raises(KalshiOrderError, match="circuit breaker is triggered"):
        client.create_order(
            ticker="INX-CPI-MAR26",
            action="buy",
            side="yes",
            count=10,
            yes_price=52,
            post_only=True,
            order_group_id=grp_id,
        )

    # Validation: reject price outside 1-99 cents
    with pytest.raises(KalshiOrderError, match="range \\[1, 99\\]"):
        client.create_order(
            ticker="INX-CPI-MAR26",
            action="buy",
            side="yes",
            count=10,
            yes_price=105,
        )


# =====================================================================
# 6. Kalshi Opportunity Scanner Tests
# =====================================================================

def test_kalshi_opportunity_scanner_microstructure_filter_and_floor():
    """Verify KalshiOpportunityScanner filters out tight HFT books and enforces $1.00 daily incentive floor."""
    scanner = KalshiOpportunityScanner(
        min_spread_cents=3,
        max_spread_cents=15,
        max_competing_depth=500,
        default_quote_size=35,
    )

    test_markets = [
        # Market 1: Jane Street / Susquehanna pegged 1c book with huge queue depth -> REJECT
        {
            "ticker": "HFT-PEGGED-1",
            "title": "Fed Rate Hold",
            "yes_bid": 50,
            "yes_ask": 51,  # 1c spread (< 3c)
            "yes_bid_size": 2500,  # > 500 depth
            "yes_ask_size": 3000,
            "incentive_daily_estimate": 5.0,
        },
        # Market 2: Massive depth competing queue -> REJECT
        {
            "ticker": "HIGH-DEPTH-1",
            "title": "Unemployment Rate",
            "yes_bid": 40,
            "yes_ask": 45,  # 5c spread
            "yes_bid_size": 850,  # >= 500 depth
            "yes_ask_size": 120,
            "incentive_daily_estimate": 2.0,
        },
        # Market 3: Unpegged niche market with wide spread and low incentive (< $1.00 floor)
        {
            "ticker": "NICHE-LOW-INC",
            "title": "FAA Space Launch Approval",
            "yes_bid": 30,
            "yes_ask": 36,  # 6c spread
            "yes_bid_size": 50,
            "yes_ask_size": 80,
            "incentive_daily_estimate": 0.45,  # Below $1.00 floor -> pays $0.00
        },
        # Market 4: Unpegged niche market with eligible incentive (>= $1.00 floor)
        {
            "ticker": "NICHE-HIGH-INC",
            "title": "Senate Judiciary Hearing Date",
            "yes_bid": 45,
            "yes_ask": 50,  # 5c spread
            "yes_bid_size": 75,
            "yes_ask_size": 90,
            "incentive_daily_estimate": 3.50,  # >= $1.00 floor -> pays $3.50
        },
    ]

    ranked = scanner.rank_markets(test_markets, bankroll=300.0, quote_size=35)

    # Markets 1 and 2 must be filtered out
    surviving_tickers = [c.ticker for c in ranked]
    assert "HFT-PEGGED-1" not in surviving_tickers
    assert "HIGH-DEPTH-1" not in surviving_tickers
    assert len(ranked) == 2

    # Verify $1.00 incentive floor enforcement:
    # Market 3 has raw incentive 0.45, so eligible incentive is 0.00
    m3 = next(c for c in ranked if c.ticker == "NICHE-LOW-INC")
    assert m3.eligible_incentive_reward_dollars == 0.00
    assert m3.spread_cents == 6

    # Market 4 has raw incentive 3.50, so eligible incentive is 3.50
    m4 = next(c for c in ranked if c.ticker == "NICHE-HIGH-INC")
    assert m4.eligible_incentive_reward_dollars == 3.50

    # Highest yield candidate must be ranked first
    assert ranked[0].ticker == "NICHE-HIGH-INC"
    assert ranked[0].net_expected_yield_per_dollar > ranked[1].net_expected_yield_per_dollar
