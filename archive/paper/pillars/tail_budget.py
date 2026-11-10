"""The tail-budget allocator's inputs, and the grant it returns each tick.

Every number the tail model reads is measured, and the two inputs it cannot
measure - the risk appetite and the outcome-error tail - are resolved once from
one named place. When either is absent this module grants nothing: an unpriced
tail is a fact to log, not a number to invent.
"""

from __future__ import annotations

from typing import Any, Optional

from polymarketbot.logs import get_logger
import polymarketbot.strategy.venue.reads as venue
from polymarketbot.strategy.books import ladder
from polymarketbot.paper.settlement_risk import (
    SettlementTailPolicy,
    load_settlement_tail_policy,
)
from polymarketbot.strategy.settlement.allocator import (
    HeldExposure,
    SettlementCandidate,
)
from polymarketbot.strategy.settlement.tail_evidence import TailEvidence, load_tail_evidence

log = get_logger(__name__)


def _cluster_key(row: dict) -> Optional[str]:
    """The resolution source Gamma puts this market in, or ``None``.

    ``negRiskMarketID`` groups the mutually exclusive legs of one neg-risk event
    (one resolution source), and the event id is the source for every other
    market.  ``None`` means the market is its own source - the narrowest
    possible cluster - so a row Gamma did not describe never merges two
    different verdicts into one risk unit.
    """
    if not isinstance(row, dict):
        return None
    neg_group = str(row.get("negRiskMarketID") or "") or None
    if neg_group:
        return neg_group
    events = row.get("events") or []
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return str(events[0].get("id") or "") or None
    return None


def resolve_tail_inputs() -> tuple[Optional[SettlementTailPolicy], Optional[TailEvidence]]:
    """Resolve the allocator's three required inputs once, from one named place.

    The tail-budget settlement allocator maximises expected profit inside the
    loss the book may carry at the stated confidence, with the tail probability
    *measured* from the proposal ledger. All three inputs (appetite, band,
    evidence) are required: a paper book that cannot state its appetite does
    not spend.
    """
    policy: Optional[SettlementTailPolicy] = None
    evidence: Optional[TailEvidence] = None
    try:
        policy = load_settlement_tail_policy()
        log.info(f"settlement tail policy: {policy.describe()}")
    except Exception as exc:  # noqa: BLE001 - absence must be visible, not fatal
        log.error(
            f"settlement tail policy unavailable ({exc}): the harvester will "
            "NOT allocate until a risk appetite is stated"
        )
    try:
        evidence = load_tail_evidence()
        log.info(f"settlement tail evidence: {evidence.describe()}")
    except Exception as exc:  # noqa: BLE001 - same rule: absence is not zero
        log.error(
            f"settlement outcome-error tail unavailable ({exc}): the harvester "
            "will NOT allocate on an assumed tail"
        )
    return policy, evidence


def tail_budget(
    engine,
    priced: list[tuple[float, venue.Market, float, object]],
    books: dict[str, dict],
    verdict_rows: dict[str, dict],
    settle_sec: float,
    cash: float,
) -> dict[str, float]:
    """Dollars the allocator grants each condition this tick, by condition id.

    Every number the tail model reads is measured: ``p_win`` is the verdict's
    survival probability, the ladder is the venue's live ask depth, the fee
    schedule is Gamma's own, gas is the shared on-chain model, the
    resolution-source cluster is Gamma's ``negRiskMarketID`` (mutually
    exclusive legs of one event) or the market's ``events[].id``, and the
    tail probability is the measured verdict-reversal rate.

    The risk *appetite* is the one preference in this call, and it is read
    from :mod:`polymarketbot.paper.settlement_risk` at construction. When it
    or the measured tail is absent this method grants nothing: an unpriced
    tail is a fact to log, not a number to invent.

    The firm's bankroll is the reporting denominator (weights, loss as a
    share of capital); the pillar's cash remains the hard spending limit,
    because ``buy`` debits only that book.
    """
    if not priced or cash <= 0.0:
        return {}
    if engine._tail_policy is None or engine._tail_evidence is None:
        engine._settlement_tail_refusals = getattr(engine, "_settlement_tail_refusals", 0) + 1
        if engine._settlement_tail_refusals in (1, 60):
            log.error(
                "settlement allocation refused: "
                + (
                    "no stated risk appetite (configs/risk.yaml)"
                    if engine._tail_policy is None
                    else "no measured outcome-error tail"
                )
                + f" - {engine._settlement_tail_refusals} tick(s) with no budget"
            )
        return {}
    prior: list[HeldExposure] = []
    claimed_by_condition = {
        market.condition_id: market.token_ids[verdict.winning_index]
        for _edge, market, _ask, verdict in priced
    }
    for lot in engine.ledger.positions("SettlementHarvester"):
        row = verdict_rows.get(lot.condition_id) or {}
        prior.append(
            HeldExposure(
                condition_id=lot.condition_id,
                token_id=lot.token_id,
                shares=float(lot.shares),
                cost_usd=float(lot.cost_usd),
                claimed_token_id=claimed_by_condition.get(lot.condition_id),
                cluster_key=_cluster_key(row),
            )
        )
    firm_equity = sum(engine.ledger.equity().values())
    candidates: list[SettlementCandidate] = []
    for _edge, market, _ask, verdict in priced:
        token_id = market.token_ids[verdict.winning_index]
        ladder_rows = ladder(books.get(token_id) or {}, asks=True)
        row = verdict_rows.get(market.condition_id) or {}
        kwargs: dict[str, Any] = {
            "condition_id": market.condition_id,
            "token_id": token_id,
            "question": market.question,
            "target_settle_sec": settle_sec,
            "p_win": float(verdict.p_win),
            "ask_ladder": list(ladder_rows),
            "fee_schedule": market.fee_schedule,
            "min_order_shares": market.min_order,
            "event_key": _cluster_key(row),
            "event_exhaustive": bool(row.get("negRisk")),
        }
        if engine.gas_usd is not None:
            kwargs["gas_usd"] = float(engine.gas_usd)
        candidates.append(SettlementCandidate(**kwargs))
    result = engine.settlement_allocator.allocate(
        candidates,
        cash,
        risk_appetite_usd=engine._tail_policy.risk_appetite_usd,
        tail_confidence=engine._tail_policy.tail_confidence,
        tail_evidence=engine._tail_evidence,
        prior=prior,
        equity_usd=firm_equity,
        capital_hurdle_usd_per_usd_per_day=engine._shadow_yield,
    )
    return {row.condition_id: float(row.all_in_usd) for row in result.allocations}
