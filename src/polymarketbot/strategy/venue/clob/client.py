"""The composed :class:`LiveClobClient` built from the per-area mixins."""

from __future__ import annotations

import threading
from typing import Any

import requests

from polymarketbot.execution.self_trade import SelfTradeGuard
from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.risk.fast_signer import (
    DEFAULT_CHAIN_ID,
    DEFAULT_CTF_EXCHANGE,
    DEFAULT_CTF_EXCHANGE_V2,
    V2_NEG_RISK_CTF_EXCHANGE,
    FastEIP712Signer,
    FastEIP712SignerV2,
    private_key_to_address,
)
from polymarketbot.strategy.venue.clob.auth import AuthHeadersMixin, ClobAuthCredentials
from polymarketbot.strategy.venue.clob.batch import BatchMixin
from polymarketbot.strategy.venue.clob.cancel import CancelMixin
from polymarketbot.strategy.venue.clob.common import (
    DEFAULT_CLOB_HOST,
    DEFAULT_ORDER_VERSION,
    MOCK_TEST_PRIVATE_KEY,
    NEG_RISK_CTF_EXCHANGE,
    SUPPORTED_ORDER_VERSIONS,
    ZERO_ADDRESS,
    ClobAPIError,
)
from polymarketbot.strategy.venue.clob.dispatch import DispatchMixin
from polymarketbot.strategy.venue.clob.http import HttpMixin
from polymarketbot.strategy.venue.clob.orders import OrderMixin
from polymarketbot.strategy.venue.clob.reads import ReadMixin
from polymarketbot.strategy.venue.clob.rebates import RebateMixin
from polymarketbot.strategy.venue.clob.rewards import RewardMixin
from polymarketbot.strategy.venue.clob.scoring import ScoringMixin
from polymarketbot.strategy.venue.clob.signing import SignedOrderMixin
from polymarketbot.strategy.venue.clob.ticks import TicksMixin

logger = get_logger("clob_client")

# FastOrderSigner alias pointing to high-performance EIP-712 signer
FastOrderSigner = FastEIP712Signer


