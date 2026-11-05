"""Rebalance-ledger tests: crossing costs are recorded and reported.

The simulator must persist one ``rebalances`` row per inventory-crossing action
(deskew taker execution, shutdown liquidation) and ``report_market_yield`` must
turn those rows into a numeric ``net_yield``. Everything here runs offline
against a temp SQLite database.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.rewards.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
)
from polymarketbot.simulator.engine import NativePaperTradingEngine
from scripts.report_market_yield import build_report

CONDITION_ID = "0xREBALANCE_LEDGER_TEST"
TS = 1_767_312_000.0  # 2026-01-02T00:00:00Z
GAS = 0.015


@pytest.fixture
def engine(tmp_path):
    eng = NativePaperTradingEngine(
        condition_id=CONDITION_ID,
        initial_cash=1_000.0,
        quote_size=20.0,
        auto_merge_threshold=1_000.0,  # keep the passive auto-merge out of the way
        institutional_mode=False,
        gas_fee_merge=GAS,
        db_path=tmp_path / "ledger.db",
        ledger_path=tmp_path / "ledger.json",
    )
    try:
        yield eng
    finally:
        eng.close()


def _fetch(eng, sql: str, params: tuple = ()):
    cur = eng._conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def _prime_books(eng) -> None:
    """YES and NO both quoted 0.49 / 0.51, so each token's mid is exactly 0.50."""
    eng.update_orderbook_snapshot(
        "YES",
        bids=[{"price": 0.49, "size": 100.0}],
        asks=[{"price": 0.51, "size": 100.0}],
        timestamp=TS,
    )
    eng.update_orderbook_snapshot(
        "NO",
        bids=[{"price": 0.49, "size": 100.0}],
        asks=[{"price": 0.51, "size": 100.0}],
        timestamp=TS,
    )


def _taker_decision(
    pathway: DeSkewPathway,
    token: str,
    side: str,
    price: float,
    size: float = 40.0,
    savings_cents: float = 0.0,
) -> DeSkewDecision:
    return DeSkewDecision(
        pathway=pathway,
        urgency_level="CRITICAL_PANIC",
        target_token=token,
        action_side=side,
        order_type="MARKET_TAKER",
        target_price=price,
        target_size=size,
        expected_cost_bps=20.0,
        synthetic_savings_cents=savings_cents,
        reason="test deskew",
    )


