"""Unit tests for Strategy C: CTF Mint & Selective Cross-Split Arbitrage Engine."""

from __future__ import annotations

import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.trading.ctf import PolygonCtfClient, SPLIT_POSITIONS_SELECTOR
from polymarketbot.trading.ctf_split_arbitrage import (
    CTF_CONTRACT_POLYGON,
    CTF_SPLIT_SELECTOR,
    DEFAULT_COLLATERAL_USDC,
    TYPICAL_SPLIT_GAS_UNITS,
    VIP_TIER_REBATES,
    CtfCrossSplitArbitrageEngine,
    CtfSplitArbConfig,
    CtfSplitExecutionPlan,
    CtfSplitLeg,
    CtfSplitOpportunity,
    compute_leg_taker_fee,
    encode_split_positions_calldata,
    estimate_polygon_eip1559_gas_usd,
)
from polymarketbot.trading.settlement import build_ctf_split


# ---------------------------------------------------------------------------
# 1. Config & VIP Tier Tests
# ---------------------------------------------------------------------------
def test_config_defaults_and_vip_tier():
    cfg = CtfSplitArbConfig()
    assert cfg.min_gross_spread_bps == 40.0
    assert cfg.min_net_edge_usd == 1.00
    assert cfg.max_mint_size_usd == 1000.0
    assert cfg.vip_tier == "bronze"
    assert cfg.vip_rebate_rate == 0.03
    assert cfg.gas_units_split == 85_000


@pytest.mark.parametrize(
    "tier_input,expected_rebate",
    [
        ("none", 0.00),
        ("None", 0.00),
        ("bronze", 0.03),
        ("Bronze", 0.03),
        ("BRONZE", 0.03),
        ("silver", 0.08),
        ("gold", 0.18),
        ("platinum", 0.32),
        ("diamond", 0.44),
        ("obsidian", 0.50),
    ],
)
def test_config_vip_tiers_rebate_rates(tier_input, expected_rebate):
    cfg = CtfSplitArbConfig(vip_tier=tier_input)
    assert cfg.vip_tier == tier_input.lower()
    assert cfg.vip_rebate_rate == expected_rebate


def test_config_invalid_vip_tier_raises():
    with pytest.raises(ValueError, match="Invalid vip_tier"):
        CtfSplitArbConfig(vip_tier="master")


def test_config_validation_bounds():
    with pytest.raises(ValueError, match="min_gross_spread_bps cannot be negative"):
        CtfSplitArbConfig(min_gross_spread_bps=-10.0)

    with pytest.raises(ValueError, match="min_net_edge_usd cannot be negative"):
        CtfSplitArbConfig(min_net_edge_usd=-1.0)

    with pytest.raises(ValueError, match="max_mint_size_usd must be greater than zero"):
        CtfSplitArbConfig(max_mint_size_usd=0.0)


# ---------------------------------------------------------------------------
# 2. Polygon PoS EIP-1559 Gas Friction Tests
# ---------------------------------------------------------------------------
def test_estimate_polygon_eip1559_gas_calculation():
    # 85,000 gas units, 40 Gwei, $0.40 POL/USD:
    # 85,000 * (40 * 1e-9) = 0.0034 POL
    # 0.0034 * $0.40 = $0.001360 USD
    gas_usd = estimate_polygon_eip1559_gas_usd(
        gas_units=85_000,
        gas_price_gwei=40.0,
        pol_usd_price=0.40,
    )
    assert gas_usd == 0.00136

    # Gas surge test: 150 Gwei, $0.65 POL/USD:
    # 85,000 * (150 * 1e-9) = 0.01275 POL
    # 0.01275 * $0.65 = $0.008288 USD
    surge_gas_usd = estimate_polygon_eip1559_gas_usd(
        gas_units=85_000,
        gas_price_gwei=150.0,
        pol_usd_price=0.65,
    )
    assert surge_gas_usd == 0.008288


# ---------------------------------------------------------------------------
# 3. Taker Fee & VIP Rebate Integration Tests
# ---------------------------------------------------------------------------
def test_compute_leg_taker_fee_with_vip_discount():
    # 1,000 shares @ p=0.50, fee_rate=0.02, exponent=1.0:
    # curve = 0.50 * 0.50 = 0.25
    # gross_fee = 1,000 * 0.02 * 0.25 = 5.00000 USDC
    # VIP Bronze (3% rebate): rebate = 5.00 * 0.03 = 0.15000 USDC
    # net_fee = 5.00 - 0.15 = 4.85000 USDC
    gross, rebate, net = compute_leg_taker_fee(
        shares=1000.0,
        price=0.50,
        fee_rate=0.02,
        fee_exponent=1.0,
        vip_tier="bronze",
    )
    assert gross == 5.00000
    assert rebate == 0.15000
    assert net == 4.85000

    # Obsidian tier (50% rebate):
    # gross = 5.00, rebate = 2.50, net = 2.50
    gross_obs, rebate_obs, net_obs = compute_leg_taker_fee(
        shares=1000.0,
        price=0.50,
        fee_rate=0.02,
        fee_exponent=1.0,
        vip_tier="obsidian",
    )
    assert gross_obs == 5.00000
    assert rebate_obs == 2.50000
    assert net_obs == 2.50000


