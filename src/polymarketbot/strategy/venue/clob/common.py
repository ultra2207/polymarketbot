"""Constants, shared result types and the API error for the CLOB client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from polymarketbot.risk.fast_signer import V2_NEG_RISK_CTF_EXCHANGE

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


# Deterministic fallback key for dry-run signature verification when no private key is supplied
MOCK_TEST_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"


"""Known Polygon USDC asset addresses (USDC.e and native USDC).

Reward rows that omit ``asset_rate`` are only assumed 1:1 for these assets; any
other asset without a rate is reported as unknown instead of being summed.
"""
USDC_ASSET_ADDRESSES = frozenset({
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
})


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


class ClobMarketParamsUnreadable(ClobAPIError):
    """A market payload omitted a parameter the order signature commits to.

    Subclasses ``ClobAPIError`` so existing callers that already degrade on a
    failed market read keep doing so, while callers that need the parameter can
    distinguish "the venue did not publish it" from a transport failure.
    """


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
    error_message: str | None = None
    signature: str = ""
    is_dry_run: bool = False
