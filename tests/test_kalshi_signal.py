"""Kalshi basis monitor: parsing, staleness, direction, and orchestration.

No network: the monitor's HTTP client is injected and answers with a synthetic
``/markets/orderbooks`` payload.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from polymarketbot.paper_trading.kalshi_signal import (
    KalshiBasisMonitor,
    kalshi_yes_mid,
    logit,
)
from polymarketbot.paper_trading.orchestrator import (
    OrchestratorConfig,
    QuoteOrchestrator,
    QueueSnapshot,
)

FRONT = QueueSnapshot(0.0, 100.0, 0.0, 100.0)
BACK = QueueSnapshot(0.0, 100.0, 1.0, 900.0)
BID_BACK = QueueSnapshot(1.0, 900.0, 0.0, 100.0)


def _mapping(tmp_path: Path) -> Path:
    path = tmp_path / "mapping.parquet"
    rows = [
        {
            "kalshi_ticker": "K-NORMAL",
            "polymarket_condition_id": "c-normal",
            "direction": "normal",
        },
        {
            "kalshi_ticker": "K-INVERTED",
            "polymarket_condition_id": "c-inverted",
            "direction": "inverted",
        },
    ]
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


class FakeClient:
    """Returns a two-sided YES .70 / NO .20 book (YES mid .75) for every ticker."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, params or {}))
        tickers = list((params or {}).get("tickers", []))
        return {
            "orderbooks": [
                {
                    "ticker": ticker,
                    "orderbook_fp": {
                        "yes_dollars": [["0.70", "100"]],
                        "no_dollars": [["0.20", "100"]],
                    },
                }
                for ticker in tickers
            ]
        }


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _monitor(tmp_path: Path, *, clock: Clock | None = None) -> KalshiBasisMonitor:
    return KalshiBasisMonitor(
        _mapping(tmp_path),
        client=FakeClient(),
        clock=clock or Clock(),
        poll_seconds=5.0,
        stale_after_s=20.0,
    )


def test_parses_dollar_and_cent_books_and_rejects_one_sided():
    dollars = {
        "orderbook_fp": {
            "yes_dollars": [["0.55", "10"], ["0.60", "5"]],
            "no_dollars": [["0.30", "7"]],
        }
    }
    assert kalshi_yes_mid(dollars) == pytest.approx((0.60 + 0.70) / 2.0)

    cents = {"yes": [[55, 10]], "no": [[30, 7]]}
    assert kalshi_yes_mid(cents) == pytest.approx(0.625)

    assert kalshi_yes_mid({"orderbook_fp": {"yes_dollars": [["0.55", "10"]]}}) is None
    assert kalshi_yes_mid({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}) is None


def test_normal_basis_is_kalshi_minus_polymarket():
    # Kalshi .75 against Polymarket .50: Kalshi is above, basis is positive.
    assert logit(0.75) - logit(0.50) > 0.0


def test_monitor_basis_sign_and_inverted_direction(tmp_path: Path):
    monitor = _monitor(tmp_path)
    assert monitor.poll_once() == 2
    assert monitor.mapped_tickers() == ("K-INVERTED", "K-NORMAL")

    normal = monitor.basis("c-normal", 0.50)
    inverted = monitor.basis("c-inverted", 0.50)
    assert normal is not None and normal > 0.0
    assert inverted is not None and inverted < 0.0
    # Inverted uses 1 - Kalshi YES mid: .75 becomes .25.
    assert inverted == pytest.approx(logit(0.25) - logit(0.50))

    # A positive basis threatens the ask; the inverted mapping flips that to
    # the bid, which is the side the orchestrator must shrink.
    orch = QuoteOrchestrator(
        OrchestratorConfig(enabled=True, signal="kalshi", policy="queue_aware")
    )
    up = orch.decide(ofi=None, queue=BACK, kalshi_basis=normal)
    assert (up.bid.action, up.ask.action) == ("QUOTE", "SHRINK")
    down = orch.decide(ofi=None, queue=BID_BACK, kalshi_basis=inverted)
    assert (down.bid.action, down.ask.action) == ("SHRINK", "QUOTE")


def test_stale_snapshot_is_none_and_keeps_both_sides(tmp_path: Path):
    clock = Clock()
    monitor = _monitor(tmp_path, clock=clock)
    assert monitor.poll_once() == 2
    assert monitor.basis("c-normal", 0.50) is not None

    clock.now += 20.0
    assert monitor.basis("c-normal", 0.50) is not None  # boundary is fresh
    clock.now += 0.01
    assert monitor.basis("c-normal", 0.50) is None
    assert monitor.fresh_count() == 0

    orch = QuoteOrchestrator(OrchestratorConfig(enabled=True, signal="kalshi"))
    decision = orch.decide(ofi=None, queue=BACK, kalshi_basis=None)
    assert (decision.bid.action, decision.ask.action) == ("QUOTE", "QUOTE")
    assert decision.bid.size_scale == pytest.approx(1.0)


def test_kalshi_threshold_boundary():
    orch = QuoteOrchestrator(
        OrchestratorConfig(
            enabled=True,
            signal="kalshi",
            policy="queue_aware",
            kalshi_basis_threshold=2.0,
        )
    )
    assert orch.decide(None, BACK, kalshi_basis=2.0).ask.action == "QUOTE"
    assert orch.decide(None, BACK, kalshi_basis=-2.0).bid.action == "QUOTE"
    assert orch.decide(None, BACK, kalshi_basis=2.001).ask.action == "SHRINK"
    assert orch.decide(None, BID_BACK, kalshi_basis=-2.001).bid.action == "SHRINK"


def test_kalshi_signal_is_off_by_default():
    # Default threshold 0.0 and signal ofi: a kalshi basis changes nothing.
    decision = QuoteOrchestrator().decide(None, BACK, kalshi_basis=9.0)
    assert (decision.bid.action, decision.ask.action) == ("QUOTE", "QUOTE")

    off = QuoteOrchestrator(OrchestratorConfig(signal="kalshi"))
    kept = off.decide(None, BACK, kalshi_basis=9.0)
    assert (kept.bid.action, kept.ask.action) == ("QUOTE", "QUOTE")