def test_compute_leg_taker_fee_zero_or_invalid():
    assert compute_leg_taker_fee(0.0, 0.50) == (0.0, 0.0, 0.0)

    with pytest.raises(ValueError, match="Price out of range"):
        compute_leg_taker_fee(100.0, -0.05)

    with pytest.raises(ValueError, match="Price out of range"):
        compute_leg_taker_fee(100.0, 1.05)


# ---------------------------------------------------------------------------
# 4. Mispriced Complete Set Discovery (sum(best_bids) = 1.032)
# ---------------------------------------------------------------------------
def test_discovery_of_mispriced_complete_set_binary():
    """Verify discovery when sum(best_bids) = 0.540 + 0.492 = 1.032 (320 bps above par)."""
    engine = CtfCrossSplitArbitrageEngine(
        config=CtfSplitArbConfig(
            min_gross_spread_bps=40.0,
            min_net_edge_usd=1.00,
            max_mint_size_usd=1000.0,
            vip_tier="bronze",
        )
    )

    legs = [
        CtfSplitLeg(
            index=0,
            token_id="11111",
            condition_id="0x" + "aa" * 32,
            title="YES",
            best_bid=0.540,
            best_bid_size=1500.0,
            fee_rate=0.02,
        ),
        CtfSplitLeg(
            index=1,
            token_id="22222",
            condition_id="0x" + "aa" * 32,
            title="NO",
            best_bid=0.492,
            best_bid_size=2000.0,
            fee_rate=0.02,
        ),
    ]

    opp = engine.scan_market(legs, pol_usd_price=0.40, gas_price_gwei=40.0)
    assert opp is not None
    assert opp.sum_best_bids == 1.032
    assert opp.gross_spread == 0.032
    assert opp.gross_spread_bps == 320.0
    # Capped at max_mint_size_usd = 1000 shares
    assert opp.mint_size_shares == 1000.0
    assert opp.mint_cost_usd == 1000.0
    assert opp.gross_proceeds_usd == 1032.0
    assert opp.gross_profit_usd == 32.0

    # Gas cost for 85,000 units
    assert opp.gas_cost_usd == 0.00136

    # Selective execution:
    # Leg 0 (YES @ 0.54): curve = 0.54 * 0.46 = 0.2484 -> gross_fee = 1000 * 0.02 * 0.2484 = 4.968
    # Leg 1 (NO @ 0.492): curve = 0.492 * 0.508 = 0.249936 -> gross_fee = 1000 * 0.02 * 0.249936 = 4.99872
    # The engine chooses Leg 0 as taker because 4.968 < 4.99872, maximizing net profit!
    assert opp.taker_leg_index == 0
    assert opp.gross_taker_fee_usd == 4.968
    assert opp.vip_rebate_usd == round(4.968 * 0.03, 5)

    # Net risk-free profit: $32.00 - net_taker_fee - gas
    net_taker_fee = round(4.968 - round(4.968 * 0.03, 5), 5)
    expected_profit = round(32.0 - net_taker_fee - 0.00136, 4)
    assert opp.net_profit_usd == expected_profit
    assert opp.net_profit_usd > 25.0
    assert opp.is_actionable is True


