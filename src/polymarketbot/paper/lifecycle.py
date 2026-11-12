"""Paper market lifecycle: resolution verdicts, redemption and closed-market blocking.

The paper universe is the *active* universe (``venue.UniverseWalker``), so a
market leaves it the moment it closes, taking its held lots out of every
refresh. Without this module those lots stay open and marked forever: the
paper engine never asked Gamma what happened, and ``Ledger`` already knows how
to apply a verdict (``set_par`` / ``redeem`` / ``write_off``).

What this module does, once per poll for every condition the ledger still
holds:

* **Final verdict** - the market is ``closed`` AND its ``outcomePrices`` are a
  final binary payout (one outcome at 1, the other at 0) AND UMA has not left
  the outcome non-final (``proposed`` / ``disputed``). A mid, a last traded
  price or a stale print is never a verdict (the cycle-1 audit's S2 defect).
  On a verdict: quotes for the condition are cancelled, ``Ledger.set_par``
  applies the payout to every strategy, the winner is redeemed on-chain at the
  measured gas, and the loser is written off at zero.
* **Closed, no verdict** - cancel the condition's quotes, block it from new
  quotes, log once. Do not write anything off: the outcome is not known yet.
* **Missing / empty Gamma answer** - leave the lots open and log the gap; no
  verdict is fabricated and nothing raises.

Measured inputs, and what happens when one is missing
-----------------------------------------------------
* **Gas** - the engine's own measured CTF transaction cost
  (``DeskewState.gas_cost_usd``, from the persisted gas-price and POL/USD
  measurements in ``paper.deskew``). It prices the protocol's documented merge
  gas-unit budget, so dividing it by that budget gives the measured dollar
  cost of one gas unit; the documented ``redeemPositions`` budget then prices
  one redemption. Both budgets are protocol constants (``CTFGasProfile``), not
  economic thresholds. Unmeasured gas -> par is applied, no redemption is
  journaled, and the refusal is logged once. The winner stays held at par and
  a later poll retries.
* **Flight** - a redemption changes no state before one Polygon RPC round trip
  plus the next block. Both are measured from the shared artifacts
  (``ctf.measure``: persisted RPC RTT and the block-header tape). If either is
  unmeasured, redemption is refused for the same reason: paper never books a
  zero-latency on-chain settlement.

Polls are paced by ``RESOLUTION_POLL_INTERVAL``, the live market-lifecycle
cadence (``live.runner_market.MARKET_STATUS_INTERVAL``, 60 s); the read is a
batch ``GammaClient.markets(ids)`` over the held conditions only.

Rows are parsed here, not with ``venue.parse_market``: that function refuses
``closed`` / not-accepting markets, which are exactly the rows this module
needs. Only ``conditionId``, ``closed``, ``umaResolutionStatus``,
``clobTokenIds``, ``outcomes`` and ``outcomePrices`` are read.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from polymarketbot.live.runner_market import MARKET_STATUS_INTERVAL
from polymarketbot.logs import get_logger
from polymarketbot.paper.actions import queue_tracker
from polymarketbot.strategy.onchain.ctf.measure import (
    load_measured_block_interval_s,
    load_persisted_rpc_rtt_s,
)
from polymarketbot.strategy.reward.queue import CTFGasProfile

log = get_logger(__name__)

#: The live market-lifecycle timer, reused verbatim so paper and live ask the
#: venue the same question at the same rate. The read is a batch over held
#: conditions, so the cost per poll is one keyset request, not one per market.
RESOLUTION_POLL_INTERVAL = float(MARKET_STATUS_INTERVAL)

#: A final payout is the venue encoding 1 / 0. ``outcomePrices`` on a closed but
#: unresolved market still carries the last indicative price (0.62 / 0.38), so
#: this is a decimal-encoding tolerance, not a settlement band: anything that is
#: not both a 1 and a 0 is refused as "not final".
FINAL_PAYOUT_EPSILON = 1e-3

#: UMA states that explicitly mean the outcome is not final yet. A closed market
#: with final 1/0 prices is settled; one that additionally says the outcome is
#: proposed or disputed is not.
NON_FINAL_UMA_STATUS = frozenset({"proposed", "disputed"})


# ------------------------------------------------------------------ parsing
def _list(value: Any) -> list[Any]:
    """Gamma's list fields are JSON arrays or JSON-encoded strings."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


