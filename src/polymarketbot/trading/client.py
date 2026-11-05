"""Production Polymarket Live CLOB Client for Real-Money Trading.

Provides:
1. EIP-712 Order Signing on Polygon (Chain ID 137). The CLOB order version is
   resolved from ``GET /version`` (production returned 2 on 2026-11-03) and the
   matching struct/domain is used: V1 FastOrder or V2 OrderV2 (millisecond
   timestamp, metadata, builder). Versions other than 1/2 raise ClobAPIError
   instead of signing blindly.
2. Level-2 HMAC-SHA256 authenticated REST calls to Polymarket CLOB:
   - POST /order (place signed limit orders)
   - DELETE /order (cancel individual orders)
   - DELETE /cancel-all (cancel all resting orders)
   - GET /data/orders (query user orders, paginated via next_cursor)
   - GET /data/trades (query user trades, paginated via next_cursor)
   - GET /balance-allowance?asset_type=COLLATERAL&signature_type=N
   - GET /markets/{condition_id}
   - GET /version, GET /tick-size, GET /neg-risk (order-version and market params)
3. Safe Graceful Dry-Run / Mock Mode:
   - When API keys or private keys are omitted or the account has $0 balance.
   - Computes authentic EIP-712 digests & signatures without broadcasting.
   - Logs simulated execution with microsecond timestamps and structured return codes.

Failure semantics (audit C2/C3/C4):
    Every authenticated REST call raises :class:`ClobAPIError` on a non-2xx status,
    a transport error, or an unparseable/unexpected body. No read method returns
    ``[]``, ``0.0`` or ``{"approved": False}`` as a stand-in for a failed call, so
    callers can distinguish "the exchange says zero" from "we could not ask".
    Dry-run mocks (``_mock_orders`` / ``_mock_balance_usd``) are the only synthetic
    values and are always flagged via ``raw``/``is_dry_run``.

Environment variables (audit M9)
    POLY_API_KEY / POLYMARKET_API_KEY            L2 API key (UUID)
    POLY_API_SECRET / POLYMARKET_SECRET          L2 API secret (url-safe base64)
    POLY_PASSPHRASE / POLYMARKET_PASSPHRASE      L2 API passphrase
    POLY_SIGNATURE_TYPE / POLYMARKET_SIGNATURE_TYPE
                                                0 = EOA (default), 1 = POLY_PROXY,
                                                2 = POLY_GNOSIS_SAFE,
                                                3 = POLY_1271 (Deposit Wallet)
    POLY_ADDRESS / PROXY_WALLET_ADDRESS          Funder / proxy wallet address that
                                                holds the collateral (maker). When
                                                omitted the EOA derived from the
                                                signing key is used as maker.
    POLY_PRIVATE_KEY / PRIVATE_KEY               EOA that signs orders (signer).

Proxy wallets: when trading through a Polymarket proxy or Gnosis Safe, the env var
below is the proxy/Safe address (the maker/funder), while the private key belongs to
the EOA that signs (the signer). Orders then carry ``maker = POLY_ADDRESS`` (the
funder) and ``signer = EOA`` with ``signatureType = 1`` or ``2``. The L2 auth header
``POLY_ADDRESS``, however, is always the signer EOA -- the address that owns the API
key -- exactly as py-clob-client's ``create_level_2_headers`` does.

Deposit wallets (``POLY_SIGNATURE_TYPE=3``, POLY_1271): ``POLY_ADDRESS`` is the
Deposit Wallet and the private key is the EOA that controls it. The order carries
``maker = signer = POLY_ADDRESS`` and a Solady ERC-7739 wrapped ECDSA signature
(the Deposit Wallet validates it via EIP-1271); the L2 ``POLY_ADDRESS`` header stays
the signer EOA. See :mod:`polymarketbot.trading.relayer` for the gasless call path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional, Union

import requests

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.execution.fast_cancel import DedicatedCancellationPool
from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.risk.fast_signer import (
    DEFAULT_CHAIN_ID,
    DEFAULT_CTF_EXCHANGE,
    DEFAULT_CTF_EXCHANGE_V2,
    V2_NEG_RISK_CTF_EXCHANGE,
    FastEIP712Signer,
    FastEIP712SignerV2,
    FastOrder,
    OrderV2,
    private_key_to_address,
)

logger = get_logger("clob_client")

# FastOrderSigner alias pointing to high-performance EIP-712 signer
FastOrderSigner = FastEIP712Signer

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
"""EIP-712 verifying contract for negative-risk markets (Polymarket CTF Exchange)."""
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
"""Cursor value returned by CLOB when the last page has been served."""
END_CURSOR = "LTE="
"""Default price increment when a market does not advertise one."""
DEFAULT_TICK_SIZE = 0.01
"""Collateral (USDC) fixed-math decimals used by CLOB balance responses."""
COLLATERAL_DECIMALS = 6
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
USER_AGENT = "polymarketbot/real-money-trading/1.0"

"""CLOB order version used when no version has been fetched yet.

``GET https://clob.polymarket.com/version`` returned ``{"version": 2}`` on
2026-11-03 and V1 CLOB orders were retired 2026-04-28, so live clients resolve
the version from the server; dry-run signature previews use this value.
"""
DEFAULT_ORDER_VERSION = 2
"""Negative-risk CTF Exchange V2 (Polygon), see docs.polymarket.com/resources/contracts."""
NEG_RISK_CTF_EXCHANGE_V2 = V2_NEG_RISK_CTF_EXCHANGE
"""Supported CLOB order versions; v3 uses position-id routing this bot lacks."""
SUPPORTED_ORDER_VERSIONS = (1, 2)

ZERO_BYTES32_HEX = "0x" + "00" * 32
"""Per-tick rounding (price dp, size dp, amount dp) from py-clob-client's ROUNDING_CONFIG.

Size is always 2 dp; the collateral/share amount is carried to price_dp + size_dp,
which is why no further product rounding is required.
"""
ROUNDING_CONFIG: dict[float, tuple[int, int, int]] = {
    0.1: (1, 2, 3),
    0.01: (2, 2, 4),
    0.005: (3, 2, 5),
    0.0025: (4, 2, 6),
    0.001: (3, 2, 5),
    0.0001: (4, 2, 6),
}
_TICK_TOLERANCE = 1e-9

# Deterministic fallback key for dry-run signature verification when no private key is supplied
MOCK_TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"

# Order salts: random 63-bit value with a process-local counter in the low 31 bits.
# Two orders created in the same millisecond can never collide (audit S17).
_SALT_LOCK = threading.Lock()
_SALT_COUNTER = 0
_SALT_COUNTER_MASK = (1 << 31) - 1
_SALT_RANDOM_BITS = 32
_SALT_MAX = (1 << 63) - 1


def _is_order_version_mismatch(text: str) -> bool:
    """True when the CLOB rejected an order because its signed version is stale."""
    return "order_version_mismatch" in str(text).lower()


def generate_order_salt() -> int:
    """Return a unique 63-bit order salt (random high bits, monotonic low bits)."""
    global _SALT_COUNTER
    with _SALT_LOCK:
        _SALT_COUNTER = (_SALT_COUNTER + 1) & _SALT_COUNTER_MASK
        counter = _SALT_COUNTER
    return ((secrets.randbits(_SALT_RANDOM_BITS) << 31) | counter) & _SALT_MAX


class ClobAPIError(RuntimeError):
    """A CLOB REST call failed, or returned a body we cannot trust.

    Attributes:
        status_code: HTTP status, or 0 for a transport/parse failure.
        message: Human-readable description including the request path.
        path: Request path (no query string) that failed.
        response_text: Raw body prefix when the server answered (empty otherwise).
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        *,
        path: str = "",
        response_text: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)
        self.path = path
        self.response_text = response_text


