"""The live settlement desk's data: what it prices, sizes and lands.

These are the shapes named in :mod:`polymarketbot.live.settlement` and shared
by the decision and the desk; each carries no behaviour beyond reading its own
fields, so the same record type is the tick's output and the operator's line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from polymarketbot.strategy.settlement.allocator import TailBudgetReport
from polymarketbot.strategy.venue.reads import Market


@dataclass(frozen=True)
class PricedMarket:
    """One market the live desk is considering, with the books it could buy.

    ``ladders`` maps token id to the venue's live ask ladder as
    ``(price, shares)`` levels; the decision reads the ladder of the token the
    *verdict* names, never of the book that happens to be richer. ``event_key``
    and ``exhaustive`` are the resolution-cluster facts the tail budget groups
    exposure by: the event id when it is known, ``None`` when the condition is
    its own resolution source (the narrowest possible cluster).
    """

    market: Market
    ladders: Mapping[str, tuple[tuple[float, float], ...]]
    event_key: Optional[str] = None
    exhaustive: bool = False

    def asks(self, token_id: str) -> tuple[tuple[float, float], ...]:
        return tuple(self.ladders.get(token_id, ()))


@dataclass(frozen=True)
class SettlementGrant:
    """One condition's allocator dollars, converted into the legs they fund."""

    condition_id: str
    token_id: str
    question: str
    p_win: float
    budget_usd: float
    """The allocator's ``all_in_usd`` grant - the whole order, fee included."""
    shares: float
    cost_usd: float
    fee_usd: float
    all_in_usd: float
    avg_price: float
    levels: int
    tail_pd: float
    tail_charge_usd: float
    break_even_ask: Optional[float]
    ask: float


@dataclass(frozen=True)
class SettlementDecision:
    """What the live trader may buy, and what it refused, for one tick."""

    grants: tuple[SettlementGrant, ...] = ()
    refusals: tuple[tuple[str, str], ...] = ()
    gas_usd: Optional[float] = None
    tail_report: Optional[TailBudgetReport] = None
    priced: int = 0

    @property
    def tail_budget_usd(self) -> float:
        """The dollars the measured tail allows (``0`` when the tail was never priced)."""
        report = self.tail_report
        if report is None:
            return 0.0
        return float(getattr(report, "tail_capacity_usd", 0.0) or 0.0)

    @property
    def all_in_usd(self) -> float:
        return sum(grant.all_in_usd for grant in self.grants)

    def describe(self) -> str:
        """One operator line: what was sized, on what tail, and what refused."""
        sized = (
            "; ".join(
                f"{g.token_id[:10]} {g.shares:.4f} sh @ {g.avg_price:.4f} "
                f"(${g.all_in_usd:.2f} all-in, fee ${g.fee_usd:.4f}, p_win {g.p_win:.4f})"
                for g in self.grants
            )
            or "none"
        )
        report = self.tail_report
        tail = (
            f"tail budget ${self.tail_budget_usd:,.2f} at q={report.tail_pd:.6f} / "
            f"{report.tail_confidence:.1%} band, appetite ${report.risk_appetite_usd:,.2f}, "
            f"shortfall ${report.expected_shortfall_usd:,.2f}, "
            f"binding={report.binding_constraint}"
            if report is not None
            else "tail budget NOT priced"
        )
        gas = (
            f"gas ${self.gas_usd:.5f}"
            if self.gas_usd is not None
            else "gas UNMEASURED (refused)"
        )
        refused = (
            " | refused: " + ", ".join(reason for reason, _detail in self.refusals)
            if self.refusals
            else ""
        )
        return (
            f"settlement decision: {len(self.grants)} position(s), "
            f"{self.priced} +EV candidate(s), sized {sized} | {tail} | {gas}{refused}"
        )


@dataclass(frozen=True)
class Landing:
    """One settled buy as it landed, with the flight it paid to get there.

    ``cost_usd`` and ``fee_usd`` are the *worst case at the limit price*, not the
    walk's average: the order is one fill-or-kill limit at the deepest funded
    level, so the debit is bounded by ``shares * limit + fee`` and can only be
    better if the cheaper levels still hold depth.
    """

    condition_id: str
    token_id: str
    shares: float
    limit_price: float
    cost_usd: float
    fee_usd: float
    flight_seconds: float
    accepted: bool
    detail: str = ""