@dataclass(frozen=True)
class ResolutionRow:
    """The resolution-relevant fields of one Gamma market row."""

    condition_id: str
    closed: bool
    uma_status: str
    token_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    outcome_prices: tuple[float, ...]

    @property
    def final_payout(self) -> str | None:
        """The token that pays $1 at a final binary payout, or ``None``."""
        if len(self.token_ids) != 2 or len(self.outcomes) != 2:
            return None
        if len(self.outcome_prices) != 2:
            return None
        yes, no = self.outcome_prices
        if yes >= 1.0 - FINAL_PAYOUT_EPSILON and no <= FINAL_PAYOUT_EPSILON:
            return self.token_ids[0]
        if no >= 1.0 - FINAL_PAYOUT_EPSILON and yes <= FINAL_PAYOUT_EPSILON:
            return self.token_ids[1]
        return None

    @property
    def uma_final(self) -> bool:
        return self.uma_status not in NON_FINAL_UMA_STATUS


def parse_resolution(row: dict[str, Any]) -> ResolutionRow | None:
    """Parse a Gamma row without ``parse_market`` (which refuses closed markets)."""
    condition_id = str(row.get("conditionId") or row.get("condition_id") or "")
    if not condition_id:
        return None
    token_ids = tuple(str(token) for token in _list(row.get("clobTokenIds")) if str(token))
    outcomes = tuple(str(outcome) for outcome in _list(row.get("outcomes")))
    prices: list[float] = []
    for raw in _list(row.get("outcomePrices")):
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(price):
            return None
        prices.append(price)
    return ResolutionRow(
        condition_id=condition_id,
        closed=bool(row.get("closed")),
        uma_status=str(row.get("umaResolutionStatus") or "").lower(),
        token_ids=token_ids,
        outcomes=outcomes,
        outcome_prices=tuple(prices),
    )


def verdict(row: ResolutionRow) -> tuple[str | None, str]:
    """``(winning token, reason)``; ``None`` when no final verdict exists.

    Every non-verdict reason is returned, including the open case, so the
    caller can distinguish "keep quoting" from "closed and unresolved".
    """
    if not row.closed:
        return None, "market is open"
    winner = row.final_payout
    if winner is None:
        return None, (
            f"closed with no final binary payout (outcomePrices="
            f"{tuple(row.outcome_prices)!r})"
        )
    if not row.uma_final:
        return None, f"UMA status is {row.uma_status!r} (not final)"
    return winner, "closed with a final binary payout"


# ------------------------------------------------------------- measurements
@dataclass(frozen=True)
class RedeemFlight:
    """Measured time an on-chain redemption needs before it can land."""

    seconds: float = 0.0
    gaps: tuple[str, ...] = ()

    @property
    def measured(self) -> bool:
        return not self.gaps and self.seconds > 0.0


def measure_redeem_flight() -> RedeemFlight:
    """One Polygon RPC round trip plus the measured block interval.

    Both components come from the shared measured artifacts (the latency probe's
    persisted RPC p50 and the polygon_gas header tape). A missing component is a
    stated gap that refuses the redemption; it is never priced as zero latency.
    """
    gaps: list[str] = []
    rtt_s = load_persisted_rpc_rtt_s()
    block_s = load_measured_block_interval_s()
    if rtt_s is None or float(rtt_s) <= 0.0:
        gaps.append("Polygon RPC round trip unmeasured (data/derived/local_latency.json)")
        rtt_s = 0.0
    if block_s is None or float(block_s) <= 0.0:
        gaps.append("Polygon block interval unmeasured (data/tape/polygon_gas)")
        block_s = 0.0
    return RedeemFlight(float(rtt_s) + float(block_s), tuple(gaps))


def redeem_gas_usd(engine) -> float | None:
    """Measured dollar cost of one ``redeemPositions``, or ``None``.

    ``DeskewState.gas_cost_usd`` is the measured dollar cost of one CTF
    transaction at the measured all-in gas price and POL/USD (``paper.deskew``
    -> ``load_measured_merge_gas_usd``), priced with the protocol's documented
    merge gas-unit budget. Dividing by that budget gives the measured dollar
    cost of one gas unit, which the documented redeem gas-unit budget then
    prices. The two transactions share the gas price and the POL/USD rate, so
    this is the shared measured on-chain cost - not a copied merge number and
    not an assumed zero.
    """
    state = getattr(engine, "deskew", None)
    merge_cost = getattr(state, "gas_cost_usd", None)
    if merge_cost is None or float(merge_cost) <= 0.0:
        return None
    profile = CTFGasProfile()
    merge_units = float(profile.gas_merge_direct_typical)
    redeem_units = float(profile.gas_redeem_typical)
    if merge_units <= 0.0 or redeem_units <= 0.0:
        return None
    return float(merge_cost) * redeem_units / merge_units


# ------------------------------------------------------------------- state
@dataclass
class PendingRedeem:
    """A redemption whose measured on-chain flight has not landed yet."""

    lands: float
    winner: str
    gas_usd: float