def test_discovery_of_mispriced_complete_set_multi_outcome_3way():
    """Verify discovery in a 3-way market (Soccer 1X2): 0.450 + 0.300 + 0.282 = 1.032."""
    engine = CtfCrossSplitArbitrageEngine(
        config=CtfSplitArbConfig(
            min_gross_spread_bps=40.0,
            min_net_edge_usd=1.00,
            max_mint_size_usd=1000.0,
            vip_tier="silver",  # 8% rebate
        )
    )

    market_dict = {
        "condition_id": "0x" + "bb" * 32,
        "legs": [
            {"index": 0, "token_id": "100", "title": "Home", "best_bid": 0.450, "best_bid_size": 1000.0, "fee_rate": 0.02},
            {"index": 1, "token_id": "200", "title": "Draw", "best_bid": 0.300, "best_bid_size": 1200.0, "fee_rate": 0.02},
            {"index": 2, "token_id": "300", "title": "Away", "best_bid": 0.282, "best_bid_size": 1500.0, "fee_rate": 0.02},
        ],
    }

    opp = engine.scan_market(market_dict, pol_usd_price=0.40, gas_price_gwei=40.0)
    assert opp is not None
    assert opp.sum_best_bids == 1.032
    assert opp.mint_size_shares == 1000.0
    assert opp.gross_profit_usd == 32.0

    # Taker fee curves:
    # Home (0.45): 0.45 * 0.55 = 0.2475 -> fee = 4.95
    # Draw (0.30): 0.30 * 0.70 = 0.2100 -> fee = 4.20
    # Away (0.282): 0.282 * 0.718 = 0.202476 -> fee = 4.04952 (lowest fee!)
    # Engine selects Leg 2 (Away) as Taker to minimize fees!
    assert opp.taker_leg_index == 2
    assert opp.gross_taker_fee_usd == 4.04952
    assert opp.vip_rebate_usd == round(4.04952 * 0.08, 5)

    assert opp.net_profit_usd > 25.0
    assert opp.is_actionable is True


def test_rejection_when_spread_below_hurdle():
    """Markets with sum(best_bids) <= 1.000 or spread < min_gross_spread_bps must be rejected."""
    engine = CtfCrossSplitArbitrageEngine(
        config=CtfSplitArbConfig(min_gross_spread_bps=40.0)  # 40 bps = 0.004
    )

    # Sub-par market (0.49 + 0.50 = 0.99)
    legs_subpar = [
        CtfSplitLeg(index=0, token_id="1", condition_id="0xaa", best_bid=0.49, best_bid_size=1000.0),
        CtfSplitLeg(index=1, token_id="2", condition_id="0xaa", best_bid=0.50, best_bid_size=1000.0),
    ]
    opp_subpar = engine.scan_market(legs_subpar)
    assert opp_subpar is not None
    assert opp_subpar.is_actionable is False
    assert opp_subpar.net_profit_usd <= 0.0

    # Insufficient spread (0.501 + 0.501 = 1.002 -> 20 bps < 40 bps hurdle)
    legs_narrow = [
        CtfSplitLeg(index=0, token_id="1", condition_id="0xaa", best_bid=0.501, best_bid_size=1000.0),
        CtfSplitLeg(index=1, token_id="2", condition_id="0xaa", best_bid=0.501, best_bid_size=1000.0),
    ]
    opp_narrow = engine.scan_market(legs_narrow)
    assert opp_narrow is not None
    assert opp_narrow.is_actionable is False


def test_rejection_when_zero_bid_liquidity():
    engine = CtfCrossSplitArbitrageEngine()
    legs_zero_liq = [
        CtfSplitLeg(index=0, token_id="1", condition_id="0xaa", best_bid=0.55, best_bid_size=0.0),
        CtfSplitLeg(index=1, token_id="2", condition_id="0xaa", best_bid=0.55, best_bid_size=100.0),
    ]
    opp = engine.scan_market(legs_zero_liq)
    assert opp is not None
    assert opp.mint_size_shares == 0.0
    assert opp.is_actionable is False


