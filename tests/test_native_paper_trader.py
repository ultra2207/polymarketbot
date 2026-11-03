"""Unit and Integration Tests for Polymarket Native Paper Trading Simulation Engine."""

import os
import sys
import tempfile
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.rewards.deskew import DeSkewPathway, DynamicDeSkewOptimizer
from polymarketbot.rewards.queue_valuation import CapitalMigrationEngine
from polymarketbot.simulator.book import SyntheticOrderBook
from polymarketbot.simulator.engine import NativePaperTradingEngine
from polymarketbot.simulator.impact import MarketImpactEngine
from scripts.run_native_paper_trader import render_dashboard
from polymarketbot.simulator.portfolio import (
    MergeResult,
    PortfolioTransaction,
    Position,
    SimulatedPortfolio,
    SplitResult,
    TransactionType,
)


class TestSimulatedPortfolio:
    """Tests for SimulatedPortfolio CTF accounting, split, merge, and fills."""

    def test_initial_state(self):
        port = SimulatedPortfolio(initial_cash=100.0, gas_fee_merge=0.015)
        assert port.cash == 100.0
        assert port.initial_cash == 100.0
        assert len(port.transactions) == 1
        assert port.transactions[0].tx_type == TransactionType.INITIAL_DEPOSIT.value

    def test_ctf_split(self):
        port = SimulatedPortfolio(initial_cash=100.0)
        res = port.split("COND_123", 25.0)

        assert isinstance(res, SplitResult)
        assert res.cash_split == 25.0
        assert res.shares_yes_created == 25.0
        assert res.shares_no_created == 25.0
        assert port.cash == 75.0

        pos = port.get_position("COND_123")
        assert pos.shares_yes == 25.0
        assert pos.shares_no == 25.0
        assert pos.complete_sets == 25.0
        assert pos.net_exposure == 0.0
        assert pos.avg_cost_yes == 0.50
        assert pos.avg_cost_no == 0.50

    def test_ctf_merge(self):
        port = SimulatedPortfolio(initial_cash=100.0, gas_fee_merge=0.015)
        # Split $50 into 50 YES + 50 NO
        port.split("COND_123", 50.0)
        assert port.cash == 50.0

        # Merge 20 complete sets
        # Gross = $20.00, Gas = $0.015 -> Net = $19.985
        # Cost basis retired = 20 * (0.50 + 0.50) = $20.00
        # Realized gain = $19.985 - $20.00 = -$0.015 (gas fee)
        merge_res = port.merge("COND_123", 20.0)
        assert isinstance(merge_res, MergeResult)
        assert merge_res.complete_sets_merged == 20.0
        assert merge_res.usdc_gross == 20.0
        assert merge_res.gas_fee_deducted == 0.015
        assert merge_res.net_cash_unlocked == 19.985
        assert round(merge_res.realized_gain, 3) == -0.015

        assert round(port.cash, 3) == 69.985
        pos = port.get_position("COND_123")
        assert pos.shares_yes == 30.0
        assert pos.shares_no == 30.0
        assert pos.complete_sets == 30.0

    def test_process_fills_and_cost_basis(self):
        port = SimulatedPortfolio(initial_cash=100.0)

        # 1. Buy 20 YES @ $0.40 -> cost = $8.00
        tx1 = port.process_fill("COND_123", side="BUY", outcome="YES", price=0.40, size=20.0)
        assert port.cash == 92.0
        pos = port.get_position("COND_123")
        assert pos.shares_yes == 20.0
        assert pos.avg_cost_yes == 0.40

        # 2. Buy 20 more YES @ $0.60 -> cost = $12.00
        # New avg cost = (20*0.40 + 20*0.60)/40 = $0.50
        tx2 = port.process_fill("COND_123", side="BUY", outcome="YES", price=0.60, size=20.0)
        assert port.cash == 80.0
        assert pos.shares_yes == 40.0
        assert pos.avg_cost_yes == 0.50

        # 3. Sell 10 YES @ $0.70 -> proceeds = $7.00
        # Realized PnL = 10 * (0.70 - 0.50) = +$2.00
        tx3 = port.process_fill("COND_123", side="SELL", outcome="YES", price=0.70, size=10.0)
        assert port.cash == 87.0
        assert pos.shares_yes == 30.0
        assert pos.avg_cost_yes == 0.50
        assert pos.realized_pnl == 2.0
        assert port.total_realized_pnl == 2.0

    def test_mark_to_market_and_equity(self):
        port = SimulatedPortfolio(initial_cash=100.0)
        # Buy 50 YES @ $0.40 (cost $20, cash left $80)
        port.process_fill("COND_123", side="BUY", outcome="YES", price=0.40, size=50.0)

        # Marks: YES mark = $0.55, NO mark = $0.45
        marks = {"COND_123": (0.55, 0.45)}
        mkt_val = port.get_market_value(marks)
        assert mkt_val == 50.0 * 0.55  # $27.50

        unrealized = port.get_unrealized_pnl(marks)
        assert unrealized == 50.0 * (0.55 - 0.40)  # +$7.50

        equity = port.get_equity(marks)
        assert equity == 80.0 + 27.50  # $107.50

        total_pnl = port.get_total_pnl(marks)
        assert total_pnl == 7.50

    def test_credit_reward(self):
        port = SimulatedPortfolio(initial_cash=100.0)
        port.credit_reward(1.50, condition_id="COND_123", note="Epoch 1 reward")
        assert port.cash == 101.50
        assert port.total_rewards_earned == 1.50


