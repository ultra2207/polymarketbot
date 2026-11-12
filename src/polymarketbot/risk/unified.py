"""One microstructure risk engine for every resting quote.

Replaces the old two-object split (``MicrostructureEngine`` + a consecutive-fill
counter) with a single object that answers, per quote: given our L3 queue
position, the flow since we arrived, and the fills we have taken, should this
order stay?

Decision precedence, highest first:

1. **Unmeasured** - flow inputs absent -> ``MAINTAIN`` with ``measured=False``.
   No provisional threshold is ever substituted (AGENTS.md).
2. **Adverse-fill streak** - the surviving reactive rule.
   ``consecutive_fills_threshold`` is required from the caller's risk
   configuration at construction; it has no default in this module.
3. **Toxic flow on the at-risk side**, ordered by evidence:
   a. measured economics: expected cost of being filled exceeds the spread
      capture forgone by pulling -> ``PULL``;
   b. no resting size ahead of us (we are next in line) -> ``PULL``;
   c. the flow gate itself reports extreme -> ``PULL``;
   d. otherwise ``WIDEN`` by the measured drift.
4. **MAINTAIN** - queue priority is the income source; leave the order alone.

Every number used here is measured at runtime (book depth, realised volatility,
``ctf_maker_markout.json``, ``ctf_quote_fill_horizon.json``) or comes from the
existing :class:`RiskConfig` knobs. Nothing is a fresh magic constant.

Note on 3a: the measured fill probability cancels out of the comparison because
both sides of it are per-fill quantities, so the rule reduces to
``adverse_drift_per_share > spread_capture_per_share`` - which is exactly the
economic question, with no free parameter.
"""

from __future__ import annotations

import itertools
import math
import statistics
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.risk.contracts import (
    FlowAssessment,
    QueueEntry,
    QueueProgress,
    RiskAction,
    RiskDecision,
)
from polymarketbot.risk.models import RiskConfig
from polymarketbot.risk.queue_position import QueuePositionTracker
from polymarketbot.strategy.reward.measurement import (
    MakerMarkout,
    QuoteFillHorizon,
    load_maker_markout,
    load_quote_fill_horizon,
)
from polymarketbot.strategy.reward.microstructure import (
    MicrostructureEngine,
    MicrostructureState,
)

log = get_logger(__name__)

_SECONDS_PER_DAY = 86_400.0
_VOL_WINDOW_SECONDS = 300.0
_VOL_MIN_SAMPLES = 8


@dataclass
class _TokenState:
    """Per-token flow engine, realised-volatility history and fill streaks."""

    flow: MicrostructureEngine
    mids: deque[tuple[float, float]] = field(default_factory=deque)
    streaks: dict[str, int] = field(default_factory=dict)
    last_fill_side: str = ""

    def observe_mid(self, mid: float, ts: float) -> None:
        """Append a distinct mid observation inside the volatility window."""
        if not (0.0 < mid < 1.0) or not math.isfinite(mid):
            return
        if self.mids and abs(self.mids[-1][1] - mid) < 1e-12:
            self.mids[-1] = (ts, mid)
            return
        self.mids.append((ts, mid))
        cutoff = ts - _VOL_WINDOW_SECONDS
        while self.mids and self.mids[0][0] < cutoff:
            self.mids.popleft()

    def realized_volatility(self) -> float | None:
        """Daily realised volatility implied by the observed mid path, or None.

        ``None`` means the window has too few distinct observations; callers
        must treat that as unmeasured rather than assuming a volatility.
        """
        if len(self.mids) < _VOL_MIN_SAMPLES:
            return None
        observations = list(self.mids)
        returns = [
            math.log(b / a)
            for (_, a), (_, b) in itertools.pairwise(observations)
            if a > 0.0 and b > 0.0
        ]
        if len(returns) < _VOL_MIN_SAMPLES - 1:
            return None
        span = max(1e-6, observations[-1][0] - observations[0][0])
        per_sample = span / max(1, len(returns))
        scale = math.sqrt(_SECONDS_PER_DAY / per_sample) if per_sample > 0.0 else 0.0
        return statistics.fmean(abs(r) for r in returns) * scale


