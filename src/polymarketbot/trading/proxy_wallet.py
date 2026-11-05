"""Legacy Polymarket Proxy Wallet (signature type 1, POLY_PROXY) support.

Scope
-----
Capability probe plus offline calldata/relay-request construction. Nothing here
broadcasts or submits.

Verified against primary sources
--------------------------------
``Polymarket/proxy-factories`` (``packages/proxy-factory/contracts``):

* ``ProxyWalletFactory.proxy(ProxyWalletLib.ProxyCall[] memory calls)``
* ``ProxyWallet.proxy(ProxyWalletLib.ProxyCall[] memory calls)`` (owner-only;
  the factory is the caller)
* ``enum CallType { INVALID, CALL, DELEGATECALL }`` and
  ``struct ProxyCall { CallType typeCode; address payable to; uint256 value;
  bytes data; }``

The mirrored relayer example (``combos/collateral-return.md``, "Proxy Wallet"
tab) documents the relay request digest::

    keccak256("rlx:" || from || to || data || relayerFee || gasPrice ||
              gasLimit || nonce || relayHub || relay)

with every numeric field as a 32-byte big-endian word, signed with
``signMessage({ raw: digest })`` -- i.e. an EIP-191 personal signature over the
digest (viem's ``signMessage`` always prefixes). The relayer supplies the
``PROXY`` nonce and the relay address via
``GET /v1/account/transactions/params?type=PROXY``.

Unsupported path
----------------
Only signature type 1 is a Proxy Wallet. Types 0/2/3 are reported as
unsupported here with a reason pointing at the correct module
(:mod:`polymarketbot.trading.safe` for 2,
:mod:`polymarketbot.trading.relayer` for 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from polymarketbot.risk.fast_signer import ecdsa_sign_digest, encode_address, keccak256

PROXY_WALLET_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_RELAY_HUB = "0xD216153c06E857cD7f72665E0aF1d7D82172F494"
PROXY_SIGNATURE_TYPE = 1

# proxy((uint8,address,uint256,bytes)[]) -- CallType, to, value, data
PROXY_CALLS_SIGNATURE = "proxy((uint8,address,uint256,bytes)[])"
PROXY_CALLS_SELECTOR = "0x34ee9791"

CALL = 1
DELEGATECALL = 2
CALL_TYPE_NAMES = {CALL: "CALL", DELEGATECALL: "DELEGATECALL"}

RELAY_DIGEST_PREFIX = b"rlx:"

UNSUPPORTED_REASON = (
    "only signature type 1 (POLY_PROXY) is a legacy Proxy Wallet; "
    "type 2 uses trading.safe and type 3 uses trading.relayer (Deposit Wallet)"
)


def _uint_word(value: int) -> bytes:
    number = int(value)
    if number < 0:
        raise ValueError(f"uint256 field must be non-negative, got {number}")
    return number.to_bytes(32, "big")


def _hex_bytes(value: str) -> bytes:
    text = str(value).removeprefix("0x").removeprefix("0X")
    if len(text) % 2:
        raise ValueError(f"hex data must have an even number of digits, got {value!r}")
    return bytes.fromhex(text)


def _pad32(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 32)


@dataclass(frozen=True)
class ProxyCall:
    """One ``ProxyWalletLib.ProxyCall``."""

    to: str
    data: str = "0x"
    value: int = 0
    type_code: int = CALL

    def __post_init__(self) -> None:
        if int(self.type_code) not in (CALL, DELEGATECALL):
            raise ValueError(
                f"type_code must be 1 (CALL) or 2 (DELEGATECALL), got {self.type_code}"
            )
        if int(self.value) < 0:
            raise ValueError(f"call value must be non-negative, got {self.value}")
        encode_address(self.to)
        if not str(self.data).startswith("0x"):
            raise ValueError(f"call data must be 0x-prefixed hex, got {self.data!r}")

    def encode(self) -> bytes:
        payload = _hex_bytes(self.data)
        return (
            _uint_word(self.type_code)
            + encode_address(self.to)
            + _uint_word(self.value)
            + _uint_word(4 * 32)  # offset of `data` inside the tuple
            + _uint_word(len(payload))
            + _pad32(payload)
        )


def encode_proxy_calls(calls: Sequence[ProxyCall]) -> str:
    """ABI-encode ``ProxyWalletFactory.proxy(ProxyCall[])`` calldata."""
    calls = list(calls)
    if not calls:
        raise ValueError("proxy calldata needs at least one call")
    encoded = [call.encode() for call in calls]
    offsets = []
    cursor = 32 * len(encoded)
    for item in encoded:
        offsets.append(cursor)
        cursor += len(item)
    array_data = (
        _uint_word(len(encoded))
        + b"".join(_uint_word(offset) for offset in offsets)
        + b"".join(encoded)
    )
    return PROXY_CALLS_SELECTOR + (_uint_word(32) + array_data).hex()


def build_relay_digest(
    *,
    from_address: str,
    to: str,
    data: str,
    relay: str,
    relayer_fee: int,
    gas_price: int,
    gas_limit: int,
    nonce: int,
    relay_hub: str = PROXY_RELAY_HUB,
) -> bytes:
    """OpenGSN relay request digest for a Proxy Wallet call (``"rlx:"`` prefix)."""
    for address in (from_address, to, relay_hub, relay):
        encode_address(address)
    return keccak256(
        RELAY_DIGEST_PREFIX
        + _hex_bytes(from_address)
        + _hex_bytes(to)
        + _hex_bytes(data)
        + _uint_word(relayer_fee)
        + _uint_word(gas_price)
        + _uint_word(gas_limit)
        + _uint_word(nonce)
        + _hex_bytes(relay_hub)
        + _hex_bytes(relay)
    )


def sign_relay_request(private_key: str, digest: bytes) -> str:
    """EIP-191 personal-sign over the relay digest, as the docs' signer does."""
    if len(digest) != 32:
        raise ValueError(f"relay digest must be 32 bytes, got {len(digest)}")
    clean = str(private_key).removeprefix("0x").removeprefix("0X")
    if len(clean) != 64:
        raise ValueError("private key must be 32 bytes of hex")
    prefixed = keccak256(b"\x19Ethereum Signed Message:\n32" + digest)
    r, s, v = ecdsa_sign_digest(prefixed, bytes.fromhex(clean))
    return "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])).hex()


