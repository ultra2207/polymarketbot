"""Comprehensive Unit & Integration Tests for Real-Money Polymarket Trading Engine.

Tests:
1. FastOrder EIP-712 Order Generation & Cryptographic Signatures (Chain ID 137).
2. Polymarket CLOB L2 HMAC-SHA256 Authentication Headers & Signing.
3. Pre-flight Balance, Credential, and Allowance Validation.
4. Dry-Run Safety: Simulated order placements, cancellations, and CTF settlement without live funds.
5. Polygon CTF Contract mergePositions ABI Calldata Encoding & Dynamic Gas Estimation.
6. RealMoneyTradingEngine Orchestration:
   - L2 Order Book Ingestion & Quote Placement.
   - CTF Complete-Set Auto-Merge Settlement.
   - Master RiskManager Circuit Breaker Trip & Emergency Unquoting.
   - Quadratic Reward Epoch Scoring & Capital Compounding.
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from polymarketbot.risk.models import CircuitBreakerReason
from polymarketbot.simulator.feed import OrderBookLevel
from polymarketbot.trading.client import (
    ClobAuthCredentials,
    LiveClobClient,
    build_clob_hmac_signature,
)
from polymarketbot.simulator.portfolio import SimulatedPortfolio
from polymarketbot.trading.ctf import (
    DEFAULT_COLLATERAL_USDC,
    MERGE_POSITIONS_SELECTOR,
    REDEEM_POSITIONS_SELECTOR,
    MergeExecutionResult,
    PolygonCtfClient,
    RedeemExecutionResult,
    calculate_optimal_merge_threshold,
    estimate_merge_cost_usd,
    get_dynamic_gas_price_gwei,
    get_dynamic_pol_usd_price,
)
from polymarketbot.trading.engine import PreflightReport, RealMoneyTradingEngine

SAMPLE_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
EXPECTED_ADDRESS = "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"


# ---------------------------------------------------------------------------
# 1. EIP-712 & HMAC Signature Generation Tests
# ---------------------------------------------------------------------------

def test_clob_order_eip712_generation_and_signing():
    """Verify EIP-712 order structure, 6-decimal scaling, and signature generation."""
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key="mock_key",
            api_secret="dGVzdF9zZWNyZXQ=",  # base64 "test_secret"
            passphrase="mock_passphrase",
            address=EXPECTED_ADDRESS,
            private_key=SAMPLE_PRIVATE_KEY,
        ),
        dry_run=True,
    )

    # Test BUY order
    fast_order, payload = client.build_signed_order(
        token_id="1234567890",
        side="BUY",
        price=0.52,
        size=50.0,
    )

    assert fast_order.side == 0  # BUY
    assert fast_order.maker == EXPECTED_ADDRESS
    # makerAmount = 50 * 0.52 * 1e6 = 26,000,000 USDC
    assert fast_order.makerAmount == 26_000_000
    # takerAmount = 50 * 1e6 = 50,000,000 shares
    assert fast_order.takerAmount == 50_000_000

    sig = payload.get("signature", "")
    assert isinstance(sig, str)
    assert sig.startswith("0x")
    assert len(sig) == 132  # 65 bytes = 130 hex chars + '0x'
    assert payload["order"]["side"] == "BUY"
    assert payload["orderType"] == "GTC"
    assert payload["owner"] == EXPECTED_ADDRESS

    # Test SELL order
    fast_sell, payload_sell = client.build_signed_order(
        token_id="1234567890",
        side="SELL",
        price=0.48,
        size=25.0,
    )
    assert fast_sell.side == 1  # SELL
    assert fast_sell.makerAmount == 25_000_000  # shares
    assert fast_sell.takerAmount == 12_000_000  # 25 * 0.48 = 12 USDC
    assert payload_sell["order"]["side"] == "SELL"


def test_hmac_sha256_header_signing():
    """Verify L2 HMAC-SHA256 signature conforms to Polymarket specification."""
    secret = "dGVzdF9zZWNyZXQ="
    timestamp = "1700000000"
    method = "POST"
    path = "/order"
    body = '{"orderID":"12345"}'

    sig = build_clob_hmac_signature(
        secret=secret,
        timestamp=timestamp,
        method=method,
        request_path=path,
        body=body,
    )
    assert isinstance(sig, str)
    assert len(sig) > 20

    # GET signature with no body
    sig_get = build_clob_hmac_signature(
        secret=secret,
        timestamp=timestamp,
        method="GET",
        request_path="/orders",
    )
    assert isinstance(sig_get, str)
    assert sig_get != sig


# ---------------------------------------------------------------------------
# 2. Polygon CTF Contract & Calldata Tests
# ---------------------------------------------------------------------------

def test_polygon_ctf_calldata_encoding():
    """Verify ABI calldata for mergePositions matches EVM standard."""
    ctf = PolygonCtfClient(dry_run=True)
    cond_id = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    calldata = ctf.encode_merge_positions(
        condition_id=cond_id,
        amount_shares=30.0,
    )

    assert calldata.startswith(f"0x{MERGE_POSITIONS_SELECTOR}")
    clean_calldata = calldata[2:]
    # Selector is 8 hex chars (4 bytes)
    assert clean_calldata[:8] == MERGE_POSITIONS_SELECTOR
    # Collateral token word (USDC.e)
    assert DEFAULT_COLLATERAL_USDC.lower().removeprefix("0x") in clean_calldata[8:72]
    # Condition ID word
    assert cond_id.lower().removeprefix("0x") in clean_calldata[136:200]
    # Amount word (30 * 1e6 = 30,000,000 = 0x1c9c380)
    assert "1c9c380" in clean_calldata[264:328]


def test_polygon_ctf_dry_run_merge():
    """Verify safe dry-run execution of complete set merge."""
    ctf = PolygonCtfClient(dry_run=True)
    with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
         patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
        res = ctf.merge_positions(
            condition_id="0xTEST_CONDITION_ID",
            amount_shares=20.0,
        )

        assert isinstance(res, MergeExecutionResult)
        assert res.success is True
        assert res.is_dry_run is True
        assert res.complete_sets_merged == 20.0
        assert res.usdc_unlocked == 20.0
        assert res.gas_fee_usd > 0.0
        assert res.tx_hash.startswith("0xmock_tx_")


def test_dynamic_gas_price_gwei_live_and_fallbacks():
    """Verify dynamic gas pricing queries base fee + priority fee with fallback to oracle / 40 Gwei."""
    # 1. EIP-1559 base fee (30 Gwei = 0x6fc23ac00) + priority fee (10 Gwei = 0x2540be400) -> 40.0 Gwei
    mock_chain = MagicMock()
    mock_chain._rpc.side_effect = lambda method, params: (
        {"baseFeePerGas": "0x6fc23ac00"} if method == "eth_getBlockByNumber"
        else "0x2540be400" if method == "eth_maxPriorityFeePerGas"
        else None
    )
    gas_gwei = get_dynamic_gas_price_gwei(chain_client=mock_chain)
    assert gas_gwei == 40.0

    # 2. Legacy eth_gasPrice fallback (45 Gwei = 0xa7a358200)
    mock_chain_legacy = MagicMock()
    mock_chain_legacy._rpc.side_effect = lambda method, params: (
        None if method == "eth_getBlockByNumber"
        else "0xa7a358200" if method == "eth_gasPrice"
        else None
    )
    gas_legacy = get_dynamic_gas_price_gwei(chain_client=mock_chain_legacy)
    assert gas_legacy == 45.0

    # 3. Fast gas oracle fallback when RPC raises exception
    mock_failing_chain = MagicMock()
    mock_failing_chain._rpc.side_effect = RuntimeError("RPC unreachable")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"fast": {"maxFee": 52.4}}

    with patch("requests.get", return_value=mock_resp):
        gas_oracle = get_dynamic_gas_price_gwei(chain_client=mock_failing_chain)
        assert gas_oracle == 52.4

    # 4. Ultimate fallback to 40.0 Gwei when RPC and oracle fail
    with patch("requests.get", side_effect=Exception("Oracle down")):
        gas_fallback = get_dynamic_gas_price_gwei(chain_client=mock_failing_chain)
        assert gas_fallback == 40.0


def test_dynamic_pol_usd_price_binance_rest_success():
    """Verify get_dynamic_pol_usd_price queries Binance POLUSDT and caches result."""
    from polymarketbot.trading.ctf import _POL_PRICE_CACHE, _POL_PRICE_LOCK

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"symbol": "POLUSDT", "price": "0.0954"}

    with _POL_PRICE_LOCK:
        _POL_PRICE_CACHE["price"] = 0.092
        _POL_PRICE_CACHE["timestamp"] = 0.0

    with patch("requests.get", return_value=mock_resp) as mock_get:
        px = get_dynamic_pol_usd_price(cache_ttl_seconds=300.0)
        assert px == 0.0954
        assert mock_get.call_count == 1
        args, kwargs = mock_get.call_args
        assert "POLUSDT" in args[0]
        assert kwargs.get("timeout") == 3.0
        assert kwargs.get("headers", {}).get("User-Agent") == "polymarketbot/1.0"


def test_dynamic_pol_usd_price_cache_ttl():
    """Verify cache hits within cache_ttl_seconds avoid outbound HTTP requests."""
    from polymarketbot.trading.ctf import _POL_PRICE_CACHE, _POL_PRICE_LOCK

    with _POL_PRICE_LOCK:
        _POL_PRICE_CACHE["price"] = 0.105
        _POL_PRICE_CACHE["timestamp"] = time.time()

    with patch("requests.get") as mock_get:
        px = get_dynamic_pol_usd_price(cache_ttl_seconds=300.0)
        assert px == 0.105
        assert mock_get.call_count == 0


def test_dynamic_pol_usd_price_matic_fallback():
    """Verify fallback to MATICUSDT when POLUSDT fails."""
    from polymarketbot.trading.ctf import _POL_PRICE_CACHE, _POL_PRICE_LOCK

    with _POL_PRICE_LOCK:
        _POL_PRICE_CACHE["price"] = 0.092
        _POL_PRICE_CACHE["timestamp"] = 0.0

    def mock_get_side_effect(url, headers=None, timeout=None):
        if "POLUSDT" in url:
            resp_fail = MagicMock()
            resp_fail.status_code = 404
            resp_fail.json.return_value = {"code": -1121, "msg": "Invalid symbol."}
            return resp_fail
        elif "MATICUSDT" in url:
            resp_ok = MagicMock()
            resp_ok.status_code = 200
            resp_ok.json.return_value = {"symbol": "MATICUSDT", "price": "0.0915"}
            return resp_ok
        raise ValueError(f"Unexpected url {url}")

    with patch("requests.get", side_effect=mock_get_side_effect) as mock_get:
        px = get_dynamic_pol_usd_price(cache_ttl_seconds=300.0)
        assert px == 0.0915
        assert mock_get.call_count == 2


def test_dynamic_pol_usd_price_all_fail_fallback():
    """Verify fallback price is returned when all Binance queries fail."""
    from polymarketbot.trading.ctf import _POL_PRICE_CACHE, _POL_PRICE_LOCK

    with _POL_PRICE_LOCK:
        _POL_PRICE_CACHE["price"] = 0.092
        _POL_PRICE_CACHE["timestamp"] = 0.0

    with patch("requests.get", side_effect=ConnectionError("Binance unreachable")):
        px = get_dynamic_pol_usd_price(fallback_price=0.088, cache_ttl_seconds=300.0)
        assert px == 0.088


def test_estimate_merge_cost_usd_calculation():
    """Verify dynamic dollar gas cost: Gas Units * Gas Price (POL) * POL/USD."""
    # 78,000 units * 40 Gwei * 1e-9 * 0.45 = 0.001404 USD
    cost = estimate_merge_cost_usd(pol_usd_price=0.45, gas_units=78_000, gas_price_gwei=40.0)
    assert cost == 0.001404

    # 78,000 units * 100 Gwei * 1e-9 * 0.50 = 0.0039 USD
    cost_high = estimate_merge_cost_usd(pol_usd_price=0.50, gas_units=78_000, gas_price_gwei=100.0)
    assert cost_high == 0.0039

    # Via PolygonCtfClient
    ctf = PolygonCtfClient(dry_run=True)
    with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0):
        ctf_cost = ctf.estimate_merge_cost_usd(pol_usd_price=0.45)
        assert ctf_cost == 0.001404
        assert isinstance(ctf_cost, float)


def test_calculate_optimal_merge_threshold():
    """Verify optimal merge threshold calculation ensures liberated capital clears friction hurdle."""
    # Baseline static equivalence: at $0.015 gas and 15 bps hurdle, returns exactly 10.0 complete sets
    threshold_baseline = calculate_optimal_merge_threshold(gas_cost_usd=0.015, fee_basis_points=0.0)
    assert threshold_baseline == 10.0

    # Low gas: $0.001404 gas -> 0.001404 / 0.0015 = 0.936 -> floored at 1.0 complete set
    threshold_low = calculate_optimal_merge_threshold(gas_cost_usd=0.001404, fee_basis_points=0.0)
    assert threshold_low == 1.0

    # High gas: $0.030 gas -> 0.030 / 0.0015 = 20.0 complete sets
    threshold_high = calculate_optimal_merge_threshold(gas_cost_usd=0.030, fee_basis_points=0.0)
    assert threshold_high == 20.0

    # Fee basis points friction: at 10 bps additional fee -> hurdle = 0.0025 -> 0.015 / 0.0025 = 6.0 sets
    threshold_fee = calculate_optimal_merge_threshold(gas_cost_usd=0.015, fee_basis_points=10.0)
    assert threshold_fee == 6.0

    # Via PolygonCtfClient
    ctf = PolygonCtfClient(dry_run=True)
    with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0):
        ctf_thresh = ctf.calculate_optimal_merge_threshold(pol_usd_price=0.45)
        assert ctf_thresh >= 1.0


def test_simulated_portfolio_dynamic_gas_and_threshold_integration():
    """Verify SimulatedPortfolio integrates dynamic gas pricing and optimal merge threshold."""
    ctf = PolygonCtfClient(dry_run=True)
    with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
         patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
        portfolio = SimulatedPortfolio(initial_cash=100.0, ctf_client=ctf)

        # Dynamic gas fee should be set from ctf_client (78,000 * 40 Gwei * 1e-9 * 0.092 = 0.000287)
        assert portfolio.gas_fee_merge == 0.000287

        # Optimal threshold from portfolio helper (0.000287 / 0.0015 = 0.191 -> floored at 1.0)
        threshold = portfolio.get_optimal_merge_threshold()
        assert threshold == 1.0

        # Test merge execution deducts dynamic gas fee
        portfolio.process_fill("DEFAULT", "BUY", "YES", 0.45, 10.0)
        portfolio.process_fill("DEFAULT", "BUY", "NO", 0.50, 10.0)
        assert portfolio.get_position("DEFAULT").complete_sets == 10.0

        res = portfolio.merge("DEFAULT", amount_sets=5.0)
        assert res is not None
        assert res.complete_sets_merged == 5.0
        assert res.gas_fee_deducted == portfolio.gas_fee_merge
        assert portfolio.total_gas_fees == portfolio.gas_fee_merge


def test_real_money_engine_dynamic_merge_threshold_initialization():
    """Verify RealMoneyTradingEngine initializes dynamic threshold without static assumptions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_dyn_trading.db"
        ledger_path = Path(tmpdir) / "test_dyn_ledger.json"

        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine = RealMoneyTradingEngine(
                condition_id="0xDYNAMIC_TEST_CID",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                db_path=db_path,
                ledger_path=ledger_path,
                dry_run=True,
            )

            try:
                # Dynamic gas fee is calculated from live conditions (78_000 * 40e-9 * 0.092 = 0.000287)
                assert engine.gas_fee_merge == 0.000287
                # Dynamic auto_merge_threshold is set
                assert engine.auto_merge_threshold == 1.0

                # Test updating gas and threshold dynamically with 100 Gwei and $0.45 POL (78,000 * 100e-9 * 0.45 = 0.00351 USD)
                with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=100.0), \
                     patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.45):
                    gas_cost, threshold = engine.update_dynamic_gas_and_threshold()
                    assert gas_cost == 0.00351
                    assert threshold == 2.34
                    assert engine.gas_fee_merge == gas_cost
                    assert engine.auto_merge_threshold == threshold
            finally:
                engine.close()


