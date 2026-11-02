"""Tests for Order Book Microstructure Sentinel."""

import pytest

from polymarketbot.rewards.microstructure import (
    MicrostructureEngine,
    MicrostructureState,
    MultiAssetMicrostructureSentinel,
    QuoteAction,
)


def test_initial_state():
    """Initial update should produce balanced OFI event = 0.0."""
    engine = MicrostructureEngine()
    state = engine.update(
        best_bid_price=0.50,
        best_bid_size=100.0,
        best_ask_price=0.52,
        best_ask_size=100.0,
    )

    assert state.mid_price == 0.51
    assert state.micro_price == 0.51
    assert state.drift_cents == 0.0
    assert state.imbalance_ratio == 0.0
    assert state.ofi_10_events == 0.0
    assert not state.is_toxic
    assert state.toxicity_reason == ""


def test_ofi_same_price_depth_changes():
    """Test OFI when prices stay identical and size changes."""
    engine = MicrostructureEngine()
    # Initial
    engine.update(0.50, 100.0, 0.52, 100.0)

    # Bid size increases +50, ask size decreases -30
    # delta_b = 150 - 100 = 50
    # delta_a = 70 - 100 = -30
    # OFI = 50 - (-30) = +80
    state = engine.update(0.50, 150.0, 0.52, 70.0)
    assert state.ofi_10_events == 80.0


def test_ofi_price_improvements_and_drops():
    """Test Cont-Kukanov-Stoikov delta calculation when price levels change."""
    engine = MicrostructureEngine()
    engine.update(0.50, 100.0, 0.52, 100.0)

    # 1. Higher bid price (improvement): delta_b = Q_b_new (200.0)
    # Ask stays same: delta_a = 0.0
    # OFI = 200.0
    state1 = engine.update(0.51, 200.0, 0.52, 100.0)
    assert state1.ofi_10_events == 200.0

    # 2. Lower ask price (improvement): delta_a = Q_a_new (150.0)
    # Bid drops price (worsened): delta_b = -Q_b_old (-200.0)
    # OFI = -200 - 150 = -350
    state2 = engine.update(0.50, 80.0, 0.51, 150.0)
    # Rolling sum: 200 + (-350) = -150
    assert state2.ofi_10_events == -150.0


def test_ofi_rolling_window_10_events():
    """Test that OFI maintains exactly a 10-event rolling window."""
    engine = MicrostructureEngine(window_size=10)
    engine.update(0.50, 100.0, 0.52, 100.0)

    # Push 10 identical events with +100 OFI each
    for i in range(1, 11):
        # same price, bid size increases by 100 each time
        engine.update(0.50, 100.0 + i * 100.0, 0.52, 100.0)

    # 10 updates with +100 each = 1000 OFI
    assert engine.latest_state.ofi_10_events == 1000.0

    # 11th event: bid size unchanged (+0), ask size increases by 100 (delta_a = 100, OFI = -100)
    # The oldest +100 drops off, replaced by -100
    state11 = engine.update(0.50, 1100.0, 0.52, 200.0)
    assert state11.ofi_10_events == 800.0


def test_stoikov_micro_price_and_drift():
    """Verify Stoikov micro-price formula: (Q_b*P_a + Q_a*P_b)/(Q_b+Q_a)."""
    engine = MicrostructureEngine()

    # P_b = 0.40, P_a = 0.60
    # Q_b = 300, Q_a = 100
    # Micro-price = (300 * 0.60 + 100 * 0.40) / 400 = (180 + 40) / 400 = 220 / 400 = 0.55
    # Mid-price = (0.40 + 0.60) / 2 = 0.50
    # Drift = |0.55 - 0.50| * 100 = 5.0 cents
    state = engine.update(
        best_bid_price=0.40,
        best_bid_size=300.0,
        best_ask_price=0.60,
        best_ask_size=100.0,
    )

    assert pytest.approx(state.mid_price, 1e-6) == 0.50
    assert pytest.approx(state.micro_price, 1e-6) == 0.55
    assert pytest.approx(state.drift_cents, 1e-6) == 5.0
    assert pytest.approx(state.imbalance_ratio, 1e-6) == (300 - 100) / 400  # 0.50
    assert pytest.approx(state.bid_ratio, 1e-6) == 0.75


