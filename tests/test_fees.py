"""Fee model tests.

The core test is that the formula reproduces the published fee tables. Those
tables are displayed to cents, so the assertion tolerance is +/- 0.005 USDC per
100 shares.
"""

from __future__ import annotations

import pytest

from polymarketbot.fees import (
    FeeSchedule,
    era_schedule,
    fee_category,
    fee_curve_peak_rate,
    maker_rebate,
    taker_fee,
)

CRYPTO = FeeSchedule(rate=0.07, rebate_rate=0.20)
SPORTS = FeeSchedule(rate=0.05, rebate_rate=0.15)
LOW = FeeSchedule(rate=0.04, rebate_rate=0.25)

# price -> fee in USDC for 100 shares, straight from docs.polymarket.com/trading/fees
CRYPTO_TABLE = {
    0.01: 0.07, 0.05: 0.33, 0.10: 0.63, 0.15: 0.89, 0.20: 1.12, 0.25: 1.31,
    0.30: 1.47, 0.35: 1.59, 0.40: 1.68, 0.45: 1.73, 0.50: 1.75, 0.55: 1.73,
    0.60: 1.68, 0.65: 1.59, 0.70: 1.47, 0.75: 1.31, 0.80: 1.12, 0.85: 0.89,
    0.90: 0.63, 0.95: 0.33, 0.99: 0.07,
}
SPORTS_TABLE = {
    0.01: 0.05, 0.05: 0.24, 0.10: 0.45, 0.15: 0.64, 0.20: 0.80, 0.25: 0.94,
    0.30: 1.05, 0.35: 1.14, 0.40: 1.20, 0.45: 1.24, 0.50: 1.25, 0.55: 1.24,
    0.60: 1.20, 0.65: 1.14, 0.70: 1.05, 0.75: 0.94, 0.80: 0.80, 0.85: 0.64,
    0.90: 0.45, 0.95: 0.24, 0.99: 0.05,
}
LOW_TABLE = {
    0.01: 0.04, 0.05: 0.19, 0.10: 0.36, 0.15: 0.51, 0.20: 0.64, 0.25: 0.75,
    0.30: 0.84, 0.35: 0.91, 0.40: 0.96, 0.45: 0.99, 0.50: 1.00, 0.55: 0.99,
    0.60: 0.96, 0.65: 0.91, 0.70: 0.84, 0.75: 0.75, 0.80: 0.64, 0.85: 0.51,
    0.90: 0.36, 0.95: 0.19, 0.99: 0.04,
}


@pytest.mark.parametrize("price,expected", CRYPTO_TABLE.items())
def test_crypto_table(price, expected):
    assert abs(taker_fee(100, price, CRYPTO) - expected) <= 0.005


@pytest.mark.parametrize("price,expected", SPORTS_TABLE.items())
def test_sports_table(price, expected):
    assert abs(taker_fee(100, price, SPORTS) - expected) <= 0.005


@pytest.mark.parametrize("price,expected", LOW_TABLE.items())
def test_low_rate_table(price, expected):
    assert abs(taker_fee(100, price, LOW) - expected) <= 0.005


@pytest.mark.parametrize("price", [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99])
def test_fee_is_symmetric_around_50c(price):
    assert taker_fee(100, price, CRYPTO) == pytest.approx(
        taker_fee(100, 1 - price, CRYPTO), abs=1e-9
    )


def test_peak_matches_published_percentages():
    # current crypto rate: 0.07 * 0.25 = 1.75% per share at p=0.5
    assert fee_curve_peak_rate(0.07) == pytest.approx(0.0175)
    # 2026 pilot: rate 0.25 with exponent 2 -> 0.25 * 0.0625 = 1.5625%
    assert fee_curve_peak_rate(0.25, 2.0) == pytest.approx(0.015625)


def test_maker_pays_nothing_by_construction():
    # There is deliberately no maker fee function; rebates are the only maker flow.
    assert maker_rebate(1.75, CRYPTO) == pytest.approx(0.35)
    assert maker_rebate(1.25, SPORTS) == pytest.approx(0.1875)


def test_tiny_fees_round_to_zero():
    # 0.0001 shares at 50c: 0.00000175 -> rounds to 0.00000
    assert taker_fee(0.0001, 0.5, CRYPTO) == 0.0


def test_invalid_price_rejected():
    with pytest.raises(ValueError):
        taker_fee(100, 1.2, CRYPTO)


