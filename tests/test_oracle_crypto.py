"""Tests for Cluster 1 Crypto Oracle Adapter and Resolution Evaluator."""

import math
import time
import pytest

from polymarketbot.oracle.base import (
    BaseOracleAdapter,
    GroundTruthOutcome,
    OracleCategory,
    OracleFeedSource,
    OracleStatus,
    ParsedAncillaryData,
    PricePoint,
    ResolutionOutcome,
    ResolutionVerdict,
)
from polymarketbot.oracle.crypto import (
    CryptoMarketMetadata,
    CryptoOracleAdapter,
    DEFAULT_ANNUAL_VOL,
    PYTH_FEED_IDS,
    _norm_cdf,
)


def test_twap_calculation_constant_price():
    """TWAP of constant price must equal that exact price."""
    samples = [(100.0, 50000.0), (120.0, 50000.0), (160.0, 50000.0)]
    twap = BaseOracleAdapter.calculate_twap(samples, start_ts=100.0, end_ts=160.0)
    assert twap == pytest.approx(50000.0, abs=1e-4)


def test_twap_calculation_linear_ramp():
    """TWAP of linear ramp from P0 to P1 over [0, T] must equal (P0 + P1) / 2."""
    samples = [(0.0, 100.0), (30.0, 150.0), (60.0, 200.0)]
    twap = BaseOracleAdapter.calculate_twap(samples, start_ts=0.0, end_ts=60.0)
    assert twap == pytest.approx(150.0, abs=1e-4)


def test_twap_calculation_boundary_clamping():
    """TWAP should clamp and interpolate cleanly when sample points extend beyond window."""
    samples = [(0.0, 100.0), (50.0, 200.0), (100.0, 300.0)]
    # Window is [25.0, 75.0]
    twap = BaseOracleAdapter.calculate_twap(samples, start_ts=25.0, end_ts=75.0)
    assert twap is not None
    # At 25.0, linear interp is 150.0; at 75.0, linear interp is 250.0; mean is 200.0
    assert twap == pytest.approx(200.0, abs=5.0)


def test_ancillary_data_parsing_uma():
    """Test parsing UMA YES_OR_NO_QUERY ancillary data string."""
    adapter = CryptoOracleAdapter(enable_network=False)
    raw = (
        b"q: title: Will Bitcoin be at or above $95,000 on November 15, 2026? "
        b"res_data: p1: 0, p2: 1, p3: 0.5. Source: Binance BTCUSDT spot close."
    )
    parsed = adapter.parse_ancillary_data(raw)
    assert parsed.asset == "BTC"
    assert parsed.strike_price == 95000.0
    assert parsed.comparison_operator == ">="
    assert "Binance" in parsed.source_feed


def test_ancillary_data_parsing_fast_binary():
    """Test parsing short-horizon fast binary question and slug."""
    adapter = CryptoOracleAdapter(enable_network=False)
    raw = "q: title: Bitcoin Up or Down: 12:00 PM - 12:05 PM UTC (1793701200)"
    parsed = adapter.parse_ancillary_data(raw)
    assert parsed.asset == "BTC"
    assert parsed.is_twap is True
    assert parsed.twap_window_seconds == 60.0
    assert parsed.comparison_operator == "UP_OR_DOWN"


def test_market_classification_regimes():
    adapter = CryptoOracleAdapter(enable_network=False)
    now = 1793700000.0

    # 5-minute fast binary
    m5 = {
        "slug": "btc-updown-5m-1793700300",
        "question": "Bitcoin Up or Down: 5m",
        "condition_id": "0xcond5m",
    }
    meta5 = adapter.classify_market(m5, now_ts=now)
    assert meta5.regime == "FAST_5M"
    assert meta5.asset == "BTC"
    assert meta5.is_up_down is True
    assert meta5.is_twap is True
    assert meta5.twap_window_s == 60.0
    assert meta5.end_timestamp == 1793700300.0

    # 15-minute fast binary ETH
    m15 = {
        "slug": "eth-updown-15m-1793700900",
        "question": "Ethereum Up or Down: 15m",
        "condition_id": "0xcond15m",
    }
    meta15 = adapter.classify_market(m15, now_ts=now)
    assert meta15.regime == "FAST_15M"
    assert meta15.asset == "ETH"
    assert meta15.is_twap is True

    # Intraday 1-hour SOL
    m1h = {
        "slug": "sol-updown-1h-1793703600",
        "question": "Solana 1-hour Up or Down",
        "condition_id": "0xcond1h",
    }
    meta1h = adapter.classify_market(m1h, now_ts=now)
    assert meta1h.regime == "INTRADAY_1H"
    assert meta1h.asset == "SOL"

    # Strike binary XRP
    m_strike = {
        "slug": "xrp-above-2-50",
        "question": "Will XRP be above $2.50 on Dec 31?",
        "condition_id": "0xstrike",
    }
    meta_s = adapter.classify_market(m_strike, now_ts=now)
    assert meta_s.regime == "STRIKE"
    assert meta_s.asset == "XRP"
    assert meta_s.strike_price == 2.50


