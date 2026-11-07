"""Unit tests for Domain 3 Path B: Multi-Outcome Negative-Risk Converter & Bundle Arbitrage Engine."""

from __future__ import annotations

import time
import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.trading.negrisk_engine import (
    NEGRISK_ADAPTER_ADDRESS,
    CoherenceChecker,
    ConversionPlanner,
    NegRiskEngine,
    NegRiskExecutionPlan,
    NegRiskExecutionResult,
    NegRiskMarket,
    NegRiskOpportunity,
    OpportunityType,
    OutcomeLeg,
    compute_taker_fee,
    estimate_conversion_gas_usd,
)
from polymarketbot.trading.settlement import (
    NEGRISK_CONVERT_SELECTOR,
    NEGRISK_MERGE_SELECTOR,
    amount_to_6dp,
)

SAMPLE_MARKET_ID = "0x" + "aa" * 32
SAMPLE_EVENT_ID = "0x" + "bb" * 32
SAMPLE_COND_0 = "0x" + "11" * 32
SAMPLE_COND_1 = "0x" + "22" * 32
SAMPLE_COND_2 = "0x" + "33" * 32
SAMPLE_COND_3 = "0x" + "44" * 32


# ---------------------------------------------------------------------------
# 1. Taker Fee Deduction Tests
# ---------------------------------------------------------------------------
def test_compute_taker_fee_zero_shares_or_rate():
    assert compute_taker_fee(0.0, 0.50, fee_rate=0.05) == 0.0
    assert compute_taker_fee(100.0, 0.50, fee_rate=0.0) == 0.0


def test_compute_taker_fee_sports_market():
    # 100 shares @ p=0.50, rate=0.05, exponent=1.0:
    # curve = 0.5 * 0.5 = 0.25
    # raw fee = 100 * 0.05 * 0.25 = 1.25000 USDC
    fee = compute_taker_fee(100.0, 0.50, fee_rate=0.05, fee_exponent=1.0)
    assert fee == 1.25000

    # 50 shares @ p=0.90, rate=0.03, exponent=1.0:
    # curve = 0.9 * 0.1 = 0.09
    # raw fee = 50 * 0.03 * 0.09 = 0.13500 USDC
    fee2 = compute_taker_fee(50.0, 0.90, fee_rate=0.03, fee_exponent=1.0)
    assert fee2 == 0.13500


def test_compute_taker_fee_with_fee_schedule():
    sched = FeeSchedule(rate=0.05, exponent=1.0)
    fee = compute_taker_fee(100.0, 0.50, fee_schedule=sched)
    assert fee == 1.25000


def test_compute_taker_fee_invalid_price_raises():
    with pytest.raises(ValueError, match="Price out of range"):
        compute_taker_fee(10.0, -0.01, fee_rate=0.05)
    with pytest.raises(ValueError, match="Price out of range"):
        compute_taker_fee(10.0, 1.05, fee_rate=0.05)


# ---------------------------------------------------------------------------
# 2. Polygon PoS Gas Estimation Tests
# ---------------------------------------------------------------------------
def test_estimate_conversion_gas_usd_calculation():
    # 3 legs with merge:
    # convert: 85,000 + merge: 78,000 = 163,000 units
    # 40 Gwei = 40e-9 POL/gas
    # POL price = $0.10
    # gas USD = 163,000 * 40e-9 * 0.10 = 0.000652 USD
    gas = estimate_conversion_gas_usd(
        k_legs=3,
        pol_usd_price=0.10,
        gas_price_gwei=40.0,
        include_merge=True,
    )
    assert gas == 0.000652

    # 3 legs without merge: 85,000 * 40e-9 * 0.10 = 0.000340 USD
    gas_no_merge = estimate_conversion_gas_usd(
        k_legs=3,
        pol_usd_price=0.10,
        gas_price_gwei=40.0,
        include_merge=False,
    )
    assert gas_no_merge == 0.000340

    # 5 legs with merge: 85,000 + 2*15,000 + 78,000 = 193,000 units
    gas_5_legs = estimate_conversion_gas_usd(
        k_legs=5,
        pol_usd_price=0.10,
        gas_price_gwei=40.0,
        include_merge=True,
    )
    assert gas_5_legs == 0.000772


