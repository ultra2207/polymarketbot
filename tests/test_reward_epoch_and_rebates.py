"""Epoch-average competitor estimation and rebate-source fallback.

Two confirmed documentation-audit defects are pinned here:

* the venue divides a maker's score by the **epoch average** of the competing
  book (1,440 samples/day), so a single L2 snapshot must be labelled
  low-confidence rather than presented as the epoch rate;
* maker rebates are a per-wallet entitlement readable from
  ``GET /rebates/current``, so the venue's own number must be used when it can
  be read and the modelled ``fees.maker_fee_rebate`` only when it cannot - with
  the fallback logged.
"""

from __future__ import annotations



import pytest

from polymarketbot.fees import FeeSchedule, maker_fee_rebate
import polymarketbot.strategy.reward.decision as reward
from polymarketbot.strategy.venue.reads import Market
from polymarketbot.strategy.reward.rebates import RebateAccountant

DAY = "2026-11-10"
SCHEDULE = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True, rebate_rate=0.15)


def _market() -> Market:
    return Market(
        condition_id="c1",
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=("yes", "no"),
        tick=0.01,
        min_order=5.0,
        daily_reward=500.0,
        reward_max_spread_cents=3.0,
        reward_min_size=200.0,
        fee_schedule=SCHEDULE,
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


def _book(bid: float, ask: float, size: float = 100.0) -> dict:
    return {
        "bids": [{"price": str(bid), "size": str(size)}],
        "asks": [{"price": str(ask), "size": str(size)}],
    }


# --------------------------------------------------- epoch-average window
def test_single_snapshot_is_marked_low_confidence():
    window = reward.CompetingScoreWindow()
    estimate = window.observe("c1", 1000.0, now=1_000.0)

    assert estimate.samples == 1
    assert estimate.low_confidence is True
    assert estimate.variance is None
    assert "LOW-CONFIDENCE" in estimate.label


def test_window_means_at_the_documented_sample_cadence():
    window = reward.CompetingScoreWindow()
    window.observe("c1", 900.0, now=1_000.0)
    # Inside the 60 s cadence the reading is not taken again.
    estimate = window.observe("c1", 4_000.0, now=1_030.0)
    assert estimate.samples == 1
    assert estimate.mean == pytest.approx(900.0)
    assert estimate.snapshot == pytest.approx(4_000.0)

    # At the documented cadence (86,400 / 1,440 = 60 s) it is.
    estimate = window.observe("c1", 1_100.0, now=1_060.0)
    assert estimate.samples == 2
    assert estimate.low_confidence is False
    assert estimate.mean == pytest.approx(1_000.0)
    assert estimate.variance == pytest.approx(20_000.0)
    assert estimate.label.startswith("n=2")


def test_candidates_carry_denominator_provenance():
    market = _market()
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    window = reward.CompetingScoreWindow()

    first = reward.reward_candidates(
        [market], books, [], competitor=window, now=1_000.0
    )[0]
    assert first["competing_samples"] == 1
    assert first["competing_low_confidence"] is True
    assert first["competing_variance"] is None
    assert first["competing_score"] == pytest.approx(first["competing_snapshot"])

    second = reward.reward_candidates(
        [market], books, [], competitor=window, now=1_060.0
    )[0]
    assert second["competing_samples"] == 2
    assert second["competing_low_confidence"] is False


def test_measured_accrual_reports_the_estimate_it_used():
    market = _market()
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    window = reward.CompetingScoreWindow()
    window.observe(market.condition_id, 1_000.0, now=1_000.0)
    estimate = window.estimate(market.condition_id, now=1_000.0)

    share = reward.measured_accrual(
        pool_usd_per_day=market.daily_reward,
        price=0.49,
        shares=200.0,
        min_size=200.0,
        max_spread_cents=3.0,
        leg_book=books["yes"],
        other_book=books["no"],
        competing=estimate,
    )
    assert share is not None
    assert share.competing_samples == 1
    assert share.low_confidence is True
    assert share.competing_score == pytest.approx(1_000.0)

    # Without a window the single snapshot is still flagged, never sold as
    # the epoch rate.
    snapshot_only = reward.measured_accrual(
        pool_usd_per_day=market.daily_reward,
        price=0.49,
        shares=200.0,
        min_size=200.0,
        max_spread_cents=3.0,
        leg_book=books["yes"],
        other_book=books["no"],
    )
    assert snapshot_only is not None
    assert snapshot_only.competing_samples == 1
    assert snapshot_only.low_confidence is True


def test_competing_detail_states_the_denominator_bracket():
    market = _market()
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    detail = reward.market_competing_detail(market, books)

    assert detail is not None
    assert detail.total == pytest.approx(detail.q_one_side + detail.q_two_side)
    # Q_one + Q_two is an upper bound on the venue's sum Q_min, so the factor
    # by which our denominator can overstate theirs is at most 1 + c = 4.
    assert detail.bias_factor_high == pytest.approx(4.0)
    assert detail.venue_denominator_low == pytest.approx(detail.total / 4.0)
    assert "overstates" in detail.direction


# ------------------------------------------------------- rebate fallback
class _RebateClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def get_current_rebates(self, date=None) -> dict:
        self.calls.append(str(date))
        return self.payload


def _venue_payload(by_condition: dict[str, float], total: float) -> dict:
    return {
        "source": "clob_api",
        "by_condition": by_condition,
        "total_usd": total,
        "row_count": len(by_condition),
    }


def test_venue_rebate_is_credited_and_the_model_is_not_added():
    client = _RebateClient(_venue_payload({"0xc": 1.5}, 1.5))
    accountant = RebateAccountant(client)

    credits = accountant.sync(1_000.0, DAY)
    assert [(c.condition_id, c.usd, c.source) for c in credits] == [
        ("0xc", 1.5, "venue")
    ]
    assert accountant.source == "venue"
    assert accountant.describe().startswith("venue")

    # A fill while the venue number is live must not also book the model.
    modelled = accountant.modelled_credit(
        condition_id="0xc", price=0.50, shares=100.0, schedule=SCHEDULE
    )
    assert modelled.usd == 0.0

    # Only the increment is credited on the next poll.
    client.payload = _venue_payload({"0xc": 2.0}, 2.0)
    credits = accountant.sync(1_060.0, DAY)
    assert [(c.condition_id, c.usd) for c in credits] == [("0xc", 0.5)]


def test_unreadable_endpoint_falls_back_to_the_model_and_logs():
    from loguru import logger

    client = _RebateClient(
        {"source": "unavailable", "error": "503 Service Unavailable", "by_condition": {}}
    )
    accountant = RebateAccountant(client)

    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        assert accountant.sync(1_000.0, DAY) == []
    finally:
        logger.remove(sink)

    assert accountant.source == "modelled"
    assert "503" in accountant.error
    assert any("modelled" in message.lower() for message in messages)
    assert "modelled" in accountant.describe()

    credit = accountant.modelled_credit(
        condition_id="0xc", price=0.50, shares=100.0, schedule=SCHEDULE
    )
    assert credit.usd == pytest.approx(maker_fee_rebate(0.50, 100.0, SCHEDULE))
    assert credit.usd > 0.0
    assert credit.detail == "maker_rebate"


def test_no_client_means_modelled_with_a_reason():
    accountant = RebateAccountant(None)
    assert accountant.sync(1_000.0, DAY) == []
    assert accountant.source == "modelled"
    assert "CLOB client" in accountant.error
    credit = accountant.modelled_credit(
        condition_id="0xc", price=0.50, shares=100.0, schedule=SCHEDULE
    )
    assert credit.usd == pytest.approx(maker_fee_rebate(0.50, 100.0, SCHEDULE))


# ------------------------------------------------- per-market persistence
def test_run_reward_persists_per_market_accrual(monkeypatch, tmp_path):
    """Each tick must journal (condition, UTC day, USD) for a later venue join."""
    from polymarketbot.paper.actions import Resting
    from polymarketbot.strategy.ledger import Ledger
    from polymarketbot.paper.pillars import reward_runner

    now = 1_000_000.0
    day = reward_runner.utc_day(now)
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "ledger.db")
    market = _market()
    quote = Resting(
        strategy="RewardHarvester",
        condition_id=market.condition_id,
        token_id="yes",
        price=0.49,
        shares=200.0,
        min_order=5.0,
        placed=now + 100.0,  # not landed yet: the fill stage skips it
        tick=0.01,
    )

    class _Actions:
        def submit(self, *args, **kwargs):
            pass

    class _Engine:
        def __init__(self) -> None:
            self.ledger = ledger
            self.markets = [market]
            self.by_condition = {market.condition_id: market}
            self.reward_quotes = {"yes": quote}
            self.actions = _Actions()
            self.interval = 10.0
            self._reward_accrual_ts = now - 10.0
            self._reward_log_ts = now
            self._reward_window = None
            self._fills = {"RewardHarvester": 0}

        def _rtt(self) -> float:
            return 0.0

        def _reserved(self, resting) -> float:
            return 0.0

    engine = _Engine()
    monkeypatch.setattr(reward_runner.time, "time", lambda: now)
    monkeypatch.setattr(
        reward,
        "measured_accrual",
        lambda **kwargs: reward.AccrualShare(
            usd_per_day=864.0,
            share=0.1,
            our_score=1.0,
            competing_score=9.0,
            pool_usd_per_day=864.0,
            qualifies=True,
            competing_samples=3,
        ),
    )
    monkeypatch.setattr(reward, "reward_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(reward, "plan_quotes", lambda *a, **kw: [])

    reward_runner.run_reward(engine, {"yes": _book(0.48, 0.52)}, [])

    # 864 USD/day over the 10 s interval.
    expected = 864.0 * 10.0 / 86_400.0
    assert ledger.reward_accrual_by_condition(day) == {
        market.condition_id: pytest.approx(expected)
    }
    assert ledger.cash("RewardHarvester") == pytest.approx(1_000.0 + expected)
    ledger.close()
