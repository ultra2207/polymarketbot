"""Queue-aware quote orchestration for the Polymarket paper maker.

The problem this solves
-----------------------
The price-only OFI side gate improved fill markout on 29 political asset-hours
(780 marked fills, 2026-11-04 01:00/05:00 UTC): reactionary -$309.69, side gate
+$17.52. But not all of that gate is useful. Joining the fills to the L3 queue
at the quote price (``scripts/analyze_queue_positions.py``, 777 marked fills)
splits them by the queue ratio ``q_ahead / size_at_level``:

    ratio < 0.60 (front)  199 fills, net/share +0.00364, net +$46.17
    ratio >= 0.60 (back)  281 fills, net/share -0.02014, net -$372.72

The gate was removing profitable front-of-queue fills and keeping its best
value for the back of deep queues. A queue-aware gate that keeps both sides at
the front and only pulls/shrinks the threatened side in the back earns
**+$66.88**, versus **+$20.71** for the unconditional side gate and **-$305.85**
for the reactionary quote.

The sign of the split is the one queue-position value predicts (Moallemi &
Yuan 2016): the deeper the queue ahead of a resting order, the worse the fill it
eventually receives. Polymarket's reward scoring makes a binary pull expensive:
a single-sided quote is divided by 3 inside 0.10-0.90 and scores zero outside
it. The orchestrator therefore defaults to *shrinking* the threatened side at
the back rather than pulling it, and keeps both sides at the front where the
fills are not adverse.

Measured fill model
-------------------
``scripts/calibrate_fill_model.py`` over the same 29 L3 asset-hours: 6,817
claimed prints, 2,184 fills (32.0 % base rate), logistic
``logit P(fill) = 3.2769 - 1.1421*ratio - 0.5733*log1p(size)``, |z| > 14. The
probability is print-conditional; the backtester's per-episode rate is much
lower (0.98 % in the one-hour quote backtest), so use it for ranking, not as an
absolute fill forecast.

Policy
------
``policy``:
  * ``reactionary``   quote both sides.
  * ``side_selection`` pull the threatened side unconditionally (the legacy gate).
  * ``queue_aware``   keep both sides when the threatened side's queue ratio is
                      below ``min_adverse_queue_ratio``; otherwise pull/shrink it.
  * ``reward_first``  quote both sides.
``signal``: ``ofi``, ``binance``, or ``kalshi``.
``gate_action``: ``shrink`` (default) or ``pull``; shrink uses ``shrink_scale``.
``kalshi_basis_threshold``: logit points of cross-venue basis (Kalshi YES minus
Polymarket YES). **The threshold is not yet measured.** The kalshi signal is
off by default (orchestrator disabled, ``signal="ofi"``), and once selected,
``0.0`` is a no-op floor. With a positive basis Kalshi sits above Polymarket,
so Polymarket must reprice up and the resting ask is threatened; a negative
basis threatens the bid.

Everything is off unless ``enabled=True``; a missing signal or queue keeps both
sides. The orchestrator is pure: the paper engine supplies the book, the OFI
mean, and the quote size, and applies the returned actions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# Measured on data/derived/l3_queue, 2026-11-04 01:00-05:59 UTC.
FILL_MODEL_COEF = (3.276861121438761, -1.1421287228083365, -0.5733129303901857)
FILL_MODEL_SOURCE = "data/derived/fill_model.json (6,817 observations, 2,184 fills)"

POLICIES = ("reactionary", "side_selection", "queue_aware", "reward_first")
SIGNALS = ("ofi", "binance", "kalshi")
GATE_ACTIONS = ("pull", "shrink")


def fill_probability(queue_ratio: float, size_at_level: float) -> float:
    """Measured print-conditional P(fill) for a resting quote at a level."""
    ratio = min(1.0, max(0.0, float(queue_ratio)))
    size = max(0.0, float(size_at_level))
    b0, b1, b2 = FILL_MODEL_COEF
    z = b0 + b1 * ratio + b2 * math.log1p(size)
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


@dataclass(frozen=True)
class QueueSnapshot:
    """FIFO position of our quote inside its own price level.

    ``queue_ratio`` follows ``build_l3_queue.py``: the share of the level still
    in front of our quote, ``q_ahead / (q_ahead + q_behind)``. A quote joining
    behind the displayed size starts at 1.0 and falls toward 0.0 as size arrives
    behind it. Sizes are shares; the level size feeds the measured fill model.
    """

    bid_queue_ratio: float
    bid_size_at_level: float
    ask_queue_ratio: float
    ask_size_at_level: float

    def ratio(self, side: str) -> float:
        if side == "bid":
            return _clamp(self.bid_queue_ratio)
        return _clamp(self.ask_queue_ratio)

    def fill_prob(self, side: str) -> float:
        if side == "bid":
            return fill_probability(self.bid_queue_ratio, self.bid_size_at_level)
        return fill_probability(self.ask_queue_ratio, self.ask_size_at_level)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


@dataclass(frozen=True)
class OrchestratorConfig:
    """Toggleable quote policy. Disabled by default: the paper is unchanged."""

    enabled: bool = False
    policy: str = "queue_aware"
    signal: str = "ofi"
    gate_action: str = "shrink"
    ofi_threshold: float = 0.0
    binance_threshold_bps: float = 1.0
    #: Logit points of cross-venue basis. NOT YET MEASURED: the orchestrator
    #: is disabled and ``signal`` defaults to ofi, so the kalshi signal is off
    #: until both are switched on; 0.0 is then a no-op floor.
    kalshi_basis_threshold: float = 0.0
    min_adverse_queue_ratio: float = 0.60
    shrink_scale: float = 0.25

    def validated(self) -> "OrchestratorConfig":
        if self.policy not in POLICIES:
            raise ValueError(f"unknown policy {self.policy!r}; choose {POLICIES}")
        if self.signal not in SIGNALS:
            raise ValueError(f"unknown signal {self.signal!r}; choose {SIGNALS}")
        if self.gate_action not in GATE_ACTIONS:
            raise ValueError(f"unknown gate_action {self.gate_action!r}; choose {GATE_ACTIONS}")
        if not 0.0 <= self.min_adverse_queue_ratio <= 1.0:
            raise ValueError("min_adverse_queue_ratio must be in [0, 1]")
        if not 0.0 <= self.shrink_scale <= 1.0:
            raise ValueError("shrink_scale must be in [0, 1]")
        if self.ofi_threshold < 0.0:
            raise ValueError("ofi_threshold must be >= 0")
        if self.kalshi_basis_threshold < 0.0:
            raise ValueError("kalshi_basis_threshold must be >= 0")
        return self


@dataclass(frozen=True)
class SideDecision:
    action: str  # "QUOTE" | "PULL" | "SHRINK"
    size_scale: float = 1.0
    reason: str = ""


@dataclass(frozen=True)
class OrchestratorDecision:
    bid: SideDecision
    ask: SideDecision
    bid_fill_prob: float
    ask_fill_prob: float
    bid_queue_ratio: float
    ask_queue_ratio: float
    policy: str
    signal: Optional[float]


class QuoteOrchestrator:
    """Decide, per side, whether to keep, shrink, or pull a resting quote."""

    def __init__(self, config: Optional[OrchestratorConfig] = None) -> None:
        self.config = (config or OrchestratorConfig()).validated()

    def decide(
        self,
        ofi: Optional[float],
        queue: Optional[QueueSnapshot],
        binance_bps: Optional[float] = None,
        kalshi_basis: Optional[float] = None,
    ) -> OrchestratorDecision:
        cfg = self.config
        bid_fp = queue.fill_prob("bid") if queue else 0.0
        ask_fp = queue.fill_prob("ask") if queue else 0.0
        bid_ratio = queue.ratio("bid") if queue else 0.0
        ask_ratio = queue.ratio("ask") if queue else 0.0

        def both(reason: str) -> OrchestratorDecision:
            return OrchestratorDecision(
                bid=SideDecision("QUOTE", 1.0, reason),
                ask=SideDecision("QUOTE", 1.0, reason),
                bid_fill_prob=bid_fp,
                ask_fill_prob=ask_fp,
                bid_queue_ratio=bid_ratio,
                ask_queue_ratio=ask_ratio,
                policy=cfg.policy,
                signal=None,
            )

        if not cfg.enabled or cfg.policy in ("reactionary", "reward_first"):
            return both("gate disabled or reward-first policy")
        if queue is None:
            return both("no queue snapshot; keep both sides")

        if cfg.signal == "ofi":
            signal: Optional[float] = ofi
            threshold = cfg.ofi_threshold
        elif cfg.signal == "binance":
            signal = binance_bps
            threshold = cfg.binance_threshold_bps
        else:
            signal = kalshi_basis
            threshold = cfg.kalshi_basis_threshold
        if signal is None:
            return both("insufficient signal; keep both sides")
        if abs(float(signal)) <= threshold:
            return both("signal at or below threshold; keep both sides")

        threatened = "ask" if float(signal) > 0.0 else "bid"
        threatened_ratio = ask_ratio if threatened == "ask" else bid_ratio

        if cfg.policy == "queue_aware" and threatened_ratio < cfg.min_adverse_queue_ratio:
            return both(
                f"front-of-queue {threatened} (ratio={threatened_ratio:.2f} < "
                f"{cfg.min_adverse_queue_ratio:.2f}); keep both for reward"
            )

        if cfg.gate_action == "pull":
            action, scale = "PULL", 0.0
        else:
            action, scale = "SHRINK", cfg.shrink_scale
        reason = (
            f"{cfg.policy}: {threatened} threatened (signal={float(signal):+.3f}, "
            f"ratio={threatened_ratio:.2f}); {action.lower()} {threatened}"
        )
        return OrchestratorDecision(
            bid=SideDecision(
                action if threatened == "bid" else "QUOTE",
                scale if threatened == "bid" else 1.0,
                reason if threatened == "bid" else f"bid kept; {reason}",
            ),
            ask=SideDecision(
                action if threatened == "ask" else "QUOTE",
                scale if threatened == "ask" else 1.0,
                reason if threatened == "ask" else f"ask kept; {reason}",
            ),
            bid_fill_prob=bid_fp,
            ask_fill_prob=ask_fp,
            bid_queue_ratio=bid_ratio,
            ask_queue_ratio=ask_ratio,
            policy=cfg.policy,
            signal=float(signal),
        )
