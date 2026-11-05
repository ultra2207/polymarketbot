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
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union

from polymarketbot.chain import (
    ALREADY_KNOWN,
    EXECUTION_REVERTED,
    NONCE_TOO_LOW,
    REPLACEMENT_UNDERPRICED,
    ChainClient,
    classify_rpc_error,
)
from polymarketbot.logs import get_logger
from polymarketbot.rewards.queue_valuation import CTFGasProfile, PolygonPoSEnvironment
from polymarketbot.risk.fast_signer import (
    _SECP256K1_N as SECP256K1_N,
    ecdsa_sign_digest,
    keccak256,
    private_key_to_address,
)
from polymarketbot.trading.pending_settlements import (
    FAILED as SETTLEMENT_FAILED,
    PENDING as SETTLEMENT_PENDING,
    REVERTED as SETTLEMENT_REVERTED,
    SUCCESS as SETTLEMENT_SUCCESS,
    PendingSettlementStore,
)

logger = get_logger("ctf_client")

# Polymarket CTF & Token Constants on Polygon PoS (Chain ID 137)
CTF_CONTRACT_POLYGON = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DEFAULT_COLLATERAL_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC.e
ZERO_BYTES32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

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
    error: Optional[str] = None
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
    index_sets: List[int]
    gas_fee_usd: float
    gas_units_used: int
    is_dry_run: bool = False
    error: Optional[str] = None
    status: str = "failed"


@dataclass
class SettlementOutcome:
    """Result of signing, persisting and broadcasting one settlement payload."""

    status: str
    tx_hash: str = ""
    gas_fee_usd: float = 0.0
    gas_units_used: int = 0
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "success"


# ---------------------------------------------------------------------------
# Minimal RLP + EIP-1559 type-2 transaction encoding
# ---------------------------------------------------------------------------
# Polygon (chainId 137) type-2 transaction field order, per EIP-1559:
#
#   signing payload:
#       0x02 || rlp([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas,
#                     gasLimit, to, value, data, accessList])
#
#   broadcast payload:
#       0x02 || rlp([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas,
#                     gasLimit, to, value, data, accessList, yParity, r, s])
#
# `yParity` is 0/1 (EIP-1559 transactions do not carry the 27/37 offset).
# `accessList` is an empty list (0xc0) for a plain CTF call.


def _int_to_bytes(value: int) -> bytes:
    """Minimal big-endian bytes for an RLP integer (0 encodes as empty bytes)."""
    if value < 0:
        raise ValueError(f"RLP cannot encode a negative integer: {value}")
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _rlp_length_prefix(length: int, offset: int) -> bytes:
    if length <= 55:
        return bytes([offset + length])
    length_bytes = _int_to_bytes(length)
    return bytes([offset + 55 + len(length_bytes)]) + length_bytes


def _hex_to_bytes(value: str | bytes | bytearray) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    clean = value.strip()
    if clean in ("", "0x", "0X"):
        return b""
    clean = clean.removeprefix("0x").removeprefix("0X")
    if len(clean) % 2:
        clean = "0" + clean
    return bytes.fromhex(clean)


def rlp_encode(item: Any) -> bytes:
    """RLP-encode bytes, text strings, integers or (nested) lists.

    Integers are treated as minimal big-endian scalars, as required by
    Ethereum's transaction encoding. Text strings are UTF-8 encoded; callers
    with hex payloads pass decoded bytes.
    """
    if isinstance(item, bool):
        raise TypeError("RLP does not encode booleans")
    if isinstance(item, int):
        item = _int_to_bytes(item)
    if isinstance(item, str):
        item = item.encode("utf-8")
    if isinstance(item, (bytes, bytearray)):
        data = bytes(item)
        if len(data) == 1 and data[0] < 0x80:
            return data
        return _rlp_length_prefix(len(data), 0x80) + data
    if isinstance(item, (list, tuple)):
        payload = b"".join(rlp_encode(element) for element in item)
        return _rlp_length_prefix(len(payload), 0xC0) + payload
    raise TypeError(f"Cannot RLP-encode {type(item)!r}")


def _address_bytes(address: str) -> bytes:
    clean = address.strip().removeprefix("0x").removeprefix("0X")
    if len(clean) != 40:
        raise ValueError(f"Invalid Ethereum address: {address!r}")
    return bytes.fromhex(clean)


def eip1559_signing_hash(
    *,
    chain_id: int,
    nonce: int,
    max_priority_fee_per_gas: int,
    max_fee_per_gas: int,
    gas_limit: int,
    to: str,
    value: int = 0,
    data: str | bytes = b"",
    access_list: list | tuple = (),
) -> bytes:
    """keccak256(0x02 || rlp([...])) — the digest an EIP-1559 tx signs."""
    payload = rlp_encode(
        [
            chain_id,
            nonce,
            max_priority_fee_per_gas,
            max_fee_per_gas,
            gas_limit,
            _address_bytes(to),
            value,
            _hex_to_bytes(data),
            access_list,
        ]
    )
    return keccak256(b"\x02" + payload)


@dataclass
class SignedEip1559Transaction:
    """A signed type-2 transaction ready for eth_sendRawTransaction."""

    raw_transaction: str
    tx_hash: str
    signing_hash: bytes
    y_parity: int
    r: int
    s: int
    chain_id: int


