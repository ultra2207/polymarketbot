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
the signer EOA. The gasless call path lived in the retired ``relayer`` module.
"""


from __future__ import annotations

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
from polymarketbot.strategy.venue.clob.auth import (
    ClobAuthCredentials,
    build_clob_hmac_signature,
)
from polymarketbot.strategy.venue.clob.client import FastOrderSigner, LiveClobClient
from polymarketbot.strategy.venue.clob.common import (
    COLLATERAL_DECIMALS,
    DEFAULT_CLOB_HOST,
    DEFAULT_ORDER_VERSION,
    DEFAULT_TICK_SIZE,
    END_CURSOR,
    MOCK_TEST_PRIVATE_KEY,
    NEG_RISK_CTF_EXCHANGE,
    NEG_RISK_CTF_EXCHANGE_V2,
    SUPPORTED_ORDER_VERSIONS,
    USDC_ASSET_ADDRESSES,
    USER_AGENT,
    ZERO_ADDRESS,
    ZERO_BYTES32_HEX,
    ClobAPIError,
    OrderPlacementResult,
)
from polymarketbot.strategy.venue.clob.signing import generate_order_salt
from polymarketbot.strategy.venue.clob.ticks import (
    ROUNDING_CONFIG,
    compute_order_amounts,
    round_price_to_tick,
    rounding_config,
)

__all__ = [
    "COLLATERAL_DECIMALS",
    "DEFAULT_CHAIN_ID",
    "DEFAULT_CLOB_HOST",
    "DEFAULT_CTF_EXCHANGE",
    "DEFAULT_CTF_EXCHANGE_V2",
    "DEFAULT_ORDER_VERSION",
    "DEFAULT_TICK_SIZE",
    "END_CURSOR",
    "MOCK_TEST_PRIVATE_KEY",
    "NEG_RISK_CTF_EXCHANGE",
    "NEG_RISK_CTF_EXCHANGE_V2",
    "ROUNDING_CONFIG",
    "SUPPORTED_ORDER_VERSIONS",
    "USDC_ASSET_ADDRESSES",
    "USER_AGENT",
    "V2_NEG_RISK_CTF_EXCHANGE",
    "ZERO_ADDRESS",
    "ZERO_BYTES32_HEX",
    "ClobAPIError",
    "ClobAuthCredentials",
    "FastEIP712Signer",
    "FastEIP712SignerV2",
    "FastOrder",
    "FastOrderSigner",
    "LiveClobClient",
    "OrderPlacementResult",
    "OrderV2",
    "build_clob_hmac_signature",
    "compute_order_amounts",
    "generate_order_salt",
    "private_key_to_address",
    "round_price_to_tick",
    "rounding_config",
]
