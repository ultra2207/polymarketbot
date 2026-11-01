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

    `fee_types` is measured from 1,937,137 markets (scripts/validate_fee_model.py).
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