class TestNativePaperTradingEngine:
    """Tests for NativePaperTradingEngine, Deskewing, Rewards, and Persistence."""

    @pytest.fixture
    def engine(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_trading.db"
            ledger_path = Path(tmpdir) / "test_ledger.json"

            eng = NativePaperTradingEngine(
                condition_id="0xTEST_COND_ID",
                asset_id_yes="TOKEN_YES",
                asset_id_no="TOKEN_NO",
                initial_cash=100.0,
                quote_size=20.0,
                spread_offset_cents=0.50,
                daily_reward_rate=28.80,  # 28.80 / 1440 = $0.02/epoch
                rewards_max_spread_cents=1.0,
                rewards_min_size=20.0,
                auto_merge_threshold=10.0,
                institutional_mode=False,
                db_path=db_path,
                ledger_path=ledger_path,
            )
            try:
                yield eng
            finally:
                eng.close()

    def test_orderbook_ingestion_and_neutral_quotes(self, engine):
        # Apply L2 snapshot: bids around 0.49, asks around 0.51
        bids = [{"price": 0.49, "size": 100.0}, {"price": 0.48, "size": 200.0}]
        asks = [{"price": 0.51, "size": 100.0}, {"price": 0.52, "size": 200.0}]

        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        # Quotes should be injected on both sides around mid = 0.50
        assert len(engine.active_quotes) == 2
        bid_q = engine.active_quotes.get("BUY_YES")
        ask_q = engine.active_quotes.get("SELL_YES")

        assert bid_q is not None
        assert ask_q is not None
        assert bid_q["price"] == 0.495
        assert ask_q["price"] == 0.505
        assert bid_q["size"] == 20.0
        assert ask_q["size"] == 20.0

    def test_trade_fill_execution_and_ctf_auto_merge(self, engine):
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        # Pre-seed with 15 NO shares via split or manual fill
        engine.portfolio.split(engine.condition_id, 15.0)
        pos_before = engine.portfolio.get_position(engine.condition_id)
        assert pos_before.shares_yes == 15.0
        assert pos_before.shares_no == 15.0

        # Simulate incoming market SELL trade at 0.49 (crossing into our bid at 0.495)
        fills = engine.process_public_trade(
            token_side="YES",
            price=0.49,
            size=10.0,
            side="SELL",
        )
        assert len(fills) == 1
        assert fills[0].fill_size == 10.0
        assert fills[0].is_maker is True

        # Now YES shares grew: 15 + 10 = 25. Complete sets = min(25, 15) = 15.
        # Since complete sets (15.0) >= auto_merge_threshold (10.0), CTF auto-merge triggers!
        pos_after = engine.portfolio.get_position(engine.condition_id)
        assert pos_after.shares_no == 0.0  # 15 merged
        assert pos_after.shares_yes == 10.0  # 25 - 15 = 10 left
        assert len(engine.merges_history) == 1
        assert engine.merges_history[0].complete_sets_merged == 15.0

    def test_rewards_epoch_scoring_and_floor(self, engine):
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        # Process epoch: Projected daily reward > $1.00 floor -> payout credited!
        res = engine.process_reward_epoch()
        assert res["epoch_index"] == 1
        assert res["user_score"] > 0
        assert res["floor_met"] is True
        assert res["epoch_payout_usd"] > 0
        assert engine.total_rewards_credited > 0

    def test_deskew_tier1_passive_fade(self, engine):
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        # Skew inventory: Hold 40 YES shares (q = +40 > q_soft 15.0)
        pos = engine.portfolio.get_position(engine.condition_id)
        pos.shares_yes = 40.0
        pos.shares_no = 0.0

        # Re-evaluate quotes
        engine.evaluate_quotes()
        assert engine.latest_deskew_decision is not None
        assert engine.latest_deskew_decision.pathway == DeSkewPathway.PASSIVE_FADE
        assert engine.latest_deskew_decision.target_token == "YES"
        assert engine.latest_deskew_decision.action_side == "SELL"

        # The BUY quote should be pulled, and the SELL quote should be active
        assert "SELL_YES" in engine.active_quotes

    def test_persistence_sqlite_and_json(self, engine):
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)
        engine.process_reward_epoch()
        engine.flush_ledger()

        # Verify SQLite has written snapshots
        cur = engine._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM portfolio_snapshots")
        assert cur.fetchone()[0] >= 1

        cur.execute("SELECT COUNT(*) FROM reward_epochs")
        assert cur.fetchone()[0] >= 1

        # Verify JSON ledger exists and is valid
        assert engine.ledger_path.exists()
        import json
        with open(engine.ledger_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert "portfolio" in data
            assert data["condition_id"] == engine.condition_id

    def test_institutional_dual_cash_mode(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            eng = NativePaperTradingEngine(
                condition_id="0xINSTITUTIONAL",
                initial_cash=150.0,
                quote_size=30.0,
                spread_offset_cents=0.50,
                institutional_mode=True,
                db_path=Path(tmp_dir) / "trading.db",
                ledger_path=Path(tmp_dir) / "ledger.json",
            )
            try:
                bids = [{"price": 0.74, "size": 100.0}]
                asks = [{"price": 0.75, "size": 100.0}]
                eng.update_orderbook_snapshot("YES", bids=bids, asks=asks)

                # In Jane Street mode: quotes are pure dual-cash bids (BUY YES + BUY NO)
                assert "BUY_YES" in eng.active_quotes
                assert "BUY_NO" in eng.active_quotes
                assert "SELL_YES" not in eng.active_quotes

                assert eng.active_quotes["BUY_YES"]["price"] <= 0.749
                # BUY NO price is 1.00 - target_ask_yes
                assert eng.active_quotes["BUY_NO"]["price"] <= 0.26
            finally:
                eng.close()

    def test_enhanced_telemetry_metrics(self, engine):
        """Verify telemetry returns reserve, APR, resting queue value, and migration status."""
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        t = engine.get_telemetry()
        assert "resting_queue_value_usd" in t
        assert t["resting_queue_value_usd"] >= 0.0
        assert "reserve_cash_usd" in t
        assert "reserve_ratio_pct" in t
        assert "nominal_apr_pct" in t
        assert "compounding_apr_pct" in t
        assert "migration_count" in t
        assert t["migration_count"] == 0

    def test_render_dashboard_telemetry(self, engine, capsys):
        """Verify render_dashboard outputs enhanced telemetry including compounding APR and migration hurdle."""
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        market_info = {
            "question": "Will AI pass the Turing test in 2026?",
            "token_yes": "TOK_YES_1",
            "token_no": "TOK_NO_1",
            "daily_reward": 50.0,
        }

        mig_state = {
            "next_scan_remaining": 215.4,
            "hurdle_status": "MONITORING",
            "y_net_a": 15.2,
            "y_net_b": 22.5,
            "switching_cost": 0.045,
            "candidate_question": "Fed interest rate decision in December 2026",
            "decision_reason": "Next scan in 215s",
        }

        render_dashboard(
            engine=engine,
            market_info=market_info,
            start_time=time.time(),
            clear_screen=False,
            migration_state=mig_state,
        )

        captured = capsys.readouterr().out
        assert "Compounding APR" in captured
        assert "Resting Queue Value ($V_queue)" in captured
        assert "DYNAMIC CAPITAL MIGRATION & HURDLE TELEMETRY" in captured
        assert "Next Scan" in captured
        assert "Candidate Yield (M_B)" in captured
        assert "Fed interest rate" in captured

    def test_engine_market_migration_flow(self, engine):
        """Verify atomic market migration clears quotes, merges sets, and registers new market."""
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        # Create complete sets on initial market
        engine.portfolio.split(engine.condition_id, 20.0)
        assert engine.portfolio.get_position(engine.condition_id).complete_sets == 20.0

        cash_before = engine.portfolio.cash

        # Evaluate hurdle for candidate market with high yield
        should_switch, decision, cost = engine.evaluate_migration_hurdle(
            candidate_daily_yield=45.0,
            holding_horizon_hours=4.0,
            daily_yield_a=10.0,
        )
        assert should_switch is True

        # Execute migration
        mig_evt = engine.migrate_to_market(
            new_condition_id="0xNEW_MARKET_CID",
            new_asset_id_yes="NEW_TOK_YES",
            new_asset_id_no="NEW_TOK_NO",
            new_daily_reward_rate=45.0,
            new_rewards_max_spread_cents=2.0,
            new_rewards_min_size=20.0,
            reason=decision.reason,
        )

        # Complete sets merged
        assert engine.portfolio.get_position("0xTEST_COND_ID").complete_sets == 0.0
        assert engine.portfolio.cash > cash_before
        assert mig_evt["unlocked_cash"] > 0.0

        # Engine now tracking new market
        assert engine.condition_id == "0xNEW_MARKET_CID"
        assert engine.asset_id_yes == "NEW_TOK_YES"
        assert engine.asset_id_no == "NEW_TOK_NO"
        assert engine.daily_reward_rate == 45.0
        assert len(engine.migration_history) == 1

        t = engine.get_telemetry()
        assert t["migration_count"] == 1

    def test_update_quote_size_safe_updating(self, engine):
        """Verify update_quote_size threshold (< 5 shares ignores churn, >= 5 shares updates resting quotes)."""
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        # Baseline quote size is 20.0
        assert engine.quote_size == 20.0
        initial_score = engine.active_quotes["BUY_YES"]["score"]
        assert engine.active_quotes["BUY_YES"]["size"] == 20.0

        # 1. Update by < 5.0 shares (e.g. 20.0 -> 23.0, diff = 3.0)
        # Should update self.quote_size but return False and NOT churn resting book
        res = engine.update_quote_size(23.0)
        assert res is False
        assert engine.quote_size == 23.0
        assert engine.active_quotes["BUY_YES"]["size"] == 20.0

        # 2. Update by >= 5.0 shares (e.g. 23.0 -> 35.0, diff = 12.0)
        # Should return True, update self.quote_size, and re-evaluate resting quotes with new size & score
        res = engine.update_quote_size(35.0)
        assert res is True
        assert engine.quote_size == 35.0
        assert engine.active_quotes["BUY_YES"]["size"] == 35.0
        new_score = engine.active_quotes["BUY_YES"]["score"]
        assert new_score > initial_score
        assert engine.active_quotes["SELL_YES"]["size"] == 35.0

    def test_continuous_governor_quote_compounding_in_epoch(self, engine):
        """Verify ContinuousOptimizationGovernor evaluates during epoch and compounds rewards into larger quotes."""
        bids = [{"price": 0.49, "size": 100.0}]
        asks = [{"price": 0.51, "size": 100.0}]
        engine.update_orderbook_snapshot("YES", bids=bids, asks=asks)

        # Initial epoch: stable sizing
        res1 = engine.process_reward_epoch()
        assert res1["epoch_index"] == 1
        assert engine.quote_size == 20.0
        initial_score = engine.active_quotes["BUY_YES"]["score"]

        # Simulate substantial accumulated rewards compounding into cash bankroll
        engine.portfolio.credit_reward(150.0, condition_id=engine.condition_id, note="Accumulated rewards pool")
        assert engine.portfolio.cash >= 250.0

        # Process next epoch: governor evaluates new cash, recommends RESIZE_QUOTE, and compounds quote size
        res2 = engine.process_reward_epoch()
        assert res2["epoch_index"] == 2
        assert "RESIZE_QUOTE" in res2["governor_actions"]
        assert engine.quote_size > 20.0
        assert res2["quote_size"] > 20.0

        # Resting virtual quotes should now have compounded size and larger quadratic score
        compounded_score = engine.active_quotes["BUY_YES"]["score"]
        assert compounded_score > initial_score
        assert engine.active_quotes["BUY_YES"]["size"] > 20.0

    def test_endogenous_reserve_quote_sizing_integration(self):
        """Verify derive_quote_size with dynamic endogenous reserve for single and multi-market portfolios."""
        from polymarketbot.simulator.optimizer import derive_quote_size

        # 1. Single market dynamic endogenous reserve
        size_dyn = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=20.0, cycles_budget=2.5)
        assert abs(size_dyn - 44.3) <= 0.2

        # 2. Explicit reserve override
        size_override = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=20.0, reserve_pct=0.25, cycles_budget=2.5)
        assert size_override == 45.0

        # 3. Multi-market portfolio diversification (5 markets)
        multi_alloc = [{"capital": 200.0, "mid": 0.50} for _ in range(5)]
        size_multi = derive_quote_size(initial_cash=1000.0, mid_price=0.50, allocations=multi_alloc)
        assert size_multi > 0.0


