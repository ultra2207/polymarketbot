"""Settlement routing primitives for Polymarket v1 CTF, v1 neg-risk and v2.

The engine has three distinct position ledgers, and a settlement call is only
valid on the ledger that minted the position:

* **v1 binary** - plain CTF positions collateralised by USDC.e. Closing a
  complete set is ``CTF.mergePositions(...)`` (``CTF_MERGE``).
* **v1 neg-risk** - binary questions linked into one multi-outcome market by
  the NegRiskAdapter. A complete-set pair is closed with
  ``NegRiskAdapter.mergePositions(bytes32 questionId, uint256 amount)``
  (``NEGRISK_MERGE``). The adapter's other primitive is
  ``convertPositions(bytes32 marketId, uint256 indexSet, uint256 amount)``
  (``NEGRISK_CONVERT``); it spends NO positions across questions and returns
  collateral plus the complementary YES positions. A market whose neg-risk
  grouping cannot be verified (no ``negRiskMarketID``) is treated as a plain
  CTF market, and the balance preflight will reject it rather than broadcast
  against the wrong ledger.
* **v2** - positions live in PositionManager and settle through the Router
  (``ROUTER_MERGE``): ``merge(bytes31 conditionId, uint256 amount)`` and
  ``redeem(bytes31 conditionId, uint256 outcomeIndex, uint256 amount)``.

ABI sources (verified 2026-11-04)
---------------------------------
* Router ABI - ``data/raw/docs/site/migrate/polymarket-v2/contract-integrations.md``
  (Polymarket V2 contract migration guide). ``conditionId`` is ``bytes31``; a
  padded ``bytes32`` must have a zero final byte before narrowing. Amounts are
  6-decimal base units.
* NegRiskAdapter ``convertPositions(bytes32,uint256,uint256)`` - the deployed
  adapter at ``0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296``; source
  ``Polymarket/neg-risk-ctf-adapter/src/NegRiskAdapter.sol``. The selector
  ``0xc64748c4`` also matches the published contract binding.
* CTF ``mergePositions(address,bytes32,bytes32,uint256[],uint256)`` - already
  encoded by ``trading/ctf.py``; the selector and packing below are byte-for
  -byte identical (``0x9e7212ad``).

Nothing in this module signs or broadcasts. ``verify_route`` only performs
read-only ``eth_call`` probes so a settlement is never sent blind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from polymarketbot.logs import get_logger
from polymarketbot.risk.fast_signer import keccak256

if TYPE_CHECKING:  # pragma: no cover - typing only
    from polymarketbot.chain import ChainClient

logger = get_logger("settlement")

# ---------------------------------------------------------------------------
# Addresses (Polygon PoS)
# ---------------------------------------------------------------------------
ROUTER = "0x12121212006e4CD160D18e3f00711DA5c3372600"
POSITION_MANAGER = "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF"
NEGRISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

ZERO_BYTES32 = "0x" + "00" * 32

# 6-decimal base units: 1_000_000 == one share / one pUSD.
SHARE_SCALE = 1_000_000
_ONE_SHARE = Decimal("0.000001")
_UINT256_MAX = 2**256 - 1

# ---------------------------------------------------------------------------
# Function selectors - keccak256("<signature>")[:4]
# ---------------------------------------------------------------------------
ROUTER_MERGE_SIGNATURE = "merge(bytes31,uint256)"
ROUTER_REDEEM_SIGNATURE = "redeem(bytes31,uint256,uint256)"
NEGRISK_CONVERT_SIGNATURE = "convertPositions(bytes32,uint256,uint256)"
NEGRISK_MERGE_SIGNATURE = "mergePositions(bytes32,uint256)"
CTF_MERGE_SIGNATURE = "mergePositions(address,bytes32,bytes32,uint256[],uint256)"
CTF_SPLIT_SIGNATURE = "splitPosition(address,bytes32,bytes32,uint256[],uint256)"
CTF_REDEEM_SIGNATURE = "redeemPositions(address,bytes32,bytes32,uint256[])"
CTF_PAYOUT_DENOMINATOR_SIGNATURE = "payoutDenominator(bytes32)"
CTF_PAYOUT_NUMERATOR_SIGNATURE = "payoutNumerators(bytes32,uint256)"
CTF_BALANCE_SIGNATURE = "balanceOf(address,uint256)"
IS_APPROVED_FOR_ALL_SIGNATURE = "isApprovedForAll(address,address)"
TRANSFER_SIGNATURE = "Transfer(address,address,uint256)"


def _selector(signature: str) -> str:
    return keccak256(signature.encode("utf-8")).hex()[:8]


ROUTER_MERGE_SELECTOR = "5b63685e"          # merge(bytes31,uint256)
ROUTER_REDEEM_SELECTOR = "d217a3cc"         # redeem(bytes31,uint256,uint256)
NEGRISK_CONVERT_SELECTOR = "c64748c4"       # convertPositions(bytes32,uint256,uint256)
NEGRISK_MERGE_SELECTOR = "b10c5c17"         # mergePositions(bytes32,uint256)
CTF_MERGE_SELECTOR = "9e7212ad"             # mergePositions(address,bytes32,bytes32,uint256[],uint256)
CTF_SPLIT_SELECTOR = "72ce4275"             # splitPosition(address,bytes32,bytes32,uint256[],uint256)
CTF_REDEEM_SELECTOR = "01b7037c"            # redeemPositions(address,bytes32,bytes32,uint256[])
CTF_PAYOUT_DENOMINATOR_SELECTOR = "dd34de67" # payoutDenominator(bytes32)
CTF_PAYOUT_NUMERATOR_SELECTOR = "0504c814"   # payoutNumerators(bytes32,uint256)
CTF_BALANCE_SELECTOR = "00fdd58e"           # balanceOf(address,uint256)
IS_APPROVED_FOR_ALL_SELECTOR = "e985e9c5"   # isApprovedForAll(address,address)
TRANSFER_TOPIC = keccak256(TRANSFER_SIGNATURE.encode("utf-8")).hex()

# Guard against a typo in the hard-coded table above.
for _sig, _expected in (
    (ROUTER_MERGE_SIGNATURE, ROUTER_MERGE_SELECTOR),
    (ROUTER_REDEEM_SIGNATURE, ROUTER_REDEEM_SELECTOR),
    (NEGRISK_CONVERT_SIGNATURE, NEGRISK_CONVERT_SELECTOR),
    (NEGRISK_MERGE_SIGNATURE, NEGRISK_MERGE_SELECTOR),
    (CTF_MERGE_SIGNATURE, CTF_MERGE_SELECTOR),
    (CTF_SPLIT_SIGNATURE, CTF_SPLIT_SELECTOR),
    (CTF_REDEEM_SIGNATURE, CTF_REDEEM_SELECTOR),
    (CTF_PAYOUT_DENOMINATOR_SIGNATURE, CTF_PAYOUT_DENOMINATOR_SELECTOR),
    (CTF_PAYOUT_NUMERATOR_SIGNATURE, CTF_PAYOUT_NUMERATOR_SELECTOR),
    (CTF_BALANCE_SIGNATURE, CTF_BALANCE_SELECTOR),
    (IS_APPROVED_FOR_ALL_SIGNATURE, IS_APPROVED_FOR_ALL_SELECTOR),
):
    assert _selector(_sig) == _expected, f"selector drift for {_sig}"


class SettlementRoute(str, Enum):
    """Which ledger a market's positions live on, and therefore how to settle."""

    CTF_MERGE = "ctf_merge"
    CTF_REDEEM = "ctf_redeem"
    ROUTER_MERGE = "router_merge"
    NEGRISK_CONVERT = "negrisk_convert"
    NEGRISK_MERGE = "negrisk_merge"
    UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------------
