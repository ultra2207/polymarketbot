"""Pure-parser tests for the Kalshi market tape (no network)."""

from __future__ import annotations

import json

from polymarketbot.data.streams.kalshi import (
    MarketChangeDetector,
    _iso_to_epoch,
    _probability,
    market_signature,
    parse_market,
    poll_trades,
)


def test_parse_cents_market() -> None:
    raw = {
        "ticker": "KXTEST-26DEC31",
        "event_ticker": "KXTEST-26",
        "series_ticker": "KXTEST",
        "title": "Will the test pass?",
        "yes_bid": 55,
        "yes_ask": 60,
        "last_price": 57,
        "volume": 1234,
        "volume_24h": 12,
        "open_interest": 99,
        "liquidity": 5000,
        "close_time": "2026-12-31T00:00:00Z",
        "status": "open",
    }
    row = parse_market(raw, 111, 222)

    assert row["recv_ts_ns"] == 111
    assert row["recv_mono_ns"] == 222
    assert row["ticker"] == "KXTEST-26DEC31"
    assert row["event_ticker"] == "KXTEST-26"
    assert row["series_ticker"] == "KXTEST"
    assert row["yes_bid"] == 0.55
    assert row["yes_ask"] == 0.60
    assert row["last_price"] == 0.57
    assert row["volume"] == 1234.0  # volumes are not probabilities
    assert row["volume_24h"] == 12.0
    assert row["open_interest"] == 99.0
    assert row["liquidity"] == 5000.0
    assert row["close_time"] == "2026-12-31T00:00:00Z"
    assert row["status"] == "open"
    assert row["raw_json"] is None


def test_parse_dollar_string_market() -> None:
    """Current Kalshi responses use ``*_dollars`` and ``*_fp`` string fields."""
    raw = {
        "ticker": "KXTEST-DOLLAR",
        "event_ticker": "KXTEST-DOLLAR",
        "title": "Dollar encoded",
        "yes_bid_dollars": "0.5500",
        "yes_ask_dollars": "0.6000",
        "last_price_dollars": "0.5700",
        "volume_fp": "1234.00",
        "volume_24h_fp": "12.00",
        "open_interest_fp": "99.00",
        "close_time": "2026-12-31T00:00:00Z",
        "status": "active",
    }
    row = parse_market(raw, 1, 2)

    assert row["yes_bid"] == 0.55
    assert row["yes_ask"] == 0.60
    assert row["last_price"] == 0.57
    assert row["volume"] == 1234.0
    assert row["volume_24h"] == 12.0
    assert row["open_interest"] == 99.0
    assert row["series_ticker"] is None
    assert row["liquidity"] is None


def test_parse_missing_fields_stay_null() -> None:
    row = parse_market({"ticker": "KXTEST-EMPTY", "event_ticker": "E"}, 1, 2)

    for key in (
        "series_ticker",
        "title",
        "yes_bid",
        "yes_ask",
        "last_price",
        "volume",
        "volume_24h",
        "open_interest",
        "liquidity",
        "close_time",
        "status",
    ):
        assert row[key] is None, key
    assert row["raw_json"] is None


def test_probability_boundaries_and_bad_input() -> None:
    assert _probability(0) == 0.0
    assert _probability(1) == 1.0
    assert _probability(100) == 1.0
    assert _probability("0.5") == 0.5
    assert _probability("55") == 0.55
    assert _probability("") is None
    assert _probability("not-a-number") is None
    assert _probability(None) is None
    assert _probability(True) is None


def _row(ticker: str, last_price: float, volume: float = 10.0) -> dict:
    return {
        "ticker": ticker,
        "yes_bid": 0.4,
        "yes_ask": 0.6,
        "last_price": last_price,
        "volume": volume,
        "open_interest": 5.0,
    }


def test_market_signature_tracks_documented_tuple() -> None:
    row = _row("A", 0.5)
    assert market_signature(row) == (0.4, 0.6, 0.5)

    # Volume/OI churn must not trigger an emission; a BBO move must.
    assert market_signature(dict(row, open_interest=6.0)) == market_signature(row)
    assert market_signature(dict(row, yes_bid=0.41)) != market_signature(row)


