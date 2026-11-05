"""Polymarket Relayer client for Deposit Wallet (POLY_1271) call batches.

Scope
-----
Pure request construction plus an injectable HTTP session. Nothing in this module
submits a transaction on import: ``submit_wallet_calls`` only runs when a caller
explicitly invokes it, and no module-level client is created.

Verified against the mirrored primary docs
(``data/raw/docs/site/trading/wallets-auth.md``, "Execute Gasless Transactions"):
  * ``POST /submit`` body ``{type: "WALLET", from, to, nonce, signature,
    metadata, depositWalletParams: {depositWallet, deadline, calls}}`` where
    ``to`` is the relayer's wallet entrypoint ``0x0000...Cc07`` and each call is
    ``{target, value: "0", data}``.
  * Nonce: ``GET /v1/account/transactions/params?address=<signer>&type=WALLET``
    authenticated with ``RELAYER_API_KEY`` + ``RELAYER_API_KEY_ADDRESS``.
  * Confirmation: poll the transaction until ``STATE_CONFIRMED``; ``STATE_FAILED``
    and ``STATE_INVALID`` are terminal failures.
  * The ``Batch`` typed data uses the Deposit Wallet EIP-712 domain
    (name "DepositWallet", version "1", chainId 137, verifyingContract = wallet)
    with ``Call {address target, uint256 value, bytes data}`` and
    ``Batch {address wallet, uint256 nonce, uint256 deadline, Call[] calls}``.

The documented confirmation URL for WALLET batches is
``/v1/account/transactions/<id>``; the WALLET-CREATE flow uses the legacy
``/transaction?id=<id>``. Both shapes are accepted by :func:`parse_transaction`;
``poll_transaction`` uses the WALLET batch endpoint and accepts a ``path``
override for the legacy one.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import requests

from polymarketbot.risk.fast_signer import (
    compute_domain_separator,
    ecdsa_sign_digest,
    encode_address,
    keccak256,
)


RELAYER_HOST = "https://relayer-v2.polymarket.com"
"""Relayer entrypoint that dispatches WALLET batches to a Deposit Wallet."""
WALLET_RELAYER_TARGET = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"

SUBMIT_PATH = "/submit"
PARAMS_PATH = "/v1/account/transactions/params"
TRANSACTION_PATH = "/v1/account/transactions/{transaction_id}"
"""Legacy lookup used by the WALLET-CREATE confirmation flow."""
LEGACY_TRANSACTION_PATH = "/transaction?id={transaction_id}"

WALLET_ACTION_TYPE = "WALLET"
DEPOSIT_WALLET_DOMAIN_NAME = "DepositWallet"
DEPOSIT_WALLET_DOMAIN_VERSION = "1"
POLYGON_CHAIN_ID = 137

STATE_NEW = "STATE_NEW"
STATE_CONFIRMED = "STATE_CONFIRMED"
TERMINAL_FAILURE_STATES = frozenset({"STATE_FAILED", "STATE_INVALID"})

CALL_TYPE = [
    {"name": "target", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "data", "type": "bytes"},
]
BATCH_TYPE = [
    {"name": "wallet", "type": "address"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
    {"name": "calls", "type": "Call[]"},
]
CALL_TYPEHASH = keccak256(b"Call(address target,uint256 value,bytes data)")
# EIP-712 typehash includes referenced struct definitions, sorted by name:
# "Batch(...)" + "Call(...)".
BATCH_TYPEHASH = keccak256(
    b"Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)"
    b"Call(address target,uint256 value,bytes data)"
)


class RelayerError(RuntimeError):
    """A Relayer call failed, or returned a body we cannot trust."""


def _env_file() -> dict[str, str]:
    """Read `.env` without adding a dependency. Absent file is fine."""
    path = Path(".env")
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _lookup_env(*names: str) -> str:
    file_values = _env_file()
    for name in names:
        value = os.environ.get(name) or file_values.get(name, "")
        if value:
            return value
    return ""


@dataclass(frozen=True)
class RelayerCredentials:
    """Relayer API key pair (Settings -> API Keys -> Relayer API Keys)."""

    api_key: str = ""
    api_key_address: str = ""

    @classmethod
    def from_env(cls) -> "RelayerCredentials":
        return cls(
            api_key=_lookup_env("POLYMARKET_RELAYER_API_KEY"),
            api_key_address=_lookup_env("POLYMARKET_RELAYER_API_KEY_ADDRESS"),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key_address)


@dataclass(frozen=True)
class RelayerCall:
    """One contract call inside a Deposit Wallet batch."""

    target: str
    data: str
    value: int = 0

    def __post_init__(self) -> None:
        if int(self.value) < 0:
            raise ValueError(f"call value must be non-negative, got {self.value}")
        encode_address(self.target)  # validates the address
        if not str(self.data).startswith("0x"):
            raise ValueError(f"call data must be 0x-prefixed hex, got {self.data!r}")

    def to_payload(self) -> dict[str, str]:
        """Relayer JSON shape: ``value`` is a decimal string, ``data`` is hex."""
        return {"target": self.target, "value": str(int(self.value)), "data": self.data}


@dataclass(frozen=True)
class RelayerTransaction:
    """Normalised Relayer transaction state."""

    transaction_id: str
    state: str
    transaction_hash: str = ""
    error_msg: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def confirmed(self) -> bool:
        return self.state == STATE_CONFIRMED

    @property
    def failed(self) -> bool:
        return self.state in TERMINAL_FAILURE_STATES


@dataclass(frozen=True)
class RelayerSubmission:
    """Response of ``POST /submit``."""

    transaction_id: str
    state: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


# ---------------------------------------------------------------------------
# Typed data
# ---------------------------------------------------------------------------
def _hex_bytes(value: str) -> bytes:
    text = str(value).removeprefix("0x").removeprefix("0X")
    if len(text) % 2:
        raise ValueError(f"hex data must have an even number of digits, got {value!r}")
    return bytes.fromhex(text)


def _uint_word(value: Any) -> bytes:
    number = int(value)
    if number < 0:
        raise ValueError(f"uint256 field must be non-negative, got {number}")
    return number.to_bytes(32, "big")


def build_batch_typed_data(
    *,
    deposit_wallet: str,
    nonce: int,
    deadline: int,
    calls: Sequence[RelayerCall],
    chain_id: int = POLYGON_CHAIN_ID,
) -> dict[str, Any]:
    """Build the Deposit Wallet ``Batch`` EIP-712 typed data (as documented)."""
    if not calls:
        raise ValueError("a Deposit Wallet batch needs at least one call")
    return {
        "domain": {
            "name": DEPOSIT_WALLET_DOMAIN_NAME,
            "version": DEPOSIT_WALLET_DOMAIN_VERSION,
            "chainId": int(chain_id),
            "verifyingContract": deposit_wallet,
        },
        "types": {
            "Call": [dict(field) for field in CALL_TYPE],
            "Batch": [dict(field) for field in BATCH_TYPE],
        },
        "primaryType": "Batch",
        "message": {
            "wallet": deposit_wallet,
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": [
                {
                    "target": call.target,
                    "value": int(call.value),
                    "data": call.data,
                }
                for call in calls
            ],
        },
    }


def hash_batch_typed_data(typed_data: dict[str, Any]) -> bytes:
    """Return the 32-byte EIP-712 digest of a ``Batch`` typed-data dict."""
    if typed_data.get("primaryType") != "Batch":
        raise ValueError("typed data primaryType must be 'Batch'")
    message = typed_data["message"]
    domain = typed_data["domain"]

    calls_hash = keccak256(
        b"".join(
            keccak256(
                CALL_TYPEHASH
                + encode_address(call["target"])
                + _uint_word(call["value"])
                + keccak256(_hex_bytes(call["data"]))
            )
            for call in message["calls"]
        )
    )
    struct_hash = keccak256(
        BATCH_TYPEHASH
        + encode_address(message["wallet"])
        + _uint_word(message["nonce"])
        + _uint_word(message["deadline"])
        + calls_hash
    )
    domain_separator = compute_domain_separator(
        name=str(domain["name"]),
        version=str(domain["version"]),
        chain_id=int(domain["chainId"]),
        verifying_contract=str(domain["verifyingContract"]),
    )
    return keccak256(b"\x19\x01" + domain_separator + struct_hash)


def sign_batch_typed_data(private_key: str, typed_data: dict[str, Any]) -> str:
    """Sign the ``Batch`` typed data with the Deposit Wallet's controlling EOA."""
    clean = str(private_key).removeprefix("0x").removeprefix("0X")
    if len(clean) != 64:
        raise ValueError("private key must be 32 bytes of hex")
    digest = hash_batch_typed_data(typed_data)
    r, s, v = ecdsa_sign_digest(digest, bytes.fromhex(clean))
    return "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])).hex()


