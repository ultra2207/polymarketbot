"""Shared contract for the unified microstructure risk engine.

The live quote path previously asked two unrelated objects whether a resting
quote was safe: :class:`polymarketbot.strategy.reward.microstructure.MicrostructureEngine`
(OFI / drift) and a separate consecutive-fill counter. Neither knew where our
order sat in the queue, so both answered "is the book moving against us"
instead of "will *our* order be filled by it". This module fixes the vocabulary
for the replacement.

One engine answers one question, per resting quote::

    given where we sit in the FIFO queue at our price, the flow since we
    arrived, and the fills we have already taken, should this order stay?

Design rules that every implementation must honour:

1. **Queue position is L3, reconstructed from L2.** ``ahead`` is the number of
   shares resting in front of our order at its price. FIFO means it is
   monotonically non-increasing: size that appears at our level after we land
   arrived *behind* us. A model that lets ``ahead`` grow is wrong.
2. **No unmeasured constants.** Thresholds are computed from the current book
   and the engine's own realised volatility. When an input needed for a
   decision is absent, the engine returns ``RiskDecision(measured=False)`` and
   must not substitute a provisional number. AGENTS.md forbids the alternative.
3. **The decision is economic, not a hardcoded cutoff.** Pulling costs queue
   priority and re-entry risk; holding costs expected adverse selection. The
   engine compares them. A calibrated probability that is unavailable makes the
   decision unmeasured, not defaulted.
4. **Every decision is recordable.** ``RiskDecision`` carries the raw signals
   that produced it so the live tape can fit thresholds from real flow instead
   of asserting them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class RiskAction(str, Enum):
    """What the quote path should do with one resting order."""

    MAINTAIN = "maintain"
    """Leave the order resting; queue priority is the income source."""

    WIDEN = "widen"
    """Reprice away from the touch; keep the order live."""

    PULL = "pull"
    """Cancel now; the order may be replaced on the next cycle."""

    SUPPRESS = "suppress"
    """Cancel now and do not requote this side until the signal decays."""

    BLOCK = "block"
    """Hard refusal from a capital, position or breaker gate (not microstructure)."""


@dataclass(frozen=True)
class QueueEntry:
    """The queue snapshot captured when our order was submitted."""

    order_id: str
    token_id: str
    side: str
    """``BUY`` or ``SELL``."""

    price: float
    size: float
    entered_ts: float
    ahead_at_entry: float
    """Shares ahead of us at ``price`` when we arrived, inferred from the L2 level."""


@dataclass(frozen=True)
class QueueProgress:
    """Current L3 position of one resting order, rebuilt from L2 updates.

    This position is **inferred**, not measured: the public feed aggregates
    size by price and destroys arrival order, so no API exposes the true FIFO
    position (``docs/limitations.md``). ``queue_position_inferred`` is always
    true and is recorded so no downstream consumer can mistake the model for a
    venue-reported measurement.
    """

    order_id: str
    token_id: str
    ahead: float
    """Shares still resting in front of us. Monotonically non-increasing."""

    initial_ahead: float
    level_size: float
    """Total size observed at ``price``, including our own order."""

    traded_at_level: float
    """Cumulative printed volume at ``price`` since entry (attribution only)."""

    filled: bool
    """A print went through our price, or printed volume exceeded ``initial_ahead``."""

    updated_ts: float
    queue_position_inferred: bool = field(default=True, init=False)
    """Always true: ``ahead`` is reconstructed from L2, never venue-reported."""

    @property
    def consumed_fraction(self) -> float:
        """Fraction of the queue in front of us that is gone, in ``[0, 1]``."""
        if self.initial_ahead <= 0.0:
            return 1.0
        return min(1.0, max(0.0, 1.0 - self.ahead / self.initial_ahead))

    @property
    def at_front(self) -> bool:
        """True when no resting size separates us from the next taker."""
        return self.ahead <= 1e-9


@dataclass(frozen=True)
class FlowAssessment:
    """Flow state and the dynamic thresholds it was judged against."""

    ofi_10_events: float
    ofi_threshold: float
    drift_cents: float
    drift_threshold_cents: float
    toxicity_reason: str
    """``""``, ``OFI``, ``DRIFT`` or ``BOTH``."""

    is_toxic: bool
    directional_signal: float
    """Positive = buying pressure (asks at risk), negative = selling (bids at risk)."""


@dataclass(frozen=True)
class RiskDecision:
    """One auditable answer for one resting quote."""

    action: RiskAction
    side: str
    reason: str
    measured: bool
    """False when a required input was missing. Never defaulted to MAINTAIN silently."""

    flow: FlowAssessment | None = None
    queue: QueueProgress | None = None
    adverse_streak: int = 0
    """Consecutive same-side fills, the surviving part of the old tracker."""

    queue_cost_usd: float = 0.0
    """Expected cost of holding, from queue position x measured adverse drift."""

    pull_cost_usd: float = 0.0
    """Expected cost of pulling: forgone spread capture plus re-entry risk."""

    widen_cents: float = 0.0
    size_multiplier: float = 1.0
    ts: float = 0.0

    def as_record(self) -> Mapping[str, object]:
        """Flat mapping for ``LiveSessionRecorder.record_event``."""
        flow = self.flow
        queue = self.queue
        return {
            "action": self.action.value,
            "side": self.side,
            "reason": self.reason,
            "measured": self.measured,
            "adverse_streak": self.adverse_streak,
            "queue_cost_usd": round(self.queue_cost_usd, 8),
            "pull_cost_usd": round(self.pull_cost_usd, 8),
            "widen_cents": round(self.widen_cents, 4),
            "size_multiplier": round(self.size_multiplier, 4),
            "ofi_10_events": None if flow is None else round(flow.ofi_10_events, 6),
            "ofi_threshold": None if flow is None else round(flow.ofi_threshold, 6),
            "drift_cents": None if flow is None else round(flow.drift_cents, 6),
            "drift_threshold_cents": (
                None if flow is None else round(flow.drift_threshold_cents, 6)
            ),
            "toxicity_reason": None if flow is None else flow.toxicity_reason,
            "queue_ahead": None if queue is None else round(queue.ahead, 6),
            "queue_ahead_at_entry": None if queue is None else round(queue.initial_ahead, 6),
            "queue_consumed_fraction": (
                None if queue is None else round(queue.consumed_fraction, 6)
            ),
            "queue_at_front": None if queue is None else queue.at_front,
            "queue_position_inferred": (
                None if queue is None else queue.queue_position_inferred
            ),
            "ts": self.ts,
        }


@runtime_checkable
class MicroRiskEngine(Protocol):
    """The single authority for quote-level microstructure risk."""

    def on_book(
        self,
        token_id: str,
        bids: Sequence[tuple[float, float]],
        asks: Sequence[tuple[float, float]],
        ts: float,
    ) -> None:
        """Ingest an L2 snapshot; update flow state and every queue at this token."""

    def on_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        taker_side: str,
        ts: float,
    ) -> None:
        """Ingest a public print; attribute queue consumption and through-trades."""

    def on_entry(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        ts: float,
    ) -> QueueEntry:
        """Register a submitted order and snapshot the queue in front of it."""

    def on_cancel(self, token_id: str, order_id: str) -> None:
        """Stop tracking an order that is no longer live."""

    def on_fill(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Record a fill; feeds the surviving consecutive-fill counter."""

    def evaluate(
        self,
        token_id: str,
        side: str,
        price: float,
        mid: float,
        ts: float,
    ) -> RiskDecision:
        """Decide what to do with one resting order."""

    def snapshot(self) -> Mapping[str, object]:
        """Engine state for the recorder: flow, queues, streaks, thresholds."""
