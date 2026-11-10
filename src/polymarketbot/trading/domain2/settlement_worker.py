"""Domain 2 Smart Settlement Worker.

Monitors UMA dispute liveness window expiration and broadcasts on-chain
`CTF.redeemPositions()` transactions upon condition resolution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
from polymarketbot.logs import get_logger
from polymarketbot.trading.ctf import (
    PolygonCtfClient,
    RedeemExecutionResult,
    DEFAULT_COLLATERAL_USDC,
    ZERO_BYTES32,
)
from polymarketbot.trading.settlement import OnchainFlight, measure_onchain_flight
from polymarketbot.trading.domain2.types import (
    Domain2Position,
    ConditionResolutionEvent,
)

logger = get_logger("domain2_settlement_worker")


@dataclass
class PendingRedemption:
    """An accepted redemption that has not paid its on-chain flight yet.

    The position stays in inventory and no par proceeds are credited until
    ``due_ts``. The flight is the measured RPC round trip plus one measured
    block interval; ``gaps`` names any component that could not be measured.
    """

    condition_id: str
    submitted_ts: float
    due_ts: float
    flight_s: float
    rpc_rtt_s: float = 0.0
    block_interval_s: float = 0.0
    gaps: Tuple[str, ...] = ()


class Domain2SettlementWorker:
    """Automated settlement and position redemption manager."""

    def __init__(
        self,
        ctf_client: PolygonCtfClient,
        collateral_token: str = DEFAULT_COLLATERAL_USDC,
        dry_run: bool = True,
    ):
        self.ctf_client = ctf_client
        self.collateral_token = collateral_token
        self.dry_run = dry_run

        # Active positions awaiting settlement: condition_id -> Position
        self._positions: Dict[str, Domain2Position] = {}
        # Confirmed resolutions received from event listener
        self._resolved_conditions: Dict[str, ConditionResolutionEvent] = {}
        # Accepted redemptions still paying their on-chain flight. The position
        # stays in `_positions` and nothing is credited until the flight lands.
        self._pending_redemptions: List[PendingRedemption] = []

    def register_position(self, position: Domain2Position) -> None:
        """Register newly filled position to be monitored for settlement."""
        cid = position.condition_id.lower()
        self._positions[cid] = position
        logger.info(
            "Registered position for settlement tracking: condition {}, shares {:,.2f}, cost ${:,.2f}, expiry {}",
            cid[:14], position.shares, position.cost_basis_usdc, position.liveness_expiry_ts,
        )

    def on_condition_resolution(self, event: ConditionResolutionEvent) -> None:
        """Record confirmed condition resolution event from CTF."""
        cid = event.condition_id.lower()
        self._resolved_conditions[cid] = event
        logger.info("Recorded confirmed on-chain resolution for condition {}", cid[:14])
        if cid in self._positions:
            self._positions[cid].is_resolved = True

    def mark_disputed(self, condition_id: str) -> None:
        """Mark position as disputed, preventing automated redemption."""
        cid = condition_id.lower()
        if cid in self._positions:
            self._positions[cid].is_disputed = True
            logger.warning("Position {} marked as DISPUTED; automated settlement disabled", cid[:14])

    def check_and_settle_ready_positions(
        self,
        now_ts: Optional[float] = None,
    ) -> List[RedeemExecutionResult]:
        """Queue newly eligible redemptions and land the ones whose flight completed.

        Eligible conditions must:
        1. Have passed their dispute liveness expiry timestamp.
        2. Have on-chain resolution confirmed by CTF.
        3. Have is_disputed == False.
        4. Not have been redeemed already.

        A newly eligible condition is not redeemed in the same call: it is
        queued for one measured RPC round trip plus one measured block
        interval. Only a later call, once that flight has elapsed, broadcasts
        and returns the result — so a caller can never credit par proceeds at
        decision time.
        """
        current_time = float(now_ts if now_ts is not None else time.time())
        results: List[RedeemExecutionResult] = []

        # 1. Land redemptions whose measured flight has completed.
        still_pending: List[PendingRedemption] = []
        for pending in self._pending_redemptions:
            if pending.due_ts > current_time:
                still_pending.append(pending)
                continue
            cid = pending.condition_id
            pos = self._positions.get(cid)
            flight = max(0.0, current_time - pending.submitted_ts)
            if pos is None or pos.is_redeemed:
                logger.info(
                    "Redeem landing for {} dropped: position is no longer live after {:.3f}s in flight",
                    cid[:14], flight,
                )
                continue
            if pos.is_disputed:
                logger.warning(
                    "Redeem landing for {} refused: position became disputed during the {:.3f}s flight",
                    cid[:14], flight,
                )
                continue
            res = self._redeem_position(pos)
            results.append(res)
            if res.success:
                pos.is_redeemed = True
                pos.redeem_tx_hash = res.tx_hash
                logger.info(
                    "Landed CTF redemption for {} (tx={}) {:.3f}s after submission "
                    "(flight {:.3f}s = RPC {:.3f}s + block {:.3f}s)",
                    cid[:14], res.tx_hash, flight, pending.flight_s,
                    pending.rpc_rtt_s, pending.block_interval_s,
                )
        self._pending_redemptions = still_pending

        # 2. Queue newly eligible positions at one measured on-chain flight.
        pending_cids = {p.condition_id for p in self._pending_redemptions}
        ready_cids = [
            cid for cid, pos in self._positions.items()
            if not pos.is_redeemed
            and not pos.is_disputed
            and cid not in pending_cids
            and (current_time >= pos.liveness_expiry_ts or pos.is_resolved)
        ]
        if not ready_cids:
            return results

        flight = self._measure_flight()
        if flight.gaps:
            logger.warning(
                "On-chain redemption flight is partially unmeasured ({}); queueing {} "
                "redemption(s) with {:.3f}s of measured flight and logging the gap.",
                "; ".join(flight.gaps), len(ready_cids), flight.total_s,
            )
        for cid in ready_cids:
            self._pending_redemptions.append(
                PendingRedemption(
                    condition_id=cid,
                    submitted_ts=current_time,
                    due_ts=current_time + flight.total_s,
                    flight_s=flight.total_s,
                    rpc_rtt_s=flight.rpc_rtt_s,
                    block_interval_s=flight.block_interval_s,
                    gaps=flight.gaps,
                )
            )
            logger.info(
                "Queued CTF redemption for {} at {:.3f}s measured on-chain flight "
                "(RPC {:.3f}s + block {:.3f}s)",
                cid[:14], flight.total_s, flight.rpc_rtt_s, flight.block_interval_s,
            )

        return results

    def _measure_flight(self) -> OnchainFlight:
        """Measured RPC round trip + block interval for one redemption."""
        return measure_onchain_flight(getattr(self.ctf_client, "chain_client", None))

    def _redeem_position(self, pos: Domain2Position) -> RedeemExecutionResult:
        """Execute on-chain redeemPositions call via PolygonCtfClient."""
        cid = pos.condition_id.lower()
        logger.info("Broadcasting CTF redeemPositions for condition {}...", cid[:14])

        try:
            # Binary markets use partition index_sets [1, 2]
            result = self.ctf_client.redeem_positions(
                condition_id=cid,
                index_sets=[1, 2],
                collateral_token=self.collateral_token,
                parent_collection_id=ZERO_BYTES32,
            )
            return result
        except Exception as e:
            logger.error("Failed to execute redeemPositions for {}: {}", cid[:14], e)
            return RedeemExecutionResult(
                success=False,
                tx_hash="",
                condition_id=cid,
                index_sets=[1, 2],
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=str(e),
                is_dry_run=self.dry_run,
                status="failed",
            )
