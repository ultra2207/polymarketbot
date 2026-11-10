"""Runtime shared by the settlement allocator submodules."""

from __future__ import annotations

from polymarketbot.logs import get_logger

# One logger for the whole package: the label and the per-file log name are the
# ones the single-module version had, so the audit trail keeps the same
# ``polymarketbot.strategy.settlement.allocator.log`` sink even though the
# records now originate in several source files.
log = get_logger(
    "domain2_settlement_allocator",
    file="polymarketbot.strategy.settlement.allocator",
)

SECONDS_PER_YEAR: float = 31_536_000.0  # 365 days * 86,400 seconds