# ---------------------------------------------------------------------------
# 3. Pre-Flight Validation & Dry-Run Safety Tests
# ---------------------------------------------------------------------------

def test_preflight_check_unconfigured_account():
    """Verify pre-flight diagnostics cleanly detect missing keys and $0 balance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_trading.db"
        ledger_path = Path(tmpdir) / "test_ledger.json"

        # Initialize engine without credentials
        clob = LiveClobClient(
            credentials=ClobAuthCredentials(),  # empty
            dry_run=True,
        )
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine = RealMoneyTradingEngine(
                condition_id="0xDEMO_CID",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=0.0,
                db_path=db_path,
                ledger_path=ledger_path,
                dry_run=True,
            )

            report = engine.preflight_check()
            assert isinstance(report, PreflightReport)
            assert report.ready_for_live is False
            assert report.dry_run_mode is True
            assert len(report.recommendations) > 0

            summary = report.summary()
            assert "Preflight Check:" in summary
            assert "USDC Balance" in summary
            engine.close()


def test_dry_run_order_placement_and_cancellation():
    """Verify dry-run mode safely places and cancels simulated orders."""
    client = LiveClobClient(dry_run=True)

    # Place order
    res = client.place_order(
        token_id="123456",
        side="BUY",
        price=0.49,
        size=20.0,
    )
    assert res.success is True
    assert res.is_dry_run is True
    assert res.order_id.startswith("mock_ord_")

    open_orders = client.get_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["orderID"] == res.order_id

    # Cancel order
    cancelled = client.cancel_order(res.order_id)
    assert cancelled is True
    assert len(client.get_open_orders()) == 0

    # Cancel all
    client.place_order(token_id="123456", side="BUY", price=0.49, size=20.0)
    assert len(client.get_open_orders()) == 1
    client.cancel_all_orders()
    assert len(client.get_open_orders()) == 0


# ---------------------------------------------------------------------------
# 4. RealMoneyTradingEngine Orchestration Tests
# ---------------------------------------------------------------------------

class TestRealMoneyTradingEngine:
    """End-to-end integration tests for RealMoneyTradingEngine."""

    @pytest.fixture
    def engine(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "real_trading.db"
            ledger_path = Path(tmpdir) / "real_ledger.json"

            clob = LiveClobClient(dry_run=True)
            ctf = PolygonCtfClient(dry_run=True)

            with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
                 patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
                eng = RealMoneyTradingEngine(
                    condition_id="0xREAL_TEST_COND",
                    asset_id_yes="TOKEN_YES",
                    asset_id_no="TOKEN_NO",
                    clob_client=clob,
                    ctf_client=ctf,
                    initial_cash=100.0,
                    quote_size=20.0,
                    spread_offset_cents=0.50,
                    daily_reward_rate=28.80,
                    auto_merge_threshold=10.0,
                    db_path=db_path,
                    ledger_path=ledger_path,
                    dry_run=True,
                )
                try:
                    yield eng
                finally:
                    eng.close()

    def test_book_update_and_active_clob_quoting(self, engine):
        """Verify L2 order book update places signed orders and tracks them on CLOB."""
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]

        engine.on_book_update("YES", bids=bids, asks=asks)

        # Quotes should be placed for YES and NO
        assert len(engine.active_quotes) >= 1
        yes_bid = engine.active_quotes.get("BUY_YES")
        assert yes_bid is not None
        assert yes_bid["status"] == "LIVE"
        assert yes_bid["order_id"].startswith("mock_ord_")
        assert yes_bid["score"] > 0.0

    def test_complete_set_auto_merge_on_fills(self, engine):
        """Verify complete sets trigger on-chain CTF merge when threshold is reached."""
        engine.shares_yes = 15.0
        engine.shares_no = 15.0
        initial_cash = engine.cash

        # Complete sets = 15.0 >= auto_merge_threshold (10.0)
        res = engine.trigger_ctf_merge()

        assert res is not None
        assert res.success is True
        assert res.complete_sets_merged == 15.0
        assert engine.shares_yes == 0.0
        assert engine.shares_no == 0.0
        assert engine.cash > initial_cash  # unlocked USDC minus gas
        assert engine.total_merges == 1

    def test_circuit_breaker_volatility_jump_unquotes(self, engine):
        """Verify mid-price jump > 2.0c within 60s trips circuit breaker and cancels CLOB orders."""
        # Initial book
        engine.on_book_update("YES", bids=[{"price": 0.50, "size": 100.0}], asks=[{"price": 0.50, "size": 100.0}])
        assert len(engine.active_quotes) > 0

        # Rapid jump by 3 cents (0.50 -> 0.53) within 60s
        t_jump = time.time() + 10.0
        engine.on_book_update(
            "YES",
            bids=[{"price": 0.53, "size": 100.0}],
            asks=[{"price": 0.53, "size": 100.0}],
            timestamp=t_jump,
        )

        # Breaker should trip, quotes cancelled, and cooldown active
        assert len(engine.active_quotes) == 0
        assert engine.is_volatility_cooldown_active is True
        assert engine.risk_manager.portfolio_state.is_halted is True

    def test_daily_loss_circuit_breaker(self, engine):
        """Verify daily loss limit ($15.00) trips risk breaker and cancels orders."""
        engine.on_book_update("YES", bids=[{"price": 0.49, "size": 100.0}], asks=[{"price": 0.51, "size": 100.0}])
        assert len(engine.active_quotes) > 0

        # Simulate heavy daily loss of $20.00
        engine.initial_cash = 100.0
        engine.cash = 80.0  # $20 loss > $15 limit
        engine.shares_yes = 0.0
        engine.shares_no = 0.0

        engine.process_reward_epoch()

        # Engine should trip DAILY_LOSS_LIMIT
        assert engine.risk_manager.portfolio_state.is_halted is True
        events = engine.risk_manager.portfolio_state.circuit_breaker_events
        assert any(e.reason == CircuitBreakerReason.DAILY_LOSS_LIMIT for e in events)
        assert len(engine.active_quotes) == 0

    def test_quadratic_rewards_epoch_scoring(self, engine):
        """Verify reward epoch scoring, floor check, and compounding APR calculation."""
        engine.on_book_update("YES", bids=[{"price": 0.49, "size": 100.0}], asks=[{"price": 0.51, "size": 100.0}])
        res = engine.process_reward_epoch(timestamp=time.time())

        assert res["epoch_index"] == 1
        assert res["user_score"] > 0.0
        assert res["floor_met"] is True
        assert res["epoch_payout_usd"] > 0.0
        assert engine.total_rewards_credited > 0.0

        telemetry = engine.get_telemetry()
        assert telemetry["total_rewards"] > 0.0
        assert "nominal_apr_pct" in telemetry
        assert "compounding_apr_pct" in telemetry

    def test_actual_rewards_telemetry_integration(self, engine):
        """Verify engine telemetry displays authentic on-chain/API reward balance alongside local formulas."""
        engine.actual_rewards_usd = 125.50
        engine.actual_rebates_usd = 14.25
        engine.actual_daily_rewards_usd = 12.00
        engine.reward_source = "polymarket_data_api"
        engine.verified_scoring_orders = {"ord_yes_1": True, "ord_no_1": True}

        telemetry = engine.get_telemetry()
        assert telemetry["actual_rewards_usd"] == 125.50
        assert telemetry["actual_rebates_usd"] == 14.25
        assert telemetry["actual_daily_rewards_usd"] == 12.00
        assert telemetry["actual_total_payout_usd"] == 139.75
        assert telemetry["reward_source"] == "polymarket_data_api"
        assert telemetry["verified_scoring_orders"] == {"ord_yes_1": True, "ord_no_1": True}
        assert "theoretical_rewards_usd" in telemetry

        # Test ledger flush includes actual rewards
        engine.flush_ledger()
        ledger_path = Path(engine.ledger_path)
        assert ledger_path.exists()
        import json
        with open(ledger_path) as f:
            ledger_data = json.load(f)
        port = ledger_data["portfolio"]
        assert port["actual_rewards_usd"] == 125.50
        assert port["actual_rebates_usd"] == 14.25
        assert port["actual_daily_rewards_usd"] == 12.00
        assert port["reward_source"] == "polymarket_data_api"


# ---------------------------------------------------------------------------
# 5. Polymarket Ground-Truth Rewards API Client Tests
# ---------------------------------------------------------------------------

def test_clob_rewards_dry_run_modes():
    """Verify dry-run mode returns structured simulated reward objects without network calls."""
    client = LiveClobClient(dry_run=True)

    # Total rewards
    tot = client.get_total_rewards()
    assert tot["source"] == "simulated"
    assert tot["total_rewards_usd"] == 0.0
    assert tot["reward_income_usd"] == 0.0
    assert tot["payout_count"] == 0

    # Daily rewards
    daily = client.get_daily_rewards("2026-11-03")
    assert daily["date"] == "2026-11-03"
    assert daily["source"] == "simulated"
    assert daily["daily_rewards_usd"] == 0.0

    # Order scoring
    score = client.get_order_scoring("mock_ord_123")
    assert score["scoring"] is False  # not in mock_orders yet

    client._mock_orders["mock_ord_123"] = {"orderID": "mock_ord_123"}
    score_after = client.get_order_scoring("mock_ord_123")
    assert score_after["scoring"] is True
    assert score_after["dry_run"] is True


def test_clob_rewards_authenticated_and_data_api():
    """Verify live client methods correctly parse CLOB and Data API reward responses."""
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key="key",
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            address="0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93",
            private_key=SAMPLE_PRIVATE_KEY,
        ),
        dry_run=False,
    )

    # Test 1: get_total_rewards via CLOB /rewards/user/total
    with patch.object(client.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "total_rewards": 450.75,
            "maker_rebates": 32.50,
            "count": 15,
            "payouts": [{"amount": 25.0}],
        }
        mock_get.return_value = mock_resp

        tot = client.get_total_rewards()
        assert tot["source"] == "clob_api"
        assert tot["total_rewards_usd"] == 450.75
        assert tot["maker_rebates_usd"] == 32.50
        assert tot["payout_count"] == 15

    # Test 2: get_daily_rewards via CLOB /rewards/user
    with patch.object(client.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "daily_rewards": 28.50,
            "maker_rebates": 2.10,
            "markets": [{"condition_id": "0x123", "score": 100.0}],
        }
        mock_get.return_value = mock_resp

        daily = client.get_daily_rewards("2026-11-03")
        assert daily["source"] == "clob_api"
        assert daily["daily_rewards_usd"] == 28.50
        assert daily["daily_rebates_usd"] == 2.10
        assert len(daily["market_breakdown"]) == 1

    # Test 3: get_order_scoring via CLOB /order-scoring
    with patch.object(client.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"scoring": True}
        mock_get.return_value = mock_resp

        score = client.get_order_scoring("0xORDER_HASH_123")
        assert score["order_id"] == "0xORDER_HASH_123"
        assert score["scoring"] is True
        assert score["dry_run"] is False

    # Test 4: get_market_reward_config via CLOB /rewards/user/markets
    with patch.object(client.session, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {
                    "condition_id": "0xCOND_1",
                    "market_id": "1001",
                    "question": "Sample Market?",
                    "rewards_config": [{"rate_per_day": 50.0}],
                    "rewards_max_spread": 2.5,
                    "rewards_min_size": 150.0,
                    "market_competitiveness": 5000.0,
                    "earning_percentage": 0.12,
                }
            ]
        }
        mock_get.return_value = mock_resp

        cfg = client.get_market_reward_config("0xCOND_1")
        assert cfg is not None
        assert cfg["rate_per_day"] == 50.0
        assert cfg["rewards_max_spread"] == 2.5
        assert cfg["rewards_min_size"] == 150.0
        assert cfg["market_competitiveness"] == 5000.0
        assert cfg["earning_percentage"] == 0.12


# ---------------------------------------------------------------------------
# 6. Pre-flight, Signing Security, & Circuit Breaker Audit Tests
# ---------------------------------------------------------------------------

def test_polygon_ctf_redeem_positions_calldata_and_simulation():
    """Verify redeemPositions calldata encoding (selector 0x01b7037c), gas estimation, and dry run."""
    ctf = PolygonCtfClient(dry_run=True)
    cond_id = "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    calldata = ctf.encode_redeem_positions(
        condition_id=cond_id,
        index_sets=[1, 2],
    )

    assert calldata.startswith(f"0x{REDEEM_POSITIONS_SELECTOR}")
    assert calldata[2:10] == "01b7037c"
    clean_calldata = calldata[10:]
    # Word 0: collateral token
    assert DEFAULT_COLLATERAL_USDC.lower().removeprefix("0x") in clean_calldata[0:64]
    # Word 1: parent collection ID (32 zero bytes)
    assert clean_calldata[64:128] == "0" * 64
    # Word 2: condition ID
    assert cond_id.lower().removeprefix("0x") in clean_calldata[128:192]
    # Word 3: offset to indexSets dynamic array = 128 = 0x80
    assert int(clean_calldata[192:256], 16) == 128
    # Word 4: length of indexSets = 2
    assert int(clean_calldata[256:320], 16) == 2
    # Elements: [1, 2]
    assert int(clean_calldata[320:384], 16) == 1
    assert int(clean_calldata[384:448], 16) == 2

    # Dry-run execution
    with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
         patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
        res = ctf.redeem_positions(condition_id=cond_id, index_sets=[1, 2])
        assert isinstance(res, RedeemExecutionResult)
        assert res.success is True
        assert res.is_dry_run is True
        assert res.tx_hash.startswith("0xmock_redeem_tx_")
        assert res.gas_units_used == 45_000
        assert res.gas_fee_usd > 0.0


def test_clob_credentials_formatting_validation():
    """Verify credential validation identifies valid vs malformed API keys, secrets, and addresses."""
    # Valid credentials
    valid_creds = ClobAuthCredentials(
        api_key="12345678-1234-4234-8234-123456789abc",
        api_secret="dGVzdF9zZWNyZXQ=",
        passphrase="valid_passphrase",
        address="0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93",
        private_key=SAMPLE_PRIVATE_KEY,
    )
    assert valid_creds.has_signing_key is True
    assert valid_creds.is_fully_configured is True
    assert len(valid_creds.validate_formatting()) == 0

    # Malformed credentials
    invalid_creds = ClobAuthCredentials(
        api_key="not-a-valid-uuid",
        api_secret="!!!not_base64!!!",
        passphrase="pass",
        address="0xinvalid_address_length",
        private_key="0xshort_key",
    )
    errors = invalid_creds.validate_formatting()
    assert len(errors) == 4
    assert any("UUID" in e for e in errors)
    assert any("base64" in e for e in errors)
    assert any("POLY_PRIVATE_KEY" in e for e in errors)
    assert any("POLY_ADDRESS" in e for e in errors)
    assert invalid_creds.has_signing_key is False
    assert invalid_creds.is_fully_configured is False


def test_preflight_allowance_enforcement():
    """Verify preflight check sets ready_for_live=False if allowances are unapproved."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_trading.db"
        ledger_path = Path(tmpdir) / "test_ledger.json"

        # Mock configured credentials with unapproved allowances
        clob = LiveClobClient(
            credentials=ClobAuthCredentials(
                api_key="12345678-1234-4234-8234-123456789abc",
                api_secret="dGVzdF9zZWNyZXQ=",
                passphrase="valid_passphrase",
                private_key=SAMPLE_PRIVATE_KEY,
            ),
            dry_run=False,
        )
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092), \
             patch.object(clob, "get_allowances", return_value={"approved": False}), \
             patch.object(clob, "get_balance", return_value={"available": 100.0}), \
             patch.object(clob, "get_total_rewards", return_value={}), \
             patch.object(clob, "get_daily_rewards", return_value={}), \
             patch.object(ctf.chain_client, "block_number", return_value=12345678):
            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_CID",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                db_path=db_path,
                ledger_path=ledger_path,
                dry_run=False,
            )

            report = engine.preflight_check()
            assert report.allowances_approved is False
            assert report.ready_for_live is False
            assert engine.dry_run is True  # Demoted to dry run for safety
            assert any("allowances" in rec.lower() for rec in report.recommendations)

            engine.close()


