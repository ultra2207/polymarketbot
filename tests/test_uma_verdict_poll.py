"""In-process proof of the settlement pillar's live UMA verdict poll.

The 2026-11-09 soak had a verdict feed that could not advance: the free dRPC
endpoint rejected the poller's 2,048-block ``eth_getLogs`` request ("ranges over
10000 blocks are not supported on free plan"), the cursor was held forever, and
the parquet tape it merged into was 83 hours old. These tests stub the provider
so the walk, the decode, the merge and the funding gate are all exercised
without a network.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from polymarketbot.data.uma import (
    CONTRACT_BY_ADDRESS,
    SPEC_BY_CONTRACT_TOPIC,
    event_topic0,
)
from polymarketbot.paper.uma_poll import (
    UMA_POLL_CONFIRMATIONS,
    UMA_POLL_LIVENESS_FALLBACK_SEC,
    UMA_POLL_MAX_BLOCKS,
    UMA_POLL_MAX_WINDOWS,
    UMA_POLL_MAX_WINDOW_BLOCKS,
    UmaProposalPoller,
)
from polymarketbot.strategy.settlement.fair_value import (
    UMA_LIVE_VERDICTS_PATH,
    SettlementFairValueModel,
    TapeProposal,
    UmaVerdictTape,
    normalize_question,
)


@dataclass(frozen=True)
class QueuedSettlementCandidate:
    """The settlement radar's candidate, reduced to what the gate reads."""

    condition_id: str
    token_id: str
    question: str
    best_ask: float
    ask_size: float
    target_settle_sec: float
    discovered_ts: float
    ask_ladder: List[Any]
    fee_schedule: Any
    p_win: float
    min_order_shares: float
    category: str
    verdict_source: str

QUESTION = "Will the Fed cut rates at the December meeting?"
ANCILLARY = f"q: title: {QUESTION}, description: resolves yes if the Fed cuts."
CID = "0xcd0b8a0f3d1a2b4c5d6e7f8091a2b3c4d5e6f708"
HEAD = 97_000_000


# --------------------------------------------------------------------------- #
# ABI encoding of the two events the poller decodes
# --------------------------------------------------------------------------- #
def _word(value: int) -> str:
    return f"{value % (1 << 256):064x}"