# ---------------------------------------------------------------------------
# 3. Market Registration & Validation Tests
# ---------------------------------------------------------------------------
def test_market_registration_validation():
    engine = NegRiskEngine(dry_run=True)

    # k < 3 must raise ValueError
    with pytest.raises(ValueError, match="k >= 3"):
        engine.register_market(SAMPLE_EVENT_ID, SAMPLE_MARKET_ID, k=2)

    market = engine.register_market(SAMPLE_EVENT_ID, SAMPLE_MARKET_ID, k=3, title="Arsenal vs Chelsea 1X2")
    assert market.k == 3
    assert not market.is_complete()

    # Leg index out of range must raise ValueError
    with pytest.raises(ValueError, match="Leg index 3 out of range"):
        engine.register_leg(SAMPLE_EVENT_ID, 3, SAMPLE_COND_0, SAMPLE_COND_0, "100", "101")

    # Valid leg registration
    leg0 = engine.register_leg(SAMPLE_EVENT_ID, 0, SAMPLE_COND_0, SAMPLE_COND_0, "100", "101", title="Arsenal")
    leg1 = engine.register_leg(SAMPLE_EVENT_ID, 1, SAMPLE_COND_1, SAMPLE_COND_1, "102", "103", title="Draw")
    assert not market.is_complete()

    leg2 = engine.register_leg(SAMPLE_EVENT_ID, 2, SAMPLE_COND_2, SAMPLE_COND_2, "104", "105", title="Chelsea")
    assert market.is_complete()


# ---------------------------------------------------------------------------
# 4. Multi-Leg Coherence Checker Tests
# ---------------------------------------------------------------------------
def _build_3way_market() -> NegRiskMarket:
    market = NegRiskMarket(
        event_id=SAMPLE_EVENT_ID,
        market_id=SAMPLE_MARKET_ID,
        title="Liverpool vs Man City 1X2",
        category="sports",
        k=3,
        fee_rate=0.03,
        fee_exponent=1.0,
    )
    for i, cond in enumerate([SAMPLE_COND_0, SAMPLE_COND_1, SAMPLE_COND_2]):
        market.legs[i] = OutcomeLeg(
            index=i,
            condition_id=cond,
            question_id=cond,
            yes_token_id=f"20{i*2}",
            no_token_id=f"20{i*2+1}",
            fee_rate=0.03,
            fee_exponent=1.0,
        )
    return market