def test_initial_cash_calibration_no_premature_halt():
    """Verify engine initialized with $100.00 cash does NOT trip daily loss circuit breaker on startup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_trading.db"
        ledger_path = Path(tmpdir) / "test_ledger.json"

        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_CID",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                db_path=db_path,
                ledger_path=ledger_path,
                dry_run=True,
            )

            # Before fix, day_start_equity defaulted to 300.0 and process_reward_epoch tripped immediately
            res = engine.process_reward_epoch(timestamp=time.time())
            assert engine.risk_manager.portfolio_state.is_halted is False
            assert len(engine.risk_manager.portfolio_state.circuit_breaker_events) == 0
            assert engine.risk_manager.portfolio_state.day_start_equity_usd == 100.0
            assert engine.risk_manager.portfolio_state.peak_equity_usd == 100.0

            engine.close()


def test_real_money_max_drawdown_circuit_breaker():
    """Verify max drawdown limit ($30.00 / 10%) trips circuit breaker, cancels quotes, and halts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_trading.db"
        ledger_path = Path(tmpdir) / "test_ledger.json"

        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_CID",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                db_path=db_path,
                ledger_path=ledger_path,
                dry_run=True,
            )

        try:
            # Place initial quote
            engine.on_book_update("YES", bids=[{"price": 0.49, "size": 100.0}], asks=[{"price": 0.51, "size": 100.0}])
            assert len(engine.active_quotes) > 0

            # Simulate account where peak equity reached $150.0, today's starting equity is $100.0,
            # and current equity drops to $98.0 ($2 daily loss < $5 limit, but peak was $150 so drawdown is $52 > $30 limit / 10%)
            engine.risk_manager.portfolio_state.peak_equity_usd = 150.0
            engine.risk_manager.portfolio_state.day_start_equity_usd = 100.0
            engine.cash = 98.0
            engine.shares_yes = 0.0
            engine.shares_no = 0.0

            engine.evaluate_quotes(timestamp=time.time())

            assert engine.risk_manager.portfolio_state.is_halted is True
            events = engine.risk_manager.portfolio_state.circuit_breaker_events
            assert any(e.reason == CircuitBreakerReason.MAX_DRAWDOWN_LIMIT for e in events)
            assert len(engine.active_quotes) == 0

            # Subsequent book updates must NOT place new quotes while halted
            engine.on_book_update("YES", bids=[{"price": 0.49, "size": 100.0}], asks=[{"price": 0.51, "size": 100.0}])
            assert len(engine.active_quotes) == 0
        finally:
            engine.close()


