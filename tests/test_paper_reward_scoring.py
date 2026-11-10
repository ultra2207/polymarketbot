"""Official Polymarket Q_min reward scoring in the native paper-trading engine.

Regression tests for ``NativePaperTradingEngine.process_reward_epoch``: epoch credit
must use Q_min = max(min(Q1, Q2), max(Q1, Q2)/3) inside [0.10, 0.90] and min(Q1, Q2)
outside, never the raw Q1 + Q2 sum (which overstated LP income by 2-3x).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.rewards.scoring import (  # noqa: E402
    OrderQuote,
    compute_book_side_scores,
    compute_minimum_score,
)
from polymarketbot.simulator.engine import (  # noqa: E402
    MissingBookPriceError,
    NativePaperTradingEngine,
)

DAILY_RATE = 28.80
EPOCH_BUDGET = DAILY_RATE / 1440.0
MAX_SPREAD = 1.0
QUOTE_SIZE = 20.0
# Resting 0.5c from a 0.50 mid inside a 1.0c qualifying band: ((1 - 0.5) / 1)^2 * size
QUOTE_SCORE_PER_SHARE = 0.25

TS = 1_700_000_000.0


def _make_engine(tmp_path: Path, **overrides) -> NativePaperTradingEngine:
    kwargs = {
        "condition_id": "0xREWARD_SCORING_TEST",
        "asset_id_yes": "TOKEN_YES",
        "asset_id_no": "TOKEN_NO",
        "initial_cash": 100.0,
        "quote_size": QUOTE_SIZE,
        "spread_offset_cents": 0.50,
        "daily_reward_rate": DAILY_RATE,
        "rewards_max_spread_cents": MAX_SPREAD,
        "rewards_min_size": QUOTE_SIZE,
        "auto_merge_threshold": 10.0,
        "institutional_mode": False,
        "db_path": tmp_path / "reward_scoring.db",
        "ledger_path": tmp_path / "reward_scoring_ledger.json",
    }
    kwargs.update(overrides)
    return NativePaperTradingEngine(**kwargs)


def _single_quote(price: float, size: float = QUOTE_SIZE) -> dict:
    """Minimal active_quotes entry, bypassing quote generation for one-sided scenarios."""
    return {
        "quote_id": "BUY_YES",
        "token_side": "YES",
        "side": "BUY",
        "price": price,
        "size": size,
        "queue_ahead": 0.0,
        "score": 0.0,
        "v_queue": 0.0,
        "timestamp": TS,
        "status": "ACTIVE",
    }


def _two_sided_engine(tmp_path: Path, **overrides) -> NativePaperTradingEngine:
    engine = _make_engine(tmp_path, **overrides)
    engine.update_orderbook_snapshot(
        "YES",
        bids=[{"price": 0.49, "size": 100.0}],
        asks=[{"price": 0.51, "size": 100.0}],
        timestamp=TS,
    )
    return engine


class TestMakerQMin:
    def test_symmetric_book_scores_q_min_not_q1_plus_q2(self, tmp_path):
        engine = _two_sided_engine(tmp_path)
        try:
            # Legacy single-token mode rests BUY_YES @0.495 and SELL_YES @0.505 -> Q1 == Q2.
            res = engine.process_reward_epoch(timestamp=TS)

            expected_side = QUOTE_SCORE_PER_SHARE * QUOTE_SIZE
            assert res["q_one"] == pytest.approx(expected_side)
            assert res["q_two"] == pytest.approx(expected_side)
            assert res["q_min"] == pytest.approx(expected_side)
            assert res["q_min"] < res["q_one"] + res["q_two"]
            assert res["user_score"] == pytest.approx(res["q_min"])
            # Returned dict exposes the raw book-side scores.
            assert {"q_one", "q_two", "q_min"} <= set(res)
        finally:
            engine.close()

    def test_one_sided_quote_inside_band_scores_max_over_three(self, tmp_path):
        engine = _two_sided_engine(tmp_path)
        try:
            engine.cancel_all_virtual_quotes()
            engine.active_quotes["BUY_YES"] = _single_quote(0.495)

            res = engine.process_reward_epoch(timestamp=TS)

            expected = QUOTE_SCORE_PER_SHARE * QUOTE_SIZE
            assert res["q_one"] == pytest.approx(expected)
            assert res["q_two"] == pytest.approx(0.0)
            assert res["q_min"] == pytest.approx(expected / 3.0, abs=1e-3)
            assert res["q_min"] == pytest.approx(res["q_one"] / 3.0, abs=1e-3)
            assert res["user_score"] == pytest.approx(res["q_min"])
        finally:
            engine.close()

    def test_one_sided_quote_outside_band_scores_min_zero(self, tmp_path):
        engine = _make_engine(tmp_path)
        try:
            engine.update_orderbook_snapshot(
                "YES",
                bids=[{"price": 0.04, "size": 100.0}],
                asks=[{"price": 0.06, "size": 100.0}],
                timestamp=TS,
            )
            engine.cancel_all_virtual_quotes()
            engine.active_quotes["BUY_YES"] = _single_quote(0.045)

            res = engine.process_reward_epoch(timestamp=TS)

            # Mid 0.05 is outside [0.10, 0.90]: Q_min = min(Q1, Q2) = 0 for a one-sided quote.
            assert res["q_one"] == pytest.approx(QUOTE_SCORE_PER_SHARE * QUOTE_SIZE)
            assert res["q_two"] == pytest.approx(0.0)
            assert res["q_min"] == pytest.approx(0.0)
            assert res["user_score"] == pytest.approx(0.0)
            assert res["epoch_payout_usd"] == pytest.approx(0.0)
            assert res["floor_met"] is False
        finally:
            engine.close()

    def test_outside_band_does_not_pay_third_of_one_sided_score(self, tmp_path):
        """Contrast case: identical one-sided quote inside the band pays max/3, outside pays 0."""
        inside = _two_sided_engine(tmp_path / "inside")
        outside = _make_engine(tmp_path / "outside")
        try:
            inside.cancel_all_virtual_quotes()
            inside.active_quotes["BUY_YES"] = _single_quote(0.495)
            outside.update_orderbook_snapshot(
                "YES",
                bids=[{"price": 0.04, "size": 100.0}],
                asks=[{"price": 0.06, "size": 100.0}],
                timestamp=TS,
            )
            outside.cancel_all_virtual_quotes()
            outside.active_quotes["BUY_YES"] = _single_quote(0.045)

            res_inside = inside.process_reward_epoch(timestamp=TS)
            res_outside = outside.process_reward_epoch(timestamp=TS)

            assert res_inside["q_one"] == pytest.approx(res_outside["q_one"])
            assert res_inside["q_min"] == pytest.approx(res_outside["q_one"] / 3.0, abs=1e-3)
            assert res_outside["q_min"] == pytest.approx(0.0)
        finally:
            inside.close()
            outside.close()


class TestEpochPayoutUsesQMin:
    def test_payout_equals_q_min_share_of_pool(self, tmp_path):
        engine = _two_sided_engine(tmp_path, competing_score_override=15.0)
        try:
            res = engine.process_reward_epoch(timestamp=TS)

            q_min = res["q_min"]
            expected = (q_min / (q_min + 15.0)) * EPOCH_BUDGET
            inflated = ((res["q_one"] + res["q_two"]) / (res["q_one"] + res["q_two"] + 15.0)) * EPOCH_BUDGET

            assert res["competing_score"] == pytest.approx(15.0)
            assert res["epoch_payout_usd"] == pytest.approx(expected, rel=1e-3)
            assert res["epoch_payout_usd"] < inflated
            assert res["total_rewards_usd"] == pytest.approx(res["epoch_payout_usd"], rel=1e-3)
        finally:
            engine.close()

    def test_payout_tracks_q_min_when_quote_size_doubles(self, tmp_path):
        small = _two_sided_engine(tmp_path / "small", competing_score_override=15.0)
        large = _two_sided_engine(tmp_path / "large", competing_score_override=15.0, quote_size=2.0 * QUOTE_SIZE)
        try:
            res_small = small.process_reward_epoch(timestamp=TS)
            res_large = large.process_reward_epoch(timestamp=TS)

            assert res_small["q_min"] == pytest.approx(QUOTE_SCORE_PER_SHARE * QUOTE_SIZE)
            assert res_large["q_min"] == pytest.approx(2.0 * res_small["q_min"])

            expected_small = (res_small["q_min"] / (res_small["q_min"] + 15.0)) * EPOCH_BUDGET
            expected_large = (res_large["q_min"] / (res_large["q_min"] + 15.0)) * EPOCH_BUDGET
            assert res_small["epoch_payout_usd"] == pytest.approx(expected_small, rel=1e-3)
            assert res_large["epoch_payout_usd"] == pytest.approx(expected_large, rel=1e-3)
            assert res_large["epoch_payout_usd"] > res_small["epoch_payout_usd"]
        finally:
            small.close()
            large.close()


class TestCompetitorQMin:
    def test_one_sided_competitor_book_uses_q_min_rule(self, tmp_path):
        engine = _make_engine(tmp_path)
        try:
            # Only real YES bids inside the band: Q2 == 0, so Q_min = max(Q1, 0)/3, never Q1.
            engine.update_orderbook_snapshot(
                "YES",
                bids=[{"price": 0.495, "size": 100.0}],
                asks=[],
                timestamp=TS,
            )
            mid_yes = 0.50
            comp_q1, comp_q2 = compute_book_side_scores(
                [OrderQuote(side="BUY", outcome="YES", price=0.495, size=100.0)],
                mid_yes,
                MAX_SPREAD,
            )
            expected_comp = compute_minimum_score(comp_q1, comp_q2, mid_yes)

            assert comp_q1 > 0.0
            assert comp_q2 == pytest.approx(0.0)
            assert expected_comp == pytest.approx(comp_q1 / 3.0)

            # The one-sided book has no live mid, so the engine refuses to score an
            # epoch against an invented 0.50 instead of reporting a competing score.
            with pytest.raises(MissingBookPriceError):
                engine.process_reward_epoch(timestamp=TS)
        finally:
            engine.close()

    def test_two_sided_competitor_book_scores_q_min(self, tmp_path):
        engine = _make_engine(tmp_path)
        try:
            engine.update_orderbook_snapshot(
                "YES",
                bids=[{"price": 0.495, "size": 100.0}],
                asks=[{"price": 0.505, "size": 100.0}],
                timestamp=TS,
            )
            mid_yes = engine.book_yes.mid_price
            comp_q1, comp_q2 = compute_book_side_scores(
                [
                    OrderQuote(side="BUY", outcome="YES", price=0.495, size=100.0),
                    OrderQuote(side="SELL", outcome="YES", price=0.505, size=100.0),
                ],
                mid_yes,
                MAX_SPREAD,
            )
            expected_comp = compute_minimum_score(comp_q1, comp_q2, mid_yes)

            res = engine.process_reward_epoch(timestamp=TS)

            assert comp_q1 == pytest.approx(comp_q2)
            assert res["competing_score"] == pytest.approx(expected_comp, abs=0.01)
            # Q1 + Q2 would have been twice this.
            assert res["competing_score"] < comp_q1 + comp_q2
        finally:
            engine.close()