@dataclass
class LifecycleState:
    """Engine-held lifecycle memory: poll clock, blocks and once-only logs."""

    last_poll: float = 0.0
    blocked: set[str] = field(default_factory=set)
    """Closed conditions that must not rest or plan a quote."""
    resolved: set[str] = field(default_factory=set)
    """Conditions whose par verdict has been applied once."""
    closed_logged: set[str] = field(default_factory=set)
    refusal_logged: set[str] = field(default_factory=set)
    redeems: dict[str, PendingRedeem] = field(default_factory=dict)


def state_for(engine) -> LifecycleState:
    current = getattr(engine, "resolution", None)
    if not isinstance(current, LifecycleState):
        current = LifecycleState()
        engine.resolution = current
    return current


def blocked_conditions(engine) -> frozenset[str]:
    """Conditions the lifecycle has closed and forbidden new quotes on."""
    return frozenset(state_for(engine).blocked)


# ------------------------------------------------------------------ quotes
def _cancel_quotes(engine, condition_id: str, now: float, *,
                   final: bool = False) -> int:
    """Cancel this condition's resting quotes and any placement still in flight.

    Returns the number of resting quotes cancelled. A placement that has not
    reached the book yet is withdrawn from ``ActionBook``: landing it after the
    cancellation would rest an order on a market no longer traded.

    ``final`` is for a *closed* market, where the venue itself retires open
    orders and no later print may fill them: the quote record is removed from
    ``reward_quotes`` immediately (its FIFO position dropped with it) instead of
    waiting out the paper cancel round trip, so the tick's fill pass cannot book
    a fill after the verdict.
    """
    cancelled = 0
    actions = getattr(engine, "actions", None)
    for token_id, quote in list(getattr(engine, "reward_quotes", {}).items()):
        if quote.condition_id != condition_id:
            continue
        if quote.cancel_at is None:
            quote.cancel_at = now
            if actions is not None:
                actions.submit("reward_cancel", quote.strategy, {"token_id": token_id})
            cancelled += 1
        if final:
            if quote.order_id:
                queue_tracker(engine).drop(token_id, quote.order_id)
            engine.reward_quotes.pop(token_id, None)
    if actions is not None:
        withdrawn = actions.cancel(
            lambda action: action.kind == "reward_quote"
            and getattr(action.payload.get("resting"), "condition_id", None) == condition_id
        )
        if withdrawn:
            log.info(
                f"paper lifecycle: withdrew {withdrawn} in-flight quote placement(s) "
                f"for {condition_id[:12]}"
            )
    return cancelled


def _apply_blocks(engine) -> None:
    """Remove blocked conditions from the engine's tradable views.

    The reward planner plans from ``engine.markets`` and every other path looks
    up ``engine.by_condition``; a blocked condition must not appear in either,
    or the next tick would re-quote a closed market and would let the merge and
    de-skew paths trade it.
    """
    blocked = state_for(engine).blocked
    if not blocked:
        return
    markets = getattr(engine, "markets", None)
    if isinstance(markets, list):
        engine.markets = [m for m in markets if m.condition_id not in blocked]
    by_condition = getattr(engine, "by_condition", None)
    if isinstance(by_condition, dict):
        engine.by_condition = {
            cid: market for cid, market in by_condition.items() if cid not in blocked
        }


# ------------------------------------------------------------------ settle
def _redeem_now(engine, condition_id: str, pending: PendingRedeem, now: float) -> None:
    """Journal the winner's redemption once its measured flight has landed."""
    redeemed = 0.0
    for strategy in engine.ledger.strategies():
        shares = sum(
            lot.shares
            for lot in engine.ledger.positions(strategy)
            if lot.condition_id == condition_id and lot.token_id == pending.winner
        )
        if shares <= 1e-9:
            continue
        engine.ledger.redeem(
            strategy,
            condition_id=condition_id,
            token_id=pending.winner,
            shares=shares,
            gas=pending.gas_usd,
        )
        redeemed += shares
    if redeemed > 0.0:
        log.info(
            f"paper lifecycle: {condition_id[:12]} redeemed {redeemed:.2f} winner "
            f"shares at ${pending.gas_usd:.6f} measured gas "
            f"(landed after the measured on-chain flight)"
        )