class LiveClobClient(
    TicksMixin,
    AuthHeadersMixin,
    HttpMixin,
    SignedOrderMixin,
    OrderMixin,
    DispatchMixin,
    BatchMixin,
    CancelMixin,
    ReadMixin,
    RewardMixin,
    RebateMixin,
    ScoringMixin,
):
    """Production REST Client for Polymarket Central Limit Order Book (CLOB)."""

    def __init__(
        self,
        credentials: ClobAuthCredentials | None = None,
        base_url: str = DEFAULT_CLOB_HOST,
        chain_id: int = DEFAULT_CHAIN_ID,
        verifying_contract: str = DEFAULT_CTF_EXCHANGE,
        timeout: float = 5.0,
        dry_run: bool = False,
        self_trade_guard: SelfTradeGuard | None = None,
        order_version: int | None = None,
    ) -> None:
        self.credentials = credentials or ClobAuthCredentials.from_env()
        self.base_url = base_url.rstrip("/")
        self.chain_id = chain_id
        self.verifying_contract = verifying_contract
        self.timeout = timeout
        self.signature_type = int(self.credentials.signature_type)

        self.self_trade_guard = self_trade_guard

        # CLOB order version: resolved from GET /version unless injected.
        self._version_lock = threading.Lock()
        self._order_version: int | None = None
        if order_version is not None:
            self._validate_order_version(order_version)
            self._order_version = int(order_version)

        # Per-token market parameter caches (filled by get_tick_size/get_neg_risk).
        self._tick_size_cache: dict[str, float] = {}
        self._neg_risk_cache: dict[str, bool] = {}

        # Epoch seconds until which the venue asked us not to submit orders
        # (matching-engine restart / throttle). Set only from the server's own
        # ``retry_after_seconds``; never from a local constant.
        self._order_restricted_until: float = 0.0

        # Enable dry_run if explicitly requested or if credentials are unconfigured/incomplete
        self.dry_run = dry_run or not self.credentials.is_fully_configured
        if self.dry_run:
            logger.info(
                f"[DRY-RUN MODE ACTIVE] LiveClobClient initialized in safe inspection mode "
                f"(configured={self.credentials.is_configured}, has_key={self.credentials.has_signing_key}). "
                f"No real funds will be risked."
            )
        elif self.signature_type == 0 and self.credentials.address and self.credentials.has_signing_key:
            derived = self._derive_eoa()
            if derived and derived.lower() != self.credentials.address.lower():
                logger.warning(
                    "POLY_ADDRESS differs from the signing key's EOA while "
                    "POLY_SIGNATURE_TYPE=0. If the address is a proxy/Safe, set "
                    "POLY_SIGNATURE_TYPE=1 (POLY_PROXY) or 2 (POLY_GNOSIS_SAFE)."
                )

        # Setup EIP-712 signer(s) with graceful fallback
        self._signing_key = self.credentials.private_key or (
            MOCK_TEST_PRIVATE_KEY if self.dry_run else ""
        )
        self._signers: dict[str, FastOrderSigner | FastEIP712SignerV2] = {}
        self._signer_lock = threading.Lock()
        self._sign_lock = threading.Lock()

        # V1 signer kept as `self.signer` for backwards compatibility; V2 signers
        # are created on demand by version/contract.
        try:
            self.signer = self._signer_for(1, self.verifying_contract)
        except (ValueError, TypeError) as e:
            logger.warning(
                f"Failed to initialize FastOrderSigner with key ({e}). "
                f"Falling back to mock test signer in safe dry-run mode."
            )
            self._signing_key = MOCK_TEST_PRIVATE_KEY
            self._signers.clear()
            self.signer = self._signer_for(1, self.verifying_contract)
            self.dry_run = True

        # Signing identity (audit C12):
        #   signer_address = EOA that produced the EIP-712 signature
        #   maker_address  = funder wallet holding collateral (proxy/Safe or the EOA)
        self.signer_address = self.signer.address or self.credentials.address or ZERO_ADDRESS
        self.maker_address = self.credentials.address or self.signer_address
        # Backwards-compatible alias used by telemetry/preflight.
        self.address = self.maker_address

        logger.info(
            f"CLOB identity: signature_type={self.signature_type} "
            f"maker={self.maker_address} signer={self.signer_address} "
            f"dry_run={self.dry_run}"
        )

        try:
            install_doh_fallback()
        except Exception:  # noqa: BLE001, S110 - DoH DNS patch is best-effort
            pass

        # Thread-safe HTTP: one requests.Session per thread (audit S12).
        # The `session` property always returns this thread's session, so both the
        # client and callers (e.g. DataAPIClient) share a per-thread connection pool.
        self._session_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: list[requests.Session] = []


        # Simulated state for dry-run tracking
        self._mock_orders: dict[str, dict[str, Any]] = {}
        self._mock_books: dict[str, dict[str, Any]] = {}
        self._mock_balance_usd: float = 100.0

    def _derive_eoa(self) -> str:
        """Return the EOA address derived from the configured private key ('' if none)."""
        if not self.credentials.has_signing_key:
            return ""
        try:
            clean_pk = self.credentials.private_key.removeprefix("0x").removeprefix("0X")
            return private_key_to_address(bytes.fromhex(clean_pk))
        except (ValueError, TypeError):
            return ""

    def _signer_for(
        self, order_version: int, verifying_contract: str
    ) -> FastOrderSigner | FastEIP712SignerV2:
        """Return the cached EIP-712 signer for (order version, verifying contract).

        Negative-risk markets use a different exchange contract and each order
        version has its own struct/domain, so one signer is cached per
        (version, contract) pair (audit C12).
        """
        version = int(order_version)
        cache_key = f"v{version}:{str(verifying_contract).lower()}"
        with self._signer_lock:
            signer = self._signers.get(cache_key)
            if signer is None:
                signer_cls = FastEIP712SignerV2 if version == 2 else FastOrderSigner
                signer = signer_cls(
                    private_key=self._signing_key or None,
                    verifying_contract=verifying_contract,
                    chain_id=self.chain_id,
                )
                self._signers[cache_key] = signer
            return signer

    @staticmethod
    def _validate_order_version(version: int) -> None:
        """Reject order versions this bot cannot sign (v3 uses position-id routing)."""
        if int(version) not in SUPPORTED_ORDER_VERSIONS:
            raise ClobAPIError(
                f"unsupported CLOB order version {version}; this bot signs only "
                f"versions {SUPPORTED_ORDER_VERSIONS} (v3 position-id routing is not supported)",
                status_code=200,
                path="/version",
            )

    def _fetch_order_version(self) -> int:
        """GET /version and validate the result (public endpoint, no auth)."""
        payload = self._request("GET", "/version", auth=False)
        raw = payload.get("version") if isinstance(payload, dict) else payload
        try:
            version = int(raw)
        except (TypeError, ValueError) as e:
            raise ClobAPIError(
                f"GET /version returned an unexpected body: {payload!r}",
                status_code=200,
                path="/version",
            ) from e
        self._validate_order_version(version)
        logger.info(f"CLOB order version resolved: {version}")
        return version

    @property
    def order_version(self) -> int:
        """Cached CLOB order version (1 or 2), fetched from ``GET /version`` once.

        Raises:
            ClobAPIError: if the version cannot be read, or is neither 1 nor 2.
        """
        cached = self._order_version
        if cached is not None:
            return cached

        # The network call must never hold the cache lock: a slow GET /version
        # would otherwise serialise every thread that only wants the cached read.
        version = self._fetch_order_version()
        with self._version_lock:
            # Double-check: a concurrent thread may have filled the cache first.
            if self._order_version is None:
                self._order_version = version
            return self._order_version

    def refresh_order_version(self) -> int:
        """Discard the cached version and resolve it again (one retry only)."""
        with self._version_lock:
            self._order_version = None
        return self.order_version

    def _resolve_order_version(self) -> int:
        """Version to sign with now.

        Dry-run never calls ``GET /version`` (offline previews) and uses
        ``DEFAULT_ORDER_VERSION`` unless a version was injected. Live clients
        always resolve the version from the server before signing.
        """
        if self._order_version is not None:
            return self._order_version
        if self.dry_run:
            with self._version_lock:
                if self._order_version is None:
                    logger.debug(
                        f"dry-run: signing with default order version {DEFAULT_ORDER_VERSION}"
                    )
                    self._order_version = DEFAULT_ORDER_VERSION
                return self._order_version
        return self.order_version

    def _exchange_contract(self, order_version: int, neg_risk: bool) -> str:
        """EIP-712 verifying contract for an order version / market type."""
        if int(order_version) == 2:
            return V2_NEG_RISK_CTF_EXCHANGE if neg_risk else DEFAULT_CTF_EXCHANGE_V2
        return NEG_RISK_CTF_EXCHANGE if neg_risk else self.verifying_contract
