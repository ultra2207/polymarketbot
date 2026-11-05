"""Unit tests for the Polygon on-chain stream parsers and planners.

No network: every parser is fed the JSON a node returns, and the planner is
checked against its cap.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from polymarketbot.data.streams import polygon


def _log_object() -> dict:
    return {
        "address": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "topics": [
            "0x63bf4d16b2a61e2b1d4b0c8d1e4c5e0a0b8b3c6f2ad2e1e3f7d1c0b9a8e7d6c5",
            "0x0000000000000000000000000000000000000000000000000000000000000001",
            "0x0000000000000000000000000000000000000000000000000000000000000002",
        ],
        "data": "0x0000000000000000000000000000000000000000000000000de0b6b3a7640000",
        "blockNumber": "0x5c378e5",
        "transactionHash": "0x" + "ab" * 32,
        "transactionIndex": "0x2",
        "blockHash": "0x" + "cd" * 32,
        "logIndex": "0x7",
        "removed": False,
    }


def test_hex_int_parses_rpc_quantities() -> None:
    assert polygon.hex_int("0x5c378e5") == 96_696_549
    assert polygon.hex_int("0x0") == 0
    assert polygon.hex_int("42") == 42
    assert polygon.hex_int(12) == 12
    assert polygon.hex_int(None) is None
    assert polygon.hex_int("0x") is None
    assert polygon.hex_int("not hex") is None
    assert polygon.hex_int(True) is None
    assert polygon.hex_int("", default=-1) == -1


def test_parse_log_maps_every_field() -> None:
    row = polygon.parse_log(_log_object(), recv_ts_ns=1_700_000_000_000_000_000, recv_mono_ns=99)
    assert row is not None
    assert row["recv_ts_ns"] == 1_700_000_000_000_000_000
    assert row["recv_mono_ns"] == 99
    assert row["block_number"] == 96_696_549
    assert row["tx_index"] == 2
    assert row["log_index"] == 7
    assert row["address"] == "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
    assert row["topic0"].startswith("0x63bf4d16")
    assert row["topic1"].endswith("01") and row["topic2"].endswith("02")
    assert row["topic3"] is None
    assert row["data"].startswith("0x0000000000000000")
    assert row["block_hash"] == "0x" + "cd" * 32
    assert row["tx_hash"] == "0x" + "ab" * 32
    assert row["removed"] is False


def test_parse_log_keeps_removed_flag() -> None:
    raw = _log_object()
    raw["removed"] = True
    row = polygon.parse_log(raw)
    assert row is not None
    assert row["removed"] is True


def test_parse_log_rejects_unplaceable_log() -> None:
    raw = _log_object()
    raw.pop("blockNumber")
    assert polygon.parse_log(raw) is None
    assert polygon.parse_log("not an object") is None


def test_parse_log_tolerates_missing_topics_and_indices() -> None:
    row = polygon.parse_log({"blockNumber": "0x10"})
    assert row is not None
    assert row["block_number"] == 16
    assert row["topic0"] is None and row["topic3"] is None
    assert row["tx_index"] is None and row["log_index"] is None
    assert row["address"] is None
    assert row["removed"] is False


def _header() -> dict:
    return {
        "number": "0x5c3791f",
        "hash": "0x" + "ef" * 32,
        "parentHash": "0x" + "01" * 32,
        "baseFeePerGas": "0x39a9ec2eb5",
        "gasUsed": "0x28049a4",
        "gasLimit": "0x9896800",
        "timestamp": "0x6aea755f",
        "transactions": ["0x" + "aa" * 32, "0x" + "bb" * 32, "0x" + "cc" * 32],
    }


def test_parse_block_header_gas_and_time() -> None:
    row = polygon.parse_block_header(_header())
    assert row is not None
    assert row["block_number"] == 96_696_607
    assert row["block_hash"] == "0x" + "ef" * 32
    # 0x39a9ec2eb5 wei = 247.664 gwei
    assert row["base_fee_gwei"] == pytest.approx(int("39a9ec2eb5", 16) / 1e9)
    assert row["base_fee_gwei"] == pytest.approx(247.66, abs=0.01)
    assert row["gas_used"] == int("28049a4", 16)
    assert row["gas_limit"] == int("9896800", 16)
    assert row["timestamp_ms"] == int("6aea755f", 16) * 1000
    assert row["tx_count"] == 3


def test_parse_block_header_without_body_leaves_tx_count_null() -> None:
    raw = _header()
    raw.pop("transactions")
    row = polygon.parse_block_header(raw)
    assert row is not None
    assert row["tx_count"] is None
    # an explicit count wins over an unparsed body
    assert polygon.parse_block_header(raw, tx_count=57)["tx_count"] == 57


def test_parse_block_header_handles_missing_base_fee() -> None:
    raw = _header()
    raw.pop("baseFeePerGas")
    row = polygon.parse_block_header(raw)
    assert row is not None
    assert row["base_fee_gwei"] is None
    # a number is never coerced to zero
    assert polygon.parse_block_header({"hash": "0x00"}) is None


def test_plan_windows_never_exceeds_span() -> None:
    assert polygon.plan_windows(100, 100, 10) == [(100, 100)]
    assert polygon.plan_windows(100, 109, 10) == [(100, 109)]
    assert polygon.plan_windows(100, 110, 10) == [(100, 109), (110, 110)]
    windows = polygon.plan_windows(96_000_000, 96_000_027, 10)
    assert windows == [(96_000_000, 96_000_009), (96_000_010, 96_000_019), (96_000_020, 96_000_027)]
    for low, high in windows:
        assert low <= high
        assert high - low + 1 <= 10


def test_plan_windows_handles_empty_and_degenerate_input() -> None:
    assert polygon.plan_windows(10, 9, 10) == []
    assert polygon.plan_windows(10, 10, 0) == [(10, 10)]
    # windows are contiguous and cover the range exactly once
    covered = [b for low, high in polygon.plan_windows(5, 33, 7) for b in range(low, high + 1)]
    assert covered == list(range(5, 34))


def test_wss_url_derivation_and_masking() -> None:
    assert polygon.wss_from_http("https://polygon-mainnet.g.alchemy.com/v2/abc") == (
        "wss://polygon-mainnet.g.alchemy.com/v2/abc"
    )
    assert polygon.wss_from_http("http://127.0.0.1:8545") == "ws://127.0.0.1:8545"
    assert polygon.wss_from_http("wss://example.invalid/ws") == "wss://example.invalid/ws"
    with pytest.raises(ValueError):
        polygon.wss_from_http("example.invalid")
    masked = polygon.mask_endpoint("wss://polygon-mainnet.g.alchemy.com/v2/supersecret")
    assert "supersecret" not in masked


def test_settings_reads_section_and_defaults() -> None:
    settings = polygon.PolygonSettings.from_section(
        {
            "rpc_url": "https://polygon.drpc.org/",
            "addresses": {
                "ctf": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
                "uma_oo": "0xCB1822859cEF82Cd2Eb4E6276C7916e692995130",
                "uma_adapter": "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74",
            },
        },
        fallbacks=["https://polygon-bor-rpc.publicnode.com/", "https://polygon.drpc.org"],
    )
    assert settings.rpc_url == "https://polygon.drpc.org"
    assert settings.ws_url == "wss://polygon.drpc.org"
    assert settings.log_block_span == 10
    assert settings.poll_seconds == 10.0
    assert settings.gas_poll_seconds == 10.0
    assert len(settings.addresses) == 3
    # the primary is first and is not repeated as a fallback
    assert settings.http_urls == [
        "https://polygon.drpc.org",
        "https://polygon-bor-rpc.publicnode.com",
    ]


def test_settings_rejects_missing_addresses() -> None:
    with pytest.raises(ValueError):
        polygon.PolygonSettings.from_section(
            {"rpc_url": "https://polygon.drpc.org", "addresses": {}}, fallbacks=[]
        )
    with pytest.raises(ValueError):
        polygon.PolygonSettings.from_section(
            {
                "rpc_url": "https://polygon.drpc.org",
                "addresses": {"ctf": "not-an-address"},
            },
            fallbacks=[],
        )
    with pytest.raises(ValueError):
        polygon.PolygonSettings.from_section(
            {"rpc_url": "https://polygon.drpc.org", "addresses": {"ctf": "0x1234"}},
            fallbacks=[],
        )


def test_settings_explicit_ws_url_wins() -> None:
    settings = polygon.PolygonSettings.from_section(
        {
            "rpc_url": "https://polygon.drpc.org",
            "ws_url": "wss://example.invalid/ws/",
            "addresses": {"ctf": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"},
            "log_block_span": 5,
            "poll_seconds": 3,
            "gas_poll_seconds": 4,
        },
        fallbacks=[],
    )
    assert settings.ws_url == "wss://example.invalid/ws"
    assert settings.log_block_span == 5
    assert settings.poll_seconds == 3.0
    assert settings.gas_poll_seconds == 4.0


# --------------------------------------------------------------------------- #
# Provider rotation, cooldown and call reduction
# --------------------------------------------------------------------------- #
ADDRESSES = {
    "ctf": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "uma_oo": "0xCB1822859cEF82Cd2Eb4E6276C7916e692995130",
    "uma_adapter": "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74",
}
ALCHEMY = "https://polygon-mainnet.g.alchemy.com/v2/testkey0001"
DRPC = "https://polygon.drpc.org"
PUBLICNODE = "https://polygon-bor-rpc.publicnode.com"


class _Clock:
    """Monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Health:
    def __init__(self) -> None:
        self.counters: dict[str, float] = {}
        self.gauges: dict[str, float] = {}

    def incr(self, stream: str, metric: str, n: float = 1.0) -> None:
        self.counters[metric] = self.counters.get(metric, 0.0) + float(n)

    def gauge(self, stream: str, metric: str, value: float, labels=None) -> None:
        self.gauges[metric] = float(value)


