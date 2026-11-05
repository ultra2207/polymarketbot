"""Transaction-lifecycle tests: endpoint resolution, error taxonomy, settlement
idempotency by hash, and the double-merge guard.

Every test runs against local fakes. Nothing here opens a socket: the RPC
transport is a scripted session, and the "chain" is an object that answers
exactly the calls the settlement client makes.
"""

from __future__ import annotations

import json
import time

import pytest

from polymarketbot.chain import (
    ALREADY_KNOWN,
    EXECUTION_REVERTED,
    NONCE_TOO_LOW,
    RATE_LIMIT,
    REPLACEMENT_UNDERPRICED,
    ChainClient,
    RpcError,
    classify_rpc_error,
    mask_url,
    normalise_endpoint,
    rpc_url,
)
from polymarketbot.risk.fast_signer import keccak256
from polymarketbot.trading.ctf import (
    MergeExecutionResult,
    PolygonCtfClient,
    SettlementOutcome,
)
from polymarketbot.trading.engine import RealMoneyTradingEngine
from polymarketbot.trading.settlement import ROUTER
from polymarketbot.trading.pending_settlements import (
    PENDING,
    REVERTED,
    SUCCESS,
    PendingSettlementStore,
)

TEST_KEY_HEX = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
CONDITION_ID = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
TOKEN_YES = "0x" + "11" * 32
TOKEN_NO = "0x" + "22" * 32

MINED = {"status": "0x1", "gasUsed": hex(60_000), "effectiveGasPrice": hex(100 * 10**9)}
REVERTED_RECEIPT = {"status": "0x0", "gasUsed": hex(60_000), "effectiveGasPrice": hex(100 * 10**9)}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeChain:
    """Answers exactly what PolygonCtfClient asks of a ChainClient."""

    def __init__(self) -> None:
        self.nonce = 7
        self.sent: list[str] = []
        self.send_errors: list[Exception] = []
        self.any_receipt: dict | None = None
        self.receipts: dict[str, dict] = {}
        self.mine_after_sends = 1
        self.receipt_calls = 0
        self.receipt_error: Exception | None = None

    def _rpc(self, method: str, params: list) -> object:
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(30 * 10**9), "timestamp": hex(int(time.time()))}
        raise AssertionError(f"unexpected RPC method in a settlement test: {method}")

    def get_transaction_count(self, address: str, block: str = "pending") -> int:
        return self.nonce

    def estimate_gas(self, tx: dict) -> int:
        return 80_000

    def eth_call(self, to: str, data: str, *, block: str | int = "latest") -> str:
        return hex(50 * 1_000_000)  # 50 shares held on both outcomes

    def send_raw_transaction(self, raw: str) -> str:
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append(raw)
        return "0x" + keccak256(bytes.fromhex(raw[2:])).hex()

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        self.receipt_calls += 1
        if self.receipt_error is not None:
            raise self.receipt_error
        # A receipt can only exist once the node holds the transaction.
        if len(self.sent) < self.mine_after_sends:
            return None
        if self.any_receipt is not None:
            return self.any_receipt
        return self.receipts.get(tx_hash)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Scripted transport: each `post` consumes the next scripted answer."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.urls: list[str] = []
        self.headers: dict[str, str] = {}

    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> FakeResponse:
        self.urls.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ctf(chain: FakeChain, store: PendingSettlementStore | None = None) -> PolygonCtfClient:
    ctf = PolygonCtfClient(
        private_key=TEST_KEY_HEX,
        dry_run=False,
        chain_client=chain,
        settlement_store=store,
    )
    ctf.get_dynamic_gas_price_gwei = lambda: 40.0  # no gas oracle traffic
    ctf.receipt_timeout_s = 0.05
    ctf.receipt_poll_interval_s = 0.001
    ctf.receipt_rebroadcast_wait_s = 0.05
    return ctf


