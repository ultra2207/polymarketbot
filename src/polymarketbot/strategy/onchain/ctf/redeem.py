"""Resolved-market position redemption."""

from __future__ import annotations

import uuid
from typing import List, Optional

from polymarketbot.logs import get_logger

from .constants import ZERO_BYTES32
from .results import RedeemExecutionResult

logger = get_logger("ctf_client")


class CtfRedeemMixin:
    """Live/dry-run redeemPositions."""

    def redeem_positions(
        self,
        condition_id: str,
        index_sets: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
        parent_collection_id: str = ZERO_BYTES32,
        pol_usd_price: Optional[float] = None,
    ) -> RedeemExecutionResult:
        """Execute or simulate on-chain CTF position redemption for resolved markets."""
        indices = list(index_sets or [1, 2])
        calldata = self.encode_redeem_positions(
            condition_id=condition_id,
            index_sets=indices,
            collateral_token=collateral_token,
            parent_collection_id=parent_collection_id,
        )

        live_key_error = self._live_without_key_reason()
        if live_key_error is not None:
            logger.error(f"Live CTF redeemPositions refused for condition {condition_id[:14]}: {live_key_error}")
            return RedeemExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=live_key_error,
                is_dry_run=False,
            )

        if self.dry_run:
            gas_fee_usd, gas_price_gwei, gas_units = self.estimate_gas_fee_redeem(pol_usd_price=pol_usd_price)
            mock_hash = f"0xmock_redeem_tx_{uuid.uuid4().hex}"
            logger.info(
                f"[DRY-RUN CTF] Simulated redeemPositions on condition {condition_id[:14]}... | "
                f"Est Gas: ${gas_fee_usd:.4f} ({gas_units} units @ {gas_price_gwei} Gwei) | "
                f"TxHash: {mock_hash[:16]}..."
            )
            return RedeemExecutionResult(
                success=True,
                tx_hash=mock_hash,
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=gas_fee_usd,
                gas_units_used=gas_units,
                is_dry_run=True,
                status="dry_run",
            )

        refusal = self._settlement_refusal_reason()
        if refusal is not None:
            logger.error(f"Live CTF redeemPositions refused for condition {condition_id[:14]}: {refusal}")
            return RedeemExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=refusal,
                is_dry_run=False,
            )

        logger.info(
            f"Broadcasting live CTF redeemPositions on Polygon PoS for condition {condition_id[:14]}..."
        )
        outcome = self._broadcast_settlement(
            calldata,
            condition_id=condition_id,
            kind="redeem",
            amount=0.0,
            pol_usd_price=pol_usd_price,
        )
        if outcome.status == "pending":
            logger.warning(
                f"CTF redeemPositions pending for {condition_id[:14]}: {outcome.error}"
            )
            return RedeemExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF redeemPositions is pending confirmation",
                is_dry_run=False,
                status="pending",
            )
        if not outcome.success:
            return RedeemExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF redeemPositions failed",
                is_dry_run=False,
                status="failed",
            )

        logger.info(
            f"CTF redeemPositions confirmed: condition {condition_id[:14]}, "
            f"tx={outcome.tx_hash}, gas={outcome.gas_units_used} units (${outcome.gas_fee_usd:.4f})"
        )
        return RedeemExecutionResult(
            success=True,
            tx_hash=outcome.tx_hash,
            condition_id=condition_id,
            index_sets=indices,
            gas_fee_usd=outcome.gas_fee_usd,
            gas_units_used=outcome.gas_units_used,
            is_dry_run=False,
            status="success",
        )