# ---------------------------------------------------------------------------
# 5. Execution Plan Generation & Risk-Free Profit Assertion
# ---------------------------------------------------------------------------
def test_execution_plan_generation_and_risk_free_profit():
    """Assert execution plan: N-1 Maker legs (0% fee), 1 Taker leg, valid CTF calldata, zero broken basket risk."""
    engine = CtfCrossSplitArbitrageEngine(
        config=CtfSplitArbConfig(
            min_gross_spread_bps=40.0,
            min_net_edge_usd=1.00,
            max_mint_size_usd=500.0,
            vip_tier="gold",  # 18% rebate
        )
    )

    condition_hex = "0x" + "12345678" * 8
    legs = [
        CtfSplitLeg(
            index=0,
            token_id="10101",
            condition_id=condition_hex,
            title="Team A",
            best_bid=0.550,
            best_bid_size=800.0,
            fee_rate=0.02,
        ),
        CtfSplitLeg(
            index=1,
            token_id="20202",
            condition_id=condition_hex,
            title="Team B",
            best_bid=0.485,
            best_bid_size=600.0,
            fee_rate=0.02,
        ),
    ]

    opp = engine.scan_market(legs)
    assert opp is not None
    assert opp.is_actionable is True

    plan = engine.evaluate_execution(opp)
    assert isinstance(plan, CtfSplitExecutionPlan)

    # 1. Broken Basket Risk is explicitly ZERO
    assert plan.broken_basket_risk_usd == 0.0

    # 2. Risk-free profit assertion
    assert plan.net_profit_usd > 1.00
    assert plan.net_roi_bps > 0.0
    assert plan.total_gross_proceeds_usd > (
        plan.total_mint_cost_usd + plan.total_gas_usd + plan.total_net_fees_usd
    )

    # 3. Order batch structure
    assert len(plan.order_batch) == 2
    maker_orders = [o for o in plan.order_batch if o.order_type == "MAKER"]
    taker_orders = [o for o in plan.order_batch if o.order_type == "TAKER"]
    assert len(maker_orders) == 1
    assert len(taker_orders) == 1

    # Maker order pays 0% fee
    assert maker_orders[0].net_fee_usd == 0.0
    assert maker_orders[0].side == "SELL"
    assert maker_orders[0].size == 500.0

    # Taker order receives VIP rebate
    assert taker_orders[0].net_fee_usd > 0.0
    assert taker_orders[0].vip_rebate_usd > 0.0
    assert taker_orders[0].side == "SELL"
    assert taker_orders[0].size == 500.0

    # 4. Mint transaction payload verification
    payload = plan.mint_payload
    assert payload.to == CTF_CONTRACT_POLYGON
    assert payload.calldata.startswith("0x" + CTF_SPLIT_SELECTOR)
    assert payload.amount_shares == 500.0
    assert payload.amount_6dp == 500_000_000
    assert payload.partition == [1, 2]

    # 5. Dry-run simulation executes cleanly
    result = engine.simulate_execution(plan)
    assert result.success is True
    assert result.is_dry_run is True
    assert result.realized_profit_usd == plan.net_profit_usd
    assert len(result.orders_placed) == 2


# ---------------------------------------------------------------------------
# 6. CTF splitPosition ABI Calldata Parity Tests
# ---------------------------------------------------------------------------
def test_split_positions_calldata_parity():
    """Verify calldata encoding across ctf_split_arbitrage, settlement, and PolygonCtfClient."""
    condition_id = "0x" + "ab" * 32
    shares = 100.5
    partition = [1, 2]
    collateral = DEFAULT_COLLATERAL_USDC

    # 1. From ctf_split_arbitrage.py
    calldata_engine = encode_split_positions_calldata(
        condition_id=condition_id,
        amount_shares=shares,
        partition=partition,
        collateral_token=collateral,
    )

    # 2. From settlement.py
    calldata_settlement = build_ctf_split(
        condition_id=condition_id,
        amount_6dp=int(round(shares * 1_000_000)),
        collateral_token=collateral,
        index_sets=partition,
    )

    # 3. From PolygonCtfClient in ctf.py
    client = PolygonCtfClient(dry_run=True)
    calldata_client = client.encode_split_positions(
        condition_id=condition_id,
        amount_shares=shares,
        index_sets=partition,
        collateral_token=collateral,
    )

    assert calldata_engine == calldata_settlement
    assert calldata_engine == calldata_client
    assert calldata_engine.startswith("0x" + CTF_SPLIT_SELECTOR)


# ---------------------------------------------------------------------------
# 7. Multiple Markets Scanning & Ranking Test
# ---------------------------------------------------------------------------
def test_scan_multiple_markets_ranking():
    engine = CtfCrossSplitArbitrageEngine(
        config=CtfSplitArbConfig(min_gross_spread_bps=40.0, min_net_edge_usd=1.00)
    )

    m1 = {
        "condition_id": "0x1111",
        "legs": [
            {"best_bid": 0.52, "best_bid_size": 500.0},
            {"best_bid": 0.50, "best_bid_size": 500.0},  # sum = 1.020 (200 bps)
        ],
    }
    m2 = {
        "condition_id": "0x2222",
        "legs": [
            {"best_bid": 0.54, "best_bid_size": 1000.0},
            {"best_bid": 0.51, "best_bid_size": 1000.0},  # sum = 1.050 (500 bps)
        ],
    }
    m3 = {
        "condition_id": "0x3333",
        "legs": [
            {"best_bid": 0.49, "best_bid_size": 1000.0},
            {"best_bid": 0.50, "best_bid_size": 1000.0},  # sum = 0.990 (no arb)
        ],
    }

    ranked = engine.scan_markets([m1, m2, m3])
    # m3 is rejected (not actionable)
    assert len(ranked) == 2
    # m2 has higher profit than m1, so it must be first
    assert ranked[0].condition_id == "0x2222"
    assert ranked[1].condition_id == "0x1111"
    assert ranked[0].net_profit_usd > ranked[1].net_profit_usd
