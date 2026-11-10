"""Receipt polling and settlement finalisation."""

from __future__ import annotations

import time
from typing import Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.core import ALREADY_KNOWN, classify_rpc_error
from polymarketbot.strategy.onchain.pending import (
    FAILED as SETTLEMENT_FAILED,
    REVERTED as SETTLEMENT_REVERTED,
    SUCCESS as SETTLEMENT_SUCCESS,
)

from . import gas as _gas
from .results import SettlementOutcome
from .rlp import _hex_to_int

logger = get_logger("ctf_client")


class CtfReceiptMixin:
    """Bounded receipt watching and terminal settlement outcomes."""

    def _wait_for_receipt(
        self,
        tx_hash: str,
        timeout_s: Optional[float] = None,
        interval_s: Optional[float] = None,
    ) -> Optional[dict]:
        """Bounded receipt poll. Returns None on timeout."""
        timeout = self.receipt_timeout_s if timeout_s is None else float(timeout_s)
        interval = (
            self.receipt_poll_interval_s if interval_s is None else float(interval_s)
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                receipt = self.chain_client.get_transaction_receipt(tx_hash)
            except Exception as exc:  # noqa: BLE001 - a transient miss is retried
                logger.debug(f"eth_getTransactionReceipt({tx_hash}) failed: {exc}")
                receipt = None
            if isinstance(receipt, dict) and receipt:
                return receipt
            if time.monotonic() >= deadline:
                return None
            if interval > 0:
                time.sleep(interval)

    def _receipt_once(self, tx_hash: str) -> Optional[dict]:
        """One receipt lookup, tolerant of a transport failure."""
        try:
            receipt = self.chain_client.get_transaction_receipt(tx_hash)
        except Exception as exc:  # noqa: BLE001 - lookup failure is "not known yet"
            logger.debug(f"eth_getTransactionReceipt({tx_hash}) failed: {exc}")
            return None
        return receipt if isinstance(receipt, dict) and receipt else None

    def _rebroadcast(self, raw_hex: str) -> bool:
        """Re-send already-signed bytes. Idempotent: same bytes, same hash.

        Only ever called with a payload that is already stored, so a failure
        here cannot produce a second transaction with a new nonce.
        """
        try:
            self.chain_client.send_raw_transaction(raw_hex)
            return True
        except Exception as exc:  # noqa: BLE001 - the outcome is decided by the receipt
            kind = classify_rpc_error(exc)
            if kind == ALREADY_KNOWN:
                return True
            logger.warning(f"Re-send of the stored settlement payload failed ({kind}): {exc}")
            return False

    def _resolve_settlement(
        self, tx_hash: str, status: str, receipt: Optional[dict] = None
    ) -> None:
        try:
            self.settlement_store.resolve(tx_hash, status, receipt)
        except Exception as exc:  # noqa: BLE001 - accounting must not depend on the log
            logger.error(f"Could not resolve settlement row {tx_hash} as {status}: {exc}")

    def _finalize_settlement(
        self,
        tx_hash: str,
        receipt: dict,
        pol_usd_price: Optional[float],
        max_fee_wei: int,
    ) -> SettlementOutcome:
        """Turn a mined receipt into a terminal outcome and resolve the row."""
        status = receipt.get("status")
        gas_used = _hex_to_int(receipt.get("gasUsed"))
        effective_price = _hex_to_int(receipt.get("effectiveGasPrice")) or max_fee_wei
        pol_price = (
            float(pol_usd_price)
            if pol_usd_price is not None
            else _gas.get_dynamic_pol_usd_price()
        )
        # Gas is spent whether the call succeeded or reverted; report the receipt's
        # actual cost either way so accounting never invents a number. Without a
        # measured POL/USD the dollar figure is unknown: report None, not zero and
        # not a price we made up.
        if pol_price is None:
            logger.error(
                "receipt read but POL/USD is unmeasured; gas_fee_usd left unknown"
            )
            gas_fee_usd: Optional[float] = None
        else:
            gas_fee_usd = round(gas_used * effective_price / 1e18 * float(pol_price), 6)

        if status is None:
            self._resolve_settlement(tx_hash, SETTLEMENT_FAILED, receipt)
            return SettlementOutcome(
                "failed", tx_hash, gas_fee_usd, gas_used, "receipt has no status field"
            )
        if _hex_to_int(status) != 1:
            self._resolve_settlement(tx_hash, SETTLEMENT_REVERTED, receipt)
            return SettlementOutcome(
                "failed", tx_hash, gas_fee_usd, gas_used, f"transaction reverted (status={status})"
            )
        self._resolve_settlement(tx_hash, SETTLEMENT_SUCCESS, receipt)
        return SettlementOutcome("success", tx_hash, gas_fee_usd, gas_used, None)