def test_buy_all_no_arbitrage_detected():
    market = _build_3way_market()

    # k = 3. Guaranteed payout for NO bundle = 3 - 1 = 2.000 USDC
    # Ask NO prices: 0.60, 0.65, 0.61 -> sum = 1.860 USDC (< 2.000)
    # Gross edge = 2.000 - 1.860 = 0.140 USDC / share
    market.legs[0].best_ask_no = 0.60
    market.legs[0].ask_size_no = 150.0

    market.legs[1].best_ask_no = 0.65
    market.legs[1].ask_size_no = 100.0  # bottleneck leg

    market.legs[2].best_ask_no = 0.61
    market.legs[2].ask_size_no = 200.0

    # Ensure quotes are fresh
    now = time.time()
    for leg in market.legs.values():
        leg.last_update_ts = now

    checker = CoherenceChecker(min_net_edge_usd=0.01, min_net_edge_bps=10.0)
    opps = checker.scan_market(
        market=market,
        pol_usd_price=0.10,
        gas_price_gwei=40.0,
    )

    no_opps = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_NO]
    assert len(no_opps) == 1
    opp = no_opps[0]

    assert opp.k_legs == 3
    assert opp.shares == 100.0  # Limited by leg 1 ask_size_no
    assert pytest.approx(opp.gross_cost_per_share, abs=1e-5) == 1.860
    assert opp.gross_payout_per_share == 2.000
    assert pytest.approx(opp.gross_edge_per_share, abs=1e-5) == 0.140
    assert pytest.approx(opp.gross_edge_usd, abs=1e-3) == 14.00

    # Check fee deduction:
    # leg 0: 100 * 0.03 * (0.6 * 0.4) = 0.72000
    # leg 1: 100 * 0.03 * (0.65 * 0.35) = 0.68250
    # leg 2: 100 * 0.03 * (0.61 * 0.39) = 0.71370
    expected_fees = 0.72000 + 0.68250 + 0.71370
    assert pytest.approx(opp.total_taker_fees_usd, abs=1e-4) == expected_fees

    # Net edge = gross_edge - fees - gas
    assert opp.net_edge_usd > 10.0
    assert opp.net_margin_pct > 5.0
    assert len(opp.legs_execution) == 3
    assert all(leg.side == "BUY" and leg.outcome_type == "NO" for leg in opp.legs_execution)
    # Converted index set: indices [0, 1] -> (1<<0) | (1<<1) = 3 (0b011)
    assert opp.convert_index_set == 3


def test_buy_all_yes_arbitrage_detected():
    market = _build_3way_market()

    # k = 3. Guaranteed payout for YES bundle = 1.000 USDC
    # Ask YES prices: 0.30, 0.32, 0.33 -> sum = 0.950 USDC (< 1.000)
    # Gross edge = 1.000 - 0.950 = 0.050 USDC / share
    market.legs[0].best_ask_yes = 0.30
    market.legs[0].ask_size_yes = 200.0

    market.legs[1].best_ask_yes = 0.32
    market.legs[1].ask_size_yes = 50.0  # bottleneck

    market.legs[2].best_ask_yes = 0.33
    market.legs[2].ask_size_yes = 300.0

    now = time.time()
    for leg in market.legs.values():
        leg.last_update_ts = now

    checker = CoherenceChecker(min_net_edge_usd=0.01, min_net_edge_bps=10.0)
    opps = checker.scan_market(market=market)

    yes_opps = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_YES]
    assert len(yes_opps) == 1
    opp = yes_opps[0]

    assert opp.shares == 50.0
    assert pytest.approx(opp.gross_cost_per_share, abs=1e-5) == 0.950
    assert opp.gross_payout_per_share == 1.000
    assert pytest.approx(opp.gross_edge_per_share, abs=1e-5) == 0.050
    assert pytest.approx(opp.gross_edge_usd, abs=1e-3) == 2.50
    assert len(opp.legs_execution) == 3
    assert all(leg.side == "BUY" and leg.outcome_type == "YES" for leg in opp.legs_execution)


