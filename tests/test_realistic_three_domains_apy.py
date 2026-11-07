"""Unit tests for the Realistic APY & Anti-Delusion Three Domains Simulation."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
import numpy as np

from scripts.simulate_realistic_three_domains_apy import (
    SimulatedTrade,
    calculate_risk_adjusted_metrics,
    run_realistic_three_domains_simulation,
)


def test_calculate_risk_adjusted_metrics():
    """Verify metric calculations for Sharpe, Sortino, Drawdown, and APY."""
    # Synthetic upward trend with single dip
    equity_series = [1000.0, 1010.0, 1020.0, 1005.0, 1030.0, 1040.0]
    trades = [
        SimulatedTrade(
            domain="Test",
            timestamp_ms=1000.0,
            condition_id="cid1",
            entry_price=0.50,
            shares=20.0,
            cost_basis=10.0,
            proceeds=12.0,
            net_profit=2.0,
            fees_paid=0.01,
            gas_paid=0.002,
            payout=1.0,
            is_win=True,
            description="Test win",
        ),
        SimulatedTrade(
            domain="Test",
            timestamp_ms=2000.0,
            condition_id="cid2",
            entry_price=0.50,
            shares=20.0,
            cost_basis=10.0,
            proceeds=8.0,
            net_profit=-2.0,
            fees_paid=0.01,
            gas_paid=0.002,
            payout=0.0,
            is_win=False,
            description="Test loss",
        ),
    ]

    duration_days = 2.0
    metrics = calculate_risk_adjusted_metrics(
        domain_name="Test Domain",
        equity_series=equity_series,
        initial_capital=1000.0,
        duration_days=duration_days,
        trades=trades,
        steps_per_year=17520.0,
        capacity_utilization_annual=0.40,
    )

    assert metrics.initial_capital_usd == 1000.0
    assert metrics.final_equity_usd == 1040.0
    assert metrics.net_pnl_usd == 40.0
    assert metrics.total_return_pct == 4.0
    assert metrics.daily_return_pct > 0.0
    assert metrics.max_drawdown_usd == pytest.approx(15.0, rel=1e-2)  # 1020 -> 1005
    assert metrics.max_drawdown_pct == pytest.approx(1.47, rel=1e-2)
    assert metrics.total_trades_count == 2
    assert metrics.win_rate_pct == 50.0


def test_simulation_execution():
    """Verify that full historical simulation runs quickly and outputs expected structure."""
    res = run_realistic_three_domains_simulation()
    assert "metadata" in res
    assert "delusion_inventory_and_fixes" in res
    assert "performance_metrics" in res
    assert "equity_trajectories_hourly" in res

    metrics = res["performance_metrics"]
    assert "domain_1_hft_maker" in metrics
    assert "domain_2_settlement_allocator" in metrics
    assert "domain_3_negrisk_arbitrage" in metrics
    assert "blended_portfolio" in metrics

    blend = metrics["blended_portfolio"]
    assert blend["initial_capital_usd"] == 3000.0
    assert blend["final_equity_usd"] > 3000.0
    assert blend["annualized_sharpe"] > 0.0
