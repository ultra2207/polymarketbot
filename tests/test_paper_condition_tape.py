"""Per-condition tape: a quoted token's own prints, not the global firehose."""

from __future__ import annotations

import time

import pytest

from polymarketbot.paper import venue
from polymarketbot.paper.venue import condition_tape


@pytest.fixture(autouse=True)
def _clear_tape_cache():
    venue._TAPE_CACHE.clear()
    venue._TAPE_COOLDOWN.clear()
    yield
    venue._TAPE_CACHE.clear()
    venue._TAPE_COOLDOWN.clear()


class _Response:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.status_code = 200

    def json(self):
        return {"data": self._rows}


class _Session:
    def __init__(self, rows_by_condition) -> None:
        self.rows_by_condition = rows_by_condition
        self.params: list[dict] = []

    def get(self, url: str, params=None, timeout=None) -> _Response:
        self.params.append(params)
        return _Response(self.rows_by_condition.get(params["condition"], []))


def _row(token: str, side: str, price: float, size: float, ts: float) -> dict:
    return {"token_id": token, "side": side, "price": price, "size": size, "timestamp": ts}


def test_fetches_each_condition_and_keeps_its_own_prints() -> None:
    now = time.time()
    http = _Session(
        {
            "c1": [_row("t1", "sell", 0.41, 10.0, now - 1)],
            "c2": [_row("t2", "BUY", 0.58, 4.0, now - 2)],
        }
    )
    prints = condition_tape(http, ["c1", "c2", "c1"])
    assert {p.condition_id for p in prints} == {"c1", "c2"}
    assert {p.token_id for p in prints} == {"t1", "t2"}
    assert all(p.side in ("BUY", "SELL") for p in prints)
    assert [p["condition"] for p in http.params] == ["c1", "c2"]


def test_prints_outside_the_window_are_dropped() -> None:
    now = time.time()
    http = _Session(
        {"c1": [_row("t1", "SELL", 0.4, 1.0, now - 600), _row("t1", "SELL", 0.4, 2.0, now - 5)]}
    )
    prints = condition_tape(http, ["c1"], window_s=120.0)
    assert [p.size for p in prints] == [2.0]


def test_empty_input_makes_no_requests() -> None:
    http = _Session({})
    assert condition_tape(http, ["", None]) == []
    assert http.params == []