@pytest.fixture(autouse=True)
def _no_pol_price_network(monkeypatch):
    monkeypatch.setattr(
        "polymarketbot.trading.ctf.get_dynamic_pol_usd_price", lambda: 0.1
    )


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_env(monkeypatch):
    monkeypatch.setattr("polymarketbot.chain._env_file", lambda: {})
    for name in ("POLYGON_RPC_URL", "POLYGON_RPC_URLS", "ETH_RPC_URL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_endpoint_prefers_polygon_rpc_url(_isolated_env):
    _isolated_env.setenv("POLYGON_RPC_URL", "https://polygon-mainnet.g.alchemy.com/v2/polygonkey0001/")
    _isolated_env.setenv("ETH_RPC_URL", "https://eth-mainnet.g.alchemy.com/v2/otherkey00001")

    url, keyed = rpc_url()

    assert keyed is True
    assert url == "https://polygon-mainnet.g.alchemy.com/v2/polygonkey0001"  # trailing slash gone


def test_endpoint_falls_back_to_eth_alias_on_polygon_host(_isolated_env):
    _isolated_env.setenv("ETH_RPC_URL", "https://eth-mainnet.g.alchemy.com/v2/ethalias000001")

    url, keyed = rpc_url()

    assert keyed is True
    # The alias is an Alchemy app key: the Polygon host is what must be dialled,
    # and health_check is what proves whether the app serves Polygon.
    assert url == "https://polygon-mainnet.g.alchemy.com/v2/ethalias000001"
    assert mask_url(url) == "https://polygon-mainnet.g.alchemy.com/v2/<key:etha...>"


def test_endpoint_accepts_bare_alchemy_key(_isolated_env):
    _isolated_env.setenv("POLYGON_RPC_URL", "AbCdEf0123456789AbCdEf0123456789")

    assert rpc_url()[0] == "https://polygon-mainnet.g.alchemy.com/v2/AbCdEf0123456789AbCdEf0123456789"


def test_endpoint_rejects_unusable_value_and_uses_free_fallback(_isolated_env):
    _isolated_env.setenv("POLYGON_RPC_URL", "not a url")
    _isolated_env.setenv("ETH_RPC_URL", "ftp://example.com/rpc")

    url, keyed = rpc_url()

    assert url == "https://polygon.drpc.org"
    assert keyed is False
    assert normalise_endpoint("  ") is None


def test_read_urls_include_configured_failover_but_writes_do_not(_isolated_env):
    _isolated_env.setenv("POLYGON_RPC_URL", "https://polygon-mainnet.g.alchemy.com/v2/primary000001")
    _isolated_env.setenv("POLYGON_RPC_URLS", "https://polygon-mainnet.g.alchemy.com/v2/backup0000001")

    client = ChainClient()

    assert client.write_url == "https://polygon-mainnet.g.alchemy.com/v2/primary000001"
    assert client.read_urls[0] == client.write_url
    assert "https://polygon-mainnet.g.alchemy.com/v2/backup0000001" in client.read_urls
    assert "https://polygon.drpc.org" in client.read_urls


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("eth_sendRawTransaction: HTTP 429 rate limited", RATE_LIMIT),
        ("{'code': -32000, 'message': 'nonce too low'}", NONCE_TOO_LOW),
        ("replacement transaction underpriced", REPLACEMENT_UNDERPRICED),
        ("execution reverted: CTF: insufficient balance", EXECUTION_REVERTED),
        ("already known", ALREADY_KNOWN),
        ("HTTP 503", "server_error"),
        ("ReadTimeout: connection timed out", "transport"),
        ("something else entirely", "unknown"),
    ],
)
def test_error_taxonomy(message, kind):
    assert classify_rpc_error(RuntimeError(message)) == kind


def test_rpc_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    session = FakeSession(
        [
            FakeResponse(429, text="rate limited"),
            FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": "0x89"}),
        ]
    )
    client = ChainClient(url="https://polygon.drpc.org", session=session, min_interval_s=0.0)

    assert client._rpc("eth_chainId", []) == "0x89"
    assert len(session.urls) == 2


