"""EIP-712 order construction and signing for CLOB order versions 1 and 2."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

import orjson

from polymarketbot.risk.fast_signer import FastOrder, OrderV2
from polymarketbot.strategy.venue.clob.common import (
    DEFAULT_TICK_SIZE,
    ZERO_ADDRESS,
    ZERO_BYTES32_HEX,
)
from polymarketbot.strategy.venue.clob.ticks import (
    compute_order_amounts,
    round_price_to_tick,
)

# Order salts: random 63-bit value with a process-local counter in the low 31 bits.
# Two orders created in the same millisecond can never collide (audit S17).
_SALT_LOCK = threading.Lock()
_SALT_COUNTER = 0
_SALT_COUNTER_MASK = (1 << 31) - 1
_SALT_RANDOM_BITS = 32
_SALT_MAX = (1 << 63) - 1


def generate_order_salt() -> int:
    """Return a unique 63-bit order salt (random high bits, monotonic low bits)."""
    global _SALT_COUNTER
    with _SALT_LOCK:
        _SALT_COUNTER = (_SALT_COUNTER + 1) & _SALT_COUNTER_MASK
        counter = _SALT_COUNTER
    return ((secrets.randbits(_SALT_RANDOM_BITS) << 31) | counter) & _SALT_MAX


@dataclass(frozen=True)
class _PreparedOrder:
    """Validated, tick-rounded order request plus its resolved market metadata."""

    token_id: str
    side: str
    price: float
    size: float
    tick_size: float
    neg_risk: bool
    rounded_price: float
    rounded_size: float


@dataclass(frozen=True)
class _SignedOrder:
    """Signed payload ready for dispatch."""

    order: FastOrder | OrderV2 | None
    payload: dict[str, Any]
    raw_bytes: bytes | None
    signature: str
    order_version: int


class SignedOrderMixin:
    """Order struct construction, tick snapping and EIP-712 signing."""


    def build_signed_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        salt: int | None = None,
        expiration: int = 0,
        signature_type: int | None = None,
        fee_rate_bps: int | None = None,
        neg_risk: bool = False,
        order_type: str = "GTC",
        tick_size: float = DEFAULT_TICK_SIZE,
        metadata: str = ZERO_BYTES32_HEX,
        builder_code: str = ZERO_BYTES32_HEX,
        timestamp: int | None = None,
        post_only: bool = False,
        order_version: int | None = None,
    ) -> tuple[FastOrder | OrderV2, dict[str, Any]]:
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
        if version == 1 and fee_rate_bps is None:
            # The V1 struct carries ``feeRateBps`` in the signed payload: a
            # defaulted zero would tell the exchange this order is fee-free.
            raise ValueError(
                "fee_rate_bps is required for a V1 order (the signed struct commits "
                "to it); pass the market's own fee rate"
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
                order: FastOrder | OrderV2 = OrderV2(
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
                # ``postOnly`` is a top-level request field, not part of the
                # signed struct: without it the order is submitted as a plain
                # GTC and may cross the book as a taker.
                if post_only:
                    payload["postOnly"] = True
        return order, payload

    @staticmethod
    def _round_to_tick(price: float, tick_size: float) -> float:
        """Snap a price to the market tick using the supported rounding table."""
        return float(round_price_to_tick(price, tick_size))

    def _sign_order(
        self,
        prepared: _PreparedOrder,
        *,
        fee_rate_bps: int,
        expiration: int,
        order_type: str,
        metadata: str,
        builder_code: str,
        post_only: bool,
    ) -> _SignedOrder:
        """Sign ``prepared``, preferring an exact pre-signed cached payload."""
        order_version = self._resolve_order_version()
        order: FastOrder | OrderV2 | None = None
        payload: dict[str, Any] | None = None
        signature = ""

        if payload is None:
            order, payload = self.build_signed_order(
                token_id=prepared.token_id,
                side=prepared.side,
                price=prepared.rounded_price,
                size=prepared.rounded_size,
                expiration=int(expiration),
                fee_rate_bps=int(fee_rate_bps),
                neg_risk=prepared.neg_risk,
                order_type=order_type,
                tick_size=prepared.tick_size,
                metadata=metadata,
                builder_code=builder_code,
                post_only=post_only,
                order_version=order_version,
            )
            signature = payload.get("signature") or payload.get("order", {}).get("signature", "")

        return _SignedOrder(
            order=order,
            payload=payload,
            raw_bytes=orjson.dumps(payload),
            signature=signature,
            order_version=order_version,
        )
