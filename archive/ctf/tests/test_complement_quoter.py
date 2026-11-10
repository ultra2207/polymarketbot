"""Comprehensive Unit Tests for Asynchronous Complement Quoter & Auto-Merger.

Validates:
1. Double-Quoting Engine & Mid Sum Guard (Mid_YES + Mid_NO <= 0.99).
2. Net Complement Inventory Tracking (Paired units C = min(pos_YES, pos_NO), imbalance q = pos_YES - pos_NO).
3. Dynamic Complement Skewing (Boosting the pair-completer leg, fading the excess leg).
4. Automated CTF Complete-Set Merger Trigger (Threshold >= 100 shares, carry calculation, invariant q).
5. Margin & Arbitrage Carry Enforcement (Bid_YES + Bid_NO <= 1.00 - margin).
6. Integration with merger.py CompleteSetPosition, build_ctf_merge_transaction, and PolygonCtfClient.
7. StrategyModule protocol conformance.
"""

from __future__ import annotations

import pytest

from archive.ctf.merger import CompleteSetPosition
from archive.ctf.strategy_types import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    StrategyModule,
)
from archive.ctf.complement_quoter import (
    ComplementMergeRecord,
    ComplementQuoteDecision,
    ComplementQuoter,
    ComplementQuoterConfig,
    LegQuote,
    round_tick,
)
from polymarketbot.trading.ctf import PolygonCtfClient


@pytest.fixture
def default_config() -> ComplementQuoterConfig:
    return ComplementQuoterConfig(
        max_mid_sum=0.99,
        target_pair_margin=0.015,  # 1.5c margin -> max combined bid <= 0.985
        spread_offset_cents=0.50,  # 0.5c = 0.005
        base_quote_size=20.0,
        min_quote_size=5.0,
        max_quote_size=100.0,
        skew_scale_shares=50.0,
        max_skew_multiplier=2.5,
        min_fade_multiplier=0.2,
        max_directional_imbalance=100.0,
        aggressiveness_cents=0.30,
        fade_retreat_cents=0.30,
        auto_merge_threshold=100.0,
        gas_fee_per_merge=0.0016,
    )


@pytest.fixture
def quoter(default_config: ComplementQuoterConfig) -> ComplementQuoter:
    return ComplementQuoter(
        condition_id="0xcond_complement_12345",
        config=default_config,
    )


# =============================================================================
# 1. Double-Quoting Engine & Mid Sum Bound
# =============================================================================

def test_double_quoting_when_mid_sum_favorable(quoter: ComplementQuoter) -> None:
    """Mid_YES (0.48) + Mid_NO (0.49) = 0.97 <= 0.99 -> Double quote active."""
    decision = quoter.compute_quotes(mid_yes=0.48, mid_no=0.49)

    assert decision.can_quote_double_bids is True
    assert decision.mid_sum == pytest.approx(0.97)
    assert decision.yes_quote.is_quoted is True
    assert decision.no_quote.is_quoted is True

    # Check sizing: neutral inventory -> base quote size
    assert decision.yes_quote.bid_size == 20.0
    assert decision.no_quote.bid_size == 20.0

    # Check price carry: combined cost <= 0.985 (1.5c carry locked in)
    assert decision.expected_combined_cost <= 0.985
    assert decision.expected_pair_carry >= 0.015
    assert decision.expected_carry_pct > 0.0


def test_double_quoting_withheld_when_mid_sum_exceeds_threshold(quoter: ComplementQuoter) -> None:
    """Mid_YES (0.505) + Mid_NO (0.505) = 1.01 > 0.99 -> Double quote withheld."""
    decision = quoter.compute_quotes(mid_yes=0.505, mid_no=0.505)

    assert decision.can_quote_double_bids is False
    assert decision.mid_sum == pytest.approx(1.01)
    assert decision.yes_quote.is_quoted is False
    assert decision.no_quote.is_quoted is False
    assert "max allowed" in decision.reason


