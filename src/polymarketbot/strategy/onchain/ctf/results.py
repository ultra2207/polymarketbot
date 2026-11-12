"""Outcome records returned by the CTF settlement client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MergeExecutionResult:
    """Outcome of on-chain CTF complete-set merge."""

    success: bool
    tx_hash: str
    condition_id: str
    complete_sets_merged: float
    usdc_unlocked: float
    gas_fee_usd: float
    gas_units_used: int
    is_dry_run: bool = False
    error: str | None = None
    # "success" | "pending" | "failed". `pending` is not a failure: the signed
    # transaction is stored and will be resolved by its hash, so the caller must
    # not retry the merge.
    status: str = "failed"


@dataclass
class RedeemExecutionResult:
    """Outcome of on-chain CTF position redemption."""

    success: bool
    tx_hash: str
    condition_id: str
    index_sets: list[int]
    gas_fee_usd: float
    gas_units_used: int
    is_dry_run: bool = False
    error: str | None = None
    status: str = "failed"


@dataclass
class SettlementOutcome:
    """Result of signing, persisting and broadcasting one settlement payload."""

    status: str
    tx_hash: str = ""
    # None means the receipt was read but POL/USD was unmeasured: the gas
    # cost is unknown, not zero.
    gas_fee_usd: float | None = 0.0
    gas_units_used: int = 0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "success"

