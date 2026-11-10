"""Minimal RLP + EIP-1559 type-2 transaction encoding.

Moved verbatim from ``strategy/onchain/ctf.py``. These functions define the
broadcast bytes of every settlement transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from polymarketbot.risk.fast_signer import (
    _SECP256K1_N as SECP256K1_N,
    ecdsa_sign_digest,
    keccak256,
)

# ---------------------------------------------------------------------------
# Minimal RLP + EIP-1559 type-2 transaction encoding
# ---------------------------------------------------------------------------
# Polygon (chainId 137) type-2 transaction field order, per EIP-1559:
#
#   signing payload:
#       0x02 || rlp([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas,
#                     gasLimit, to, value, data, accessList])
#
#   broadcast payload:
#       0x02 || rlp([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas,
#                     gasLimit, to, value, data, accessList, yParity, r, s])
#
# `yParity` is 0/1 (EIP-1559 transactions do not carry the 27/37 offset).
# `accessList` is an empty list (0xc0) for a plain CTF call.


def _int_to_bytes(value: int) -> bytes:
    """Minimal big-endian bytes for an RLP integer (0 encodes as empty bytes)."""
    if value < 0:
        raise ValueError(f"RLP cannot encode a negative integer: {value}")
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _rlp_length_prefix(length: int, offset: int) -> bytes:
    if length <= 55:
        return bytes([offset + length])
    length_bytes = _int_to_bytes(length)
    return bytes([offset + 55 + len(length_bytes)]) + length_bytes


def _hex_to_bytes(value: str | bytes | bytearray) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    clean = value.strip()
    if clean in ("", "0x", "0X"):
        return b""
    clean = clean.removeprefix("0x").removeprefix("0X")
    if len(clean) % 2:
        clean = "0" + clean
    return bytes.fromhex(clean)


def rlp_encode(item: Any) -> bytes:
    """RLP-encode bytes, text strings, integers or (nested) lists.

    Integers are treated as minimal big-endian scalars, as required by
    Ethereum's transaction encoding. Text strings are UTF-8 encoded; callers
    with hex payloads pass decoded bytes.
    """
    if isinstance(item, bool):
        raise TypeError("RLP does not encode booleans")
    if isinstance(item, int):
        item = _int_to_bytes(item)
    if isinstance(item, str):
        item = item.encode("utf-8")
    if isinstance(item, (bytes, bytearray)):
        data = bytes(item)
        if len(data) == 1 and data[0] < 0x80:
            return data
        return _rlp_length_prefix(len(data), 0x80) + data
    if isinstance(item, (list, tuple)):
        payload = b"".join(rlp_encode(element) for element in item)
        return _rlp_length_prefix(len(payload), 0xC0) + payload
    raise TypeError(f"Cannot RLP-encode {type(item)!r}")


def _address_bytes(address: str) -> bytes:
    clean = address.strip().removeprefix("0x").removeprefix("0X")
    if len(clean) != 40:
        raise ValueError(f"Invalid Ethereum address: {address!r}")
    return bytes.fromhex(clean)


def eip1559_signing_hash(
    *,
    chain_id: int,
    nonce: int,
    max_priority_fee_per_gas: int,
    max_fee_per_gas: int,
    gas_limit: int,
    to: str,
    value: int = 0,
    data: str | bytes = b"",
    access_list: list | tuple = (),
) -> bytes:
    """keccak256(0x02 || rlp([...])) — the digest an EIP-1559 tx signs."""
    payload = rlp_encode(
        [
            chain_id,
            nonce,
            max_priority_fee_per_gas,
            max_fee_per_gas,
            gas_limit,
            _address_bytes(to),
            value,
            _hex_to_bytes(data),
            access_list,
        ]
    )
    return keccak256(b"\x02" + payload)


@dataclass
class SignedEip1559Transaction:
    """A signed type-2 transaction ready for eth_sendRawTransaction."""

    raw_transaction: str
    tx_hash: str
    signing_hash: bytes
    y_parity: int
    r: int
    s: int
    chain_id: int


def sign_eip1559_transaction(
    private_key: bytes,
    *,
    chain_id: int,
    nonce: int,
    max_priority_fee_per_gas: int,
    max_fee_per_gas: int,
    gas_limit: int,
    to: str,
    value: int = 0,
    data: str | bytes = b"",
    access_list: list | tuple = (),
) -> SignedEip1559Transaction:
    """Sign a Polygon type-2 transaction with the in-tree secp256k1 signer.

    The signed payload is the same field list as the signing payload followed by
    (yParity, r, s), prefixed with the type byte 0x02.
    """
    if len(private_key) != 32:
        raise ValueError("Private key must be 32 bytes")
    fields = [
        chain_id,
        nonce,
        max_priority_fee_per_gas,
        max_fee_per_gas,
        gas_limit,
        _address_bytes(to),
        value,
        _hex_to_bytes(data),
        access_list,
    ]
    signing_hash = keccak256(b"\x02" + rlp_encode(fields))
    r, s, v = ecdsa_sign_digest(signing_hash, private_key)
    y_parity = v - 27
    raw = b"\x02" + rlp_encode(fields + [y_parity, r, s])
    return SignedEip1559Transaction(
        raw_transaction="0x" + raw.hex(),
        tx_hash="0x" + keccak256(raw).hex(),
        signing_hash=signing_hash,
        y_parity=y_parity,
        r=r,
        s=s,
        chain_id=chain_id,
    )


def _parse_private_key(raw: str | None) -> Optional[bytes]:
    """Return validated 32-byte key material, or None when absent."""
    if not raw:
        return None
    clean = raw.strip().removeprefix("0x").removeprefix("0X")
    if len(clean) != 64:
        raise ValueError("Private key must be 32 bytes of hex")
    try:
        key = bytes.fromhex(clean)
    except ValueError as exc:
        raise ValueError(f"Private key is not valid hex: {exc}") from exc
    as_int = int.from_bytes(key, "big")
    if not 1 <= as_int < SECP256K1_N:
        raise ValueError("Private key is outside the secp256k1 scalar range")
    return key


def _hex_to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(str(value), 16)

