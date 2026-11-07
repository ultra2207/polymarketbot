"""Unit tests for Maker Fee Rebates (Volume Share Accounting).

Polymarket redistributes a fraction of taker fees collected from a market and credits
it directly to makers whose resting limit orders were filled.

Tests:
1. Calculation of maker rebate for standard, sports, and crypto markets.
2. Net maker edge improvement: buying at $0.980 with 0% maker fee + 0.08c maker rebate
   improves effective cost to $0.9792!
3. Domain 2 Settlement Maker Pricing Engine accounts for rebates and effective prices.
4. Domain 2 Harvester Engine accurately books net cost basis and settlement P&L.
5. Domain 2 OR-Tools Allocator boosts net edge and ARR from maker rebates.
6. Domain 1 RealMoneyTradingEngine credits maker rebates, logs to SQLite fills, and tracks cash.
7. Simulator Portfolio reflects net basis and maker rebates.
"""

from __future__ import annotations

from unittest.mock import MagicMock
import sqlite3
import pytest

from polymarketbot.fees import (
    FeeSchedule,
    effective_maker_price,
    maker_fee_rebate,
    maker_rebate,
    taker_fee,
)
from polymarketbot.trading.domain2.maker_pricing_engine import (
    MakerEngineConfig,
    SettlementMakerPricingEngine,
)
from polymarketbot.trading.domain2.types import (
    DiscountOpportunity,
    Domain2Position,
    MakerExecutionRegime,
)
from polymarketbot.trading.domain2.harvester_engine import (
    Domain2SettlementHarvester,
)
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Candidate,
    Domain2OrtoolsAllocator,
)
from polymarketbot.trading.engine import RealMoneyTradingEngine
from polymarketbot.simulator.portfolio import SimulatedPortfolio


# ---------------------------------------------------------------------------
# Test 1: Calculation across Standard, Sports, and Crypto Markets
# ---------------------------------------------------------------------------

def test_maker_rebate_standard_sports_crypto():
    """Verify maker fee rebate across market categories."""
    # 1. Standard fee-free market (e.g. Geopolitics or fee-free categories)
    free_market = FeeSchedule.zero()
    assert free_market.rate == 0.0
    assert maker_fee_rebate(0.50, 100, free_market) == 0.0

    # 2. Crypto Fee Structure V2: 7% rate, 20% rebate share
    crypto_v2 = FeeSchedule(rate=0.07, exponent=1.0, rebate_rate=0.20)
    # Taker fee at 50c for 100 shares = 100 * 0.07 * 0.25 = 1.75 USDC
    # Maker rebate = 20% of 1.75 = 0.35 USDC (0.35c / share)
    t_fee_crypto = taker_fee(100, 0.50, crypto_v2)
    assert t_fee_crypto == pytest.approx(1.75, abs=1e-5)
    r_maker_crypto = maker_fee_rebate(0.50, 100, crypto_v2)
    assert r_maker_crypto == pytest.approx(0.35, abs=1e-5)

    # 3. Crypto pilot curve: 25% rate, exponent 2.0, 20% rebate share
    crypto_pilot = FeeSchedule(rate=0.25, exponent=2.0, rebate_rate=0.20)
    # Taker fee at 50c for 100 shares = 100 * 0.25 * (0.25)^2 = 1.5625 USDC
    # Maker rebate = 20% of 1.5625 = 0.3125 USDC
    t_fee_pilot = taker_fee(100, 0.50, crypto_pilot)
    assert t_fee_pilot == pytest.approx(1.5625, abs=1e-4)
    r_maker_pilot = maker_fee_rebate(0.50, 100, crypto_pilot)
    assert r_maker_pilot == pytest.approx(0.3125, abs=1e-4)

    # 4. Sports Fee Structure V2: 3% rate, 25% rebate share
    sports_v2 = FeeSchedule(rate=0.03, exponent=1.0, rebate_rate=0.25)
    # Taker fee at 50c for 100 shares = 100 * 0.03 * 0.25 = 0.75 USDC
    # Maker rebate = 25% of 0.75 = 0.1875 USDC
    t_fee_sports = taker_fee(100, 0.50, sports_v2)
    assert t_fee_sports == pytest.approx(0.75, abs=1e-5)
    r_maker_sports = maker_fee_rebate(0.50, 100, sports_v2)
    assert r_maker_sports == pytest.approx(0.1875, abs=1e-5)

    # 5. Sports Post-July 10: 5% rate, 15% rebate share
    sports_post = FeeSchedule(rate=0.05, exponent=1.0, rebate_rate=0.15)
    # Taker fee at 50c for 100 shares = 100 * 0.05 * 0.25 = 1.25 USDC
    # Maker rebate = 15% of 1.25 = 0.1875 USDC
    t_fee_post = taker_fee(100, 0.50, sports_post)
    assert t_fee_post == pytest.approx(1.25, abs=1e-5)
    r_maker_post = maker_fee_rebate(0.50, 100, sports_post)
    assert r_maker_post == pytest.approx(0.1875, abs=1e-5)

    # 6. Override maker rebate share
    r_override = maker_fee_rebate(0.50, 100, crypto_v2, maker_rebate_share_pct=0.40)
    assert r_override == pytest.approx(0.70, abs=1e-5)

    # 7. Edge cases
    assert maker_fee_rebate(0.50, 0, crypto_v2) == 0.0
    with pytest.raises(ValueError):
        maker_fee_rebate(1.50, 100, crypto_v2)