def test_era_before_fees_is_free():
    assert era_schedule("crypto", "2024-06-01").rate == 0.0
    assert era_schedule("sports", "2024-06-01").rate == 0.0


def test_era_crypto_pilot_uses_rate_and_exponent():
    s = era_schedule("crypto", "2026-02-01")
    assert s.rate == pytest.approx(0.25)
    assert s.exponent == pytest.approx(2.0)
    assert era_schedule("sports", "2026-02-01").rate == 0.0


def test_pilot_curve_shape_differs_from_exponent_one():
    # Same peak at p=0.5, very different curve away from the centre.
    pilot = era_schedule("crypto", "2026-02-01")
    assert taker_fee(100, 0.5, pilot) == pytest.approx(1.5625, abs=1e-6)
    # 100 * 0.25 * (0.1 * 0.9)^2 = 0.2025
    assert taker_fee(100, 0.1, pilot) == pytest.approx(0.2025)
    # An exponent-1 schedule with the same peak would charge 0.5625 here.
    assert taker_fee(100, 0.1, FeeSchedule(rate=0.0625)) == pytest.approx(0.5625)


def test_era_sports_rate_change_on_july_10():
    before = era_schedule("sports", "2026-07-09")
    after = era_schedule("sports", "2026-07-11")
    assert before.rate == pytest.approx(0.03)
    assert before.rebate_rate == pytest.approx(0.25)
    assert after.rate == pytest.approx(0.05)
    assert after.rebate_rate == pytest.approx(0.15)


def test_geopolitics_is_fee_free_in_every_era():
    for when in ("2026-02-01", "2026-05-01", "2026-08-01"):
        assert era_schedule("geopolitics", when).rate == 0.0


def test_health_maps_to_other_rate():
    assert fee_category("health") == "other"
    assert era_schedule("health", "2026-08-01").rate == pytest.approx(0.05)


def test_from_gamma_parses_schedule():
    s = FeeSchedule.from_gamma({
        "rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.15,
    })
    assert s.rate == 0.05
    assert s.rebate_rate == 0.15
    assert s.source == "gamma"


def test_schedule_from_market_prefers_gamma_metadata():
    from polymarketbot.fees import schedule_from_market

    row = {"fee_rate": 0.25, "fee_exponent": 2.0, "fee_rebate": 0.20,
           "category": "crypto", "created_at": "2026-02-01"}
    s = schedule_from_market(row)
    assert s.source == "gamma"
    assert s.rate == 0.25 and s.exponent == 2.0
    assert taker_fee(100, 0.1, s) == pytest.approx(0.2025)


def test_schedule_from_market_falls_back_to_era():
    from polymarketbot.fees import schedule_from_market

    s = schedule_from_market({"category": "crypto", "created_at": "2024-06-01"})
    assert s.rate == 0.0
    s = schedule_from_market({"category": "sports", "created_at": "2026-08-01"})
    assert s.rate == pytest.approx(0.05)


# ---------------------------------------------------------------- point-in-time
# Gamma publishes only the *current* schedule, so a market created before fees
# existed still shows today's rate. Pricing a 2025 fill from that rate charges
# fees that were never collected. These tests pin the gate.


def test_schedule_at_gates_pre_2026_fill_to_zero():
    from polymarketbot.fees import schedule_at

    row = {"fee_rate": 0.07, "fee_exponent": 1.0, "fee_rebate": 0.20,
           "category": "crypto", "fee_type": "crypto_fees_v2"}
    # A 2025-11 fill on a market whose *current* schedule is 0.07: free.
    assert schedule_at(row, "2025-11-01").rate == 0.0
    assert schedule_at(row, "2025-11-01").source == "zero"
    # From the pilot's first day the market's own (era-stamped) type applies.
    assert schedule_at(row, "2026-02-01").rate == pytest.approx(0.07)
    assert schedule_at(row, "2026-02-01").source == "type:crypto_fees_v2"
    assert schedule_at(row, "2026-08-01").rate == pytest.approx(0.07)


def test_schedule_at_uses_fee_type_over_category():
    from polymarketbot.fees import schedule_at

    # category says "crypto" but the market is explicitly fee-free by design.
    row = {"fee_type": "zero_fees", "category": "crypto", "fee_rate": 0.0}
    assert schedule_at(row, "2026-09-01").rate == 0.0
    assert schedule_at(row, "2026-09-01").source == "type:zero_fees"


