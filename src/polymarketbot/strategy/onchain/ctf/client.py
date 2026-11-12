"""``PolygonCtfClient``: identity, mode gating and method composition.

The behaviour lives in the mixins; this module owns the constructor, the
safety gates and the public class the rest of the bot imports.
"""

from __future__ import annotations

import os

from polymarketbot.logs import get_logger
from polymarketbot.risk.fast_signer import private_key_to_address
from polymarketbot.strategy.onchain.core import ChainClient
from polymarketbot.strategy.onchain.pending import PendingSettlementStore
from polymarketbot.strategy.reward.queue import CTFGasProfile, PolygonPoSEnvironment

from .balances import CtfBalanceMixin
from .calldata import CtfCalldataMixin
from .constants import (
    CTF_CONTRACT_POLYGON,
    DEFAULT_COLLATERAL_USDC,
    RECEIPT_POLL_INTERVAL_S,
    RECEIPT_REBROADCAST_WAIT_S,
    RECEIPT_TIMEOUT_S,
)
from .fees import CtfFeeMixin
from .merge import CtfMergeMixin
from .receipts import CtfReceiptMixin
from .redeem import CtfRedeemMixin
from .rlp import _parse_private_key
from .submission import CtfSubmissionMixin

logger = get_logger("ctf_client")


class PolygonCtfClient(
    CtfCalldataMixin,
    CtfFeeMixin,
    CtfBalanceMixin,
    CtfReceiptMixin,
    CtfSubmissionMixin,
    CtfMergeMixin,
    CtfRedeemMixin,
):
    """Direct on-chain client for Polymarket Conditional Tokens Framework on Polygon."""

    def __init__(
        self,
        rpc_url: str | None = None,
        private_key: str | None = None,
        ctf_address: str = CTF_CONTRACT_POLYGON,
        collateral_address: str = DEFAULT_COLLATERAL_USDC,
        chain_client: ChainClient | None = None,
        dry_run: bool = False,
        signature_type: int = 0,
        settlement_store: PendingSettlementStore | None = None,
    ) -> None:
        self.ctf_address = ctf_address.lower()
        self.collateral_address = collateral_address.lower()
        self.chain_client = chain_client or ChainClient(url=rpc_url)
        self.private_key = private_key or os.environ.get("POLY_PRIVATE_KEY", "")
        # `dry_run` may be False while the key is absent: the client then stays
        # safe/dry internally, but settlement calls must refuse rather than
        # report a simulated success for a live-mode request.
        self._dry_run_requested = bool(dry_run)
        self.dry_run = dry_run or not bool(self.private_key)
        # 0 = EOA (funds + tokens on the signing key), 1 = POLY_PROXY,
        # 2 = POLY_GNOSIS_SAFE. Only an EOA can be settled by this signer.
        self.signature_type = int(signature_type)

        self._private_key_bytes: bytes | None = None
        self.signer_address: str = ""
        if self.private_key:
            try:
                self._private_key_bytes = _parse_private_key(self.private_key)
            except ValueError as exc:
                logger.error(f"Invalid POLY_PRIVATE_KEY supplied; settlement disabled: {exc}")
            if self._private_key_bytes is not None:
                self.signer_address = private_key_to_address(self._private_key_bytes)

        self.gas_profile = CTFGasProfile()
        # No fee-market environment is built here: it would carry invented
        # defaults. Resolve one from measurement at the point of use with
        # ``PolygonPoSEnvironment.measured(...)``.
        self.pos_env: PolygonPoSEnvironment | None = None

        # Every live settlement is written here before it is broadcast. The
        # engine replaces this with a store bound to the engine database so the
        # record outlives the process; a standalone client still gets the
        # in-process guarantee.
        self.settlement_store = settlement_store or PendingSettlementStore()
        self.receipt_timeout_s = RECEIPT_TIMEOUT_S
        self.receipt_poll_interval_s = RECEIPT_POLL_INTERVAL_S
        self.receipt_rebroadcast_wait_s = RECEIPT_REBROADCAST_WAIT_S

        if self.dry_run:
            logger.info(
                f"[DRY-RUN CTF] PolygonCtfClient operating in safe dry-run mode "
                f"(has_key={bool(self.private_key)}). Real on-chain transactions will be simulated."
            )
        elif not self.is_settlement_enabled:
            logger.warning(
                "PolygonCtfClient is not in dry-run mode but cannot settle on-chain "
                f"(valid_key={self._private_key_bytes is not None}, signature_type={self.signature_type}). "
                "merge_positions/redeem_positions will refuse to broadcast."
            )

    @property
    def is_settlement_enabled(self) -> bool:
        """True only when live, EOA-keyed settlement can actually be broadcast.

        Proxy-wallet accounts (signature_type 1/2) never qualify: the CTF
        contract requires the call from the position owner (the proxy), which
        this client cannot sign for. Callers must not treat refusal as a
        simulated success.
        """
        return (
            not self.dry_run
            and self._private_key_bytes is not None
            and self.signature_type == 0
        )

    def _settlement_refusal_reason(self) -> str | None:
        if self.dry_run:
            return "client is in dry-run mode"
        if self.signature_type != 0:
            return (
                f"on-chain settlement requires an EOA (signature_type=0); "
                f"signature_type={self.signature_type} is a proxy wallet and cannot be "
                "settled by this signer"
            )
        if self._private_key_bytes is None:
            return "no valid POLY_PRIVATE_KEY available for signing"
        return None

    def _live_without_key_reason(self) -> str | None:
        """Refuse a live-mode request that has no usable signing key.

        Without this, `dry_run=False` plus an absent key silently degrades to a
        simulated success, which is exactly the accounting failure C1 describes.
        An explicit `dry_run=True` still simulates.
        """
        if self._dry_run_requested or self._private_key_bytes is not None:
            return None
        return (
            "live settlement requested (dry_run=False) but no valid "
            "POLY_PRIVATE_KEY is configured; refusing to simulate"
        )