def test_convert_no_to_yes_arbitrage_detected():
    market = _build_3way_market()

    # Target outcome: Chelsea (leg 2)
    # k = 3 -> k - 2 = 1.000 USDC
    # Bid YES Chelsea = 0.38 (size = 80)
    # Payout = 1.000 + 0.38 = 1.380 USDC
    # Ask NO Arsenal (leg 0) = 0.65 (size = 120)
    # Ask NO Draw (leg 1) = 0.68 (size = 80)
    # Sum Ask NO = 0.65 + 0.68 = 1.330 USDC (< 1.380)
    # Gross edge = 1.380 - 1.330 = 0.050 USDC / share
    market.legs[0].best_ask_no = 0.65
    market.legs[0].ask_size_no = 120.0

    market.legs[1].best_ask_no = 0.68
    market.legs[1].ask_size_no = 80.0

    market.legs[2].best_bid_yes = 0.38
    market.legs[2].bid_size_yes = 100.0

    now = time.time()
    for leg in market.legs.values():
        leg.last_update_ts = now

    checker = CoherenceChecker(min_net_edge_usd=0.01, min_net_edge_bps=10.0)
    opps = checker.scan_market(
        market=market,
        pol_usd_price=0.10,
        gas_price_gwei=40.0,
    )

    conv_opps = [o for o in opps if o.opportunity_type == OpportunityType.CONVERT_NO_TO_YES]
    assert len(conv_opps) >= 1
    opp = [o for o in conv_opps if o.target_outcome_index == 2][0]

    assert opp.shares == 80.0  # min(120, 80, 100)
    assert pytest.approx(opp.gross_cost_per_share, abs=1e-5) == 1.330
    assert pytest.approx(opp.gross_payout_per_share, abs=1e-5) == 1.380
    assert pytest.approx(opp.gross_edge_per_share, abs=1e-5) == 0.050
    assert opp.target_outcome_index == 2
    # Converted index set: indices [0, 1] -> (1<<0) | (1<<1) = 3 (0b011)
    assert opp.convert_index_set == 3

    # Check legs: 2 BUY NO (legs 0 and 1) + 1 SELL YES (leg 2)
    buy_legs = [l for l in opp.legs_execution if l.side == "BUY"]
    sell_legs = [l for l in opp.legs_execution if l.side == "SELL"]
    assert len(buy_legs) == 2
    assert len(sell_legs) == 1
    assert sell_legs[0].leg_index == 2
    assert sell_legs[0].outcome_type == "YES"


def test_stale_quotes_rejected():
    market = _build_3way_market()
    market.legs[0].best_ask_no = 0.60
    market.legs[0].ask_size_no = 100.0
    market.legs[1].best_ask_no = 0.60
    market.legs[1].ask_size_no = 100.0
    market.legs[2].best_ask_no = 0.60
    market.legs[2].ask_size_no = 100.0

    # Leg 1 is 120 seconds old (> 60s max age)
    now = time.time()
    market.legs[0].last_update_ts = now
    market.legs[1].last_update_ts = now - 120.0
    market.legs[2].last_update_ts = now

    checker = CoherenceChecker(max_quote_age_s=60.0)
    opps = checker.scan_market(market=market)
    assert len(opps) == 0  # Stale leg must prevent blind execution


def test_incomplete_market_rejected():
    market = _build_3way_market()
    del market.legs[2]  # Remove 3rd leg

    checker = CoherenceChecker()
    opps = checker.scan_market(market=market)
    assert len(opps) == 0


def test_coherent_market_produces_no_opportunity():
    market = _build_3way_market()
    # Market sums strictly exceed bounds
    market.legs[0].best_ask_no = 0.70
    market.legs[0].ask_size_no = 100.0
    market.legs[1].best_ask_no = 0.70
    market.legs[1].ask_size_no = 100.0
    market.legs[2].best_ask_no = 0.70
    market.legs[2].ask_size_no = 100.0

    market.legs[0].best_ask_yes = 0.35
    market.legs[0].ask_size_yes = 100.0
    market.legs[1].best_ask_yes = 0.35
    market.legs[1].ask_size_yes = 100.0
    market.legs[2].best_ask_yes = 0.35
    market.legs[2].ask_size_yes = 100.0

    now = time.time()
    for leg in market.legs.values():
        leg.last_update_ts = now

    checker = CoherenceChecker()
    opps = checker.scan_market(market=market)
    assert len(opps) == 0