def test_pair_margin_enforcement_clips_bids(quoter: ComplementQuoter) -> None:
    """Ensure that Bid_YES + Bid_NO <= 1.000 - target_pair_margin is strictly enforced."""
    # Suppose mids are 0.495 and 0.495 (sum = 0.99 <= 0.99)
    decision = quoter.compute_quotes(mid_yes=0.495, mid_no=0.495)

    assert decision.can_quote_double_bids is True
    # Combined bids must not exceed 1.000 - 0.015 = 0.985
    assert decision.expected_combined_cost <= 0.985 + 1e-6
    assert decision.expected_pair_carry >= 0.015 - 1e-6


# =============================================================================
# 2. Net Complement Inventory Tracking
# =============================================================================

def test_inventory_tracking_paired_units_and_imbalance(quoter: ComplementQuoter) -> None:
    """Verify paired units C = min(YES, NO) and net imbalance q = YES - NO."""
    assert quoter.paired_units == 0.0
    assert quoter.net_imbalance == 0.0

    # 1. Buy 50 YES @ 0.48
    quoter.record_fill("YES", "BUY", price=0.48, size=50.0)
    assert quoter.shares_yes == 50.0
    assert quoter.shares_no == 0.0
    assert quoter.paired_units == 0.0
    assert quoter.net_imbalance == 50.0
    assert quoter.avg_cost_yes == pytest.approx(0.48)
    assert quoter.usdc_cash == pytest.approx(-24.0)  # spent 50 * 0.48

    # 2. Buy 30 NO @ 0.49
    quoter.record_fill("NO", "BUY", price=0.49, size=30.0)
    assert quoter.shares_yes == 50.0
    assert quoter.shares_no == 30.0
    # Matched complete sets C = min(50, 30) = 30.0
    assert quoter.paired_units == 30.0
    # Net directional exposure q = 50 - 30 = +20.0
    assert quoter.net_imbalance == 20.0
    assert quoter.avg_cost_no == pytest.approx(0.49)
    assert quoter.usdc_cash == pytest.approx(-24.0 - 14.70)


# =============================================================================
# 3. Dynamic Complement Skewing
# =============================================================================

def test_dynamic_skew_when_long_yes(quoter: ComplementQuoter) -> None:
    """When long YES (q > 0), NO is the pair completer:

    - NO bid size scales UP (boosted).
    - YES bid size scales DOWN (faded).
    - NO price shifts closer to mid (aggressive).
    - YES price retreats away from mid (protective).
    """
    # Create inventory: 25 shares of YES, 0 shares of NO -> q = +25
    quoter.record_fill("YES", "BUY", price=0.48, size=25.0)
    assert quoter.net_imbalance == 25.0

    decision = quoter.compute_quotes(mid_yes=0.48, mid_no=0.49)

    # NO is the pair-completer:
    assert decision.no_quote.is_skew_boosted is True
    assert decision.no_quote.is_skew_faded is False
    assert decision.no_quote.bid_size > quoter.config.base_quote_size  # > 20.0

    # YES is the excess leg:
    assert decision.yes_quote.is_skew_faded is True
    assert decision.yes_quote.is_skew_boosted is False
    assert decision.yes_quote.bid_size < quoter.config.base_quote_size  # < 20.0

    # Intensity for q=25 / q_scale=50 is 0.5
    # Expected boost: 1.0 + (2.5 - 1.0) * 0.5 = 1.75 -> size = 20 * 1.75 = 35.0
    assert decision.no_quote.bid_size == pytest.approx(35.0)
    # Expected fade: 1.0 - 0.5 = 0.5 -> size = 20 * 0.5 = 10.0
    assert decision.yes_quote.bid_size == pytest.approx(10.0)


def test_dynamic_skew_when_long_no(quoter: ComplementQuoter) -> None:
    """When long NO (q < 0), YES is the pair completer:

    - YES bid size scales UP (boosted).
    - NO bid size scales DOWN (faded).
    """
    # Create inventory: 0 YES, 30 NO -> q = -30
    quoter.record_fill("NO", "BUY", price=0.49, size=30.0)
    assert quoter.net_imbalance == -30.0

    decision = quoter.compute_quotes(mid_yes=0.48, mid_no=0.49)

    assert decision.yes_quote.is_skew_boosted is True
    assert decision.yes_quote.bid_size > quoter.config.base_quote_size
    assert decision.no_quote.is_skew_faded is True
    assert decision.no_quote.bid_size < quoter.config.base_quote_size


