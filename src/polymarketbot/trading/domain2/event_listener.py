"""Domain 2 Event Listener for UMA and CTF Polygon Contracts.

Listens to and decodes:
1. UMA Optimistic Oracle V2 (0xee3afe347d5c74317041e2618c49534daf887c24):
   - ProposePrice: signals candidate resolution and begins 2-hour liveness window.
   - DisputePrice: emergency trigger signaling proposal challenge.
   - Settle: signals finalized payout after dispute window expiry.
2. Conditional Tokens Framework (CTF) (0x4D97DCd97eC945f40cF65F87097ACe5EA0476045):
   - ConditionResolution: confirms on-chain payout vector for settlement.
"""

from __future__ import annotations

import json
from typing import Callable, Optional, Sequence, Dict, Any, List
from Crypto.Hash import keccak

from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.types import (
    UmaProposalEvent,
    UmaDisputeEvent,
    ConditionResolutionEvent,
)

logger = get_logger("domain2_event_listener")

UMA_OO_V2 = "0xee3afe347d5c74317041e2618c49534daf887c24"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Event signatures and topic0s
TOPIC_PROPOSE_PRICE = "0x0e239305ca6e08d3b05241e244e536db62596f9da6a363dea1c79b930098e39f"
TOPIC_DISPUTE_PRICE = "0x5165909c3d1c01c5d1e121ac6f6d01dda1ba24bc9e1f975b5a375339c15be7f3"
TOPIC_CONDITION_RESOLUTION = "0xde98718b6234214aec5762e0f1792b39b41cb7505507bc9cd055f3e2f0bab2ed"


def _clean_hex(val: Any) -> str:
    """Normalize hex string."""
    if not val:
        return "0x"
    s = str(val).strip().lower()
    return s if s.startswith("0x") else "0x" + s


def _bytes32_to_address(val: str) -> str:
    """Extract 20-byte address from padded 32-byte log topic."""
    c = _clean_hex(val)[2:]
    return "0x" + c[-40:]