# ---------------------------------------------------------------------------
# 5. Conversion Planner & ABI Calldata Tests
# ---------------------------------------------------------------------------
def test_conversion_planner_buy_all_no():
    market = _build_3way_market()
    market.legs[0].best_ask_no = 0.60
    market.legs[0].ask_size_no = 50.0
    market.legs[1].best_ask_no = 0.60
    market.legs[1].ask_size_no = 50.0
    market.legs[2].best_ask_no = 0.60
    market.legs[2].ask_size_no = 50.0

    now = time.time()
    for leg in market.legs.values():
        leg.last_update_ts = now

    checker = CoherenceChecker(min_net_edge_usd=0.01)
    opps = checker.scan_market(market=market)
    opp = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_NO][0]

    plan = ConversionPlanner.create_plan(opportunity=opp, market=market, order_type="IOC")
    assert plan.requires_on_chain_conversion is True
    assert len(plan.clob_orders) == 3
    assert len(plan.on_chain_calls) == 2

    # Step 1 on-chain call: convertPositions(bytes32,uint256,uint256)
    convert_call = plan.on_chain_calls[0]
    assert convert_call.contract_address == NEGRISK_ADAPTER_ADDRESS
    assert convert_call.function_name == "convertPositions"
    assert convert_call.calldata.startswith("0x" + NEGRISK_CONVERT_SELECTOR)
    # Calldata layout: selector (8 hex chars) + marketId (64 hex chars) + indexSet (64 hex chars) + amount (64 hex chars)
    # len("0x" + 8 + 64*3) = 2 + 8 + 192 = 202
    assert len(convert_call.calldata) == 202

    # Step 2 on-chain call: mergePositions(bytes32,uint256)
    merge_call = plan.on_chain_calls[1]
    assert merge_call.contract_address == NEGRISK_ADAPTER_ADDRESS
    assert merge_call.function_name == "mergePositions"
    assert merge_call.calldata.startswith("0x" + NEGRISK_MERGE_SELECTOR)
    # len("0x" + 8 + 64*2) = 2 + 8 + 128 = 138
    assert len(merge_call.calldata) == 138


def test_conversion_planner_buy_all_yes():
    market = _build_3way_market()
    market.legs[0].best_ask_yes = 0.30
    market.legs[0].ask_size_yes = 50.0
    market.legs[1].best_ask_yes = 0.30
    market.legs[1].ask_size_yes = 50.0
    market.legs[2].best_ask_yes = 0.30
    market.legs[2].ask_size_yes = 50.0

    now = time.time()
    for leg in market.legs.values():
        leg.last_update_ts = now

    checker = CoherenceChecker(min_net_edge_usd=0.01)
    opps = checker.scan_market(market=market)
    opp = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_YES][0]

    plan = ConversionPlanner.create_plan(opportunity=opp, market=market)
    assert plan.requires_on_chain_conversion is False
    assert len(plan.on_chain_calls) == 0
    assert len(plan.clob_orders) == 3


# ---------------------------------------------------------------------------
# 6. Full Engine Lifecycle & Dry-Run Execution Tests
# ---------------------------------------------------------------------------
def test_negrisk_engine_full_dry_run_pipeline():
    engine = NegRiskEngine(
        min_net_edge_usd=0.01,
        min_net_edge_bps=10.0,
        dry_run=True,
    )

    # 1. Register 3-way sports market (Real Madrid vs Barcelona 1X2)
    engine.register_market(
        event_id=SAMPLE_EVENT_ID,
        market_id=SAMPLE_MARKET_ID,
        k=3,
        title="Real Madrid vs Barcelona 1X2",
        category="sports",
        fee_rate=0.05,
    )
    engine.register_leg(SAMPLE_EVENT_ID, 0, SAMPLE_COND_0, SAMPLE_COND_0, "500", "501", title="Real Madrid")
    engine.register_leg(SAMPLE_EVENT_ID, 1, SAMPLE_COND_1, SAMPLE_COND_1, "502", "503", title="Draw")
    engine.register_leg(SAMPLE_EVENT_ID, 2, SAMPLE_COND_2, SAMPLE_COND_2, "504", "505", title="Barcelona")

    # 2. Update quotes with significant negative risk dislocation:
    # Ask NO prices: 0.58, 0.62, 0.60 -> sum = 1.80 < 2.00
    now = time.time()
    engine.update_leg_quotes(SAMPLE_EVENT_ID, 0, best_ask_no=0.58, ask_size_no=200.0, timestamp=now)
    engine.update_leg_quotes(SAMPLE_EVENT_ID, 1, best_ask_no=0.62, ask_size_no=150.0, timestamp=now)
    engine.update_leg_quotes(SAMPLE_EVENT_ID, 2, best_ask_no=0.60, ask_size_no=180.0, timestamp=now)

    # 3. Scan all markets
    opps = engine.scan_all()
    assert len(opps) >= 1

    best_opp = opps[0]
    assert best_opp.opportunity_type == OpportunityType.BUY_ALL_NO
    assert best_opp.shares == 150.0

    # 4. Plan execution
    plan = engine.plan_execution(best_opp)
    assert plan.status == "PENDING"
    assert len(plan.clob_orders) == 3
    assert len(plan.on_chain_calls) == 2

    # 5. Execute in dry-run mode
    res = engine.execute_plan(plan)
    assert res.success is True
    assert res.is_dry_run is True
    assert res.status == "SUCCESS"
    assert len(res.orders_placed) == 3
    assert len(res.tx_hashes) == 2
    assert plan.status == "SIMULATED_SUCCESS"