def test_extreme_imbalance_pauses_excess_leg(quoter: ComplementQuoter) -> None:
    """When |q| >= max_directional_imbalance (100), the excess leg is paused.

    The pair-completer leg continues at max_quote_size to de-risk inventory.
    """
    quoter.record_fill("YES", "BUY", price=0.48, size=100.0)
    assert quoter.net_imbalance == 100.0

    decision = quoter.compute_quotes(mid_yes=0.48, mid_no=0.49)

    # YES (excess) is paused:
    assert decision.yes_quote.is_quoted is False
    assert decision.yes_quote.bid_size == 0.0

    # NO (completer) quotes max size:
    assert decision.no_quote.is_quoted is True
    assert decision.no_quote.bid_size == quoter.config.max_quote_size  # 100.0


# =============================================================================
# 4. Automated CTF Merger Trigger & Carry Calculation
# =============================================================================

def test_auto_merge_triggers_at_threshold_and_locks_carry(quoter: ComplementQuoter) -> None:
    """Validate retail asynchronous fills -> auto-merging 100 complete sets:

    - Buy 100 YES @ 0.48 ($48.00).
    - Buy 100 NO @ 0.49 ($49.00).
    - Threshold 100 reached -> auto-merge triggers!
    - Unlocks $100.00 USDC.
    - Combined cost: $97.00.
    - Realized carry: $100.00 - $97.00 - $0.0016 = $2.9984 (+3.0% spread carry).
    - Net inventory q remains invariant (0.0).
    """
    # 1. Fill 100 YES @ 0.48
    record1 = quoter.record_fill("YES", "BUY", price=0.48, size=100.0)
    assert record1 is None  # NO is 0 -> C=0 < 100 -> no merge
    assert quoter.shares_yes == 100.0
    assert quoter.shares_no == 0.0

    # 2. Fill 100 NO @ 0.49 -> C = min(100, 100) = 100 >= 100 -> Merge triggers!
    record2 = quoter.record_fill("NO", "BUY", price=0.49, size=100.0)
    assert record2 is not None
    assert isinstance(record2, ComplementMergeRecord)

    # Validate merge metrics
    assert record2.shares_merged == 100.0
    assert record2.usdc_unlocked == 100.0
    assert record2.cost_basis_merged == pytest.approx(97.00)
    assert record2.gas_fee == pytest.approx(0.0016)
    expected_carry = 100.00 - 97.00 - 0.0016  # 2.9984
    assert record2.realized_carry == pytest.approx(expected_carry)

    # Post-merge inventory reset
    assert quoter.shares_yes == 0.0
    assert quoter.shares_no == 0.0
    assert quoter.paired_units == 0.0
    assert quoter.net_imbalance == 0.0

    # Cash balance reflects unlocked $100 less purchases $97 less gas $0.0016
    assert quoter.usdc_cash == pytest.approx(2.9984)
    assert quoter.total_realized_carry == pytest.approx(2.9984)
    assert quoter.total_merged_shares == 100.0


def test_merge_invariance_of_net_imbalance(quoter: ComplementQuoter) -> None:
    """Directional net imbalance q = pos_YES - pos_NO is strictly invariant to merges:

    Initial: 120 YES, 100 NO -> q = +20
    Post-merge: 20 YES, 0 NO -> q = +20
    """
    quoter.record_fill("YES", "BUY", price=0.48, size=120.0)
    quoter.record_fill("NO", "BUY", price=0.49, size=100.0)

    # 100 sets merged
    assert len(quoter.merges_history) == 1
    merge = quoter.merges_history[0]
    assert merge.shares_merged == 100.0
    assert merge.remaining_yes == 20.0
    assert merge.remaining_no == 0.0
    assert merge.net_imbalance_q == 20.0

    assert quoter.shares_yes == 20.0
    assert quoter.shares_no == 0.0
    assert quoter.paired_units == 0.0
    assert quoter.net_imbalance == 20.0

    # Remaining cost basis on YES is exactly 20 * 0.48 = 9.60
    assert quoter.cost_basis_yes == pytest.approx(9.60)