# ---------------------------------------------------------------------------
# Submit body
# ---------------------------------------------------------------------------
def build_wallet_submit_body(
    *,
    signer: str,
    deposit_wallet: str,
    nonce: int,
    deadline: int,
    signature: str,
    calls: Sequence[RelayerCall],
    metadata: str,
    to: str = WALLET_RELAYER_TARGET,
) -> dict[str, Any]:
    """Build the exact ``POST /submit`` JSON body for a WALLET batch."""
    if not calls:
        raise ValueError("a Deposit Wallet batch needs at least one call")
    encode_address(signer)
    encode_address(deposit_wallet)
    encode_address(to)
    return {
        "type": WALLET_ACTION_TYPE,
        "from": signer,
        "to": to,
        "nonce": str(int(nonce)),
        "signature": signature,
        "metadata": metadata,
        "depositWalletParams": {
            "depositWallet": deposit_wallet,
            "deadline": str(int(deadline)),
            "calls": [call.to_payload() for call in calls],
        },
    }


def parse_transaction(payload: Any) -> RelayerTransaction:
    """Normalise a Relayer transaction response (object or legacy list shape)."""
    item = payload
    if isinstance(item, list):
        if not item:
            raise RelayerError("relayer returned an empty transaction list")
        item = item[0]
    if not isinstance(item, dict):
        raise RelayerError(f"unexpected relayer transaction body: {payload!r}")
    transaction_id = item.get("transaction_id") or item.get("transactionID") or ""
    transaction_hash = item.get("transaction_hash") or item.get("transactionHash") or ""
    state = item.get("state") or ""
    if not transaction_id or not state:
        raise RelayerError(f"relayer transaction body missing id/state: {item!r}")
    return RelayerTransaction(
        transaction_id=str(transaction_id),
        state=str(state),
        transaction_hash=str(transaction_hash),
        error_msg=item.get("error_msg"),
        raw=dict(item),
    )