def _pad_bytes(payload: bytes) -> str:
    body = payload.hex()
    padding = (-len(body) // 2) % 32
    return body + "00" * padding


def _bytes32_text(text: str) -> str:
    raw = text.encode("ascii")
    return (raw + b"\x00" * (32 - len(raw))).hex()


def _encode(types: List[str], values: List[Any]) -> str:
    """ABI-encode one event's non-indexed tail (only bytes is dynamic)."""
    head_slots = 32 * len(types)
    head: List[str] = []
    tail: List[str] = []
    for type_name, value in zip(types, values):
        if type_name == "bytes":
            offset = head_slots + sum(len(t) // 2 for t in tail)
            head.append(_word(offset))
            raw = value if isinstance(value, bytes) else str(value).encode()
            tail.append(_word(len(raw)) + _pad_bytes(raw))
        elif type_name == "bytes32":
            head.append(str(value))
        elif type_name == "address":
            head.append(_word(int(str(value), 16)))
        elif type_name == "int256":
            head.append(_word(int(value)))
        else:  # uint256
            head.append(_word(int(value)))
    return "0x" + "".join(head + tail)


def _spec(event: str):
    for (contract, _topic), spec in SPEC_BY_CONTRACT_TOPIC.items():
        if spec.name == event:
            address = next(a for a, c in CONTRACT_BY_ADDRESS.items() if c == contract)
            return address, spec
    raise AssertionError(f"no spec for {event}")


def _log(event: str, block: int, *, price_wei: int, expiry: float) -> Dict[str, Any]:
    address, spec = _spec(event)
    identifier = _bytes32_text("YES_OR_NO_QUERY")
    if event == "ProposePrice":
        data = _encode(
            ["bytes32", "uint256", "bytes", "int256", "uint256", "address"],
            [identifier, 1_700_000_000, ANCILLARY.encode(), price_wei, int(expiry), address],
        )
    else:
        data = _encode(
            ["bytes32", "uint256", "bytes", "int256"],
            [identifier, 1_700_000_000, ANCILLARY.encode(), price_wei],
        )
    return {
        "blockNumber": hex(block),
        "blockHash": "0x" + "11" * 32,
        "transactionHash": "0x" + "22" * 32,
        "transactionIndex": "0x1",
        "logIndex": "0x0",
        "address": address,
        "topics": [
            event_topic0(spec.signature),
            "0x" + "00" * 12 + "aa" * 20,
            "0x" + "00" * 12 + "bb" * 20,
        ],
        "data": data,
    }


class _StubProvider:
    """A chain client stub: one endpoint, one range cap, zero network."""

    def __init__(self, logs: List[Dict[str, Any]], head: int = HEAD, span_limit: int | None = None):
        self.url = "https://stub.invalid/rpc"
        self.read_urls = [self.url]
        self.head = head
        self.logs = logs
        self.span_limit = span_limit
        self.chunk = 100_000
        self.requests: List[tuple[int, int]] = []

    def block_number(self) -> int:
        return self.head

    def get_logs(self, *, address, from_block, to_block=None, topics=None, progress=False):
        self.requests.append((from_block, to_block))
        span = to_block - from_block + 1
        if self.span_limit is not None and span > self.span_limit:
            raise RuntimeError(
                "eth_getLogs: HTTP 400 ranges over 10000 blocks are not supported on free plan"
            )
        return [
            log
            for log in self.logs
            if from_block <= int(log["blockNumber"], 16) <= to_block
        ]


class _FixedHazard:
    def get_credible_upper_hazard(self, _category: str) -> float:
        return 0.0126


def _poller(
    tape: UmaVerdictTape, provider: _StubProvider, state_path: Path
) -> UmaProposalPoller:
    return UmaProposalPoller(
        tape=tape,
        providers=[provider],
        state_path=state_path,
        block_interval_s=2.1,
        liveness_fallback_sec=UMA_POLL_LIVENESS_FALLBACK_SEC,
        max_blocks=UMA_POLL_MAX_BLOCKS,
        max_windows=UMA_POLL_MAX_WINDOWS,
        confirmations=UMA_POLL_CONFIRMATIONS,
    )


def _model(tape: UmaVerdictTape) -> SettlementFairValueModel:
    return SettlementFairValueModel(dispute_tracker=_FixedHazard(), tape=tape)


def _fundable(poller: UmaProposalPoller, pending: List[QueuedSettlementCandidate]):
    """The deploy-path gate: keep only candidates whose verdict may fund."""
    return [cand for cand in pending if poller.fundable(cand)]


def _tape(_tmp_path: Path) -> UmaVerdictTape:
    # Relative glob: ``Path.glob`` refuses absolute patterns, and the tests run
    # with cwd=tmp_path so this matches no parquet at all (a stale empty tape).
    return UmaVerdictTape(glob="no-tape/*.parquet", refresh_sec=0.0)


def _candidate(verdict, ask: float = 0.93) -> QueuedSettlementCandidate:
    """The candidate the radar builds from a verdict (same fields, same order)."""
    return QueuedSettlementCandidate(
        condition_id=CID,
        token_id="yes-token",
        question=QUESTION,
        best_ask=ask,
        ask_size=500.0,
        target_settle_sec=2.45 * 3600.0,
        discovered_ts=time.time(),
        ask_ladder=[(ask, 500.0)],
        fee_schedule=None,
        p_win=float(verdict.p_win),
        min_order_shares=5.0,
        category="settlement",
        verdict_source=verdict.source,
    )


@pytest.fixture()
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "uma_poll_cursor.json"
    monkeypatch.chdir(tmp_path)
    return path


def test_live_proposal_poll_queues_a_fundable_candidate(state_path, tmp_path):
    now = time.time()
    proposal = _log(
        "ProposePrice", HEAD - 40, price_wei=10**18, expiry=now + 5400.0
    )
    provider = _StubProvider([proposal])
    tape = _tape(tmp_path)
    poller = _poller(tape, provider, state_path)

    poller.poll(now)

    assert provider.requests, "the poll must reach the provider"
    live = tape.proposal_for(CID, QUESTION)
    assert live is not None and live.proposed_price == 1.0
    assert live.expiry_ts == pytest.approx(now + 5400.0, abs=1.0)
    assert poller.is_live(live, "settlement", now)

    verdict = _model(tape).fair_value(
        condition_id=CID,
        question=QUESTION,
        outcomes=["Yes", "No"],
        prices=["0.62", "0.38"],  # a book print, not a payout vector
        closed=False,
        uma_status="proposed",
        category="settlement",
        now=now,
    )
    assert verdict is not None
    assert verdict.source == "uma_tape"
    assert verdict.winning_index == 0
    assert verdict.p_win == pytest.approx(1.0 - 0.0126)

    cand = _candidate(verdict)
    # The deploy-path gate (real method) keeps a candidate whose verdict is live
    # on the tape that exists at decision time.
    assert _fundable(poller, [cand]) == [cand]


def test_disputed_proposal_is_dropped_and_the_candidate_falls_out(state_path, tmp_path):
    now = time.time()
    proposal_block = HEAD - 60
    dispute_block = HEAD - 10
    logs = [
        _log("ProposePrice", proposal_block, price_wei=10**18, expiry=now + 5400.0),
        _log("DisputePrice", dispute_block, price_wei=10**18, expiry=now + 5400.0),
    ]
    provider = _StubProvider(logs)
    tape = _tape(tmp_path)
    poller = _poller(tape, provider, state_path)

    poller.poll(now)

    assert tape.proposal_for(CID, QUESTION) is None
    model = _model(tape)
    assert (
        model.fair_value(
            condition_id=CID,
            question=QUESTION,
            outcomes=["Yes", "No"],
            prices=["0.62", "0.38"],
            closed=False,
            uma_status="proposed",
            category="settlement",
            now=now,
        )
        is None
    )
    stale = _candidate(
        type("V", (), {"p_win": 1.0 - 0.0126, "source": "uma_tape"})(), ask=0.93
    )
    assert _fundable(poller, [stale]) == []


def test_poll_respects_a_provider_range_cap_and_persists_its_cursor(state_path, tmp_path):
    """dRPC's free plan: 100-block windows only. The walk must shrink, not stall."""
    now = time.time()
    provider = _StubProvider([], span_limit=100)
    tape = _tape(tmp_path)
    poller = _poller(tape, provider, state_path)

    poller.poll(now)

    assert provider.requests, "the poll must attempt at least one window"
    # Every request after the first rejection fits inside the measured cap; the
    # first request is the one-shot hard-cap probe.
    accepted = [span for _lo, hi in provider.requests for span in [hi - _lo + 1] if span <= 100]
    assert accepted, f"no window fit the cap: {provider.requests}"
    assert len(provider.requests) <= UMA_POLL_MAX_WINDOWS
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    boot_lookback = max(
        UMA_POLL_MAX_BLOCKS,
        int(UMA_POLL_LIVENESS_FALLBACK_SEC / 2.1),
    )
    assert saved["cursor"] > HEAD - boot_lookback
    assert saved["providers"][provider.url]["window"] <= 1_000
    assert saved["providers"][provider.url]["max_tried"] is True

    # A restart resumes from the persisted cursor instead of re-deriving a
    # multi-day lookback; the boot re-walk only reaches back one liveness window.
    resumed = _poller(tape, provider, state_path)
    resume_from = resumed.state()["cursor"]
    provider.requests.clear()
    resumed.poll(now + 60.0)
    assert provider.requests
    boot_window = int(UMA_POLL_LIVENESS_FALLBACK_SEC / 2.1)
    assert provider.requests[0][0] <= resume_from
    assert provider.requests[0][0] >= resume_from - boot_window


def test_widest_known_window_is_used_and_advances_the_cursor(state_path, tmp_path):
    now = time.time()
    provider = _StubProvider([], span_limit=10_000)
    tape = _tape(tmp_path)
    poller = _poller(tape, provider, state_path)

    poller.poll(now)

    # The boot lookback is one liveness window; it is walked in whole windows of
    # the provider's (measured) cap and then stops at the confirmed head.
    lookback = int(UMA_POLL_LIVENESS_FALLBACK_SEC / 2.1)
    expected = -(-lookback // UMA_POLL_MAX_WINDOW_BLOCKS)
    assert len(provider.requests) == expected
    for lo, hi in provider.requests:
        assert hi - lo + 1 <= UMA_POLL_MAX_WINDOW_BLOCKS
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    # The served width is remembered as this provider's window.
    assert saved["providers"][provider.url]["window"] == UMA_POLL_MAX_WINDOW_BLOCKS
    # The walk reached the confirmed head, so the next sweep has nothing to fetch.
    assert saved["cursor"] > HEAD - UMA_POLL_CONFIRMATIONS


def test_live_verdicts_survive_a_process_restart(state_path, tmp_path):
    """The overlay is chain state, not process state: a restart must keep it.

    Measured 2026-11-09: 74 ``ProposePrice`` events landed on Optimistic Oracle
    V2 in the previous 25 h while the parquet tape stayed 5,035 min stale, and a
    memory-only overlay meant every soak iteration began with an empty verdict
    index ("0 oracle-priced" for the whole run).
    """
    now = time.time()
    proposal = _log("ProposePrice", HEAD - 40, price_wei=10**18, expiry=now + 5400.0)
    provider = _StubProvider([proposal])
    tape = _tape(tmp_path)
    poller = _poller(tape, provider, state_path)

    poller.poll(now)
    assert tape.proposal_for(CID, QUESTION) is not None
    overlay = tmp_path / UMA_LIVE_VERDICTS_PATH
    assert overlay.exists(), "the live overlay must be written for the next process"

    fresh = UmaVerdictTape(glob="no-tape/*.parquet", refresh_sec=0.0)
    restored = fresh.proposal_for(CID, QUESTION)
    assert restored is not None and restored.proposed_price == 1.0
    # A parquet reload rebuilds the index from disk; the overlay must survive it.
    fresh.refresh(now=now, force=True)
    assert fresh.proposal_for(CID, QUESTION) is not None


def test_a_restart_rewalks_the_still_live_liveness_window(state_path, tmp_path):
    """A cursor parked at the tip must not hide proposals still inside liveness.

    The cursor file is shared by every process that polls, so on restart it is
    ahead of every proposal this process has never seen. Re-walking one liveness
    window is what turns the poll back into an ever-updating input.
    """
    now = time.time()
    proposal = _log("ProposePrice", HEAD - 3_000, price_wei=10**18, expiry=now + 3600.0)
    provider = _StubProvider([proposal])
    tape = _tape(tmp_path)
    state_path.write_text(
        json.dumps({"cursor": HEAD - UMA_POLL_CONFIRMATIONS, "providers": {}}),
        encoding="utf-8",
    )
    poller = _poller(tape, provider, state_path)

    poller.poll(now)

    assert provider.requests, "the first sweep must issue a request"
    assert provider.requests[0][0] <= HEAD - 3_000, "the boot re-walk skipped the proposal"
    assert tape.proposal_for(CID, QUESTION) is not None


def test_scanned_universe_binds_condition_id_to_a_live_verdict(state_path, tmp_path):
    """``ProposePrice`` has no condition id; the sweep's own universe supplies it."""
    now = time.time()
    proposal = _log("ProposePrice", HEAD - 40, price_wei=10**18, expiry=now + 5400.0)
    provider = _StubProvider([proposal])
    tape = _tape(tmp_path)
    poller = _poller(tape, provider, state_path)
    poller.poll(now)

    live = tape.proposal_for("", QUESTION)
    assert live is not None and live.condition_id == ""
    assert tape.bind_condition_ids({normalize_question(QUESTION): CID}) == 1
    # The condition-id index is exact: no question text needed.
    bound = tape.proposal_for(CID.upper(), "")
    assert bound is not None and bound.condition_id == CID
    assert tape.bind_condition_ids({normalize_question(QUESTION): CID}) == 0
    # The binding is persisted with the verdict.
    fresh = UmaVerdictTape(glob="no-tape/*.parquet", refresh_sec=0.0)
    assert fresh.proposal_for(CID, "") is not None


def test_proposed_is_never_a_payout():
    model = SettlementFairValueModel(dispute_tracker=_FixedHazard(), tape=_tape(Path(".")))
    verdict = model.fair_value(
        condition_id=CID,
        question=QUESTION,
        outcomes=["Yes", "No"],
        prices=["1", "0"],
        closed=False,
        uma_status="proposed",
        category="settlement",
    )
    assert verdict is None
    assert model.final_payout(["1", "0"], False, "proposed") is None
    assert model.final_payout(["1", "0"], False, "resolved") == 0


def test_live_verdict_survives_a_parquet_tape_reload(tmp_path, monkeypatch):
    """A stale parquet read must not erase proposals the live poll already merged."""
    monkeypatch.chdir(tmp_path)
    tape = UmaVerdictTape(glob="no-tape/*.parquet", refresh_sec=0.0)
    now = time.time()
    live = TapeProposal(
        question=QUESTION,
        condition_id="",
        proposed_price=1.0,
        ts=now - 60.0,
        liveness_sec=7200.0,
        expiry_ts=now + 7200.0,
    )
    assert tape.ingest([live]) == 1
    tape.refresh(now=now, force=True)  # unreadable tape: index rebuilt from disk
    assert tape.proposal_for("", QUESTION) is not None


def test_tape_verdict_survives_its_liveness_window():
    """An elapsed liveness window is a *stronger* verdict, not an expired one."""
    tape = UmaVerdictTape(glob="unused/**/*.parquet")
    poller = _poller(
        tape, _StubProvider([]), Path("unused-poll-cursor.json")
    )
    now = time.time()
    survived = TapeProposal(
        question=QUESTION,
        condition_id="",
        proposed_price=1.0,
        ts=now - 7600.0,
        liveness_sec=0.0,
        expiry_ts=now - 400.0,  # 400 s past expiry, relayer/redeem legs pending
    )
    assert poller.is_live(survived, "settlement", now)
    ancient = TapeProposal(
        question=QUESTION,
        condition_id="",
        proposed_price=1.0,
        ts=now - 200_000.0,
        liveness_sec=0.0,
        expiry_ts=now - 190_000.0,
    )
    assert not poller.is_live(ancient, "settlement", now)
    assert not poller.is_live(None, "settlement", now)


def test_live_verdict_condition_ids_excludes_aged_proposals(state_path, tmp_path):
    """The verdict-keyed sweep must carry only proposals that can still pay.

    The radar sweeps these ids every pass so a verdict market is discovered by
    its own condition id rather than by where its endDate lands in a capped
    Gamma stratum (HTTP 422 past offset 2300, measured 2026-11-09). An aged
    proposal must not drag its resolved market into the sweep forever.
    """
    now = time.time()
    tape = _tape(tmp_path)
    live = TapeProposal(
        question=QUESTION,
        condition_id=CID,
        proposed_price=0.0,
        ts=now - 60.0,
        liveness_sec=7_200.0,
        expiry_ts=now + 7_140.0,
    )
    aged = TapeProposal(
        question="Will an aged proposal still be priced?",
        condition_id="0x" + "ab" * 32,
        proposed_price=1.0,
        ts=now - 48 * 3600.0,
        liveness_sec=7_200.0,
        expiry_ts=now - 48 * 3600.0 + 7_200.0,
    )
    tape.ingest([live, aged])

    ids = tape.live_verdict_condition_ids(now)
    assert ids == [CID]
    # Well past expiry + the verdict tail, even the live row ages out.
    assert tape.live_verdict_condition_ids(now + 7_140.0 + 6 * 3600.0 + 1.0) == []


def test_binding_covers_rows_only_the_parquet_index_holds(state_path, tmp_path):
    """A tape row that never entered the overlay must still get its exact id.

    ``refresh`` rebuilds ``_by_question`` from parquet and a merge can keep the
    parquet row when it is newer than the overlay copy. Binding only the overlay
    therefore lost the id and flipped the verdict join back to question text -
    the binding instability behind "2 oracle-priced -> 0" between sweeps.
    """
    now = time.time()
    tape = _tape(tmp_path)
    proposal = TapeProposal(
        question=QUESTION,
        condition_id="",
        proposed_price=1.0,
        ts=now - 30.0,
        liveness_sec=7_200.0,
        expiry_ts=now + 7_170.0,
    )
    # Exactly the state a parquet reload leaves: indexed by question, not in
    # the persisted live overlay.
    key = normalize_question(QUESTION)
    tape._by_question[key] = proposal
    tape._proposals.append(proposal)

    assert tape.bind_condition_ids({key: CID}) == 1
    bound = tape.proposal_for(CID, "")
    assert bound is not None and bound.condition_id == CID
    # Idempotent: a second sweep re-attaching the same id changes nothing.
    assert tape.bind_condition_ids({key: CID}) == 0