@dataclass(frozen=True)
class ProxyWalletCapabilities:
    """Result of :func:`probe_proxy_wallet`."""

    signature_type: int
    supported: bool
    relay_type: str = "PROXY"
    proxy_factory: str = PROXY_WALLET_FACTORY
    proxy_function: str = PROXY_CALLS_SIGNATURE
    proxy_selector: str = PROXY_CALLS_SELECTOR
    relay_hub: str = PROXY_RELAY_HUB
    call_types: tuple[str, ...] = ("CALL", "DELEGATECALL")
    reason: str = ""

    def describe(self) -> str:
        state = "supported" if self.supported else "unsupported"
        detail = f" ({self.reason})" if self.reason else ""
        return f"POLY_PROXY type {self.signature_type}: {state}{detail}"


def probe_proxy_wallet(signature_type: int = PROXY_SIGNATURE_TYPE) -> ProxyWalletCapabilities:
    """Report whether a signature type can use the legacy Proxy Wallet relay path."""
    signature_type = int(signature_type)
    if signature_type != PROXY_SIGNATURE_TYPE:
        return ProxyWalletCapabilities(
            signature_type=signature_type, supported=False, reason=UNSUPPORTED_REASON
        )
    return ProxyWalletCapabilities(signature_type=signature_type, supported=True)


def build_proxy_calls(*calls: ProxyCall) -> str:
    """Convenience wrapper around :func:`encode_proxy_calls`."""
    return encode_proxy_calls(calls)