def test_schedule_at_knows_the_february_sports_introduction():
    """The era table has no sports rate before 2026-03-30; Gamma does.

    9,842 markets carry `sports_fees` at 0.0175 for the NCAAB/Serie A launch on
    2026-02-18, which `configs/fees.yaml` explicitly does not model. The
    per-market type is the only source that prices them, so this is the case
    that proves the cascade must consult it before the era table.
    """
    from polymarketbot.fees import schedule_at

    row = {"fee_type": "sports_fees", "category": "sports", "fee_rate": 0.0175}
    assert era_schedule("sports", "2026-03-01").rate == 0.0
    assert schedule_at(row, "2026-03-01").rate == pytest.approx(0.0175)
    assert schedule_at(row, "2026-02-17").rate == 0.0


def test_schedule_at_sports_v3_after_july_10():
    from polymarketbot.fees import schedule_at

    row = {"fee_type": "sports_fees_v3", "category": "sports"}
    s = schedule_at(row, "2026-08-01")
    assert s.rate == pytest.approx(0.05)
    assert s.rebate_rate == pytest.approx(0.15)


def test_unknown_fee_type_falls_back_to_era():
    from polymarketbot.fees import schedule_at

    row = {"fee_type": "made_up_fee", "category": "sports", "fee_rate": None}
    assert schedule_at(row, "2026-08-01").rate == pytest.approx(0.05)


def test_observed_fee_types_match_era_end_state():
    """Contract: the era table's final rates must equal what Gamma reports.

    `fee_types` is measured from 1,937,137 markets (the measurement script has
    been retired; the table is the recorded contract).
    If an era edit and the observed schedules disagree, every PnL number is
    wrong in one of the two eras, so fail loudly here.
    """
    from polymarketbot.fees import schedule_for_fee_type

    era_of = {
        "crypto_15_min": "crypto", "crypto_fees": "crypto", "crypto_fees_v2": "crypto",
        "sports_fees_v2": "sports", "sports_fees_v3": "sports",
        "weather_fees": "weather", "politics_fees": "politics",
        "culture_fees": "culture", "finance_prices_fees": "finance",
        "tech_fees": "tech", "economics_fees": "economics",
        "mentions_fees": "mentions", "general_fees": "other",
    }
    # Deadline-stamped types are compared inside their own era: the two pilot
    # crypto types carry 0.25/exp2 and only exist because of the 2026-01-05
    # regime, so measuring them against the post-March era would be wrong.
    # Two sports types are deliberately absent because they are per-market
    # exceptions the era table cannot express (both measured, both real):
    #   sports_fees              0.0175 from 2026-02-18, the NCAAB/Serie A launch
    #   sports_fees_nfl_cfb_oct26 0.03  from 2026-08-21, below the 0.05 sports rate
    # The per-market type is their only source.
    own_era = {
        "crypto_15_min": "2026-02-01", "crypto_fees": "2026-03-15",
        "sports_fees_v2": "2026-05-01",
    }
    end = "2026-09-01"
    for fee_type, category in era_of.items():
        at = own_era.get(fee_type, end)
        observed = schedule_for_fee_type(fee_type, at)
        era = era_schedule(category, at)
        assert observed is not None, fee_type
        assert observed.rate == pytest.approx(era.rate), f"{fee_type} rate vs {category} era"
        assert observed.exponent == pytest.approx(era.exponent), fee_type
        assert observed.rebate_rate == pytest.approx(era.rebate_rate), f"{fee_type} rebate"


# ---------------------------------------------------------------------------
# Taker Rebate Program
# ---------------------------------------------------------------------------

def test_weighted_volume_matches_published_example():
    """Politics, 1,000 shares at 0.40 => 400 * 0.60 * 1.3 = 312 wV."""
    from polymarketbot.fees import weighted_volume
    assert weighted_volume(400.0, 0.40, "politics") == pytest.approx(312.0)


def test_weighted_volume_price_matters():
    """Two $50 crypto trades; the cheap one earns more (0.95 vs 0.50 upside)."""
    from polymarketbot.fees import weighted_volume
    cheap = weighted_volume(50.0, 0.05, "crypto")
    rich = weighted_volume(50.0, 0.50, "crypto")
    assert cheap == pytest.approx(109.25)
    assert rich == pytest.approx(57.5)
    assert cheap > rich


def test_geopolitics_earns_no_weighted_volume():
    from polymarketbot.fees import weighted_volume
    assert weighted_volume(1_000.0, 0.40, "geopolitics") == 0.0


