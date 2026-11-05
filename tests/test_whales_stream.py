"""Whale stream parsing, dedupe and poll loop tests. No network."""

from __future__ import annotations

import asyncio

import orjson
import pytest

from polymarketbot.data.context import HealthRegistry
from polymarketbot.data.streams.whales import (
    NAME,
    ActivityDeduper,
    HttpResult,
    parse_activity_rows,
    parse_leaderboard_wallets,
    parse_retry_after,
    poll_once,
    timestamp_to_ms,
)

RECV_TS = 1_700_000_000_000_000_000
RECV_MONO = 123_456_789


class _NullLog:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _FakeCtx:
    def __init__(self) -> None:
        self.health = HealthRegistry()
        self.emitted: list[dict] = []
        self.stop = asyncio.Event()

    def emit(self, table: str, row: dict) -> None:
        self.emitted.append({"table": table, **row})

    def log(self, _name: str):
        return _NullLog()


class _FakeHttp:
    def __init__(self, responses: dict[tuple[str, str], HttpResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, object]] = []

    def get_json(self, path: str, params=None) -> HttpResult:
        self.calls.append((path, params))
        if path == "/v2/leaderboard":
            key = (path, "")
        else:
            user = dict(params or []).get("user", "")
            key = (path, user)
        return self.responses[key]


def _activity(wallet: str, tx: str = "0xTX", ts: int = 1_782_752_879) -> HttpResult:
    return HttpResult(
        200,
        {
            "data": [
                {
                    "proxy_wallet": wallet,
                    "timestamp": ts,
                    "condition_id": "0xCOND",
                    "type": "TRADE",
                    "size": 25,
                    "usdc_size": 12.25,
                    "transaction_hash": tx,
                    "price": 0.49,
                    "token_id": "1075",
                    "side": "BUY",
                    "outcome": "Yes",
                }
            ],
            "pagination": {"limit": 100, "offset": 0, "has_more": False, "next_cursor": None},
        },
        f"https://data-api.polymarket.com/v2/activity?user={wallet}",
    )


def test_parse_leaderboard_extracts_user_id_and_aliases():
    payload = {
        "data": [
            {"rank": 1, "user_id": "0xAAA"},
            {"rank": 2, "wallet": "0xBBB"},
            {"rank": 3, "user_id": "0xAAA"},  # duplicate
            {"rank": 4, "user_id": None},
            "not-a-row",
        ]
    }
    assert parse_leaderboard_wallets(payload) == ["0xAAA", "0xBBB"]
    assert parse_leaderboard_wallets({"data": None}) == []
    assert parse_leaderboard_wallets(None) == []


def test_parse_activity_normalises_documented_v2_fields():
    payload = {
        "data": [
            {
                "proxy_wallet": "0xWALLET",
                "timestamp": 1_782_752_879,
                "condition_id": "0xCOND",
                "type": "TRADE",
                "size": "14.285715",
                "usdc_size": "13.285715",
                "transaction_hash": "0xTX",
                "price": "0.93",
                "token_id": "982553",
                "side": "SELL",
            }
        ]
    }
    rows = parse_activity_rows(payload, "0xWALLET", recv_ts_ns=RECV_TS, recv_mono_ns=RECV_MONO)
    assert len(rows) == 1
    row = rows[0]
    assert row["wallet"] == "0xWALLET"
    assert row["condition_id"] == "0xCOND"
    assert row["asset_id"] == "982553"
    assert row["side"] == "SELL"
    assert row["size"] == pytest.approx(14.285715)
    assert row["usdc_size"] == pytest.approx(13.285715)
    assert row["price"] == pytest.approx(0.93)
    assert row["activity_ts_ms"] == 1_782_752_879_000
    assert row["tx_hash"] == "0xTX"
    assert row["activity_type"] == "TRADE"
    assert row["recv_ts_ns"] == RECV_TS
    assert row["recv_mono_ns"] == RECV_MONO
    assert orjson.loads(row["raw_json"])["transaction_hash"] == "0xTX"


