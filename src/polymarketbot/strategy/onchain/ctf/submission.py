"""Sign, persist and broadcast one settlement payload."""

from __future__ import annotations

import sqlite3

from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.core import (
    EXECUTION_REVERTED,
    NONCE_TOO_LOW,
    REPLACEMENT_UNDERPRICED,
    classify_rpc_error,
)
from polymarketbot.strategy.onchain.pending import FAILED as SETTLEMENT_FAILED

from . import rlp as _rlp
from .constants import POLYGON_CHAIN_ID
from .results import SettlementOutcome

logger = get_logger("ctf_client")


class CtfSubmissionMixin:
    """The one path that turns calldata into a broadcast transaction."""

    def _broadcast_settlement(
        self,
        calldata: str,
        *,
        to_address: str | None = None,
        condition_id: str = "",
        kind: str = "merge",
        amount: float = 0.0,
        pol_usd_price: float | None = None,
    ) -> SettlementOutcome:
        """Sign, persist, broadcast and confirm one CTF transaction.

        The signed payload is written to the settlement store *before*
        `eth_sendRawTransaction`. From that point the hash is the identity of
        the settlement: a transient failure, a timeout or a `nonce too low`
        leads to the stored bytes being re-sent or the stored hash being looked
        up, never to a freshly signed transaction. The row is resolved only by a
        receipt; anything else is reported as `pending`.

        Gas cost is derived from the receipt (gasUsed * effectiveGasPrice),
        never from the pre-flight estimate.
        """
        refusal = self._settlement_refusal_reason()
        if refusal is not None or self._private_key_bytes is None:
            return SettlementOutcome(
                "failed", error=refusal or "settlement is not enabled"
            )
        target = to_address or self.ctf_address

        try:
            nonce = self.chain_client.get_transaction_count(self.signer_address, "pending")
            priority_wei, max_fee_wei = self._eip1559_fee_params_wei()

            estimated_gas = int(
                self.chain_client.estimate_gas(
                    {
                        "from": self.signer_address,
                        "to": target,
                        "data": calldata,
                        "value": "0x0",
                    }
                )
            )
            if estimated_gas <= 0:
                return SettlementOutcome(
                    "failed", error="eth_estimateGas returned no usable gas estimate"
                )
            gas_limit = int(estimated_gas * 1.25)  # 25% headroom for state drift

            signed = _rlp.sign_eip1559_transaction(
                self._private_key_bytes,
                chain_id=POLYGON_CHAIN_ID,
                nonce=nonce,
                max_priority_fee_per_gas=priority_wei,
                max_fee_per_gas=max_fee_wei,
                gas_limit=gas_limit,
                to=target,
                value=0,
                data=calldata,
                access_list=[],
            )
        except Exception as exc:  # noqa: BLE001 - preparation failures are reported
            logger.error(f"CTF settlement preparation failed: {exc}")
            return SettlementOutcome("failed", error=f"broadcast failed: {exc}")

        tx_hash = signed.tx_hash

        # --- persistence point: the payload exists on disk before it is sent ---
        try:
            if not self.settlement_store.record(
                tx_hash=tx_hash,
                raw_tx=signed.raw_transaction,
                nonce=nonce,
                condition_id=condition_id,
                kind=kind,
                amount=amount,
            ):
                logger.warning(
                    f"Settlement tx {tx_hash} was already stored; reusing the stored payload"
                )
        except sqlite3.IntegrityError:
            # The partial unique index allows one PENDING row per condition. If
            # this insert loses that race, another settlement is already in
            # flight (possibly from another process) and must be resolved first.
            logger.error(
                f"CTF settlement refused: another settlement is already pending for "
                f"{condition_id[:14]}; signed tx {tx_hash} was not broadcast"
            )
            return SettlementOutcome(
                "failed",
                tx_hash,
                error=(
                    f"another settlement is already pending for condition {condition_id}; "
                    "resolve it by hash before settling again"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - unpersisted broadcast is refused
            logger.error(f"CTF settlement refused: signed tx could not be persisted: {exc}")
            return SettlementOutcome(
                "failed", tx_hash, error=f"signed transaction could not be persisted: {exc}"
            )

        logger.info(
            f"Broadcasting CTF {kind} settlement tx {tx_hash} "
            f"(nonce={nonce}, gas_limit={gas_limit}, max_fee={max_fee_wei / 1e9:.2f} Gwei)"
        )
        try:
            broadcast_hash = self.chain_client.send_raw_transaction(signed.raw_transaction)
            if broadcast_hash and str(broadcast_hash).lower() != tx_hash.lower():
                logger.warning(
                    f"Node reported tx hash {broadcast_hash} for stored tx {tx_hash}"
                )
        except Exception as exc:  # noqa: BLE001 - ambiguity is the whole point here
            error_kind = classify_rpc_error(exc)
            logger.error(f"CTF settlement broadcast failed ({error_kind}): {exc}")
            if error_kind == EXECUTION_REVERTED:
                # The node rejected the call outright: nothing was mined.
                self._resolve_settlement(tx_hash, SETTLEMENT_FAILED)
                return SettlementOutcome(
                    "failed", tx_hash, error=f"broadcast rejected: {exc}"
                )

            receipt = self._receipt_once(tx_hash)
            if receipt is not None:
                # It landed despite the error, or a previous attempt mined it.
                return self._finalize_settlement(tx_hash, receipt, pol_usd_price, max_fee_wei)
            if error_kind in (NONCE_TOO_LOW, REPLACEMENT_UNDERPRICED):
                # Another transaction owns this nonce, so these bytes cannot be
                # mined as-is. Re-sending them changes nothing; a new nonce
                # would be a second merge. Leave the row PENDING.
                return SettlementOutcome(
                    "pending",
                    tx_hash,
                    error=(
                        f"settlement ambiguous ({error_kind}): {exc}; tx {tx_hash} has no "
                        "receipt yet and is not retried"
                    ),
                )
            # Transient (429 / 5xx / timeout): the node may already hold the
            # bytes. Re-send the identical payload, which is idempotent.
            if not self._rebroadcast(signed.raw_transaction):
                return SettlementOutcome(
                    "pending",
                    tx_hash,
                    error=f"settlement ambiguous ({error_kind}): {exc}; tx {tx_hash} not confirmed",
                )

        receipt = self._wait_for_receipt(tx_hash)
        if receipt is None:
            # Not mined in the window. It may still be mined later, so re-send
            # the identical bytes once, watch briefly, then leave it PENDING.
            if self._rebroadcast(signed.raw_transaction):
                receipt = self._wait_for_receipt(
                    tx_hash, timeout_s=self.receipt_rebroadcast_wait_s
                )
            if receipt is None:
                return SettlementOutcome(
                    "pending",
                    tx_hash,
                    error=(
                        f"transaction {tx_hash} not mined within "
                        f"{self.receipt_timeout_s:.0f}s; stored and left pending"
                    ),
                )
        return self._finalize_settlement(tx_hash, receipt, pol_usd_price, max_fee_wei)