# ---------------------------------------------------------------------------
# Test 2: Net Maker Edge Improvement (Buying at $0.980 with 0.08c rebate -> $0.9792)
# ---------------------------------------------------------------------------

def test_net_maker_edge_improvement_98_cents():
    """Test net maker edge improvement: buying at $0.980 with 0% maker fee + 0.08c rebate -> $0.9792!"""
    # Rate of 0.2040816 at p=0.980 gives taker fee 0.40c per share, so 20% rebate is 0.08c ($0.0008)
    schedule = FeeSchedule(rate=0.2040816, exponent=1.0, rebate_rate=0.20)

    # Single share fill
    rebate_1_share = maker_fee_rebate(0.980, 1.0, schedule)
    assert rebate_1_share == pytest.approx(0.0008, abs=1e-5)  # 0.08 cents

    # Effective price for BUY: 0.980 - 0.0008 = 0.9792
    eff_price_buy = effective_maker_price(0.980, 1.0, "BUY", schedule)
    assert eff_price_buy == pytest.approx(0.9792, abs=1e-6)

    # 100 shares fill: total rebate = $0.08 (8 cents), rebate per share = 0.08c
    rebate_100_shares = maker_fee_rebate(0.980, 100.0, schedule)
    assert rebate_100_shares == pytest.approx(0.08, abs=1e-4)

    eff_price_100 = effective_maker_price(0.980, 100.0, "BUY", schedule)
    assert eff_price_100 == pytest.approx(0.9792, abs=1e-6)

    # Direct rebate argument
    eff_direct = effective_maker_price(0.980, 100.0, "BUY", maker_rebate_usd=0.08)
    assert eff_direct == pytest.approx(0.9792, abs=1e-6)

    # Effective price for SELL: 0.020 + 0.0008 = 0.0208
    eff_price_sell = effective_maker_price(0.020, 1.0, "SELL", schedule)
    assert eff_price_sell == pytest.approx(0.0208, abs=1e-6)

    # Edge boost quantification:
    gross_margin = (1.000 - 0.980) / 0.980
    net_margin = (1.000 - eff_price_buy) / eff_price_buy
    edge_boost_bps = (net_margin - gross_margin) * 10000.0
    assert edge_boost_bps > 8.0  # Over 8 bps edge improvement


# ---------------------------------------------------------------------------
# Test 3: Domain 2 Settlement Maker Pricing Engine Integration
# ---------------------------------------------------------------------------