def test_toxicity_triggers():
    """Test OFI threshold and drift threshold triggers."""
    engine = MicrostructureEngine(
        ofi_threshold=850.0,
        drift_threshold_cents=0.45,
    )

    # Normal state
    s0 = engine.update(0.50, 100.0, 0.51, 100.0)
    assert not s0.is_toxic
    assert s0.toxicity_reason == ""

    # 1. Trigger via OFI only (drift = 0.0)
    engine.reset()
    engine.update(0.50, 100.0, 0.51, 100.0)
    s_ofi = engine.update(0.50, 1000.0, 0.51, 1000.0)
    # delta_b = 900, delta_a = 900 -> OFI = 0
    # Now trigger single large one-sided move
    s_ofi2 = engine.update(0.50, 1950.0, 0.51, 1000.0)  # delta_b = +950, OFI = +950 > 850
    assert abs(s_ofi2.ofi_10_events) > 850.0
    assert s_ofi2.is_toxic
    assert s_ofi2.toxicity_reason in ("OFI", "BOTH")

    # 2. Trigger via Drift only
    engine.reset()
    # Balanced OFI, but severe size skew causing drift > 0.45c
    # P_b = 0.50, P_a = 0.51 (spread = 1c)
    # Q_b = 9000, Q_a = 1000 -> Micro = (9000*0.51 + 1000*0.50)/10000 = 0.509
    # Mid = 0.505. Drift = (0.509 - 0.505)*100 = 0.40c
    # With Q_b = 9800, Q_a = 200 -> Micro = (9800*0.51 + 200*0.50)/10000 = 0.5098
    # Drift = (0.5098 - 0.505)*100 = 0.48c > 0.45c
    s_drift = engine.update(0.50, 9800.0, 0.51, 200.0)
    assert s_drift.drift_cents > 0.45
    assert s_drift.is_toxic


def test_quote_action_evaluation_toxic_buying():
    """When flow is toxic buying (positive OFI / upward drift), asks should be widened/pulled."""
    engine = MicrostructureEngine(
        ofi_threshold=850.0,
        drift_threshold_cents=0.45,
    )
    # Establish base
    engine.update(0.50, 500.0, 0.51, 500.0)
    # Huge buying pressure: OFI = +900
    engine.update(0.50, 1400.0, 0.51, 500.0)

    state = engine.latest_state
    assert state.is_toxic

    # Ask evaluation: toxic buyers will lift asks -> protect asks!
    ask_action = engine.evaluate_quote_action("SELL")
    assert ask_action.action in ("WIDEN", "PULL")
    assert ask_action.should_widen is True
    assert engine.should_widen_quote("SELL") is True

    # Bid evaluation: market is moving upwards away from bids -> bids are safe
    bid_action = engine.evaluate_quote_action("BUY")
    assert bid_action.action == "MAINTAIN"
    assert bid_action.should_pull is False
    assert engine.should_pull_quote("BUY") is False


def test_quote_action_evaluation_toxic_selling():
    """When flow is toxic selling (negative OFI / downward drift), bids should be widened/pulled."""
    engine = MicrostructureEngine(
        ofi_threshold=850.0,
        drift_threshold_cents=0.45,
    )
    # Establish base
    engine.update(0.50, 500.0, 0.51, 500.0)
    # Huge selling pressure: OFI = -900
    engine.update(0.50, 500.0, 0.51, 1400.0)

    state = engine.latest_state
    assert state.is_toxic

    # Bid evaluation: toxic sellers will hit bids -> protect bids!
    bid_action = engine.evaluate_quote_action("BID")
    assert bid_action.action in ("WIDEN", "PULL")
    assert bid_action.should_widen is True
    assert engine.should_widen_quote("BID") is True

    # Ask evaluation: market is moving downwards away from asks -> asks are safe
    ask_action = engine.evaluate_quote_action("ASK")
    assert ask_action.action == "MAINTAIN"
    assert ask_action.should_pull is False
    assert engine.should_pull_quote("ASK") is False


def test_multi_asset_microstructure_sentinel():
    """Verify sentinel tracks independent state across different assets."""
    sentinel = MultiAssetMicrostructureSentinel(ofi_threshold=850.0)

    s_asset1 = sentinel.update(
        asset_id="asset_yes",
        best_bid_price=0.40,
        best_bid_size=100.0,
        best_ask_price=0.42,
        best_ask_size=100.0,
    )
    s_asset2 = sentinel.update(
        asset_id="asset_no",
        best_bid_price=0.58,
        best_bid_size=200.0,
        best_ask_price=0.60,
        best_ask_size=200.0,
    )

    assert s_asset1.mid_price == pytest.approx(0.41)
    assert s_asset2.mid_price == pytest.approx(0.59)
    assert sentinel.get_engine("asset_yes").latest_state.mid_price == pytest.approx(0.41)
    assert sentinel.get_engine("asset_no").latest_state.mid_price == pytest.approx(0.59)