def test_activity_missing_fields_stay_null_and_miss_is_empty():
    rows = parse_activity_rows({"data": [{"proxy_wallet": "0xW"}]}, "0xW", recv_ts_ns=1, recv_mono_ns=2)
    assert rows[0]["asset_id"] is None
    assert rows[0]["size"] is None
    assert rows[0]["activity_ts_ms"] is None
    assert rows[0]["activity_type"] is None
    assert parse_activity_rows({"data": None}, "0xW") == []


def test_timestamp_to_ms_handles_seconds_millis_and_iso():
    assert timestamp_to_ms(1_782_752_879) == 1_782_752_879_000
    assert timestamp_to_ms(1_782_752_879_000) == 1_782_752_879_000
    assert timestamp_to_ms("1782752879") == 1_782_752_879_000
    assert timestamp_to_ms("2026-09-08T16:56:49Z") == 1_788_886_609_000
    assert timestamp_to_ms(None) is None
    assert timestamp_to_ms("not-a-time") is None


def test_dedupe_is_across_polls_and_bounded():
    dedupe = ActivityDeduper(max_keys=2)
    payload = {"data": [{"timestamp": 10, "transaction_hash": "0xA"}, {"timestamp": 10, "transaction_hash": "0xB"}]}
    first = parse_activity_rows(payload, "0xW", recv_ts_ns=1, recv_mono_ns=1)
    assert [dedupe.accept(row) for row in first] == [True, True]
    second = parse_activity_rows(payload, "0xW", recv_ts_ns=2, recv_mono_ns=2)
    assert [dedupe.accept(row) for row in second] == [False, False]
    # A third distinct key evicts the oldest, and the evicted key is accepted again.
    third = parse_activity_rows({"data": [{"timestamp": 11, "transaction_hash": "0xC"}]}, "0xW")
    assert dedupe.accept(third[0]) is True
    assert dedupe.accept(first[0]) is True


def test_parse_retry_after_seconds_date_and_default():
    assert parse_retry_after("3", 10.0) == 3.0
    assert parse_retry_after(None, 7.0) == 7.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 7.0) >= 0.0
    assert parse_retry_after("garbage", 7.0) == 7.0


def test_poll_once_emits_new_rows_and_skips_rejected_wallet():
    board = HttpResult(
        200,
        {"data": [{"user_id": "0xAAA"}, {"user_id": "0xBBB"}]},
        "https://data-api.polymarket.com/v2/leaderboard?time_period=day&limit=50",
    )
    rejected = HttpResult(
        422,
        None,
        "https://data-api.polymarket.com/v2/activity?user=0xBBB&type=TRADE&limit=100",
        "HTTP 422: unsupported param",
    )
    http = _FakeHttp({("/v2/leaderboard", ""): board, ("/v2/activity", "0xAAA"): _activity("0xAAA"), ("/v2/activity", "0xBBB"): rejected})
    ctx = _FakeCtx()
    dedupe = ActivityDeduper()

    wallets, rows, errors = asyncio.run(
        poll_once(ctx, http, dedupe, leaderboard_limit=50, activity_limit=100)
    )
    assert (wallets, rows, errors) == (2, 1, 1)
    assert [r["table"] for r in ctx.emitted] == ["whale_activity"]
    assert ctx.emitted[0]["wallet"] == "0xAAA"

    activity_calls = [params for path, params in http.calls if path == "/v2/activity"]
    assert ("type", "TRADE") in activity_calls[0]

    # Same rows next poll: dedupe removes them, the rejected wallet still counts.
    wallets, rows, errors = asyncio.run(
        poll_once(ctx, http, dedupe, leaderboard_limit=50, activity_limit=100)
    )
    assert (wallets, rows, errors) == (2, 0, 1)
    assert len(ctx.emitted) == 1

    metrics = {row["metric"]: row["value"] for row in ctx.health.snapshot()}
    assert metrics["wallets"] == 2
    assert metrics["rows"] == 1
    assert metrics["errors"] == 2