class _Log:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass


class _Ctx:
    def __init__(self) -> None:
        self.health = _Health()
        self.rows: list[tuple[str, dict]] = []
        self._log = _Log()

    def log(self, name: str) -> _Log:
        return self._log

    def emit(self, table: str, row: dict) -> None:
        self.rows.append((table, row))


class _Response:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _Session:
    """Answers per URL: a list of statuses survives repeated calls."""

    def __init__(self, answers: dict[str, list[_Response]]) -> None:
        self.answers = answers
        self.urls: list[str] = []
        self.headers: dict[str, str] = {}

    def post(self, url: str, json=None, timeout=None) -> _Response:  # noqa: A002
        self.urls.append(url)
        queue = self.answers[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def close(self) -> None:
        pass


class _Rpc:
    """Counts calls so redundancy is measurable."""

    def __init__(self, head: int, logs: list[dict] | None = None) -> None:
        self.head = head
        self.logs = logs or []
        self.log_calls: list[tuple[int, int]] = []
        self.block_calls: list[int] = []

    def block_number(self) -> int:
        return self.head

    def get_logs(self, addresses, low: int, high: int) -> list[dict]:
        self.log_calls.append((low, high))
        return list(self.logs)

    def get_block(self, number: int) -> dict | None:
        self.block_calls.append(number)
        return {
            "number": hex(number),
            "hash": "0x" + "aa" * 32,
            "baseFeePerGas": hex(30 * 10**9),
            "gasUsed": hex(1),
            "gasLimit": hex(2),
            "timestamp": hex(1_700_000_000),
            "transactions": [],
        }


def _settings(**overrides) -> "polygon.PolygonSettings":
    section = {
        "rpc_url": ALCHEMY,
        "addresses": ADDRESSES,
        "poll_seconds": 10,
        "log_block_span": 10,
        "gas_poll_seconds": 10,
    }
    section.update(overrides)
    return polygon.PolygonSettings.from_section(
        section, fallbacks=[DRPC, PUBLICNODE, ALCHEMY]
    )


def test_provider_rotation_starts_at_the_primary_and_excludes_cooling() -> None:
    pool = polygon.provider_pool(_settings())
    assert [h.name for h in pool.health()] == ["alchemy", "drpc", "publicnode"]
    assert pool.ws_candidates()[0].wss == "wss://polygon-mainnet.g.alchemy.com/v2/testkey0001"

    cooled = pool.mark_rate_limited(ALCHEMY)
    assert 45.0 <= cooled <= 75.0  # 60 s +/- equal jitter
    assert [h.name for h in pool.ws_candidates()] == ["drpc", "publicnode"]
    assert [h.name for h in pool.available()] == ["drpc", "publicnode"]


def test_cooldown_escalates_then_caps_and_resets_on_success() -> None:
    clock = _Clock()
    pool = polygon.ProviderPool(
        [DRPC, PUBLICNODE], cooldown_base_s=60.0, cooldown_max_s=300.0,
        clock=clock, rng=lambda: 0.5,
    )
    assert pool.mark_rate_limited(DRPC) == pytest.approx(60.0)
    clock.advance(60.0)
    assert pool.mark_rate_limited(DRPC) == pytest.approx(120.0)
    clock.advance(120.0)
    assert pool.mark_rate_limited(DRPC) == pytest.approx(240.0)
    clock.advance(240.0)
    assert pool.mark_rate_limited(DRPC) == pytest.approx(300.0)  # capped

    pool.mark_ok(DRPC)
    assert pool.cooldown_remaining(DRPC) == pytest.approx(0.0)
    assert pool.mark_rate_limited(DRPC) == pytest.approx(60.0)  # ladder reset


def test_cooling_provider_is_skipped_until_its_cooldown_expires() -> None:
    clock = _Clock()
    pool = polygon.ProviderPool(
        [ALCHEMY, DRPC], cooldown_base_s=60.0, clock=clock, rng=lambda: 0.5
    )
    pool.mark_rate_limited(ALCHEMY)
    assert pool.next_available_in() == 0.0  # drpc is still available
    pool.mark_rate_limited(DRPC)
    assert pool.available() == []
    assert pool.next_available_in() == pytest.approx(60.0)
    clock.advance(61.0)
    assert [h.name for h in pool.available()] == ["alchemy", "drpc"]


def test_jittered_delay_grows_and_stays_within_the_cap() -> None:
    low = polygon.jittered_delay(3, base=1.0, cap=5.0, rng=lambda: 0.0)
    high = polygon.jittered_delay(3, base=1.0, cap=5.0, rng=lambda: 1.0)
    assert low == pytest.approx(2.5)
    assert high == pytest.approx(5.0)
    # cap holds however long the ladder runs
    assert polygon.jittered_delay(40, base=1.0, cap=5.0, rng=lambda: 1.0) <= 5.0
    # two peers drawing different numbers do not retry in lockstep
    assert polygon.jittered_delay(0, base=1.0, cap=30.0, rng=lambda: 0.2) != (
        polygon.jittered_delay(0, base=1.0, cap=30.0, rng=lambda: 0.8)
    )


def test_http_rpc_fails_over_on_429_and_cools_the_primary() -> None:
    session = _Session(
        {
            ALCHEMY: [_Response(429, text="rate limited")],
            DRPC: [_Response(200, {"jsonrpc": "2.0", "id": 1, "result": "0x5c378e5"})],
        }
    )
    pool = polygon.provider_pool(_settings())
    rpc = polygon.HttpRpc(pool)
    rpc.session = session

    assert rpc.block_number() == 96_696_549
    assert session.urls == [ALCHEMY, DRPC]
    assert pool.cooldown_remaining(ALCHEMY) > 0.0
    assert pool.cooldown_remaining(DRPC) == 0.0

    # The next call goes straight to the healthy provider, not the cooled one.
    assert rpc.block_number() == 96_696_549
    assert session.urls == [ALCHEMY, DRPC, DRPC]


def test_http_rpc_reports_every_provider_when_all_fail() -> None:
    session = _Session(
        {
            ALCHEMY: [_Response(429, text="rate limited")],
            DRPC: [_Response(503, text="unavailable")],
            PUBLICNODE: [_Response(503, text="unavailable")],
        }
    )
    rpc = polygon.HttpRpc(polygon.provider_pool(_settings()))
    rpc.session = session
    with pytest.raises(RuntimeError) as excinfo:
        rpc.block_number()
    assert "3 endpoint(s)" in str(excinfo.value)
    assert set(session.urls) == {ALCHEMY, DRPC, PUBLICNODE}


def test_gas_is_sampled_at_the_configured_cadence() -> None:
    settings = _settings(gas_poll_seconds=10)
    ctx, state, rpc = _Ctx(), polygon.StreamState(), _Rpc(head=1000)

    asyncio.run(polygon._sample_gas(ctx, settings, state, rpc, 1000))
    asyncio.run(polygon._sample_gas(ctx, settings, state, rpc, 1005))
    assert rpc.block_calls == [1000]  # inside the cadence: no call
    assert state.head_block == 1000

    state.gas_wall -= 11.0
    asyncio.run(polygon._sample_gas(ctx, settings, state, rpc, 1013))
    assert rpc.block_calls == [1000, 1013]
    assert state.head_block == 1013
    # Sampling skips blocks by design: counted, not warned about.
    assert ctx.health.counters["gas_blocks_skipped"] == 12
    assert not any("block gap" in w for w in ctx._log.warnings)


def test_catch_up_skips_the_log_window_the_socket_already_covered() -> None:
    settings = _settings()
    ctx, rpc = _Ctx(), _Rpc(head=500)
    state = polygon.StreamState(log_block=500, backfill_block=500)
    asyncio.run(polygon._catch_up(ctx, settings, state, rpc))
    assert rpc.log_calls == []  # contiguous cursor is at head: no wasted call
    assert rpc.block_calls == [500]


def test_catch_up_fetches_every_missed_window() -> None:
    settings = _settings(log_block_span=10)
    ctx, rpc = _Ctx(), _Rpc(head=525)
    state = polygon.StreamState(log_block=500, backfill_block=500)
    asyncio.run(polygon._catch_up(ctx, settings, state, rpc))
    # The old implementation floored at head-span; the gap is now fully walked.
    assert rpc.log_calls == [(501, 510), (511, 520), (521, 525)]
    assert state.backfill_block == 525
    assert not any("not backfilled" in w for w in ctx._log.warnings)


def test_supervise_rotates_to_the_next_provider_after_a_429(monkeypatch) -> None:
    attempts: list[str] = []

    async def fake_session(ctx, settings, state, rpc, ws_url):
        attempts.append(ws_url)
        if len(attempts) == 1:
            raise RuntimeError("server rejected WebSocket connection: HTTP 429")
        ctx.stop.set()

    monkeypatch.setattr(polygon, "_ws_session", fake_session)
    monkeypatch.setattr(polygon, "ROTATE_POLL_S", 0.0)
    monkeypatch.setattr(polygon, "_sleep", _instant_sleep)

    ctx = _Ctx()
    ctx.stop = asyncio.Event()
    settings = _settings()
    pool = polygon.provider_pool(settings)
    rpc = _Rpc(head=1)
    asyncio.run(polygon._supervise(ctx, settings, polygon.StreamState(), rpc, pool))

    assert attempts == [
        "wss://polygon-mainnet.g.alchemy.com/v2/testkey0001",
        "wss://polygon.drpc.org",
    ]
    assert pool.cooldown_remaining(ALCHEMY) > 0.0
    assert ctx.health.counters["rate_limited"] == 1
    assert ctx.health.counters["reconnects"] == 1


async def _instant_sleep(_ctx, _seconds: float) -> None:
    return None


def test_provider_metrics_expose_cooldown_and_counters() -> None:
    clock = _Clock()
    pool = polygon.ProviderPool(
        [ALCHEMY, DRPC], clock=clock, cooldown_base_s=60.0, rng=lambda: 0.5
    )
    pool.mark_rate_limited(ALCHEMY)
    pool.mark_ok(DRPC)
    metrics = pool.metrics()
    assert metrics["provider_alchemy_rate_limits"] == 1.0
    assert metrics["provider_alchemy_cooldown_s"] == pytest.approx(60.0)
    assert metrics["provider_drpc_cooldown_s"] == 0.0
    assert metrics["provider_drpc_errors"] == 0.0


# --------------------------------------------------------------------------- #
# Live probe (opt-in): one fallback answers after the primary is simulated 429
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    os.environ.get("POLYGON_LIVE_PROBE") != "1",
    reason="live network probe; set POLYGON_LIVE_PROBE=1 to run",
)
def test_live_fallback_serves_http_and_wss_after_a_simulated_primary_429() -> None:
    """Three requests total, rate-limit friendly: the 2026-11-04 failure, replayed."""
    pool = polygon.ProviderPool([ALCHEMY, DRPC, PUBLICNODE], cooldown_base_s=60.0)
    pool.mark_rate_limited(ALCHEMY)  # what Alchemy did to every handshake
    rpc = polygon.HttpRpc(pool, timeout_s=15.0)
    try:
        head = rpc.block_number()
        block = rpc.get_block(head)
    finally:
        rpc.close()

    assert isinstance(head, int) and head > 0
    assert block is not None and polygon.hex_int(block["number"]) == head
    assert pool.cooldown_remaining(ALCHEMY) > 0.0
    assert pool.get(DRPC).ok == 2, "the fallback, not the cooled primary, answered"

    async def subscribe_once() -> str:
        from websockets.asyncio.client import connect

        async with connect(pool.ws_candidates()[0].wss, open_timeout=15) as ws:
            await ws.send('{"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newHeads"]}')
            return await asyncio.wait_for(ws.recv(), timeout=20)

    first = asyncio.run(subscribe_once())
    assert "eth_subscription" in first or "result" in first
