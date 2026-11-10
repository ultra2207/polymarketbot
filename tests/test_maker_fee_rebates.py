"""Unit tests for Maker Fee Rebates (Volume Share Accounting).

Polymarket redistributes a fraction of taker fees collected from a market and credits
it directly to makers whose resting limit orders were filled.

Tests:
1. Calculation of maker rebate for standard, sports, and crypto markets.
2. Net maker edge improvement: buying at $0.980 with 0% maker fee + 0.08c maker rebate
   improves effective cost to $0.9792!
3. Domain 1 RealMoneyTradingEngine credits maker rebates, logs to SQLite fills, and tracks cash.
"""

from __future__ import annotations

import sqlite3
import pytest

from polymarketbot.fees import (
    FeeSchedule,
    effective_maker_price,
    maker_fee_rebate,
    maker_rebate,
    taker_fee,
)
from polymarketbot.live.engine import RealMoneyTradingEngine


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
# Test 3: Domain 1 RealMoneyTradingEngine Maker Rebate Execution & SQLite
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

