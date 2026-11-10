"""Data types of the Domain 2 settlement allocator.

``SettlementCandidate`` carries a candidate's CLOB ladder and its closed-form
mathematics; ``SettlementAllocation`` and ``SettlementOptimizationResult`` are
the solver's output records; ``HeldExposure`` and ``Position`` describe the
inventory already held.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.strategy.settlement.hazard import redemption_gas_usd

from ._shared import SECONDS_PER_YEAR

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from .risk import TailBudgetReport


@dataclass
class SettlementCandidate:
    """A resolving market candidate with CLOB ask ladder and settlement horizon."""

    condition_id: str
    token_id: str
    question: str
    target_settle_sec: float
    p_win: float
    """P(the token pays 1.00): the oracle's verdict after the measured hazard.

    Deliberately has no default. A default of ``1.0`` reads as "certain" and
    prices par with zero dispute haircut, which is the same shortcut that made
    ``final_payout`` accept pending UMA statuses as par: a caller that forgot
    the hazard would silently fund a -1.26 % EV trade. An unknown survival
    probability must be a ``TypeError``, never a 1.
    """
    ask_ladder: List[Tuple[float, float]] = field(default_factory=list)  # [(ask_price, shares_size), ...]
    # Per-redemption gas from the shared Polygon PoS EIP-1559 model
    # (rewards/queue_valuation.PolygonPoSEnvironment), never a local constant.
    gas_usd: float = field(default_factory=redemption_gas_usd)
    fee_schedule: Optional[FeeSchedule] = None
    min_order_shares: float = 0.0  # Venue orderMinSize (shares); 0 means unconstrained
    event_key: Optional[str] = None
    """Measured resolution-source key: Gamma's ``events[].id`` or
    ``negRiskMarketID`` for the market.  ``None`` means the condition is its
    own resolution source - the narrowest possible group - so an unmeasured
    event never merges two sources into one risk group."""
    event_exhaustive: bool = False
    """True when the group's legs are mutually exclusive *and* exhaustive
    (Gamma's ``negRisk`` flag), so exactly one of them pays 1.00."""
    risk_price_per_usd: float = 0.0
    """Shadow price of the tail budget, USD charged per USD of all-in cost.

    A *solver variable*, never an input: :meth:`optimize_tail_budget` sets it to
    ``mu * q_c / (1 - alpha)`` for the cluster this candidate resolves in, so
    the marginal dollar pays exactly the budget's shadow price for the tail
    exposure it creates.  ``0.0`` means the tail budget is not binding.
    """

    def __post_init__(self) -> None:
        """Sanitize ladder and sort ascending by ask price."""
        self.target_settle_sec = max(1.0, float(self.target_settle_sec))
        self.p_win = max(0.0, min(1.0, float(self.p_win)))
        self.min_order_shares = max(0.0, float(self.min_order_shares))
        self.gas_usd = max(0.0, float(self.gas_usd))
        clean_ladder: List[Tuple[float, float]] = []
        for px, sz in self.ask_ladder:
            p = float(px)
            s = float(sz)
            if 0.0 < p < 1.0 and s > 0.0:
                clean_ladder.append((p, s))
        clean_ladder.sort(key=lambda x: x[0])
        self.ask_ladder = clean_ladder

    @property
    def best_ask(self) -> float:
        """Lowest available ask price."""
        if self.ask_ladder:
            return self.ask_ladder[0][0]
        return 1.0

    @property
    def total_depth_usd(self) -> float:
        """Real capital capacity in USD across all ask levels (the only market cap)."""
        return sum(px * sz for px, sz in self.ask_ladder)

    def per_share_fee(self, price: float) -> float:
        """Exact taker fee per share at ``price`` from the market's fee schedule.

        Polymarket charges ``shares * rate * (p * (1 - p)) ** exponent``; the
        per-share component is therefore independent of order size.
        """
        if self.fee_schedule is None or self.fee_schedule.rate <= 0.0:
            return 0.0
        p = min(1.0, max(0.0, float(price)))
        return self.fee_schedule.rate * ((p * (1.0 - p)) ** self.fee_schedule.exponent)

    def fee_for_fills(self, fills: Sequence[Tuple[float, float, float]]) -> float:
        """Exact total taker fee for a set of ``(price, shares, cost)`` fills."""
        return sum(shs * self.per_share_fee(px) for px, shs, _ in fills)

    def fills_for_shares(
        self, shares: float
    ) -> Tuple[float, List[Tuple[float, float, float]]]:
        """Cost and level fills needed to buy exactly ``shares`` on this ladder.

        Returns ``(0.0, [])`` when the ladder cannot supply that many shares.
        """
        remaining = float(shares)
        if remaining <= 0.0:
            return 0.0, []
        cost = 0.0
        fills: List[Tuple[float, float, float]] = []
        for px, sz in self.ask_ladder:
            if remaining <= 1e-12:
                break
            take = min(remaining, sz)
            level_cost = take * px
            fills.append((px, take, level_cost))
            cost += level_cost
            remaining -= take
        if remaining > 1e-9:
            return 0.0, []
        return cost, fills

    @property
    def min_ticket_shares(self) -> float:
        """Smallest order the venue accepts, in shares.

        ``min_order_shares`` is the venue's ``orderMinSize``; when the venue
        publishes none, one share (the asset's own unit) is the smallest order
        that can exist, which is what makes the fixed redemption gas a real cost
        rather than a free option.
        """
        return max(float(self.min_order_shares), 1.0)

    def is_fundable(self) -> bool:
        """True when the *smallest order the venue accepts* has positive expectancy.

        This is the gas- and fee-aware entry test the marginal velocity cannot
        express on its own: gas is a fixed charge per market, so the smallest
        ticket is the least profitable tranche and a market whose minimum ticket
        cannot clear gas plus fees must never be started.
        """
        if not self.ask_ladder:
            return False
        want = self.min_ticket_shares
        cost, fills = self.fills_for_shares(want)
        if cost <= 0.0:
            return False
        net = self.p_win * want - cost - self.fee_for_fills(fills) - self.gas_usd
        return net > 0.0

    def ticket_net_profit(self, shares: float) -> float:
        """Expected net profit of a ``shares``-sized order net of fee and gas."""
        cost, fills = self.fills_for_shares(shares)
        if cost <= 0.0:
            return 0.0
        return self.p_win * float(shares) - cost - self.fee_for_fills(fills) - self.gas_usd

    def marginal_edge(self, price: Optional[float] = None) -> float:
        """Per-share expected edge of the next share at ``price`` (default: best ask).

        The same expression the paper pillar funds on, in the same units:
        ``p_win - price - taker_fee(1 share, price, schedule)``.  The fee is the
        *_exact* one-share fee (5 dp rounded) rather than the unrounded
        ``per_share_fee``, so a research allocation and a live paper fill cannot
        disagree about whether a level was fundable.  Without a measured
        schedule the fee is zero here and the pillar refuses the market outright;
        this branch therefore only exists for the schedule-less research path.

        ``risk_price_per_usd`` (the tail budget's shadow price) is charged on the
        all-in dollar, i.e. on ``price + fee``, because the money the failure
        branch loses is exactly what the order debits.
        """
        p = self.best_ask if price is None else float(price)
        if not 0.0 < p < 1.0:
            return 0.0
        fee = 0.0 if self.fee_schedule is None else taker_fee(1.0, p, self.fee_schedule)
        edge = self.p_win - p - fee
        if self.risk_price_per_usd > 0.0:
            edge -= self.risk_price_per_usd * (p + fee)
        return edge

    @property
    def initial_marginal_velocity(self) -> float:
        """Initial marginal velocity d(V*C)/dC at C = 0, net of survival and fees."""
        p = self.best_ask
        if p >= 1.000 or p <= 0.0:
            return 0.0
        return (self.marginal_edge(p) / p) * (SECONDS_PER_YEAR / self.target_settle_sec)


@dataclass
class SettlementAllocation:
    """Executable capital allocation for a settlement candidate."""

    condition_id: str
    token_id: str
    question: str
    allocated_usd: float
    shares: float
    effective_avg_price: float
    marginal_price: float
    marginal_velocity: float
    levels_consumed: int
    net_profit_usd: float
    annualized_arr: float
    duration_sec: float
    gas_fee_usd: float
    fee_usd: float = 0.0
    """Exact taker fee the funded tranche pays, USD."""
    all_in_usd: float = 0.0
    """``allocated_usd + fee_usd`` - what the order actually debits.

    A caller that hands this to a landing budget must use the all-in figure:
    the pillar's affordability test charges ``shares * price + fee``, so a
    grant of the fee-exclusive cost silently truncates the order (measured: a
    54.0-share grant landed 53.96 shares).
    """
    level_fills: List[Tuple[float, float, float]] = field(default_factory=list)  # [(price, shares, cost_usd)]
    # -- tail-budget report (populated by the tail solver) ------------------
    mu_per_usd: float = 0.0
    """Expected net profit of the funded tranche per dollar of capital."""
    tail_pd: float = 0.0
    """``q_c`` - the measured upper bound on this resolution cluster reversing."""
    notional_at_risk_usd: float = 0.0
    """All-in cost of this tranche that the failure branch loses (LGD = 1)."""
    tail_charge_usd: float = 0.0
    """Shortfall this tranche contributes: ``q_c * notional / (1 - alpha)``."""
    weight: float = 0.0
    """``all_in_usd / equity`` - the portfolio weight, an OUTPUT."""
    risk_contribution: float = 0.0
    """Share of the book's expected shortfall carried by this tranche."""
    risk_group: str = ""
    """Resolution-source cluster this exposure was charged to."""
    risk_links: str = ""
    """The protocol fact that placed this candidate in its cluster."""


@dataclass
class SettlementOptimizationResult:
    """Aggregated portfolio optimization statement for Strategy 2."""

    total_allocated_usd: float
    usable_capital_usd: float
    allocations: List[SettlementAllocation]
    expected_portfolio_profit_usd: float
    equalized_marginal_velocity: float
    # -- tail-budget statement (None/0 when the tail model was not used) ---
    tail_budget: Optional["TailBudgetReport"] = None
    """The measured tail inputs, the band loss and the binding constraint."""


@dataclass(frozen=True)
class HeldExposure:
    """A position already held, as the tail budget charges it.

    ``claimed_token_id`` is the token the *current* oracle verdict says pays
    1.00 for this condition.  It is what distinguishes a hedge from a double
    charge: the other token of one binary condition pays par when the claimed
    one does not, which is arithmetic on the payoff structure.  When it is
    ``None`` the allocator will not credit a hedge it cannot identify, and the
    position is charged its full cost.
    """

    condition_id: str
    token_id: str
    shares: float
    cost_usd: float
    claimed_token_id: Optional[str] = None
    cluster_key: Optional[str] = None

    @property
    def cluster(self) -> str:
        return str(self.cluster_key) if self.cluster_key else self.condition_id


@dataclass(frozen=True)
class Position:
    """The token identity of one exposure, for the link classifier."""

    condition_id: str
    token_id: str
    cluster: str