def _parse_collateral_amount(value: Any) -> float:
    """Convert a CLOB fixed-math amount into decimal collateral units.

    CLOB returns 6-decimal base units as strings (``"1500000"`` == 1.5 USDC).
    Some deployments/proxies return already-decimal strings (``"1.5"``); a value
    containing ``.`` (or an exponent) is treated as decimal, everything else as
    raw base units. The ``> 100_000`` guess from the old implementation is gone.
    """
    if isinstance(value, bool):
        raise ValueError(f"not an amount: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty amount")
    if "." in text or "e" in text.lower():
        return float(text)
    return int(text) / float(10**COLLATERAL_DECIMALS)


def _allowance_from_payload(payload: dict[str, Any]) -> float:
    """Extract the collateral allowance from a /balance-allowance response."""
    if payload.get("allowance") not in (None, ""):
        return _parse_collateral_amount(payload["allowance"])
    allowances = payload.get("allowances")
    if isinstance(allowances, dict):
        amounts = [
            _parse_collateral_amount(v)
            for v in allowances.values()
            if v not in (None, "")
        ]
        # An allowance map holds one entry per spender (exchange / neg-risk exchange).
        # Collateral is tradeable when at least one spender is approved.
        return max(amounts) if amounts else 0.0
    raise ValueError("response contains neither 'allowance' nor 'allowances'")


def rounding_config(tick_size: float) -> tuple[int, int, int]:
    """Return (price dp, size dp, amount dp) for a market tick.

    Raises:
        ValueError: for a tick that is not in the supported table. The old code
            silently rounded to 3 dp; a wrong tick means a rejected order.
    """
    tick = float(tick_size)
    for known, config in ROUNDING_CONFIG.items():
        if abs(tick - known) <= _TICK_TOLERANCE:
            return config
    raise ValueError(f"unsupported tick size {tick_size!r}")


def _quantize(value: Decimal, places: int, rounding: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=rounding)


def round_price_to_tick(price: float, tick_size: float) -> Decimal:
    """Snap ``price`` to the nearest tick, returning an exact Decimal."""
    config = rounding_config(tick_size)
    tick = Decimal(str(tick_size))
    steps = (_quantize(Decimal(str(price)) / tick, 0, ROUND_HALF_UP))
    return (_quantize(steps * tick, config[0], ROUND_HALF_UP))


def compute_order_amounts(
    side: str,
    size: float,
    price: float,
    tick_size: float,
) -> tuple[int, int]:
    """Return (makerAmount, takerAmount) in 6-decimal raw units.

    Mirrors py-clob-client ``OrderBuilder.get_order_amounts``:
    size is floored to 2 dp, price is rounded to the tick's price precision,
    and the product is carried to the tick's amount precision.

    BUY:  makerAmount = shares * price (collateral), takerAmount = shares
    SELL: makerAmount = shares,                    takerAmount = shares * price
    """
    price_dp, size_dp, amount_dp = rounding_config(tick_size)
    side_norm = str(side).upper()
    if side_norm not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")

    raw_price = _quantize(Decimal(str(price)), price_dp, ROUND_HALF_UP)
    raw_size = _quantize(Decimal(str(size)), size_dp, ROUND_DOWN)
    if raw_size <= 0:
        raise ValueError(f"size {size} floors to 0 at {size_dp} dp")
    raw_product = _quantize(raw_size * raw_price, amount_dp, ROUND_DOWN)

    scale = Decimal(10**COLLATERAL_DECIMALS)
    shares_raw = int(raw_size * scale)
    product_raw = int(raw_product * scale)
    if side_norm == "BUY":
        return product_raw, shares_raw
    return shares_raw, product_raw


"""Known Polygon USDC asset addresses (USDC.e and native USDC).

Reward rows that omit ``asset_rate`` are only assumed 1:1 for these assets; any
other asset without a rate is reported as unknown instead of being summed.
"""
USDC_ASSET_ADDRESSES = frozenset({
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
})


def _as_float(value: Any) -> Optional[float]:
    """Parse a JSON number/string; None when absent or malformed."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _earnings_usd(row: dict[str, Any]) -> Optional[float]:
    """USD value of one reward row: ``earnings * asset_rate``.

    A missing rate is 1.0 only for a known USDC asset; otherwise the row cannot
    be valued and returns None (the caller excludes it from the USD sum).
    """
    earnings = _as_float(row.get("earnings"))
    if earnings is None:
        return None
    rate = _as_float(row.get("asset_rate"))
    if rate is None:
        address = str(row.get("asset_address") or "").strip().lower()
        if address not in USDC_ASSET_ADDRESSES:
            return None
        rate = 1.0
    return earnings * rate


def _summarise_earnings(
    rows: list[dict[str, Any]],
) -> tuple[float, dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Sum reward rows in USD and group them per condition.

    Returns ``(total_usd, breakdown_by_condition, unknown_rows)``.
    """
    total = 0.0
    breakdown: dict[str, dict[str, Any]] = {}
    unknown: list[dict[str, Any]] = []
    for row in rows:
        usd = _earnings_usd(row)
        if usd is None:
            unknown.append(row)
            continue
        total += usd
        key = str(row.get("condition_id") or row.get("asset_address") or "unknown")
        entry = breakdown.get(key)
        if entry is None:
            entry = {
                "condition_id": row.get("condition_id"),
                "asset_address": row.get("asset_address"),
                "earned_usd": 0.0,
                "earnings": 0.0,
            }
            breakdown[key] = entry
        entry["earned_usd"] += usd
        entry["earnings"] += _as_float(row.get("earnings")) or 0.0
    return total, breakdown, unknown