def test_tier_boundaries():
    from polymarketbot.fees import taker_tier
    assert taker_tier(0) == ("None", 0.0)
    assert taker_tier(1_999) == ("None", 0.0)
    assert taker_tier(2_000) == ("Bronze", 0.03)
    assert taker_tier(19_999)[0] == "Bronze"
    assert taker_tier(20_000) == ("Silver", 0.08)
    assert taker_tier(10_000_000)[0] == "Obsidian"


def test_rebate_does_not_exist_before_launch():
    """The program opened 2026-05-28; a 2026-04 fee cannot have been rebated."""
    from polymarketbot.fees import taker_rebate
    assert taker_rebate(10.0, 1_000_000, "2026-04-01", 0.5, "crypto") == 0.0
    assert taker_rebate(10.0, 1_000_000, "2026-07-01", 0.5, "crypto") > 0.0


def test_rebate_is_small_at_entry_tier():
    """The point of the model: Bronze rebate is a rounding error versus a 2c spread."""
    from polymarketbot.fees import taker_rebate
    fee_at_peak = 0.07 * (0.5 * 0.5)      # crypto peak fee per share
    rebate = taker_rebate(fee_at_peak, 2_500, "2026-07-01", 0.5, "crypto")
    assert rebate < 0.002, "Bronze rebate should be well under a fifth of a cent"


def test_turnover_needed_for_gold_is_out_of_reach_at_small_size():
    from polymarketbot.fees import turnover_needed_for_tier
    turns = turnover_needed_for_tier(200_000, 2_000, 0.5, "crypto")
    assert turns > 80, "reaching Gold at $2k capital would need an implausible turnover"


# ---------------------------------------------------------------------------
# Venue schedules (2026-11-10)
# ---------------------------------------------------------------------------
def test_us_taker_tier_ladder_matches_the_published_table() -> None:
    from polymarketbot.fees import us_taker_coefficient, us_taker_tiers

    assert [(name, theta) for name, _floor, theta in us_taker_tiers()] == [
        ("tier_1", 0.095),
        ("tier_2", 0.085),
        ("tier_3", 0.075),
        ("tier_4", 0.0695),
    ]
    # Boundaries are inclusive on the upper tier's floor.
    assert us_taker_coefficient(0.0) == pytest.approx(0.095)
    assert us_taker_coefficient(9_999.99) == pytest.approx(0.095)
    assert us_taker_coefficient(10_000.0) == pytest.approx(0.085)
    assert us_taker_coefficient(49_999.99) == pytest.approx(0.085)
    assert us_taker_coefficient(50_000.0) == pytest.approx(0.075)
    assert us_taker_coefficient(249_999.99) == pytest.approx(0.075)
    assert us_taker_coefficient(250_000.0) == pytest.approx(0.0695)


def test_unmeasured_us_volume_is_refused_not_assumed() -> None:
    """Pricing an unknown tier at either end is a guess: 0.095 vs 0.0695 is a
    36.7 % fee difference on the same trade."""
    from polymarketbot.fees import us_taker_coefficient

    assert us_taker_coefficient(None) is None
    with pytest.raises(ValueError):
        us_taker_coefficient(-1.0)


def test_us_fee_matches_the_documented_worked_examples() -> None:
    """docs.polymarket.us/fees, Theta 0.0695: 1,000 contracts at 0.10 -> 6.26,
    0.65 -> 15.81, 0.30 -> 14.60, 0.90 -> 6.26. Exact cents: the published rule
    rounds to the cent, half to even, so a binary-float value of 14.595 or
    6.255 must land on the documented side of the tie."""
    from polymarketbot.fees import us_taker_fee

    assert us_taker_fee(1_000, 0.10, 0.0695) == pytest.approx(6.26)
    assert us_taker_fee(1_000, 0.65, 0.0695) == pytest.approx(15.81)
    assert us_taker_fee(1_000, 0.30, 0.0695) == pytest.approx(14.60)
    assert us_taker_fee(1_000, 0.90, 0.0695) == pytest.approx(6.26)
    # Documented maximum for a 100-lot at p = 0.50 is 1.74.
    assert us_taker_fee(100, 0.50, 0.0695) == pytest.approx(1.74)


def test_us_tier_one_under_charges_by_a_third_if_priced_as_tier_four() -> None:
    from polymarketbot.fees import us_taker_fee

    cheap = us_taker_fee(1_000, 0.50, 0.095)
    best = us_taker_fee(1_000, 0.50, 0.0695)
    # Cent rounding perturbs the exact coefficient ratio at the 4th digit.
    assert cheap / best == pytest.approx(0.095 / 0.0695, rel=1e-3)
    assert cheap / best > 1.36


