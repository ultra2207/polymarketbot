"""The tick's decision: gate each market, size it, and state every refusal.

This module re-implements nothing: the gate is
:mod:`polymarketbot.strategy.settlement.decision`, the dollars are
:mod:`polymarketbot.strategy.settlement.allocator`, the appetite and band come
from the paper path's own loader
(:mod:`polymarketbot.paper.settlement_risk`, the single named place that reads
``configs/risk.yaml``), and the measured tail comes from
:mod:`polymarketbot.strategy.settlement.tail_evidence`.

Two refusals are inherited, not softened:

* **No oracle proposal, no price.** ``verdict_for`` returns ``None`` and the
  candidate is refused. A book mid is never substituted for the oracle's value.
* **No measured gas or POL/USD, no order.** Both inputs must resolve; the
  measured probes are called with an explicit unmeasured sentinel so "the
  measurement failed" is a ``None`` here, never an invented Gwei or dollar. An
  unpriced on-chain exit must not be treated as free, and the action refuses.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from polymarketbot.paper.settlement_risk import (
    RISK_CONFIG_PATH,
    SettlementTailPolicy,
)
from polymarketbot.strategy.settlement import decision as settlement
from polymarketbot.strategy.settlement.allocator import (
    HeldExposure,
    MarginalSettlementAllocator,
    SettlementCandidate,
    SettlementOptimizationResult,
)
from polymarketbot.strategy.settlement.tail_evidence import TailEvidence

from .models import PricedMarket, SettlementDecision, SettlementGrant

# Refusal reasons. Named so a zero-fill hour states which gate stopped the buy
# instead of logging nothing. The first five are the paper pillar's own names.
NO_VERDICT = "no_live_verdict"
NO_ASK = "no_ask"
NO_EDGE = "edge_unpriced"
NEGATIVE_EDGE = "edge_negative"
MARKET_NOT_TRADABLE = "market_not_accepting_orders"
# Live-only refusals: inputs the paper book can proceed without and a real-money
# book cannot.
GAS_UNMEASURED = "gas_unmeasured"
NO_APPETITE = "no_stated_risk_appetite"
NO_TAIL = "no_measured_tail"
NO_GRANT = "no_grant"
BELOW_SHADOW_FLOOR = "no_candidate_above_the_shadow_floor"
NO_RTT = "no_measured_round_trip"


def _best_ask(asks: Iterable[tuple[float, float]]) -> Optional[float]:
    """Lowest tradable ask in the ladder, or ``None`` when the book is empty."""
    prices = [price for price, size in asks if 0.0 < price < 1.0 and size > 0.0]
    return min(prices) if prices else None


def _capital_day_rate(
    allocator: MarginalSettlementAllocator, candidate: SettlementCandidate
) -> Optional[float]:
    """The allocator's own profit-per-capital-day at the venue's smallest ticket.

    The shadow-floor test lives in the solver; this states *its* number in the
    refusal line (the solver's own static helper, no second formula) so an idle
    desk can be told apart from one whose only candidates sit below the measured
    alternative yield.
    """
    helper = getattr(type(allocator), "_profit_per_dollar_day", None)
    if helper is None:  # pragma: no cover - a solver refactor must not crash the desk
        return None
    shares = candidate.min_ticket_shares
    cost, fills = candidate.fills_for_shares(shares)
    if cost <= 0.0:
        return None
    all_in = cost + candidate.fee_for_fills(fills)
    return float(helper(candidate.ticket_net_profit(shares), all_in, candidate.target_settle_sec))


def _candidate_for(
    item: PricedMarket,
    *,
    verdict: Any,
    token_id: str,
    gas_usd: float,
    settle_sec: float,
) -> SettlementCandidate:
    """The allocator candidate for one gated market.

    Built from the *decision module's* gated verdict (``p_win``, winning token)
    and Gamma's own fee schedule and minimum ticket, so the allocator prices
    exactly what the paper pillar prices.
    """
    return SettlementCandidate(
        condition_id=item.market.condition_id,
        token_id=token_id,
        question=item.market.question,
        target_settle_sec=settle_sec,
        p_win=float(verdict.p_win),
        ask_ladder=list(item.asks(token_id)),
        fee_schedule=item.market.fee_schedule,
        min_order_shares=item.market.min_order,
        event_key=item.event_key,
        event_exhaustive=bool(item.exhaustive),
        gas_usd=float(gas_usd),
    )


def decide(
    *,
    priced: Sequence[PricedMarket],
    cash_usd: float,
    model: Any,
    policy: Optional[SettlementTailPolicy],
    evidence: Optional[TailEvidence],
    gas_usd: Optional[float],
    settle_sec: float,
    prior: Sequence[HeldExposure] = (),
    equity_usd: Optional[float] = None,
    shadow_yield: Optional[float] = None,
    allocator: Optional[MarginalSettlementAllocator] = None,
) -> SettlementDecision:
    """Size live settlement buys with the paper pillar's gate and tail budget.

    Every candidate must pass, in this order: the measured gas (``gas_usd`` is
    not ``None``), the stated appetite and the measured tail, the oracle's
    verdict (:func:`polymarketbot.strategy.settlement.decision.verdict_for`),
    a live ask on the *verdict's* token, and a strictly positive marginal edge
    (:func:`...decision.edge_inputs`). What survives is handed to
    :meth:`MarginalSettlementAllocator.optimize_tail_budget`, whose dollars are
    then converted onto the ladder by
    :func:`...decision.fundable_legs` - the same two steps, in the same order,
    as the paper pillar's tick and landing.
    """
    if gas_usd is None:
        # No measured exit cost: nothing is sized, and the reason is explicit.
        return SettlementDecision(
            refusals=((GAS_UNMEASURED, "no measured gas price or POL/USD"),)
        )
    if policy is None:
        return SettlementDecision(
            gas_usd=float(gas_usd),
            refusals=((NO_APPETITE, f"no appetite stated in {RISK_CONFIG_PATH}"),),
        )
    if evidence is None:
        return SettlementDecision(
            gas_usd=float(gas_usd),
            refusals=((NO_TAIL, "no measured verdict-reversal tail"),),
        )

    solver = allocator or MarginalSettlementAllocator("tail_budget")
    refusals: list[tuple[str, str]] = []
    candidates: list[SettlementCandidate] = []
    gated: dict[str, tuple[PricedMarket, Any, settlement.EdgeInputs]] = {}

    for item in priced:
        market = item.market
        verdict = settlement.verdict_for(model, market)
        if verdict is None:
            # The invariant this module inherits: a market without a proposal
            # has no verdict and therefore no price. The book is not a value.
            refusals.append((NO_VERDICT, market.condition_id))
            continue
        index = int(verdict.winning_index)
        if not 0 <= index < len(market.token_ids):
            refusals.append((NO_VERDICT, f"{market.condition_id} verdict indexes no token"))
            continue
        token_id = market.token_ids[index]
        ask = _best_ask(item.asks(token_id))
        if ask is None:
            refusals.append((NO_ASK, f"{market.condition_id[:10]} {token_id[:10]}"))
            continue
        inputs = settlement.edge_inputs(market, verdict, ask)
        if inputs is None:
            refusals.append((NO_EDGE, f"{market.condition_id[:10]} {token_id[:10]}"))
            continue
        if inputs.edge_per_share <= 0.0:
            break_even = (
                "unmeasurable" if inputs.break_even_ask is None
                else f"{inputs.break_even_ask:.4f}"
            )
            refusals.append(
                (
                    NEGATIVE_EDGE,
                    f"{market.condition_id[:10]} ask {ask:.4f} >= break-even "
                    f"{break_even} at p_win {inputs.p_win:.4f}",
                )
            )
            continue
        candidates.append(
            _candidate_for(
                item,
                verdict=verdict,
                token_id=token_id,
                gas_usd=gas_usd,
                settle_sec=settle_sec,
            )
        )
        gated[market.condition_id] = (item, verdict, inputs)

    if not candidates:
        return SettlementDecision(
            refusals=tuple(refusals), gas_usd=float(gas_usd), priced=0
        )

    result: SettlementOptimizationResult = solver.allocate(
        candidates,
        max(0.0, float(cash_usd)),
        risk_appetite_usd=policy.risk_appetite_usd,
        tail_confidence=policy.tail_confidence,
        tail_evidence=evidence,
        prior=prior,
        equity_usd=equity_usd,
        capital_hurdle_usd_per_usd_per_day=shadow_yield,
    )
    report = result.tail_budget
    grants: list[SettlementGrant] = []

    for row in result.allocations:
        item, verdict, inputs = gated[row.condition_id]
        # The allocator's grant is the whole order (its own docstring: a
        # fee-exclusive grant silently truncates it). The decision's own ladder
        # gate then converts it into legs, exactly as the paper landing does.
        budget = float(row.all_in_usd)
        legs = settlement.fundable_legs(item.market, verdict, item.asks(row.token_id), budget)
        if not legs:
            refusals.append(
                (
                    NO_GRANT,
                    f"{row.condition_id[:10]} granted ${budget:,.2f} but the ladder "
                    "funded no leg at the venue minimum",
                )
            )
            continue
        shares = sum(leg.shares for leg in legs)
        cost = sum(leg.shares * leg.price for leg in legs)
        fee = sum(leg.fee_usd for leg in legs)
        grants.append(
            SettlementGrant(
                condition_id=row.condition_id,
                token_id=row.token_id,
                question=item.market.question,
                p_win=float(verdict.p_win),
                budget_usd=budget,
                shares=shares,
                cost_usd=cost,
                fee_usd=fee,
                all_in_usd=cost + fee,
                avg_price=(cost / shares) if shares > 0.0 else 0.0,
                levels=len(legs),
                tail_pd=float(row.tail_pd),
                tail_charge_usd=float(row.tail_charge_usd),
                break_even_ask=inputs.break_even_ask,
                ask=float(inputs.ask),
            )
        )

    funded = {row.condition_id for row in result.allocations}
    for candidate in candidates:
        if candidate.condition_id in funded:
            continue
        _item, _verdict, inputs = gated[candidate.condition_id]
        rate = _capital_day_rate(solver, candidate)
        rate_text = "unmeasured" if rate is None else f"${rate:.5f}/$/day"
        below_floor = (
            shadow_yield is not None and rate is not None and rate < float(shadow_yield)
        )
        alternative = (
            f"${float(shadow_yield):.5f}/$/day" if shadow_yield is not None else "UNPRICED"
        )
        tail_note = (
            f"; tail capacity ${report.tail_capacity_usd:,.2f} of "
            f"${report.risk_appetite_usd:,.2f} appetite at q={report.tail_pd:.6f}"
            if report is not None
            else ""
        )
        refusals.append(
            (
                BELOW_SHADOW_FLOOR if below_floor else NO_GRANT,
                f"{candidate.condition_id[:10]} best ask {inputs.ask:.4f} "
                f"p_win {inputs.p_win:.4f} edge/share ${inputs.edge_per_share:+.6f} "
                f"profit {rate_text} vs alternative {alternative}{tail_note}",
            )
        )

    return SettlementDecision(
        grants=tuple(grants),
        refusals=tuple(refusals),
        gas_usd=float(gas_usd),
        tail_report=report,
        priced=len(candidates),
    )


def held_exposures(
    *,
    condition_id: str,
    token_ids: Sequence[str],
    shares: Sequence[float],
    cost_usd: Sequence[float],
    claimed_token_id: Optional[str] = None,
    cluster_key: Optional[str] = None,
) -> list[HeldExposure]:
    """The live book already held, as the tail budget charges it.

    One record per held token, the same shape the paper pillar charges: that
    inventory is exposure the failure branch loses whether or not this desk
    opened it. ``claimed_token_id`` is what lets the allocator credit the other
    leg as a hedge instead of double-charging the condition.
    """
    exposures: list[HeldExposure] = []
    for token_id, held, cost in zip(token_ids, shares, cost_usd):
        if float(held) <= 0.0:
            continue
        exposures.append(
            HeldExposure(
                condition_id=condition_id,
                token_id=str(token_id),
                shares=float(held),
                cost_usd=float(cost),
                claimed_token_id=claimed_token_id,
                cluster_key=cluster_key,
            )
        )
    return exposures