@dataclass
class ClobAuthCredentials:
    """Polymarket CLOB API Authentication Credentials.

    ``api_*`` are the L2 API key credentials (the API key is a UUID and is sent
    as the order ``owner``). ``private_key`` is the EOA signing key.
    ``address`` is the funder/proxy wallet that holds collateral (the maker);
    when empty the EOA derived from ``private_key`` acts as maker too.
    """

    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    address: str = ""
    private_key: str = ""
    # 0: EOA, 1: POLY_PROXY, 2: POLY_GNOSIS_SAFE, 3: POLY_1271 (Deposit Wallet)
    signature_type: int = 0

    def __post_init__(self) -> None:
        try:
            signature_type = int(self.signature_type)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {self.signature_type!r})"
            ) from exc
        if signature_type not in (0, 1, 2, 3):
            raise ValueError(
                f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {signature_type})"
            )
        self.signature_type = signature_type

    @classmethod
    def from_env(cls) -> ClobAuthCredentials:
        """Load credentials from environment variables or .env file."""
        env_dict: dict[str, str] = {}
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env_dict[k.strip()] = v.strip().strip('"').strip("'")

        def _lookup(*names: str) -> str:
            for name in names:
                value = os.environ.get(name) or env_dict.get(name, "")
                if value:
                    return value
            return ""

        api_key = _lookup("POLY_API_KEY", "POLYMARKET_API_KEY")
        api_secret = _lookup("POLY_API_SECRET", "POLYMARKET_SECRET")
        passphrase = _lookup("POLY_PASSPHRASE", "POLYMARKET_PASSPHRASE")
        # Funder / proxy wallet (maker). Never derived here: it is the wallet that
        # actually holds the collateral, which may be a proxy or a Gnosis Safe.
        address = _lookup("POLY_ADDRESS", "PROXY_WALLET_ADDRESS")
        private_key = _lookup("POLY_PRIVATE_KEY", "PRIVATE_KEY")
        signature_type_raw = _lookup("POLY_SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE")

        signature_type = 0
        if signature_type_raw:
            try:
                signature_type = int(signature_type_raw)
            except ValueError as exc:
                raise ValueError(
                    f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {signature_type_raw!r})"
                ) from exc

        return cls(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            address=address,
            private_key=private_key,
            signature_type=signature_type,
        )

    @property
    def is_configured(self) -> bool:
        """True if core L2 credentials are non-empty."""
        return bool(self.api_key and self.api_secret and self.passphrase)

    @property
    def has_signing_key(self) -> bool:
        """True if private key is present and is a valid 32-byte hex string."""
        if not self.private_key:
            return False
        clean = self.private_key.removeprefix("0x").removeprefix("0X")
        if len(clean) != 64:
            return False
        try:
            int(clean, 16)
            return True
        except ValueError:
            return False

    @property
    def is_fully_configured(self) -> bool:
        """True if both L2 API credentials and on-chain private signing key are present and formatted."""
        return self.is_configured and self.has_signing_key

    def validate_formatting(self) -> list[str]:
        """Validate format of API key, API secret, private key, and address.

        Returns:
            List of error messages describing any formatting issues (empty if valid).
        """
        errors = []
        if self.api_key:
            clean_key = self.api_key.strip()
            try:
                uuid.UUID(clean_key)
            except ValueError:
                errors.append(f"POLY_API_KEY does not appear to be a valid UUID format: '{self.api_key[:8]}...'")

        if self.api_secret:
            try:
                padded = self.api_secret + "=" * (-len(self.api_secret) % 4)
                base64.urlsafe_b64decode(padded)
            except Exception:
                errors.append("POLY_API_SECRET is not valid base64 encoded data.")

        if self.private_key:
            clean_pk = self.private_key.removeprefix("0x").removeprefix("0X")
            if len(clean_pk) != 64:
                errors.append(f"POLY_PRIVATE_KEY must be exactly 32 bytes (64 hex characters), got {len(clean_pk)} chars.")
            else:
                try:
                    int(clean_pk, 16)
                except ValueError:
                    errors.append("POLY_PRIVATE_KEY contains non-hexadecimal characters.")

        if self.address:
            clean_addr = self.address.removeprefix("0x").removeprefix("0X")
            if len(clean_addr) != 40:
                errors.append(f"POLY_ADDRESS must be a 20-byte Ethereum address (40 hex characters), got {len(clean_addr)} chars.")
            else:
                try:
                    int(clean_addr, 16)
                except ValueError:
                    errors.append("POLY_ADDRESS contains non-hexadecimal characters.")

        if self.signature_type not in (0, 1, 2, 3):
            errors.append(f"POLY_SIGNATURE_TYPE must be one of 0, 1, 2, 3 (got {self.signature_type}).")

        return errors


def build_clob_hmac_signature(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: Optional[Union[str, bytes]] = None,
) -> str:
    """Generate Layer-2 HMAC-SHA256 signature for Polymarket CLOB.

    Decodes URL-safe base64 secret, concatenates timestamp + method + path + body,
    and encodes the HMAC digest into URL-safe base64.
    """
    if not secret:
        return ""

    try:
        # Handle optional padding if needed
        padded_secret = secret + "=" * (-len(secret) % 4)
        base64_secret = base64.urlsafe_b64decode(padded_secret)
    except Exception:
        base64_secret = secret.encode("utf-8")

    message = f"{timestamp}{method.upper()}{request_path}"
    if body is not None:
        if isinstance(body, bytes):
            body_str = body.decode("utf-8", errors="replace")
        else:
            body_str = str(body)
        # Ensure double quotes parity across Go/Python/TS
        message += body_str.replace("'", '"')

    h = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


@dataclass
class OrderPlacementResult:
    """Outcome of submitting an order to Polymarket CLOB."""

    success: bool
    order_id: str
    status: str
    side: str
    price: float
    size: float
    raw_response: dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    signature: str = ""
    is_dry_run: bool = False


