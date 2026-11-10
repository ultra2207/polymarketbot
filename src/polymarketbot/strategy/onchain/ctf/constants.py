"""Immutable Polygon PoS CTF contract constants.

Moved verbatim from ``strategy/onchain/ctf.py``. Calldata selectors and the
chain id are consensus data: editing one changes the signed bytes.
"""

from __future__ import annotations

# Polymarket CTF & Token Constants on Polygon PoS (Chain ID 137)
CTF_CONTRACT_POLYGON = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DEFAULT_COLLATERAL_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC.e
ZERO_BYTES32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

# splitPosition(address,bytes32,bytes32,uint256[],uint256) -> 0x72ce4275
SPLIT_POSITIONS_SELECTOR = "72ce4275"
# mergePositions(address,bytes32,bytes32,uint256[],uint256) -> 0x9e7212ad
MERGE_POSITIONS_SELECTOR = "9e7212ad"
# redeemPositions(address,bytes32,bytes32,uint256[]) -> 0x01b7037c
REDEEM_POSITIONS_SELECTOR = "01b7037c"
# balanceOf(address) -> 0x70a08231
ERC20_BALANCE_OF_SELECTOR = "70a08231"
# balanceOf(address,uint256) (ERC-1155 CTF positions) -> 0x00fdd58e
CTF_POSITION_BALANCE_SELECTOR = "00fdd58e"

# Polygon PoS mainnet chain id, used in the EIP-1559 signing payload.
POLYGON_CHAIN_ID = 137

# CTF position amounts are denominated in the 6-decimal collateral.
CTF_SHARE_DECIMALS = 1_000_000

# Settlement transaction gas clamps (Wei). Fees come from the live oracle but a
# stuck transaction is worse than an over-priced one, so the floors are firm.
_MIN_PRIORITY_FEE_WEI = 25 * 10**9
_MAX_PRIORITY_FEE_WEI = 50 * 10**9
_MIN_MAX_FEE_WEI = 25 * 10**9
_MAX_MAX_FEE_WEI = 1_000 * 10**9

RECEIPT_POLL_INTERVAL_S = 0.5
RECEIPT_TIMEOUT_S = 60.0
# After a timeout the identical payload is re-sent once and watched for this
# long before the settlement is left PENDING for the replay.
RECEIPT_REBROADCAST_WAIT_S = 5.0