# Minimal ABI words (no web3 dependency)
# ---------------------------------------------------------------------------
def _clean_hex(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a hex string, got {type(value).__name__}")
    clean = value.strip()
    if clean[:2].lower() == "0x":
        clean = clean[2:]
    if not clean:
        raise ValueError(f"{field} is empty")
    if any(ch not in "0123456789abcdefABCDEF" for ch in clean):
        raise ValueError(f"{field} is not valid hex: {value!r}")
    return clean.lower()


def _word_uint(value: Any, field: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer, got {type(value).__name__}")
    if value < 0 or value > _UINT256_MAX:
        raise ValueError(f"{field} out of uint256 range: {value}")
    return value.to_bytes(32, "big")


def _word_bytes32(value: Any, field: str) -> bytes:
    clean = _clean_hex(value, field)
    if len(clean) != 64:
        raise ValueError(f"{field} must be 32 bytes (64 hex chars), got {len(clean) // 2}")
    return bytes.fromhex(clean)


def _word_address(value: Any, field: str) -> bytes:
    clean = _clean_hex(value, field)
    if len(clean) != 40:
        raise ValueError(f"{field} must be a 20-byte address, got {len(clean) // 2} bytes")
    return bytes.fromhex(clean).rjust(32, b"\x00")


def validate_bytes31(condition_id: str) -> bytes:
    """Narrow a padded v2 ``bytes32`` condition id to ``bytes31``.

    The Router ABI declares ``bytes31``. For a padded ``bytes32`` value, the
    final byte must be ``0x00``; otherwise narrowing silently truncates a
    meaningful byte. Returns the 31-byte value, not the ABI word.
    """
    raw = _word_bytes32(condition_id, "condition_id")
    if raw[-1] != 0:
        raise ValueError(
            "condition_id is not a valid bytes31: final byte must be 0x00, "
            f"got 0x{raw[-1]:02x} ({condition_id!r})"
        )
    return raw[:31]


def _word_bytes31(condition_id: str) -> bytes:
    """ABI word for ``bytes31``: 31 bytes left-aligned, one zero byte of padding."""
    return validate_bytes31(condition_id) + b"\x00"


def amount_to_6dp(shares: Any) -> int:
    """Convert a share amount to 6-decimal base units with ROUND_DOWN.

    ROUND_DOWN is deliberate: claiming more base units than the holder owns is
    a guaranteed revert. Floats are routed through ``repr`` so ``0.1`` becomes
    ``Decimal("0.1")`` rather than its binary expansion.
    """
    try:
        if isinstance(shares, Decimal):
            dec = shares
        elif isinstance(shares, float):
            dec = Decimal(repr(shares))
        else:
            dec = Decimal(str(shares))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid share amount: {shares!r}") from exc

    if not dec.is_finite():
        raise ValueError(f"invalid share amount: {shares!r}")
    if dec < 0:
        raise ValueError(f"share amount cannot be negative: {shares!r}")
    try:
        return int(dec.quantize(_ONE_SHARE, rounding=ROUND_DOWN) * SHARE_SCALE)
    except InvalidOperation as exc:
        raise ValueError(f"share amount too large: {shares!r}") from exc


def negrisk_index_set(question_indices: Iterable[int]) -> int:
    """Build the NegRiskAdapter ``indexSet`` bitmask from question indices.

    The adapter sets bit *i* when the caller wants to convert the NO position
    of question *i* (zero-based). ``[0, 2]`` therefore becomes ``0b101 == 5``.
    """
    bits = 0
    for index in question_indices:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"question index must be an integer, got {index!r}")
        if index < 0:
            raise ValueError(f"question index cannot be negative: {index}")
        if index >= 256:
            raise ValueError(f"question index out of uint256 bit range: {index}")
        bits |= 1 << index
    if bits == 0:
        raise ValueError("neg-risk conversion needs at least one NO question index")
    return bits


# ---------------------------------------------------------------------------
# Calldata builders
# ---------------------------------------------------------------------------
def build_router_merge(condition_id: str, amount_6dp: int) -> str:
    """Calldata for ``Router.merge(bytes31 conditionId, uint256 amount)``."""
    words = _word_bytes31(condition_id) + _word_uint(amount_6dp, "amount_6dp")
    return "0x" + ROUTER_MERGE_SELECTOR + words.hex()


def build_router_redeem(condition_id: str, outcome_index: int, amount_6dp: int) -> str:
    """Calldata for ``Router.redeem(bytes31,uint256 outcomeIndex,uint256 amount)``.

    V2 redemption uses ``0`` for YES and ``1`` for NO (the CTF index sets
    ``1`` and ``2`` do not apply here).
    """
    if outcome_index not in (0, 1):
        raise ValueError(f"outcome_index must be 0 (YES) or 1 (NO), got {outcome_index!r}")
    words = (
        _word_bytes31(condition_id)
        + _word_uint(outcome_index, "outcome_index")
        + _word_uint(amount_6dp, "amount_6dp")
    )
    return "0x" + ROUTER_REDEEM_SELECTOR + words.hex()


def build_negrisk_convert(market_id: str, index_sets: list[int], amount_6dp: int) -> str:
    """Calldata for NegRiskAdapter ``convertPositions(bytes32,uint256,uint256)``.

    ``index_sets`` is a list of zero-based question indices whose NO position
    should be converted; they are OR-ed into the adapter's single ``indexSet``
    bitmask (see :func:`negrisk_index_set`). Every bit must be below the
    market's ``questionCount`` or the adapter reverts with ``InvalidIndexSet``.
    ``amount_6dp`` is required by the verified ABI even though the routing
    ticket listed only two parameters.
    """
    if isinstance(index_sets, (int, bool)) or index_sets is None:
        raise ValueError("index_sets must be a list of zero-based question indices")
    index_set = negrisk_index_set(list(index_sets))
    words = (
        _word_bytes32(market_id, "market_id")
        + _word_uint(index_set, "index_set")
        + _word_uint(amount_6dp, "amount_6dp")
    )
    return "0x" + NEGRISK_CONVERT_SELECTOR + words.hex()


def build_negrisk_merge(condition_id: str, amount_6dp: int) -> str:
    """Calldata for NegRiskAdapter ``mergePositions(bytes32,uint256)``.

    This is the per-question complete-set merge used by the engine. It is a
    different call from :func:`build_negrisk_convert`, which converts NO
    inventory across questions. ``condition_id`` is the question id (the
    adapter's ``_questionId``); it is a full ``bytes32``, not ``bytes31``.
    """
    words = _word_bytes32(condition_id, "condition_id") + _word_uint(
        amount_6dp, "amount_6dp"
    )
    return "0x" + NEGRISK_MERGE_SELECTOR + words.hex()


def build_ctf_merge(
    condition_id: str,
    amount_6dp: int,
    *,
    collateral_token: str = USDC_E,
    index_sets: Sequence[int] = (1, 2),
    parent_collection_id: str = ZERO_BYTES32,
) -> str:
    """Calldata for CTF ``mergePositions(address,bytes32,bytes32,uint256[],uint256)``.

    Byte-for-byte compatible with ``PolygonCtfClient.encode_merge_positions``;
    the difference is that ``amount_6dp`` is already an integer, so no float
    rounding is involved.
    """
    if not index_sets:
        raise ValueError("index_sets partition cannot be empty")
    indices = [_word_uint(index, "index_set") for index in index_sets]
    words = (
        _word_address(collateral_token, "collateral_token")
        + _word_bytes32(parent_collection_id, "parent_collection_id")
        + _word_bytes32(condition_id, "condition_id")
        + _word_uint(160, "indexSets head offset")  # 5 static words before the tail
        + _word_uint(amount_6dp, "amount_6dp")
        + _word_uint(len(indices), "indexSets length")
        + b"".join(indices)
    )
    return "0x" + CTF_MERGE_SELECTOR + words.hex()


def build_ctf_split(
    condition_id: str,
    amount_6dp: int,
    *,
    collateral_token: str = USDC_E,
    index_sets: Sequence[int] = (1, 2),
    parent_collection_id: str = ZERO_BYTES32,
) -> str:
    """Calldata for CTF ``splitPosition(address,bytes32,bytes32,uint256[],uint256)``.

    Byte-for-byte compatible with ``PolygonCtfClient.encode_split_positions``;
    ``amount_6dp`` is in 6-decimal base units.
    """
    if not index_sets:
        raise ValueError("index_sets partition cannot be empty")
    indices = [_word_uint(index, "index_set") for index in index_sets]
    words = (
        _word_address(collateral_token, "collateral_token")
        + _word_bytes32(parent_collection_id, "parent_collection_id")
        + _word_bytes32(condition_id, "condition_id")
        + _word_uint(160, "indexSets head offset")
        + _word_uint(amount_6dp, "amount_6dp")
        + _word_uint(len(indices), "indexSets length")
        + b"".join(indices)
    )
    return "0x" + CTF_SPLIT_SELECTOR + words.hex()


def build_ctf_redeem(
    condition_id: str,
    index_sets: Sequence[int] = (1, 2),
    *,
    collateral_token: str = USDC_E,
    parent_collection_id: str = ZERO_BYTES32,
) -> str:
    """Calldata for CTF ``redeemPositions(address,bytes32,bytes32,uint256[])``.

    Redeems winning positions on Polygon PoS ConditionalTokens contract.
    Byte-for-byte compatible with ``PolygonCtfClient.encode_redeem_positions``.
    """
    if not index_sets:
        raise ValueError("index_sets partition cannot be empty")
    indices = [_word_uint(index, "index_set") for index in index_sets]
    words = (
        _word_address(collateral_token, "collateral_token")
        + _word_bytes32(parent_collection_id, "parent_collection_id")
        + _word_bytes32(condition_id, "condition_id")
        + _word_uint(128, "indexSets head offset")  # 4 static words before the dynamic array
        + _word_uint(len(indices), "indexSets length")
        + b"".join(indices)
    )
    return "0x" + CTF_REDEEM_SELECTOR + words.hex()


# ---------------------------------------------------------------------------
# Route classification
# ---------------------------------------------------------------------------
def _meta_value(meta: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in meta and meta[key] is not None:
            return meta[key]
    return default


def _has_verified_market_id(value: Any) -> bool:
    try:
        return _word_bytes32(value, "neg_risk_market_id") != bytes(32)
    except ValueError:
        return False


def classify_market(meta: dict | None) -> SettlementRoute:
    """Pick the settlement ledger from Gamma market metadata.

    Accepts both this module's snake_case keys and raw Gamma camelCase rows.

    * ``version == "v2"`` -> ``ROUTER_MERGE`` (PositionManager ledger).
    * ``version == "v1"`` and ``negRisk`` with a well-formed
      ``negRiskMarketID`` -> ``NEGRISK_CONVERT``.
    * ``version == "v1"`` otherwise -> ``CTF_MERGE``.
    * anything else (missing/unknown version) -> ``UNSUPPORTED``.

    A ``negRisk`` flag without a usable ``negRiskMarketID`` falls back to
    ``CTF_MERGE`` as specified, but is logged: that is a metadata
    inconsistency, and the CTF balance preflight will fail if the positions are
    really wrapped by the NegRiskAdapter.
    """
    if not meta:
        return SettlementRoute.UNSUPPORTED

    version = str(_meta_value(meta, "version", default="") or "").strip().lower()
    neg_risk = bool(_meta_value(meta, "neg_risk", "negRisk", default=False))
    market_id = _meta_value(meta, "neg_risk_market_id", "negRiskMarketID")

    if version == "v2":
        return SettlementRoute.ROUTER_MERGE

    if version == "v1":
        if neg_risk:
            if _has_verified_market_id(market_id):
                return SettlementRoute.NEGRISK_CONVERT
            logger.warning(
                "neg-risk market has no usable negRiskMarketID; "
                "falling back to CTF_MERGE (settlement will be rejected if "
                "positions are adapter-wrapped)"
            )
        return SettlementRoute.CTF_MERGE

    logger.warning(f"unsupported Gamma market version for settlement: {version!r}")
    return SettlementRoute.UNSUPPORTED


def classify_merge_route(meta: dict | None) -> SettlementRoute:
    """Pick the *complete-set merge* primitive for a market.

    ``classify_market`` answers the conversion question (v1 neg-risk ->
    ``NEGRISK_CONVERT``). A pair merge on a v1 neg-risk market is the adapter's
    ``mergePositions(questionId, amount)`` instead, so the engine uses this
    function. Version/neg-risk handling is otherwise identical.
    """
    route = classify_market(meta)
    if route is SettlementRoute.NEGRISK_CONVERT:
        return SettlementRoute.NEGRISK_MERGE
    return route


def classify_redeem_route(meta: dict | None) -> SettlementRoute:
    """Pick the *redemption* primitive for an expired/resolving market.

    * ``version == "v2"`` -> ``ROUTER_MERGE`` (Router.redeem / merge).
    * ``version == "v1"`` (binary or neg-risk) -> ``CTF_REDEEM``
      (``ConditionalTokens.redeemPositions``).
    * anything else -> ``UNSUPPORTED``.
    """
    if not meta:
        return SettlementRoute.UNSUPPORTED

    version = str(_meta_value(meta, "version", default="") or "").strip().lower()
    if version == "v2":
        return SettlementRoute.ROUTER_MERGE
    if version == "v1":
        return SettlementRoute.CTF_REDEEM

    logger.warning(f"unsupported Gamma market version for settlement redemption: {version!r}")
    return SettlementRoute.UNSUPPORTED


# ---------------------------------------------------------------------------
# Read-only verification
# ---------------------------------------------------------------------------
def derive_ctf_position_id(
    condition_id: str,
    index_set: int,
    collateral_token: str = USDC_E,
    parent_collection_id: str = ZERO_BYTES32,
) -> int:
    """ERC-1155 position id minted by CTF, per the CTF collection math.

    ``collectionId = keccak256(abi.encodePacked(parent, conditionId, indexSet))``
    ``positionId  = keccak256(abi.encodePacked(collateralToken, collectionId))``
    """
    collection_id = keccak256(
        _word_bytes32(parent_collection_id, "parent_collection_id")
        + _word_bytes32(condition_id, "condition_id")
        + _word_uint(index_set, "index_set")
    )
    collateral = _word_address(collateral_token, "collateral_token")[12:]
    return int.from_bytes(keccak256(collateral + collection_id), "big")


def _token_id(value: Any) -> int:
    """Parse a Gamma token id (decimal string) or a hex/uint token id."""
    if isinstance(value, bool):
        raise ValueError("token id cannot be a bool")
    if isinstance(value, int):
        token = value
    elif isinstance(value, str):
        text = value.strip()
        token = int(text, 16) if text[:2].lower() == "0x" else int(text)
    else:
        raise ValueError(f"unsupported token id: {value!r}")
    if token < 0 or token > _UINT256_MAX:
        raise ValueError(f"token id out of uint256 range: {value!r}")
    return token


def _eth_call_uint(chain_client: "ChainClient", to: str, data: str) -> int:
    result = chain_client.eth_call(to=to, data=data)
    if not result or result == "0x":
        raise RuntimeError(f"eth_call to {to} returned no data")
    return int(result, 16)


def _is_approved_for_all(
    chain_client: "ChainClient", token_contract: str, holder: str, operator: str
) -> bool:
    data = (
        "0x"
        + IS_APPROVED_FOR_ALL_SELECTOR
        + _word_address(holder, "holder").hex()
        + _word_address(operator, "operator").hex()
    )
    return _eth_call_uint(chain_client, token_contract, data) != 0


def _ctf_balance(chain_client: "ChainClient", holder: str, token_id: int) -> int:
    data = (
        "0x"
        + CTF_BALANCE_SELECTOR
        + _word_address(holder, "holder").hex()
        + _word_uint(token_id, "token_id").hex()
    )
    return _eth_call_uint(chain_client, CTF, data)


# ---------------------------------------------------------------------------
# Resolution evidence - the authority for writing a position off
# ---------------------------------------------------------------------------
class CtfOutcomeStatus(str, Enum):
    """On-chain outcome of one held CTF position token."""

    WON = "won"
    LOST = "lost"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CtfOutcomeEvidence:
    """Read-only resolution evidence for one held CTF outcome token."""

    status: CtfOutcomeStatus
    unit_payout: float
    payout_denominator: int
    reason: str

    @property
    def verified(self) -> bool:
        """True only when the chain resolved the condition and mapped the token."""
        return self.status in (CtfOutcomeStatus.WON, CtfOutcomeStatus.LOST)

    @property
    def verified_loss(self) -> bool:
        """True only for a chain-verified zero payout; never for missing data."""
        return self.status is CtfOutcomeStatus.LOST


def _ctf_token_ids(condition_id: str, meta: dict | None) -> list[int]:
    """Binary CTF position ids, from Gamma metadata when it is present."""
    clob_ids = (meta or {}).get("clob_token_ids")
    if clob_ids:
        token_ids = [_token_id(token) for token in clob_ids][:2]
        if token_ids:
            return token_ids
    return [
        derive_ctf_position_id(condition_id, 1),
        derive_ctf_position_id(condition_id, 2),
    ]


def _ctf_payout_denominator(chain_client: "ChainClient", condition_id: str) -> int:
    data = (
        "0x"
        + CTF_PAYOUT_DENOMINATOR_SELECTOR
        + _word_bytes32(condition_id, "condition_id").hex()
    )
    return _eth_call_uint(chain_client, CTF, data)


def _ctf_payout_numerator(
    chain_client: "ChainClient", condition_id: str, outcome_index: int
) -> int:
    data = (
        "0x"
        + CTF_PAYOUT_NUMERATOR_SELECTOR
        + _word_bytes32(condition_id, "condition_id").hex()
        + _word_uint(outcome_index, "outcome_index").hex()
    )
    return _eth_call_uint(chain_client, CTF, data)


def verify_ctf_outcome(
    condition_id: str,
    token_id: str | int,
    chain_client: "ChainClient",
    *,
    meta: dict | None = None,
) -> CtfOutcomeEvidence:
    """Chain-verified outcome for one held CTF token, without trusting Gamma.

    Uses the same read-only primitives as ``verify_route``'s ``CTF_REDEEM``
    branch: ``payoutDenominator`` proves the condition resolved, and
    ``payoutNumerators`` give this token's payout per share. An unresolved
    condition, an unmappable token, or any ``eth_call`` failure returns
    ``UNRESOLVED``/``UNKNOWN`` - never a loss - so a caller cannot write a
    position off from absent market data.
    """
    try:
        _word_bytes32(condition_id, "condition_id")
    except ValueError as exc:
        return CtfOutcomeEvidence(CtfOutcomeStatus.UNKNOWN, 0.0, 0, str(exc))
    try:
        held = _token_id(token_id)
    except ValueError as exc:
        return CtfOutcomeEvidence(CtfOutcomeStatus.UNKNOWN, 0.0, 0, f"invalid token id: {exc}")

    try:
        denominator = _ctf_payout_denominator(chain_client, condition_id)
    except Exception as exc:  # noqa: BLE001 - RPC failure is not resolution evidence
        return CtfOutcomeEvidence(
            CtfOutcomeStatus.UNKNOWN, 0.0, 0, f"CTF payoutDenominator check failed: {exc}"
        )
    if denominator == 0:
        return CtfOutcomeEvidence(
            CtfOutcomeStatus.UNRESOLVED,
            0.0,
            0,
            "condition has not been resolved on CTF (payoutDenominator == 0)",
        )

    try:
        token_ids = _ctf_token_ids(condition_id, meta)
    except (TypeError, ValueError) as exc:
        return CtfOutcomeEvidence(
            CtfOutcomeStatus.UNKNOWN, 0.0, denominator, f"invalid clob_token_ids in metadata: {exc}"
        )
    try:
        outcome_index = token_ids.index(held)
    except ValueError:
        return CtfOutcomeEvidence(
            CtfOutcomeStatus.UNKNOWN,
            0.0,
            denominator,
            f"held token {held} is not one of the market's CTF position ids",
        )

    try:
        numerator = _ctf_payout_numerator(chain_client, condition_id, outcome_index)
    except Exception as exc:  # noqa: BLE001 - RPC failure is not resolution evidence
        return CtfOutcomeEvidence(
            CtfOutcomeStatus.UNKNOWN, 0.0, denominator, f"CTF payoutNumerators check failed: {exc}"
        )

    if numerator > 0:
        return CtfOutcomeEvidence(
            CtfOutcomeStatus.WON,
            numerator / denominator,
            denominator,
            f"CTF resolved: outcome {outcome_index} pays {numerator}/{denominator}",
        )
    return CtfOutcomeEvidence(
        CtfOutcomeStatus.LOST,
        0.0,
        denominator,
        f"CTF resolved: outcome {outcome_index} pays 0/{denominator}",
    )


# ---------------------------------------------------------------------------
# On-chain flight - the latency a merge/split/redeem pays before it lands
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OnchainFlight:
    """Measured time an on-chain settlement call needs before it can land.

    A broadcast cannot change state before one RPC round trip is spent and
    before the next block is produced. Both components are measured from the
    endpoint the write will be sent to; a component that cannot be measured is
    named in ``gaps`` and contributes no time, so callers queue the action and
    log the gap instead of assuming the latency away.
    """

    rpc_rtt_s: float = 0.0
    block_interval_s: float = 0.0
    gaps: tuple[str, ...] = ()

    @property
    def total_s(self) -> float:
        return self.rpc_rtt_s + self.block_interval_s

    @property
    def measured(self) -> bool:
        return not self.gaps


def measure_onchain_flight(chain_client: Any) -> OnchainFlight:
    """Measure one RPC round trip and the current block interval.

    Read-only: ``eth_blockNumber`` times the client's own hot path (including
    its rate-limit spacing and failover), and the two most recent block
    timestamps give the interval the next transaction has to wait for. A probe
    failure is reported in ``gaps``, never silently treated as zero latency.
    """
    rpc = getattr(chain_client, "_rpc", None)
    if rpc is None:
        return OnchainFlight(gaps=("no RPC client available",))

    gaps: list[str] = []
    rtt_s = 0.0
    head_number: int | None = None
    started = time.perf_counter()
    try:
        head_number = int(str(rpc("eth_blockNumber", [])), 16)
        rtt_s = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 - a failed probe is a measured gap
        gaps.append(f"RPC round trip unmeasured: {exc}")

    block_interval_s = 0.0
    try:
        head = rpc(
            "eth_getBlockByNumber",
            [hex(head_number) if head_number is not None else "latest", False],
        )
        if not isinstance(head, dict) or "timestamp" not in head:
            raise ValueError("latest block has no timestamp")
        number = int(str(head.get("number")), 16)
        parent = rpc("eth_getBlockByNumber", [hex(number - 1), False])
        if not isinstance(parent, dict) or "timestamp" not in parent:
            raise ValueError("parent block has no timestamp")
        delta = int(str(head["timestamp"]), 16) - int(str(parent["timestamp"]), 16)
        if delta <= 0:
            raise ValueError(f"non-positive block interval {delta}s")
        block_interval_s = float(delta)
    except Exception as exc:  # noqa: BLE001 - a failed probe is a measured gap
        gaps.append(f"block interval unmeasured: {exc}")

    return OnchainFlight(rtt_s, block_interval_s, tuple(gaps))


def verify_route(
    route: SettlementRoute | str,
    condition_id: str,
    holder: str,
    chain_client: "ChainClient",
    *,
    meta: dict | None = None,
) -> tuple[bool, str]:
    """Read-only preflight for one settlement route.

    Returns ``(True, reason)`` only when the on-chain state proves the call is
    likely to succeed. Any ``eth_call`` failure becomes ``(False, reason)`` so
    callers can skip settlement instead of broadcasting blind.

    ``meta`` (Gamma metadata from ``GammaClient.market_meta``) lets the checks
    use the authoritative ``clob_token_ids``/``position_ids`` instead of
    deriving them; the four positional parameters remain sufficient otherwise.

    Checks per route:

    * ``CTF_MERGE`` - CTF ``balanceOf(holder, tokenId)`` for both binary
      outcomes (derived, or taken from ``clob_token_ids``).
    * ``NEGRISK_CONVERT`` - CTF ``isApprovedForAll(holder, NEGRISK_ADAPTER)``,
      required because the adapter pulls the NO positions.
    * ``ROUTER_MERGE`` - PositionManager
      ``isApprovedForAll(holder, ROUTER)``. ``getPayout`` is deliberately not
      read: it reverts before resolution, and a merge must be possible while
      the market is still trading.
    """
    try:
        selected = SettlementRoute(route)
    except ValueError:
        return False, f"unknown settlement route: {route!r}"

    if selected is SettlementRoute.UNSUPPORTED:
        return False, "market version has no supported settlement route"

    try:
        _word_address(holder, "holder")
    except ValueError as exc:
        return False, str(exc)

    if selected is SettlementRoute.CTF_MERGE:
        try:
            _word_bytes32(condition_id, "condition_id")
        except ValueError as exc:
            return False, str(exc)

        token_ids: list[int] = []
        clob_ids = (meta or {}).get("clob_token_ids")
        if clob_ids:
            try:
                token_ids = [_token_id(token) for token in clob_ids][:2]
            except (TypeError, ValueError) as exc:
                return False, f"invalid clob_token_ids in metadata: {exc}"
        if not token_ids:
            try:
                token_ids = [derive_ctf_position_id(condition_id, 1),
                             derive_ctf_position_id(condition_id, 2)]
            except ValueError as exc:
                return False, str(exc)
        if len(token_ids) < 2:
            return False, "CTF merge needs two outcome token ids"

        for token_id in token_ids:
            try:
                balance = _ctf_balance(chain_client, holder, token_id)
            except Exception as exc:  # noqa: BLE001 - RPC failure must not settle
                return False, f"CTF balanceOf({token_id}) failed: {exc}"
            if balance <= 0:
                return False, f"holder has no CTF balance for token {token_id}"
        return True, "holder holds both CTF outcome tokens"

    if selected is SettlementRoute.CTF_REDEEM:
        try:
            _word_bytes32(condition_id, "condition_id")
        except ValueError as exc:
            return False, str(exc)

        # 1. On-Chain Resolution Check: payoutDenominator must be > 0
        try:
            payout_denom = _ctf_payout_denominator(chain_client, condition_id)
        except Exception as exc:  # noqa: BLE001
            return False, f"CTF payoutDenominator check failed: {exc}"

        if payout_denom == 0:
            return False, "condition has not been resolved on CTF (payoutDenominator == 0)"

        # 2. Check token balances
        try:
            token_ids = _ctf_token_ids(condition_id, meta)
        except (TypeError, ValueError) as exc:
            return False, f"invalid clob_token_ids in metadata: {exc}"

        total_balance = 0
        winning_balance = 0
        for idx, token_id in enumerate(token_ids):
            try:
                bal = _ctf_balance(chain_client, holder, token_id)
                num = _ctf_payout_numerator(chain_client, condition_id, idx)
            except Exception as exc:  # noqa: BLE001
                return False, f"CTF balance/numerator check failed: {exc}"

            total_balance += bal
            if num > 0 and bal > 0:
                winning_balance += bal

        if total_balance <= 0:
            return False, f"holder has no position balance for condition {condition_id}"
        if winning_balance <= 0:
            return False, "holder has position balance but winning outcome numerator is 0"

        return True, "condition resolved on CTF and holder has redeemable winning balance"

    if selected in (
        SettlementRoute.NEGRISK_CONVERT,
        SettlementRoute.NEGRISK_MERGE,
    ):
        try:
            approved = _is_approved_for_all(
                chain_client, CTF, holder, NEGRISK_ADAPTER
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"CTF isApprovedForAll check failed: {exc}"
        if not approved:
            return False, (
                "holder has not approved the NegRiskAdapter on CTF "
                "(CTF.setApprovalForAll(NEGRISK_ADAPTER, true) is required)"
            )
        return True, "NegRiskAdapter is approved to move the holder's CTF positions"

    # ROUTER_MERGE
    try:
        approved = _is_approved_for_all(
            chain_client, POSITION_MANAGER, holder, ROUTER
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"PositionManager isApprovedForAll check failed: {exc}"
    if not approved:
        return False, (
            "holder has not approved the Router on PositionManager "
            "(PositionManager.setApprovalForAll(ROUTER, true) is required)"
        )

    # Do not gate on PositionManager.getPayout: it reverts before resolution,
    # and a merge must be possible while the market is still trading. The
    # approval check above plus eth_estimateGas are the real guards.
    return True, "Router is approved for the holder's v2 positions"


# ---------------------------------------------------------------------------
# Receipt helpers
# ---------------------------------------------------------------------------
def receipt_succeeded(receipt: dict | None) -> bool:
    """True only for a mined receipt whose status is ``0x1``."""
    if not isinstance(receipt, dict):
        return False
    status = receipt.get("status")
    if status is None:
        return False
    try:
        return int(str(status), 16) == 1
    except (TypeError, ValueError):
        return False


def decode_erc20_transfer_total(
    receipt: dict | None,
    token: str,
    recipient: str | None = None,
) -> int:
    """Sum ERC-20 ``Transfer`` amounts to ``recipient`` for ``token``.

    Useful for confirming the collateral returned by a merge/redeem
    (``PUSD`` for v2, ``USDC_E`` for v1) without trusting the preflight
    estimate. Returns 0 for a missing/failed receipt or no matching log.
    """
    if not isinstance(receipt, dict) or not receipt_succeeded(receipt):
        return 0
    token_lc = token.lower()
    recipient_topic = None
    if recipient is not None:
        recipient_topic = "0x" + _word_address(recipient, "recipient").hex()

    total = 0
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict):
            continue
        if str(log.get("address", "")).lower() != token_lc:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        if str(topics[0]).lower().removeprefix("0x") != TRANSFER_TOPIC:
            continue
        if recipient_topic is not None and str(topics[2]).lower() != recipient_topic:
            continue
        raw = str(log.get("data", "0x"))
        if not raw or raw == "0x":
            continue
        try:
            total += int(raw, 16)
        except ValueError:
            continue
    return total