class LiveClobClient:
    """Production REST Client for Polymarket Central Limit Order Book (CLOB)."""

    def __init__(
        self,
        credentials: Optional[ClobAuthCredentials] = None,
        base_url: str = DEFAULT_CLOB_HOST,
        chain_id: int = DEFAULT_CHAIN_ID,
        verifying_contract: str = DEFAULT_CTF_EXCHANGE,
        timeout: float = 5.0,
        dry_run: bool = False,
        use_fast_cancel_pool: bool = False,
        order_version: Optional[int] = None,
    ) -> None:
        self.credentials = credentials or ClobAuthCredentials.from_env()
        self.base_url = base_url.rstrip("/")
        self.chain_id = chain_id
        self.verifying_contract = verifying_contract
        self.timeout = timeout
        self.signature_type = int(self.credentials.signature_type)

        # CLOB order version: resolved from GET /version unless injected.
        self._version_lock = threading.Lock()
        self._order_version: Optional[int] = None
        if order_version is not None:
            self._validate_order_version(order_version)
            self._order_version = int(order_version)

        # Per-token market parameter caches (filled by get_tick_size/get_neg_risk).
        self._tick_size_cache: dict[str, float] = {}
        self._neg_risk_cache: dict[str, bool] = {}

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
        self._signers: dict[str, Union[FastOrderSigner, FastEIP712SignerV2]] = {}
        self._signer_lock = threading.Lock()
        self._sign_lock = threading.Lock()

        # V1 signer kept as `self.signer` for backwards compatibility; V2 signers
        # are created on demand by version/contract.
        try:
            self.signer = self._signer_for(1, self.verifying_contract)
        except Exception as e:
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
        except Exception:
            pass

        # Thread-safe HTTP: one requests.Session per thread (audit S12).
        # The `session` property always returns this thread's session, so both the
        # client and callers (e.g. DataAPIClient) share a per-thread connection pool.
        self._session_local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: list[requests.Session] = []

        # Dedicated low-latency cancellation pool
        self._fast_cancel_pool: Optional[DedicatedCancellationPool] = None
        if use_fast_cancel_pool and not self.dry_run:
            try:
                host = self.base_url.removeprefix("https://").removeprefix("http://").split("/")[0]
                self._fast_cancel_pool = DedicatedCancellationPool(host=host, auto_warm=False)
            except Exception as e:
                logger.warning(f"Failed to initialize fast cancel pool: {e}")

        # Simulated state for dry-run tracking
        self._mock_orders: dict[str, dict[str, Any]] = {}
        self._mock_balance_usd: float = 100.0

    # ------------------------------------------------------------------
    # Identity / signing
    # ------------------------------------------------------------------
    def _derive_eoa(self) -> str:
        """Return the EOA address derived from the configured private key ('' if none)."""
        if not self.credentials.has_signing_key:
            return ""
        try:
            clean_pk = self.credentials.private_key.removeprefix("0x").removeprefix("0X")
            return private_key_to_address(bytes.fromhex(clean_pk))
        except Exception:
            return ""

    def _signer_for(
        self, order_version: int, verifying_contract: str
    ) -> Union[FastOrderSigner, FastEIP712SignerV2]:
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

    # ------------------------------------------------------------------
    # CLOB order version / market parameters
    # ------------------------------------------------------------------
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
        with self._version_lock:
            if self._order_version is None:
                self._order_version = self._fetch_order_version()
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

    def get_tick_size(self, token_id: str, *, refresh: bool = False) -> float:
        """Fetch (and cache) the market tick size from ``GET /tick-size``.

        Raises:
            ClobAPIError: on a failed call, a missing amount, or a tick this bot
                has no rounding table for.
        """
        key = str(token_id)
        if not refresh:
            cached = self._tick_size_cache.get(key)
            if cached is not None:
                return cached

        path = "/tick-size"
        payload = self._request("GET", path, params={"token_id": key}, auth=False)
        raw = None
        if isinstance(payload, dict):
            raw = payload.get("minimum_tick_size")
            if raw in (None, ""):
                raw = payload.get("tick_size")
        try:
            tick = float(raw)
        except (TypeError, ValueError) as e:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body: {payload!r}",
                status_code=200,
                path=path,
            ) from e
        try:
            rounding_config(tick)
        except ValueError as e:
            raise ClobAPIError(
                f"GET {path} returned an unsupported tick: {e}",
                status_code=200,
                path=path,
            ) from e
        self._tick_size_cache[key] = tick
        return tick

    def get_neg_risk(self, token_id: str, *, refresh: bool = False) -> bool:
        """Fetch (and cache) the negative-risk flag from ``GET /neg-risk``.

        Raises:
            ClobAPIError: on a failed call or a missing/malformed flag.
        """
        key = str(token_id)
        if not refresh and key in self._neg_risk_cache:
            return self._neg_risk_cache[key]

        path = "/neg-risk"
        payload = self._request("GET", path, params={"token_id": key}, auth=False)
        raw = payload.get("neg_risk") if isinstance(payload, dict) else None
        if isinstance(raw, bool):
            flag = raw
        elif isinstance(raw, str):
            flag = raw.strip().lower() in ("true", "1", "yes")
        elif isinstance(raw, (int, float)):
            flag = bool(raw)
        else:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body: {payload!r}",
                status_code=200,
                path=path,
            )
        self._neg_risk_cache[key] = flag
        return flag

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _get_session(self) -> requests.Session:
        """Return the calling thread's requests.Session (created lazily)."""
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
            self._session_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    @property
    def session(self) -> requests.Session:
        """This thread's HTTP session (kept for callers that pass it on)."""
        return self._get_session()

    def close(self) -> None:
        """Close every session created by this client (best effort)."""
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def _get_auth_headers(
        self,
        method: str,
        path: str,
        body: Optional[Union[str, bytes]] = None,
    ) -> dict[str, str]:
        """Construct L2 authentication headers.

        ``POLY_ADDRESS`` must be the EOA that owns the API key (the signer),
        matching py-clob-client's ``create_level_2_headers``, which always sends
        ``signer.address()``. It is never the funder/proxy wallet: that address
        is only carried by the order as ``maker``.
        """
        if not self.credentials.is_configured:
            return {}

        ts = str(int(time.time()))
        sig = build_clob_hmac_signature(
            secret=self.credentials.api_secret,
            timestamp=ts,
            method=method,
            request_path=path,
            body=body,
        )

        headers = {
            "POLY_API_KEY": self.credentials.api_key,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_PASSPHRASE": self.credentials.passphrase,
        }
        if self.signer_address:
            headers["POLY_ADDRESS"] = self.signer_address
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[str] = None,
        auth: bool = True,
    ) -> Any:
        """Execute one CLOB REST call and return its parsed JSON body.

        Raises:
            ClobAPIError: on a non-2xx response, a transport error, or a body
                that is not valid JSON. The signature is computed over the bare
                path (query parameters are not signed), matching py-clob-client's
                ``create_level_2_headers`` (see ``py_clob_client/headers/headers.py``).
        """
        method = method.upper()
        if auth and not self.credentials.is_configured:
            raise ClobAPIError(
                f"{method} {path} requires L2 API credentials",
                status_code=401,
                path=path,
            )

        headers = self._get_auth_headers(method, path, body=body) if auth else {}
        kwargs: dict[str, Any] = {"headers": headers, "timeout": self.timeout}
        if params:
            kwargs["params"] = params
        if body is not None:
            kwargs["data"] = body

        session = self._get_session()
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                resp = session.get(url, **kwargs)
            elif method == "POST":
                resp = session.post(url, **kwargs)
            elif method == "DELETE":
                resp = session.delete(url, **kwargs)
            else:
                resp = session.request(method, url, **kwargs)
        except requests.RequestException as e:
            raise ClobAPIError(
                f"{method} {path} transport error: {e}",
                status_code=0,
                path=path,
            ) from e

        text = getattr(resp, "text", "") or ""
        if not 200 <= resp.status_code < 300:
            raise ClobAPIError(
                f"{method} {path} returned HTTP {resp.status_code}: {text[:300]}",
                status_code=resp.status_code,
                path=path,
                response_text=text,
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ClobAPIError(
                f"{method} {path} returned an unparseable body: {text[:300]}",
                status_code=resp.status_code,
                path=path,
                response_text=text,
            ) from e

    def _paginate(
        self,
        path: str,
        base_params: Optional[dict[str, Any]] = None,
        *,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Follow ``next_cursor`` over a ``{limit, next_cursor, count, data}`` endpoint.

        Reference: ``GET /data/orders`` / ``GET /data/trades`` in
        https://docs.polymarket.com/api-reference/trade/get-user-orders and
        https://raw.githubusercontent.com/Polymarket/py-clob-client/main/py_clob_client/client.py
        (``get_orders`` / ``get_trades``; ``END_CURSOR = "LTE="``).
        """
        pages = max(1, int(max_pages))
        results: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor = "MA=="
        for page in range(pages):
            params = dict(base_params or {})
            params["next_cursor"] = cursor
            payload = self._request("GET", path, params=params)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ClobAPIError(
                    f"GET {path} returned an unexpected body "
                    f"(expected an object with a 'data' list)",
                    status_code=200,
                    path=path,
                )
            results.extend(item for item in payload["data"] if isinstance(item, dict))

            next_cursor = payload.get("next_cursor")
            next_cursor = "" if next_cursor is None else str(next_cursor)
            if not next_cursor or next_cursor == END_CURSOR:
                return results
            if next_cursor in seen_cursors:
                raise ClobAPIError(
                    f"GET {path} repeated pagination cursor {next_cursor!r}; "
                    f"refusing to return a partial order list",
                    status_code=200,
                    path=path,
                )
            seen_cursors.add(next_cursor)
            if page == pages - 1:
                logger.warning(
                    f"GET {path} stopped at max_pages={pages} with more pages available; "
                    f"the list is truncated"
                )
                return results
            cursor = next_cursor
        return results

    # ------------------------------------------------------------------
    # Order construction
    # ------------------------------------------------------------------
    def build_signed_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        salt: Optional[int] = None,
        expiration: int = 0,
        signature_type: Optional[int] = None,
        fee_rate_bps: int = 0,
        neg_risk: bool = False,
        order_type: str = "GTC",
        tick_size: float = DEFAULT_TICK_SIZE,
        metadata: str = ZERO_BYTES32_HEX,
        builder_code: str = ZERO_BYTES32_HEX,
        timestamp: Optional[int] = None,
        post_only: bool = False,
        order_version: Optional[int] = None,
    ) -> tuple[Union[FastOrder, OrderV2], dict[str, Any]]:
        """Create the order struct and generate an authentic EIP-712 signature.

        Order version is selected from the cached ``GET /version`` (or the
        ``order_version`` argument). Dry-run never performs that call: it uses
        the injected version, else ``DEFAULT_ORDER_VERSION``. Version 1 signs the 12-field
        ``FastOrder`` struct; version 2 signs the 11-field ``OrderV2`` struct
        (no taker/nonce/expiration/feeRateBps, adds millisecond ``timestamp``,
        ``metadata`` and ``builder``) against the V2 exchange domain.

        Amounts in 6-decimal raw units, from the per-tick rounding table:
        - BUY:  makerAmount = floor(size, 2dp) * price, takerAmount = floor(size, 2dp)
        - SELL: makerAmount = floor(size, 2dp), takerAmount = floor(size, 2dp) * price

        The order carries the real identity (audit C12): ``maker`` is the funder /
        proxy wallet, ``signer`` is the EOA that signs, ``signatureType`` comes from
        the credentials, and the submitted ``owner`` field is the API key UUID.

        For ``signature_type=3`` (POLY_1271 / Deposit Wallet) the funder *is* the
        Deposit Wallet and it is also the order ``signer``, exactly as
        py-clob-client-v2's ``OrderBuilder._v2_order_signer`` does; the EOA that
        signs stays the L2 ``POLY_ADDRESS``. POLY_1271 orders are V2-only.

        Raises:
            ValueError: for a non-numeric token id, a price outside (0, 1), a
                non-positive size, an unsupported tick / side / signature type.
            ClobAPIError: if the order version cannot be resolved or is unsupported.
        """
        side_norm = str(side).upper()
        if side_norm not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")

        token_str = str(token_id).strip()
        if not token_str.isdigit():
            raise ValueError(
                f"token_id must be a decimal integer string, got {token_id!r} "
                f"(refusing to hash a garbage token id)"
            )

        price_f = float(price)
        size_f = float(size)
        if not 0.0 < price_f < 1.0:
            raise ValueError(f"price must satisfy 0 < price < 1, got {price_f}")
        if size_f <= 0.0:
            raise ValueError(f"size must be positive, got {size_f}")

        sig_type = self.signature_type if signature_type is None else int(signature_type)
        if sig_type not in (0, 1, 2, 3):
            raise ValueError(f"signature_type must be one of 0, 1, 2, 3, got {sig_type}")

        version = self._resolve_order_version() if order_version is None else int(order_version)
        self._validate_order_version(version)
        if sig_type == 3 and version != 2:
            raise ValueError(
                "signature_type 3 (POLY_1271 Deposit Wallet) requires CLOB order version 2"
            )
        if sig_type == 3 and not self.credentials.address:
            raise ValueError(
                "signature_type 3 requires the Deposit Wallet address "
                "(POLY_ADDRESS / PROXY_WALLET_ADDRESS)"
            )

        side_int = 0 if side_norm == "BUY" else 1
        maker_amount, taker_amount = compute_order_amounts(
            side_norm, size_f, price_f, tick_size
        )
        order_salt = generate_order_salt() if salt is None else int(salt)
        contract = self._exchange_contract(version, neg_risk)
        signer = self._signer_for(version, contract)
        # POLY_1271: the Deposit Wallet verifies the signature via EIP-1271, so the
        # order `signer` is the wallet itself, not the owning EOA.
        order_signer = self.maker_address if sig_type == 3 else self.signer_address

        # Each signer reuses an internal packing buffer; serialise packing/signing.
        with self._sign_lock:
            if version == 2:
                order_ts_ms = (
                    int(timestamp) if timestamp is not None else int(time.time() * 1000)
                )
                order: Union[FastOrder, OrderV2] = OrderV2(
                    salt=order_salt,
                    maker=self.maker_address,
                    signer=order_signer,
                    tokenId=int(token_str),
                    makerAmount=maker_amount,
                    takerAmount=taker_amount,
                    timestamp=order_ts_ms,
                    side=side_int,
                    signatureType=sig_type,
                    metadata=metadata,
                    builder=builder_code,
                )
                payload = signer.build_signed_order_payload(
                    order,
                    owner=self.credentials.api_key,
                    expiration=int(expiration),
                    order_type=str(order_type or "GTC"),
                    post_only=bool(post_only),
                )
            else:
                order = FastOrder(
                    salt=order_salt,
                    maker=self.maker_address,
                    signer=self.signer_address,
                    taker=ZERO_ADDRESS,
                    tokenId=int(token_str),
                    makerAmount=maker_amount,
                    takerAmount=taker_amount,
                    expiration=int(expiration),
                    nonce=0,
                    feeRateBps=int(fee_rate_bps),
                    side=side_int,
                    signatureType=sig_type,
                )
                payload = signer.build_signed_order_payload(order)
                # CLOB expects the owner field to be the API key UUID, not an address.
                payload["owner"] = self.credentials.api_key
                payload["orderType"] = str(order_type or "GTC")
        return order, payload

    @staticmethod
    def _round_to_tick(price: float, tick_size: float) -> float:
        """Snap a price to the market tick using the supported rounding table."""
        return float(round_price_to_tick(price, tick_size))

    def _placement_failure(
        self,
        status: str,
        message: str,
        side: str,
        price: float,
        size: float,
        *,
        raw: Optional[dict[str, Any]] = None,
        signature: str = "",
    ) -> OrderPlacementResult:
        return OrderPlacementResult(
            success=False,
            order_id="",
            status=status,
            side=str(side).upper(),
            price=float(price) if isinstance(price, (int, float)) else 0.0,
            size=float(size) if isinstance(size, (int, float)) else 0.0,
            raw_response=raw or {},
            error_message=message,
            signature=signature,
            is_dry_run=self.dry_run,
        )

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        *,
        tick_size: Optional[float] = None,
        fee_rate_bps: int = 0,
        expiration: int = 0,
        order_type: str = "GTC",
        neg_risk: Optional[bool] = None,
        post_only: bool = False,
        metadata: str = ZERO_BYTES32_HEX,
        builder_code: str = ZERO_BYTES32_HEX,
    ) -> OrderPlacementResult:
        """Sign and place a limit order on Polymarket CLOB.

        Validation (audit C12/C13/S17):
            - ``token_id`` must be all digits; the old md5 fallback is gone.
            - ``0 < price < 1`` and ``size > 0``; price is snapped to ``tick_size``
              and size is floored to 2 dp per the per-tick rounding table.
            - ``tick_size``/``neg_risk`` default to ``GET /tick-size`` and
              ``GET /neg-risk`` for the token (cached), so live orders never use a
              guessed market parameter. Dry-run uses 0.01 / False offline.
            - The order version comes from ``GET /version``; an
              ``order_version_mismatch`` rejection refetches the version once and
              retries the order exactly once.

        In dry-run mode, computes the authentic EIP-712 signature and records
        the order locally without sending real funds to CLOB.
        """
        try:
            token_str = str(token_id).strip()
            if not token_str.isdigit():
                return self._placement_failure(
                    "REJECTED",
                    f"token_id must be a decimal integer string, got {token_id!r}",
                    side,
                    price,
                    size,
                )

            side_norm = str(side).upper()
            if side_norm not in ("BUY", "SELL"):
                return self._placement_failure(
                    "REJECTED", f"side must be 'BUY' or 'SELL', got {side!r}", side, price, size
                )

            price_f = float(price)
            size_f = float(size)
            if not 0.0 < price_f < 1.0:
                return self._placement_failure(
                    "REJECTED", f"price must satisfy 0 < price < 1, got {price_f}", side, price, size
                )
            if size_f <= 0.0:
                return self._placement_failure(
                    "REJECTED", f"size must be positive, got {size_f}", side, price, size
                )

            if tick_size is None:
                # Dry-run is fully offline: never call GET /tick-size for it.
                # Live mode resolves the real market tick (cached per token).
                market_tick = self.get_tick_size(token_str) if not self.dry_run else DEFAULT_TICK_SIZE
            else:
                market_tick = float(tick_size)
            if market_tick <= 0:
                return self._placement_failure(
                    "REJECTED", f"tick_size must be positive, got {market_tick}", side, price, size
                )

            if neg_risk is None:
                # Same rule: offline dry-run assumes a non-negative-risk market,
                # live mode asks GET /neg-risk before choosing the exchange.
                neg_risk_flag = self.get_neg_risk(token_str) if not self.dry_run else False
            else:
                neg_risk_flag = bool(neg_risk)

            rounded_price = self._round_to_tick(price_f, market_tick)
            if abs(rounded_price - price_f) > _TICK_TOLERANCE:
                logger.debug(
                    f"price {price_f} snapped to tick {market_tick}: {rounded_price}"
                )
            if not 0.0 < rounded_price < 1.0:
                return self._placement_failure(
                    "REJECTED",
                    f"price {price_f} snaps to {rounded_price}, outside (0, 1) at tick {market_tick}",
                    side,
                    price,
                    size,
                )
            # Size is floored to 2 dp by the rounding table; mirror that in the result.
            rounded_size = float(_quantize(Decimal(str(size_f)), 2, ROUND_DOWN))
            if rounded_size <= 0.0:
                return self._placement_failure(
                    "REJECTED",
                    f"size {size_f} rounds to {rounded_size} at 2 dp",
                    side,
                    price,
                    size,
                )

            order_version = self._resolve_order_version()
            fast_order, payload = self.build_signed_order(
                token_id=token_str,
                side=side_norm,
                price=rounded_price,
                size=rounded_size,
                expiration=int(expiration),
                fee_rate_bps=int(fee_rate_bps),
                neg_risk=neg_risk_flag,
                order_type=order_type,
                tick_size=market_tick,
                metadata=metadata,
                builder_code=builder_code,
                post_only=post_only,
                order_version=order_version,
            )
            sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
        except ClobAPIError as e:
            logger.error(f"CLOB order setup failed: {e}")
            return self._placement_failure("REJECTED", str(e), side, price, size)
        except ValueError as e:
            logger.error(f"Invalid order parameters: {e}")
            return self._placement_failure("REJECTED", str(e), side, price, size)
        except Exception as e:
            logger.error(f"Failed to generate signed order payload: {e}")
            return self._placement_failure("ERROR", str(e), side, price, size)

        if self.dry_run:
            mock_id = f"mock_ord_{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[DRY-RUN] Simulated CLOB POST /order | ID: {mock_id} | {side_norm} {rounded_size:.2f} @ ${rounded_price:.3f} | "
                f"v{order_version} | Sig: {sig[:14]}..."
            )
            self._mock_orders[mock_id] = {
                "orderID": mock_id,
                "id": mock_id,
                "token_id": token_str,
                "side": side_norm,
                "price": rounded_price,
                "size": rounded_size,
                "timestamp": time.time(),
                "status": "LIVE",
                "order_version": order_version,
            }
            return OrderPlacementResult(
                success=True,
                order_id=mock_id,
                status="LIVE",
                side=side_norm,
                price=rounded_price,
                size=rounded_size,
                raw_response={"orderID": mock_id, "status": "LIVE", "mock": True, "order_version": order_version},
                signature=sig,
                is_dry_run=True,
            )

        path = "/order"
        retried_version = False
        while True:
            body_json = json.dumps(payload)
            try:
                data = self._request("POST", path, body=body_json)
            except ClobAPIError as e:
                if not retried_version and _is_order_version_mismatch(str(e)):
                    retried_version = True
                    refreshed = self._retry_with_refreshed_version(
                        token_id=token_str,
                        side=side_norm,
                        price=rounded_price,
                        size=rounded_size,
                        tick_size=market_tick,
                        neg_risk=neg_risk_flag,
                        fee_rate_bps=int(fee_rate_bps),
                        expiration=int(expiration),
                        order_type=order_type,
                        metadata=metadata,
                        builder_code=builder_code,
                        post_only=post_only,
                    )
                    if isinstance(refreshed, OrderPlacementResult):
                        return refreshed
                    order_version, fast_order, payload = refreshed
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                    continue
                status = "NETWORK_ERROR" if e.status_code == 0 else "REJECTED"
                logger.error(f"CLOB order placement failed: {e}")
                return self._placement_failure(status, str(e), side, price, size, signature=sig)

            if not isinstance(data, dict):
                return self._placement_failure(
                    "REJECTED",
                    f"POST /order returned an unexpected body: {data!r}",
                    side,
                    price,
                    size,
                    signature=sig,
                )

            if not data.get("success", True):
                err = data.get("errorMsg") or data.get("error") or "order rejected"
                if not retried_version and _is_order_version_mismatch(f"{err} {data}"):
                    retried_version = True
                    refreshed = self._retry_with_refreshed_version(
                        token_id=token_str,
                        side=side_norm,
                        price=rounded_price,
                        size=rounded_size,
                        tick_size=market_tick,
                        neg_risk=neg_risk_flag,
                        fee_rate_bps=int(fee_rate_bps),
                        expiration=int(expiration),
                        order_type=order_type,
                        metadata=metadata,
                        builder_code=builder_code,
                        post_only=post_only,
                    )
                    if isinstance(refreshed, OrderPlacementResult):
                        return refreshed
                    order_version, fast_order, payload = refreshed
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                    continue
                logger.error(f"CLOB order placement rejected: {err}")
                return self._placement_failure(
                    "REJECTED", str(err), side, price, size, raw=data, signature=sig
                )

            order_id = str(data.get("orderID") or data.get("id") or "")
            logger.info(
                f"Live CLOB order placed successfully: ID={order_id} "
                f"(v{order_version} {side_norm} {rounded_size} @ ${rounded_price})"
            )
            return OrderPlacementResult(
                success=True,
                order_id=order_id,
                status="LIVE",
                side=side_norm,
                price=rounded_price,
                size=rounded_size,
                raw_response=data,
                signature=sig,
                is_dry_run=False,
            )

    def _retry_with_refreshed_version(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        tick_size: float,
        neg_risk: bool,
        fee_rate_bps: int,
        expiration: int,
        order_type: str,
        metadata: str,
        builder_code: str,
        post_only: bool,
    ) -> Union[tuple[int, Union[FastOrder, OrderV2], dict[str, Any]], OrderPlacementResult]:
        """Refetch ``GET /version`` once and rebuild the order for it."""
        try:
            version = self.refresh_order_version()
            order, payload = self.build_signed_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                expiration=expiration,
                fee_rate_bps=fee_rate_bps,
                neg_risk=neg_risk,
                order_type=order_type,
                tick_size=tick_size,
                metadata=metadata,
                builder_code=builder_code,
                post_only=post_only,
                order_version=version,
            )
        except ClobAPIError as e:
            logger.error(f"Order version refresh failed: {e}")
            return self._placement_failure("REJECTED", str(e), side, price, size)
        logger.warning(f"order_version_mismatch: retrying the order with CLOB version {version}")
        return version, order, payload

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an individual resting order by order ID.

        Returns False on any failure (empty id, transport error, non-2xx).
        """
        if not order_id:
            return False

        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /order | Cancelled order {order_id}")
            self._mock_orders.pop(order_id, None)
            return True

        path = "/order"
        payload = json.dumps({"orderID": order_id})
        if self._fast_cancel_pool is not None:
            headers = self._get_auth_headers("DELETE", path, body=payload)
            res = self._fast_cancel_pool.cancel_order(order_id, headers=headers)
            return res.success

        try:
            self._request("DELETE", path, body=payload)
            return True
        except ClobAPIError as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all resting orders for the authenticated account.

        Returns False on any failure (transport error or non-2xx).
        """
        if self.dry_run:
            count = len(self._mock_orders)
            self._mock_orders.clear()
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /cancel-all | Cleared {count} resting orders.")
            return True

        try:
            self._request("DELETE", "/cancel-all")
            return True
        except ClobAPIError as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_open_orders(
        self,
        market: Optional[str] = None,
        *,
        order_id: Optional[str] = None,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Query user orders from ``GET /data/orders`` (audit C3/M1).

        Reference: https://docs.polymarket.com/api-reference/trade/get-user-orders
        Response is ``{limit, next_cursor, count, data}``; ``next_cursor`` is
        followed until empty/``LTE=``. ``market`` filters by condition id and
        ``order_id`` returns that single order regardless of status.

        Raises:
            ClobAPIError: on any failed call, unexpected body, or a repeated
                pagination cursor. Never returns ``[]`` for a failed call.
        """
        if self.dry_run:
            orders = list(self._mock_orders.values())
            if order_id:
                return [o for o in orders if str(o.get("id") or o.get("orderID") or "") == order_id]
            if market:
                return [o for o in orders if o.get("market") in (None, market)]
            return orders

        if order_id:
            params: dict[str, Any] = {"id": order_id}
            pages = 1
        else:
            params = {"market": market} if market else {}
            pages = max_pages
        return self._paginate("/data/orders", params, max_pages=pages)

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """Fetch one order by id (first element of ``/data/orders?id=...``), or None.

        Raises:
            ClobAPIError: on any failed call. None means the exchange answered
                and had no such order, or ``order_id`` was empty.
        """
        if not order_id:
            return None
        if self.dry_run:
            return self._mock_orders.get(order_id)
        orders = self.get_open_orders(order_id=order_id, max_pages=1)
        return orders[0] if orders else None

    def get_trades(
        self,
        market: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query the authenticated user's trades from ``GET /data/trades``.

        Reference: https://docs.polymarket.com/api-reference/trade/get-trades
        Required query parameter ``maker_address`` is sent as ``self.maker_address``;
        response is ``{limit, next_cursor, count, data}`` and ``next_cursor`` is
        followed until ``LTE=``.

        Raises:
            ClobAPIError: on any failed call or unexpected body.
        """
        if self.dry_run:
            return []

        params: dict[str, Any] = {
            "maker_address": self.maker_address,
            "limit": int(limit),
        }
        if market:
            params["market"] = market
        return self._paginate("/data/trades", params, max_pages=20)

    def _get_balance_allowance(self) -> dict[str, Any]:
        """GET /balance-allowance for COLLATERAL at our signature type (audit C4)."""
        path = "/balance-allowance"
        params = {
            "asset_type": "COLLATERAL",
            "signature_type": self.signature_type,
        }
        payload = self._request("GET", path, params=params)
        if not isinstance(payload, dict) or "balance" not in payload:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body (missing 'balance')",
                status_code=200,
                path=path,
            )
        if "allowance" not in payload and "allowances" not in payload:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body (missing 'allowance')",
                status_code=200,
                path=path,
            )
        return payload

    def get_balance(self) -> dict[str, Any]:
        """Query the CLOB collateral balance for the authenticated account.

        Returns ``{"available", "locked", "allowance", "raw"}``. ``available`` is
        the decimal USDC balance; ``locked`` is the collateral reserved by resting
        BUY orders (not reported by this endpoint yet, always 0.0); ``allowance``
        is the approved collateral allowance.

        Raises:
            ClobAPIError: on any failed call or unparseable amount.
        """
        if self.dry_run:
            return {
                "available": self._mock_balance_usd,
                "locked": 0.0,
                "allowance": self._mock_balance_usd,
                "raw": {"mock": True},
            }

        payload = self._get_balance_allowance()
        try:
            available = _parse_collateral_amount(payload["balance"])
            allowance = _allowance_from_payload(payload)
        except (TypeError, ValueError) as e:
            raise ClobAPIError(
                f"GET /balance-allowance returned unparseable amounts: {e}",
                status_code=200,
                path="/balance-allowance",
            ) from e
        return {
            "available": available,
            "locked": 0.0,
            "allowance": allowance,
            "raw": payload,
        }

    def get_allowances(self) -> dict[str, Any]:
        """Query collateral approvals from ``/balance-allowance?asset_type=COLLATERAL``.

        Returns ``{"approved", "collateral", "conditional", "raw"}``; ``approved``
        is ``collateral > 0`` for the signature type in use. ``conditional`` is not
        reported by this call (always 0.0).

        Raises:
            ClobAPIError: if the call fails or the body cannot be parsed. A missing
                key never becomes ``approved=False``.
        """
        if self.dry_run:
            return {
                "approved": True,
                "collateral": 1_000_000.0,
                "conditional": 0.0,
                "raw": {"mock": True},
            }

        payload = self._get_balance_allowance()
        try:
            collateral = _allowance_from_payload(payload)
        except (TypeError, ValueError) as e:
            raise ClobAPIError(
                f"GET /balance-allowance returned an unparseable allowance: {e}",
                status_code=200,
                path="/balance-allowance",
            ) from e
        return {
            "approved": collateral > 0.0,
            "collateral": collateral,
            "conditional": 0.0,
            "raw": payload,
        }

    def get_market(self, condition_id: str) -> dict[str, Any]:
        """Fetch market parameters from ``GET /markets/{condition_id}``.

        Returns ``{"neg_risk", "tick_size", "fee_rate_bps", "closed",
        "accepting_orders", "end_date_iso", "raw"}``.

        Raises:
            ClobAPIError: on any failed call or non-object body.
            ValueError: for an empty condition id.
        """
        if not condition_id:
            raise ValueError("condition_id is required")

        path = f"/markets/{condition_id}"
        payload = self._request("GET", path, auth=False)
        if not isinstance(payload, dict):
            raise ClobAPIError(
                f"GET {path} returned a non-object body",
                status_code=200,
                path=path,
            )

        tick_raw = payload.get("minimum_tick_size")
        if tick_raw in (None, ""):
            tick_raw = payload.get("tick_size")
        try:
            tick_size = float(tick_raw) if tick_raw not in (None, "") else DEFAULT_TICK_SIZE
        except (TypeError, ValueError):
            tick_size = DEFAULT_TICK_SIZE
        if tick_size <= 0.0:
            tick_size = DEFAULT_TICK_SIZE

        fee_raw = payload.get("fee_rate_bps")
        if fee_raw is None:
            fee_raw = payload.get("taker_base_fee")
        try:
            fee_rate_bps = int(float(fee_raw)) if fee_raw not in (None, "") else 0
        except (TypeError, ValueError):
            fee_rate_bps = 0

        end_date_iso = payload.get("end_date_iso")
        return {
            "neg_risk": bool(payload.get("neg_risk", False)),
            "tick_size": tick_size,
            "fee_rate_bps": fee_rate_bps,
            "closed": bool(payload.get("closed", False)),
            "accepting_orders": bool(payload.get("accepting_orders", False)),
            "end_date_iso": str(end_date_iso) if end_date_iso else None,
            "raw": payload,
        }

    # ------------------------------------------------------------------
    # Rewards / scoring telemetry
    # ------------------------------------------------------------------
    def get_total_rewards(self) -> dict[str, Any]:
        """Query the account's **today** reward earnings from CLOB.

        ``GET /rewards/user/total`` is a per-day endpoint (``date`` required,
        ``signature_type`` and ``maker_address`` for proxy accounts) and returns
        a JSON array of ``{date, asset_address, maker_address, earnings,
        native_earnings?, sponsored_earnings?, asset_rate}``. The headline
        values are today's USD total and are flagged ``source="clob_api_daily"``
        so nobody mistakes them for lifetime earnings.

        On failure it falls back to Data API v2 lifetime stats
        (``source="polymarket_data_api"``); if that also fails it returns zeros
        with ``source="unavailable"`` and an ``error`` string.
        """
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.dry_run or not self.maker_address:
            return {
                "total_rewards_usd": 0.0,
                "reward_income_usd": 0.0,
                "maker_rebates_usd": 0.0,
                "yield_income_usd": 0.0,
                "payout_count": 0,
                "source": "simulated",
                "recent_payouts": [],
            }

        errors: list[str] = []

        # 1. CLOB per-day totals (reference:
        # https://docs.polymarket.com/api-reference/rewards/get-total-earnings-for-user-by-date)
        if self.credentials.is_configured:
            path = "/rewards/user/total"
            try:
                payload = self._request(
                    "GET",
                    path,
                    params={
                        "date": target_date,
                        "signature_type": self.signature_type,
                        "maker_address": self.maker_address,
                    },
                )
                if not isinstance(payload, list):
                    raise ClobAPIError(
                        f"GET {path} returned {type(payload).__name__}, expected a JSON array",
                        status_code=200,
                        path=path,
                    )
                rows = [row for row in payload if isinstance(row, dict)]
                total_usd, _, unknown = _summarise_earnings(rows)
                return {
                    "total_rewards_usd": total_usd,
                    "reward_income_usd": total_usd,
                    # Not reported by this endpoint; only the day's total is.
                    "maker_rebates_usd": 0.0,
                    "yield_income_usd": 0.0,
                    "payout_count": len(rows),
                    "date": target_date,
                    "source": "clob_api_daily",
                    "asset_earnings": [
                        {
                            "asset_address": row.get("asset_address"),
                            "earnings": _as_float(row.get("earnings")),
                            "asset_rate": _as_float(row.get("asset_rate")),
                            "usd": _earnings_usd(row),
                        }
                        for row in rows
                    ],
                    "unknown_earnings": unknown,
                    "recent_payouts": [],
                }
            except Exception as e:
                errors.append(str(e))
                logger.debug(f"CLOB /rewards/user/total query: {e}")

        # 2. Data API v2 lifetime fallback (NOT today; identified by `source`).
        try:
            data_client = DataAPIClient(session=self.session)
            stats = data_client.user_stats(self.maker_address)
            all_time = stats.get("all_time_pnl", {}) if isinstance(stats, dict) else {}
            reward_inc = float(all_time.get("reward_income", 0.0) or 0.0)
            maker_reb = float(all_time.get("maker_rebate", 0.0) or 0.0)
            yield_inc = float(all_time.get("yield_income", 0.0) or 0.0)

            payouts = []
            try:
                act = data_client.activity(
                    user=self.maker_address,
                    types=["REWARD", "MAKER_REBATE"],
                    max_pages=1,
                )
                payouts = act[:20] if act else []
            except Exception as e:
                errors.append(f"activity: {e}")

            return {
                "total_rewards_usd": reward_inc + maker_reb,
                "reward_income_usd": reward_inc,
                "maker_rebates_usd": maker_reb,
                "yield_income_usd": yield_inc,
                "payout_count": len(payouts),
                "source": "polymarket_data_api",
                "recent_payouts": payouts,
            }
        except Exception as e:
            errors.append(str(e))
            logger.warning(f"Failed to fetch authentic reward balance from Data API: {e}")

        return {
            "total_rewards_usd": 0.0,
            "reward_income_usd": 0.0,
            "maker_rebates_usd": 0.0,
            "yield_income_usd": 0.0,
            "payout_count": 0,
            "source": "unavailable",
            "recent_payouts": [],
            "error": "; ".join(errors) if errors else "rewards unavailable",
        }

    def get_daily_rewards(self, date: Optional[str] = None) -> dict[str, Any]:
        """Query one day's reward earnings from ``GET /rewards/user``.

        The endpoint is paginated (``{limit, count, next_cursor, data}`` with
        ``next_cursor == "LTE="`` on the last page) and returns per-market rows
        ``{date, condition_id, asset_address, maker_address, earnings,
        asset_rate}``. USD value per row is ``earnings * asset_rate``; a missing
        rate is treated as 1.0 only for a known USDC asset, otherwise the row is
        excluded from the USD sum and reported in ``unknown_earnings``.

        Raises nothing: on failure it returns zeros with ``source="unavailable"``
        and an ``error`` string (C2-style: never a fabricated zero marked valid).
        """
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.dry_run or not self.maker_address:
            return {
                "date": target_date,
                "daily_rewards_usd": 0.0,
                "daily_rebates_usd": 0.0,
                "daily_combined_usd": 0.0,
                "market_breakdown": [],
                "source": "simulated",
            }

        path = "/rewards/user"
        try:
            rows = self._paginate(
                path,
                {
                    "date": target_date,
                    "signature_type": self.signature_type,
                    "maker_address": self.maker_address,
                },
                max_pages=50,
            )
        except ClobAPIError as e:
            logger.warning(f"CLOB /rewards/user query failed: {e}")
            return {
                "date": target_date,
                "daily_rewards_usd": 0.0,
                "daily_rebates_usd": 0.0,
                "daily_combined_usd": 0.0,
                "market_breakdown": [],
                "source": "unavailable",
                "error": str(e),
            }

        total_usd, breakdown, unknown = _summarise_earnings(rows)
        return {
            "date": target_date,
            "daily_rewards_usd": total_usd,
            "daily_rebates_usd": 0.0,
            "daily_combined_usd": total_usd,
            "market_breakdown": list(breakdown.values()),
            "source": "clob_api",
            "row_count": len(rows),
            "unknown_earnings": unknown,
        }

    def get_order_scoring(self, order_id: str) -> dict[str, Any]:
        """Query live qualifying maker reward scoring for one order.

        Queries CLOB ``GET /order-scoring?order_id=<id>``. Returns ``scoring`` as
        True/False when the exchange answered; on failure it returns
        ``scoring=None`` plus an ``error`` string (never a bare False, which would
        look like "answered: not scoring").
        """
        if not order_id:
            return {"order_id": "", "scoring": False, "dry_run": self.dry_run}

        if self.dry_run:
            is_mock = order_id in self._mock_orders
            return {
                "order_id": order_id,
                "scoring": is_mock,
                "dry_run": True,
                "raw_response": {"scoring": is_mock, "mock": True},
            }

        path = "/order-scoring"
        try:
            data = self._request("GET", path, params={"order_id": order_id})
            if not isinstance(data, dict):
                raise ClobAPIError(
                    f"GET {path} returned a non-object body",
                    status_code=200,
                    path=path,
                )
            return {
                "order_id": order_id,
                "scoring": bool(data.get("scoring", False)),
                "dry_run": False,
                "raw_response": data,
            }
        except ClobAPIError as e:
            logger.error(f"Failed to query order scoring for {order_id}: {e}")
            return {
                "order_id": order_id,
                "scoring": None,
                "dry_run": False,
                "error": str(e),
                "raw_response": {"status_code": e.status_code},
            }

    def get_market_reward_config(self, condition_id_or_market_id: str) -> Optional[dict[str, Any]]:
        """Query live official reward parameters from CLOB ``/rewards/user/markets``.

        Returns the config dict, or None when the market is absent from the
        response. A transport failure is logged and also returns None (the
        caller cannot distinguish it), so treat None as "unknown", not "no rewards".
        """
        if not condition_id_or_market_id:
            return None

        key = str(condition_id_or_market_id).lower()
        path = "/rewards/user/markets"

        try:
            data = self._request("GET", path, auth=self.credentials.is_configured)
            markets = data.get("data", []) if isinstance(data, dict) else []
            for m in markets:
                cid = str(m.get("condition_id", "")).lower()
                mid = str(m.get("market_id", "")).lower()
                if key in (cid, mid):
                    cfg = m.get("rewards_config", [{}])[0] if m.get("rewards_config") else {}
                    return {
                        "condition_id": m.get("condition_id"),
                        "market_id": m.get("market_id"),
                        "question": m.get("question"),
                        "rate_per_day": float(cfg.get("rate_per_day", 0.0) or 0.0),
                        "rewards_max_spread": float(m.get("rewards_max_spread", 0.0) or 0.0),
                        "rewards_min_size": float(m.get("rewards_min_size", 0.0) or 0.0),
                        "market_competitiveness": float(m.get("market_competitiveness", 0.0) or 0.0),
                        "earning_percentage": float(m.get("earning_percentage", 0.0) or 0.0),
                        "earnings": m.get("earnings", []),
                    }
        except Exception as e:
            logger.warning(f"Failed to fetch market reward config: {e}")

        return None