# ---------------------------------------------------------------------------
# 7. Multi-Level Book Depth Sweeping Tests
# ---------------------------------------------------------------------------
def test_sweep_depth_bundle_multilevel():
    market = _build_3way_market()
    # Configure multi-level depth on each leg:
    # Leg 0: 50 @ 0.58, 100 @ 0.60, 200 @ 0.65
    # Leg 1: 80 @ 0.60, 80 @ 0.62, 100 @ 0.66
    # Leg 2: 60 @ 0.59, 120 @ 0.61, 150 @ 0.64
    from polymarketbot.trading.negrisk_engine import OrderBookDepth, OrderBookLevel

    market.legs[0].depth_no = OrderBookDepth(
        asks=[OrderBookLevel(0.58, 50.0), OrderBookLevel(0.60, 100.0), OrderBookLevel(0.65, 200.0)]
    )
    market.legs[1].depth_no = OrderBookDepth(
        asks=[OrderBookLevel(0.60, 80.0), OrderBookLevel(0.62, 80.0), OrderBookLevel(0.66, 100.0)]
    )
    market.legs[2].depth_no = OrderBookDepth(
        asks=[OrderBookLevel(0.59, 60.0), OrderBookLevel(0.61, 120.0), OrderBookLevel(0.64, 150.0)]
    )

    now = time.time()
    for leg in market.legs.values():
        leg.last_update_ts = now

    checker = CoherenceChecker(min_net_edge_usd=0.01, min_net_edge_bps=10.0)

    # Sweep for 100 shares (which exceeds top-of-book size of 50 on leg 0)
    opp = checker.sweep_depth_bundle(
        market=market,
        opportunity_type=OpportunityType.BUY_ALL_NO,
        target_shares=100.0,
        gas_price_gwei=40.0,
        pol_usd_price=0.10,
    )

    assert opp is not None
    assert opp.shares == 100.0
    # Leg 0 fills 50 @ 0.58 + 50 @ 0.60 -> cost = 29.0 + 30.0 = 59.0 -> VWAP = 0.5900
    assert pytest.approx(opp.legs_execution[0].price, abs=1e-3) == 0.5900
    # Leg 1 fills 80 @ 0.60 + 20 @ 0.62 -> cost = 48.0 + 12.4 = 60.4 -> VWAP = 0.6040
    assert pytest.approx(opp.legs_execution[1].price, abs=1e-3) == 0.6040
    # Leg 2 fills 60 @ 0.59 + 40 @ 0.61 -> cost = 35.4 + 24.4 = 59.8 -> VWAP = 0.5980
    assert pytest.approx(opp.legs_execution[2].price, abs=1e-3) == 0.5980

    # Total VWAP sum = 0.5900 + 0.6040 + 0.5980 = 1.7920 < 2.000
    assert opp.gross_cost_per_share < 1.80
    assert opp.net_edge_usd > 15.0