def test_domain2_pricing_engine_accounts_for_rebates():
    """Verify SettlementMakerPricingEngine incorporates maker rebates into expected ARR."""
    schedule = FeeSchedule(rate=0.2040816, exponent=1.0, rebate_rate=0.20)
    config = MakerEngineConfig(default_tick_size=0.001)

    engine_with_fee = SettlementMakerPricingEngine(config=config)
    engine_fee_free = SettlementMakerPricingEngine(config=config)

    # Evaluate decision with schedule vs fee-free
    dec_with_rebate = engine_with_fee.evaluate_decision(
        best_bid=0.980,
        best_ask=0.985,
        allocated_capital_usdc=98.0,
        hours_to_expiry=5.0,
        schedule=schedule,
    )

    dec_fee_free = engine_fee_free.evaluate_decision(
        best_bid=0.980,
        best_ask=0.985,
        allocated_capital_usdc=98.0,
        hours_to_expiry=5.0,
        schedule=FeeSchedule.zero(),
    )

    assert dec_with_rebate.maker_rebate_usd > 0.0
    assert dec_fee_free.maker_rebate_usd == 0.0

    # Effective price must be lower than quote price for maker bids
    assert dec_with_rebate.effective_price < dec_with_rebate.quote_price
    assert dec_fee_free.effective_price == dec_fee_free.quote_price

    # ARR with rebate must be higher than without rebate
    assert dec_with_rebate.expected_arr > dec_fee_free.expected_arr


# ---------------------------------------------------------------------------
# Test 4: Domain 2 Harvester Engine Fill & Settlement Accounting
# ---------------------------------------------------------------------------

def test_domain2_harvester_net_cost_basis():
    """Verify Domain2SettlementHarvester records net cost basis reducing cash outflow."""
    clob_mock = MagicMock()
    ctf_mock = MagicMock()
    engine = Domain2SettlementHarvester(
        clob_client=clob_mock,
        ctf_client=ctf_mock,
        initial_capital_usdc=1000.0,
        dry_run=True,
    )

    # Buy 100 shares at $0.980 with $0.08 maker rebate
    pos = engine.record_fill(
        condition_id="0x" + "a" * 64,
        asset_id="0x" + "1" * 64,
        shares=100.0,
        price=0.980,
        liveness_expiry_ts=1700000000,
        is_maker=True,
        maker_rebate_usd=0.08,
    )

    # Net cost should be 100 * 0.980 - 0.08 = 97.92 USDC
    assert pos.cost_basis_usdc == pytest.approx(97.92, abs=1e-5)
    assert pos.entry_price == pytest.approx(0.9792, abs=1e-5)
    assert pos.maker_rebate_usd == pytest.approx(0.08, abs=1e-5)

    # Available cash deducted net amount: 1000 - 97.92 = 902.08
    assert engine.available_cash == pytest.approx(902.08, abs=1e-5)


# ---------------------------------------------------------------------------
# Test 5: Domain 2 OR-Tools Allocator Edge Boost from Maker Rebates
# ---------------------------------------------------------------------------

def test_domain2_ortools_allocator_rebate_boost():
    """Verify OR-Tools Allocator credits maker rebate reducing effective price and boosting edge."""
    schedule = FeeSchedule(rate=0.2040816, exponent=1.0, rebate_rate=0.20)
    allocator = Domain2OrtoolsAllocator(min_order_usd=20.0)

    # Candidate with fee schedule (maker order)
    cand_rebate = Domain2Candidate(
        condition_id="0x" + "b" * 64,
        token_id="0x" + "2" * 64,
        question="Will X occur?",
        category="crypto",
        price=0.980,
        hours_to_resolution=2.0,
        depth_usd=5000.0,
        fee_schedule=schedule,
        is_taker=False,
        is_uma_proposed=True,
    )

    # Candidate without fee schedule (0% fee, 0 rebate)
    cand_zero = Domain2Candidate(
        condition_id="0x" + "c" * 64,
        token_id="0x" + "3" * 64,
        question="Will Y occur?",
        category="geopolitics",
        price=0.980,
        hours_to_resolution=2.0,
        depth_usd=5000.0,
        fee_schedule=FeeSchedule.zero(),
        is_taker=False,
        is_uma_proposed=True,
    )

    res_rebate = allocator.optimize_allocation(candidates=[cand_rebate], bankroll_usd=1000.0)
    res_zero = allocator.optimize_allocation(candidates=[cand_zero], bankroll_usd=1000.0)

    assert len(res_rebate.allocations) == 1
    assert len(res_zero.allocations) == 1

    alloc_reb = res_rebate.allocations[0]
    alloc_zero = res_zero.allocations[0]

    assert alloc_reb.maker_rebate_usd > 0.0
    assert alloc_zero.maker_rebate_usd == 0.0

    # Net edge of candidate with rebate must be strictly greater than candidate without rebate
    assert alloc_reb.net_edge_pct > alloc_zero.net_edge_pct