def _settle(engine, row: ResolutionRow, winner: str, now: float) -> None:
    """Apply a final verdict: cancel, par, redeem the winner, write off the loser."""
    state = state_for(engine)
    condition_id = row.condition_id
    state.blocked.add(condition_id)
    _cancel_quotes(engine, condition_id, now, final=True)
    held_tokens = {
        lot.token_id
        for strategy in engine.ledger.strategies()
        for lot in engine.ledger.positions(strategy)
        if lot.condition_id == condition_id
    }
    if held_tokens and not held_tokens <= set(row.token_ids):
        # A verdict whose paying token ids do not match the held ids would apply
        # par to the wrong lot (and could zero the whole position). Treat it as
        # an unresolved closed market instead.
        _log_closed_unresolved(
            state, condition_id,
            "the final payout token ids are not the held token ids",
        )
        return

    engine.ledger.set_par(condition_id, winner)
    written_off = 0.0
    for strategy in engine.ledger.strategies():
        for lot in list(engine.ledger.positions(strategy)):
            if lot.condition_id != condition_id or lot.token_id == winner:
                continue
            written_off += lot.shares
            engine.ledger.write_off(
                strategy,
                condition_id=condition_id,
                token_id=lot.token_id,
                shares=lot.shares,
                detail="resolution_loser",
            )
    if condition_id not in state.resolved:
        state.resolved.add(condition_id)
        log.info(
            f"paper lifecycle: {condition_id[:12]} resolved to {winner[:12]}; "
            f"par applied to every strategy, {written_off:.2f} loser shares "
            "written off at zero"
        )

    gas_usd = redeem_gas_usd(engine)
    flight = measure_redeem_flight()
    gaps = list(flight.gaps)
    if gas_usd is None:
        gaps.append(
            "redeem gas unmeasured (no measured CTF gas cost from paper.deskew)"
        )
    if gaps:
        if condition_id not in state.refusal_logged:
            state.refusal_logged.add(condition_id)
            log.warning(
                f"paper lifecycle: redemption refused for {condition_id[:12]}: "
                f"{'; '.join(gaps)}. Winner par applied and held; no on-chain "
                "redeem is journaled (a zero-gas or zero-latency redeem is never booked)."
            )
        return
    state.refusal_logged.discard(condition_id)
    if condition_id in state.redeems:
        # A redemption already in flight: re-scheduling it would keep pushing
        # the landing time out on every poll.
        return
    state.redeems[condition_id] = PendingRedeem(
        lands=now + flight.seconds, winner=winner, gas_usd=float(gas_usd)
    )
    log.info(
        f"paper lifecycle: {condition_id[:12]} redemption scheduled to land in "
        f"{flight.seconds:.3f}s (measured RPC round trip + block interval) at "
        f"${gas_usd:.6f} measured gas"
    )


def _log_closed_unresolved(state: LifecycleState, condition_id: str, reason: str) -> None:
    if condition_id in state.closed_logged:
        return
    state.closed_logged.add(condition_id)
    log.warning(
        f"paper lifecycle: {condition_id[:12]} is closed without a final verdict "
        f"({reason}); its quotes are cancelled and new quotes are blocked. "
        "No position is written off."
    )


def _drain(engine, state: LifecycleState, now: float) -> None:
    for condition_id, pending in list(state.redeems.items()):
        if now < pending.lands:
            continue
        state.redeems.pop(condition_id, None)
        _redeem_now(engine, condition_id, pending, now)


# ------------------------------------------------------------------ refresh
def refresh(engine, now: float | None = None, *, force: bool = False) -> None:
    """Refresh resolution state for every held condition.

    Called from ``PaperEngine._tick`` after the universe/``by_condition`` refresh
    and before marks and risk. The poll itself is rate-limited to
    ``RESOLUTION_POLL_INTERVAL``; the block filter and landing redemptions run
    every tick so a late-landing quote or a landed redemption is never missed.

    A missing or empty Gamma answer is a no-op with a logged gap: held lots stay
    open, no verdict is invented and nothing raises.
    """
    state = state_for(engine)
    stamp = time.time() if now is None else float(now)
    _drain(engine, state, stamp)
    _apply_blocks(engine)
    if not force and stamp - state.last_poll < RESOLUTION_POLL_INTERVAL:
        return
    state.last_poll = stamp
    conditions = engine.ledger.conditions()
    if not conditions:
        state.blocked.clear()
        return
    try:
        rows = engine.gamma.markets(sorted(conditions))
    except Exception as exc:  # noqa: BLE001 - a read failure is not a verdict
        log.warning(
            f"paper lifecycle: resolution poll failed for {len(conditions)} held "
            f"condition(s) ({exc}); they stay open until the next poll"
        )
        return
    if not rows:
        log.debug(
            f"paper lifecycle: Gamma returned no rows for {len(conditions)} held "
            "condition(s); no verdict fabricated"
        )
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = parse_resolution(raw)
        if row is None or row.condition_id not in conditions:
            continue
        winner, reason = verdict(row)
        if winner is None:
            if row.closed:
                state.blocked.add(row.condition_id)
                _cancel_quotes(engine, row.condition_id, stamp, final=True)
                _log_closed_unresolved(state, row.condition_id, reason)
            continue
        _settle(engine, row, winner, stamp)
    # A condition with no lots left and no pending redemption needs no block.
    held = engine.ledger.conditions()
    state.blocked = {
        cid for cid in state.blocked
        if cid in held or cid in state.redeems
    }
    # A condition blocked by this poll must leave the tradable views before the
    # same tick's mark / risk / planner passes read them.
    _apply_blocks(engine)
