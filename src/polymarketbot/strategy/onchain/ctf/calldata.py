"""ERC-1155 split / merge / redeem calldata builders.

The packing below is byte-stable: every body was moved verbatim from
``strategy/onchain/ctf.py`` and the selectors come from ``constants``.
"""

from __future__ import annotations

from typing import List, Optional

from .constants import (
    MERGE_POSITIONS_SELECTOR,
    REDEEM_POSITIONS_SELECTOR,
    SPLIT_POSITIONS_SELECTOR,
    ZERO_BYTES32,
)


class CtfCalldataMixin:
    """ABI packing for the CTF contract calls."""

    def encode_merge_positions(
        self,
        condition_id: str,
        amount_shares: float,
        index_sets: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
        parent_collection_id: str = ZERO_BYTES32,
    ) -> str:
        """ABI-encode calldata for mergePositions(collateralToken, parentCollectionId, conditionId, indexSets, amount).

        Args:
            condition_id: 32-byte hex condition identifier.
            amount_shares: Decimal shares to merge (e.g. 25.0).
            index_sets: Partition index sets ([1, 2] for binary YES/NO).
            collateral_token: Backing ERC-20 address (default: USDC.e).
            parent_collection_id: Parent collection ID (default: 32 zero bytes).

        Returns:
            Hex string starting with '0x'.
        """
        indices = index_sets or [1, 2]
        collat = (collateral_token or self.collateral_address).lower().removeprefix("0x").rjust(64, "0")
        parent = parent_collection_id.lower().removeprefix("0x").rjust(64, "0")
        cond = condition_id.lower().removeprefix("0x").rjust(64, "0")

        # Head offset to dynamic array `indexSets` is at word index 5 (5 * 32 = 160 = 0xa0)
        offset_partition = hex(160)[2:].rjust(64, "0")
        amount_raw = int(round(amount_shares * 1_000_000))
        amount_hex = hex(amount_raw)[2:].rjust(64, "0")

        # Dynamic array encoding: length followed by elements
        arr_len = hex(len(indices))[2:].rjust(64, "0")
        arr_elems = "".join(hex(idx)[2:].rjust(64, "0") for idx in indices)

        calldata = (
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
        return calldata

    def encode_split_positions(
        self,
        condition_id: str,
        amount_shares: float,
        index_sets: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
        parent_collection_id: str = ZERO_BYTES32,
    ) -> str:
        """ABI-encode calldata for splitPosition(collateralToken, parentCollectionId, conditionId, indexSets, amount).

        Args:
            condition_id: 32-byte hex condition identifier.
            amount_shares: Decimal shares to mint/split (e.g. 25.0).
            index_sets: Partition index sets ([1, 2] for binary YES/NO).
            collateral_token: Backing ERC-20 address (default: USDC.e).
            parent_collection_id: Parent collection ID (default: 32 zero bytes).

        Returns:
            Hex string starting with '0x'.
        """
        indices = index_sets or [1, 2]
        collat = (collateral_token or self.collateral_address).lower().removeprefix("0x").rjust(64, "0")
        parent = parent_collection_id.lower().removeprefix("0x").rjust(64, "0")
        cond = condition_id.lower().removeprefix("0x").rjust(64, "0")

        # Head offset to dynamic array `indexSets` is at word index 5 (5 * 32 = 160 = 0xa0)
        offset_partition = hex(160)[2:].rjust(64, "0")
        amount_raw = int(round(amount_shares * 1_000_000))
        amount_hex = hex(amount_raw)[2:].rjust(64, "0")

        arr_len = hex(len(indices))[2:].rjust(64, "0")
        arr_elems = "".join(hex(idx)[2:].rjust(64, "0") for idx in indices)

        calldata = (
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
        return calldata


    def encode_redeem_positions(
        self,
        condition_id: str,
        index_sets: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
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
        collat = (collateral_token or self.collateral_address).lower().removeprefix("0x").rjust(64, "0")
        parent = parent_collection_id.lower().removeprefix("0x").rjust(64, "0")
        cond = condition_id.lower().removeprefix("0x").rjust(64, "0")

        # Head offset to dynamic array `indexSets` is at word index 4 (4 * 32 = 128 = 0x80)
        offset_partition = hex(128)[2:].rjust(64, "0")

        # Dynamic array encoding: length followed by elements
        arr_len = hex(len(indices))[2:].rjust(64, "0")
        arr_elems = "".join(hex(idx)[2:].rjust(64, "0") for idx in indices)

        calldata = (
            "0x"
            + REDEEM_POSITIONS_SELECTOR
            + collat
            + parent
            + cond
            + offset_partition
            + arr_len
            + arr_elems
        )
        return calldata