def test_on_merge_callback_triggered(default_config: ComplementQuoterConfig) -> None:
    """Ensure on_merge_callback is executed upon merge."""
    captured_records: list[ComplementMergeRecord] = []

    def callback(record: ComplementMergeRecord) -> None:
        captured_records.append(record)

    q = ComplementQuoter(
        condition_id="0xcallback_test",
        config=default_config,
        on_merge_callback=callback,
    )

    q.record_fill("YES", "BUY", price=0.48, size=100.0)
    q.record_fill("NO", "BUY", price=0.49, size=100.0)

    assert len(captured_records) == 1
    assert captured_records[0].shares_merged == 100.0


# =============================================================================
# 5. Integration with merger.py & ctf.py
# =============================================================================

def test_to_complete_set_position_interoperability(quoter: ComplementQuoter) -> None:
    """Verify export to merger.py CompleteSetPosition."""
    quoter.record_fill("YES", "BUY", price=0.48, size=50.0)
    quoter.record_fill("NO", "BUY", price=0.49, size=30.0)

    csp = quoter.to_complete_set_position()
    assert isinstance(csp, CompleteSetPosition)
    assert csp.shares_yes == 50.0
    assert csp.shares_no == 30.0
    assert csp.mergeable_shares == 30.0
    assert csp.net_inventory == 20.0


def test_build_ctf_merge_transaction(quoter: ComplementQuoter) -> None:
    """Verify transaction payload generation for Polygon mergePositions."""
    quoter.record_fill("YES", "BUY", price=0.48, size=50.0)
    quoter.record_fill("NO", "BUY", price=0.49, size=30.0)

    tx = quoter.build_merge_transaction()
    assert tx["method"] == "mergePositions"
    assert tx["params"]["conditionId"] == quoter.condition_id
    assert tx["params"]["partition"] == [1, 2]
    assert tx["params"]["amount"] == 30_000_000  # 30 shares * 1e6


def test_polygon_ctf_client_dry_run_integration(default_config: ComplementQuoterConfig) -> None:
    """Verify integration with PolygonCtfClient in dry-run mode."""
    ctf_client = PolygonCtfClient(dry_run=True)
    q = ComplementQuoter(
        condition_id="0xctf_dry_run_test",
        config=default_config,
        ctf_client=ctf_client,
    )

    q.record_fill("YES", "BUY", price=0.48, size=100.0)
    record = q.record_fill("NO", "BUY", price=0.49, size=100.0)

    assert record is not None
    assert record.is_dry_run is True
    assert record.tx_hash.startswith("0xmock_tx_")


# =============================================================================
# 6. StrategyModule Protocol Compliance
# =============================================================================

def test_strategy_module_protocol_compliance(quoter: ComplementQuoter) -> None:
    """Verify ComplementQuoter implements the StrategyModule protocol."""
    assert isinstance(quoter, StrategyModule)
    assert quoter.name == "asynchronous-complement-quoter"

    market = MarketView(
        condition_id="0xcond_123",
        question="Will Event Happen?",
        rate_per_day=20.0,
        rewards_min_size=20.0,
        rewards_max_spread=3.0,
        tokens=("asset_yes_1", "asset_no_1"),
    )
    quoter.setup(market)
    assert quoter.condition_id == "0xcond_123"

    # Feed top of books
    book_yes = BookContext(
        condition_id="0xcond_123",
        asset_id="asset_yes_1",
        outcome="YES",
        mid=0.48,
        best_bid=0.47,
        best_ask=0.49,
        tick_size=0.01,
        spread_cents=2.0,
    )
    book_no = BookContext(
        condition_id="0xcond_123",
        asset_id="asset_no_1",
        outcome="NO",
        mid=0.49,
        best_bid=0.48,
        best_ask=0.50,
        tick_size=0.01,
        spread_cents=2.0,
    )
    quoter.on_book(book_yes)
    quoter.on_book(book_no)

    ctx = DecisionContext(
        book=book_yes,
        seconds_elapsed=10.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
    )

    action = quoter.decide(ctx)
    assert isinstance(action, SideActions)
    assert action.bid == "QUOTE"
    assert 0.0 <= action.bid_scale <= 1.0
    assert action.bid_price is not None
    assert 0.0 < action.bid_price < 1.0


