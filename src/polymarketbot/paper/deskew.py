"""Complete-set merge and inventory de-skew for the paper engine.

The paper trader quotes a dual-sided matched pair, but a fill lands on one leg
at a time, so between fills it holds directional inventory. Live runs the same
strategy with three exits for that state and this module gives paper the same
three, driven by the same shared decision code:

* **Complete-set merge** - once both outcome legs are held, ``Ledger.merge``
  burns the matched shares and returns par. The batch size is the fitted
  ``DynamicThresholdCalculator.compute_auto_merge_threshold``, so a merge that
  cannot amortise its gas is held as a complete set (risk-free, redeemable)
  exactly as live holds it.
* **Deskew** - ``strategy.reward.deskew.DynamicDeSkewOptimizer`` decides
  ``HOLD_STABLE`` / ``PASSIVE_FADE`` / ``SYNTHETIC_MERGE`` / ``DIRECT_CROSS``
  per condition. The optimizer is the shared one, not a copy.
* **Taker landing** - the Tier-2 cross is submitted into ``ActionBook`` and
  lands after the measured round trip against the book that exists on arrival
  (``paper.actions.land_taker``), never at the decision snapshot.

Measured inputs, and what happens when one is missing
-----------------------------------------------------
Every optimizer argument is a measurement: the fitted calibration artifact,
merge gas read from the persisted gas/POL measurements, the market's fee
schedule, the measured two-sided book, the published reward minimum, the
holding horizon, and the tape print rate measured from the prints the tick
already fetched. A missing calibration, gas measurement or remaining-life
reference disables the action that needs it and logs why; nothing is defaulted
and no gas constant is invented.

Model differences that remain (documented, not hidden)
------------------------------------------------------
* The paper ``Market`` payload carries no end date, so "time to resolution" is
  the fit's measured ``remaining_life_days_reference`` for every market. Live
  prices each market's own remaining life.
* Fills are replayed from the public tape; a taker cross here is filled against
  the simulated landing book, and a merge is atomic at tick time. Live fills are
  venue executions and its merge waits for a Polygon block + receipt.
* The venue's published taker hold (live: ``get_taker_delay_s``, 250 ms on some
  markets) is not part of the paper ``Market`` payload, so the cross pays the
  measured CLOB round trip only.
* Live's Tier-1 fade subtracts a fitted ``spread_offset_cents`` (from an
  economics artifact the paper trader does not load); paper uses one tick.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymarketbot.fees import FeeSchedule
from polymarketbot.logs import get_logger
from polymarketbot.paper import risk as paper_risk
from polymarketbot.paper.actions import TAKER_KIND as TAKER_ACTION
from polymarketbot.strategy.books import best_ask, best_bid, shares_at
from polymarketbot.strategy.onchain.ctf.gas import (
    DEFAULT_MAX_STALE_SECONDS,
    estimate_merge_cost_usd,
)
from polymarketbot.strategy.reward.deskew import (
    DESKEW_CALIBRATION_ARTIFACT,
    VENUE_MIN_ORDER_SHARES,
    DeSkewCalibration,
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
    DynamicThresholdCalculator,
    load_deskew_calibration,
)
from polymarketbot.strategy.reward.microstructure import MicrostructureEngine

log = get_logger(__name__)

#: Where the measured Polygon gas price and POL/USD price are persisted. The
#: live engine measures these through its chain client; the paper trader has no
#: chain client and reuses the same recorded measurements.
GAS_ARTIFACT = Path("data/derived/onchain_gas.json")
POL_ARTIFACT = Path("data/derived/onchain_pol.json")
#: The tape fit the OFI / micro-price sentinels consume (shared with live).
RISK_CALIBRATION_ARTIFACT = Path("data/derived/risk_calibration.json")

#: A recorded gas price older than the live engine's own staleness limit is not
#: a measurement of today's chain. The shared module owns the number.
MERGE_GAS_MAX_AGE_S = DEFAULT_MAX_STALE_SECONDS
#: Minimum seconds between Tier-2 crosses on one condition (live:
#: ``deskew_min_interval_s``). A cross travels the CLOB round trip and an
#: in-flight one must resolve before another is stacked on top of it.
DESKEW_MIN_INTERVAL_S = 30.0


# --------------------------------------------------------------- measurements
def _recorded_value(
    path: Path, key: str, now: float, max_age_s: float
) -> float | None:
    """One recorded measurement from ``path``, or ``None`` when absent/stale."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        log.warning(f"measurement {path} unreadable: {exc}")
        return None
    if not isinstance(payload, dict):
        return None
    try:
        value = float(payload.get(key) or 0.0)
        recorded_at = float(payload.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    if value <= 0.0:
        return None
    age = max(0.0, float(now) - recorded_at)
    if recorded_at <= 0.0 or age > float(max_age_s):
        log.warning(
            f"measurement {path}[{key}]={value} is "
            f"{'undated' if recorded_at <= 0.0 else f'{age:.0f}s old'} "
            f"(limit {max_age_s:.0f}s); refusing to use it"
        )
        return None
    return value


def load_measured_merge_gas_usd(
    gas_path: str | Path = GAS_ARTIFACT,
    pol_path: str | Path = POL_ARTIFACT,
    *,
    now: float | None = None,
    max_age_s: float | None = None,
) -> float | None:
    """Dollar cost of one merge from the persisted gas and POL measurements.

    ``None`` means unmeasured, never zero: a merge priced at zero gas is a
    merge whose cost was assumed away. The documented gas-unit budget of the
    shared estimator is the protocol's, not a model parameter.
    """
    stamp = time.time() if now is None else float(now)
    limit = MERGE_GAS_MAX_AGE_S if max_age_s is None else float(max_age_s)
    gwei = _recorded_value(Path(gas_path), "gwei", stamp, limit)
    pol_usd = _recorded_value(Path(pol_path), "usd", stamp, limit)
    if gwei is None or pol_usd is None:
        log.warning(
            "merge gas unmeasured: "
            f"{gas_path} gwei={'n/a' if gwei is None else f'{gwei:.4f}'}, "
            f"{pol_path} usd={'n/a' if pol_usd is None else f'{pol_usd:.6f}'}"
        )
        return None
    cost = estimate_merge_cost_usd(gas_price_gwei=gwei, pol_usd_price=pol_usd)
    if cost is None or float(cost) <= 0.0:
        log.warning(f"merge gas unmeasured: estimator returned {cost!r}")
        return None
    return float(cost)


def measure_merge_gas_usd() -> float | None:
    """Measure merge gas now, falling back to a recording only if it is fresh.

    The shared estimator reads the chain's gas price from public RPC and
    POL/USD from listed venues, and replays a persisted reading only while it
    is inside :data:`MERGE_GAS_MAX_AGE_S`. ``None`` means unmeasured; a
    transport failure is a refusal, never a crash or a zero.
    """
    try:
        cost = estimate_merge_cost_usd()
    except Exception as exc:  # noqa: BLE001 - a measurement failure is a refusal
        log.warning(f"merge gas measurement failed: {exc}")
        return None
    if cost is None or float(cost) <= 0.0:
        log.warning(f"merge gas unmeasured: estimator returned {cost!r}")
        return None
    return float(cost)


def load_measured_resolution_days(
    path: str | Path = DESKEW_CALIBRATION_ARTIFACT,
) -> float | None:
    """Measured remaining-life reference from the de-skew fit, or ``None``.

    The paper ``Market`` payload does not carry an end date, so this is the
    only measured life available. Using it is a stated model difference (see
    the module docstring), not an invented horizon.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    diagnostics = payload.get("diagnostics") if isinstance(payload, dict) else None
    if not isinstance(diagnostics, dict):
        return None
    try:
        days = float(diagnostics.get("remaining_life_days_reference") or 0.0)
    except (TypeError, ValueError):
        return None
    return days if days > 0.0 else None


def measured_trades_per_min(
    prints: list[Any], condition_id: str, window_s: float
) -> float:
    """Prints per minute for one condition over the tape window already fetched."""
    if window_s <= 0.0:
        return 0.0
    count = sum(1 for item in prints if getattr(item, "condition_id", None) == condition_id)
    return float(count) * 60.0 / float(window_s)


def taker_fee_bps(schedule: FeeSchedule, price: float) -> float:
    """Taker fee as basis points of notional at ``price``.

    The venue charges ``C * rate * (p(1-p))^exponent`` (``fees.taker_fee``), so
    the fee per dollar crossed is that over ``p``. Same derivation as
    ``live.base.BaseMarketMakingEngine.taker_fee_bps_at``, computed here so the
    paper package does not import the live engine at decision time.
    """
    p = float(price)
    if not 0.0 < p < 1.0:
        raise ValueError(f"cannot price a taker fee at {price!r}")
    fraction = schedule.rate * ((p * (1.0 - p)) ** schedule.exponent) / p
    return float(fraction) * 10_000.0


def _risk_coefficients() -> Any | None:
    """The fitted OFI coefficients, or ``None`` when the artifact is absent.

    Live passes zero flow when its microstructure gate has no fit; the paper
    engine does the same and says so once, rather than inventing coefficients.
    """
    try:
        from polymarketbot.live.base import load_risk_calibration

        return load_risk_calibration(RISK_CALIBRATION_ARTIFACT)
    except Exception as exc:  # noqa: BLE001 - a missing gate is not a crash
        log.warning(f"risk calibration unavailable ({exc}); OFI sentinel disabled")
        return None


# ---------------------------------------------------------------------- state
@dataclass(frozen=True)
class Skew:
    """A deskew-derived replacement price for one leg of a resting pair."""

    condition_id: str
    token_id: str
    price: float
    pathway: str
    reason: str


@dataclass
class DeskewState:
    """Everything the merge/de-skew paths need, and their refusal reasons."""

    calibration: DeSkewCalibration | None = None
    gas_cost_usd: float | None = None
    resolution_days: float | None = None
    risk: Any | None = None
    merge_refusal: str = ""
    """Non-empty when a merge may not run (no calibration or no measured gas)."""
    deskew_refusal: str = ""
    """Non-empty when de-skewing may not run (the merge refusals, or no life)."""
    skews: dict[str, dict[str, Skew]] = field(default_factory=dict)
    """condition_id -> token_id -> the price the deskew wants that leg at."""
    inventory_since: dict[str, float] = field(default_factory=dict)
    last_attempt: dict[str, float] = field(default_factory=dict)
    flows: dict[str, MicrostructureEngine] = field(default_factory=dict)
    last_decision: dict[str, DeSkewDecision] = field(default_factory=dict)
    merges: int = 0
    takers: int = 0

    @property
    def merge_enabled(self) -> bool:
        return not self.merge_refusal and self.calibration is not None and (
            self.gas_cost_usd is not None and self.gas_cost_usd > 0.0
        )

    @property
    def deskew_enabled(self) -> bool:
        return self.merge_enabled and not self.deskew_refusal


def build_state(measure_gas: bool = False) -> DeskewState:
    """Load the measured inputs once, at engine startup.

    ``measure_gas=True`` takes a fresh chain measurement (public RPC + POL
    venues, the same call the live engine makes) and falls back to a recording
    only while it is fresh. The default reads the persisted measurements, which
    is what a test and an offline run get.

    Missing measured inputs are refusals with a stated reason, never defaults:
    quoting continues, and the merge / de-skew action that needs the number
    does not run.
    """
    calibration: DeSkewCalibration | None = None
    calibration_error = ""
    try:
        calibration = load_deskew_calibration()
    except (TypeError, ValueError) as exc:
        calibration_error = str(exc)

    gas = measure_merge_gas_usd() if measure_gas else load_measured_merge_gas_usd()
    resolution_days = load_measured_resolution_days()

    reasons: list[str] = []
    if calibration is None:
        reasons.append(
            "no fitted de-skew calibration "
            f"({DESKEW_CALIBRATION_ARTIFACT})"
            + (f": {calibration_error}" if calibration_error else "")
        )
    if gas is None:
        reasons.append(f"merge gas unmeasured ({GAS_ARTIFACT} / {POL_ARTIFACT})")
    merge_refusal = "; ".join(reasons)
    deskew_refusal = merge_refusal
    if not deskew_refusal and resolution_days is None:
        deskew_refusal = (
            "remaining life unmeasured "
            f"({DESKEW_CALIBRATION_ARTIFACT} has no reference)"
        )

    state = DeskewState(
        calibration=calibration,
        gas_cost_usd=gas,
        resolution_days=resolution_days,
        merge_refusal=merge_refusal,
        deskew_refusal=deskew_refusal,
    )
    if merge_refusal:
        log.warning(
            f"paper merge/de-skew refused: {merge_refusal}. Quoting continues; "
            "no complete set is merged and no inventory is crossed"
        )
    elif deskew_refusal:
        log.warning(
            f"paper de-skew refused: {deskew_refusal}. Merges stay enabled at "
            f"${gas:.6f}/merge; quoting continues"
        )
    else:
        log.info(
            f"paper de-skew enabled: gas ${gas:.6f}/merge, "
            f"remaining life {resolution_days:.2f}d (fit reference), "
            f"gamma={calibration.gamma:.6f}"
        )
        state.risk = _risk_coefficients()
    return state


def get_state(engine) -> DeskewState | None:
    return getattr(engine, "deskew", None)


def skew_target(engine, token_id: str) -> Skew | None:
    """The deskew price for ``token_id``, or ``None`` for the planner's price."""
    state = get_state(engine)
    if state is None:
        return None
    for legs in state.skews.values():
        target = legs.get(token_id)
        if target is not None:
            return target
    return None


# ------------------------------------------------------------------ helpers
def _held(
    ledger, strategy: str, condition_id: str, token_ids: tuple[str, ...]
) -> dict[str, float]:
    held = {token_id: 0.0 for token_id in token_ids}
    for lot in ledger.positions(strategy):
        if lot.condition_id == condition_id and lot.token_id in held:
            held[lot.token_id] += lot.shares
    return held


def _spread_cents(book: dict[str, Any]) -> float | None:
    bid, ask = best_bid(book), best_ask(book)
    if bid is None or ask is None or ask <= bid:
        return None
    return (float(ask) - float(bid)) * 100.0


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0.0:
        return float(price)
    steps = round(float(price) / float(tick))
    return max(float(tick), min(1.0 - float(tick), steps * float(tick)))


def _quote_size(engine, condition_id: str, market) -> float:
    """The share count the condition is quoting, in the same units live uses."""
    sizes = [
        float(quote.initial_shares or quote.shares)
        for quote in engine.reward_quotes.values()
        if quote.condition_id == condition_id
        and quote.cancel_at is None
        and quote.shares > 0.0
    ]
    if sizes:
        return max(sizes)
    return max(
        VENUE_MIN_ORDER_SHARES,
        float(getattr(market, "reward_min_size", 0.0) or 0.0),
    )


def _reservation_price(
    mid_yes: float, net_q: float, gamma: float, days: float
) -> float:
    """Avellaneda-Stoikov reservation price, using the shared bounded variance."""
    tau = max(1.0 / 24.0, float(days))
    variance = DynamicThresholdCalculator.calculate_bounded_variance(mid_yes, tau)
    return max(0.01, min(0.99, mid_yes - (net_q * gamma * variance)))


def _flow(
    state: DeskewState, condition_id: str, book_yes: dict[str, Any]
) -> tuple[float, float]:
    """(rolling OFI, micro-price drift in cents) for one condition (0,0 = calm)."""
    if state.risk is None:
        return 0.0, 0.0
    engine_flow = state.flows.get(condition_id)
    if engine_flow is None:
        if state.calibration is None:
            return 0.0, 0.0
        engine_flow = MicrostructureEngine(
            c_depth=float(state.risk.c_depth),
            z_score=float(state.risk.z_score),
            v_ref=float(state.risk.v_ref),
            beta_vol=float(state.risk.beta_vol),
            pull_multiplier=float(state.risk.pull_multiplier),
            widen_offset_multiplier=float(state.risk.widen_offset_multiplier),
            # The thresholds are the fitted toxicity triggers of the same tape
            # fit the de-skew optimizer was calibrated against.
            ofi_threshold=float(state.calibration.ofi_toxicity_threshold),
            drift_threshold_cents=float(state.calibration.drift_toxicity_cents),
        )
        state.flows[condition_id] = engine_flow
    bid, ask = best_bid(book_yes), best_ask(book_yes)
    if bid is None or ask is None:
        return 0.0, 0.0
    snapshot = engine_flow.update(
        best_bid_price=float(bid),
        best_bid_size=shares_at(book_yes, bid=True, price=float(bid)),
        best_ask_price=float(ask),
        best_ask_size=shares_at(book_yes, bid=False, price=float(ask)),
        timestamp=time.time(),
    )
    return float(snapshot.ofi_10_events), float(snapshot.drift_cents)


# --------------------------------------------------------------- merges
def run_merges(engine, books: dict[str, dict], strategies: tuple[str, ...]) -> None:
    """Merge every condition's matched shares once both legs are held.

    Runs after the fill pass, so a pair completed this tick is merged on the
    same tick. Two legs and measured gas are both required; a batch below the
    fitted auto-merge threshold is left as a complete set (live does the same:
    merging below it spends more gas than the spread it unlocks).
    """
    state = get_state(engine)
    if state is None or not state.merge_enabled:
        return
    assert state.calibration is not None and state.gas_cost_usd is not None
    gas = float(state.gas_cost_usd)

    for strategy in strategies:
        grouped: dict[str, dict[str, float]] = {}
        for lot in engine.ledger.positions(strategy):
            legs = grouped.setdefault(lot.condition_id, {})
            legs[lot.token_id] = legs.get(lot.token_id, 0.0) + lot.shares
        for condition_id, legs in grouped.items():
            if len(legs) < 2:
                continue
            (token_a, shares_a), (token_b, shares_b) = sorted(
                legs.items(), key=lambda item: item[1], reverse=True
            )[:2]
            if shares_a <= 1e-9 or shares_b <= 1e-9:
                continue
            market = engine.by_condition.get(condition_id)
            # Only a binary condition's two legs are complements: a neg-risk
            # multi-outcome set is not merged pairwise, and ``Ledger.merge``
            # would pair it with whichever sibling it met first.
            if market is not None and not market.binary:
                continue
            if market is None and len(legs) != 2:
                continue
            spread = _spread_cents(books.get(token_a) or {})
            if spread is None:
                log.debug(
                    f"merge skipped for {condition_id[:12]}: no two-sided "
                    f"{token_a[:10]} book to price the gas hurdle"
                )
                continue
            matched = min(shares_a, shares_b)
            bankroll = float(engine.ledger.equity().get(strategy, 0.0))
            threshold = DynamicThresholdCalculator.compute_auto_merge_threshold(
                gas_cost_usd=gas,
                spread_cents=spread,
                quote_size=_quote_size(engine, condition_id, market),
                bankroll=bankroll,
                calibration=state.calibration,
            )
            if matched + 1e-9 < threshold:
                log.debug(
                    f"merge skipped for {condition_id[:12]}: {matched:.2f} sets "
                    f"below fitted threshold {threshold:.2f}; holding the set"
                )
                continue
            cash_before = engine.ledger.cash(strategy)
            pnl = engine.ledger.merge(
                strategy,
                condition_id=condition_id,
                token_id=token_a,
                pair_shares=matched,
                gas=gas,
                tag="ctf_merge",
            )
            after = _held(engine.ledger, strategy, condition_id, (token_a, token_b))
            if (
                after.get(token_a, 0.0) > shares_a - matched + 1e-6
                or after.get(token_b, 0.0) > shares_b - matched + 1e-6
            ):
                # ``Ledger.merge`` refuses without burning either leg; the cash
                # check keeps an unexpected refusal from being logged as a merge.
                log.warning(
                    f"merge refused for {condition_id[:12]}: ledger did not burn "
                    f"{matched:.2f} sets"
                )
                continue
            state.merges += 1
            log.info(
                f"paper merge: {matched:.2f} complete sets on {condition_id[:12]} "
                f"-> ${matched:.2f} par, gas ${gas:.6f} | "
                f"cash ${cash_before:.2f} -> ${engine.ledger.cash(strategy):.2f} | "
                f"realised {pnl:+.4f}"
            )


# --------------------------------------------------------------- de-skew
def _passive_skew(
    state: DeskewState,
    market,
    decision: DeSkewDecision,
    net_q: float,
    bid_yes: float,
    ask_yes: float,
    bid_no: float,
    ask_no: float,
) -> dict[str, Skew]:
    """The two bid prices a passive fade rests, in the paper's bid space.

    The optimizer returns a YES ask for an excess-YES book; the paper trader
    quotes bids, so the target is mirrored through the complement
    (``1 - ask``), the same mapping live's institutional fade uses. The excess
    side is faded toward its reservation price so the pair is no longer the
    symmetric bid pair that produced the imbalance.
    """
    assert state.calibration is not None and state.resolution_days is not None
    tick = float(getattr(market, "tick", 0.0) or 0.0)
    if tick <= 0.0:
        tick = 0.001
    token_yes, token_no = market.token_ids[0], market.token_ids[1]
    mid_yes = (bid_yes + ask_yes) / 2.0
    reservation = _reservation_price(
        mid_yes, net_q, state.calibration.gamma, state.resolution_days
    )
    max_bid_yes = _round_to_tick(ask_yes - tick, tick)
    max_bid_no = _round_to_tick(ask_no - tick, tick)
    legs: dict[str, Skew] = {}
    if net_q > 0:
        missing = _round_to_tick(
            1.0 - max(bid_yes + tick, float(decision.target_price)), tick
        )
        missing = min(missing, max_bid_no)
        excess = min(_round_to_tick(reservation - tick, tick), max_bid_yes)
        if missing > 0.0:
            legs[token_no] = Skew(
                condition_id=market.condition_id,
                token_id=token_no,
                price=missing,
                pathway=decision.pathway.value,
                reason="missing NO leg (complete the set) under a long-YES fade",
            )
        if excess > 0.0:
            legs[token_yes] = Skew(
                condition_id=market.condition_id,
                token_id=token_yes,
                price=excess,
                pathway=decision.pathway.value,
                reason="excess YES bid faded toward the reservation price",
            )
    else:
        missing = min(
            _round_to_tick(float(decision.target_price), tick), max_bid_yes
        )
        excess = min(
            _round_to_tick(1.0 - (reservation + tick), tick), max_bid_no
        )
        if missing > 0.0:
            legs[token_yes] = Skew(
                condition_id=market.condition_id,
                token_id=token_yes,
                price=missing,
                pathway=decision.pathway.value,
                reason="missing YES leg (complete the set) under a long-NO fade",
            )
        if excess > 0.0:
            legs[token_no] = Skew(
                condition_id=market.condition_id,
                token_id=token_no,
                price=excess,
                pathway=decision.pathway.value,
                reason="excess NO bid faded toward the reservation price",
            )
    return legs


def _submit_taker(
    engine,
    state: DeskewState,
    strategy: str,
    market,
    decision: DeSkewDecision,
    held: dict[str, float],
    cash: float,
    quote_size: float,
    now: float,
) -> None:
    """Submit one Tier-2 cross; it lands after the measured round trip."""
    condition_id = market.condition_id
    target_token = str(decision.target_token).upper()
    side = str(decision.action_side).upper()
    if target_token not in ("YES", "NO") or side not in ("BUY", "SELL"):
        return
    if now - state.last_attempt.get(condition_id, 0.0) < DESKEW_MIN_INTERVAL_S:
        return
    # Every attempt is rate-limited, refusals included: a condition that stays
    # cash-short must not log a refusal on every tick.
    state.last_attempt[condition_id] = now
    for pending in engine.actions.pending():
        if pending.kind != TAKER_ACTION:
            continue
        if pending.payload.get("condition_id") == condition_id:
            log.debug(
                f"de-skew cross skipped for {condition_id[:12]}: one is already "
                "in flight"
            )
            return

    token_id = market.token_ids[0] if target_token == "YES" else market.token_ids[1]
    size = min(float(decision.target_size), float(quote_size) * 2.0)
    # The live Tier-2 path caps the cross at the shared per-order share cap;
    # the paper cross must not book a size the live gate would have clamped.
    share_cap = paper_risk.max_quote_shares(engine)
    if share_cap > 0.0:
        size = min(size, share_cap)
    if side == "SELL":
        size = min(size, float(held.get(token_id, 0.0)))
    if size <= 0.0:
        log.debug(
            f"de-skew cross skipped for {condition_id[:12]}: size after caps is 0"
        )
        return
    refusal = paper_risk.taker_refusal(
        engine,
        strategy=strategy,
        token_id=token_id,
        side=side,
        shares=size,
        price=float(decision.target_price),
    )
    if refusal is not None:
        log.warning(
            f"de-skew cross refused for {condition_id[:12]}: {refusal}"
        )
        return
    if side == "BUY" and size * float(decision.target_price) > cash + 1e-9:
        log.warning(
            f"de-skew cross refused for {condition_id[:12]}: BUY "
            f"{size:.2f} {target_token} @ ${decision.target_price:.3f} "
            f"= ${size * decision.target_price:.2f} exceeds free cash ${cash:.2f}"
        )
        return
    try:
        rtt = engine._rtt()
    except RuntimeError as exc:
        log.error(f"de-skew cross refused: {exc}")
        return
    payload = {
        "condition_id": condition_id,
        "token_id": token_id,
        "target_token": target_token,
        "side": side,
        "shares": float(size),
        "pathway": decision.pathway.value,
        "decision_price": float(decision.target_price),
        "fee_schedule": market.fee_schedule,
        "reason": decision.reason,
    }
    action = engine.actions.submit(TAKER_ACTION, strategy, payload, rtt=rtt)
    if action is None:
        log.warning(
            f"de-skew cross refused for {condition_id[:12]}: no measured round trip"
        )
        return
    state.takers += 1
    log.warning(
        f"paper de-skew {decision.pathway.value}: {side} {size:.2f} {target_token} "
        f"submitted at decision price ${decision.target_price:.3f}; lands after "
        f"{rtt * 1000.0:.0f}ms of measured RTT against the book then"
    )


def run_deskew(engine, books: dict[str, dict], prints: list[Any], strategies: tuple[str, ...]) -> None:
    """Re-evaluate every condition's directional inventory and act on it.

    Runs before the reward planner so a passive fade is what this tick's plan
    rests, and after ``_land`` so yesterday's fills are already in the ledger.
    """
    state = get_state(engine)
    if state is None or not state.deskew_enabled:
        return
    if paper_risk.blocks_actions(engine):
        # Live refuses the Tier-2 cross on a halt too (its gate checks the
        # risk manager before sizing). Stale fades must not survive into a
        # later tick after an operator resumes.
        state.skews.clear()
        return
    assert state.calibration is not None
    assert state.gas_cost_usd is not None
    assert state.resolution_days is not None
    now = time.time()
    window_s = max(120.0, 4.0 * float(getattr(engine, "interval", 10.0)))
    # A condition that left the universe can no longer be re-priced; its last
    # fade must not survive into a later tick.
    for stale in [cid for cid in state.skews if cid not in engine.by_condition]:
        state.skews.pop(stale, None)
    for stale in [cid for cid in state.inventory_since if cid not in engine.by_condition]:
        state.inventory_since.pop(stale, None)

    for strategy in strategies:
        equity = float(engine.ledger.equity().get(strategy, 0.0))
        if equity <= 0.0:
            continue
        cash = float(engine.ledger.cash(strategy))
        reserved = engine._reserved(engine.reward_quotes)
        cash_reserve = max(0.0, cash - reserved)
        for condition_id, market in engine.by_condition.items():
            if not market.binary or len(market.token_ids) != 2:
                continue
            token_yes, token_no = market.token_ids
            held = _held(engine.ledger, strategy, condition_id, (token_yes, token_no))
            net_q = held[token_yes] - held[token_no]
            if abs(net_q) <= 1e-9:
                state.inventory_since.pop(condition_id, None)
                state.skews.pop(condition_id, None)
                continue
            state.inventory_since.setdefault(condition_id, now)
            book_yes = books.get(token_yes) or {}
            book_no = books.get(token_no) or {}
            bid_yes, ask_yes = best_bid(book_yes), best_ask(book_yes)
            bid_no, ask_no = best_bid(book_no), best_ask(book_no)
            if (
                bid_yes is None or ask_yes is None
                or bid_no is None or ask_no is None
                or not (0.0 < bid_yes < ask_yes < 1.0)
                or not (0.0 < bid_no < ask_no < 1.0)
            ):
                state.skews.pop(condition_id, None)
                continue
            schedule = market.fee_schedule
            if schedule is None:
                state.skews.pop(condition_id, None)
                log.debug(
                    f"de-skew skipped for {condition_id[:12]}: no measured fee "
                    "schedule, a taker leg cannot be priced as free"
                )
                continue
            if float(market.reward_min_size) <= 0.0:
                state.skews.pop(condition_id, None)
                log.debug(
                    f"de-skew skipped for {condition_id[:12]}: no published "
                    "rewards minimum size"
                )
                continue
            trades_per_min = measured_trades_per_min(prints, condition_id, window_s)
            if trades_per_min <= 0.0:
                state.skews.pop(condition_id, None)
                log.debug(
                    f"de-skew skipped for {condition_id[:12]}: no prints in the "
                    f"measured tape window, the holding horizon is unmeasured"
                )
                continue
            mid_yes = (bid_yes + ask_yes) / 2.0
            quote_size = _quote_size(engine, condition_id, market)
            try:
                optimizer = DynamicDeSkewOptimizer(
                    quote_size=quote_size,
                    bankroll=equity,
                    cash_reserve=cash_reserve,
                    taker_fee_bps=taker_fee_bps(schedule, mid_yes),
                    calibration=state.calibration,
                    mid_price=mid_yes,
                    time_to_resolution_days=state.resolution_days,
                    spread_cents=(ask_yes - bid_yes) * 100.0,
                    gas_cost_usd=state.gas_cost_usd,
                    trades_per_min=trades_per_min,
                    rewards_min_size=float(market.reward_min_size),
                    time_to_expiry_seconds=state.resolution_days * 86_400.0,
                )
            except (TypeError, ValueError) as exc:
                state.skews.pop(condition_id, None)
                log.warning(
                    f"de-skew skipped for {condition_id[:12]}: optimizer refused "
                    f"its measured inputs ({exc})"
                )
                continue
            ofi, drift_cents = _flow(state, condition_id, book_yes)
            decision = optimizer.evaluate_skew(
                held[token_yes],
                held[token_no],
                bid_yes,
                ask_yes,
                bid_no,
                ask_no,
                holding_time_seconds=max(
                    0.0, now - state.inventory_since[condition_id]
                ),
                rolling_ofi=ofi,
                micro_price_drift_cents=drift_cents,
                time_to_resolution_days=state.resolution_days,
            )
            state.last_decision[condition_id] = decision

            if decision.pathway is DeSkewPathway.HOLD_STABLE:
                state.skews.pop(condition_id, None)
                continue
            if decision.pathway is DeSkewPathway.PASSIVE_FADE:
                legs = _passive_skew(
                    state, market, decision, net_q,
                    bid_yes, ask_yes, bid_no, ask_no,
                )
                if legs:
                    state.skews[condition_id] = legs
                    log.info(
                        f"paper de-skew PASSIVE_FADE {condition_id[:12]} q={net_q:+.1f}: "
                        + ", ".join(
                            f"{leg.token_id[:8]}@{leg.price:.3f}"
                            for leg in legs.values()
                        )
                        + f" | {decision.reason}"
                    )
                else:
                    state.skews.pop(condition_id, None)
                continue

            # Tier 2: the cross is a taker order, it cannot act on this snapshot.
            state.skews.pop(condition_id, None)
            _submit_taker(
                engine, state, strategy, market, decision, held, cash,
                quote_size, now,
            )
