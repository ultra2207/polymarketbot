"""Live settlement buys: the paper pillar's verdict gate and tail budget, on the venue.

Before this module the live trader had **no settlement sizing at all**. It merged
complete sets it already held (``live/engine/merge.py``), redeemed its own
inventory after the market closed (``--redeem-on-close``), and never bought a
resolved-but-unfinalised position. The paper pillar that is being audited sizes
that buy with two shared modules:

* :mod:`polymarketbot.strategy.settlement.decision` - the verdict gate
  (``verdict_for`` refuses without an oracle proposal, ``edge_inputs`` /
  ``fundable_legs`` price the ask against that verdict);
* :mod:`polymarketbot.strategy.settlement.allocator` - the tail budget
  (``optimize_tail_budget`` maximises expected profit inside the measured
  expected shortfall, at the appetite and band stated in ``configs/risk.yaml``).

This module is *that same decision* for the live trader. It re-implements
nothing: the gate is ``decision``, the dollars are the allocator, the appetite
and band come from the paper path's own loader
(:mod:`polymarketbot.paper.settlement_risk`, the single named place that reads
``configs/risk.yaml``), the measured tail comes from
:mod:`polymarketbot.strategy.settlement.tail_evidence`, and the gas cost comes
from the measured on-chain path (:mod:`polymarketbot.strategy.onchain.ctf.gas`).

Two refusals are inherited, not softened:

* **No oracle proposal, no price.** ``verdict_for`` returns ``None`` and the
  candidate is refused. A book mid is never substituted for the oracle's value.
* **No measured gas or POL/USD, no order.** Both inputs must resolve; the
  measured probes are called with an explicit unmeasured sentinel so "the
  measurement failed" is a ``None`` here, never an invented Gwei or dollar. An
  unpriced on-chain exit must not be treated as free, and the action refuses.

The order itself is not executed where the decision is made: it is queued on
:class:`polymarketbot.strategy.latency.ActionBook` and lands one measured CLOB
round trip later, against the book that exists at landing time (re-read, and
re-gated, in :meth:`LiveSettlementDesk.land_due`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from polymarketbot.fees import taker_fee
from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.paper.settlement_risk import (
    RISK_CONFIG_PATH,
    SettlementTailPolicy,
    load_settlement_tail_policy,
)
from polymarketbot.strategy.latency import Action, ActionBook
from polymarketbot.strategy.onchain.ctf.gas import (
    estimate_merge_cost_usd,
    get_dynamic_gas_price_gwei,
    get_dynamic_pol_usd_price,
)
from polymarketbot.strategy.settlement import decision as settlement
from polymarketbot.strategy.settlement.allocator import (
    HeldExposure,
    MarginalSettlementAllocator,
    SettlementCandidate,
    SettlementOptimizationResult,
    TailBudgetReport,
)
from polymarketbot.strategy.settlement.hazard import DEFAULT_CTF_REDEEM_GAS_UNITS
from polymarketbot.strategy.settlement.tail_evidence import (
    TailEvidence,
    UnmeasuredTail,
    load_tail_evidence,
)
from polymarketbot.strategy.venue.reads import Market, parse_market

log = get_logger(__name__)

#: Strategy name settlement buys are booked under - the paper pillar's own name,
#: so a fill reads the same in both ledgers.
STRATEGY = "SettlementHarvester"

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


# ---------------------------------------------------------------------------
# Measured gas
# ---------------------------------------------------------------------------
def measure_gas_usd(
    *,
    gas_units: int = DEFAULT_CTF_REDEEM_GAS_UNITS,
    chain_client: Any = None,
) -> Optional[float]:
    """One on-chain settlement action's cost in USD, or ``None`` when unmeasured.

    The probes are :mod:`polymarketbot.strategy.onchain.ctf.gas`'s own, called
    with ``nan`` as their documented fallback so "nothing resolved" is
    *detectable* instead of being silently replaced by the module's invented
    Gwei/POL baselines. ``None`` is a refusal: an unpriced exit is not free, and
    the live trader must not size a position whose redemption cost it guessed.
    """
    gwei = get_dynamic_gas_price_gwei(
        chain_client=chain_client, fallback_gwei=float("nan")
    )
    if not (math.isfinite(gwei) and gwei > 0.0):
        log.warning(
            "Settlement gas refused: no Polygon gas price could be measured "
            "(RPC base fee, eth_gasPrice and the fast oracle all failed)"
        )
        return None
    pol_usd = get_dynamic_pol_usd_price(fallback_price=float("nan"))
    if not (math.isfinite(pol_usd) and pol_usd > 0.0):
        log.warning(
            "Settlement gas refused: no POL/USD ticker resolved, and a guessed "
            "rate would price gas wrong"
        )
        return None
    usd = estimate_merge_cost_usd(
        pol_usd_price=pol_usd,
        gas_price_gwei=gwei,
        gas_units=int(gas_units),
        chain_client=chain_client,
    )
    log.info(
        f"Settlement gas measured: {int(gas_units):,} units x {gwei:.2f} Gwei x "
        f"${pol_usd:.5f}/POL = ${usd:.5f}"
    )
    return float(usd)


def load_market(condition_id: str, *, gamma: Optional[GammaClient] = None) -> Optional[Market]:
    """The tradable market for ``condition_id``, or ``None``.

    ``venue.parse_market`` is the venue's own trading gate: a market that has
    stopped accepting orders cannot be bought at any price, so there is no
    settlement *buy* to size. Returning ``None`` says exactly that instead of
    pricing a book that does not exist.
    """
    client = gamma or GammaClient()
    try:
        row = client.market(condition_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable venue is a refusal
        log.warning(f"Settlement market lookup for {condition_id[:10]} failed: {exc}")
        return None
    if not isinstance(row, dict) or not row:
        return None
    return parse_market(row)


# ---------------------------------------------------------------------------
# The decision's inputs and outputs
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# The desk: plan on the tick, land one measured round trip later
# ---------------------------------------------------------------------------
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


class LiveSettlementDesk:
    """Settlement buys for the live trader: plan, queue, land against a fresh book.

    The decision is taken on the tick (book snapshot, verdict, allocator) and the
    order is queued on an :class:`ActionBook`: it becomes executable only once
    one measured CLOB round trip has elapsed, and it then re-reads the book and
    re-gates the verdict, so it lands against the state that exists at landing
    time rather than the snapshot that triggered it. A desk with no measured
    round trip submits nothing - never at 0 ms.
    """

    def __init__(
        self,
        *,
        model: Any,
        policy: Optional[SettlementTailPolicy],
        evidence: Optional[TailEvidence],
        actions: ActionBook,
        book_reader: Callable[[str], Optional[dict]],
        place_taker: Callable[[str, float, float], bool],
        gas_probe: Callable[..., Optional[float]] = measure_gas_usd,
        allocator: Optional[MarginalSettlementAllocator] = None,
        cluster_key: Optional[Callable[[str], Optional[str]]] = None,
        exhaustive: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.model = model
        self.policy = policy
        self.evidence = evidence
        self.actions = actions
        self._book_reader = book_reader
        self._place_taker = place_taker
        self._gas_probe = gas_probe
        self._allocator = allocator or MarginalSettlementAllocator("tail_budget")
        self._cluster_key = cluster_key
        self._exhaustive = exhaustive
        self.last_decision: Optional[SettlementDecision] = None
        self.landings: list[Landing] = []

    @classmethod
    def build(
        cls,
        *,
        actions: ActionBook,
        book_reader: Callable[[str], Optional[dict]],
        place_taker: Callable[[str, float, float], bool],
        gas_probe: Callable[..., Optional[float]] = measure_gas_usd,
        policy: Optional[SettlementTailPolicy] = None,
        evidence: Optional[TailEvidence] = None,
        model: Any = None,
        config_path: str = RISK_CONFIG_PATH,
        allocator: Optional[MarginalSettlementAllocator] = None,
        cluster_key: Optional[Callable[[str], Optional[str]]] = None,
        exhaustive: Optional[Callable[[str], bool]] = None,
    ) -> "LiveSettlementDesk":
        """Resolve the three inputs once, from one named place each.

        A missing appetite or an unmeasured tail is logged and carried as
        ``None``: the desk then refuses every buy (``decide`` states which input
        is missing) instead of inventing one.
        """
        if policy is None:
            try:
                policy = load_settlement_tail_policy(config_path)
                log.info(f"settlement tail policy: {policy.describe()}")
            except Exception as exc:  # noqa: BLE001 - absence is a refusal, not a crash
                log.error(
                    f"settlement tail policy unavailable ({exc}): the live trader "
                    "will NOT size a settlement buy until an appetite is stated"
                )
        if evidence is None:
            try:
                evidence = load_tail_evidence()
                log.info(f"settlement tail evidence: {evidence.describe()}")
            except (UnmeasuredTail, OSError, ValueError) as exc:
                log.error(
                    f"settlement outcome-error tail unavailable ({exc}): the live "
                    "trader will NOT size a settlement buy on an assumed tail"
                )
        if model is None:
            model = settlement.model()
        return cls(
            model=model,
            policy=policy,
            evidence=evidence,
            actions=actions,
            book_reader=book_reader,
            place_taker=place_taker,
            gas_probe=gas_probe,
            allocator=allocator,
            cluster_key=cluster_key,
            exhaustive=exhaustive,
        )

    def settle_seconds(self) -> float:
        """Measured settlement horizon: the tape's liveness, never a default."""
        tape = getattr(self.model, "tape", None)
        measured = float(getattr(tape, "measured_liveness_sec", 0.0) or 0.0)
        return max(1.0, measured)

    def claimed_token(self, market: Market) -> Optional[str]:
        """The oracle's winning token for ``market``, or ``None`` without a verdict.

        This is what lets the tail budget credit the *other* leg of a held
        position as a hedge instead of charging it twice: with no verdict the
        hedge is unidentified and the position is charged in full.
        """
        verdict = settlement.verdict_for(self.model, market)
        if verdict is None:
            return None
        index = int(verdict.winning_index)
        if not 0 <= index < len(market.token_ids):
            return None
        return market.token_ids[index]

    def plan(
        self,
        *,
        markets: Sequence[Market],
        cash_usd: float,
        prior: Sequence[HeldExposure] = (),
        equity_usd: Optional[float] = None,
        shadow_yield: Optional[float] = None,
    ) -> SettlementDecision:
        """Price the markets, size them, and queue the funded conditions.

        Returns the decision either way: a refusal is a result here, not an
        exception, so the caller logs it and retries on the next tick.
        """
        gas_usd = self._gas_probe()
        refresh = getattr(self.model, "refresh", None)
        if callable(refresh):
            refresh()
        priced: list[PricedMarket] = []
        not_tradable: list[tuple[str, str]] = []
        for market in markets:
            ladders = {
                token: tuple(_ask_ladder(self._book_reader(token)))
                for token in market.token_ids
            }
            if not any(ladders.values()):
                # No ask on either leg: there is no settlement buy to size at
                # any price. Distinct from a negative edge, which a book exists
                # for.
                not_tradable.append((MARKET_NOT_TRADABLE, market.condition_id))
                continue
            priced.append(
                PricedMarket(
                    market=market,
                    ladders=ladders,
                    event_key=(
                        self._cluster_key(market.condition_id)
                        if self._cluster_key is not None
                        else None
                    ),
                    exhaustive=bool(
                        self._exhaustive(market.condition_id)
                        if self._exhaustive is not None
                        else False
                    ),
                )
            )
        decision = decide(
            priced=priced,
            cash_usd=cash_usd,
            model=self.model,
            policy=self.policy,
            evidence=self.evidence,
            gas_usd=gas_usd,
            settle_sec=self.settle_seconds(),
            prior=prior,
            equity_usd=equity_usd,
            shadow_yield=shadow_yield,
            allocator=self._allocator,
        )
        if not_tradable:
            decision = SettlementDecision(
                grants=decision.grants,
                refusals=tuple(not_tradable) + decision.refusals,
                gas_usd=decision.gas_usd,
                tail_report=decision.tail_report,
                priced=decision.priced,
            )
        queued = self._queue(decision, priced)
        self.last_decision = queued
        return queued

    def _queue(
        self, decision: SettlementDecision, priced: Sequence[PricedMarket]
    ) -> SettlementDecision:
        """Hand each grant to the landing queue; drop them all if there is no RTT."""
        if not decision.grants:
            return decision
        by_condition = {item.market.condition_id: item.market for item in priced}
        queued: list[Action] = []
        for grant in decision.grants:
            market = by_condition.get(grant.condition_id)
            if market is None:  # pragma: no cover - a grant implies a market
                continue
            action = self.actions.submit(
                "settle_buy",
                STRATEGY,
                {
                    "market": market,
                    "condition_id": grant.condition_id,
                    "token_id": grant.token_id,
                    "budget_usd": grant.all_in_usd,
                    "p_win": grant.p_win,
                    "ask": grant.ask,
                },
            )
            if action is None:
                # ``ActionBook`` refuses without a measured round trip. A partial
                # queue would report "nothing sized" while an order was in flight,
                # so pull back whatever was already queued.
                if queued:
                    ids = {id(item) for item in queued}
                    self.actions.cancel(lambda pending, ids=ids: id(pending) in ids)
                return SettlementDecision(
                    refusals=decision.refusals
                    + ((NO_RTT, "no measured CLOB round trip"),),
                    gas_usd=decision.gas_usd,
                    tail_report=decision.tail_report,
                    priced=decision.priced,
                )
            queued.append(action)
        return decision

    def land_due(self) -> list[Landing]:
        """Land every queued buy whose flight has elapsed, against a fresh book."""
        landed: list[Landing] = []
        for action in self.actions.due():
            try:
                landed.extend(self._land(action))
            except Exception as exc:  # noqa: BLE001 - one landing must not stop the rest
                log.error(
                    f"[SETTLEMENT LANDING] {str(action.payload.get('condition_id'))[:10]} "
                    f"failed to land: {exc}"
                )
        self.landings.extend(landed)
        return landed

    def pending(self) -> list[Action]:
        return self.actions.pending()

    def _land(self, action: Action) -> list[Landing]:
        payload = action.payload
        market: Market = payload["market"]
        budget = float(payload.get("budget_usd") or 0.0)
        token_id = str(payload.get("token_id") or "")
        condition_id = str(payload.get("condition_id") or "")
        flight = max(0.0, float(action.lands) - float(action.submitted))
        if budget <= 0.0 or not token_id:
            return []
        # Landing re-reads the oracle and the book: the decision's snapshot is
        # not evidence about the venue one round trip later.
        refresh = getattr(self.model, "refresh", None)
        if callable(refresh):
            refresh()
        verdict = settlement.verdict_for(self.model, market)
        if verdict is None:
            log.warning(
                f"[SETTLEMENT LANDING] {condition_id[:10]} refused: the oracle verdict "
                "is gone at landing"
            )
            return []
        winning_token = market.token_ids[int(verdict.winning_index)]
        if winning_token != token_id:
            log.warning(
                f"[SETTLEMENT LANDING] {condition_id[:10]} refused: the verdict moved "
                f"from {token_id[:10]} to {winning_token[:10]} during the flight"
            )
            return []
        asks = _ask_ladder(self._book_reader(token_id))
        if not asks:
            log.warning(
                f"[SETTLEMENT LANDING] {condition_id[:10]} refused: no book at landing"
            )
            return []
        legs = settlement.fundable_legs(market, verdict, asks, budget)
        if not legs:
            log.info(
                f"[SETTLEMENT LANDING] {condition_id[:10]} landed with nothing to buy: "
                f"the ladder funds no leg inside the ${budget:,.2f} grant"
            )
            return []
        # One fill-or-kill taker order at the deepest funded price reproduces the
        # sized walk when the depth is there and cannot fill worse: the venue
        # crosses the cheaper levels first, and FOK means no residue is left
        # resting at the limit on a market that has stopped trading. The order's
        # size is capped so that *every* share filling at the limit still fits
        # the grant - a limit order does not bound the spend by the walk's
        # average price, so the walk's share count is the ceiling, not the size.
        limit_price = max(leg.price for leg in legs)
        walk_shares = sum(leg.shares for leg in legs)
        schedule = market.fee_schedule
        shares = walk_shares
        for _ in range(4):
            fee = taker_fee(shares, limit_price, schedule) if schedule else 0.0
            debit = shares * limit_price + fee
            if debit <= budget + 1e-9:
                break
            shares = min(
                walk_shares,
                max(0.0, (budget - fee) / limit_price),
            )
        if shares < float(market.min_order) or shares <= 0.0:
            log.info(
                f"[SETTLEMENT LANDING] {condition_id[:10]} refused: the walk's "
                f"{walk_shares:.4f} shares at a {limit_price:.4f} limit cannot fit "
                f"the ${budget:,.2f} grant at the venue minimum"
            )
            return []
        fee = taker_fee(shares, limit_price, schedule) if schedule else 0.0
        cost = shares * limit_price
        edge = sum(leg.edge_usd for leg in legs) * (shares / walk_shares) if walk_shares > 0 else 0.0
        accepted = bool(self._place_taker(token_id, limit_price, shares))
        landing = Landing(
            condition_id=condition_id,
            token_id=token_id,
            shares=shares,
            limit_price=limit_price,
            cost_usd=cost,
            fee_usd=fee,
            flight_seconds=flight,
            accepted=accepted,
            detail=(
                f"{len(legs)} level(s) walked, worst-case ${cost + fee:,.4f} at the "
                f"limit, p_win {float(verdict.p_win):.4f}, edge ${edge:+.4f}"
            ),
        )
        log.info(
            f"[SETTLEMENT LANDING] {condition_id[:10]} "
            f"{'placed' if accepted else 'REJECTED'} FOK BUY {shares:.4f} @ "
            f"{limit_price:.4f} (worst case ${cost + fee:,.4f} of a ${budget:,.4f} "
            f"grant, walk {walk_shares:.4f} sh) after {flight * 1000.0:.1f}ms flight | "
            f"{landing.detail}"
        )
        return [landing]


def _ask_ladder(book: Optional[dict]) -> list[tuple[float, float]]:
    """``(price, shares)`` ask levels from a feed snapshot or a paper-shaped book.

    Accepts the feed's ``snapshot.asks`` (objects, or ``(price, size)`` pairs) or
    a dict with an ``asks`` list of dicts, so the desk reads the same structure
    the paper pillar's ``books.ladder`` reads without a second venue parser.
    """
    if book is None:
        return []
    levels = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
    out: list[tuple[float, float]] = []
    for level in levels or []:
        try:
            if isinstance(level, dict):
                price, size = float(level.get("price")), float(level.get("size"))
            elif hasattr(level, "price"):
                price, size = float(level.price), float(level.size)
            else:
                price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            out.append((price, size))
    out.sort(key=lambda item: item[0])
    return out