def test_us_combo_uses_the_coefficient_in_force_at_the_trade_time() -> None:
    from polymarketbot.fees import us_combo_taker_fee

    before = us_combo_taker_fee(1_000, 0.10, 0.0695, "2026-11-10T12:00:00-05:00")
    after = us_combo_taker_fee(1_000, 0.10, 0.0695, "2026-11-11T14:00:00-05:00")
    assert before > after  # 0.085 on (1-p)^4 drops to 0.06
    # Fee = C * p * [theta(1-p) + fixed(1-p)^4]
    expected_before = 1_000 * 0.10 * (0.0695 * 0.90 + 0.085 * 0.90 ** 4)
    expected_after = 1_000 * 0.10 * (0.0695 * 0.90 + 0.06 * 0.90 ** 4)
    assert before == pytest.approx(round(expected_before, 2))
    assert after == pytest.approx(round(expected_after, 2))


def test_venue_is_resolved_from_the_endpoint() -> None:
    from polymarketbot.fees import VENUE_GLOBAL, VENUE_US, venue_for_host

    assert venue_for_host("https://clob.polymarket.com") == VENUE_GLOBAL
    assert venue_for_host("https://api.polymarket.us/v1") == VENUE_US
    assert venue_for_host("") == VENUE_GLOBAL


def test_global_category_table_still_matches_the_published_rates() -> None:
    """The global schedule is per category; the US Theta ladder must not leak
    into it. Rates from docs.polymarket.com/trading/fees, read 2026-11-10."""
    from datetime import datetime, timezone

    from polymarketbot.fees import us_taker_fee

    when = datetime(2026, 11, 10, tzinfo=timezone.utc)
    published = {
        "crypto": 0.07,
        "sports": 0.05,
        "finance": 0.04,
        "politics": 0.04,
        "economics": 0.05,
        "culture": 0.05,
        "weather": 0.05,
        "other": 0.05,
        "mentions": 0.04,
        "tech": 0.04,
        "geopolitics": 0.0,
        "combos": 0.05,
    }
    for category, rate in published.items():
        assert era_schedule(category, when).rate == pytest.approx(rate), category
    # 100 shares at p = 0.50: politics 1.00, crypto 1.75 (both published).
    politics = era_schedule("politics", when)
    crypto = era_schedule("crypto", when)
    assert taker_fee(100, 0.50, politics) == pytest.approx(1.00, abs=0.005)
    assert taker_fee(100, 0.50, crypto) == pytest.approx(1.75, abs=0.005)
    # The US schedule is a different object with a different rounding rule.
    assert us_taker_fee(100, 0.50, 0.0695) == pytest.approx(1.74)


def test_global_taker_rebate_ladder_and_weighted_volume() -> None:
    """Tier 0 is 0 % and covers every volume below $2,000 of weighted volume,
    which is this bot's current state - so booking no rebate is exact today and
    an unmeasured volume must refuse rather than assume the top tier."""
    from polymarketbot.fees import taker_rebate_rate, weighted_taker_volume

    assert taker_rebate_rate(0.0) == pytest.approx(0.0)
    assert taker_rebate_rate(1_999.99) == pytest.approx(0.0)
    assert taker_rebate_rate(2_000.0) == pytest.approx(0.03)
    assert taker_rebate_rate(20_000.0) == pytest.approx(0.08)
    assert taker_rebate_rate(200_000.0) == pytest.approx(0.18)
    assert taker_rebate_rate(1_000_000.0) == pytest.approx(0.32)
    assert taker_rebate_rate(4_000_000.0) == pytest.approx(0.44)
    assert taker_rebate_rate(10_000_000.0) == pytest.approx(0.50)
    assert taker_rebate_rate(None) is None

    # Published example: $400 of Politics at 0.40 -> 400 * 0.60 * 1.3 = 312 wV.
    assert weighted_taker_volume(400.0, 0.40, "politics") == pytest.approx(312.0)
    # Published example: $50 of Crypto at 0.05 -> 50 * 0.95 * 2.3 = 109.25 wV.
    assert weighted_taker_volume(50.0, 0.05, "crypto") == pytest.approx(109.25)
    # Fee-free markets earn no weighted volume however large the trade.
    assert weighted_taker_volume(1_000_000.0, 0.40, "geopolitics") == pytest.approx(0.0)
