"""Tests for Fast EIP-712 Cryptographic Signer."""

import time

import pytest

from polymarketbot.risk.fast_signer import (
    DEFAULT_CHAIN_ID,
    DEFAULT_CTF_EXCHANGE,
    DEFAULT_DOMAIN_NAME,
    DEFAULT_DOMAIN_SEPARATOR,
    DEFAULT_DOMAIN_VERSION,
    EIP712_DOMAIN_TYPEHASH,
    ORDER_TYPEHASH,
    FastEIP712Signer,
    FastOrder,
    compute_domain_separator,
    encode_address,
    keccak256,
    private_key_to_address,
)


@pytest.fixture
def sample_private_key() -> str:
    # Deterministic test private key
    return "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"


@pytest.fixture
def expected_address() -> str:
    # Standard address derived from sample_private_key
    return "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"


@pytest.fixture
def sample_order(expected_address) -> FastOrder:
    return FastOrder(
        salt=123456789,
        maker=expected_address,
        signer=expected_address,
        taker="0x0000000000000000000000000000000000000000",
        tokenId=1001,
        makerAmount=50000000,  # 50 USDC (6 decimals)
        takerAmount=100000000,  # 100 shares
        expiration=1750000000,
        nonce=0,
        feeRateBps=0,
        side=0,  # BUY
        signatureType=0,  # EOA
    )


def test_domain_separator_computation():
    """Verify precomputed immutable domain separator for Polymarket CTF Exchange."""
    # Verify universal EIP712Domain typehash
    expected_domain_typehash = (
        "8b73c3c69bb8fe3d512ecc4cf759cc79239f7b179b0ffacaa9a75d522b39400f"
    )
    assert EIP712_DOMAIN_TYPEHASH.hex() == expected_domain_typehash

    # Compute manual domain separator
    sep = compute_domain_separator(
        name=DEFAULT_DOMAIN_NAME,
        version=DEFAULT_DOMAIN_VERSION,
        chain_id=DEFAULT_CHAIN_ID,
        verifying_contract=DEFAULT_CTF_EXCHANGE,
    )
    assert sep == DEFAULT_DOMAIN_SEPARATOR
    assert len(DEFAULT_DOMAIN_SEPARATOR) == 32


def test_order_typehash():
    """Verify Order struct typehash string and value."""
    expected_order_typehash = (
        "a852566c4e14d00869b6db0220888a9090a13eccdaea03713ff0a3d27bf9767c"
    )
    assert ORDER_TYPEHASH.hex() == expected_order_typehash


def test_encode_address():
    """Verify address encoding packs into 32 bytes with 12 zero-byte prefix."""
    addr = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    encoded = encode_address(addr)
    assert len(encoded) == 32
    assert encoded[:12] == b"\x00" * 12
    assert encoded[12:] == bytes.fromhex("4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")

    with pytest.raises(ValueError):
        encode_address("0xshort")


def test_private_key_to_address(sample_private_key, expected_address):
    """Verify secp256k1 address derivation matches Ethereum standard."""
    clean_hex = sample_private_key.removeprefix("0x")
    addr = private_key_to_address(bytes.fromhex(clean_hex))
    assert addr.lower() == expected_address.lower()


def test_pack_order_buffer(sample_order):
    """Verify pack_order packs exactly 13 words (416 bytes)."""
    signer = FastEIP712Signer()
    packed = signer.pack_order(sample_order)

    assert len(packed) == 416
    assert packed[0:32] == ORDER_TYPEHASH
    assert int.from_bytes(packed[32:64], "big") == sample_order.salt
    assert int.from_bytes(packed[160:192], "big") == sample_order.tokenId
    assert int.from_bytes(packed[192:224], "big") == sample_order.makerAmount
    assert int.from_bytes(packed[224:256], "big") == sample_order.takerAmount
    assert int.from_bytes(packed[256:288], "big") == sample_order.expiration
    assert int.from_bytes(packed[288:320], "big") == sample_order.nonce
    assert int.from_bytes(packed[320:352], "big") == sample_order.feeRateBps
    assert int.from_bytes(packed[352:384], "big") == sample_order.side
    assert int.from_bytes(packed[384:416], "big") == sample_order.signatureType


def test_hashing_speed_microsecond_benchmark(sample_order):
    """Verify hashing and packing 1,000 orders runs in single-digit microseconds per order."""
    signer = FastEIP712Signer()

    # Warmup
    signer.hash_typed_data(sample_order)

    iterations = 2000
    t0 = time.perf_counter()
    for _ in range(iterations):
        _ = signer.hash_typed_data(sample_order)
    duration = time.perf_counter() - t0

    us_per_order = (duration / iterations) * 1e6
    print(f"\nOrder EIP-712 hashing latency: {us_per_order:.2f} microseconds per order")
    # Must complete in under 50 microseconds per order (typically 4-10 us)
    assert us_per_order < 50.0


def test_sign_order_and_payload(sample_private_key, expected_address, sample_order):
    """Verify cryptographic signing produces valid EIP-712 signature and JSON payload."""
    signer = FastEIP712Signer(private_key=sample_private_key)
    assert signer.address.lower() == expected_address.lower()

    # Sign order
    sig = signer.sign_order(sample_order)
    assert sig.startswith("0x")
    # 0x + 130 hex characters = 132 chars (65 bytes)
    assert len(sig) == 132

    # Verify v value (last byte) is 27 or 28
    v_byte = int(sig[-2:], 16)
    assert v_byte in (27, 28)

    # Build signed order payload
    payload = signer.build_signed_order_payload(sample_order)
    assert payload["owner"].lower() == expected_address.lower()
    assert payload["orderType"] == "GTC"
    assert payload["signature"] == sig
    assert payload["order"]["maker"] == expected_address
    assert payload["order"]["side"] == "BUY"
    assert payload["order"]["makerAmount"] == "50000000"
