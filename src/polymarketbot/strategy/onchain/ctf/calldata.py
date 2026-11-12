"""ERC-1155 split / merge / redeem calldata builders.

The packing below is byte-stable: every body was moved verbatim from
``strategy/onchain/ctf.py`` and the selectors come from ``constants``.

Two rules the packing enforces, because a hand-built ABI word is a direct
instruction to the contract:

* every word is validated (hex, exact width) instead of being ``rjust``-padded
  to 64 characters, which silently accepted a malformed condition id or a
  truncated address and broadcast garbage;
* amounts are converted with :func:`amount_to_6dp`'s ROUND_DOWN rule. Python's
  ``round`` is half-to-even and can round *up*, asking the CTF contract to move
  more base units than the holder owns — a guaranteed revert.
"""

from __future__ import annotations

from typing import Any

from polymarketbot.strategy.onchain.settlement import amount_to_6dp

from .constants import (
    MERGE_POSITIONS_SELECTOR,
    REDEEM_POSITIONS_SELECTOR,
    SPLIT_POSITIONS_SELECTOR,
    ZERO_BYTES32,
)

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _word_hex(
    value: Any, field: str, *, width: int | None = None, strict: bool = True
) -> str:
    """One ABI word from a hex string.

    ``strict`` (the live path) validates the characters and the exact width, so
    a malformed condition id or address is refused instead of being packed into
    a broadcast payload. A dry-run simulation passes ``strict=False``: nothing
    is signed or sent, and the simulated call only needs stable bytes.
    """
    if not strict:
        return str(value).lower().removeprefix("0x").rjust(64, "0")
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 - callers contract on ValueError for invalid input
            f"{field} must be a hex string, got {type(value).__name__}"
        )
    clean = value.strip()
    if clean[:2].lower() == "0x":
        clean = clean[2:]
    if not clean:
        raise ValueError(f"{field} is empty")
    if any(ch not in _HEX_DIGITS for ch in clean):
        raise ValueError(f"{field} is not valid hex: {value!r}")
    if width is not None and len(clean) != width:
        raise ValueError(
            f"{field} must be {width // 2} bytes ({width} hex chars), got {len(clean) // 2}"
        )
    if len(clean) > 64:
        raise ValueError(f"{field} exceeds one 32-byte word: {value!r}")
    return clean.rjust(64, "0")


