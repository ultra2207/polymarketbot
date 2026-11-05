"""Book-snapshot stream tests (no network).

The fixture reproduces the measured ``/markets/orderbooks`` shape: dollar
strings for prices, contract-count strings for sizes, bids only on both sides.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pyarrow as pa
import requests

from polymarketbot.data.schema import KALSHI_BOOKS
from polymarketbot.data.streams.kalshi import plan_ticker_batches
from polymarketbot.data.streams.kalshi_books import (
    BookChangeDetector,
    orderbook_path,
    parse_orderbooks_payload,
    poll_books,
)

# Two books: one with ladders on both sides, one the venue returned empty.
FIXTURE = {
    "orderbooks": [
        {
            "ticker": "HOUSEAZ6-26-D",
            "orderbook_fp": {
                "yes_dollars": [
                    ["0.45", "2000.00"],
                    ["0.53", "100.00"],
                    ["0.52", "250.00"],
                    ["0.51", "400.00"],
                    ["0.50", "1000.00"],
                    ["0.40", "5000.00"],
                ],
                "no_dollars": [["0.44", "80.00"], ["0.43", "60.00"]],
            },
        },
        {
            "ticker": "KXCALIEUTENANTGOV-26NOV03-FMA",
            "orderbook_fp": {"yes_dollars": [], "no_dollars": []},
        },
    ]
}

TS = 1_760_000_000_000_000_000
MONO = 123_456_789


def _rows() -> list[dict]:
    rows, _ = parse_orderbooks_payload(FIXTURE, TS, MONO)
    return rows


# ------------------------------------------------------------------ parsing
def test_parse_two_books_with_derived_asks() -> None:
    rows, empty = parse_orderbooks_payload(FIXTURE, TS, MONO)

    assert [row["ticker"] for row in rows] == ["HOUSEAZ6-26-D", "KXCALIEUTENANTGOV-26NOV03-FMA"]
    assert empty == 1
    assert all(row["recv_ts_ns"] == TS and row["recv_mono_ns"] == MONO for row in rows)

    row = rows[0]
    assert row["yes_best_bid"] == 0.53
    assert row["no_best_bid"] == 0.44
    # Asks come from the opposite side, not from the venue.
    assert row["yes_best_ask"] == 0.56
    assert row["no_best_ask"] == 0.47
    assert row["mid"] == 0.545
    assert row["spread"] == 0.03
    assert row["yes_bid_size"] == 100.0
    assert row["no_bid_size"] == 80.0


def test_parse_sorts_levels_best_first_and_packs_json() -> None:
    row = _rows()[0]

    yes = json.loads(row["yes_bids"])
    assert yes[0] == [0.53, 100.0]
    assert [price for price, _ in yes] == sorted((p for p, _ in yes), reverse=True)
    assert [0.45, 2000.0] in yes

    no = json.loads(row["no_bids"])
    assert [price for price, _ in no] == [0.44, 0.43]

    # raw_json is the venue entry verbatim, so the tape can be re-parsed.
    assert json.loads(row["raw_json"]) == FIXTURE["orderbooks"][0]


def test_depth_bands_cumulate_within_cents_of_best() -> None:
    row = _rows()[0]

    # yes best 0.53: 1c >= 0.52 -> 350, 2c >= 0.51 -> 750,
    # 5c >= 0.48 -> 1750, 10c >= 0.43 -> 3750 (0.40 falls outside).
    assert row["yes_bid_size_1c"] == 350.0
    assert row["yes_bid_size_2c"] == 750.0
    assert row["yes_bid_size_5c"] == 1750.0
    assert row["yes_bid_size_10c"] == 3750.0
    # no best 0.44: both levels sit inside 1c.
    assert row["no_bid_size_1c"] == 140.0
    assert row["no_bid_size_10c"] == 140.0


def test_empty_book_keeps_nulls_and_zero_bands() -> None:
    row = _rows()[1]

    assert row["yes_best_bid"] is None
    assert row["yes_best_ask"] is None
    assert row["no_best_ask"] is None
    assert row["mid"] is None and row["spread"] is None
    assert row["yes_bids"] == "[]" and row["no_bids"] == "[]"
    assert row["yes_bid_size"] is None
    assert row["yes_bid_size_10c"] is None


def test_rows_fit_the_tape_schema() -> None:
    table = pa.Table.from_pylist(_rows(), schema=KALSHI_BOOKS)
    assert table.num_rows == 2
    assert table.column("ticker").to_pylist() == ["HOUSEAZ6-26-D", "KXCALIEUTENANTGOV-26NOV03-FMA"]
    assert table.column("yes_best_bid").to_pylist() == [0.53, None]


def test_parse_rejects_wrong_payload_shape() -> None:
    for bad in ({}, {"orderbooks": {}}, {"books": []}):
        try:
            parse_orderbooks_payload(bad, TS, MONO)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


# ------------------------------------------------------------------ batching
def test_hundred_tickers_is_one_request() -> None:
    tickers = [f"KXTEST-{a:02d}-{b:02d}" for a in range(10) for b in range(10)]
    assert len(set(tickers)) == 100

    batches = plan_ticker_batches(tickers, 100)
    assert len(batches) == 1 and len(batches[0]) == 100

    path = orderbook_path(batches[0])
    query = parse_qs(urlsplit(path).query, keep_blank_values=True)
    assert path.count("tickers=") == 100
    assert query["tickers"] == tickers


def test_batching_splits_past_the_cap() -> None:
    tickers = [f"KXTEST-{i:03d}" for i in range(233)]
    batches = plan_ticker_batches(tickers, 100)
    assert [len(batch) for batch in batches] == [100, 100, 33]
    assert sum(batches, []) == tickers


# ---------------------------------------------------------- change detection
def test_identical_snapshot_suppressed_until_heartbeat() -> None:
    row = _rows()[0]
    detector = BookChangeDetector(heartbeat_seconds=60.0)

    assert detector.select([row], 1000.0) == [row]  # first sighting
    assert detector.select([dict(row)], 1005.0) == []  # identical, 5 s later
    assert detector.select([dict(row)], 1059.9) == []  # still inside the interval
    assert detector.select([dict(row)], 1060.0) == [row]  # heartbeat anchor
    assert detector.select([dict(row)], 1061.0) == []
    assert detector.tracked() == 1


def test_changed_ladder_is_emitted_even_between_heartbeats() -> None:
    row = _rows()[0]
    detector = BookChangeDetector(heartbeat_seconds=60.0)
    detector.select([row], 1000.0)

    moved = dict(row, raw_json="{}")
    moved["yes_bids"] = json.dumps([[0.54, 10.0]])
    assert detector.select([moved], 1001.0) == [moved]
    # ... and the new ladder is the baseline from then on.
    assert detector.select([dict(moved)], 1002.0) == []


def test_detector_forgets_tickers_that_left_the_mapping() -> None:
    detector = BookChangeDetector(heartbeat_seconds=60.0)
    detector.select(_rows(), 1000.0)
    assert detector.tracked() == 2

    detector.forget(["HOUSEAZ6-26-D"])
    assert detector.tracked() == 1


# ----------------------------------------------------------------- transport
class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Serves a payload per repeated-tickers request; records every URL."""

    def __init__(self, fail_first: Exception | None = None) -> None:
        self.paths: list[str] = []
        self.fail_first = fail_first

    def get_json(self, path: str) -> dict:
        self.paths.append(path)
        tickers = parse_qs(urlsplit(path).query)["tickers"]
        assert len(tickers) == len(set(tickers))
        if self.fail_first is not None and len(self.paths) == 1:
            raise self.fail_first
        return {
            "orderbooks": [
                {"ticker": ticker, "orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
                for ticker in tickers
            ]
        }


def test_poll_books_uses_repeated_tickers_params() -> None:
    client = _FakeClient()
    outcomes = poll_books(client, [["A", "B"], ["C"]])

    assert [outcome.ok for outcome in outcomes] == [True, True]
    assert client.paths[0] == "/markets/orderbooks?tickers=A&tickers=B"
    assert client.paths[1] == "/markets/orderbooks?tickers=C"
    assert [row["ticker"] for row in outcomes[0].rows] == ["A", "B"]
    assert outcomes[0].empty_books == 2


def test_failed_batch_does_not_skip_its_siblings() -> None:
    client = _FakeClient(fail_first=requests.ConnectionError("reset"))
    outcomes = poll_books(client, [["A", "B"], ["C"]])

    assert outcomes[0].ok is False
    assert "ConnectionError" in (outcomes[0].error or "")
    assert outcomes[0].status is None
    assert outcomes[1].ok is True
    assert [row["ticker"] for row in outcomes[1].rows] == ["C"]
    # Both batches were attempted: isolation, not a short-circuit.
    assert len(client.paths) == 2


def test_429_is_reported_on_the_outcome() -> None:
    error = requests.HTTPError("HTTP 429", response=_FakeResponse(429))
    client = _FakeClient(fail_first=error)
    outcomes = poll_books(client, [["A"], ["B"]])

    assert outcomes[0].status == 429
    assert outcomes[0].rows == []
    assert outcomes[1].ok is True
