"""RiskManager must refuse a quote the unified micro engine pulls.

The quoting loop is not the only gate: every placement goes through
``RiskManager.validate_quote``. If a toxicity pull is not honoured there, the
engine cancels a resting quote and the placement path immediately re-places it.
"""

from __future__ import annotations

from polymarketbot.risk.contracts import RiskAction, RiskDecision
from polymarketbot.risk.manager import RiskManager


class _Micro:
    def __init__(self, action: RiskAction, reason: str = "test") -> None:
        self.action = action
        self.reason = reason

    def evaluate(self, *_args, **_kwargs) -> RiskDecision:
        return RiskDecision(
            action=self.action, side="BUY", reason=self.reason, measured=True
        )


def _quote(rm: RiskManager):
    return rm.validate_quote(
        "tok",
        "cid",
        "BUY",
        0.50,
        10.0,
        current_pos_shares=0.0,
        current_pos_cost=0.0,
        current_portfolio_inventory_usd=0.0,
        current_cash_usd=300.0,
        mid_price=0.50,
        tick_size=0.01,
        timestamp=1_000.0,
    )


def test_pull_from_the_micro_engine_blocks_the_placement() -> None:
    rm = RiskManager()
    rm.set_micro_risk(_Micro(RiskAction.PULL, "next in queue under toxic OFI flow"))

    allowed, reason, size, _price = _quote(rm)

    assert allowed is False
    assert "next in queue" in reason
    assert size == 0.0


def test_suppress_from_the_micro_engine_blocks_the_placement() -> None:
    rm = RiskManager()
    rm.set_micro_risk(_Micro(RiskAction.SUPPRESS, "3 consecutive BUY fills"))

    allowed, reason, size, _price = _quote(rm)

    assert allowed is False
    assert "consecutive" in reason
    assert size == 0.0


def test_widen_does_not_block_the_placement() -> None:
    rm = RiskManager()
    rm.set_micro_risk(_Micro(RiskAction.WIDEN, "toxic selling flow"))

    allowed, _reason, size, _price = _quote(rm)

    assert allowed is True
    assert size > 0.0


def test_without_a_micro_engine_the_gate_still_works() -> None:
    rm = RiskManager()

    allowed, _reason, size, _price = _quote(rm)

    assert allowed is True
    assert size > 0.0
