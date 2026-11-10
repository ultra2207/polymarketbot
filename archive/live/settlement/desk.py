"""The desk: plan on the tick, queue on the action book, land a round trip later.

The order itself is not executed where the decision is made: it is queued on
:class:`polymarketbot.strategy.latency.ActionBook` and lands one measured CLOB
round trip later, against the book that exists at landing time (re-read, and
re-gated, in :meth:`LiveSettlementDesk.land_due`).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from polymarketbot.fees import taker_fee
from polymarketbot.paper.settlement_risk import (
    RISK_CONFIG_PATH,
    SettlementTailPolicy,
    load_settlement_tail_policy,
)
from polymarketbot.strategy.latency import Action, ActionBook
from polymarketbot.strategy.settlement import decision as settlement
from polymarketbot.strategy.settlement.allocator import (
    HeldExposure,
    MarginalSettlementAllocator,
)
from polymarketbot.strategy.settlement.tail_evidence import (
    TailEvidence,
    UnmeasuredTail,
    load_tail_evidence,
)
from polymarketbot.strategy.venue.reads import Market

from .decision import MARKET_NOT_TRADABLE, NO_RTT, decide
from .measure import _ask_ladder, log, measure_gas_usd
from .models import Landing, PricedMarket, SettlementDecision

#: Strategy name settlement buys are booked under - the paper pillar's own name,
#: so a fill reads the same in both ledgers.
STRATEGY = "SettlementHarvester"


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
