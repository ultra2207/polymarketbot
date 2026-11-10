"""The settlement harvester's risk appetite: one named place, no default.

The tail-budget allocator needs three things before it will size a book:

* ``risk_appetite_usd`` - the most the book may lose in the worst
  ``1 - tail_confidence`` band.  A **preference**, so it is an explicit input
  and the code refuses without it (``configs/risk.yaml``).
* ``tail_confidence`` - the band the loss is stated at.  Also stated, not
  derived.
* the measured verdict-reversal tail - evidence, read from
  :mod:`polymarketbot.strategy.settlement.tail_evidence`.

This module is the single place the first two are read.  The engine loads the
policy once, logs it with its source, and refuses to allocate when it is absent:
a missing risk appetite is never replaced by a number, and a paper book that
cannot state its appetite must not spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from polymarketbot.logs import get_logger

log = get_logger(__name__)

RISK_CONFIG_PATH = "configs/risk.yaml"
CONFIG_SECTION = "settlement_harvester"


class UnmeasuredRiskAppetite(RuntimeError):
    """No stated risk appetite: the allocator must not size a book."""


@dataclass(frozen=True)
class SettlementTailPolicy:
    """The stated risk appetite and the band it is stated at."""

    risk_appetite_usd: float
    tail_confidence: float
    source: str

    def describe(self) -> str:
        return (
            f"risk appetite ${self.risk_appetite_usd:,.2f} at "
            f"{self.tail_confidence:.0%} confidence (from {self.source})"
        )


def load_settlement_tail_policy(path: str = RISK_CONFIG_PATH) -> SettlementTailPolicy:
    """Read the stated appetite; raise when it is absent or unusable."""
    p = Path(path)
    if not p.exists():
        raise UnmeasuredRiskAppetite(
            f"no settlement risk appetite: {path} is missing (the tail budget has "
            "no default)"
        )
    try:
        import yaml

        payload = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        raise UnmeasuredRiskAppetite(f"{path} is unreadable: {exc}") from exc
    section = payload.get(CONFIG_SECTION)
    if not isinstance(section, dict):
        raise UnmeasuredRiskAppetite(
            f"{path} has no `{CONFIG_SECTION}` section: nothing states the appetite"
        )
    raw_appetite = section.get("risk_appetite_usd")
    raw_confidence = section.get("tail_confidence")
    if raw_appetite is None or raw_confidence is None:
        raise UnmeasuredRiskAppetite(
            f"{path} must state both risk_appetite_usd and tail_confidence"
        )
    appetite = float(raw_appetite)
    confidence = float(raw_confidence)
    if appetite <= 0.0:
        raise UnmeasuredRiskAppetite(
            f"{path}: risk_appetite_usd must be positive, got {appetite!r}"
        )
    if not 0.0 < confidence < 1.0:
        raise UnmeasuredRiskAppetite(
            f"{path}: tail_confidence must be in (0, 1), got {confidence!r}"
        )
    return SettlementTailPolicy(
        risk_appetite_usd=appetite,
        tail_confidence=confidence,
        source=path,
    )
