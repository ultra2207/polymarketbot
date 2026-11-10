"""Measured flight time of an on-chain CTF leg, and the complete-set count.

A merge is not a venue message: it is a signed transaction that pays the
measured Polygon RPC round trip and then waits for the next block.  Both inputs
are measurements, and when either is missing the leg is refused.  There is no
fallback interval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

from polymarketbot.logs import get_logger

logger = get_logger("paper.ctf")


class _LotLike(Protocol):
    """The ledger lot fields ``merge_pairs`` reads; no other structure assumed."""

    condition_id: str
    token_id: str
    shares: float

# ---------------------------------------------------------------------------
# On-chain leg clock: split / merge / redeem
#
# A merge is not a venue message.  It is a signed transaction that pays the
# measured Polygon RPC round trip and then waits for the next block, so its
# landing time is ``rpc_rtt + block_interval``, not the CLOB round trip the
# ``ActionBook`` uses for order messages.  Both inputs are measurements: the RPC
# round trip persisted by ``polymarketbot.tools.latency_probe`` and the block
# interval measured from consecutive heads.  When either is missing -- or when
# the head itself cannot be read (the RPC layer's rate limiter puts
# ``eth_blockNumber`` on a cooldown measured in hundreds of seconds) -- the leg
# is refused and logged.  There is no fallback interval: an on-chain action
# timed from an assumed block rate is a fabricated fill, and the ledger would
# credit par for a merge that has not landed.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OnChainTiming:
    """Measured flight time of an on-chain CTF leg."""

    rpc_rtt_s: float
    block_interval_s: float

    @property
    def flight_s(self) -> float:
        """Expected seconds from broadcast until the leg lands in a block."""
        return self.rpc_rtt_s + self.block_interval_s


class OnChainRefused(RuntimeError):
    """An on-chain leg was refused because its clock is not measured."""


def require_on_chain_timing(
    rpc_rtt_s: Optional[float],
    block_interval_s: Optional[float],
    *,
    action: str,
    head_unavailable_reason: Optional[str] = None,
) -> OnChainTiming:
    """The measured on-chain flight, or raise :class:`OnChainRefused`.

    ``action`` names the leg (``merge``/``split``/``redeem``) so the refusal is
    diagnosable from the log alone.  Every missing input is named in the
    refusal: an unreadable head, an unmeasured RPC round trip, an unmeasured
    block interval.  Nothing is defaulted; in particular there is no assumed
    block time to fall back to.
    """
    missing: list[str] = []
    if head_unavailable_reason:
        missing.append(f"live Polygon head ({head_unavailable_reason})")
    if rpc_rtt_s is None or not math.isfinite(float(rpc_rtt_s)) or float(rpc_rtt_s) <= 0.0:
        missing.append(
            "measured Polygon RPC round trip "
            "(run polymarketbot.tools.latency_probe: data/derived/local_latency.json "
            "carries no polygon_rpc endpoint)"
        )
    if (
        block_interval_s is None
        or not math.isfinite(float(block_interval_s))
        or float(block_interval_s) <= 0.0
    ):
        missing.append(
            "measured Polygon block interval (no consecutive-head sequence in "
            "data/tape/polygon_gas)"
        )
    if missing:
        reason = "; ".join(missing)
        logger.error(f"on-chain {action} refused: clock unmeasured -- {reason}")
        raise OnChainRefused(f"on-chain {action} refused: {reason}")
    timing = OnChainTiming(float(rpc_rtt_s), float(block_interval_s))
    logger.info(
        f"on-chain {action} admitted: flight {timing.flight_s:.2f}s "
        f"(rpc {timing.rpc_rtt_s:.2f}s + block {timing.block_interval_s:.2f}s)"
    )
    return timing


def _live_head_error() -> Optional[str]:
    """``None`` when the live head reads, else the refusal reason."""
    try:
        from polymarketbot.chain import ChainClient

        head = int(ChainClient().block_number())
    except Exception as exc:  # noqa: BLE001 - an unreadable head is the signal
        return str(exc) or "eth_blockNumber failed"
    if head <= 0:
        return f"eth_blockNumber returned {head}"
    return None


def _measured_rpc_rtt() -> Optional[float]:
    try:
        from polymarketbot.trading.ctf import load_persisted_rpc_rtt_s

        return load_persisted_rpc_rtt_s()
    except Exception as exc:  # noqa: BLE001 - a missing measurement is data, not a crash
        logger.warning(f"RPC round-trip measurement unavailable: {exc}")
        return None


def _measured_block_interval() -> Optional[float]:
    try:
        from polymarketbot.trading.ctf import load_measured_block_interval_s

        return load_measured_block_interval_s()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Block-interval measurement unavailable: {exc}")
        return None


def resolve_on_chain_timing(
    action: str,
    *,
    head_error: Optional[str] = None,
    probe_head: bool = True,
    rpc_rtt_loader: Optional[Callable[[], Optional[float]]] = None,
    block_interval_loader: Optional[Callable[[], Optional[float]]] = None,
) -> OnChainTiming:
    """Resolve every measured input for an on-chain leg, then admit or refuse.

    ``probe_head`` reads the live Polygon head; ``head_error`` lets a caller
    that already saw the failure pass its reason through instead of paying a
    second round trip.  The loaders are injectable so the gate is testable
    without network or tape.
    """
    reason = head_error
    if probe_head and not reason:
        reason = _live_head_error()
    rpc_rtt = (rpc_rtt_loader or _measured_rpc_rtt)()
    interval = (block_interval_loader or _measured_block_interval)()
    return require_on_chain_timing(
        rpc_rtt, interval, action=action, head_unavailable_reason=reason
    )


def merge_pairs(lots: Iterable[_LotLike], condition_id: str) -> float:
    """Complete sets available across a condition's token legs.

    Sums each token's positive shares over the lots whose ``condition_id``
    matches, then returns the minimum across the distinct tokens: one complete
    set holds one share of every token, so the minimum is the largest number of
    sets that can be merged.  The result is in shares; lots of other
    conditions, non-positive sizes and unknown tokens contribute nothing, and
    fewer than two distinct tokens yields ``0.0``.  Pure arithmetic, no I/O.
    """
    totals: dict[str, float] = {}
    for lot in lots:
        if lot.condition_id != condition_id:
            continue
        shares = float(lot.shares)
        if shares <= 0.0:
            continue
        totals[lot.token_id] = totals.get(lot.token_id, 0.0) + shares
    if len(totals) < 2:
        return 0.0
    return min(totals.values())