def test_round_tick_helper() -> None:
    """Validate tick rounding helper."""
    assert round_tick(0.484, 0.01) == 0.48
    assert round_tick(0.486, 0.01) == 0.49
    assert round_tick(0.489, 0.01, mode="floor") == 0.48
    assert round_tick(0.481, 0.01, mode="ceil") == 0.49


# =============================================================================
# 7. Institutional Avellaneda-Stoikov / Guéant Pair Quoting & Execution Leg Risk
# =============================================================================

def test_avellaneda_stoikov_reservation_prices_and_gueant_spreads() -> None:
    """Validate Avellaneda-Stoikov reservation prices and Guéant-Tapia optimal pair spreads:

    1. When q = 0 (neutral):
       r_yes == mid_yes, r_no == mid_no.
       Optimal spread reflects Guéant-Tapia arrival intensity kappa and capital carrying cost.
    2. When q > 0 (long YES):
       delta_r(q) = q * gamma * sigma^2 * tau > 0
       r_yes shifts down (discourages buying more YES)
       r_no shifts up (eagerly bids for NO to neutralize delta)
    """
    config = ComplementQuoterConfig(
        use_avellaneda_stoikov=True,
        risk_aversion_gamma=0.2,
        pair_volatility=0.10,
        holding_horizon_tau=1.0,
        arrival_intensity_kappa=20.0,
        capital_carrying_cost_rate=0.05,
        target_pair_margin=0.015,
    )
    quoter = ComplementQuoter(condition_id="0xas_test", config=config)

    # 1. Neutral inventory (q = 0)
    decision_neutral = quoter.compute_quotes(mid_yes=0.48, mid_no=0.49)
    assert decision_neutral.can_quote_double_bids is True
    assert decision_neutral.reservation_price_yes == pytest.approx(0.48)
    assert decision_neutral.reservation_price_no == pytest.approx(0.49)
    assert decision_neutral.var_penalty_usd == 0.0
    assert decision_neutral.optimal_pair_spread > 0.0

    # 2. Long YES inventory (q = +30)
    quoter.record_fill("YES", "BUY", price=0.48, size=30.0)
    assert quoter.net_imbalance == 30.0

    decision_skewed = quoter.compute_quotes(mid_yes=0.48, mid_no=0.49, best_ask_no=0.51)
    # Expected delta_r = 30 * 0.2 * (0.10^2) * 1.0 = 30 * 0.2 * 0.01 = 0.06
    # r_yes = 0.48 - 0.5 * 0.06 = 0.450
    # r_no = 0.49 + 0.5 * 0.06 = 0.520
    assert decision_skewed.reservation_price_yes == pytest.approx(0.450)
    assert decision_skewed.reservation_price_no == pytest.approx(0.520)

    # Reservation price on YES dropped; on NO rose
    assert decision_skewed.reservation_price_yes < decision_neutral.reservation_price_yes
    assert decision_skewed.reservation_price_no > decision_neutral.reservation_price_no


