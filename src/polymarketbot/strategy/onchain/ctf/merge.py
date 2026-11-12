"""Complete-set merge execution and non-CTF settlement targets."""

from __future__ import annotations

import uuid

from polymarketbot.logs import get_logger

from .results import MergeExecutionResult

logger = get_logger("ctf_client")


class CtfMergeMixin:
    """Live/dry-run mergePositions and router-funded settlement."""

    def merge_positions(
        self,
        condition_id: str,
        amount_shares: float,
        index_sets: list[int] | None = None,
        pol_usd_price: float | None = None,
        token_ids: tuple[str, ...] | None = None,
    ) -> MergeExecutionResult:
        """Execute or simulate on-chain CTF complete-set merge.

        Converts complete sets (YES + NO) into USDC cash collateral. Live
        success is only reported once the transaction receipt has status 1;
        the returned gas cost is the receipt's, not an estimate.
        """
        if amount_shares <= 0.0:
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error="Zero amount shares to merge",
            )

        indices = list(index_sets or [1, 2])
        try:
            calldata = self.encode_merge_positions(condition_id, amount_shares, index_sets)
        except ValueError as exc:
            # A hand-built ABI word is an instruction to the contract: refuse
            # instead of broadcasting a malformed payload.
            logger.error(
                f"CTF mergePositions refused for condition {condition_id[:14]}: {exc}"
            )
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=f"invalid merge calldata: {exc}",
                is_dry_run=bool(self.dry_run),
            )

        live_key_error = self._live_without_key_reason()
        if live_key_error is not None:
            logger.error(f"Live CTF mergePositions refused for condition {condition_id[:14]}: {live_key_error}")
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=live_key_error,
                is_dry_run=False,
            )

        if self.dry_run:
            gas_fee_usd, gas_price_gwei, gas_units = self.estimate_gas_fee(pol_usd_price=pol_usd_price)
            mock_hash = f"0xmock_tx_{uuid.uuid4().hex}"
            logger.info(
                f"[DRY-RUN CTF] Simulated mergePositions({amount_shares:.2f} sets) on condition {condition_id[:14]}... | "
                f"Unlocked: ${amount_shares:.2f} USDC | Est Gas: ${gas_fee_usd:.4f} ({gas_units} units @ {gas_price_gwei} Gwei) | "
                f"TxHash: {mock_hash[:16]}..."
            )
            return MergeExecutionResult(
                success=True,
                tx_hash=mock_hash,
                condition_id=condition_id,
                complete_sets_merged=amount_shares,
                usdc_unlocked=amount_shares,
                gas_fee_usd=gas_fee_usd,
                gas_units_used=gas_units,
                is_dry_run=True,
                status="dry_run",
            )

        refusal = self._settlement_refusal_reason()
        if refusal is not None:
            logger.error(f"Live CTF mergePositions refused for condition {condition_id[:14]}: {refusal}")
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=refusal,
                is_dry_run=False,
            )

        balance_error = self._verify_position_balances(
            condition_id, amount_shares, indices, token_ids
        )
        if balance_error is not None:
            logger.error(f"CTF mergePositions aborted: {balance_error}")
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=balance_error,
                is_dry_run=False,
            )

        logger.info(
            f"Broadcasting live CTF mergePositions for {amount_shares:.2f} complete sets on Polygon PoS..."
        )
        outcome = self._broadcast_settlement(
            calldata,
            condition_id=condition_id,
            kind="merge",
            amount=amount_shares,
            pol_usd_price=pol_usd_price,
        )
        if outcome.status == "pending":
            logger.warning(
                f"CTF mergePositions pending for {condition_id[:14]}: {outcome.error}"
            )
            return MergeExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF mergePositions is pending confirmation",
                is_dry_run=False,
                status="pending",
            )
        if not outcome.success:
            return MergeExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF mergePositions failed",
                is_dry_run=False,
                status="failed",
            )

        logger.info(
            f"CTF mergePositions confirmed: {amount_shares:.2f} sets, "
            f"tx={outcome.tx_hash}, gas={outcome.gas_units_used} units (${outcome.gas_fee_usd:.4f})"
        )
        return MergeExecutionResult(
            success=True,
            tx_hash=outcome.tx_hash,
            condition_id=condition_id,
            complete_sets_merged=amount_shares,
            usdc_unlocked=amount_shares,
            gas_fee_usd=outcome.gas_fee_usd,
            gas_units_used=outcome.gas_units_used,
            is_dry_run=False,
            status="success",
        )


    def execute_settlement(
        self,
        calldata: str,
        *,
        to_address: str,
        condition_id: str,
        amount_shares: float,
        pol_usd_price: float | None = None,
        kind: str = "merge",
    ) -> MergeExecutionResult:
        """Broadcast a merge/convert call to a non-CTF target.

        Used for the v2 Router (``merge(bytes31,uint256)``) and the NegRisk
        adapter (``mergePositions(bytes32,uint256)``). The CTF preflight in
        :meth:`merge_positions` reads CTF balances, which do not exist on those
        ledgers, so callers must run ``settlement.verify_route`` instead. The
        amount is reported 1:1 only after a receipt with status 1.
        """
        amount = float(amount_shares)
        if not (
            isinstance(to_address, str)
            and to_address.startswith("0x")
            and len(to_address) == 42
        ):
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=f"invalid settlement target: {to_address!r}",
                status="failed",
            )
        if self.dry_run:
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                is_dry_run=True,
                error="non-CTF settlement has no dry-run simulation",
                status="failed",
            )

        outcome = self._broadcast_settlement(
            calldata,
            to_address=to_address,
            condition_id=condition_id,
            kind=kind,
            amount=amount,
            pol_usd_price=pol_usd_price,
        )
        success = outcome.status == "success"
        return MergeExecutionResult(
            success=success,
            tx_hash=outcome.tx_hash,
            condition_id=condition_id,
            complete_sets_merged=amount if success else 0.0,
            usdc_unlocked=amount if success else 0.0,
            gas_fee_usd=outcome.gas_fee_usd,
            gas_units_used=outcome.gas_units_used,
            is_dry_run=False,
            error=outcome.error,
            status=outcome.status,
        )

