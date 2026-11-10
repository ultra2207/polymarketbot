"""Complete-set merge logic for Polymarket Conditional Tokens (CTF).

Copied verbatim from ``polymarketbot.strategy.reward.merger`` when that module was
deleted (its last live consumer was the archived complement quoter).  In binary
prediction markets 1 YES token + 1 NO token merge into exactly 1.00 USDC
collateral, so a two-sided maker recycles matched collateral instead of leaving
it locked in offsetting positions.

Archive code may not import the live packages, so the merge bookkeeping
(``MergeEvent``, ``CompleteSetPosition``) and the ``mergePositions`` calldata
builder live here.  Nothing invents a value: gas defaults are the measured
per-merge cost carried by the original module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


CTF_EXCHANGE_POLYGON = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC.e


@dataclass
class MergeEvent:
    """Record of a complete-set merge execution."""

    timestamp: float
    shares_merged: float
    usdc_unlocked: float
    remaining_yes: float
    remaining_no: float
    net_inventory: float
    gas_fee: float = 0.0016


@dataclass
class CompleteSetPosition:
    """Tracks token balances, cash, and automated complete-set merging."""

    shares_yes: float = 0.0
    shares_no: float = 0.0
    cost_basis_yes: float = 0.0
    cost_basis_no: float = 0.0
    usdc_cash: float = 0.0
    total_merged_shares: float = 0.0
    total_unlocked_usd: float = 0.0
    total_gas_fees: float = 0.0
    gas_fee_per_merge: float = 0.0016  # measured over 248,319 CTF merges
    auto_merge_threshold: float = 100.0  # Merge whenever min(YES, NO) >= threshold (batched)
    can_merge_fn: Optional[Callable[[float], bool]] = None  # Risk/Gas merge validation hook
    history: List[MergeEvent] = field(default_factory=list)

    @property
    def net_inventory(self) -> float:
        """Net directional exposure q in YES shares: (YES - NO)."""
        return self.shares_yes - self.shares_no

    @property
    def mergeable_shares(self) -> float:
        """Quantity of complete pairs currently eligible to merge into USDC."""
        return min(max(0.0, self.shares_yes), max(0.0, self.shares_no))

    def record_fill(
        self,
        outcome: str,
        side: str,
        price: float,
        size: float,
        timestamp: Optional[float] = None,
    ) -> Optional[MergeEvent]:
        """Record trade execution and trigger auto-merge if threshold met."""
        outcome_norm = outcome.upper()
        side_norm = side.upper()
        notional = size * price

        if outcome_norm == "YES":
            if side_norm == "BUY":
                self.shares_yes += size
                self.cost_basis_yes += notional
                self.usdc_cash -= notional
            elif side_norm == "SELL":
                if self.shares_yes >= size:
                    ratio = size / max(1e-6, self.shares_yes)
                    self.cost_basis_yes = max(0.0, self.cost_basis_yes * (1.0 - ratio))
                    self.shares_yes -= size
                    self.usdc_cash += notional
                else:
                    existing = max(0.0, self.shares_yes)
                    needed = size - existing
                    self.shares_yes = 0.0
                    self.cost_basis_yes = 0.0
                    self.shares_no += needed
                    self.cost_basis_no += needed * max(0.0, 1.0 - price)
                    self.usdc_cash += notional - (needed * 1.00)
        elif outcome_norm == "NO":
            if side_norm == "BUY":
                self.shares_no += size
                self.cost_basis_no += notional
                self.usdc_cash -= notional
            elif side_norm == "SELL":
                if self.shares_no >= size:
                    ratio = size / max(1e-6, self.shares_no)
                    self.cost_basis_no = max(0.0, self.cost_basis_no * (1.0 - ratio))
                    self.shares_no -= size
                    self.usdc_cash += notional
                else:
                    existing = max(0.0, self.shares_no)
                    needed = size - existing
                    self.shares_no = 0.0
                    self.cost_basis_no = 0.0
                    self.shares_yes += needed
                    self.cost_basis_yes += needed * max(0.0, 1.0 - price)
                    self.usdc_cash += notional - (needed * 1.00)

        # Check automated merge
        if self.mergeable_shares >= self.auto_merge_threshold:
            if self.can_merge_fn is None or self.can_merge_fn(self.mergeable_shares):
                return self.execute_merge(timestamp=timestamp)
        return None

    def execute_merge(self, timestamp: Optional[float] = None) -> MergeEvent:
        """Merge all available complete sets into USDC cash."""
        amount = self.mergeable_shares
        if amount <= 0.0:
            return MergeEvent(
                timestamp=timestamp or time.time(),
                shares_merged=0.0,
                usdc_unlocked=0.0,
                remaining_yes=self.shares_yes,
                remaining_no=self.shares_no,
                net_inventory=self.net_inventory,
                gas_fee=0.0,
            )

        # 1 YES + 1 NO = $1.00 USDC
        ratio_yes = amount / max(1e-6, self.shares_yes)
        self.cost_basis_yes = max(0.0, self.cost_basis_yes * (1.0 - ratio_yes))
        ratio_no = amount / max(1e-6, self.shares_no)
        self.cost_basis_no = max(0.0, self.cost_basis_no * (1.0 - ratio_no))

        self.shares_yes -= amount
        self.shares_no -= amount
        unlocked = amount * 1.00
        # Deduct Polygon gas fee from unlocked cash
        self.usdc_cash += unlocked - self.gas_fee_per_merge
        self.total_gas_fees += self.gas_fee_per_merge

        self.total_merged_shares += amount
        self.total_unlocked_usd += unlocked

        event = MergeEvent(
            timestamp=timestamp or time.time(),
            shares_merged=round(amount, 4),
            usdc_unlocked=round(unlocked, 4),
            remaining_yes=round(self.shares_yes, 4),
            remaining_no=round(self.shares_no, 4),
            net_inventory=round(self.net_inventory, 4),
            gas_fee=round(self.gas_fee_per_merge, 4),
        )
        self.history.append(event)
        return event


def build_ctf_merge_transaction(
    condition_id: str,
    amount_shares: float,
    collateral_address: str = USDC_POLYGON,
    ctf_exchange_address: str = CTF_EXCHANGE_POLYGON,
) -> dict[str, Any]:
    """Build transaction parameters for Polygon CTF mergePositions call.

    Solidity interface:
    mergePositions(
        IERC20 collateralToken,
        bytes32 parentCollectionId,  // 0x0 for root markets
        bytes32 conditionId,
        uint256[] partition,        // [1, 2] for binary outcomes (YES/NO)
        uint256 amount              // in micro-USDC (6 decimals)
    )
    """
    amount_units = int(amount_shares * 1_000_000)  # 6 decimals for USDC
    parent_collection_id = "0x" + "0" * 64
    partition = [1, 2]

    return {
        "to": ctf_exchange_address,
        "method": "mergePositions",
        "params": {
            "collateralToken": collateral_address,
            "parentCollectionId": parent_collection_id,
            "conditionId": condition_id,
            "partition": partition,
            "amount": amount_units,
        },
    }
