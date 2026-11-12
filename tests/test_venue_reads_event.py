"""Venue reads that the correlated-event caps and the heartbeat rely on.

``Market.event_id`` must come from whichever shape a Gamma/metadata row uses,
and ``BookFeed.last_ok_ts`` must stamp a *successful* ``/books`` call only:
a cached snapshot or a failed fetch is not a data-source heartbeat.
"""

from __future__ import annotations

import threading

import pytest

from polymarketbot.strategy.venue.reads import BookFeed, parse_market

_GAMMA_ROW = {
    "conditionId": "0xabc",
    "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["1", "2"]',
    "orderPriceMinTickSize": 0.01,
    "orderMinSize": 5,
}


def _row(**overrides):
    return {**_GAMMA_ROW, **overrides}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"event_id": "e-snake"}, "e-snake"),
        ({"eventId": "e-camel"}, "e-camel"),
        ({"events": [{"id": "e-list"}]}, "e-list"),
        # Gamma sometimes serialises the list as JSON text.
        ({"events": '[{"id": "e-json"}]'}, "e-json"),
        # An event object without an id is missing metadata, not an empty id.
        ({"events": [{"slug": "no-id"}]}, ""),
        ({}, ""),
    ],
)
def test_parse_market_reads_the_event_id_shape(overrides, expected):
    market = parse_market(_row(**overrides))
    assert market is not None
    assert market.event_id == expected


def test_snake_case_event_id_wins_over_the_events_list():
    market = parse_market(
        _row(event_id="flat", events=[{"id": "nested"}])
    )
    assert market is not None
    assert market.event_id == "flat"


class _Response:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Http:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self._lock = threading.Lock()

    def post(self, url, json=None, timeout=None):
        with self._lock:
            self.calls += 1
            return self._responses.pop(0)


def test_book_feed_stamps_only_a_successful_fetch():
    http = _Http(
        [
            _Response(500, None),
            _Response(200, [{"asset_id": "t1", "bids": [], "asks": []}]),
            _Response(500, None),
        ]
    )
    feed = BookFeed(http, ttl=0.0)
    assert feed.last_ok_ts is None

    # A non-2xx chunk is a failed call: no heartbeat to record.
    feed.books(["t1"], force=True)
    assert feed.last_ok_ts is None

    # A 2xx with a parsed body stamps the call.
    feed.books(["t1"], force=True)
    first = feed.last_ok_ts
    assert first is not None and first > 0.0

    # A later failure leaves the successful timestamp alone.
    feed.books(["t1"], force=True)
    assert feed.last_ok_ts == first


def test_book_feed_does_not_stamp_an_unparsable_2xx_body():
    http = _Http(
        [
            _Response(200, ValueError("not json")),
            _Response(200, [{"asset_id": "t1", "bids": [], "asks": []}]),
        ]
    )
    feed = BookFeed(http, ttl=0.0)
    feed.books(["t1"], force=True)
    assert feed.last_ok_ts is None
    feed.books(["t1"], force=True)
    assert feed.last_ok_ts is not None
