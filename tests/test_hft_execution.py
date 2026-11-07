"""Comprehensive Unit Tests and Microsecond Benchmarks for HFT Execution Components.

Verifies:
1. PreSignedOrderCache: sub-microsecond retrieval (<1 us), tick-grid warming, TTL eviction.
2. HftConnectionPool: TCP_NODELAY=1, SO_KEEPALIVE=1, LIFO connection reuse, orjson serialization.
3. SelfTradeGuard: deterministic sub-microsecond wash trade prevention, complementary token checks.
4. Integrated LiveClobClient: zero-signing quoting, batch placement (POST /orders), STP blocking.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import orjson
import pytest

from polymarketbot.execution import (
    CancelResult,
    HftConnectionPool,
    HftOrderResponse,
    MakerOrderRecord,
    PreSignedOrder,
    PreSignedOrderCache,
    SelfTradeGuard,
    STPAction,
    STPCheckResult,
    make_bucket_key,
)
from polymarketbot.risk.fast_signer import (
    DEFAULT_CHAIN_ID,
    DEFAULT_CTF_EXCHANGE,
    DEFAULT_CTF_EXCHANGE_V2,
    FastEIP712Signer,
    FastEIP712SignerV2,
    FastOrder,
    OrderV2,
)
from polymarketbot.trading.client import (
    ClobAuthCredentials,
    LiveClobClient,
    OrderPlacementResult,
)
from polymarketbot.trading.engine import RealMoneyTradingEngine

SAMPLE_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
EXPECTED_ADDRESS = "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"
SAMPLE_API_KEY = "11111111-2222-3333-4444-555555555555"
SAMPLE_API_SECRET = "ZXhhbXBsZS1zZWNyZXQta2V5LXRlc3QtZGF0YQ=="
SAMPLE_PASSPHRASE = "testpassphrase"
TOKEN_YES = "1001"
TOKEN_NO = "1002"


# =============================================================================
# 1. PreSignedOrderCache Tests & Microsecond Benchmarks
# =============================================================================

class TestPreSignedOrderCache:
    """Test suite for PreSignedOrderCache."""

    @pytest.fixture
    def cache(self) -> PreSignedOrderCache:
        return PreSignedOrderCache(
            private_key=SAMPLE_PRIVATE_KEY,
            api_key=SAMPLE_API_KEY,
            default_version=2,
        )

    def test_warm_bucket_v2(self, cache: PreSignedOrderCache):
        """Verify warming a bucket creates valid V2 signed orders with unique salts."""
        count = cache.warm_bucket(
            token_id=TOKEN_YES,
            side="BUY",
            price=0.55,
            size=50.0,
            count=5,
            order_version=2,
        )
        assert count == 5
        assert cache.get_bucket_depth(TOKEN_YES, "BUY", 0.55, 50.0) == 5

        # Peek order
        peeked = cache.peek_order(TOKEN_YES, "BUY", 0.55, 50.0)
        assert peeked is not None
        assert peeked.token_id == TOKEN_YES
        assert peeked.side == "BUY"
        assert peeked.price == 0.55
        assert peeked.size == 50.0
        assert peeked.version == 2
        assert peeked.signature.startswith("0x")
        assert len(peeked.raw_json_bytes) > 0

        # Verify raw bytes are valid orjson payload
        parsed = orjson.loads(peeked.raw_json_bytes)
        assert parsed["order"]["tokenId"] == TOKEN_YES
        assert parsed["order"]["side"] == "BUY"

    def test_warm_bucket_v1(self, cache: PreSignedOrderCache):
        """Verify warming a bucket creates valid V1 signed orders."""
        count = cache.warm_bucket(
            token_id=TOKEN_NO,
            side="SELL",
            price=0.45,
            size=25.0,
            count=3,
            order_version=1,
        )
        assert count == 3
        order = cache.pop_order(TOKEN_NO, "SELL", 0.45, 25.0)
        assert order is not None
        assert order.version == 1
        assert order.order.side == 1  # SELL
        assert order.signature.startswith("0x")

    def test_microsecond_retrieval_benchmark(self, cache: PreSignedOrderCache):
        """Benchmark order retrieval from memory: MUST take <1 microsecond per pop."""
        iterations = 5000
        # Generate 1 authentic EIP-712 pre-signed order
        cache.warm_bucket(
            token_id=TOKEN_YES,
            side="BUY",
            price=0.50,
            size=10.0,
            count=1,
            order_version=2,
        )
        key = make_bucket_key(TOKEN_YES, "BUY", 0.50, 10.0)
        template_order = cache._buckets[key][0]
        # Duplicate in memory to benchmark pure memory retrieval latency
        cache._buckets[key].extend([template_order] * (iterations - 1))

        t0 = time.perf_counter()
        popped_count = 0
        for _ in range(iterations):
            ord_obj = cache.pop_order(TOKEN_YES, "BUY", 0.50, 10.0)
            if ord_obj is not None:
                popped_count += 1
        duration_sec = time.perf_counter() - t0

        assert popped_count == iterations
        us_per_order = (duration_sec / iterations) * 1e6
        print(f"\n[BENCHMARK] PreSignedOrderCache pop latency: {us_per_order:.3f} microseconds per order")

        # Zero-latency requirement: sub-microsecond (<1.0 us)
        assert us_per_order < 1.0

    def test_warm_grid(self, cache: PreSignedOrderCache):
        """Verify grid warming across discrete prices p in [0.48, 0.52] and sizes."""
        sizes = [10.0, 50.0]
        total = cache.warm_grid(
            token_id=TOKEN_YES,
            sizes=sizes,
            min_price=0.48,
            max_price=0.52,
            tick_size=0.01,
            depth_per_tick=2,
            sides=("BUY", "SELL"),
        )
        # 5 prices (0.48, 0.49, 0.50, 0.51, 0.52) * 2 sizes * 2 sides * 2 depth = 40 orders
        assert total == 40
        stats = cache.stats()
        assert stats["total_cached_orders"] == 40
        assert stats["active_buckets"] == 20

    def test_warm_adjacent_ticks(self, cache: PreSignedOrderCache):
        """Verify adjacent ticks warming around mid-price."""
        total = cache.warm_adjacent_ticks(
            token_id=TOKEN_YES,
            mid_price=0.50,
            ticks_above=2,
            ticks_below=2,
            tick_size=0.01,
            sizes=(10.0,),
            depth_per_tick=2,
        )
        # 5 ticks * 1 size * 2 sides * 2 depth = 20 orders
        assert total == 20

    def test_stale_eviction(self, cache: PreSignedOrderCache):
        """Verify orders exceeding TTL are pruned."""
        cache.warm_bucket(TOKEN_YES, "BUY", 0.50, 10.0, count=3)
        assert cache.get_bucket_depth(TOKEN_YES, "BUY", 0.50, 10.0) == 3

        # Evict with negative max_age (simulates immediate expiration)
        evicted = cache.evict_stale(max_age_seconds=-1.0)
        assert evicted == 3
        assert cache.get_bucket_depth(TOKEN_YES, "BUY", 0.50, 10.0) == 0

    def test_cache_miss_and_stats(self, cache: PreSignedOrderCache):
        """Verify cache miss returns None and records stats."""
        res = cache.pop_order("9999", "BUY", 0.99, 100.0)
        assert res is None
        stats = cache.stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 0
        assert stats["hit_ratio"] == 0.0


# =============================================================================
# 2. HftConnectionPool Tests
# =============================================================================

class TestHftConnectionPool:
    """Test suite for Dedicated Pre-Warmed HftConnectionPool."""

    @pytest.fixture
    def pool(self) -> HftConnectionPool:
        return HftConnectionPool(
            host="clob.polymarket.com",
            port=443,
            pool_size=2,
            api_key=SAMPLE_API_KEY,
            api_secret=SAMPLE_API_SECRET,
            passphrase=SAMPLE_PASSPHRASE,
            signer_address=EXPECTED_ADDRESS,
            auto_warm=False,
        )

    def test_fast_auth_headers_byte_concatenation(self, pool: HftConnectionPool):
        """Verify Level-2 HMAC headers are generated directly on bytes without string allocation."""
        body = b'{"orderID":"0x12345"}'
        headers = pool.build_fast_auth_headers("DELETE", "/order", body_bytes=body)
        assert headers["POLY_API_KEY"] == SAMPLE_API_KEY
        assert "POLY_SIGNATURE" in headers
        assert len(headers["POLY_SIGNATURE"]) > 0
        assert headers["POLY_PASSPHRASE"] == SAMPLE_PASSPHRASE
        assert headers["POLY_ADDRESS"] == EXPECTED_ADDRESS
        assert headers["Connection"] == "keep-alive"

    def test_tcp_nodelay_and_keepalive_socket_options(self, pool: HftConnectionPool):
        """Verify created sockets configure TCP_NODELAY=1 and SO_KEEPALIVE=1."""
        mock_sock = MagicMock(spec=socket.socket)
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn.sock = mock_sock

        with patch("http.client.HTTPSConnection", return_value=mock_conn):
            conn = pool._create_connection()
            mock_conn.connect.assert_called_once()
            mock_sock.setsockopt.assert_any_call(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            mock_sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def test_place_order_and_lifo_recycling(self, pool: HftConnectionPool):
        """Verify place_order sends POST /order with orjson bytes and returns to LIFO queue."""
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"success": true, "orderID": "ord_hft_123"}'
        mock_conn.getresponse.return_value = mock_resp

        # Put mock connection into pool
        pool._pool.put_nowait(mock_conn)

        payload = {"order": {"tokenId": "1001", "price": "0.55"}}
        resp = pool.place_order(payload)

        assert resp.success is True
        assert resp.status == 200
        assert resp.data["orderID"] == "ord_hft_123"
        assert resp.endpoint == "/order"
        assert resp.latency_ms >= 0.0

        # Verify connection was requested with POST /order and returned to pool
        mock_conn.request.assert_called_once()
        call_args = mock_conn.request.call_args
        assert call_args.args[0] == "POST"
        assert call_args.args[1] == "/order"
        # Body was serialized with orjson
        body = call_args.kwargs.get("body")
        assert isinstance(body, bytes)
        assert b'"tokenId":"1001"' in body

        # Verify connection was put back into LIFO queue
        assert pool._pool.qsize() == 1

    def test_place_orders_batch(self, pool: HftConnectionPool):
        """Verify place_orders sends POST /orders with batch payload in single round-trip."""
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'[{"success": true, "orderID": "ord_1"}, {"success": true, "orderID": "ord_2"}]'
        mock_conn.getresponse.return_value = mock_resp

        pool._pool.put_nowait(mock_conn)

        batch_payload = [{"order": {"id": 1}}, {"order": {"id": 2}}]
        resp = pool.place_orders(batch_payload)

        assert resp.success is True
        assert len(resp.data) == 2
        assert resp.data[0]["orderID"] == "ord_1"
        assert resp.data[1]["orderID"] == "ord_2"

    def test_cancel_order_minimal_serialization(self, pool: HftConnectionPool):
        """Verify cancel_order sends DELETE /order with minimal ASCII payload."""
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"success": true}'
        mock_conn.getresponse.return_value = mock_resp

        pool._pool.put_nowait(mock_conn)

        res = pool.cancel_order("ord_cancel_999")
        assert res.success is True
        assert res.order_id == "ord_cancel_999"

        call_args = mock_conn.request.call_args
        assert call_args.args[0] == "DELETE"
        assert call_args.args[1] == "/order"
        assert call_args.kwargs.get("body") == b'{"orderID":"ord_cancel_999"}'

    def test_cancel_all(self, pool: HftConnectionPool):
        """Verify cancel_all sends DELETE /cancel-all."""
        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"success": true}'
        mock_conn.getresponse.return_value = mock_resp

        pool._pool.put_nowait(mock_conn)

        res = pool.cancel_all(market="cond_abc")
        assert res.success is True
        call_args = mock_conn.request.call_args
        assert call_args.args[0] == "DELETE"
        assert call_args.args[1] == "/cancel-all"
        assert call_args.kwargs.get("body") == b'{"market":"cond_abc"}'


# =============================================================================
# 3. SelfTradeGuard (Wash Trading Guard) Tests & Benchmarks
# =============================================================================

class TestSelfTradeGuard:
    """Test suite for SelfTradeGuard."""

    @pytest.fixture
    def guard(self) -> SelfTradeGuard:
        g = SelfTradeGuard(default_action=STPAction.REJECT_TAKER)
        g.link_complementary_tokens(TOKEN_YES, TOKEN_NO)
        return g

    def test_taker_buy_crossing_resting_sell_blocked(self, guard: SelfTradeGuard):
        """Taker BUY at price >= resting maker SELL on same token MUST be blocked."""
        # Register maker SELL at $0.55
        guard.register_maker_order(
            order_id="maker_ask_1",
            token_id=TOKEN_YES,
            side="SELL",
            price=0.55,
            size=100.0,
        )

        # 1. Taker BUY at $0.56 (crosses maker ask at $0.55) -> BLOCKED
        res = guard.check_order(TOKEN_YES, "BUY", 0.56, 10.0, is_taker=True)
        assert res.allowed is False
        assert res.action == STPAction.REJECT_TAKER
        assert len(res.crossing_orders) == 1
        assert res.crossing_orders[0].order_id == "maker_ask_1"
        assert "Self-trade violation" in res.reason

        # 2. Taker BUY at $0.55 (exact touch cross) -> BLOCKED
        res_exact = guard.check_order(TOKEN_YES, "BUY", 0.55, 10.0, is_taker=True)
        assert res_exact.allowed is False

        # 3. Taker BUY at $0.54 (below maker ask) -> ALLOWED
        res_ok = guard.check_order(TOKEN_YES, "BUY", 0.54, 10.0, is_taker=True)
        assert res_ok.allowed is True

    def test_taker_sell_crossing_resting_buy_blocked(self, guard: SelfTradeGuard):
        """Taker SELL at price <= resting maker BUY on same token MUST be blocked."""
        # Register maker BUY at $0.48
        guard.register_maker_order(
            order_id="maker_bid_1",
            token_id=TOKEN_YES,
            side="BUY",
            price=0.48,
            size=100.0,
        )

        # 1. Taker SELL at $0.47 (crosses maker bid at $0.48) -> BLOCKED
        res = guard.check_order(TOKEN_YES, "SELL", 0.47, 20.0, is_taker=True)
        assert res.allowed is False
        assert res.action == STPAction.REJECT_TAKER
        assert len(res.crossing_orders) == 1

        # 2. Taker SELL at $0.49 (above maker bid) -> ALLOWED
        res_ok = guard.check_order(TOKEN_YES, "SELL", 0.49, 20.0, is_taker=True)
        assert res_ok.allowed is True

    def test_complementary_binary_crossing_blocked(self, guard: SelfTradeGuard):
        """Complementary token cross where P(YES) + P(NO) >= 1.00 MUST be blocked."""
        # Register maker BUY on TOKEN_NO at $0.48
        guard.register_maker_order(
            order_id="maker_no_bid",
            token_id=TOKEN_NO,
            side="BUY",
            price=0.48,
            size=50.0,
        )

        # 1. Taker BUY on TOKEN_YES at $0.53:
        # P(YES) 0.53 + P(NO) 0.48 = 1.01 >= 1.00 -> BLOCKED!
        res_cross = guard.check_order(TOKEN_YES, "BUY", 0.53, 25.0, is_taker=True)
        assert res_cross.allowed is False
        assert "Complementary self-trade violation" in res_cross.reason
        assert len(res_cross.crossing_orders) == 1
        assert res_cross.crossing_orders[0].order_id == "maker_no_bid"

        # 2. Taker BUY on TOKEN_YES at $0.51:
        # P(YES) 0.51 + P(NO) 0.48 = 0.99 < 1.00 -> ALLOWED
        res_ok = guard.check_order(TOKEN_YES, "BUY", 0.51, 25.0, is_taker=True)
        assert res_ok.allowed is True

    def test_unregister_and_update_lifecycle(self, guard: SelfTradeGuard):
        """Verify unregistering or updating maker orders resets crossing boundaries."""
        guard.register_maker_order("bid_1", TOKEN_YES, "BUY", 0.50, 100.0)
        assert guard.check_order(TOKEN_YES, "SELL", 0.49, 10.0).allowed is False

        # Unregister maker order
        removed = guard.unregister_maker_order("bid_1")
        assert removed is not None
        assert removed.order_id == "bid_1"

        # Now taker sell at $0.49 is allowed
        assert guard.check_order(TOKEN_YES, "SELL", 0.49, 10.0).allowed is True

        # Test update
        guard.register_maker_order("bid_2", TOKEN_YES, "BUY", 0.40, 100.0)
        assert guard.check_order(TOKEN_YES, "SELL", 0.45, 10.0).allowed is True  # 0.45 > 0.40
        guard.update_maker_order("bid_2", new_size=100.0, new_price=0.46)
        assert guard.check_order(TOKEN_YES, "SELL", 0.45, 10.0).allowed is False  # 0.45 <= 0.46

    def test_cancel_maker_action_mode(self):
        """Verify CANCEL_MAKER mode returns crossing orders for cancellation."""
        guard = SelfTradeGuard(default_action=STPAction.CANCEL_MAKER)
        guard.register_maker_order("ask_1", TOKEN_YES, "SELL", 0.55, 100.0)

        res = guard.check_order(TOKEN_YES, "BUY", 0.58, 20.0)
        assert res.allowed is False
        assert res.action == STPAction.CANCEL_MAKER
        assert len(res.crossing_orders) == 1
        assert res.crossing_orders[0].order_id == "ask_1"

    def test_sub_microsecond_stp_benchmark(self, guard: SelfTradeGuard):
        """Benchmark STP check execution: MUST execute in <1 microsecond."""
        guard.register_maker_order("bid_1", TOKEN_YES, "BUY", 0.45, 100.0)
        guard.register_maker_order("ask_1", TOKEN_YES, "SELL", 0.55, 100.0)

        iterations = 10000
        t0 = time.perf_counter()
        for _ in range(iterations):
            _ = guard.check_order(TOKEN_YES, "BUY", 0.50, 10.0, is_taker=True)
        duration_sec = time.perf_counter() - t0

        us_per_check = (duration_sec / iterations) * 1e6
        print(f"\n[BENCHMARK] SelfTradeGuard check latency: {us_per_check:.3f} microseconds per check")

        # Must execute in under 1 microsecond (typically 100-300 ns)
        assert us_per_check < 1.0


# =============================================================================
# 4. Integrated LiveClobClient & TradingEngine Tests
# =============================================================================

class TestIntegratedHftExecution:
    """Test suite for integrated LiveClobClient and RealMoneyTradingEngine."""

    @pytest.fixture
    def presigned_cache(self) -> PreSignedOrderCache:
        cache = PreSignedOrderCache(
            private_key=SAMPLE_PRIVATE_KEY,
            api_key=SAMPLE_API_KEY,
            default_version=2,
        )
        # Pre-warm bucket
        cache.warm_bucket(
            token_id=TOKEN_YES,
            side="BUY",
            price=0.55,
            size=50.0,
            count=5,
            order_version=2,
        )
        return cache

    @pytest.fixture
    def stp_guard(self) -> SelfTradeGuard:
        guard = SelfTradeGuard(default_action=STPAction.REJECT_TAKER)
        guard.link_complementary_tokens(TOKEN_YES, TOKEN_NO)
        return guard

    def test_client_zero_signing_overhead_via_presigned_cache(
        self,
        presigned_cache: PreSignedOrderCache,
    ):
        """Verify LiveClobClient dispatches pre-signed order with 0 ms signing latency."""
        creds = ClobAuthCredentials(
            api_key=SAMPLE_API_KEY,
            api_secret=SAMPLE_API_SECRET,
            passphrase=SAMPLE_PASSPHRASE,
            private_key=SAMPLE_PRIVATE_KEY,
        )
        client = LiveClobClient(
            credentials=creds,
            dry_run=True,
            presigned_cache=presigned_cache,
            order_version=2,
        )

        assert presigned_cache.get_bucket_depth(TOKEN_YES, "BUY", 0.55, 50.0) == 5

        # Place order matching pre-signed bucket
        t0 = time.perf_counter()
        res = client.place_order(
            token_id=TOKEN_YES,
            side="BUY",
            price=0.55,
            size=50.0,
        )
        elapsed_us = (time.perf_counter() - t0) * 1e6

        assert res.success is True
        assert res.price == 0.55
        assert res.size == 50.0
        # Consumed one from cache
        assert presigned_cache.get_bucket_depth(TOKEN_YES, "BUY", 0.55, 50.0) == 4
        assert presigned_cache.stats()["hits"] == 1
        print(f"\n[BENCHMARK] LiveClobClient place_order with pre-signed cache: {elapsed_us:.2f} us")

    def test_client_self_trade_guard_rejection(
        self,
        stp_guard: SelfTradeGuard,
    ):
        """Verify LiveClobClient blocks taker orders that cross registered maker orders."""
        creds = ClobAuthCredentials(
            api_key=SAMPLE_API_KEY,
            api_secret=SAMPLE_API_SECRET,
            passphrase=SAMPLE_PASSPHRASE,
            private_key=SAMPLE_PRIVATE_KEY,
        )
        client = LiveClobClient(
            credentials=creds,
            dry_run=True,
            self_trade_guard=stp_guard,
        )

        # Place maker order at $0.55
        maker_res = client.place_order(
            token_id=TOKEN_YES,
            side="SELL",
            price=0.55,
            size=50.0,
            post_only=True,
        )
        assert maker_res.success is True
        assert stp_guard.stats()["active_maker_orders"] == 1

        # Attempt taker BUY at $0.56 (would cross own maker SELL at $0.55)
        taker_res = client.place_order(
            token_id=TOKEN_YES,
            side="BUY",
            price=0.56,
            size=10.0,
            post_only=False,
        )
        assert taker_res.success is False
        assert taker_res.status == "STP_REJECTED"
        assert "Self-trade violation" in str(taker_res.error_message)

        # Cancel maker order and verify taker is now allowed
        cancelled = client.cancel_order(maker_res.order_id)
        assert cancelled is True
        assert stp_guard.stats()["active_maker_orders"] == 0

        # Now taker BUY at $0.56 succeeds
        taker_res2 = client.place_order(
            token_id=TOKEN_YES,
            side="BUY",
            price=0.56,
            size=10.0,
            post_only=False,
        )
        assert taker_res2.success is True

    def test_client_place_orders_batch(self):
        """Verify LiveClobClient.place_orders executes batch placements."""
        client = LiveClobClient(dry_run=True, order_version=2)
        batch = [
            {"token_id": TOKEN_YES, "side": "BUY", "price": 0.48, "size": 25.0},
            {"token_id": TOKEN_YES, "side": "SELL", "price": 0.52, "size": 25.0},
            {"token_id": TOKEN_NO, "side": "BUY", "price": 0.47, "size": 20.0},
        ]
        results = client.place_orders(batch)
        assert len(results) == 3
        for res in results:
            assert res.success is True
            assert res.status == "LIVE"
            assert res.order_id.startswith("mock_ord_")

    def test_engine_initializes_with_self_trade_guard(self):
        """Verify RealMoneyTradingEngine wires SelfTradeGuard and links YES/NO tokens."""
        mock_ctf = MagicMock()
        mock_ctf.dry_run = True
        mock_ctf.estimate_merge_cost_usd.return_value = 0.005
        mock_ctf.calculate_optimal_merge_threshold.return_value = 5.0
        engine = RealMoneyTradingEngine(
            condition_id="0x" + "a" * 64,
            asset_id_yes=TOKEN_YES,
            asset_id_no=TOKEN_NO,
            ctf_client=mock_ctf,
            initial_cash=100.0,
            dry_run=True,
            db_path=":memory:",
        )
        guard = engine.clob_client.self_trade_guard
        assert guard is not None
        # Verify complementary link was established
        assert guard._complementary_tokens.get(TOKEN_YES) == TOKEN_NO
        assert guard._complementary_tokens.get(TOKEN_NO) == TOKEN_YES