def test_change_detector_emits_changes_and_heartbeat() -> None:
    detector = MarketChangeDetector(heartbeat_seconds=60.0)
    rows = [_row("A", 0.5), _row("B", 0.3)]

    first = detector.select(rows, now_mono=0.0)
    assert [row["ticker"] for row in first] == ["A", "B"]
    assert detector.tracked == 2

    assert detector.select(rows, now_mono=1.0) == []

    changed = [_row("A", 0.7), rows[1]]
    selected = detector.select(changed, now_mono=2.0)
    assert [row["ticker"] for row in selected] == ["A"]

    # Unchanged markets are still snapshotted once the heartbeat is due.
    heartbeat = detector.select(changed, now_mono=63.0)
    assert sorted(row["ticker"] for row in heartbeat) == ["A", "B"]

    # A market leaving the open set is dropped from memory.
    only_a = detector.select([changed[0]], now_mono=64.0)
    assert only_a == []  # changed rows == previous signature, no heartbeat yet
    assert detector.tracked == 1


# ----------------------------------------------------------- transport layer
from pathlib import Path

import requests

from polymarketbot.data.streams.kalshi import (
    DEFAULT_BATCH_SIZE,
    EdgePool,
    KalshiClient,
    MappingCache,
    TradeDeduper,
    backoff_delay,
    filter_trades,
    parse_markets_payload,
    parse_trades_payload,
    plan_ticker_batches,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class _FakeSession:
    """Records which edge served each call; the bad edge resets the connection."""

    def __init__(self, ip: str, behaviour) -> None:
        self.ip = ip
        self.behaviour = behaviour
        self.calls = 0
        self.closed = False
        self._kalshi_edge_ip = ip

    def get(self, url, params=None, timeout=None):  # noqa: ANN001
        self.calls += 1
        return self.behaviour(self.ip, url, params)

    def close(self) -> None:
        self.closed = True


def _client(behaviour, *, ips=("10.0.0.1", "10.0.0.2"), retry_total=2):
    edges = EdgePool(
        "api.example.test",
        resolver=lambda host: list(ips),
        probe=lambda host, ip: True,
        clock=lambda: 0.0,
    )
    sessions: list[_FakeSession] = []

    def factory(ip: str) -> _FakeSession:
        session = _FakeSession(ip, behaviour)
        sessions.append(session)
        return session

    client = KalshiClient(
        "https://api.example.test/trade-api/v2",
        edges=edges,
        session_factory=factory,
        sleep=lambda seconds: None,
        retry_total=retry_total,
    )
    return client, edges, sessions


def test_parse_markets_payload_keeps_cursor_and_timestamps() -> None:
    payload = {
        "markets": [{"ticker": "A"}, "not-a-dict", {"ticker": "B"}],
        "cursor": "abc123",
    }
    items, cursor = parse_markets_payload(payload, 11, 22)

    assert [item["raw"]["ticker"] for item in items] == ["A", "B"]
    assert all(item["recv_ts_ns"] == 11 and item["recv_mono_ns"] == 22 for item in items)
    assert cursor == "abc123"


def test_parse_markets_payload_rejects_wrong_shape() -> None:
    for bad in ({}, {"markets": "nope"}, {"markets": None}):
        try:
            parse_markets_payload(bad, 1, 2)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_parse_trades_payload_reads_dollar_and_fp_fields() -> None:
    payload = {
        "trades": [
            {
                "trade_id": "t-1",
                "ticker": "KXXRP15M-26NOV040100-00",
                "created_time": "2026-11-04T05:57:00.237574Z",
                "yes_price_dollars": "0.7500",
                "no_price_dollars": "0.2500",
                "count_fp": "76.00",
                "taker_side": "no",
                "taker_book_side": "ask",
                "is_block_trade": False,
            }
        ],
        "cursor": "next",
    }
    rows, cursor = parse_trades_payload(payload, 7, 8)

    assert cursor == "next"
    row = rows[0]
    assert row["trade_id"] == "t-1"
    assert row["yes_price"] == 0.75
    assert row["no_price"] == 0.25
    assert row["count"] == 76.0
    assert row["taker_side"] == "no"
    assert row["recv_ts_ns"] == 7


def test_plan_ticker_batches_dedupes_and_chunks() -> None:
    batches = plan_ticker_batches(["A", "B", "A", "", "C", "D", "E"], batch_size=2)
    assert batches == [["A", "B"], ["C", "D"], ["E"]]
    assert plan_ticker_batches([], batch_size=10) == []
    assert len(plan_ticker_batches([f"T{i}" for i in range(250)])) == 3
    assert len(plan_ticker_batches([f"T{i}" for i in range(100)], DEFAULT_BATCH_SIZE)) == 1


def test_backoff_delay_is_jittered_capped_and_monotone() -> None:
    low = [backoff_delay(attempt, base=0.5, cap=8.0, rng=lambda: 0.0) for attempt in range(1, 7)]
    high = [backoff_delay(attempt, base=0.5, cap=8.0, rng=lambda: 1.0) for attempt in range(1, 7)]

    assert low[0] == 0.25 and high[0] == 0.5
    assert all(a <= b + 1e-9 for a, b in zip(low, low[1:]))
    assert all(a <= b + 1e-9 for a, b in zip(high, high[1:]))
    assert max(high) <= 8.0

    # Two clients retrying at "the same time" do not sleep the same amount.
    assert backoff_delay(4, rng=lambda: 0.1) != backoff_delay(4, rng=lambda: 0.9)


def test_edge_pool_skips_resetting_edge_until_ttl() -> None:
    now = [0.0]
    probed: list[str] = []

    def probe(host: str, ip: str) -> bool:
        probed.append(ip)
        return ip != "10.0.0.1"  # first edge resets the TLS handshake

    edges = EdgePool(
        "api.example.test",
        resolver=lambda host: ["10.0.0.1", "10.0.0.2"],
        probe=probe,
        clock=lambda: now[0],
    )

    assert edges.first_reachable() == "10.0.0.2"
    assert probed == ["10.0.0.1", "10.0.0.2"]
    assert edges.healthy() == ["10.0.0.2"]  # 10.0.0.1 excluded for the TTL

    now[0] = 10.0
    assert edges.healthy() == ["10.0.0.2"]  # good edge still first

    now[0] = 400.0  # exclusion expired
    assert set(edges.healthy()) == {"10.0.0.1", "10.0.0.2"}


def test_edge_pool_forgets_exclusions_when_all_edges_are_bad() -> None:
    edges = EdgePool(
        "api.example.test",
        resolver=lambda host: ["10.0.0.1"],
        probe=lambda host, ip: False,
        clock=lambda: 0.0,
    )
    edges.mark_bad("10.0.0.1")
    assert edges.healthy() == ["10.0.0.1"]  # never go dark on stale blame
    assert edges.first_reachable() is None


def test_client_rotates_off_an_edge_that_resets() -> None:
    def behaviour(ip: str, url: str, params):
        if ip == "10.0.0.1":
            raise requests.ConnectionError(
                "('Connection aborted.', ConnectionResetError(10054, ...))"
            )
        return _FakeResponse(200, {"markets": [{"ticker": "OK"}]})

    client, edges, sessions = _client(behaviour)
    payload = client.get_json("/markets", {"tickers": "A"})

    assert payload["markets"] == [{"ticker": "OK"}]
    assert client.stats.resets == 1
    assert client._edge == "10.0.0.2"
    assert "10.0.0.1" in edges.bad
    assert sessions[0].closed is True

    # The next call reuses the working edge instead of re-probing.
    client.get_json("/markets", {"tickers": "A"})
    assert sessions[-1].ip == "10.0.0.2"
    assert len(sessions) == 2


def test_client_gives_up_after_retry_budget() -> None:
    def behaviour(ip: str, url: str, params):
        raise requests.ConnectionError("reset")

    client, _, sessions = _client(behaviour, retry_total=2)
    try:
        client.get_json("/markets", {})
    except requests.ConnectionError:
        pass
    else:
        raise AssertionError("expected the client to raise")
    assert client.stats.resets >= 3
    assert len(sessions) == 3


def test_client_backs_off_on_http_5xx_without_blaming_the_edge() -> None:
    def behaviour(ip: str, url: str, params):
        return _FakeResponse(503, {})

    client, edges, _ = _client(behaviour, retry_total=1)
    try:
        client.get_json("/markets", {})
    except requests.HTTPError:
        pass
    else:
        raise AssertionError("expected HTTPError")
    assert edges.bad == {}
    assert client.stats.retries >= 1


def test_mapping_cache_reloads_only_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "mapping.parquet"
    path.write_text("first", encoding="utf-8")
    loads: list[str] = []
    now = [0.0]

    def loader(target):
        loads.append(str(target))
        return tuple(path.read_text(encoding="utf-8").split())

    cache = MappingCache(path, refresh_seconds=10.0, loader=loader, clock=lambda: now[0])
    assert cache.get() == ("first",)
    assert cache.get() == ("first",)
    assert loads == [str(path)]

    now[0] = 11.0
    path.write_text("first second", encoding="utf-8")
    assert cache.get() == ("first", "second")

    # A missing artifact degrades to "no tickers" instead of raising.
    path.unlink()
    now[0] = 22.0
    assert cache.get() == ()


def test_trade_deduper_evicts_oldest_ids() -> None:
    deduper = TradeDeduper(capacity=3)
    rows = [{"trade_id": "a"}, {"trade_id": "b"}, {"trade_id": "a"}, {"trade_id": "c"}]
    assert [row["trade_id"] for row in deduper.accept(rows)] == ["a", "b", "c"]
    assert deduper.accept([{"trade_id": "a"}, {"trade_id": "d"}]) == [{"trade_id": "d"}]
    # "a" was evicted once capacity (3) was exceeded, so it is accepted again.
    assert len(deduper) == 3
    assert [row["trade_id"] for row in deduper.accept([{"trade_id": "a"}])] == ["a"]


def test_filter_trades_keeps_only_mapped_tickers() -> None:
    rows = [
        {"trade_id": "1", "ticker": "KXBTC15M-A"},
        {"trade_id": "2", "ticker": "KXNBA-B"},
        {"trade_id": "3", "ticker": "KXBTC15M-A"},
    ]
    assert [row["trade_id"] for row in filter_trades(rows, ["KXBTC15M-A"])] == ["1", "3"]
    # An empty mapping means "no filter", never "drop everything".
    assert filter_trades(rows, []) == rows

def test_poll_trades_walks_cursor_until_min_ts():
    """A fixed cadence drops prints; the cursor catch-up keeps the feed contiguous."""
    pages = {
        None: {
            "trades": [
                {"trade_id": "t3", "ticker": "A", "created_time": "2026-11-06T00:00:09Z", "count_fp": "1"},
                {"trade_id": "t2", "ticker": "A", "created_time": "2026-11-06T00:00:07Z", "count_fp": "1"},
            ],
            "cursor": "c2",
        },
        "c2": {
            "trades": [
                {"trade_id": "t1", "ticker": "A", "created_time": "2026-11-06T00:00:02Z", "count_fp": "1"},
            ],
            "cursor": "",
        },
    }
    calls = []

    class Fake:
        def get_json(self, path, params=None):
            calls.append(dict(params or {}))
            return pages[params.get("cursor")]

    min_ts = int(_iso_to_epoch("2026-11-06T00:00:01Z"))
    rows = poll_trades(Fake(), limit=1000, min_ts=min_ts, max_pages=5)
    assert [r["trade_id"] for r in rows] == ["t3", "t2", "t1"]
    assert calls[0]["min_ts"] == min_ts
    assert calls[1]["cursor"] == "c2"


def test_poll_trades_stops_when_page_reaches_min_ts():
    class Fake:
        def __init__(self):
            self.n = 0

        def get_json(self, path, params=None):
            self.n += 1
            return {"trades": [{"trade_id": f"t{self.n}", "created_time": "2026-11-06T00:00:01Z"}], "cursor": "next"}

    fake = Fake()
    rows = poll_trades(fake, min_ts=int(_iso_to_epoch("2026-11-06T00:00:01Z")), max_pages=5)
    assert len(rows) == 1 and fake.n == 1


def test_iso_to_epoch_parses_kalshi_timestamps():
    assert _iso_to_epoch("2026-11-06T00:00:02.123456Z") is not None
    assert _iso_to_epoch(None) is None
    assert _iso_to_epoch("not-a-time") is None
