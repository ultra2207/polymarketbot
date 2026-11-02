"""Unit and Integration Tests for Polymarket Native Paper Trading Simulation Engine."""

import os
import tempfile
import time
from pathlib import Path
import pytest

from polymarketbot.rewards.deskew import DeSkewPathway, DynamicDeSkewOptimizer
from polymarketbot.simulator.book import SyntheticOrderBook
from polymarketbot.simulator.engine import NativePaperTradingEngine
from polymarketbot.simulator.impact import MarketImpactEngine
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
                gas_fee_merge=0.015,
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
