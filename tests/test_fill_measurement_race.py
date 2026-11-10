"""The measured tape tables must never be observed half-loaded.

``_fill_table`` / ``_markout_table`` are called from the scanner's thread pool.
They used to set their tried flag before assigning the table, so a thread
entering during the load window skipped the load and returned ``None`` — which
the caller reads as "fill side unmeasured" and prices with ``stay_fraction=1.0``
and a fill yield of ``0.0``, i.e. a quote that looks free of adverse selection.

Measured before the fix: 6 of the first 24 markets of a cold scan priced on a
missing measurement.
"""

from __future__ import annotations

import threading
import time

import pytest

from polymarketbot.strategy.reward import fill_measurement as fm
from polymarketbot.strategy.reward import measurement


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(fm, "_QUOTE_FILL_HORIZON", None)
    monkeypatch.setattr(fm, "_QUOTE_FILL_HORIZON_TRIED", False)
    monkeypatch.setattr(fm, "_MAKER_MARKOUT", None)
    monkeypatch.setattr(fm, "_MAKER_MARKOUT_TRIED", False)


def _hammer(call, threads: int = 16) -> list:
    out: list = []
    lock = threading.Lock()

    def worker() -> None:
        result = call()
        with lock:
            out.append(result)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
    return out


def test_concurrent_fill_table_readers_never_see_none(monkeypatch):
    sentinel = object()

    def slow_load():
        time.sleep(0.05)
        return sentinel

    monkeypatch.setattr(measurement, "load_quote_fill_horizon", slow_load)
    results = _hammer(fm._fill_table)
    assert len(results) == 16
    assert all(r is sentinel for r in results), (
        f"{sum(r is None for r in results)} of {len(results)} readers saw a "
        "half-loaded fill table"
    )


def test_concurrent_markout_readers_never_see_none(monkeypatch):
    sentinel = object()

    def slow_load():
        time.sleep(0.05)
        return sentinel

    monkeypatch.setattr(measurement, "load_maker_markout", slow_load)
    results = _hammer(fm._markout_table)
    assert len(results) == 16
    assert all(r is sentinel for r in results)


def test_loader_runs_once_per_process(monkeypatch):
    calls: list[int] = []

    def counting():
        calls.append(1)
        time.sleep(0.01)
        return object()

    monkeypatch.setattr(measurement, "load_quote_fill_horizon", counting)
    _hammer(fm._fill_table, threads=8)
    assert len(calls) == 1