def _word_uint(value: Any, field: str) -> str:
    """One validated ABI uint256 word."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(  # noqa: TRY004 - callers contract on ValueError for invalid input
            f"{field} must be an integer, got {type(value).__name__}"
        )
    if value < 0 or value > 2**256 - 1:
        raise ValueError(f"{field} out of uint256 range: {value}")
    return hex(value)[2:].rjust(64, "0")


class CtfCalldataMixin:
    """ABI packing for the CTF contract calls."""

    def encode_merge_positions(
        self,
        condition_id: str,
        amount_shares: float,
        index_sets: list[int] | None = None,
        collateral_token: str | None = None,
        parent_collection_id: str = ZERO_BYTES32,
    ) -> str:
        """ABI-encode calldata for mergePositions(collateralToken, parentCollectionId, conditionId, indexSets, amount).

        Args:
            condition_id: 32-byte hex condition identifier.
            amount_shares: Decimal shares to merge (e.g. 25.0), floored to 6 dp.
            index_sets: Partition index sets ([1, 2] for binary YES/NO).
            collateral_token: Backing ERC-20 address (default: USDC.e).
            parent_collection_id: Parent collection ID (default: 32 zero bytes).

        Returns:
            Hex string starting with '0x'.

        Raises:
            ValueError: for a malformed condition id, address, parent id,
                index set or amount.
        """
        indices = index_sets or [1, 2]
        strict = not bool(getattr(self, "dry_run", False))
        collat = _word_hex(
            collateral_token or self.collateral_address,
            "collateral_token",
            width=40,
            strict=strict,
        )
        parent = _word_hex(
            parent_collection_id, "parent_collection_id", width=64, strict=strict
        )
        cond = _word_hex(condition_id, "condition_id", width=64, strict=strict)

        # Head offset to dynamic array `indexSets` is at word index 5 (5 * 32 = 160 = 0xa0)
        offset_partition = hex(160)[2:].rjust(64, "0")
        amount_hex = _word_uint(amount_to_6dp(amount_shares), "amount")

        # Dynamic array encoding: length followed by elements
        arr_len = _word_uint(len(indices), "indexSets length")
        arr_elems = "".join(_word_uint(idx, "index_set") for idx in indices)

        return (
            "0x"
            + MERGE_POSITIONS_SELECTOR
            + collat
            + parent
            + cond
            + offset_partition
            + amount_hex
            + arr_len
            + arr_elems
        )

    def encode_split_positions(
        self,
        condition_id: str,
        amount_shares: float,
        index_sets: list[int] | None = None,
        collateral_token: str | None = None,
        parent_collection_id: str = ZERO_BYTES32,
    ) -> str:
        """ABI-encode calldata for splitPosition(collateralToken, parentCollectionId, conditionId, indexSets, amount).

        Args:
            condition_id: 32-byte hex condition identifier.
            amount_shares: Decimal shares to mint/split (e.g. 25.0), floored to 6 dp.
            index_sets: Partition index sets ([1, 2] for binary YES/NO).
            collateral_token: Backing ERC-20 address (default: USDC.e).
            parent_collection_id: Parent collection ID (default: 32 zero bytes).

        Returns:
            Hex string starting with '0x'.
        """
        indices = index_sets or [1, 2]
        strict = not bool(getattr(self, "dry_run", False))
        collat = _word_hex(
            collateral_token or self.collateral_address,
            "collateral_token",
            width=40,
            strict=strict,
        )
        parent = _word_hex(
            parent_collection_id, "parent_collection_id", width=64, strict=strict
        )
        cond = _word_hex(condition_id, "condition_id", width=64, strict=strict)

        # Head offset to dynamic array `indexSets` is at word index 5 (5 * 32 = 160 = 0xa0)
        offset_partition = hex(160)[2:].rjust(64, "0")
        amount_hex = _word_uint(amount_to_6dp(amount_shares), "amount")

        arr_len = _word_uint(len(indices), "indexSets length")
        arr_elems = "".join(_word_uint(idx, "index_set") for idx in indices)

        return (
            "0x"
            + SPLIT_POSITIONS_SELECTOR
            + collat
            + parent
            + cond
            + offset_partition
            + amount_hex
            + arr_len
            + arr_elems
        )

    def encode_redeem_positions(
        self,
        condition_id: str,
        index_sets: list[int] | None = None,
        collateral_token: str | None = None,
        parent_collection_id: str = ZERO_BYTES32,
    ) -> str:
        """ABI-encode calldata for redeemPositions(collateralToken, parentCollectionId, conditionId, indexSets).

        Args:
            condition_id: 32-byte hex condition identifier.
            index_sets: Partition index sets ([1, 2] for binary YES/NO).
            collateral_token: Backing ERC-20 address (default: USDC.e).
            parent_collection_id: Parent collection ID (default: 32 zero bytes).

        Returns:
            Hex string starting with '0x'.
        """
        indices = index_sets or [1, 2]
        strict = not bool(getattr(self, "dry_run", False))
        collat = _word_hex(
            collateral_token or self.collateral_address,
            "collateral_token",
            width=40,
            strict=strict,
        )
        parent = _word_hex(
            parent_collection_id, "parent_collection_id", width=64, strict=strict
        )
        cond = _word_hex(condition_id, "condition_id", width=64, strict=strict)

        # Head offset to dynamic array `indexSets` is at word index 4 (4 * 32 = 128 = 0x80)
        offset_partition = hex(128)[2:].rjust(64, "0")

        # Dynamic array encoding: length followed by elements
        arr_len = _word_uint(len(indices), "indexSets length")
        arr_elems = "".join(_word_uint(idx, "index_set") for idx in indices)

        return (
            "0x"
            + REDEEM_POSITIONS_SELECTOR
            + collat
            + parent
            + cond
            + offset_partition
            + arr_len
            + arr_elems
        )