# ---------------------------------------------------------------------------
# 8. Empirical Historical Vectors from negrisk_cells.parquet
# ---------------------------------------------------------------------------
def test_historical_tape_vector_event_363207():
    """Event 363207 from negrisk_cells.parquet: 12.0% net margin opportunity."""
    market = NegRiskMarket(
        event_id="363207",
        market_id=SAMPLE_MARKET_ID,
        title="Historical Tape Match 363207",
        category="sports",
        k=3,
        fee_rate=0.03,
        fee_exponent=1.0,
    )
    # Total ask NO = 1.860 (gross = 0.140)
    # e.g. 0.60, 0.62, 0.64
    prices = [0.60, 0.62, 0.64]
    for i, px in enumerate(prices):
        market.legs[i] = OutcomeLeg(
            index=i,
            condition_id=f"0x{i+1:02x}" * 32,
            question_id=f"0x{i+1:02x}" * 32,
            yes_token_id=f"10{i}",
            no_token_id=f"20{i}",
            best_ask_no=px,
            ask_size_no=100.0,
            fee_rate=0.03,
            fee_exponent=1.0,
            last_update_ts=time.time(),
        )

    checker = CoherenceChecker(min_net_edge_usd=0.01)
    opps = checker.scan_market(market=market, pol_usd_price=0.09, gas_price_gwei=35.0)

    no_opps = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_NO]
    assert len(no_opps) == 1
    opp = no_opps[0]

    # Gross edge = 2.0 - 1.86 = 0.140 / share
    assert pytest.approx(opp.gross_edge_per_share, abs=1e-4) == 0.140
    # Fee ~ 0.020 per share (matching negrisk_cells.parquet fee of 0.020052)
    fee_per_share = opp.total_taker_fees_usd / opp.shares
    assert pytest.approx(fee_per_share, abs=0.005) == 0.020
    # Net edge ~ 0.120 per share
    net_per_share = opp.net_edge_usd / opp.shares
    assert pytest.approx(net_per_share, abs=0.005) == 0.120


def test_historical_tape_vector_event_496282():
    """Event 496282 from negrisk_cells.parquet: 5.5% net margin opportunity."""
    market = NegRiskMarket(
        event_id="496282",
        market_id=SAMPLE_MARKET_ID,
        title="Historical Tape Match 496282",
        category="sports",
        k=3,
        fee_rate=0.03,
        fee_exponent=1.0,
    )
    # Total ask NO = 1.931 (gross = 0.069)
    prices = [0.640, 0.641, 0.650]
    fee_rates = [0.03, 0.03, 0.0]  # Two fee-enabled legs, one fee-free in tape
    for i, px in enumerate(prices):
        market.legs[i] = OutcomeLeg(
            index=i,
            condition_id=f"0x{i+1:02x}" * 32,
            question_id=f"0x{i+1:02x}" * 32,
            yes_token_id=f"30{i}",
            no_token_id=f"40{i}",
            best_ask_no=px,
            ask_size_no=100.0,
            fee_rate=fee_rates[i],
            fee_exponent=1.0,
            last_update_ts=time.time(),
        )

    checker = CoherenceChecker(min_net_edge_usd=0.01)
    opps = checker.scan_market(market=market, pol_usd_price=0.09, gas_price_gwei=35.0)

    no_opps = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_NO]
    assert len(no_opps) == 1
    opp = no_opps[0]

    # Gross edge = 2.0 - 1.931 = 0.069 / share
    assert pytest.approx(opp.gross_edge_per_share, abs=1e-4) == 0.069
    # Net edge ~ 0.055 per share (matching negrisk_cells.parquet net of 0.055471)
    net_per_share = opp.net_edge_usd / opp.shares
    assert pytest.approx(net_per_share, abs=0.005) == 0.055

