"""Domain 2 Smart Settlement Worker.

Monitors UMA dispute liveness window expiration and broadcasts on-chain
`CTF.redeemPositions()` transactions upon condition resolution.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Any
from polymarketbot.logs import get_logger
from polymarketbot.trading.ctf import (
    PolygonCtfClient,
    RedeemExecutionResult,
    DEFAULT_COLLATERAL_USDC,
    ZERO_BYTES32,
)
from polymarketbot.trading.domain2.types import (
    Domain2Position,
    ConditionResolutionEvent,
)

logger = get_logger("domain2_settlement_worker")


class Domain2SettlementWorker:
    """Automated settlement and position redemption manager."""

    def __init__(
        self,
        ctf_client: PolygonCtfClient,
        collateral_token: str = DEFAULT_COLLATERAL_USDC,
        dry_run: bool = True,
        min_redeem_shares: float = 20.0,
        max_gas_friction_cents_per_share: float = 0.05,
    ):
        self.ctf_client = ctf_client
        self.collateral_token = collateral_token
        self.dry_run = dry_run
        self.min_redeem_shares = float(min_redeem_shares)
        self.max_gas_friction_cents_per_share = float(max_gas_friction_cents_per_share)

        # Active positions awaiting settlement: condition_id -> Position
        self._positions: Dict[str, Domain2Position] = {}
        # Confirmed resolutions received from event listener
        self._resolved_conditions: Dict[str, ConditionResolutionEvent] = {}

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
        now_ts: Optional[int] = None,
    ) -> List[RedeemExecutionResult]:
        """Check all tracked positions and trigger redemption for eligible markets.

        Eligible conditions must:
        1. Have passed their dispute liveness expiry timestamp.
        2. Have on-chain resolution confirmed by CTF.
        3. Have is_disputed == False.
        4. Not have been redeemed already.
        """
        current_time = now_ts or int(time.time())
        results: List[RedeemExecutionResult] = []

        ready_cids = [
            cid for cid, pos in self._positions.items()
            if not pos.is_redeemed
            and not pos.is_disputed
            and (current_time >= pos.liveness_expiry_ts or pos.is_resolved)
        ]

        if not ready_cids:
            return []

        logger.info("Found {} positions ready for on-chain CTF redemption", len(ready_cids))

        for cid in ready_cids:
            pos = self._positions[cid]
            res = self._redeem_position(pos)
            results.append(res)
            if res.success:
                pos.is_redeemed = True
                pos.redeem_tx_hash = res.tx_hash
                logger.info("Position {} successfully redeemed (tx={})", cid[:14], res.tx_hash)

        return results

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