def test_execution_leg_risk_asymmetric_fill_unhedging_and_var_penalty() -> None:
    """Validate instantaneous Value-at-Risk (VaR) adverse selection penalty on hanging unhedged leg."""
    config = ComplementQuoterConfig(
        use_avellaneda_stoikov=True,
        risk_aversion_gamma=0.2,
        pair_volatility=0.15,
        holding_horizon_tau=1.0,
        arrival_intensity_kappa=10.0,
        unhedge_time_horizon_seconds=120.0,
        var_confidence_z=1.96,
        base_quote_size=20.0,
        max_quote_size=100.0,
        min_quote_size=5.0,
    )
    quoter = ComplementQuoter(condition_id="0xleg_risk_test", config=config)

    # Create unhedged directional inventory: 50 YES filled
    quoter.record_fill("YES", "BUY", price=0.48, size=50.0)
    assert quoter.net_imbalance == 50.0

    decision = quoter.compute_quotes(
        mid_yes=0.48,
        mid_no=0.49,
        best_bid_yes=0.47,
        best_ask_yes=0.49,
        best_bid_no=0.48,
        best_ask_no=0.50,
    )

    # VaR penalty must be positive for unhedged 50 shares
    assert decision.var_penalty_usd > 0.0

    # NO is the pair completer: boosted size & tightened towards ask
    assert decision.no_quote.is_skew_boosted is True
    assert decision.no_quote.bid_size > 20.0
    assert decision.yes_quote.is_skew_faded is True
    assert decision.yes_quote.bid_size < 20.0

    # Pair margin bound is strictly maintained
    assert decision.expected_combined_cost <= (1.000 - config.target_pair_margin)


# =============================================================================
# 8. Dynamic Economic Order Quantity (EOQ) Batch Merging
# =============================================================================

def test_dynamic_eoq_batch_merging_balances_gas_and_carrying_cost() -> None:
    """Validate batch size for CTF.mergePositions() as exact Economic Order Quantity (EOQ):

    Q* = sqrt((2 * S * D) / H)
    Where S is dynamic Polygon PoS gas cost, D is arrival rate, H is carrying cost.
    """
    config = ComplementQuoterConfig(
        use_eoq_batching=True,
        merge_gas_units=78_000,
        gas_price_gwei=40.0,       # 40 Gwei
        pol_usd_price=0.20,         # $0.20 POL
        complete_set_arrival_rate=10_000.0,  # 10k sets/year
        capital_carrying_cost_rate=0.05,     # 5% capital cost
        max_gas_friction_cents_per_pair=0.05,# 0.05c/share hurdle
    )
    quoter = ComplementQuoter(condition_id="0xeoq_test", config=config)

    # Expected dynamic gas cost:
    # 78,000 * 40e-9 POL * $0.20 = 0.000624 USD
    expected_gas_usd = quoter.compute_dynamic_gas_cost_usd()
    assert expected_gas_usd == pytest.approx(0.000624, abs=1e-6)

    # Optimal EOQ batch size:
    # S = 0.000624, D = 10,000, H = 0.05 * 0.98 = 0.049
    # Q* = sqrt((2 * 0.000624 * 10,000) / 0.049) = sqrt(12.48 / 0.049) = sqrt(254.69) ~ 15.96
    # Hurdle check: 0.000624 / 0.0005 = 1.25 shares
    # Optimal Q = max(1.25, 15.96) = 15.96 shares
    eoq = quoter.compute_optimal_eoq_batch_size()
    assert eoq == pytest.approx(15.96, abs=0.1)

    # Verify auto_merge_threshold dynamically reflects EOQ
    assert quoter.auto_merge_threshold == eoq

    # Case B: Gas spikes 5x to 200 Gwei -> Batch size must scale up to amortize gas!
    config_high_gas = ComplementQuoterConfig(
        use_eoq_batching=True,
        merge_gas_units=78_000,
        gas_price_gwei=200.0,      # 200 Gwei
        pol_usd_price=0.20,
        complete_set_arrival_rate=10_000.0,
        capital_carrying_cost_rate=0.05,
    )
    quoter_high_gas = ComplementQuoter(condition_id="0xeoq_high_gas", config=config_high_gas)
    eoq_high_gas = quoter_high_gas.compute_optimal_eoq_batch_size()
    assert eoq_high_gas > eoq
    # sqrt(5) ~ 2.236x higher
    assert eoq_high_gas == pytest.approx(eoq * (200.0 / 40.0) ** 0.5, abs=0.5)