def test_fast_binary_expired_resolution_up_wins():
    """Verify UP wins when closing TWAP >= opening TWAP."""
    adapter = CryptoOracleAdapter(enable_network=False)
    t0 = 1793700000.0
    t_end = t0 + 300.0  # 5-minute expiry

    # Record opening TWAP samples (~90,000)
    for s in range(0, 65, 10):
        adapter.record_price("BTC", 90000.0, timestamp_s=t0 + s)

    # Record closing TWAP samples (~90,100)
    for s in range(240, 305, 10):
        adapter.record_price("BTC", 90100.0, timestamp_s=t0 + s)

    m = {
        "slug": f"btc-updown-5m-{int(t_end)}",
        "question": "Bitcoin Up or Down 5m",
        "condition_id": "0xupdown1",
    }
    # Evaluate at expiry
    verdict = adapter.evaluate_market_resolution(m, as_of_timestamp=t_end + 1.0)
    assert verdict.outcome == ResolutionOutcome.YES
    assert verdict.p_yes == 1.0
    assert verdict.is_deterministic is True
    assert verdict.settlement_price == pytest.approx(90100.0)
    assert verdict.benchmark_price == pytest.approx(90000.0)


def test_fast_binary_expired_resolution_down_wins():
    """Verify DOWN wins when closing TWAP < opening TWAP."""
    adapter = CryptoOracleAdapter(enable_network=False)
    t0 = 1793700000.0
    t_end = t0 + 300.0

    # Opening TWAP = 90,000
    for s in range(0, 65, 10):
        adapter.record_price("BTC", 90000.0, timestamp_s=t0 + s)

    # Closing TWAP = 89,900
    for s in range(240, 305, 10):
        adapter.record_price("BTC", 89900.0, timestamp_s=t0 + s)

    m = {
        "slug": f"btc-updown-5m-{int(t_end)}",
        "question": "Bitcoin Up or Down 5m",
        "condition_id": "0xupdown2",
    }
    verdict = adapter.evaluate_market_resolution(m, as_of_timestamp=t_end + 1.0)
    assert verdict.outcome == ResolutionOutcome.NO
    assert verdict.p_yes == 0.0
    assert verdict.is_deterministic is True
    assert verdict.settlement_price == pytest.approx(89900.0)
    assert verdict.benchmark_price == pytest.approx(90000.0)


def test_fast_binary_tie_resolves_to_up():
    """Polymarket resolution rule: Ties resolve to UP."""
    adapter = CryptoOracleAdapter(enable_network=False)
    t0 = 1793700000.0
    t_end = t0 + 300.0

    # Exactly 90,000 at open and close
    for s in range(0, 305, 15):
        adapter.record_price("BTC", 90000.0, timestamp_s=t0 + s)

    m = {
        "slug": f"btc-updown-5m-{int(t_end)}",
        "question": "Bitcoin Up or Down 5m",
        "condition_id": "0xupdown_tie",
    }
    verdict = adapter.evaluate_market_resolution(m, as_of_timestamp=t_end + 1.0)
    assert verdict.outcome == ResolutionOutcome.YES
    assert verdict.p_yes == 1.0
    assert verdict.is_deterministic is True