def sign_eip1559_transaction(
    private_key: bytes,
    *,
    chain_id: int,
    nonce: int,
    max_priority_fee_per_gas: int,
    max_fee_per_gas: int,
    gas_limit: int,
    to: str,
    value: int = 0,
    data: str | bytes = b"",
    access_list: list | tuple = (),
) -> SignedEip1559Transaction:
    """Sign a Polygon type-2 transaction with the in-tree secp256k1 signer.

    The signed payload is the same field list as the signing payload followed by
    (yParity, r, s), prefixed with the type byte 0x02.
    """
    if len(private_key) != 32:
        raise ValueError("Private key must be 32 bytes")
    fields = [
        chain_id,
        nonce,
        max_priority_fee_per_gas,
        max_fee_per_gas,
        gas_limit,
        _address_bytes(to),
        value,
        _hex_to_bytes(data),
        access_list,
    ]
    signing_hash = keccak256(b"\x02" + rlp_encode(fields))
    r, s, v = ecdsa_sign_digest(signing_hash, private_key)
    y_parity = v - 27
    raw = b"\x02" + rlp_encode(fields + [y_parity, r, s])
    return SignedEip1559Transaction(
        raw_transaction="0x" + raw.hex(),
        tx_hash="0x" + keccak256(raw).hex(),
        signing_hash=signing_hash,
        y_parity=y_parity,
        r=r,
        s=s,
        chain_id=chain_id,
    )


def _parse_private_key(raw: str | None) -> Optional[bytes]:
    """Return validated 32-byte key material, or None when absent."""
    if not raw:
        return None
    clean = raw.strip().removeprefix("0x").removeprefix("0X")
    if len(clean) != 64:
        raise ValueError("Private key must be 32 bytes of hex")
    try:
        key = bytes.fromhex(clean)
    except ValueError as exc:
        raise ValueError(f"Private key is not valid hex: {exc}") from exc
    as_int = int.from_bytes(key, "big")
    if not 1 <= as_int < SECP256K1_N:
        raise ValueError("Private key is outside the secp256k1 scalar range")
    return key


def _hex_to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(str(value), 16)