def test_report_prints_numeric_net_yield_for_seeded_db(engine):
    _prime_books(engine)
    cur = engine._conn.cursor()
    for index in range(2):
        cur.execute(
            """
            INSERT INTO fills (
                fill_id, timestamp, condition_id, token_side, side, price, size,
                liquidity_type, slippage_cents, queue_depleted, post_fill_drift,
                realized_pnl, cash_after, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"SEED{index}",
                TS + index * 60.0,
                CONDITION_ID,
                "YES",
                "SELL",
                0.51,
                10.0,
                "MAKER",
                0.0,
                0.0,
                -0.2,
                0.5,
                1_000.0,
                "seed fill",
            ),
        )
    engine._conn.commit()

    engine._record_rebalance(
        ts=TS + 30.0,
        crossed_cents=1.0,
        taker_fee_usd=0.0,
        gas_usd=GAS,
        inventory_before=10.0,
        inventory_after=0.0,
        reason="seed rebalance",
    )

    report = build_report(engine.db_path)
    line = next(ln for ln in report.splitlines() if ln.startswith(CONDITION_ID))
    assert "insufficient data" not in line
    assert "fills" in line

    match = re.search(r"([+-]\d+\.\d{4}) \((\d+) fills, (\d+) rebalances", line)
    assert match is not None
    assert int(match.group(2)) == 2
    assert int(match.group(3)) == 1
    # realized 2 x 0.5 minus (1c x 10 shares) minus gas minus 2 x 0.2 adverse markout
    assert float(match.group(1)) == pytest.approx(1.0 - 0.10 - GAS - 0.4, abs=1e-4)


def test_tier2_synthetic_merge_writes_rebalance_row(engine):
    _prime_books(engine)
    pos = engine.portfolio.get_position(CONDITION_ID)
    pos.shares_yes = 40.0
    pos.shares_no = 0.0
    pos.avg_cost_yes = 0.49

    engine._execute_tier2_taker_deskew(
        _taker_decision(DeSkewPathway.SYNTHETIC_MERGE, "NO", "BUY", 0.51, savings_cents=0.75),
        TS,
    )

    rows = _fetch(
        engine,
        """
        SELECT ts, condition_id, crossed_cents, taker_fee_usd, gas_usd,
               inventory_before, inventory_after, reason
        FROM rebalances
        """,
    )
    assert len(rows) == 1
    ts, cid, crossed_cents, taker_fee_usd, gas_usd, before, after, reason = rows[0]
    assert ts == pytest.approx(TS)
    assert cid == CONDITION_ID
    assert crossed_cents == pytest.approx(1.0)  # 0.51 vs NO mid 0.50
    assert taker_fee_usd == 0.0  # no per-market fee schedule on the simulator
    assert gas_usd == pytest.approx(GAS)  # merge executed in the same action
    assert before == pytest.approx(40.0)
    assert after == pytest.approx(0.0)  # 40 YES + 40 NO merged back to zero net
    assert "SYNTHETIC_MERGE" in reason
    assert "fee schedule missing" in reason

    events = _fetch(engine, "SELECT pathway, savings_cents FROM deskew_events")
    assert (DeSkewPathway.SYNTHETIC_MERGE.value, 0.75) in events


def test_tier2_direct_cross_records_zero_gas(engine):
    _prime_books(engine)
    pos = engine.portfolio.get_position(CONDITION_ID)
    pos.shares_yes = 40.0
    pos.shares_no = 0.0
    pos.avg_cost_yes = 0.49

    engine._execute_tier2_taker_deskew(
        _taker_decision(DeSkewPathway.DIRECT_CROSS, "YES", "SELL", 0.49, savings_cents=-0.5),
        TS,
    )

    rows = _fetch(
        engine,
        "SELECT crossed_cents, taker_fee_usd, gas_usd, inventory_before, inventory_after, reason "
        "FROM rebalances",
    )
    assert len(rows) == 1
    crossed_cents, taker_fee_usd, gas_usd, before, after, reason = rows[0]
    assert crossed_cents == pytest.approx(1.0)  # 0.49 vs YES mid 0.50
    assert taker_fee_usd == 0.0
    assert gas_usd == 0.0  # no merge in a direct cross
    assert before == pytest.approx(40.0)
    assert after == pytest.approx(0.0)
    assert "DIRECT_CROSS" in reason


def test_passive_fade_carries_signed_advantage(engine):
    optimizer = DynamicDeSkewOptimizer(
        dynamic_scaling=False,
        q_soft_threshold=15.0,
        q_hard_threshold=100.0,
    )
    decision = optimizer.evaluate_skew(
        shares_yes=40.0,
        shares_no=0.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
        rolling_ofi=0.0,
        micro_price_drift_cents=0.0,
    )
    assert decision.pathway == DeSkewPathway.PASSIVE_FADE
    # Resting the ask inside the spread earns cents over crossing at the bid now.
    assert decision.synthetic_savings_cents == pytest.approx(
        (decision.target_price - 0.49) * 100.0, abs=1e-3
    )
    assert decision.synthetic_savings_cents > 0.0

    engine._record_deskew_event(decision, TS)
    rows = _fetch(engine, "SELECT savings_cents FROM deskew_events")
    assert rows == [(pytest.approx(decision.synthetic_savings_cents),)]


def test_shutdown_liquidation_records_crossing_cost(engine):
    _prime_books(engine)
    pos = engine.portfolio.get_position(CONDITION_ID)
    pos.shares_yes = 12.0
    pos.shares_no = 0.0
    pos.avg_cost_yes = 0.49

    engine.liquidate_all_positions(timestamp=TS)

    rows = _fetch(
        engine,
        "SELECT crossed_cents, gas_usd, inventory_before, inventory_after, reason FROM rebalances",
    )
    assert len(rows) == 1
    crossed_cents, gas_usd, before, after, reason = rows[0]
    assert crossed_cents >= 0.0
    assert gas_usd == 0.0
    assert before == pytest.approx(12.0)
    assert after == pytest.approx(0.0)
    assert "Liquidation" in reason