def test_rpc_does_not_retry_definitive_errors(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    session = FakeSession(
        [FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nonce too low"}})]
    )
    client = ChainClient(url="https://polygon.drpc.org", session=session, min_interval_s=0.0)

    with pytest.raises(RpcError) as excinfo:
        client._rpc("eth_sendRawTransaction", ["0xdeadbeef"])

    assert excinfo.value.kind == NONCE_TOO_LOW
    assert len(session.urls) == 1  # a definitive answer is not retried


def test_rpc_write_never_fails_over(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    session = FakeSession([FakeResponse(503, text="unavailable")] * 4)
    client = ChainClient(url="https://polygon.drpc.org", session=session, min_interval_s=0.0)
    client.read_urls = ["https://polygon.drpc.org", "https://other.example/rpc"]

    with pytest.raises(RuntimeError):
        client._rpc("eth_sendRawTransaction", ["0xdeadbeef"])

    assert set(session.urls) == {"https://polygon.drpc.org"}


def test_health_check_requires_polygon_and_a_fresh_head(monkeypatch):
    now = int(time.time())
    session = FakeSession(
        [
            FakeResponse(200, {"result": "0x89"}),
            FakeResponse(200, {"result": {"timestamp": hex(now), "number": "0x1"}}),
        ]
    )
    client = ChainClient(url="https://polygon.drpc.org", session=session, min_interval_s=0.0)
    ok, reason = client.health_check()
    assert ok is True, reason

    session = FakeSession([FakeResponse(200, {"result": "0x1"})])
    client = ChainClient(url="https://eth-mainnet.example", session=session, min_interval_s=0.0)
    ok, reason = client.health_check()
    assert ok is False
    assert "wrong chain" in reason

    stale = FakeSession(
        [
            FakeResponse(200, {"result": "0x89"}),
            FakeResponse(200, {"result": {"timestamp": hex(now - 600)}}),
        ]
    )
    client = ChainClient(url="https://polygon.drpc.org", session=stale, min_interval_s=0.0)
    ok, reason = client.health_check()
    assert ok is False
    assert "stale head" in reason


# ---------------------------------------------------------------------------
# Settlement store: the hash is the identity
# ---------------------------------------------------------------------------


def test_store_is_idempotent_on_tx_hash():
    store = PendingSettlementStore()
    fields = dict(
        tx_hash="0xABC",
        raw_tx="0x02f8",
        nonce=3,
        condition_id=CONDITION_ID,
        kind="merge",
        amount=10.0,
    )

    assert store.record(**fields) is True
    assert store.record(**fields) is False
    assert store.count() == 1
    assert store.get("0xabc")["status"] == PENDING
    assert store.pending_for(CONDITION_ID)[0]["amount"] == 10.0

    assert store.resolve("0xabc", SUCCESS, MINED) is True
    assert store.resolve("0xabc", REVERTED, MINED) is False  # already terminal
    assert store.pending_for(CONDITION_ID) == []
    assert json.loads(store.get("0xabc")["receipt_json"])["status"] == "0x1"


# ---------------------------------------------------------------------------
# Settlement broadcast: ambiguous outcomes never become a second merge
# ---------------------------------------------------------------------------


def test_429_then_success_keeps_one_row_and_one_hash():
    chain = FakeChain()
    chain.send_errors = [RpcError("eth_sendRawTransaction: HTTP 429 rate limited", RATE_LIMIT)]
    chain.any_receipt = MINED
    store = PendingSettlementStore()
    ctf = _ctf(chain, store)

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.success is True
    assert res.status == "success"
    assert store.count() == 1
    row = store.get(res.tx_hash)
    assert row["status"] == SUCCESS
    # One signed payload was re-sent; the retry is the same bytes, not a new tx.
    assert len(chain.sent) == 1
    assert row["raw_tx"] == chain.sent[0]


def test_timeout_rebroadcasts_identical_bytes_then_confirms():
    chain = FakeChain()
    chain.mine_after_sends = 2  # the first wait window sees no receipt
    store = PendingSettlementStore()
    ctf = _ctf(chain, store)

    def receipt_after_rebroadcast(tx_hash: str) -> dict | None:
        chain.receipt_calls += 1
        return MINED if len(chain.sent) >= 2 else None

    chain.get_transaction_receipt = receipt_after_rebroadcast

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.success is True
    assert len(chain.sent) == 2
    assert chain.sent[0] == chain.sent[1]  # byte-identical rebroadcast
    assert store.count() == 1
    assert store.get(res.tx_hash)["status"] == SUCCESS


def test_unmined_settlement_is_pending_not_failed_and_keeps_the_row():
    chain = FakeChain()
    chain.mine_after_sends = 99
    store = PendingSettlementStore()
    ctf = _ctf(chain, store)

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.success is False
    assert res.status == "pending"
    assert res.tx_hash.startswith("0x")
    assert res.complete_sets_merged == 0.0
    assert res.usdc_unlocked == 0.0
    assert len(chain.sent) == 2  # original + one identical rebroadcast
    assert chain.sent[0] == chain.sent[1]
    assert store.count() == 1
    assert store.get(res.tx_hash)["status"] == PENDING


def test_reverted_receipt_resolves_the_row_as_reverted():
    chain = FakeChain()
    chain.any_receipt = REVERTED_RECEIPT
    store = PendingSettlementStore()
    ctf = _ctf(chain, store)

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.success is False
    assert res.status == "failed"
    assert "reverted" in (res.error or "")
    assert store.get(res.tx_hash)["status"] == REVERTED
    assert len(chain.sent) == 1  # a revert is definitive: never re-sent


def test_nonce_too_low_looks_up_the_receipt_by_hash():
    chain = FakeChain()
    chain.send_errors = [RpcError("nonce too low", NONCE_TOO_LOW)]
    chain.mine_after_sends = 0  # the node already knows this hash
    chain.any_receipt = MINED
    store = PendingSettlementStore()
    ctf = _ctf(chain, store)

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.success is True
    assert res.status == "success"
    assert chain.sent == []  # nothing was broadcast a second time
    assert store.count() == 1
    assert store.get(res.tx_hash)["status"] == SUCCESS


def test_nonce_too_low_without_receipt_stays_pending():
    chain = FakeChain()
    chain.send_errors = [RpcError("nonce too low", NONCE_TOO_LOW)]
    chain.mine_after_sends = 0  # nothing is known yet
    store = PendingSettlementStore()
    ctf = _ctf(chain, store)

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.status == "pending"
    assert res.success is False
    assert chain.sent == []  # re-sending bytes for a spent nonce would only fail again
    assert store.get(res.tx_hash)["status"] == PENDING


def test_execution_reverted_broadcast_failure_is_terminal():
    chain = FakeChain()
    chain.send_errors = [RpcError("execution reverted: CTF: not approved", EXECUTION_REVERTED)]
    store = PendingSettlementStore()
    ctf = _ctf(chain, store)

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.status == "failed"
    assert chain.sent == []
    assert store.count() == 1
    assert store.get(res.tx_hash)["status"] == "FAILED"


def test_a_second_settlement_for_the_same_condition_is_refused_before_broadcast():
    chain = FakeChain()
    chain.any_receipt = MINED
    store = PendingSettlementStore()
    store.record(
        tx_hash="0xfirst",
        raw_tx="0x02aa",
        nonce=1,
        condition_id=CONDITION_ID,
        kind="merge",
        amount=5.0,
    )
    ctf = _ctf(chain, store)

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.success is False
    assert res.status == "failed"
    assert "already pending" in (res.error or "")
    assert chain.sent == []  # the losing settlement never reaches the wire
    assert store.count() == 1


def test_broadcast_is_refused_when_the_payload_cannot_be_persisted():
    class BrokenStore(PendingSettlementStore):
        def record(self, **kwargs):
            raise RuntimeError("disk I/O error")

    chain = FakeChain()
    chain.any_receipt = MINED
    ctf = _ctf(chain, BrokenStore())

    res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=(TOKEN_YES, TOKEN_NO))

    assert res.success is False
    assert res.status == "failed"
    assert "persisted" in (res.error or "")
    assert chain.sent == []  # unpersisted bytes are never broadcast


# ---------------------------------------------------------------------------
# Engine: pending replay and the double-merge guard
# ---------------------------------------------------------------------------


class FakeCredentials:
    is_configured = False
    has_signing_key = False
    address = "0xMAKER"

    def validate_formatting(self) -> list[str]:
        return []


class FakeClob:
    dry_run = False
    order_version = 2
    maker_address = "0xMAKER"
    signer_address = "0xMAKER"

    def __init__(self) -> None:
        self.credentials = FakeCredentials()

    def get_balance(self) -> dict:
        return {"available": 100.0, "locked": 0.0, "allowance": 1_000.0, "raw": {}}

    def get_open_orders(self, market=None, *, order_id=None, max_pages=20) -> list:
        return []

    def cancel_order(self, order_id: str) -> bool:
        return True


class FakeCtf:
    dry_run = False
    is_settlement_enabled = True

    def __init__(self) -> None:
        self.chain_client = FakeChain()
        self.merge_calls: list[dict] = []

    def estimate_merge_cost_usd(self) -> float:
        return 0.01

    def calculate_optimal_merge_threshold(self, gas_cost_usd: float = 0.01) -> float:
        return 1.0

    def get_position_balance(self, token_id: str, address: str) -> float:
        return 40.0

    def merge_positions(self, condition_id, amount_shares, **kwargs) -> MergeExecutionResult:
        self.merge_calls.append({"condition_id": condition_id, "amount_shares": amount_shares})
        return MergeExecutionResult(
            success=False,
            tx_hash="0xpending",
            condition_id=condition_id,
            complete_sets_merged=0.0,
            usdc_unlocked=0.0,
            gas_fee_usd=0.0,
            gas_units_used=0,
            error="transaction 0xpending not mined",
            status="pending",
        )


def _engine(tmp_path):
    ctf = FakeCtf()
    engine = RealMoneyTradingEngine(
        condition_id="0xCOND",
        asset_id_yes="1111",
        asset_id_no="2222",
        clob_client=FakeClob(),
        ctf_client=ctf,
        initial_cash=100.0,
        db_path=tmp_path / "engine.db",
        ledger_path=tmp_path / "ledger.json",
        dry_run=True,
        gas_fee_merge=0.01,
        auto_merge_threshold=1.0,
    )
    engine.dry_run = False
    engine._cost_basis_unknown = False
    engine.cash = 100.0
    engine.shares_yes = 40.0
    engine.shares_no = 40.0
    return engine, ctf


def _store_pending(engine, tx_hash="0xdeadbeef", condition_id="0xCOND"):
    store = PendingSettlementStore(engine._conn)
    store.record(
        tx_hash=tx_hash,
        raw_tx="0x02f8",
        nonce=9,
        condition_id=condition_id,
        kind="merge",
        amount=40.0,
    )
    return store


def test_engine_refuses_a_second_merge_while_a_settlement_is_pending(tmp_path):
    engine, ctf = _engine(tmp_path)
    _store_pending(engine)

    assert engine.trigger_ctf_merge() is None
    assert ctf.merge_calls == []
    assert engine.shares_yes == pytest.approx(40.0)
    assert engine.total_merges == 0


def test_engine_merges_again_once_the_pending_row_is_resolved(tmp_path):
    engine, ctf = _engine(tmp_path)
    store = _store_pending(engine)
    store.resolve("0xdeadbeef", SUCCESS, MINED)

    res = engine.trigger_ctf_merge()

    assert res is not None and res.status == "pending"
    assert len(ctf.merge_calls) == 1
    # A pending outcome still mutates nothing.
    assert engine.shares_yes == pytest.approx(40.0)
    assert engine.total_merges == 0


def test_startup_replay_resolves_a_mined_pending_settlement(tmp_path):
    engine, ctf = _engine(tmp_path)
    store = _store_pending(engine, tx_hash="0xfeed")
    ctf.chain_client.mine_after_sends = 0  # mined by an earlier process
    ctf.chain_client.any_receipt = MINED

    summary = engine.reconcile_pending_settlements()

    assert summary == {"checked": 1, "resolved": 1, "pending": 0}
    assert store.get("0xfeed")["status"] == SUCCESS


def test_startup_replay_leaves_an_unmined_settlement_pending(tmp_path):
    engine, ctf = _engine(tmp_path)
    store = _store_pending(engine, tx_hash="0xfeed")
    ctf.chain_client.any_receipt = None
    ctf.chain_client.mine_after_sends = 99

    summary = engine.reconcile_pending_settlements()

    assert summary == {"checked": 1, "resolved": 0, "pending": 1}
    assert store.get("0xfeed")["status"] == PENDING


def test_execute_settlement_forwards_target_and_maps_receipt(monkeypatch):
    client = PolygonCtfClient(
        private_key=TEST_KEY_HEX, chain_client=FakeChain(), dry_run=False
    )
    captured: dict = {}

    def fake_broadcast(calldata, **kwargs):
        captured["calldata"] = calldata
        captured.update(kwargs)
        return SettlementOutcome("success", "0xabc", 0.01, 60_000)

    monkeypatch.setattr(client, "_broadcast_settlement", fake_broadcast)
    res = client.execute_settlement(
        "0xdeadbeef",
        to_address=ROUTER,
        condition_id=CONDITION_ID,
        amount_shares=2.5,
    )

    assert res.success is True
    assert res.status == "success"
    assert res.usdc_unlocked == pytest.approx(2.5)
    assert captured["to_address"] == ROUTER
    assert captured["calldata"] == "0xdeadbeef"
    assert captured["amount"] == pytest.approx(2.5)


def test_reconcile_startup_replays_before_reading_balances(tmp_path):
    engine, ctf = _engine(tmp_path)
    store = _store_pending(engine, tx_hash="0xfeed")
    ctf.chain_client.mine_after_sends = 0
    ctf.chain_client.any_receipt = REVERTED_RECEIPT
    clob = engine.clob_client
    clob.get_open_orders = lambda market=None, **kw: []

    engine.reconcile_startup()

    assert store.get("0xfeed")["status"] == REVERTED