class Domain2EventListener:
    """Polygon event subscriber and log decoder for Domain 2."""

    def __init__(
        self,
        chain_client: Optional[Any] = None,
        on_proposal: Optional[Callable[[UmaProposalEvent], None]] = None,
        on_dispute: Optional[Callable[[UmaDisputeEvent], None]] = None,
        on_resolution: Optional[Callable[[ConditionResolutionEvent], None]] = None,
    ):
        self.chain_client = chain_client
        self.on_proposal = on_proposal
        self.on_dispute = on_dispute
        self.on_resolution = on_resolution

    def decode_log(self, log: Dict[str, Any]) -> Optional[Any]:
        """Decode a single raw Polygon event log.

        Supports RPC dictionary format or tape parquet schema.
        """
        address = _clean_hex(log.get("address", ""))
        topics = log.get("topics")
        if not topics:
            # Handle flattened schema (topic0, topic1, etc.)
            topics = [log.get(f"topic{i}") for i in range(4) if log.get(f"topic{i}") is not None]

        if not topics:
            return None

        topic0 = _clean_hex(topics[0])
        block_num = int(log.get("block_number") or log.get("blockNumber") or 0)
        tx_hash = _clean_hex(log.get("tx_hash") or log.get("transactionHash") or "")
        data_raw = log.get("data", "")

        # 1. UMA ProposePrice
        if address == UMA_OO_V2.lower() and topic0 == TOPIC_PROPOSE_PRICE:
            return self._decode_propose_price(topics, data_raw, block_num, tx_hash, log)

        # 2. UMA DisputePrice
        if address == UMA_OO_V2.lower() and topic0 == TOPIC_DISPUTE_PRICE:
            return self._decode_dispute_price(topics, data_raw, block_num, tx_hash, log)

        # 3. CTF ConditionResolution
        if address == CTF_CONTRACT.lower() and topic0 == TOPIC_CONDITION_RESOLUTION:
            return self._decode_condition_resolution(topics, data_raw, block_num, tx_hash)

        return None

    def _decode_propose_price(
        self,
        topics: Sequence[Any],
        data_hex: str,
        block_num: int,
        tx_hash: str,
        raw_log: Dict[str, Any],
    ) -> UmaProposalEvent:
        requester = _bytes32_to_address(topics[1])
        proposer = _bytes32_to_address(topics[2])
        identifier = _clean_hex(topics[3])

        # Decode data fields: timestamp, ancillaryData offset, proposedPrice, expirationTimestamp
        raw_bytes = bytes.fromhex(_clean_hex(data_hex)[2:])
        # 32-byte words
        words = [int.from_bytes(raw_bytes[i:i+32], 'big') for i in range(0, min(len(raw_bytes), 128), 32)]
        req_ts = words[0] if len(words) > 0 else 0
        
        # In OOv2: proposedPrice is signed int256
        prop_price_raw = int.from_bytes(raw_bytes[64:96], 'big', signed=True) if len(raw_bytes) >= 96 else 0
        prop_price = prop_price_raw / 1e18 if prop_price_raw != 0 else 0.0

        expiry_ts = int.from_bytes(raw_bytes[96:128], 'big') if len(raw_bytes) >= 128 else 0

        # Try condition_id from raw log if present, or identifier
        cid = str(raw_log.get("condition_id") or raw_log.get("conditionId") or identifier)

        event = UmaProposalEvent(
            condition_id=cid,
            requester=requester,
            proposer=proposer,
            identifier=identifier,
            proposed_price=prop_price,
            request_timestamp=req_ts,
            expiration_timestamp=expiry_ts,
            ancillary_data=data_hex,
            block_number=block_num,
            tx_hash=tx_hash,
            raw_event=raw_log,
        )
        if self.on_proposal:
            self.on_proposal(event)
        return event

    def _decode_dispute_price(
        self,
        topics: Sequence[Any],
        data_hex: str,
        block_num: int,
        tx_hash: str,
        raw_log: Dict[str, Any],
    ) -> UmaDisputeEvent:
        requester = _bytes32_to_address(topics[1])
        proposer = _bytes32_to_address(topics[2])
        disputer = _bytes32_to_address(topics[3])
        identifier = _clean_hex(topics[3])

        raw_bytes = bytes.fromhex(_clean_hex(data_hex)[2:])
        prop_price_raw = int.from_bytes(raw_bytes[64:96], 'big', signed=True) if len(raw_bytes) >= 96 else 0
        prop_price = prop_price_raw / 1e18 if prop_price_raw != 0 else 0.0

        cid = str(raw_log.get("condition_id") or raw_log.get("conditionId") or identifier)

        event = UmaDisputeEvent(
            condition_id=cid,
            requester=requester,
            proposer=proposer,
            disputer=disputer,
            identifier=identifier,
            proposed_price=prop_price,
            block_number=block_num,
            tx_hash=tx_hash,
            raw_event=raw_log,
        )
        if self.on_dispute:
            self.on_dispute(event)
        return event

    def _decode_condition_resolution(
        self,
        topics: Sequence[Any],
        data_hex: str,
        block_num: int,
        tx_hash: str,
    ) -> ConditionResolutionEvent:
        condition_id = _clean_hex(topics[1])
        oracle = _bytes32_to_address(topics[2]) if len(topics) > 2 else ""

        # Decode payouts from data
        raw_bytes = bytes.fromhex(_clean_hex(data_hex)[2:])
        chunks = [int.from_bytes(raw_bytes[i:i+32], 'big') for i in range(0, len(raw_bytes), 32)]

        payout_numerators: List[int] = []
        is_void = False
        winning_index = None

        if len(chunks) >= 5:
            p0 = chunks[3]
            p1 = chunks[4]
            payout_numerators = [p0, p1]
            if p0 > 0 and p1 > 0:
                is_void = True
            elif p0 > 0:
                winning_index = 0
            elif p1 > 0:
                winning_index = 1

        event = ConditionResolutionEvent(
            condition_id=condition_id,
            oracle=oracle,
            question_id=chunks[0] if len(chunks) > 0 else 0,
            outcome_slot_count=chunks[1] if len(chunks) > 1 else 2,
            payout_numerators=payout_numerators,
            winning_outcome_index=winning_index,
            is_void=is_void,
            block_number=block_num,
            tx_hash=tx_hash,
        )
        if self.on_resolution:
            self.on_resolution(event)
        return event

    def process_logs_batch(self, logs: Sequence[Dict[str, Any]]) -> List[Any]:
        """Process and dispatch an iterable batch of logs."""
        decoded = []
        for log in logs:
            event = self.decode_log(log)
            if event:
                decoded.append(event)
        return decoded