def test_order_book_levels_dataclass_ingestion():
    """Verify on_book_update seamlessly parses OrderBookLevel dataclass objects from LivePolymarketFeed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_trading.db"
        ledger_path = Path(tmpdir) / "test_ledger.json"

        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_CID",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                db_path=db_path,
                ledger_path=ledger_path,
                dry_run=True,
            )

        try:
            # Feed uses OrderBookLevel objects
            bids = [OrderBookLevel(price=0.48, size=150.0), OrderBookLevel(price=0.47, size=200.0)]
            asks = [OrderBookLevel(price=0.52, size=120.0), OrderBookLevel(price=0.53, size=180.0)]

            # Should execute cleanly without "'OrderBookLevel' object is not subscriptable"
            engine.on_book_update("YES", bids=bids, asks=asks)

            assert engine._best_bids["YES"] == 0.48
            assert engine._best_asks["YES"] == 0.52
            assert len(engine.active_quotes) > 0
        finally:
            engine.close()


def test_live_vs_dry_run_trade_update_fill_behavior():
    """Verify on_trade_update does NOT infer simulated fills in live mode, but retains them in dry-run mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_trading.db"
        ledger_path = Path(tmpdir) / "test_ledger.json"

        # --- 1. Dry Run Mode ---
        clob_dry = LiveClobClient(dry_run=True)
        ctf_dry = PolygonCtfClient(dry_run=True)
        with patch.object(ctf_dry, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine_dry = RealMoneyTradingEngine(
                condition_id="0xTEST_DRY",
                clob_client=clob_dry,
                ctf_client=ctf_dry,
                initial_cash=100.0,
                quote_size=20.0,
                db_path=db_path,
                ledger_path=ledger_path,
                dry_run=True,
            )

        try:
            # Place resting quote
            engine_dry.on_book_update("YES", bids=[{"price": 0.49, "size": 100.0}], asks=[{"price": 0.51, "size": 100.0}])
            assert len(engine_dry.active_quotes) > 0

            # Public trade crossing our bid price at 0.48
            engine_dry.on_trade_update("YES", trade_price=0.48, trade_size=10.0, trade_side="SELL")
            # In dry run, simulated fill MUST be recorded
            assert len(engine_dry.fills_history) == 1
            assert engine_dry.shares_yes == 10.0
        finally:
            engine_dry.close()

        # --- 2. Live Mode (dry_run=False) ---
        clob_live = LiveClobClient(
            credentials=ClobAuthCredentials(
                api_key="12345678-1234-4234-8234-123456789abc",
                api_secret="dGVzdF9zZWNyZXQ=",
                passphrase="valid_passphrase",
                private_key=SAMPLE_PRIVATE_KEY,
            ),
            dry_run=False,
        )
        ctf_live = PolygonCtfClient(private_key=SAMPLE_PRIVATE_KEY, dry_run=False)

        with patch.object(ctf_live, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092), \
             patch.object(clob_live, "get_allowances", return_value={"approved": True}), \
             patch.object(clob_live, "get_balance", return_value={"available": 100.0}), \
             patch.object(clob_live, "get_total_rewards", return_value={}), \
             patch.object(clob_live, "get_daily_rewards", return_value={}), \
             patch.object(ctf_live.chain_client, "block_number", return_value=12345678), \
             patch.object(clob_live, "place_order") as mock_place, \
             patch.object(clob_live, "get_open_orders") as mock_open:
            
            from polymarketbot.trading.client import OrderPlacementResult
            mock_place.return_value = OrderPlacementResult(
                success=True,
                order_id="live_order_123",
                status="LIVE",
                side="BUY",
                price=0.49,
                size=20.0,
                is_dry_run=False,
            )
            # The resting order remains open on the exchange with 0 matched size
            mock_open.return_value = [
                {"orderID": "live_order_123", "size_matched": 0.0, "status": "LIVE"}
            ]

            engine_live = RealMoneyTradingEngine(
                condition_id="0xTEST_LIVE",
                clob_client=clob_live,
                ctf_client=ctf_live,
                initial_cash=100.0,
                quote_size=20.0,
                db_path=Path(tmpdir) / "live_trading.db",
                ledger_path=Path(tmpdir) / "live_ledger.json",
                dry_run=False,
            )

            try:
                assert engine_live.dry_run is False
                # Place live quote
                engine_live.on_book_update("YES", bids=[{"price": 0.49, "size": 100.0}], asks=[{"price": 0.51, "size": 100.0}])
                assert "BUY_YES" in engine_live.active_quotes

                # Public trade crosses at 0.48. In LIVE mode, this MUST NOT produce a simulated fill!
                # It only triggers sync_live_orders_and_fills() and updates heartbeat/monitors.
                # Since the exchange still reports our order as LIVE with 0 matched, fills_history remains empty.
                engine_live.on_trade_update("YES", trade_price=0.48, trade_size=10.0, trade_side="SELL")
                assert len(engine_live.fills_history) == 0
                assert engine_live.shares_yes == 0.0
            finally:
                engine_live.close()


def test_sync_live_orders_and_fills_partial_and_full():
    """Verify sync_live_orders_and_fills accurately reconciles partial and full fills from CLOB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_SYNC",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                quote_size=20.0,
                db_path=Path(tmpdir) / "sync_trading.db",
                ledger_path=Path(tmpdir) / "sync_ledger.json",
                dry_run=True,
            )

        try:
            # Place resting quote
            engine.on_book_update("YES", bids=[{"price": 0.50, "size": 20.0}], asks=[{"price": 0.52, "size": 20.0}])
            qid = "BUY_YES"
            assert qid in engine.active_quotes
            order_id = engine.active_quotes[qid]["order_id"]
            quote_px = engine.active_quotes[qid]["price"]
            no_oid = engine.active_quotes.get("BUY_NO", {}).get("order_id", "")

            # Scenario 1: Partial fill on exchange (8.0 shares matched of YES; NO remains open)
            open_list = [{"orderID": order_id, "size_matched": 8.0, "status": "LIVE"}]
            if no_oid:
                open_list.append({"orderID": no_oid, "size_matched": 0.0, "status": "LIVE"})

            with patch.object(clob, "get_open_orders", return_value=open_list):
                new_fills = engine.sync_live_orders_and_fills()
                assert len(new_fills) == 1
                assert new_fills[0].token_side == "YES"
                assert new_fills[0].fill_size == 8.0
                assert abs(new_fills[0].fill_price - quote_px) < 1e-4
                assert engine.shares_yes == 8.0
                assert abs(engine.cash - (100.0 - (8.0 * quote_px))) < 1e-4
                assert engine.active_quotes[qid]["size_matched"] == 8.0

            # Scenario 2: YES order disappears from open orders without cancellation -> full fill of remainder (12.0 shares)
            open_list_2 = []
            if no_oid:
                open_list_2.append({"orderID": no_oid, "size_matched": 0.0, "status": "LIVE"})

            with patch.object(clob, "get_open_orders", return_value=open_list_2):
                new_fills2 = engine.sync_live_orders_and_fills()
                assert len(new_fills2) == 1
                assert new_fills2[0].token_side == "YES"
                assert new_fills2[0].fill_size == 12.0
                assert engine.shares_yes == 20.0
                assert abs(engine.cash - (100.0 - (20.0 * quote_px))) < 1e-4
                assert qid not in engine.active_quotes

            # Scenario 3: Explicit cancellation does NOT emit fill
            engine.on_book_update("YES", bids=[{"price": 0.50, "size": 20.0}], asks=[{"price": 0.52, "size": 20.0}])
            assert qid in engine.active_quotes
            canceled_oid = engine.active_quotes[qid]["order_id"]
            engine._cancel_quote(qid)
            assert canceled_oid in engine._canceled_order_ids

            # Cancel any other remaining quotes
            for rem_qid in list(engine.active_quotes.keys()):
                engine._cancel_quote(rem_qid)

            with patch.object(clob, "get_open_orders", return_value=[]):
                new_fills3 = engine.sync_live_orders_and_fills()
                assert len(new_fills3) == 0
        finally:
            engine.close()


def test_safe_ctf_merge_complete_set_verification():
    """Verify trigger_ctf_merge safely verifies held complete sets before executing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092), \
             patch.object(ctf, "merge_positions") as mock_merge:

            mock_merge.return_value = MergeExecutionResult(
                success=True,
                tx_hash="0xmock_merge_tx",
                condition_id="0xTEST_MERGE",
                complete_sets_merged=10.0,
                usdc_unlocked=10.0,
                gas_fee_usd=0.015,
                gas_units_used=78000,
                is_dry_run=True,
            )

            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_MERGE",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                auto_merge_threshold=10.0,
                db_path=Path(tmpdir) / "merge_trading.db",
                ledger_path=Path(tmpdir) / "merge_ledger.json",
                dry_run=True,
            )

            try:
                # Case 1: No complete sets (YES=15, NO=0)
                engine.shares_yes = 15.0
                engine.shares_no = 0.0
                res = engine.trigger_ctf_merge()
                assert res is None
                mock_merge.assert_not_called()

                # Case 2: Complete sets available (YES=10, NO=10)
                engine.shares_no = 10.0
                res2 = engine.trigger_ctf_merge()
                assert res2 is not None
                mock_merge.assert_called_once_with(condition_id="0xTEST_MERGE", amount_shares=10.0)

                # Case 3: Requested amount_sets exceeds available complete sets -> clamped safely
                mock_merge.reset_mock()
                engine.shares_yes = 5.0
                engine.shares_no = 5.0
                mock_merge.return_value.complete_sets_merged = 5.0
                mock_merge.return_value.usdc_unlocked = 5.0

                res3 = engine.trigger_ctf_merge(amount_sets=25.0)  # requested 25, only 5 available
                assert res3 is not None
                mock_merge.assert_called_once_with(condition_id="0xTEST_MERGE", amount_shares=5.0)
            finally:
                engine.close()


