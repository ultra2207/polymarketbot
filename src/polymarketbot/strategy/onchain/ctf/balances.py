"""On-chain CTF / ERC-20 balance reads and merge backing verification."""

from __future__ import annotations

from polymarketbot.logs import get_logger
from polymarketbot.risk.fast_signer import keccak256

from .constants import (
    CTF_POSITION_BALANCE_SELECTOR,
    CTF_SHARE_DECIMALS,
    ERC20_BALANCE_OF_SELECTOR,
    ZERO_BYTES32,
)
from .rlp import _address_bytes, _hex_to_bytes

logger = get_logger("ctf_client")


class CtfBalanceMixin:
    """Wallet and position balances the merge preflight depends on."""

    def get_pol_balance(self, address: str) -> float | None:
        """Native POL (gas) balance for ``address``, or ``None`` when unmeasured.

        A failed RPC read or a wallet this client does not know is *not* a zero
        balance: returning ``0.0`` here (at ``debug`` level) recorded a
        fabricated 0 POL in the tape and made a gas-starved wallet look funded.
        """
        if not address or address.startswith("0x00000"):
            return None

        try:
            raw_bal = self.chain_client._rpc("eth_getBalance", [address, "latest"])
        except (RuntimeError, ValueError, OSError) as e:
            logger.warning(f"Failed to query POL balance for {address}: {e}")
            return None
        if not raw_bal:
            return None
        try:
            wei = int(str(raw_bal), 16)
        except (TypeError, ValueError) as e:
            logger.warning(f"POL balance for {address} was not a hex quantity: {e}")
            return None
        return round(wei / 1e18, 4)

    def get_usdc_balance(self, address: str) -> float | None:
        """On-chain ERC-20 USDC balance for ``address``, or ``None`` when unmeasured."""
        if not address or address.startswith("0x00000"):
            return None

        try:
            clean_addr = address.lower().removeprefix("0x").rjust(64, "0")
            data = f"0x{ERC20_BALANCE_OF_SELECTOR}{clean_addr}"
            res = self.chain_client.eth_call(to=self.collateral_address, data=data)
        except (RuntimeError, ValueError, OSError) as e:
            logger.warning(f"Failed to query on-chain USDC balance for {address}: {e}")
            return None
        if not res or res == "0x":
            return None
        try:
            raw_units = int(res, 16)
        except (TypeError, ValueError) as e:
            logger.warning(f"On-chain USDC balance for {address} was not a hex quantity: {e}")
            return None
        return round(raw_units / 1_000_000.0, 2)

    # ------------------------------------------------------------------
    # CTF position balances
    # ------------------------------------------------------------------

    def derive_position_token_id(
        self,
        condition_id: str,
        index_set: int,
        parent_collection_id: str = ZERO_BYTES32,
    ) -> str:
        """Derive the ERC-1155 token id the CTF contract mints for one outcome.

        Mirrors the CTF implementation:
            collectionId = keccak256(abi.encodePacked(parentCollectionId, conditionId, indexSet))
            positionId   = keccak256(abi.encodePacked(collateralToken, collectionId))
        """
        parent = _hex_to_bytes(parent_collection_id).rjust(32, b"\x00")
        condition = _hex_to_bytes(condition_id).rjust(32, b"\x00")
        collection_id = keccak256(parent + condition + int(index_set).to_bytes(32, "big"))
        position_id = keccak256(_address_bytes(self.collateral_address) + collection_id)
        return "0x" + position_id.hex()

    def _position_balance_raw(self, token_id: str, address: str) -> int:
        """Raw 6-decimal CTF balanceOf(address,uint256). Raises on RPC failure."""
        try:
            token_int = token_id if isinstance(token_id, int) else int(str(token_id), 16)
        except ValueError as exc:
            raise ValueError(f"Invalid CTF token id: {token_id!r}") from exc
        if token_int < 0 or token_int >= 2**256:
            raise ValueError(f"CTF token id out of range: {token_id!r}")
        addr_word = _address_bytes(address).rjust(32, b"\x00").hex()
        token_word = token_int.to_bytes(32, "big").hex()
        data = f"0x{CTF_POSITION_BALANCE_SELECTOR}{addr_word}{token_word}"
        res = self.chain_client.eth_call(to=self.ctf_address, data=data)
        if not res or res == "0x":
            raise RuntimeError(f"CTF balanceOf returned no data for token {token_id}")
        return int(res, 16)

    def get_position_balance(self, token_id: str, address: str) -> float:
        """On-chain CTF share balance (6-decimal units) for one outcome token.

        Raises rather than returning a placeholder: a fabricated balance is the
        exact failure mode that lets a merge broadcast blind.
        """
        return self._position_balance_raw(token_id, address) / CTF_SHARE_DECIMALS

    def _verify_position_balances(
        self,
        condition_id: str,
        amount_shares: float,
        index_sets: list[int],
        token_ids: tuple[str, ...] | None,
    ) -> str | None:
        """Return an error string when the merge cannot be proven to be backed.

        tokenIds are not part of mergePositions calldata, so they are supplied
        by the caller when known, otherwise derived from the CTF collection
        math. Any failure to read a balance aborts: broadcasting blind is worse
        than skipping the merge.
        """
        try:
            if token_ids is not None:
                ids = [str(token_id) if isinstance(token_id, str) else token_id
                       for token_id in token_ids]
            else:
                ids = [
                    self.derive_position_token_id(condition_id, index_set)
                    for index_set in index_sets
                ]
        except Exception as exc:  # noqa: BLE001 - derivation failure must abort
            return f"CTF balance verification failed: {exc}"
        if not ids:
            return "no CTF token ids available to verify merge balances"

        required_raw = round(float(amount_shares) * CTF_SHARE_DECIMALS)
        problems: list[str] = []
        for token_id in ids:
            try:
                held_raw = self._position_balance_raw(token_id, self.signer_address)
            except Exception as exc:  # noqa: BLE001 - unreadable balance must abort
                problems.append(f"balance verification failed for token {token_id}: {exc}")
                continue
            if held_raw < required_raw:
                problems.append(
                    f"insufficient CTF balance for token {token_id}: "
                    f"{held_raw / CTF_SHARE_DECIMALS:.6f} < {amount_shares:.6f} shares"
                )
        return "; ".join(problems) if problems else None

