"""Legacy Gnosis Safe (POLY_GNOSIS_SAFE / signature type 2) transaction signing.

Scope
-----
Offline EIP-712 hashing, owner-signature packing and ``execTransaction`` calldata
construction. There is no broadcast path here: callers either submit the signed
payload through :mod:`polymarketbot.trading.relayer` (``type: "SAFE"``) or hand it
to an existing settlement client.

Verified against primary sources
--------------------------------
``safe-fndn/safe-smart-account`` ``contracts/Safe.sol`` declares:

    DOMAIN_SEPARATOR_TYPEHASH =
        0x47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218
        keccak256("EIP712Domain(uint256 chainId,address verifyingContract)")
    SAFE_TX_TYPEHASH =
        0xbb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8
        keccak256("SafeTx(address to,uint256 value,bytes data,uint8 operation,
                 uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,
                 address gasToken,address refundReceiver,uint256 nonce)")

so the Safe domain carries **only** ``chainId`` and ``verifyingContract`` (no name
or version), which is why it is version-independent across Safe releases.

``checkNSignatures`` treats ``v > 30`` as an ``eth_sign`` signature: it recovers
over ``keccak256("\\x19Ethereum Signed Message:\\n32" + dataHash)``. The mirrored
Polymarket relayer example (``combos/collateral-return.md``) signs the SafeTx hash
with ``signMessage({ raw: digest })`` and shifts ``v`` by ``+4`` (``27/28 -> 31/32``),
so :func:`sign_safe_tx_hash` defaults to that ``eth_sign`` mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from polymarketbot.risk.fast_signer import (
    ecdsa_sign_digest,
    encode_address,
    keccak256,
)


POLYGON_CHAIN_ID = 137
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# keccak256("EIP712Domain(uint256 chainId,address verifyingContract)") -- Safe.sol
SAFE_DOMAIN_TYPEHASH = bytes.fromhex(
    "47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
)
# keccak256("SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 "
#          "safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,"
#          "address refundReceiver,uint256 nonce)") -- Safe.sol
SAFE_TX_TYPEHASH = bytes.fromhex(
    "bb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8"
)
SAFE_TX_TYPE_STRING = (
    "SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,"
    "uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,"
    "uint256 nonce)"
)

# execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)
EXEC_TRANSACTION_SELECTOR = "0x6a761202"
# nonce() -- Safe public storage getter
NONCE_SELECTOR = "0xaffed0e0"

OPERATION_CALL = 0
OPERATION_DELEGATECALL = 1


def _uint_word(value: Any) -> bytes:
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


def safe_domain_separator(safe_address: str, chain_id: int = POLYGON_CHAIN_ID) -> bytes:
    """EIP-712 domain separator of a Safe (chainId + verifyingContract only)."""
    return keccak256(
        SAFE_DOMAIN_TYPEHASH + _uint_word(chain_id) + encode_address(safe_address)
    )


def safe_tx_hash(
    *,
    safe: str,
    to: str,
    data: str = "0x",
    value: int = 0,
    operation: int = OPERATION_CALL,
    safe_tx_gas: int = 0,
    base_gas: int = 0,
    gas_price: int = 0,
    gas_token: str = ZERO_ADDRESS,
    refund_receiver: str = ZERO_ADDRESS,
    nonce: int = 0,
    chain_id: int = POLYGON_CHAIN_ID,
) -> bytes:
    """Compute the 32-byte SafeTx EIP-712 digest the Safe owners sign."""
    if int(operation) not in (OPERATION_CALL, OPERATION_DELEGATECALL):
        raise ValueError(f"operation must be 0 (call) or 1 (delegatecall), got {operation}")
    struct_hash = keccak256(
        SAFE_TX_TYPEHASH
        + encode_address(to)
        + _uint_word(value)
        + keccak256(_hex_bytes(data))
        + _uint_word(operation)
        + _uint_word(safe_tx_gas)
        + _uint_word(base_gas)
        + _uint_word(gas_price)
        + encode_address(gas_token)
        + encode_address(refund_receiver)
        + _uint_word(nonce)
    )
    return keccak256(b"\x19\x01" + safe_domain_separator(safe, chain_id) + struct_hash)


def pack_owner_signature(signature: str, *, eth_sign: bool = False) -> str:
    """Normalise a 65-byte owner signature into Safe's packed form.

    ``v`` is normalised to 27/28 first (0/1 are accepted from some signers).
    ``eth_sign=True`` shifts it to 31/32, which tells ``checkNSignatures`` to
    recover over the EIP-191 prefixed hash instead of the bare digest.
    """
    raw = _hex_bytes(signature)
    if len(raw) != 65:
        raise ValueError(f"owner signature must be 65 bytes, got {len(raw)}")
    v = raw[64]
    if v in (0, 1):
        v += 27
    if v not in (27, 28):
        raise ValueError(f"owner signature v must be 27/28 (or 0/1), got {raw[64]}")
    if eth_sign:
        v += 4
    return "0x" + (raw[:64] + bytes([v])).hex()


def sign_safe_tx_hash(
    private_key: str,
    tx_hash: bytes,
    *,
    eth_sign: bool = True,
) -> str:
    """Sign a SafeTx digest as an owner and return the 65-byte packed signature."""
    if len(tx_hash) != 32:
        raise ValueError(f"SafeTx hash must be 32 bytes, got {len(tx_hash)}")
    clean = str(private_key).removeprefix("0x").removeprefix("0X")
    if len(clean) != 64:
        raise ValueError("private key must be 32 bytes of hex")
    digest = tx_hash
    if eth_sign:
        digest = keccak256(b"\x19Ethereum Signed Message:\n32" + tx_hash)
    r, s, v = ecdsa_sign_digest(digest, bytes.fromhex(clean))
    signature = "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])).hex()
    return pack_owner_signature(signature, eth_sign=eth_sign)


@dataclass
class SafeExecTransaction:
    """A ready-to-sign ``execTransaction`` call for a legacy Safe wallet."""

    safe: str
    to: str
    data: str = "0x"
    value: int = 0
    operation: int = OPERATION_CALL
    safe_tx_gas: int = 0
    base_gas: int = 0
    gas_price: int = 0
    gas_token: str = ZERO_ADDRESS
    refund_receiver: str = ZERO_ADDRESS
    nonce: int = 0
    chain_id: int = POLYGON_CHAIN_ID

    def __post_init__(self) -> None:
        self.safe_tx_hash = safe_tx_hash(
            safe=self.safe,
            to=self.to,
            data=self.data,
            value=self.value,
            operation=self.operation,
            safe_tx_gas=self.safe_tx_gas,
            base_gas=self.base_gas,
            gas_price=self.gas_price,
            gas_token=self.gas_token,
            refund_receiver=self.refund_receiver,
            nonce=self.nonce,
            chain_id=self.chain_id,
        )

    def signature(self, owner_private_key: str, *, eth_sign: bool = True) -> str:
        """Owner signature over :attr:`safe_tx_hash` (eth_sign mode by default)."""
        return sign_safe_tx_hash(
            owner_private_key, self.safe_tx_hash, eth_sign=eth_sign
        )

    def exec_calldata(self, signature: str) -> str:
        """ABI-encoded ``execTransaction(...)`` calldata for the Safe itself."""
        signatures = _hex_bytes(signature)
        payload = _hex_bytes(self.data)
        head_len = 10 * 32
        data_tail = _uint_word(len(payload)) + _pad32(payload)
        signatures_tail = _uint_word(len(signatures)) + _pad32(signatures)
        head = (
            encode_address(self.to)
            + _uint_word(self.value)
            + _uint_word(head_len)  # offset of `data`
            + _uint_word(self.operation)
            + _uint_word(self.safe_tx_gas)
            + _uint_word(self.base_gas)
            + _uint_word(self.gas_price)
            + encode_address(self.gas_token)
            + encode_address(self.refund_receiver)
            + _uint_word(head_len + len(data_tail))  # offset of `signatures`
        )
        return (
            EXEC_TRANSACTION_SELECTOR
            + (head + data_tail + signatures_tail).hex()
        )

    def relayer_envelope(
        self,
        signature: str,
        *,
        owner: str,
        metadata: str = "",
    ) -> dict[str, Any]:
        """Build the ``type: "SAFE"`` envelope the Relayer accepts.

        Shape verified from the mirrored relayer example
        (``combos/collateral-return.md``).
        """
        encode_address(owner)
        return {
            "type": "SAFE",
            "from": owner,
            "to": self.to,
            "proxyWallet": self.safe,
            "value": str(int(self.value)),
            "data": self.data,
            "nonce": str(int(self.nonce)),
            "signature": signature,
            "metadata": metadata,
            "signatureParams": {
                "operation": str(int(self.operation)),
                "safeTxnGas": str(int(self.safe_tx_gas)),
                "baseGas": str(int(self.base_gas)),
                "gasPrice": str(int(self.gas_price)),
                "gasToken": self.gas_token,
                "refundReceiver": self.refund_receiver,
            },
        }


def build_exec_transaction(
    *,
    safe: str,
    to: str,
    data: str = "0x",
    value: int = 0,
    operation: int = OPERATION_CALL,
    safe_tx_gas: int = 0,
    base_gas: int = 0,
    gas_price: int = 0,
    gas_token: str = ZERO_ADDRESS,
    refund_receiver: str = ZERO_ADDRESS,
    nonce: int = 0,
    chain_id: int = POLYGON_CHAIN_ID,
) -> SafeExecTransaction:
    """Build (but do not sign or broadcast) a Safe ``execTransaction``."""
    return SafeExecTransaction(
        safe=safe,
        to=to,
        data=data,
        value=value,
        operation=operation,
        safe_tx_gas=safe_tx_gas,
        base_gas=base_gas,
        gas_price=gas_price,
        gas_token=gas_token,
        refund_receiver=refund_receiver,
        nonce=nonce,
        chain_id=chain_id,
    )


def nonce(
    safe_address: str,
    *,
    chain: Optional[Any] = None,
    block: str = "latest",
) -> int:
    """Read ``Safe.nonce()`` (read-only ``eth_call``; no transaction)."""
    if chain is None:
        from polymarketbot.chain import ChainClient

        chain = ChainClient()
    result = chain.eth_call(safe_address, NONCE_SELECTOR, block=block)
    text = str(result or "").removeprefix("0x")
    if not text:
        raise RuntimeError(
            f"eth_call nonce() on {safe_address} returned no data "
            f"(is it a deployed Safe?)"
        )
    return int(text, 16)