def test_sync_live_balances_and_alias():
    """Verify sync_live_balances queries CLOB & CTF balances and sync_balances aliases it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):
            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_BAL",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=50.0,
                db_path=Path(tmpdir) / "bal_trading.db",
                ledger_path=Path(tmpdir) / "bal_ledger.json",
                dry_run=True,
            )

        try:
            with patch.object(clob, "get_balance", return_value={"available": 250.0}), \
                 patch.object(ctf, "get_pol_balance", return_value=1.85), \
                 patch.object(ctf, "get_usdc_balance", return_value=250.0):
                
                bals = engine.sync_live_balances()
                assert bals["cash"] == 250.0
                assert bals["pol"] == 1.85
                assert engine.cash == 250.0
                assert engine.pol_balance == 1.85

                # Verify alias
                engine.cash = 0.0
                engine.sync_balances()
                assert engine.cash == 250.0
        finally:
            engine.close()


def test_dynamic_budget_percentage_allocation_and_isolation():
    """Verify dynamic budget percentage sizing isolates remaining balance and bounds trading bankroll."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clob = LiveClobClient(dry_run=True)
        ctf = PolygonCtfClient(dry_run=True)

        with patch.object(ctf, "get_dynamic_gas_price_gwei", return_value=40.0), \
             patch("polymarketbot.trading.ctf.get_dynamic_pol_usd_price", return_value=0.092):

            # Case 1: 20% budget of $1,000 wallet with $70 cap -> bankroll capped at $70.00
            engine = RealMoneyTradingEngine(
                condition_id="0xTEST_BUDGET",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=70.0,
                total_wallet_balance=1000.0,
                budget_pct=0.20,
                max_budget=70.0,
                db_path=Path(tmpdir) / "budget_trading.db",
                ledger_path=Path(tmpdir) / "budget_ledger.json",
                dry_run=True,
            )

            try:
                assert engine.total_wallet_balance == 1000.0
                assert engine.budget_pct == 0.20
                assert engine.max_budget == 70.0
                assert engine.cash == 70.0
                assert engine.isolated_cash == 930.0

                t = engine.get_telemetry()
                assert t["total_wallet_balance"] == 1000.0
                assert t["budget_pct"] == 20.0
                assert t["trading_bankroll"] == 70.0
                assert t["cash"] == 70.0

                # Sync balances with $1200 discovered wallet balance
                with patch.object(clob, "get_balance", return_value={"available": 1200.0}):
                    engine.sync_balances()
                    # 20% of $1200 is $240, but capped by max_budget at $70.00
                    assert engine.total_wallet_balance == 1200.0
                    assert engine.cash == 70.0
            finally:
                engine.close()

            # Case 2: 25% budget of $400 wallet without cap -> bankroll is $100.00
            engine2 = RealMoneyTradingEngine(
                condition_id="0xTEST_BUDGET2",
                clob_client=clob,
                ctf_client=ctf,
                initial_cash=100.0,
                total_wallet_balance=400.0,
                budget_pct=0.25,
                max_budget=None,
                db_path=Path(tmpdir) / "budget_trading2.db",
                ledger_path=Path(tmpdir) / "budget_ledger2.json",
                dry_run=True,
            )

            try:
                assert engine2.total_wallet_balance == 400.0
                assert engine2.budget_pct == 0.25
                assert engine2.cash == 100.0
                assert engine2.isolated_cash == 300.0

                # Sync balances when wallet is $500
                with patch.object(clob, "get_balance", return_value={"available": 500.0}):
                    engine2.sync_balances()
                    # 25% of $500 = $125.00
                    assert engine2.total_wallet_balance == 500.0
                    assert engine2.cash == 125.0
            finally:
                engine2.close()


def test_real_money_trader_cli_budget_args():
    """Verify CLI argument parsing and normalization for --budget-pct and --max-budget."""
    import sys
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import run_real_money_trader
    parse_args = run_real_money_trader.parse_args

    test_args = ["run_real_money_trader.py", "--budget-pct", "0.20", "--max-budget", "70.0", "--initial-cash", "50.0"]
    with patch.object(sys, "argv", test_args):
        args = parse_args()
        assert args.budget_pct == 0.20
        assert args.max_budget == 70.0
        assert args.initial_cash == 50.0

    # Test defaults
    with patch.object(sys, "argv", ["run_real_money_trader.py"]):
        args_default = parse_args()
        assert args_default.budget_pct == 1.0
        assert args_default.max_budget is None
        assert args_default.initial_cash == 100.0