def test_in_flight_probabilistic_pricing():
    """Check in-flight digital option pricing dynamics."""
    adapter = CryptoOracleAdapter(enable_network=False)
    t0 = 1793700000.0
    t_end = t0 + 300.0

    # Benchmark established at 90,000
    for s in range(0, 65, 10):
        adapter.record_price("BTC", 90000.0, timestamp_s=t0 + s)

    m = {
        "slug": f"btc-updown-5m-{int(t_end)}",
        "question": "Bitcoin Up or Down 5m",
        "condition_id": "0xprob",
    }

    # Mid-flight at t = 150s, spot is 90,500 (> benchmark)
    v_up = adapter.evaluate_market_resolution(
        m, current_prices={"BTC": 90500.0}, as_of_timestamp=t0 + 150.0
    )
    assert v_up.outcome == ResolutionOutcome.PENDING
    assert v_up.p_yes > 0.5
    assert v_up.seconds_remaining == 150.0

    # Mid-flight at t = 150s, spot is 89,500 (< benchmark)
    v_down = adapter.evaluate_market_resolution(
        m, current_prices={"BTC": 89500.0}, as_of_timestamp=t0 + 150.0
    )
    assert v_down.outcome == ResolutionOutcome.PENDING
    assert v_down.p_yes < 0.5


def test_strike_binary_market_resolution():
    """Fixed strike market resolution evaluation."""
    adapter = CryptoOracleAdapter(enable_network=False)
    t_end = 1793700000.0
    m = {
        "slug": "eth-above-3500",
        "question": "Will ETH be above $3,500 on Friday?",
        "condition_id": "0xethstrike",
        "end_timestamp": t_end,
    }

    # Expired above strike
    v_above = adapter.evaluate_market_resolution(
        m, current_prices={"ETH": 3550.0}, as_of_timestamp=t_end + 10.0
    )
    assert v_above.outcome == ResolutionOutcome.YES
    assert v_above.p_yes == 1.0

    # Expired below strike
    v_below = adapter.evaluate_market_resolution(
        m, current_prices={"ETH": 3450.0}, as_of_timestamp=t_end + 10.0
    )
    assert v_below.outcome == ResolutionOutcome.NO
    assert v_below.p_yes == 0.0


def test_pyth_and_feed_metadata():
    """Verify Pyth feed IDs and standard volatility constants exist for all assets."""
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        assert asset in PYTH_FEED_IDS
        assert PYTH_FEED_IDS[asset].startswith("0x")
        assert asset in DEFAULT_ANNUAL_VOL
        assert DEFAULT_ANNUAL_VOL[asset] > 0.0


def test_can_handle_classification():
    """Verify can_handle routes questions appropriately."""
    adapter = CryptoOracleAdapter(enable_network=False)
    assert adapter.can_handle("Bitcoin Up or Down: 5m") is True
    assert adapter.can_handle("Will Ethereum reach $4,000?") is True
    assert adapter.can_handle("Solana price on Friday", resolution_source="Binance") is True
    assert adapter.can_handle("Will Manchester City win?", category="sports") is False


class MockCandidate:
    def __init__(self, condition_id, question, slug, end_ts):
        self.condition_id = condition_id
        self.question = question
        self.slug = slug
        self.market_slug = slug
        self.end_date_ts = end_ts
        self.category = "crypto"


def test_evaluate_candidate_ground_truth():
    """Test candidate evaluation interface for ExogenousOraclePipeline."""
    adapter = CryptoOracleAdapter(enable_network=False)
    t_end = 1793700000.0
    candidate = MockCandidate(
        condition_id="0xcand1",
        question="Will Bitcoin be above $100,000?",
        slug="btc-above-100k",
        end_ts=t_end,
    )
    adapter.record_price("BTC", 102000.0, timestamp_s=t_end + 5.0)
    ground_truth = adapter.evaluate(candidate)
    assert ground_truth is not None
    assert ground_truth.category == OracleCategory.CRYPTO
    assert ground_truth.concluded is True
    assert ground_truth.status == OracleStatus.CONCLUDED
    assert ground_truth.p_hat == 1.0
    assert ground_truth.is_deterministic_conclusion is True


def test_async_get_latest_price_cached():
    """Test asynchronous get_latest_price from cache without network."""
    import asyncio
    adapter = CryptoOracleAdapter(enable_network=False)
    adapter.record_price("BTC", 95500.0, timestamp_s=1793700000.0, source=OracleFeedSource.BINANCE_SPOT)
    pt = asyncio.run(adapter.get_latest_price("BTC"))
    assert pt.price == 95500.0
    assert pt.asset == "BTC"
    assert pt.source == OracleFeedSource.BINANCE_SPOT
    assert pt.timestamp_s == 1793700000.0