# ---------------------------------------------------------------------------
# Test 6: Domain 1 RealMoneyTradingEngine Maker Rebate Execution & SQLite
# ---------------------------------------------------------------------------

def test_domain1_real_money_trading_engine_maker_rebates(tmp_path):
    """Verify RealMoneyTradingEngine books maker rebates and updates SQLite fills table."""
    schedule = FeeSchedule(rate=0.07, exponent=1.0, rebate_rate=0.20)
    db_file = tmp_path / "test_trading.db"
    ledger_file = tmp_path / "test_ledger.json"

    engine = RealMoneyTradingEngine(
        condition_id="0x" + "d" * 64,
        initial_cash=1000.0,
        fee_schedule=schedule,
        maker_rebate_share_pct=0.20,
        db_path=db_file,
        ledger_path=ledger_file,
        dry_run=True,
    )

    # Book a maker BUY fill: 100 shares at 50c
    # At 50c, taker fee is 1.75 USDC, maker rebate is 0.35 USDC
    qdata = {"order_id": "test_order_1", "price": 0.50, "size": 100.0}
    record = engine._book_exchange_fill(
        qdata=qdata,
        tok="YES",
        side="BUY",
        size=100.0,
        price=0.50,
        is_taker=False,
        ts=1700000000.0,
        note="maker execution test",
    )

    assert record.is_maker is True
    assert record.maker_rebate_usd == pytest.approx(0.35, abs=1e-4)

    # Engine tracked cumulative maker rebates and cash
    assert engine.actual_rebates_usd == pytest.approx(0.35, abs=1e-4)
    # Gross cost = 100 * 0.50 = 50.0. Cash had 1000.0.
    # self.cash -= 50.0, then self.cash += 0.35 rebate -> 950.35
    assert engine.cash == pytest.approx(950.35, abs=1e-4)
    assert engine.realized_pnl == pytest.approx(0.35, abs=1e-4)

    # Verify SQLite fills table has maker_rebate_usd recorded
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("SELECT is_maker, fee_usd, maker_rebate_usd, realized_pnl FROM fills")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == 1  # is_maker
    assert row[1] == 0.0  # fee_usd paid is 0
    assert row[2] == pytest.approx(0.35, abs=1e-4)  # maker_rebate_usd
    assert row[3] == pytest.approx(0.35, abs=1e-4)  # realized_pnl delta
    conn.close()


# ---------------------------------------------------------------------------
# Test 7: Simulator Portfolio Net Basis and Rebates
# ---------------------------------------------------------------------------

def test_simulator_portfolio_maker_rebate():
    """Verify SimulatedPortfolio handles maker rebates on BUY and SELL fills."""
    portfolio = SimulatedPortfolio(initial_cash=1000.0)

    # Process maker BUY fill of 100 YES @ $0.980 with $0.08 maker rebate
    tx = portfolio.process_fill(
        condition_id="cond_1",
        side="BUY",
        outcome="YES",
        price=0.980,
        size=100.0,
        is_maker=True,
        maker_rebate_usd=0.08,
    )

    assert tx.maker_rebate == pytest.approx(0.08, abs=1e-4)
    # Net cash paid: 98.00 - 0.08 = 97.92
    assert tx.net_cash_delta == pytest.approx(-97.92, abs=1e-4)
    assert portfolio.cash == pytest.approx(902.08, abs=1e-4)

    # Average cost basis should be 97.92 / 100 = 0.9792
    pos = portfolio.get_position("cond_1")
    assert pos.avg_cost_yes == pytest.approx(0.9792, abs=1e-5)
    assert getattr(portfolio, "total_rebates_usd", 0.0) == pytest.approx(0.08, abs=1e-4)