def parse_submission(payload: Any) -> RelayerSubmission:
    """Normalise a ``POST /submit`` response."""
    if not isinstance(payload, dict):
        raise RelayerError(f"unexpected relayer submit body: {payload!r}")
    transaction_id = payload.get("transactionID") or payload.get("transaction_id") or ""
    if not transaction_id:
        raise RelayerError(f"relayer submit body has no transaction id: {payload!r}")
    return RelayerSubmission(
        transaction_id=str(transaction_id),
        state=str(payload.get("state") or ""),
        raw=dict(payload),
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@dataclass
class RelayerClient:
    """Thin Relayer REST client; the session is injectable for tests.

    No call is made at construction. Every method is an explicit request, so the
    bot's live gates are unchanged: constructing this object cannot submit.
    """

    credentials: RelayerCredentials
    host: str = RELAYER_HOST
    session: requests.Session = field(default_factory=requests.Session)
    timeout: float = 15.0

    def _headers(self) -> dict[str, str]:
        if not self.credentials.is_configured:
            raise RelayerError(
                "Relayer API key is not configured "
                "(POLYMARKET_RELAYER_API_KEY / POLYMARKET_RELAYER_API_KEY_ADDRESS)"
            )
        return {
            "Content-Type": "application/json",
            "RELAYER_API_KEY": self.credentials.api_key,
            "RELAYER_API_KEY_ADDRESS": self.credentials.api_key_address,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = self.host.rstrip("/") + path
        try:
            response = self.session.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RelayerError(f"{method} {path} failed: {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 300 or status == 0:
            text = str(getattr(response, "text", "") or "")[:500]
            raise RelayerError(f"{method} {path} -> HTTP {status}: {text}")
        try:
            return response.json()
        except ValueError as exc:
            raise RelayerError(f"{method} {path} returned non-JSON body") from exc

    def get_wallet_nonce(self, signer_address: str) -> int:
        """Fetch the fresh ``WALLET`` nonce for the signer EOA."""
        payload = self._request(
            "GET",
            PARAMS_PATH,
            params={"address": signer_address, "type": WALLET_ACTION_TYPE},
        )
        if not isinstance(payload, dict) or "nonce" not in payload:
            raise RelayerError(f"relayer nonce body is unusable: {payload!r}")
        try:
            nonce = int(str(payload["nonce"]))
        except (TypeError, ValueError) as exc:
            raise RelayerError(f"relayer nonce is not an integer: {payload['nonce']!r}") from exc
        if nonce < 0:
            raise RelayerError(f"relayer nonce must be non-negative, got {nonce}")
        return nonce

    def submit_wallet_calls(
        self,
        calls: Iterable[RelayerCall],
        *,
        signer: str,
        deposit_wallet: str,
        nonce: int,
        deadline: int,
        signature: str,
        metadata: str = "",
    ) -> RelayerSubmission:
        """Submit one signed Deposit Wallet batch (explicit call only)."""
        body = build_wallet_submit_body(
            signer=signer,
            deposit_wallet=deposit_wallet,
            nonce=nonce,
            deadline=deadline,
            signature=signature,
            calls=list(calls),
            metadata=metadata,
        )
        return parse_submission(self._request("POST", SUBMIT_PATH, json_body=body))

    def poll_transaction(
        self,
        transaction_id: str,
        *,
        interval: float = 1.0,
        timeout: float = 120.0,
        path: str = TRANSACTION_PATH,
    ) -> RelayerTransaction:
        """Poll until ``STATE_CONFIRMED``; raise on terminal failure or timeout."""
        deadline_at = time.monotonic() + float(timeout)
        while True:
            payload = self._request("GET", path.format(transaction_id=transaction_id))
            transaction = parse_transaction(payload)
            if transaction.confirmed:
                return transaction
            if transaction.failed:
                raise RelayerError(
                    f"relayer transaction {transaction_id} ended {transaction.state}"
                    + (f": {transaction.error_msg}" if transaction.error_msg else "")
                )
            if time.monotonic() >= deadline_at:
                raise RelayerError(
                    f"relayer transaction {transaction_id} still {transaction.state} "
                    f"after {timeout:.0f}s"
                )
            if interval > 0:
                time.sleep(interval)


# ---------------------------------------------------------------------------
# Module-level helpers (explicit calls; no default client is cached)
# ---------------------------------------------------------------------------
def get_wallet_nonce(
    signer_address: str,
    *,
    client: Optional[RelayerClient] = None,
    credentials: Optional[RelayerCredentials] = None,
    session: Optional[requests.Session] = None,
    host: str = RELAYER_HOST,
) -> int:
    """Convenience wrapper for :meth:`RelayerClient.get_wallet_nonce`."""
    client = client or RelayerClient(
        credentials=credentials or RelayerCredentials.from_env(),
        host=host,
        session=session or requests.Session(),
    )
    return client.get_wallet_nonce(signer_address)


def submit_wallet_calls(
    calls: Iterable[RelayerCall],
    *,
    signer: str,
    deposit_wallet: str,
    nonce: int,
    deadline: int,
    signature: str,
    metadata: str = "",
    client: Optional[RelayerClient] = None,
    credentials: Optional[RelayerCredentials] = None,
    session: Optional[requests.Session] = None,
    host: str = RELAYER_HOST,
) -> RelayerSubmission:
    """Convenience wrapper for :meth:`RelayerClient.submit_wallet_calls`."""
    client = client or RelayerClient(
        credentials=credentials or RelayerCredentials.from_env(),
        host=host,
        session=session or requests.Session(),
    )
    return client.submit_wallet_calls(
        calls,
        signer=signer,
        deposit_wallet=deposit_wallet,
        nonce=nonce,
        deadline=deadline,
        signature=signature,
        metadata=metadata,
    )


def poll_transaction(
    transaction_id: str,
    *,
    interval: float = 1.0,
    timeout: float = 120.0,
    path: str = TRANSACTION_PATH,
    client: Optional[RelayerClient] = None,
    credentials: Optional[RelayerCredentials] = None,
    session: Optional[requests.Session] = None,
    host: str = RELAYER_HOST,
) -> RelayerTransaction:
    """Convenience wrapper for :meth:`RelayerClient.poll_transaction`."""
    client = client or RelayerClient(
        credentials=credentials or RelayerCredentials.from_env(),
        host=host,
        session=session or requests.Session(),
    )
    return client.poll_transaction(
        transaction_id, interval=interval, timeout=timeout, path=path
    )
