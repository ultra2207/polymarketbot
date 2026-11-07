"""Tests for the 4-Strategy Live Paper Soak Runner and Preflight Verification."""

import sys
import time
from pathlib import Path

# Add project root to sys.path so scripts can be imported in tests
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.preflight_verification import PreflightAuditor
from scripts.run_live_paper_soak import LivePaperSoakDaemon, SoakTelemetryDb, StrategyMetrics


def test_soak_telemetry_db(tmp_path: Path) -> None:
    db_file = tmp_path / "test_soak.db"
    db = SoakTelemetryDb(db_file)

    # Record trade
    db.record_trade(
        strategy="LatencySniper",
        condition_id="cond_101",
        side="BUY",
        price=0.50,
        size=100.0,
        gross_pnl=2.50,
        fee=0.10,
        rebate=0.05,
        latency_us=45.0,
        details="Unit test trade",
    )

    # Record snapshot
    strats = {
        "LatencySniper": StrategyMetrics("LatencySniper", 1000.0, 1002.45, 1000.0, 1, 2.50, 0.10, 0.05, 2.45),
        "Domain2ARR": StrategyMetrics("Domain2ARR", 1000.0, 1000.0, 400.0),
        "CtfSplitter": StrategyMetrics("CtfSplitter", 1000.0, 1000.0, 250.0),
        "SettlementHarvester": StrategyMetrics("SettlementHarvester", 1000.0, 1000.0, 400.0),
    }
    db.record_snapshot(
        uptime_sec=60.0,
        total_equity=4002.45,
        total_net_pnl=2.45,
        strats=strats,
        clob_rtt_ms=8.5,
        binance_rtt_ms=12.2,
        trades_count=1,
    )

    # Verify tables contain records
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM soak_trades")
    assert cur.fetchone()[0] == 1

    cur.execute("SELECT count(*) FROM soak_snapshots")
    assert cur.fetchone()[0] == 1
    conn.close()


def test_live_paper_soak_daemon_lifecycle(tmp_path: Path) -> None:
    daemon = LivePaperSoakDaemon(capital_per_strategy=500.0, vip_tier="bronze", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "soak_test.db")

    assert daemon.capital_per_strategy == 500.0
    assert daemon.vip_tier == "bronze"
    assert len(daemon.metrics) == 4
    assert daemon.metrics["LatencySniper"].starting_cash == 500.0


def test_preflight_auditor_local_sanity() -> None:
    auditor = PreflightAuditor(test_order=False, test_ctf=False, strict=False)
    auditor.run_phase_1()
    auditor.run_phase_5()
    report = auditor.generate_report()

    assert report.passed_count >= 3
    assert len(report.results) > 0
