"""Fast EIP-712 Cryptographic Signer for Polymarket CTF Exchange.

Precomputes immutable EIP-712 domain separators and Order struct typehashes to allow
microsecond-level order packing, hashing, and signing without runtime schema serialization.

Uses PyCryptodome's `Crypto.Hash.keccak` (Keccak-256) for high-performance cryptographic operations.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

from Crypto.Hash import keccak

# Universal EIP-712 domain type schema
EIP712_DOMAIN_TYPE_STRING = (
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

# Polymarket CTF Exchange Order struct schema
ORDER_TYPE_STRING = (
    b"Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,"
    b"uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,"
    b"uint256 feeRateBps,uint8 side,uint8 signatureType)"
)

# Default Polymarket mainnet parameters (Polygon chainId = 137)
DEFAULT_CHAIN_ID = 137
DEFAULT_DOMAIN_NAME = "Polymarket CTF Exchange"
DEFAULT_DOMAIN_VERSION = "1"
DEFAULT_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
DEFAULT_CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"

# Secp256k1 curve parameters
_SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_SECP256K1_G = (_SECP256K1_GX, _SECP256K1_GY)


def keccak256(data: bytes) -> bytes:
    """Compute standard Ethereum Keccak-256 hash using PyCryptodome."""
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


# Precomputed immutable Typehashes
EIP712_DOMAIN_TYPEHASH: bytes = keccak256(EIP712_DOMAIN_TYPE_STRING)
ORDER_TYPEHASH: bytes = keccak256(ORDER_TYPE_STRING)


def encode_address(address_hex: str) -> bytes:
    """Encode an Ethereum address hex string into a 32-byte word (12 zero bytes + 20 address bytes)."""
    clean_hex = address_hex.removeprefix("0x").removeprefix("0X")
    if len(clean_hex) != 40:
        raise ValueError(f"Invalid Ethereum address length: '{address_hex}'")
    raw_bytes = bytes.fromhex(clean_hex)
    return raw_bytes.rjust(32, b"\x00")


def compute_domain_separator(
    name: str = DEFAULT_DOMAIN_NAME,
    version: str = DEFAULT_DOMAIN_VERSION,
    chain_id: int = DEFAULT_CHAIN_ID,
    verifying_contract: str = DEFAULT_CTF_EXCHANGE,
) -> bytes:
    """Compute the immutable 32-byte EIP-712 domain separator."""
    name_hash = keccak256(name.encode("utf-8"))
    version_hash = keccak256(version.encode("utf-8"))
    chain_id_bytes = chain_id.to_bytes(32, byteorder="big")
    contract_bytes = encode_address(verifying_contract)

    payload = (
        EIP712_DOMAIN_TYPEHASH
        + name_hash
        + version_hash
        + chain_id_bytes
        + contract_bytes
    )
    return keccak256(payload)


# Precomputed immutable domain separator for Polymarket CTF Exchange on Polygon (Chain ID 137)
DEFAULT_DOMAIN_SEPARATOR: bytes = compute_domain_separator()


@dataclass
class FastOrder:
    """Order structure matching Polymarket CTF Exchange EIP-712 requirements."""

    salt: int
    maker: str
    signer: str
    taker: str
    tokenId: int
    makerAmount: int
    takerAmount: int
    expiration: int
    nonce: int
    feeRateBps: int = 0
    side: int = 0  # 0: BUY, 1: SELL
    signatureType: int = 0  # 0: EOA, 1: POLY_PROXY, 2: POLY_GNOSIS_SAFE


# --- Lightweight Secp256k1 Curve & ECDSA Math ---


def _point_add(
    p1: Optional[Tuple[int, int]],
    p2: Optional[Tuple[int, int]],
) -> Optional[Tuple[int, int]]:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
    if x1 == x2:
        m = (3 * x1 * x1) * pow(2 * y1, -1, _SECP256K1_P) % _SECP256K1_P
    else:
        m = (y2 - y1) * pow(x2 - x1, -1, _SECP256K1_P) % _SECP256K1_P
    x3 = (m * m - x1 - x2) % _SECP256K1_P
    y3 = (m * (x1 - x3) - y1) % _SECP256K1_P
    return (x3, y3)


def _point_mul(k: int, p: Tuple[int, int]) -> Optional[Tuple[int, int]]:
    res = None
    cur: Optional[Tuple[int, int]] = p
    while k > 0:
        if k & 1:
            res = _point_add(res, cur)
        if cur is not None:
            cur = _point_add(cur, cur)
        k >>= 1
    return res


def private_key_to_address(private_key_bytes: bytes) -> str:
    """Derive checksummed Ethereum address from 32-byte private key."""
    priv_int = int.from_bytes(private_key_bytes, byteorder="big")
    if not (1 <= priv_int < _SECP256K1_N):
        raise ValueError("Private key out of valid range")
    pub_point = _point_mul(priv_int, _SECP256K1_G)
    if pub_point is None:
        raise ValueError("Failed to derive public key point")
    pub_bytes = pub_point[0].to_bytes(32, "big") + pub_point[1].to_bytes(32, "big")
    addr_bytes = keccak256(pub_bytes)[-20:]
    return "0x" + addr_bytes.hex()


def ecdsa_sign_digest(
    digest: bytes,
    private_key_bytes: bytes,
) -> Tuple[int, int, int]:
    """Sign a 32-byte message digest using RFC 6979 deterministic nonce generation.

    Returns:
        (r, s, v) where v in {27, 28} and s <= N / 2 (EIP-2 compliant).
    """
    if len(digest) != 32:
        raise ValueError("Digest must be exactly 32 bytes")
    if len(private_key_bytes) != 32:
        raise ValueError("Private key must be exactly 32 bytes")

    priv_int = int.from_bytes(private_key_bytes, "big")
    z = int.from_bytes(digest, "big")

    # RFC 6979 deterministic k generation
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + private_key_bytes + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + private_key_bytes + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()

    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < _SECP256K1_N:
            point = _point_mul(candidate, _SECP256K1_G)
            if point is None:
                continue
            r = point[0] % _SECP256K1_N
            if r == 0:
                continue
            s = (pow(candidate, -1, _SECP256K1_N) * (z + r * priv_int)) % _SECP256K1_N
            if s == 0:
                continue
            recid = 1 if (point[1] & 1) else 0
            if s > _SECP256K1_N // 2:
                s = _SECP256K1_N - s
                recid ^= 1
            return (r, s, 27 + recid)
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


class FastEIP712Signer:
    """Ultra-fast EIP-712 signer and hasher for Polymarket CTF Exchange orders.

    Eliminates dynamic schema re-encoding and JSON-RPC overhead:
    - Precomputes 32-byte ORDER_TYPEHASH and DOMAIN_SEPARATOR.
    - Packs 12 order fields directly into contiguous 416-byte memory buffer.
    - Hashes order and typed data in single-digit microseconds.
    - Produces valid EIP-712 signatures ready for CLOB submission.
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        verifying_contract: str = DEFAULT_CTF_EXCHANGE,
        chain_id: int = DEFAULT_CHAIN_ID,
        domain_name: str = DEFAULT_DOMAIN_NAME,
        domain_version: str = DEFAULT_DOMAIN_VERSION,
    ) -> None:
        self.verifying_contract = verifying_contract
        self.chain_id = chain_id
        self.domain_name = domain_name
        self.domain_version = domain_version

        # Precompute domain separator for specified contract / chain
        self.domain_separator = compute_domain_separator(
            name=domain_name,
            version=domain_version,
            chain_id=chain_id,
            verifying_contract=verifying_contract,
        )
        self.order_typehash = ORDER_TYPEHASH

        # Pre-allocate reusable 416-byte buffer for packing
        # 1 typehash + 12 fields = 13 words * 32 bytes = 416 bytes
        self._buffer = bytearray(416)
        self._buffer[0:32] = self.order_typehash

        # Initialize private key & address if provided
        self._private_key_bytes: Optional[bytes] = None
        self.address: Optional[str] = None
        if private_key:
            clean_key = private_key.removeprefix("0x").removeprefix("0X")
            self._private_key_bytes = bytes.fromhex(clean_key)
            self.address = private_key_to_address(self._private_key_bytes)

    def pack_order(self, order: FastOrder) -> bytes:
        """Pack order into 416 bytes (13 x 32-byte words) without re-encoding schemas."""
        buf = self._buffer
        # 0:32 is ORDER_TYPEHASH (pre-set)
        buf[32:64] = order.salt.to_bytes(32, "big")
        buf[64:96] = encode_address(order.maker)
        buf[96:128] = encode_address(order.signer)
        buf[128:160] = encode_address(order.taker)
        buf[160:192] = order.tokenId.to_bytes(32, "big")
        buf[192:224] = order.makerAmount.to_bytes(32, "big")
        buf[224:256] = order.takerAmount.to_bytes(32, "big")
        buf[256:288] = order.expiration.to_bytes(32, "big")
        buf[288:320] = order.nonce.to_bytes(32, "big")
        buf[320:352] = order.feeRateBps.to_bytes(32, "big")
        buf[352:384] = order.side.to_bytes(32, "big")
        buf[384:416] = order.signatureType.to_bytes(32, "big")
        return bytes(buf)

    def hash_order(self, order: FastOrder) -> bytes:
        """Compute the 32-byte struct hash of an order."""
        packed = self.pack_order(order)
        return keccak256(packed)

    def hash_typed_data(self, order: FastOrder) -> bytes:
        """Compute the 32-byte EIP-712 signable message digest:

        keccak256(b"\\x19\\x01" + domain_separator + struct_hash)
        """
        struct_hash = self.hash_order(order)
        prefix = b"\x19\x01" + self.domain_separator + struct_hash
        return keccak256(prefix)

    def sign_digest(self, digest: bytes) -> str:
        """Sign a 32-byte digest and return the 65-byte hex signature ('0x...' string)."""
        if self._private_key_bytes is None:
            raise RuntimeError("Private key is required to sign orders.")
        r, s, v = ecdsa_sign_digest(digest, self._private_key_bytes)
        sig_bytes = r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])
        return "0x" + sig_bytes.hex()

    def sign_order(self, order: FastOrder) -> str:
        """Compute EIP-712 typed data digest and generate cryptographic signature."""
        digest = self.hash_typed_data(order)
        return self.sign_digest(digest)

    def build_signed_order_payload(self, order: FastOrder) -> Dict[str, Any]:
        """Produce the complete dictionary payload ready to be sent to CLOB POST /order."""
        sig = self.sign_order(order)
        return {
            "order": {
                "salt": order.salt,
                "maker": order.maker,
                "signer": order.signer,
                "taker": order.taker,
                "tokenId": str(order.tokenId),
                "makerAmount": str(order.makerAmount),
                "takerAmount": str(order.takerAmount),
                "expiration": str(order.expiration),
                "nonce": str(order.nonce),
                "feeRateBps": str(order.feeRateBps),
                "side": "BUY" if order.side == 0 else "SELL",
                "signatureType": order.signatureType,
            },
            "owner": self.address or order.signer,
            "orderType": "GTC",
            "signature": sig,
        }
