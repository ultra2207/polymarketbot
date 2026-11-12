"""Tests for the Gamma and Data API clients (no network)."""

from __future__ import annotations

import pytest

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.fees import FeeSchedule
from polymarketbot.gamma import (
    GammaClient,
    GammaError,
    GammaHTTPError,
    GammaUnparseable,
)


# --------------------------------------------------------------------- gamma
class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = "", headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Serves canned responses in order and records how often it was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(responses, **kwargs) -> GammaClient:
    return GammaClient(
        min_interval_s=0.0, session=FakeSession(responses), **kwargs
    )


def test_gamma_4xx_is_not_retried():
    client = _client([FakeResponse(404, text="not found")])
    with pytest.raises(GammaHTTPError) as excinfo:
        client.get("/markets", {"limit": 1})
    assert excinfo.value.status == 404
    assert client.session.calls == 1  # a malformed request cannot be fixed by waiting


def test_gamma_429_exhaustion_names_the_status():
    client = _client(
        [FakeResponse(429, headers={"Retry-After": "0"})] * 2, max_retries=2
    )
    with pytest.raises(GammaError) as excinfo:
        client.get("/markets")
    assert "429" in str(excinfo.value)  # used to end in ": None"
    assert client.session.calls == 2


def test_gamma_500_then_success_retries(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("polymarketbot.gamma.time.sleep", slept.append)
    client = _client([FakeResponse(503), FakeResponse(200, {"markets": []})])
    assert client.get("/markets") == {"markets": []}
    assert client.session.calls == 2
    assert len(slept) == 1  # the retry rested before the second attempt


def test_gamma_unparseable_body_is_its_own_error(monkeypatch):
    monkeypatch.setattr("polymarketbot.gamma.time.sleep", lambda _s: None)
    client = _client([FakeResponse(200, ValueError("bad json"))] * 2, max_retries=2)
    with pytest.raises(GammaUnparseable):
        client.get("/markets")


def test_gamma_transport_error_is_retried_then_reported(monkeypatch):
    import requests

    monkeypatch.setattr("polymarketbot.gamma.time.sleep", lambda _s: None)
    client = _client(
        [requests.ConnectionError("reset"), requests.ConnectionError("reset")],
        max_retries=2,
    )
    with pytest.raises(GammaError) as excinfo:
        client.get("/markets")
    assert not isinstance(excinfo.value, GammaHTTPError)
    assert client.session.calls == 2


def test_parse_ids_handles_json_string():
    # clobTokenIds arrives as a JSON string, not a list
    assert GammaClient.parse_ids('["123", "456"]') == ["123", "456"]


def test_parse_ids_handles_list_and_none():
    assert GammaClient.parse_ids(["1", "2"]) == ["1", "2"]
    assert GammaClient.parse_ids(None) is None
    assert GammaClient.parse_ids("not json") is None


def test_fee_schedule_parses_gamma_shape():
    s = GammaClient.fee_schedule({
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.15}
    })
    assert isinstance(s, FeeSchedule)
    assert s.rate == 0.05 and s.rebate_rate == 0.15
    assert s.source == "gamma"


def test_fee_schedule_absent_is_none():
    assert GammaClient.fee_schedule({}) is None


def test_tick_size_precedence():
    assert GammaClient.tick_size({"orderPriceMinTickSize": 0.001}) == 0.001
    assert GammaClient.tick_size({"orderMinTickSize": 0.01}) == 0.01
    assert GammaClient.tick_size({"tickSize": "0.001"}) == 0.001
    assert GammaClient.tick_size({}) is None


def test_min_order_size():
    assert GammaClient.min_order_size({"orderMinSize": 5}) == 5.0
    assert GammaClient.min_order_size({"minimumOrderSize": "5"}) == 5.0
    assert GammaClient.min_order_size({}) is None


def test_tag_labels():
    m = {"tags": [{"label": "Sports"}, {"label": None}, {"slug": "x"}]}
    assert GammaClient.tag_labels(m) == ["Sports"]


# ------------------------------------------------------------------ data api
class FakeDataAPI(DataAPIClient):
    """Captures params and serves canned pages."""

    def __init__(self, pages):
        super().__init__(min_interval_s=0.0)
        self.pages = list(pages)
        self.calls: list[list[tuple]] = []

    def get(self, path, params=None):  # type: ignore[override]
        self.calls.append(list(params or []))
        return self.pages.pop(0)


def test_paginate_follows_next_cursor():
    client = FakeDataAPI([
        {"data": [{"i": 1}], "pagination": {"next_cursor": "abc"}},
        {"data": [{"i": 2}], "pagination": {"next_cursor": None}},
    ])
    rows = list(client.paginate("/v2/trades", {"condition": "0xabc"}, page_size=1))
    assert rows == [{"i": 1}, {"i": 2}]
    assert ("cursor", "abc") in client.calls[1]
    assert ("limit", "1") in client.calls[0]


def test_paginate_stops_at_max_pages():
    client = FakeDataAPI([
        {"data": [{"i": 1}], "pagination": {"next_cursor": "a"}},
        {"data": [{"i": 2}], "pagination": {"next_cursor": "b"}},
        {"data": [{"i": 3}], "pagination": {"next_cursor": "c"}},
    ])
    rows = list(client.paginate("/v2/trades", [], max_pages=2))
    assert rows == [{"i": 1}, {"i": 2}]
    assert len(client.calls) == 2


def test_trades_builds_repeated_condition_params():
    client = FakeDataAPI([{"data": [], "pagination": {}}])
    client.trades(condition=["0xa", "0xb"], user="0xu", full_history=True)
    params = client.calls[0]
    assert ("condition", "0xa") in params and ("condition", "0xb") in params
    assert ("user", "0xu") in params and ("full_history", "true") in params


def test_tokens_requires_exactly_one_selector():
    client = FakeDataAPI([])
    with pytest.raises(ValueError):
        client.tokens()
    with pytest.raises(ValueError):
        client.tokens(condition="0xc", token_id="1")


def test_activity_builds_filters():
    client = FakeDataAPI([{"data": [], "pagination": {}}])
    client.activity(user="0xu", types=["TRADE", "REDEEM"], start=1, end=2,
                    sort_direction="ASC")
    params = client.calls[0]
    assert ("type", "TRADE") in params and ("type", "REDEEM") in params
    assert ("user", "0xu") in params
    assert ("start", "1") in params and ("end", "2") in params
    assert ("sort_direction", "ASC") in params


def test_activity_excludes_deposits_by_default():
    client = FakeDataAPI([{"data": [], "pagination": {}}])
    client.activity(user="0xu")
    assert ("exclude_deposits_withdrawals", "false") not in client.calls[0]

    client = FakeDataAPI([{"data": [], "pagination": {}}])
    client.activity(user="0xu", exclude_deposits_withdrawals=False)
    assert ("exclude_deposits_withdrawals", "false") in client.calls[0]


def test_user_pnl_points_extracts_points():
    client = FakeDataAPI([{"data": {"points": [{"t": 1}, {"t": 2}]}}])
    assert client.user_pnl_points("0xu") == [{"t": 1}, {"t": 2}]


def test_user_stats_returns_object():
    client = FakeDataAPI([{"data": {"all_time_pnl": {"maker_rebate": 1.0}}}])
    assert client.user_stats("0xu")["all_time_pnl"]["maker_rebate"] == 1.0
