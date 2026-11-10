"""The reward fill path must credit the maker rebate, not raise.

Regression test for a live defect: ``apply_resting_fills`` called
``reward.maker_rebate(price, shares, schedule)``, but that function never
existed in ``polymarketbot.strategy.reward.decision``. ``paper.engine._tick`` wraps
the whole tick in ``except Exception``, so the failure surfaced as:

  * the fill already booked by ``ledger.buy`` (so inventory was right),
  * no rebate credited (money silently dropped),
  * the exception escaping ``apply_resting_fills`` and therefore skipping the
    settlement and mark stages of that same tick.

The measured consequence in a soak window was ``0`` ``maker_rebate`` rows and
``$0.00`` rebate across 1,388 accrual rows. The published maker-rebate terms
give the maker an explicit per-wallet share, so a zero rebate is a defect, not
a modelling choice.
"""

from __future__ import annotations

import pytest

from polymarketbot.fees import FeeSchedule, maker_fee_rebate
from polymarketbot.paper.actions import Resting
from polymarketbot.paper.pillars import reward_runner
import polymarketbot.strategy.reward.decision as reward

PRICE = 0.81
SHARES = 20.0
SCHEDULE = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True, rebate_rate=0.15)


class _Ledger:
    def __init__(self) -> None:
        self.buys: list[dict] = []
        self.credits: list[dict] = []

    def buy(self, strategy, *, condition_id, token_id, shares, price, tag="", **_kw):
        self.buys.append(
            {
                "strategy": strategy,
                "condition_id": condition_id,
                "token_id": token_id,
                "shares": shares,
                "price": price,
                "tag": tag,
            }
        )
        return True

    def credit(self, strategy, *, amount, condition_id="", token_id="", detail="rebate"):
        self.credits.append(
            {"strategy": strategy, "amount": amount, "detail": detail}
        )


class _Market:
    fee_schedule = SCHEDULE


class _Engine:
    def __init__(self) -> None:
        self.ledger = _Ledger()
        self.by_condition = {"0xcond": _Market()}
        self._fills = {"RewardHarvester": 0}


def _quote() -> Resting:
    return Resting(
        strategy="RewardHarvester",
        condition_id="0xcond",
        token_id="tok",
        price=PRICE,
        shares=SHARES,
        min_order=5.0,
        placed=0.0,
        tick=0.001,
    )


def test_a_fill_on_a_fee_paying_market_credits_the_measured_rebate(monkeypatch):
    """The rebate must equal the fee function's own output, not zero."""
    monkeypatch.setattr(
        reward, "measured_fill_shares", lambda *a, **kw: SHARES
    )
    engine = _Engine()

    reward_runner.apply_resting_fills(
        engine, {"tok": _quote()}, "RewardHarvester", {"tok": {}}, [], 30.0
    )

    assert len(engine.ledger.buys) == 1, "the fill itself must still be booked"
    rebates = [c for c in engine.ledger.credits if c["detail"] == "maker_rebate"]
    assert len(rebates) == 1, "a fee-paying fill must credit exactly one rebate"
    expected = maker_fee_rebate(PRICE, SHARES, SCHEDULE)
    assert expected > 0.0
    assert rebates[0]["amount"] == pytest.approx(expected)
    assert engine._fills["RewardHarvester"] == 1


def test_a_fill_on_a_zero_fee_market_credits_nothing(monkeypatch):
    """A zero-rate schedule yields no rebate row, and still books the fill."""
    monkeypatch.setattr(reward, "measured_fill_shares", lambda *a, **kw: SHARES)
    engine = _Engine()
    engine.by_condition = {
        "0xcond": type("M", (), {"fee_schedule": FeeSchedule.zero()})()
    }

    reward_runner.apply_resting_fills(
        engine, {"tok": _quote()}, "RewardHarvester", {"tok": {}}, [], 30.0
    )

    assert maker_fee_rebate(PRICE, SHARES, FeeSchedule.zero()) == 0.0
    assert len(engine.ledger.buys) == 1
    assert not [c for c in engine.ledger.credits if c["detail"] == "maker_rebate"]