class UnifiedRiskEngine:
    """Single authority for quote-level microstructure risk."""

    def __init__(
        self,
        *,
        tick_size: float,
        flow_factory: Callable[[], MicrostructureEngine],
        horizon_seconds: float,
        consecutive_fills_threshold: int,
        config: RiskConfig | None = None,
        fill_horizon: QuoteFillHorizon | None = None,
        markout: MakerMarkout | None = None,
    ) -> None:
        """Create the engine.

        ``flow_factory`` must build a fully fitted
        :class:`MicrostructureEngine`; this class never constructs one with
        unmeasured coefficients.

        ``horizon_seconds`` is the quote lifetime the decision prices: the
        reprice cadence of the caller, not a fixed 60 s. P(fill) and markout
        are read at exactly this horizon, so a decision cannot price a 2 s
        quote with a 225 s fill probability.

        ``consecutive_fills_threshold`` has no default. Provenance: the
        caller's risk configuration (live runner / replay calibration). A
        fabricated streak length would fire on noise, so it must be supplied
        and is validated here instead of being replaced by a constant.
        """
        if horizon_seconds <= 0.0:
            raise ValueError("horizon_seconds must be positive")
        if int(consecutive_fills_threshold) < 1:
            raise ValueError("consecutive_fills_threshold must be >= 1")
        self.tick_size = float(tick_size)
        self.horizon_seconds = float(horizon_seconds)
        self.consecutive_fills_threshold = int(consecutive_fills_threshold)
        self.config = config or RiskConfig()
        self._fills = fill_horizon if fill_horizon is not None else load_quote_fill_horizon()
        self._markout = markout if markout is not None else load_maker_markout()
        self._flow_factory = flow_factory
        self._queue = QueuePositionTracker()
        self._entries: dict[str, QueueEntry] = {}
        self._tokens: dict[str, _TokenState] = {}
        self._lock = threading.RLock()
        self.last_decision: RiskDecision | None = None
        # Inputs change only through the ingestion methods below; every one of
        # them bumps the version, so an identical evaluate() call can reuse its
        # previous answer instead of re-running the same decision twice per
        # order (quoting leg + RiskManager.validate_quote).
        self._version = 0
        self._eval_cache: tuple[tuple[Any, ...], RiskDecision] | None = None

    def _bump_version(self) -> None:
        """Invalidate the memoised decision after any state mutation."""
        self._version += 1

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def _state(self, token_id: str) -> _TokenState:
        state = self._tokens.get(token_id)
        if state is None:
            state = _TokenState(flow=self._flow_factory())
            self._tokens[token_id] = state
        return state

    def on_book(
        self,
        token_id: str,
        bids: Sequence[tuple[float, float]],
        asks: Sequence[tuple[float, float]],
        ts: float,
    ) -> None:
        with self._lock:
            state = self._state(token_id)
            best_bid = max((float(p) for p, _ in bids), default=0.0)
            best_ask = min((float(p) for p, _ in asks), default=0.0)
            bid_size = sum(float(s) for _, s in bids)
            ask_size = sum(float(s) for _, s in asks)
            if not (0.0 < best_bid < 1.0) or not (0.0 < best_ask < 1.0):
                return
            state.observe_mid((best_bid + best_ask) / 2.0, float(ts))
            state.flow.update(
                best_bid_price=best_bid,
                best_bid_size=bid_size,
                best_ask_price=best_ask,
                best_ask_size=ask_size,
                timestamp=float(ts),
                realized_volatility=state.realized_volatility(),
            )
            self._queue.on_book(token_id, list(bids), list(asks), float(ts))
            self._bump_version()

    def on_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        taker_side: str,
        ts: float,
    ) -> None:
        with self._lock:
            self._state(token_id)
            self._queue.on_trade(
                token_id, float(price), float(size), str(taker_side).upper(), float(ts)
            )
            self._bump_version()

    def on_entry(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        ts: float,
        bids: Sequence[tuple[float, float]] = (),
        asks: Sequence[tuple[float, float]] = (),
    ) -> QueueEntry:
        with self._lock:
            self._state(token_id)
            entry = self._queue.on_entry(
                order_id,
                token_id,
                str(side).upper(),
                float(price),
                float(size),
                float(ts),
                list(bids),
                list(asks),
            )
            self._entries[order_id] = entry
            self._bump_version()
            return entry

    def on_cancel(self, token_id: str, order_id: str) -> None:
        with self._lock:
            self._entries.pop(order_id, None)
            self._queue.drop(token_id, order_id)
            self._bump_version()

    def on_fill(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Record a fill, advance the streak, and shrink or retire our queue entry."""
        with self._lock:
            state = self._state(token_id)
            norm = str(side).upper()
            if state.last_fill_side == norm:
                state.streaks[norm] = state.streaks.get(norm, 0) + 1
            else:
                state.streaks[norm] = 1
                if state.last_fill_side:
                    state.streaks[state.last_fill_side] = 0
            state.last_fill_side = norm

            for order_id, entry in list(self._entries.items()):
                if entry.token_id != token_id or entry.side != norm:
                    continue
                if abs(entry.price - float(price)) > 1e-9:
                    continue
                remaining = entry.size - float(size)
                if remaining <= 1e-9:
                    self._entries.pop(order_id, None)
                    self._queue.drop(token_id, order_id)
                else:
                    self._entries[order_id] = QueueEntry(
                        order_id=entry.order_id,
                        token_id=entry.token_id,
                        side=entry.side,
                        price=entry.price,
                        size=remaining,
                        entered_ts=entry.entered_ts,
                        ahead_at_entry=entry.ahead_at_entry,
                    )
            self._bump_version()

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    def _measured_costs(
        self,
        *,
        price: float,
        mid: float,
        size: float,
        queue: QueueProgress | None,
    ) -> tuple[float, float] | None:
        """``(queue_cost_usd, pull_cost_usd)`` at the caller's quote horizon, or None."""
        if size <= 0.0 or not (0.0 < price < 1.0):
            return None
        # Price the decision at the lifetime the quote actually lives between
        # reprices. ``None`` from either table means that horizon was not
        # measured: the economics are unmeasured, never substituted.
        p_fill = self._fills.p_fill(self.horizon_seconds, self.tick_size, size)
        drift = self._markout.markout(self.horizon_seconds, self.tick_size)
        if p_fill is None or drift is None:
            return None
        consumption = 1.0 if queue is None else queue.consumed_fraction
        # ``MakerMarkout`` stores the per-share loss a fill inflicts, so a
        # positive value is the adverse drift we pay, not a gain.
        adverse_per_share = max(0.0, float(drift))
        capture_per_share = abs(float(mid) - float(price))
        # Hold cost scales with the probability *we* are filled; the capture we
        # forgo by pulling scales with the baseline fill probability of a fresh
        # quote. Dividing through by p_fill would cancel the queue entirely and
        # make every toxic tick a pull, even behind thousands of shares.
        return (
            p_fill * consumption * adverse_per_share * size,
            p_fill * capture_per_share * size,
        )

    def _entry_for(
        self, token_id: str, side: str, price: float
    ) -> QueueEntry | None:
        """Newest order at this exact price, else the nearest price.

        An exact match must win: after a cancel-all the dead entry and the
        re-placed order share a price, and the live one is the newer record.
        """
        exact: QueueEntry | None = None
        nearest: QueueEntry | None = None
        nearest_distance = math.inf
        for entry in self._entries.values():
            if entry.token_id != token_id or entry.side != side:
                continue
            distance = abs(entry.price - price)
            if distance <= 1e-9:
                if exact is None or entry.entered_ts > exact.entered_ts:
                    exact = entry
                continue
            if distance < nearest_distance or (
                distance == nearest_distance
                and nearest is not None
                and entry.entered_ts > nearest.entered_ts
            ):
                nearest, nearest_distance = entry, distance
        return exact if exact is not None else nearest

    def _queue_for(self, entry: QueueEntry | None) -> QueueProgress | None:
        if entry is None:
            return None
        return self._queue.progress(entry.order_id)

    def evaluate(
        self,
        token_id: str,
        side: str,
        price: float,
        mid: float,
        ts: float,
    ) -> RiskDecision:
        """Decide one resting quote, memoised for an unchanged engine state.

        The live path asks twice for the same order in one tick (the quoting
        leg, then ``RiskManager.validate_quote``). Both calls pass identical
        arguments and no ingestion happens in between, so the second call must
        return the first decision rather than re-running it. Any book, trade,
        fill, entry, cancel or reset bumps ``_version`` and forces a fresh
        evaluation.
        """
        norm = str(side).upper()
        key = (
            self._version,
            float(self.tick_size),
            str(token_id),
            norm,
            float(price),
            float(mid),
            float(ts),
        )
        with self._lock:
            cached = self._eval_cache
            if cached is not None and cached[0] == key:
                return cached[1]
            decision = self._evaluate_locked(
                token_id, norm, float(price), float(mid), float(ts)
            )
            self._eval_cache = (key, decision)
            return decision

    def _evaluate_locked(
        self,
        token_id: str,
        side: str,
        price: float,
        mid: float,
        ts: float,
    ) -> RiskDecision:
        with self._lock:
            norm = str(side).upper()
            state = self._tokens.get(token_id)
            if state is None:
                return RiskDecision(
                    action=RiskAction.MAINTAIN,
                    side=norm,
                    reason="token not tracked by the risk engine",
                    measured=False,
                    ts=float(ts),
                )

            flow_state: MicrostructureState | None = state.flow.latest_state
            if flow_state is None or not getattr(flow_state, "measured", True):
                return RiskDecision(
                    action=RiskAction.MAINTAIN,
                    side=norm,
                    reason="flow inputs unmeasured; refusing to act on a guessed threshold",
                    measured=False,
                    ts=float(ts),
                )

            entry = self._entry_for(token_id, norm, float(price))
            queue = self._queue_for(entry)
            flow = FlowAssessment(
                ofi_10_events=flow_state.ofi_10_events,
                ofi_threshold=flow_state.ofi_threshold,
                drift_cents=flow_state.drift_cents,
                drift_threshold_cents=flow_state.drift_threshold_cents,
                toxicity_reason=flow_state.toxicity_reason,
                is_toxic=flow_state.is_toxic,
                directional_signal=self._direction(flow_state),
            )
            streak = state.streaks.get(norm, 0)
            costs = self._measured_costs(
                price=float(price),
                mid=float(mid),
                size=entry.size if entry is not None else 0.0,
                queue=queue,
            )
            queue_cost, pull_cost = costs if costs is not None else (0.0, 0.0)
            # A flow-only decision is not a full measurement: without a tracked
            # queue entry the economic rule and the queue rule never ran.
            fully_measured = entry is not None and costs is not None
            if entry is None:
                missing = "; queue position untracked"
            elif costs is None:
                missing = "; economics unmeasured for this tick"
            else:
                missing = ""

            # 2. The reactive rule, now one input to a single decision.
            threshold = self.consecutive_fills_threshold
            if streak >= threshold:
                return self._remember(
                    self._streak_decision(norm, streak, flow, queue, queue_cost, pull_cost, ts)
                )

            # 3. Toxic flow, but only on the side it threatens.
            if not flow.is_toxic or not self._side_at_risk(flow, norm):
                reason = (
                    "flow is balanced"
                    if not flow.is_toxic
                    else f"toxic {flow.toxicity_reason} flow threatens the other side"
                )
                return self._remember(
                    RiskDecision(
                        action=RiskAction.MAINTAIN,
                        side=norm,
                        reason=reason + missing,
                        measured=fully_measured,
                        flow=flow,
                        queue=queue,
                        adverse_streak=streak,
                        queue_cost_usd=queue_cost,
                        pull_cost_usd=pull_cost,
                        ts=float(ts),
                    )
                )

            # 3a. Measured economics: holding costs more than the capture we forgo.
            if costs is not None and queue_cost > pull_cost:
                return self._remember(
                    RiskDecision(
                        action=RiskAction.PULL,
                        side=norm,
                        reason=(
                            f"expected hold cost ${queue_cost:.6f} exceeds "
                            f"forgone capture ${pull_cost:.6f} under {flow.toxicity_reason} flow"
                        ),
                        measured=True,
                        flow=flow,
                        queue=queue,
                        adverse_streak=streak,
                        queue_cost_usd=queue_cost,
                        pull_cost_usd=pull_cost,
                        ts=float(ts),
                    )
                )

            # 3b. No size in front of us: the next taker is ours.
            if queue is not None and queue.at_front:
                return self._remember(
                    RiskDecision(
                        action=RiskAction.PULL,
                        side=norm,
                        reason=f"next in queue under toxic {flow.toxicity_reason} flow",
                        measured=True,
                        flow=flow,
                        queue=queue,
                        adverse_streak=streak,
                        queue_cost_usd=queue_cost,
                        pull_cost_usd=pull_cost,
                        ts=float(ts),
                    )
                )

            # 3c/3d. Defer the width decision to the flow gate's own extreme test.
            action = state.flow.evaluate_quote_action(norm, flow_state)
            if action.should_pull:
                decision = RiskDecision(
                    action=RiskAction.PULL,
                    side=norm,
                    reason=action.reason + missing,
                    measured=fully_measured,
                    flow=flow,
                    queue=queue,
                    adverse_streak=streak,
                    queue_cost_usd=queue_cost,
                    pull_cost_usd=pull_cost,
                    ts=float(ts),
                )
            elif action.should_widen:
                decision = RiskDecision(
                    action=RiskAction.WIDEN,
                    side=norm,
                    reason=action.reason + missing,
                    measured=fully_measured,
                    flow=flow,
                    queue=queue,
                    adverse_streak=streak,
                    queue_cost_usd=queue_cost,
                    pull_cost_usd=pull_cost,
                    widen_cents=float(action.spread_offset_cents),
                    ts=float(ts),
                )
            else:
                decision = RiskDecision(
                    action=RiskAction.MAINTAIN,
                    side=norm,
                    reason=action.reason + missing,
                    measured=fully_measured,
                    flow=flow,
                    queue=queue,
                    adverse_streak=streak,
                    queue_cost_usd=queue_cost,
                    pull_cost_usd=pull_cost,
                    ts=float(ts),
                )
            return self._remember(decision)

    def _streak_decision(
        self,
        norm: str,
        streak: int,
        flow: FlowAssessment,
        queue: QueueProgress | None,
        queue_cost: float,
        pull_cost: float,
        ts: float,
    ) -> RiskDecision:
        if str(self.config.toxicity_action).lower() == "suppress":
            return RiskDecision(
                action=RiskAction.SUPPRESS,
                side=norm,
                reason=f"{streak} consecutive {norm} fills",
                measured=True,
                flow=flow,
                queue=queue,
                adverse_streak=streak,
                queue_cost_usd=queue_cost,
                pull_cost_usd=pull_cost,
                ts=float(ts),
            )
        widen = max(float(self.config.toxicity_spread_penalty_cents), flow.drift_cents)
        return RiskDecision(
            action=RiskAction.WIDEN,
            side=norm,
            reason=f"{streak} consecutive {norm} fills",
            measured=True,
            flow=flow,
            queue=queue,
            adverse_streak=streak,
            queue_cost_usd=queue_cost,
            pull_cost_usd=pull_cost,
            widen_cents=widen,
            size_multiplier=max(0.0, 1.0 - float(self.config.toxicity_size_reduction_pct)),
            ts=float(ts),
        )

    def _remember(self, decision: RiskDecision) -> RiskDecision:
        self.last_decision = decision
        return decision

    @staticmethod
    def _direction(flow_state: MicrostructureState) -> float:
        """Directional signal from the signal that actually fired.

        A DRIFT-triggered move can carry a small OFI of the opposite sign;
        routing by OFI alone would protect the wrong side of the book.
        """
        drift = flow_state.micro_price - flow_state.mid_price
        if flow_state.toxicity_reason == "DRIFT":
            return drift
        if flow_state.ofi_10_events != 0.0:
            return flow_state.ofi_10_events
        return drift

    @staticmethod
    def _side_at_risk(flow: FlowAssessment, side: str) -> bool:
        """True when toxic flow is about to hit this side of the book.

        Buying pressure lifts the price into resting ASKs; selling pressure
        drops it into resting BIDs.
        """
        sell = side in ("SELL", "ASK")
        buy = side in ("BUY", "BID")
        if not (sell or buy):
            return False
        if flow.directional_signal > 0.0:
            return sell
        if flow.directional_signal < 0.0:
            return buy
        return False

    def flow(self, token_id: str) -> MicrostructureState | None:
        """Latest flow snapshot for a token, or None before the first book."""
        with self._lock:
            state = self._tokens.get(token_id)
            return None if state is None else state.flow.latest_state

    def reset(self) -> None:
        """Drop all flow, queue and streak state (market switch / restart)."""
        with self._lock:
            self._queue.clear()
            self._tokens.clear()
            self._entries.clear()
            self.last_decision = None
            self._eval_cache = None
            self._bump_version()

    # ------------------------------------------------------------------
    # Introspection for the recorder
    # ------------------------------------------------------------------
    def snapshot(self) -> Mapping[str, Any]:
        with self._lock:
            tokens = {}
            for token_id, state in self._tokens.items():
                flow = state.flow.latest_state
                tokens[token_id] = {
                    "measured": None if flow is None else bool(getattr(flow, "measured", True)),
                    "is_toxic": None if flow is None else bool(flow.is_toxic),
                    "toxicity_reason": None if flow is None else flow.toxicity_reason,
                    "ofi_10_events": None if flow is None else round(flow.ofi_10_events, 6),
                    "ofi_threshold": None if flow is None else round(flow.ofi_threshold, 6),
                    "drift_cents": None if flow is None else round(flow.drift_cents, 6),
                    "drift_threshold_cents": (
                        None if flow is None else round(flow.drift_threshold_cents, 6)
                    ),
                    "realized_volatility": state.realized_volatility(),
                    "streaks": dict(state.streaks),
                }
            return {
                "tokens": tokens,
                "queues": self._queue.snapshot(),
                "orders": len(self._entries),
                "last_decision": (
                    None if self.last_decision is None else dict(self.last_decision.as_record())
                ),
            }
