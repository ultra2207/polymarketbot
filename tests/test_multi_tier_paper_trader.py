"""Unit and integration tests for Multi-Tier Paper Trading Orchestrator."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.rewards.scanner import reset_scanner_telemetry, get_scanner_telemetry
from polymarketbot.simulator.feed import LivePolymarketFeed, OrderBookSnapshot
from scripts.run_multi_tier_paper_trader import (
    TierContext,
    resolve_starter_market,
    run_multi_tier_async,
)



def test_starter_market_resolution():
    """Verify resolve_starter_market queries candidate pools for smallest bankroll."""
    mock_feed = MagicMock()
    mock_candidates = [
        {
            "condition_id": "0x_multi_test",
            "question": "Will Multi-Tier Test Pass?",
            "daily_pool": 250.0,
            "max_spread": 2.0,
            "min_size": 20.0,
            "mid_yes": 0.50,
            "daily_roi_pct": 5.0,
            "yes_token": "TOK_Y_1",
            "no_token": "TOK_N_1",
        }
    ]

    with patch("scripts.run_multi_tier_paper_trader.find_optimal_lp_markets", return_value=mock_candidates):
        market = resolve_starter_market(feed=mock_feed, lowest_bankroll=70.0)
        assert market["condition_id"] == "0x_multi_test"
        assert market["daily_reward"] == 250.0
        assert market["max_spread"] == 2.0
        assert market["token_yes"] == "TOK_Y_1"


def test_multi_tier_quote_sizing_and_isolation(tmp_path):
    """Verify each tier engine receives properly scaled quote size and isolated DB/ledger."""
    from polymarketbot.simulator.engine import NativePaperTradingEngine
    from polymarketbot.simulator.optimizer import derive_quote_size

    bankrolls = [70.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0]
    engines = []

    for b in bankrolls:
        q_sz = derive_quote_size(initial_cash=b, mid_price=0.50, min_size=20.0)
        db_p = tmp_path / f"test_{int(b)}.db"
        ledg_p = tmp_path / f"test_ledger_{int(b)}.json"

        eng = NativePaperTradingEngine(
            condition_id="0xTEST_CID",
            asset_id_yes="TOK_Y",
            asset_id_no="TOK_N",
            initial_cash=b,
            quote_size=q_sz,
            spread_offset_cents=0.50,
            daily_reward_rate=100.0,
            rewards_max_spread_cents=2.0,
            rewards_min_size=20.0,
            db_path=db_p,
            ledger_path=ledg_p,
        )
        engines.append((b, q_sz, eng))

    # Verify scaling progression: larger bankrolls have strictly larger quote sizes
    sizes = [sz for _, sz, _ in engines]
    assert sizes[0] >= 20.0  # $70 >= 20 min_size
    assert sizes[1] > sizes[0]  # $500 > $70
    assert sizes[2] > sizes[1]  # $1,000 > $500
    assert sizes[3] > sizes[2]  # $2,000 > $1,000
    assert sizes[4] > sizes[3]  # $5,000 > $2,000
    assert sizes[5] > sizes[4]  # $10,000 > $5,000

    # Ingest book into all engines
    for _, _, eng in engines:
        eng.update_orderbook_snapshot("YES", bids=[{"price": 0.49, "size": 5000.0}], asks=[{"price": 0.51, "size": 5000.0}])
        eng.evaluate_quotes()
        assert len(eng.active_quotes) > 0
        eng.close()


def test_shared_scanner_caching_across_tiers():
    """Verify that multiple bankroll tiers querying the scanner reuse the 5s TTL cache."""
    from polymarketbot.rewards.scanner import find_optimal_lp_markets

    reset_scanner_telemetry()
    mock_payload = {
        "data": [
            {
                "condition_id": "0x_shared_pool",
                "question": "Shared Pool Test",
                "rewards_config": [{"rate_per_day": "300.0"}],
                "rewards_max_spread": 4.5,
                "rewards_min_size": 20.0,
                "end_date_iso": "2026-12-31T00:00:00Z",
                "tokens": [
                    {"token_id": "tok_y", "outcome": "YES", "price": "0.55"},
                    {"token_id": "tok_n", "outcome": "NO", "price": "0.45"},
                ],
            }
        ]
    }

    with patch("requests.Session.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_payload
        mock_get.return_value = mock_resp

        with patch("polymarketbot.rewards.scanner.get_real_l2_score") as mock_l2:
            mock_l2.return_value = (300.0, 500.0, 0.55)

            # Tier 1 ($70) triggers initial scan
            res1 = find_optimal_lp_markets(bankroll_usd=70.0)
            telemetry = get_scanner_telemetry()
            assert telemetry.total_scans == 1
            assert telemetry.cache_hits == 0

            # Tier 2 ($500), Tier 3 ($1,000), Tier 4 ($2,000) evaluate candidate
            # In our runner, we query with min_bankroll so all tiers hit the exact same cache_key
            res2 = find_optimal_lp_markets(bankroll_usd=70.0)
            res3 = find_optimal_lp_markets(bankroll_usd=70.0)
            res4 = find_optimal_lp_markets(bankroll_usd=70.0)

            # Verified: 3 cache hits, zero additional network calls
            assert telemetry.total_scans == 4
            assert telemetry.cache_hits == 3
            assert telemetry.cache_misses == 1


def test_multi_tier_portfolio_diversification_scaling():
    """Verify MILP optimizer automatically diversifies higher bankroll tiers across multiple markets."""
    from polymarketbot.rewards.allocator import LiquidityRewardsAllocator

    # Synthetic pool of 15 candidate markets with diminishing returns
    candidates = []
    for i in range(15):
        candidates.append({
            "condition_id": f"0x_pool_{i}",
            "question": f"Candidate Market Pool {i}",
            "daily_pool": 100.0 + i * 20.0,
            "competing_score": 500.0 + i * 100.0,
            "max_spread": 2.5,
            "min_size": 20.0,
            "mid_yes": 0.50,
            "yes_token": f"TOK_Y_{i}",
            "no_token": f"TOK_N_{i}",
            "raw_market": {
                "tokens": [
                    {"token_id": f"TOK_Y_{i}", "outcome": "YES"},
                    {"token_id": f"TOK_N_{i}", "outcome": "NO"},
                ]
            },
        })

    allocator = LiquidityRewardsAllocator(reserve_pct=0.25)

    # 1. $70 Tier: Usable is calculated endogenously (~$56.00). Concentrates into 1 dominant market
    res_70 = allocator.optimize_allocation_milp(candidates, bankroll_usd=70.0)
    assert len(res_70.allocations) == 1
    assert res_70.total_allocated <= 70.0
    assert res_70.cash_reserve > 0.0

    # 2. $500 Tier: Slices capital across multiple elbow points
    res_500 = allocator.optimize_allocation_milp(candidates, bankroll_usd=500.0)
    assert len(res_500.allocations) >= 4
    assert res_500.total_allocated <= 500.0

    # 3. $1,000 Tier: Funds more markets
    res_1000 = allocator.optimize_allocation_milp(candidates, bankroll_usd=1000.0)
    assert len(res_1000.allocations) >= len(res_500.allocations)
    assert res_1000.total_daily_payout > res_500.total_daily_payout
    assert res_1000.total_allocated <= 1000.0

    # 4. $10,000 Tier: Diversifies across 8+ markets with endogenous reserve
    res_10000 = allocator.optimize_allocation_milp(candidates, bankroll_usd=10000.0)
    assert len(res_10000.allocations) >= 8
    assert res_10000.total_daily_payout > res_1000.total_daily_payout
    assert res_10000.total_allocated <= 10000.0


def test_tier_context_aggregation_and_flush(tmp_path):
    """Verify TierContext correctly aggregates equity, cash, rewards, and writes atomic master ledger."""
    from polymarketbot.simulator.engine import NativePaperTradingEngine
    from scripts.run_multi_tier_paper_trader import MarketInstance, TierContext

    db1 = tmp_path / "t_mkt1.db"
    ledg1 = tmp_path / "t_mkt1.json"
    eng1 = NativePaperTradingEngine(
        condition_id="0xCID_1",
        asset_id_yes="TOK_Y1",
        asset_id_no="TOK_N1",
        initial_cash=50.0,
        quote_size=20.0,
        spread_offset_cents=1.0,
        daily_reward_rate=50.0,
        rewards_max_spread_cents=2.0,
        rewards_min_size=20.0,
        db_path=db1,
        ledger_path=ledg1,
    )

    db2 = tmp_path / "t_mkt2.db"
    ledg2 = tmp_path / "t_mkt2.json"
    eng2 = NativePaperTradingEngine(
        condition_id="0xCID_2",
        asset_id_yes="TOK_Y2",
        asset_id_no="TOK_N2",
        initial_cash=100.0,
        quote_size=30.0,
        spread_offset_cents=1.0,
        daily_reward_rate=100.0,
        rewards_max_spread_cents=2.0,
        rewards_min_size=20.0,
        db_path=db2,
        ledger_path=ledg2,
    )

    m1 = MarketInstance(
        condition_id="0xCID_1",
        question="Market 1",
        token_yes="TOK_Y1",
        token_no="TOK_N1",
        allocated_capital=50.0,
        quote_size=20.0,
        spread_offset_cents=1.0,
        daily_reward_rate=50.0,
        max_spread=2.0,
        min_size=20.0,
        competing_score=500.0,
        engine=eng1,
        db_path=db1,
        ledger_path=ledg1,
        market_entry_time=100.0,
    )

    m2 = MarketInstance(
        condition_id="0xCID_2",
        question="Market 2",
        token_yes="TOK_Y2",
        token_no="TOK_N2",
        allocated_capital=100.0,
        quote_size=30.0,
        spread_offset_cents=1.0,
        daily_reward_rate=100.0,
        max_spread=2.0,
        min_size=20.0,
        competing_score=800.0,
        engine=eng2,
        db_path=db2,
        ledger_path=ledg2,
        market_entry_time=100.0,
    )

    master_ledg = tmp_path / "master_ledger_200.json"
    tier = TierContext(
        tier_name="$200",
        target_bankroll=200.0,
        initial_cash=200.0,
        reserve_cash=50.0,
        markets={"0xCID_1": m1, "0xCID_2": m2},
        master_ledger_path=master_ledg,
        migration_state={},
    )

    # Verify initial aggregation: reserve (50) + m1 (50) + m2 (100) = 200
    assert tier.get_total_cash() == 200.0
    assert tier.get_total_equity() == 200.0

    # Credit rewards
    eng1.credit_reward(1.50, condition_id="0xCID_1", note="Epoch 1 reward")
    eng2.credit_reward(3.00, condition_id="0xCID_2", note="Epoch 1 reward")

    assert tier.get_total_rewards() == 4.50
    assert tier.get_total_cash() == 204.50

    # Flush master ledger
    tier.flush_master_ledger()
    assert master_ledg.exists()

    import json
    with open(master_ledg, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["tier_name"] == "$200"
    assert data["portfolio"]["cash"] == 204.50
    assert data["active_markets_count"] == 2

    # Clean close
    eng1.close()
    eng2.close()