def get_dynamic_gas_price_gwei(
    chain_client: Optional[ChainClient] = None,
    rpc_url: Optional[str] = None,
    fallback_gwei: float = 40.0,
) -> float:
    """Query dynamic gas price in Gwei on Polygon PoS using EIP-1559 base fee + priority fee.

    Fallback sequence:
    1. Polygon RPC eth_getBlockByNumber("latest") baseFeePerGas + eth_maxPriorityFeePerGas.
    2. Polygon RPC eth_gasPrice.
    3. Fast gas oracle (https://gasstation.polygon.technology/v2).
    4. Fallback baseline (40.0 Gwei).

    Returns:
        Gas price in Gwei (float).
    """
    client = chain_client
    if client is None and rpc_url is not None:
        try:
            client = ChainClient(url=rpc_url)
        except Exception as e:
            logger.debug(f"Failed to instantiate ChainClient for gas query: {e}")

    if client is not None:
        try:
            block = client._rpc("eth_getBlockByNumber", ["latest", False])
            if isinstance(block, dict) and "baseFeePerGas" in block and block["baseFeePerGas"]:
                base_fee_gwei = int(str(block["baseFeePerGas"]), 16) / 1e9
                priority_fee_gwei = 30.0
                try:
                    raw_tip = client._rpc("eth_maxPriorityFeePerGas", [])
                    if raw_tip:
                        priority_fee_gwei = int(str(raw_tip), 16) / 1e9
                except Exception as tip_err:
                    logger.debug(f"eth_maxPriorityFeePerGas lookup fallback: {tip_err}")

                total_gwei = base_fee_gwei + priority_fee_gwei
                if total_gwei > 0:
                    return float(max(25.0, min(500.0, round(total_gwei, 2))))
        except Exception as e:
            logger.debug(f"RPC EIP-1559 gas price lookup failed: {e}")

        try:
            raw_gas_price = client._rpc("eth_gasPrice", [])
            if raw_gas_price:
                live_gwei = int(str(raw_gas_price), 16) / 1e9
                if live_gwei > 0:
                    return float(max(25.0, min(500.0, round(live_gwei, 2))))
        except Exception as e:
            logger.debug(f"RPC eth_gasPrice fallback lookup failed: {e}")

    try:
        import requests
        resp = requests.get("https://gasstation.polygon.technology/v2", timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                fast_data = data.get("fast") or data.get("standard")
                if isinstance(fast_data, dict) and "maxFee" in fast_data:
                    oracle_gwei = float(fast_data["maxFee"])
                    if oracle_gwei > 0:
                        return float(max(25.0, min(500.0, round(oracle_gwei, 2))))
    except Exception as oracle_err:
        logger.debug(f"Fast gas oracle lookup fallback: {oracle_err}")

    return float(fallback_gwei)


_POL_PRICE_CACHE: dict[str, float] = {"price": 0.092, "timestamp": 0.0}
_POL_PRICE_LOCK = threading.Lock()


def get_dynamic_pol_usd_price(
    fallback_price: float = 0.092,
    cache_ttl_seconds: float = 300.0,
) -> float:
    """Query live POL/USD exchange rate from Binance REST with caching and fallback.

    Queries POLUSDT with fallback to MATICUSDT.
    Cached thread-safely for cache_ttl_seconds (default: 300.0s).

    Args:
        fallback_price: Value returned if all queries fail (default: 0.092).
        cache_ttl_seconds: TTL cache window in seconds (default: 300.0).

    Returns:
        Live or cached POL price in USD (float).
    """
    now = time.time()
    with _POL_PRICE_LOCK:
        if now - _POL_PRICE_CACHE["timestamp"] < cache_ttl_seconds and _POL_PRICE_CACHE["price"] > 0:
            return _POL_PRICE_CACHE["price"]

    symbols = ["POLUSDT", "MATICUSDT"]
    headers = {"User-Agent": "polymarketbot/1.0"}
    for sym in symbols:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
        try:
            import requests
            resp = requests.get(url, headers=headers, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "price" in data:
                    px = float(data["price"])
                    if px > 0.0:
                        with _POL_PRICE_LOCK:
                            _POL_PRICE_CACHE["price"] = px
                            _POL_PRICE_CACHE["timestamp"] = now
                        return px
        except Exception as e:
            logger.debug(f"Failed to fetch live POL price for {sym}: {e}")

    logger.debug(f"POL price lookup failed across all sources, returning fallback {fallback_price}")
    with _POL_PRICE_LOCK:
        cached = _POL_PRICE_CACHE["price"]
        if cached > 0 and _POL_PRICE_CACHE["timestamp"] > 0:
            return cached
    return float(fallback_price)


def estimate_merge_cost_usd(
    pol_usd_price: Optional[float] = None,
    gas_units: int = 78_000,
    gas_price_gwei: Optional[float] = None,
    chain_client: Optional[ChainClient] = None,
) -> float:
    """Calculate dynamic dollar gas cost for CTF complete-set merge: Gas Units * Gas Price * POL/USD.

    Args:
        pol_usd_price: Current market exchange rate for POL in USD (default: dynamic lookup).
        gas_units: Number of Polygon PoS gas units consumed (default: 78,000 for mergePositions).
        gas_price_gwei: Optional explicit gas price in Gwei. If None, queries live dynamic gas price.
        chain_client: Optional ChainClient instance for RPC queries.

    Returns:
        Estimated gas cost in USD (float).
    """
    gwei = (
        float(gas_price_gwei)
        if gas_price_gwei is not None
        else get_dynamic_gas_price_gwei(chain_client=chain_client)
    )
    px = float(pol_usd_price) if pol_usd_price is not None else get_dynamic_pol_usd_price()
    gas_fee_pol = gas_units * (gwei * 1e-9)
    gas_cost_usd = gas_fee_pol * px
    return round(gas_cost_usd, 6)


def calculate_optimal_merge_threshold(
    gas_cost_usd: Optional[float] = None,
    fee_basis_points: float = 0.0,
    max_gas_friction_ratio: float = 0.0015,
    min_threshold_shares: float = 1.0,
    pol_usd_price: Optional[float] = None,
    chain_client: Optional[ChainClient] = None,
) -> float:
    """Calculate the optimal complete-set merge threshold in shares.

    Ensures complete sets are merged only when capital liberated exceeds
    the gas friction hurdle. At baseline $0.015 gas and 15 bps hurdle, returns 10.0 complete sets.
    Dynamically scales with live on-chain gas costs and live POL price.

    Formula:
        hurdle_ratio = max_gas_friction_ratio + (fee_basis_points / 10_000.0)
        threshold = gas_cost_usd / hurdle_ratio

    Args:
        gas_cost_usd: Dynamic USD cost of mergePositions transaction. If None, estimates dynamically.
        fee_basis_points: Optional exchange/protocol fee or hurdle in basis points (1 bp = 0.0001).
        max_gas_friction_ratio: Maximum tolerable gas drag as fraction of capital (default: 0.0015 = 15 bps).
        min_threshold_shares: Minimum threshold floor in complete sets (default: 1.0).
        pol_usd_price: Optional POL exchange rate in USD (default: dynamic lookup).
        chain_client: Optional ChainClient instance for RPC queries.

    Returns:
        Optimal threshold in complete sets (float).
    """
    if gas_cost_usd is None:
        cost = estimate_merge_cost_usd(pol_usd_price=pol_usd_price, chain_client=chain_client)
    else:
        cost = float(gas_cost_usd)

    if cost <= 0.0:
        return float(min_threshold_shares)

    hurdle = float(max_gas_friction_ratio) + (float(fee_basis_points) / 10_000.0)
    if hurdle <= 0.0:
        hurdle = 0.0015

    optimal_shares = cost / hurdle
    return max(float(min_threshold_shares), round(optimal_shares, 2))


class PolygonCtfClient:
    """Direct on-chain client for Polymarket Conditional Tokens Framework on Polygon."""

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        private_key: Optional[str] = None,
        ctf_address: str = CTF_CONTRACT_POLYGON,
        collateral_address: str = DEFAULT_COLLATERAL_USDC,
        chain_client: Optional[ChainClient] = None,
        dry_run: bool = False,
        signature_type: int = 0,
        settlement_store: Optional[PendingSettlementStore] = None,
    ) -> None:
        self.ctf_address = ctf_address.lower()
        self.collateral_address = collateral_address.lower()
        self.chain_client = chain_client or ChainClient(url=rpc_url)
        self.private_key = private_key or os.environ.get("POLY_PRIVATE_KEY", "")
        # `dry_run` may be False while the key is absent: the client then stays
        # safe/dry internally, but settlement calls must refuse rather than
        # report a simulated success for a live-mode request.
        self._dry_run_requested = bool(dry_run)
        self.dry_run = dry_run or not bool(self.private_key)
        # 0 = EOA (funds + tokens on the signing key), 1 = POLY_PROXY,
        # 2 = POLY_GNOSIS_SAFE. Only an EOA can be settled by this signer.
        self.signature_type = int(signature_type)

        self._private_key_bytes: Optional[bytes] = None
        self.signer_address: str = ""
        if self.private_key:
            try:
                self._private_key_bytes = _parse_private_key(self.private_key)
            except ValueError as exc:
                logger.error(f"Invalid POLY_PRIVATE_KEY supplied; settlement disabled: {exc}")
            if self._private_key_bytes is not None:
                self.signer_address = private_key_to_address(self._private_key_bytes)

        self.gas_profile = CTFGasProfile()
        self.pos_env = PolygonPoSEnvironment()

        # Every live settlement is written here before it is broadcast. The
        # engine replaces this with a store bound to the engine database so the
        # record outlives the process; a standalone client still gets the
        # in-process guarantee.
        self.settlement_store = settlement_store or PendingSettlementStore()
        self.receipt_timeout_s = RECEIPT_TIMEOUT_S
        self.receipt_poll_interval_s = RECEIPT_POLL_INTERVAL_S
        self.receipt_rebroadcast_wait_s = RECEIPT_REBROADCAST_WAIT_S

        if self.dry_run:
            logger.info(
                f"[DRY-RUN CTF] PolygonCtfClient operating in safe dry-run mode "
                f"(has_key={bool(self.private_key)}). Real on-chain transactions will be simulated."
            )
        elif not self.is_settlement_enabled:
            logger.warning(
                "PolygonCtfClient is not in dry-run mode but cannot settle on-chain "
                f"(valid_key={self._private_key_bytes is not None}, signature_type={self.signature_type}). "
                "merge_positions/redeem_positions will refuse to broadcast."
            )

    @property
    def is_settlement_enabled(self) -> bool:
        """True only when live, EOA-keyed settlement can actually be broadcast.

        Proxy-wallet accounts (signature_type 1/2) never qualify: the CTF
        contract requires the call from the position owner (the proxy), which
        this client cannot sign for. Callers must not treat refusal as a
        simulated success.
        """
        return (
            not self.dry_run
            and self._private_key_bytes is not None
            and self.signature_type == 0
        )

    def _settlement_refusal_reason(self) -> Optional[str]:
        if self.dry_run:
            return "client is in dry-run mode"
        if self.signature_type != 0:
            return (
                f"on-chain settlement requires an EOA (signature_type=0); "
                f"signature_type={self.signature_type} is a proxy wallet and cannot be "
                "settled by this signer"
            )
        if self._private_key_bytes is None:
            return "no valid POLY_PRIVATE_KEY available for signing"
        return None

    def _live_without_key_reason(self) -> Optional[str]:
        """Refuse a live-mode request that has no usable signing key.

        Without this, `dry_run=False` plus an absent key silently degrades to a
        simulated success, which is exactly the accounting failure C1 describes.
        An explicit `dry_run=True` still simulates.
        """
        if self._dry_run_requested or self._private_key_bytes is not None:
            return None
        return (
            "live settlement requested (dry_run=False) but no valid "
            "POLY_PRIVATE_KEY is configured; refusing to simulate"
        )

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

    def get_dynamic_gas_price_gwei(self) -> float:
        """Query dynamic gas price in Gwei with fallback to fast oracle / 40 Gwei."""
        return get_dynamic_gas_price_gwei(self.chain_client)

    def estimate_merge_cost_usd(
        self,
        pol_usd_price: Optional[float] = None,
        gas_units: Optional[int] = None,
    ) -> float:
        """Calculate dynamic dollar gas cost for CTF complete-set merge: Gas Units * Gas Price * POL/USD."""
        px = pol_usd_price if pol_usd_price is not None else get_dynamic_pol_usd_price()
        units = gas_units or self.gas_profile.gas_merge_direct_typical
        gwei = self.get_dynamic_gas_price_gwei()
        return estimate_merge_cost_usd(
            pol_usd_price=px,
            gas_units=units,
            gas_price_gwei=gwei,
            chain_client=self.chain_client,
        )

    def calculate_optimal_merge_threshold(
        self,
        gas_cost_usd: Optional[float] = None,
        fee_basis_points: float = 0.0,
        pol_usd_price: Optional[float] = None,
    ) -> float:
        """Calculate optimal complete set merge threshold using dynamic gas cost."""
        cost = (
            gas_cost_usd
            if gas_cost_usd is not None
            else self.estimate_merge_cost_usd(pol_usd_price=pol_usd_price)
        )
        return calculate_optimal_merge_threshold(
            gas_cost_usd=cost,
            fee_basis_points=fee_basis_points,
            pol_usd_price=pol_usd_price,
            chain_client=self.chain_client,
        )

    def estimate_gas_fee(self, pol_usd_price: Optional[float] = None) -> Tuple[float, float, int]:
        """Estimate dynamic Polygon PoS gas fee for mergePositions.

        Returns:
            (gas_fee_usd, gas_price_gwei, gas_units)
        """
        px = pol_usd_price if pol_usd_price is not None else get_dynamic_pol_usd_price()
        gas_units = self.gas_profile.gas_merge_direct_typical  # ~78,000 gas units
        gas_price_gwei = self.get_dynamic_gas_price_gwei()
        gas_fee_usd = estimate_merge_cost_usd(
            pol_usd_price=px,
            gas_units=gas_units,
            gas_price_gwei=gas_price_gwei,
            chain_client=self.chain_client,
        )
        return round(gas_fee_usd, 4), round(gas_price_gwei, 1), gas_units

    def get_pol_balance(self, address: str) -> float:
        """Query native POL (MATIC) gas balance for wallet address."""
        if not address or address.startswith("0x00000"):
            return 0.50 if self.dry_run else 0.0

        try:
            raw_bal = self.chain_client._rpc("eth_getBalance", [address, "latest"])
            if raw_bal:
                wei = int(str(raw_bal), 16)
                return round(wei / 1e18, 4)
        except Exception as e:
            logger.debug(f"Failed to query POL balance: {e}")

        return 0.50 if self.dry_run else 0.0

    def get_usdc_balance(self, address: str) -> float:
        """Query on-chain ERC-20 USDC balance for wallet address."""
        if not address or address.startswith("0x00000"):
            return 100.0 if self.dry_run else 0.0

        try:
            clean_addr = address.lower().removeprefix("0x").rjust(64, "0")
            data = f"0x{ERC20_BALANCE_OF_SELECTOR}{clean_addr}"
            res = self.chain_client.eth_call(to=self.collateral_address, data=data)
            if res and res != "0x":
                raw_units = int(res, 16)
                return round(raw_units / 1_000_000.0, 2)
        except Exception as e:
            logger.debug(f"Failed to query on-chain USDC balance: {e}")

        return 100.0 if self.dry_run else 0.0

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
        index_sets: List[int],
        token_ids: Optional[Tuple[str, ...]],
    ) -> Optional[str]:
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

        required_raw = int(round(float(amount_shares) * CTF_SHARE_DECIMALS))
        problems: List[str] = []
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

    def _eip1559_fee_params_wei(self) -> Tuple[int, int]:
        """(maxPriorityFeePerGas, maxFeePerGas) in Wei from the live gas oracle."""
        oracle_gwei = float(self.get_dynamic_gas_price_gwei())
        base_fee_gwei = 0.0
        try:
            block = self.chain_client._rpc("eth_getBlockByNumber", ["latest", False])
            if isinstance(block, dict) and block.get("baseFeePerGas"):
                base_fee_gwei = int(str(block["baseFeePerGas"]), 16) / 1e9
        except Exception as exc:  # noqa: BLE001 - oracle still provides a floor
            logger.debug(f"baseFeePerGas lookup failed, deriving from gas oracle: {exc}")

        if base_fee_gwei <= 0.0:
            base_fee_gwei = oracle_gwei * 0.7

        priority_gwei = min(
            _MAX_PRIORITY_FEE_WEI / 1e9,
            max(_MIN_PRIORITY_FEE_WEI / 1e9, oracle_gwei - base_fee_gwei),
        )
        # Headroom over the next base fee: EIP-1559 requires maxFee >= base + tip.
        max_fee_gwei = min(
            _MAX_MAX_FEE_WEI / 1e9,
            max(
                _MIN_MAX_FEE_WEI / 1e9,
                oracle_gwei,
                base_fee_gwei * 1.25 + priority_gwei,
            ),
        )
        return int(round(priority_gwei * 1e9)), int(round(max_fee_gwei * 1e9))

    def _wait_for_receipt(
        self,
        tx_hash: str,
        timeout_s: Optional[float] = None,
        interval_s: Optional[float] = None,
    ) -> Optional[dict]:
        """Bounded receipt poll. Returns None on timeout."""
        timeout = self.receipt_timeout_s if timeout_s is None else float(timeout_s)
        interval = (
            self.receipt_poll_interval_s if interval_s is None else float(interval_s)
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                receipt = self.chain_client.get_transaction_receipt(tx_hash)
            except Exception as exc:  # noqa: BLE001 - a transient miss is retried
                logger.debug(f"eth_getTransactionReceipt({tx_hash}) failed: {exc}")
                receipt = None
            if isinstance(receipt, dict) and receipt:
                return receipt
            if time.monotonic() >= deadline:
                return None
            if interval > 0:
                time.sleep(interval)

    def _receipt_once(self, tx_hash: str) -> Optional[dict]:
        """One receipt lookup, tolerant of a transport failure."""
        try:
            receipt = self.chain_client.get_transaction_receipt(tx_hash)
        except Exception as exc:  # noqa: BLE001 - lookup failure is "not known yet"
            logger.debug(f"eth_getTransactionReceipt({tx_hash}) failed: {exc}")
            return None
        return receipt if isinstance(receipt, dict) and receipt else None

    def _rebroadcast(self, raw_hex: str) -> bool:
        """Re-send already-signed bytes. Idempotent: same bytes, same hash.

        Only ever called with a payload that is already stored, so a failure
        here cannot produce a second transaction with a new nonce.
        """
        try:
            self.chain_client.send_raw_transaction(raw_hex)
            return True
        except Exception as exc:  # noqa: BLE001 - the outcome is decided by the receipt
            kind = classify_rpc_error(exc)
            if kind == ALREADY_KNOWN:
                return True
            logger.warning(f"Re-send of the stored settlement payload failed ({kind}): {exc}")
            return False

    def _resolve_settlement(
        self, tx_hash: str, status: str, receipt: Optional[dict] = None
    ) -> None:
        try:
            self.settlement_store.resolve(tx_hash, status, receipt)
        except Exception as exc:  # noqa: BLE001 - accounting must not depend on the log
            logger.error(f"Could not resolve settlement row {tx_hash} as {status}: {exc}")

    def _finalize_settlement(
        self,
        tx_hash: str,
        receipt: dict,
        pol_usd_price: Optional[float],
        max_fee_wei: int,
    ) -> SettlementOutcome:
        """Turn a mined receipt into a terminal outcome and resolve the row."""
        status = receipt.get("status")
        gas_used = _hex_to_int(receipt.get("gasUsed"))
        effective_price = _hex_to_int(receipt.get("effectiveGasPrice")) or max_fee_wei
        pol_price = (
            float(pol_usd_price)
            if pol_usd_price is not None
            else get_dynamic_pol_usd_price()
        )
        # Gas is spent whether the call succeeded or reverted; report the receipt's
        # actual cost either way so accounting never invents a number.
        gas_fee_usd = round(gas_used * effective_price / 1e18 * pol_price, 6)

        if status is None:
            self._resolve_settlement(tx_hash, SETTLEMENT_FAILED, receipt)
            return SettlementOutcome(
                "failed", tx_hash, gas_fee_usd, gas_used, "receipt has no status field"
            )
        if _hex_to_int(status) != 1:
            self._resolve_settlement(tx_hash, SETTLEMENT_REVERTED, receipt)
            return SettlementOutcome(
                "failed", tx_hash, gas_fee_usd, gas_used, f"transaction reverted (status={status})"
            )
        self._resolve_settlement(tx_hash, SETTLEMENT_SUCCESS, receipt)
        return SettlementOutcome("success", tx_hash, gas_fee_usd, gas_used, None)

    def _broadcast_settlement(
        self,
        calldata: str,
        *,
        to_address: Optional[str] = None,
        condition_id: str = "",
        kind: str = "merge",
        amount: float = 0.0,
        pol_usd_price: Optional[float] = None,
    ) -> SettlementOutcome:
        """Sign, persist, broadcast and confirm one CTF transaction.

        The signed payload is written to the settlement store *before*
        `eth_sendRawTransaction`. From that point the hash is the identity of
        the settlement: a transient failure, a timeout or a `nonce too low`
        leads to the stored bytes being re-sent or the stored hash being looked
        up, never to a freshly signed transaction. The row is resolved only by a
        receipt; anything else is reported as `pending`.

        Gas cost is derived from the receipt (gasUsed * effectiveGasPrice),
        never from the pre-flight estimate.
        """
        refusal = self._settlement_refusal_reason()
        if refusal is not None or self._private_key_bytes is None:
            return SettlementOutcome(
                "failed", error=refusal or "settlement is not enabled"
            )
        target = to_address or self.ctf_address

        try:
            nonce = self.chain_client.get_transaction_count(self.signer_address, "pending")
            priority_wei, max_fee_wei = self._eip1559_fee_params_wei()

            estimated_gas = int(
                self.chain_client.estimate_gas(
                    {
                        "from": self.signer_address,
                        "to": target,
                        "data": calldata,
                        "value": "0x0",
                    }
                )
            )
            if estimated_gas <= 0:
                return SettlementOutcome(
                    "failed", error="eth_estimateGas returned no usable gas estimate"
                )
            gas_limit = int(estimated_gas * 1.25)  # 25% headroom for state drift

            signed = sign_eip1559_transaction(
                self._private_key_bytes,
                chain_id=POLYGON_CHAIN_ID,
                nonce=nonce,
                max_priority_fee_per_gas=priority_wei,
                max_fee_per_gas=max_fee_wei,
                gas_limit=gas_limit,
                to=target,
                value=0,
                data=calldata,
                access_list=[],
            )
        except Exception as exc:  # noqa: BLE001 - preparation failures are reported
            logger.error(f"CTF settlement preparation failed: {exc}")
            return SettlementOutcome("failed", error=f"broadcast failed: {exc}")

        tx_hash = signed.tx_hash

        # --- persistence point: the payload exists on disk before it is sent ---
        try:
            if not self.settlement_store.record(
                tx_hash=tx_hash,
                raw_tx=signed.raw_transaction,
                nonce=nonce,
                condition_id=condition_id,
                kind=kind,
                amount=amount,
            ):
                logger.warning(
                    f"Settlement tx {tx_hash} was already stored; reusing the stored payload"
                )
        except sqlite3.IntegrityError:
            # The partial unique index allows one PENDING row per condition. If
            # this insert loses that race, another settlement is already in
            # flight (possibly from another process) and must be resolved first.
            logger.error(
                f"CTF settlement refused: another settlement is already pending for "
                f"{condition_id[:14]}; signed tx {tx_hash} was not broadcast"
            )
            return SettlementOutcome(
                "failed",
                tx_hash,
                error=(
                    f"another settlement is already pending for condition {condition_id}; "
                    "resolve it by hash before settling again"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - unpersisted broadcast is refused
            logger.error(f"CTF settlement refused: signed tx could not be persisted: {exc}")
            return SettlementOutcome(
                "failed", tx_hash, error=f"signed transaction could not be persisted: {exc}"
            )

        logger.info(
            f"Broadcasting CTF {kind} settlement tx {tx_hash} "
            f"(nonce={nonce}, gas_limit={gas_limit}, max_fee={max_fee_wei / 1e9:.2f} Gwei)"
        )
        try:
            broadcast_hash = self.chain_client.send_raw_transaction(signed.raw_transaction)
            if broadcast_hash and str(broadcast_hash).lower() != tx_hash.lower():
                logger.warning(
                    f"Node reported tx hash {broadcast_hash} for stored tx {tx_hash}"
                )
        except Exception as exc:  # noqa: BLE001 - ambiguity is the whole point here
            error_kind = classify_rpc_error(exc)
            logger.error(f"CTF settlement broadcast failed ({error_kind}): {exc}")
            if error_kind == EXECUTION_REVERTED:
                # The node rejected the call outright: nothing was mined.
                self._resolve_settlement(tx_hash, SETTLEMENT_FAILED)
                return SettlementOutcome(
                    "failed", tx_hash, error=f"broadcast rejected: {exc}"
                )

            receipt = self._receipt_once(tx_hash)
            if receipt is not None:
                # It landed despite the error, or a previous attempt mined it.
                return self._finalize_settlement(tx_hash, receipt, pol_usd_price, max_fee_wei)
            if error_kind in (NONCE_TOO_LOW, REPLACEMENT_UNDERPRICED):
                # Another transaction owns this nonce, so these bytes cannot be
                # mined as-is. Re-sending them changes nothing; a new nonce
                # would be a second merge. Leave the row PENDING.
                return SettlementOutcome(
                    "pending",
                    tx_hash,
                    error=(
                        f"settlement ambiguous ({error_kind}): {exc}; tx {tx_hash} has no "
                        "receipt yet and is not retried"
                    ),
                )
            # Transient (429 / 5xx / timeout): the node may already hold the
            # bytes. Re-send the identical payload, which is idempotent.
            if not self._rebroadcast(signed.raw_transaction):
                return SettlementOutcome(
                    "pending",
                    tx_hash,
                    error=f"settlement ambiguous ({error_kind}): {exc}; tx {tx_hash} not confirmed",
                )

        receipt = self._wait_for_receipt(tx_hash)
        if receipt is None:
            # Not mined in the window. It may still be mined later, so re-send
            # the identical bytes once, watch briefly, then leave it PENDING.
            if self._rebroadcast(signed.raw_transaction):
                receipt = self._wait_for_receipt(
                    tx_hash, timeout_s=self.receipt_rebroadcast_wait_s
                )
            if receipt is None:
                return SettlementOutcome(
                    "pending",
                    tx_hash,
                    error=(
                        f"transaction {tx_hash} not mined within "
                        f"{self.receipt_timeout_s:.0f}s; stored and left pending"
                    ),
                )
        return self._finalize_settlement(tx_hash, receipt, pol_usd_price, max_fee_wei)

    def merge_positions(
        self,
        condition_id: str,
        amount_shares: float,
        index_sets: Optional[List[int]] = None,
        pol_usd_price: Optional[float] = None,
        token_ids: Optional[Tuple[str, ...]] = None,
    ) -> MergeExecutionResult:
        """Execute or simulate on-chain CTF complete-set merge.

        Converts complete sets (YES + NO) into USDC cash collateral. Live
        success is only reported once the transaction receipt has status 1;
        the returned gas cost is the receipt's, not an estimate.
        """
        if amount_shares <= 0.0:
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error="Zero amount shares to merge",
            )

        indices = list(index_sets or [1, 2])
        calldata = self.encode_merge_positions(condition_id, amount_shares, index_sets)

        live_key_error = self._live_without_key_reason()
        if live_key_error is not None:
            logger.error(f"Live CTF mergePositions refused for condition {condition_id[:14]}: {live_key_error}")
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=live_key_error,
                is_dry_run=False,
            )

        if self.dry_run:
            gas_fee_usd, gas_price_gwei, gas_units = self.estimate_gas_fee(pol_usd_price=pol_usd_price)
            mock_hash = f"0xmock_tx_{uuid.uuid4().hex}"
            logger.info(
                f"[DRY-RUN CTF] Simulated mergePositions({amount_shares:.2f} sets) on condition {condition_id[:14]}... | "
                f"Unlocked: ${amount_shares:.2f} USDC | Est Gas: ${gas_fee_usd:.4f} ({gas_units} units @ {gas_price_gwei} Gwei) | "
                f"TxHash: {mock_hash[:16]}..."
            )
            return MergeExecutionResult(
                success=True,
                tx_hash=mock_hash,
                condition_id=condition_id,
                complete_sets_merged=amount_shares,
                usdc_unlocked=amount_shares,
                gas_fee_usd=gas_fee_usd,
                gas_units_used=gas_units,
                is_dry_run=True,
                status="dry_run",
            )

        refusal = self._settlement_refusal_reason()
        if refusal is not None:
            logger.error(f"Live CTF mergePositions refused for condition {condition_id[:14]}: {refusal}")
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=refusal,
                is_dry_run=False,
            )

        balance_error = self._verify_position_balances(
            condition_id, amount_shares, indices, token_ids
        )
        if balance_error is not None:
            logger.error(f"CTF mergePositions aborted: {balance_error}")
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=balance_error,
                is_dry_run=False,
            )

        logger.info(
            f"Broadcasting live CTF mergePositions for {amount_shares:.2f} complete sets on Polygon PoS..."
        )
        outcome = self._broadcast_settlement(
            calldata,
            condition_id=condition_id,
            kind="merge",
            amount=amount_shares,
            pol_usd_price=pol_usd_price,
        )
        if outcome.status == "pending":
            logger.warning(
                f"CTF mergePositions pending for {condition_id[:14]}: {outcome.error}"
            )
            return MergeExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF mergePositions is pending confirmation",
                is_dry_run=False,
                status="pending",
            )
        if not outcome.success:
            return MergeExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF mergePositions failed",
                is_dry_run=False,
                status="failed",
            )

        logger.info(
            f"CTF mergePositions confirmed: {amount_shares:.2f} sets, "
            f"tx={outcome.tx_hash}, gas={outcome.gas_units_used} units (${outcome.gas_fee_usd:.4f})"
        )
        return MergeExecutionResult(
            success=True,
            tx_hash=outcome.tx_hash,
            condition_id=condition_id,
            complete_sets_merged=amount_shares,
            usdc_unlocked=amount_shares,
            gas_fee_usd=outcome.gas_fee_usd,
            gas_units_used=outcome.gas_units_used,
            is_dry_run=False,
            status="success",
        )

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

    def execute_settlement(
        self,
        calldata: str,
        *,
        to_address: str,
        condition_id: str,
        amount_shares: float,
        pol_usd_price: Optional[float] = None,
        kind: str = "merge",
    ) -> MergeExecutionResult:
        """Broadcast a merge/convert call to a non-CTF target.

        Used for the v2 Router (``merge(bytes31,uint256)``) and the NegRisk
        adapter (``mergePositions(bytes32,uint256)``). The CTF preflight in
        :meth:`merge_positions` reads CTF balances, which do not exist on those
        ledgers, so callers must run ``settlement.verify_route`` instead. The
        amount is reported 1:1 only after a receipt with status 1.
        """
        amount = float(amount_shares)
        if not (
            isinstance(to_address, str)
            and to_address.startswith("0x")
            and len(to_address) == 42
        ):
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=f"invalid settlement target: {to_address!r}",
                status="failed",
            )
        if self.dry_run:
            return MergeExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                complete_sets_merged=0.0,
                usdc_unlocked=0.0,
                gas_fee_usd=0.0,
                gas_units_used=0,
                is_dry_run=True,
                error="non-CTF settlement has no dry-run simulation",
                status="failed",
            )

        outcome = self._broadcast_settlement(
            calldata,
            to_address=to_address,
            condition_id=condition_id,
            kind=kind,
            amount=amount,
            pol_usd_price=pol_usd_price,
        )
        success = outcome.status == "success"
        return MergeExecutionResult(
            success=success,
            tx_hash=outcome.tx_hash,
            condition_id=condition_id,
            complete_sets_merged=amount if success else 0.0,
            usdc_unlocked=amount if success else 0.0,
            gas_fee_usd=outcome.gas_fee_usd,
            gas_units_used=outcome.gas_units_used,
            is_dry_run=False,
            error=outcome.error,
            status=outcome.status,
        )

    def estimate_gas_fee_redeem(self, pol_usd_price: Optional[float] = None) -> Tuple[float, float, int]:
        """Estimate dynamic Polygon PoS gas fee for redeemPositions.

        Returns:
            (gas_fee_usd, gas_price_gwei, gas_units)
        """
        px = pol_usd_price if pol_usd_price is not None else get_dynamic_pol_usd_price()
        gas_units = 45_000  # typical redeem gas cost ~45,000 units
        gas_price_gwei = self.get_dynamic_gas_price_gwei()
        gas_fee_pol = gas_units * (gas_price_gwei * 1e-9)
        gas_fee_usd = gas_fee_pol * float(px)
        return round(gas_fee_usd, 4), round(gas_price_gwei, 1), gas_units

    def redeem_positions(
        self,
        condition_id: str,
        index_sets: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
        parent_collection_id: str = ZERO_BYTES32,
        pol_usd_price: Optional[float] = None,
    ) -> RedeemExecutionResult:
        """Execute or simulate on-chain CTF position redemption for resolved markets."""
        indices = list(index_sets or [1, 2])
        calldata = self.encode_redeem_positions(
            condition_id=condition_id,
            index_sets=indices,
            collateral_token=collateral_token,
            parent_collection_id=parent_collection_id,
        )

        live_key_error = self._live_without_key_reason()
        if live_key_error is not None:
            logger.error(f"Live CTF redeemPositions refused for condition {condition_id[:14]}: {live_key_error}")
            return RedeemExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=live_key_error,
                is_dry_run=False,
            )

        if self.dry_run:
            gas_fee_usd, gas_price_gwei, gas_units = self.estimate_gas_fee_redeem(pol_usd_price=pol_usd_price)
            mock_hash = f"0xmock_redeem_tx_{uuid.uuid4().hex}"
            logger.info(
                f"[DRY-RUN CTF] Simulated redeemPositions on condition {condition_id[:14]}... | "
                f"Est Gas: ${gas_fee_usd:.4f} ({gas_units} units @ {gas_price_gwei} Gwei) | "
                f"TxHash: {mock_hash[:16]}..."
            )
            return RedeemExecutionResult(
                success=True,
                tx_hash=mock_hash,
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=gas_fee_usd,
                gas_units_used=gas_units,
                is_dry_run=True,
                status="dry_run",
            )

        refusal = self._settlement_refusal_reason()
        if refusal is not None:
            logger.error(f"Live CTF redeemPositions refused for condition {condition_id[:14]}: {refusal}")
            return RedeemExecutionResult(
                success=False,
                tx_hash="",
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=0.0,
                gas_units_used=0,
                error=refusal,
                is_dry_run=False,
            )

        logger.info(
            f"Broadcasting live CTF redeemPositions on Polygon PoS for condition {condition_id[:14]}..."
        )
        outcome = self._broadcast_settlement(
            calldata,
            condition_id=condition_id,
            kind="redeem",
            amount=0.0,
            pol_usd_price=pol_usd_price,
        )
        if outcome.status == "pending":
            logger.warning(
                f"CTF redeemPositions pending for {condition_id[:14]}: {outcome.error}"
            )
            return RedeemExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF redeemPositions is pending confirmation",
                is_dry_run=False,
                status="pending",
            )
        if not outcome.success:
            return RedeemExecutionResult(
                success=False,
                tx_hash=outcome.tx_hash,
                condition_id=condition_id,
                index_sets=indices,
                gas_fee_usd=outcome.gas_fee_usd,
                gas_units_used=outcome.gas_units_used,
                error=outcome.error or "CTF redeemPositions failed",
                is_dry_run=False,
                status="failed",
            )

        logger.info(
            f"CTF redeemPositions confirmed: condition {condition_id[:14]}, "
            f"tx={outcome.tx_hash}, gas={outcome.gas_units_used} units (${outcome.gas_fee_usd:.4f})"
        )
        return RedeemExecutionResult(
            success=True,
            tx_hash=outcome.tx_hash,
            condition_id=condition_id,
            index_sets=indices,
            gas_fee_usd=outcome.gas_fee_usd,
            gas_units_used=outcome.gas_units_used,
            is_dry_run=False,
            status="success",
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


