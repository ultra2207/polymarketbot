"""Production Polygon PoS Conditional Token Framework (CTF) Settlement Client.

Interacts with the Polymarket CTF contract on Polygon PoS:
- Contract: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
- Collateral: Bridged USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
- Function: mergePositions(collateralToken, parentCollectionId, conditionId, indexSets, amount)

Provides:
1. Dynamic Polygon PoS EIP-1559 gas fee estimation (base fee + priority fee).
2. Direct ABI calldata packing for mergePositions / redeemPositions and ERC-20 /
   ERC-1155 balanceOf without heavy web3 dependencies.
3. Native POL (gas) and USDC collateral balance auditing.
4. Real EIP-1559 transaction signing + broadcasting for EOA accounts, and an
   explicit dry-run mode. Proxy-wallet (signature_type 1/2) settlement is
   refused rather than silently simulated: the CTF contract is only callable by
   the token owner, and this client can only sign for an EOA.

Package layout (every body was moved verbatim from the former ``ctf.py``):

- ``rlp``        RLP + EIP-1559 type-2 transaction encoding.
- ``gas``        EIP-1559 gas pricing, POL/USD and merge-cost sizing.
- ``measure``    Measured block cadence and RPC round-trip probes.
- ``calldata``   ERC-1155 split / merge / redeem calldata builders.
- ``balances``   Wallet/position balance reads and merge preflight.
- ``fees``       Gas estimates and the signed fee caps.
- ``receipts``   Receipt polling and settlement finalisation.
- ``submission`` Sign, persist and broadcast one settlement payload.
- ``merge`` / ``redeem``  Public settlement entry points.
- ``client``     ``PolygonCtfClient``: identity and mode gating.
"""

# ruff: noqa: F401  -- this module is a facade over the submodules.

from __future__ import annotations

# Submodules first, dependency order, so monkeypatch targets such as
# ``polymarketbot.strategy.onchain.ctf.gas.get_dynamic_pol_usd_price`` exist.
from . import (  # noqa: F401
    balances,
    calldata,
    constants,
    fees,
    gas,
    measure,
    merge,
    receipts,
    redeem,
    results,
    rlp,
    submission,
)
from .client import PolygonCtfClient
from .constants import (
    CTF_CONTRACT_POLYGON,
    CTF_POSITION_BALANCE_SELECTOR,
    CTF_SHARE_DECIMALS,
    DEFAULT_COLLATERAL_USDC,
    ERC20_BALANCE_OF_SELECTOR,
    MERGE_POSITIONS_SELECTOR,
    POLYGON_CHAIN_ID,
    RECEIPT_POLL_INTERVAL_S,
    RECEIPT_REBROADCAST_WAIT_S,
    RECEIPT_TIMEOUT_S,
    REDEEM_POSITIONS_SELECTOR,
    SPLIT_POSITIONS_SELECTOR,
    ZERO_BYTES32,
)
from .gas import (
    _POL_PRICE_CACHE,
    _POL_PRICE_LOCK,
    calculate_optimal_merge_threshold,
    estimate_merge_cost_usd,
    get_dynamic_gas_price_gwei,
    get_dynamic_pol_usd_price,
)
from .measure import (
    DEFAULT_POLYGON_GAS_TAPE,
    load_measured_block_interval_s,
    load_persisted_rpc_rtt_s,
)
from .results import MergeExecutionResult, RedeemExecutionResult, SettlementOutcome
from .rlp import (
    SignedEip1559Transaction,
    eip1559_signing_hash,
    rlp_encode,
    sign_eip1559_transaction,
)

__all__ = [
    "CTF_CONTRACT_POLYGON",
    "DEFAULT_COLLATERAL_USDC",
    "ZERO_BYTES32",
    "MERGE_POSITIONS_SELECTOR",
    "REDEEM_POSITIONS_SELECTOR",
    "ERC20_BALANCE_OF_SELECTOR",
    "CTF_POSITION_BALANCE_SELECTOR",
    "POLYGON_CHAIN_ID",
    "SignedEip1559Transaction",
    "rlp_encode",
    "eip1559_signing_hash",
    "sign_eip1559_transaction",
    "MergeExecutionResult",
    "RedeemExecutionResult",
    "SettlementOutcome",
    "PolygonCtfClient",
    "get_dynamic_pol_usd_price",
    "get_dynamic_gas_price_gwei",
    "estimate_merge_cost_usd",
    "calculate_optimal_merge_threshold",
]
