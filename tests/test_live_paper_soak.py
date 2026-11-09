"""Tests for the 4-Strategy Live Paper Soak Runner and Preflight Verification."""

import sys
import threading
import time
from pathlib import Path

# Add project root to sys.path so scripts can be imported in tests
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.preflight_verification import PreflightAuditor
from scripts.run_live_paper_soak import LivePaperSoakDaemon, SoakTelemetryDb, StrategyMetrics


def test_soak_telemetry_db(tmp_path: Path) -> None:
    db_file = tmp_path / "test_soak.db"
    db = SoakTelemetryDb(db_file)

    # Record trade
    db.record_trade(
        strategy="LatencySniper",
        condition_id="cond_101",
        side="BUY",
        price=0.50,
        size=100.0,
        gross_pnl=2.50,
        fee=0.10,
        rebate=0.05,
        latency_us=45.0,
        details="Unit test trade",
    )

    # Record snapshot
    strats = {
        "LatencySniper": StrategyMetrics("LatencySniper", 1000.0, 1002.45, 1000.0, 1, 2.50, 0.10, 0.05, 2.45),
        "Domain2ARR": StrategyMetrics("Domain2ARR", 1000.0, 1000.0, 400.0),
        "CtfSplitter": StrategyMetrics("CtfSplitter", 1000.0, 1000.0, 250.0),
        "SettlementHarvester": StrategyMetrics("SettlementHarvester", 1000.0, 1000.0, 400.0),
    }
    db.record_snapshot(
        uptime_sec=60.0,
        total_equity=4002.45,
        total_net_pnl=2.45,
        strats=strats,
        clob_rtt_ms=8.5,
        binance_rtt_ms=12.2,
        trades_count=1,
    )

    # Verify tables contain records
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM soak_trades")
    assert cur.fetchone()[0] == 1

    cur.execute("SELECT count(*) FROM soak_snapshots")
    assert cur.fetchone()[0] == 1
    conn.close()


def test_live_paper_soak_daemon_lifecycle(tmp_path: Path) -> None:
    daemon = LivePaperSoakDaemon(capital_per_strategy=500.0, vip_tier="bronze", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "soak_test.db")

    assert daemon.capital_per_strategy == 500.0
    assert daemon.vip_tier == "bronze"
    assert len(daemon.metrics) == 3
    assert (daemon.metrics.get("RewardHarvester") or daemon.metrics.get("Domain2ARR")).starting_cash == 500.0
    assert "LatencySniper" not in daemon.metrics
    assert hasattr(daemon, "settlement_allocator")


def test_settlement_harvester_marginal_pipeline_deploy(tmp_path: Path, monkeypatch) -> None:
    """Verify that _deploy_pipeline_capital uses MarginalSettlementAllocator to acquire positions."""
    from scripts.run_live_paper_soak import QueuedSettlementCandidate

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="none", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "soak_test_pipeline.db")

    # Queue 2 candidates
    cand1 = QueuedSettlementCandidate(
        condition_id="0xCOND1",
        token_id="tok1",
        question="Market 1 (Fast 60s)",
        best_ask=0.960,
        ask_size=200.0,
        target_settle_sec=60.0,
        discovered_ts=time.time(),
        ask_ladder=[(0.960, 200.0)],
    )
    cand2 = QueuedSettlementCandidate(
        condition_id="0xCOND2",
        token_id="tok2",
        question="Market 2 (Slow 1800s)",
        best_ask=0.985,
        ask_size=200.0,
        target_settle_sec=1800.0,
        discovered_ts=time.time(),
        ask_ladder=[(0.985, 200.0)],
    )
    daemon.candidate_queue = [cand1, cand2]

    # Mock the atomic batch book helper to return the candidate ladders.
    def mock_fetch_l2_books_batch(session, token_ids, *args, **kwargs):
        books = {}
        if "tok1" in token_ids:
            books["tok1"] = {"asset_id": "tok1", "asks": [{"price": "0.960", "size": "200.0"}]}
        if "tok2" in token_ids:
            books["tok2"] = {"asset_id": "tok2", "asks": [{"price": "0.985", "size": "200.0"}]}
        return books

    import scripts.run_live_paper_soak as soak_mod
    monkeypatch.setattr(soak_mod, "fetch_l2_books_batch", mock_fetch_l2_books_batch)

    deployed = daemon._deploy_pipeline_capital()
    assert deployed >= 1
    assert len(daemon.settlement_positions) == deployed

    # High-velocity market 1 should receive full capacity ($192)
    pos_map = {p.condition_id: p for p in daemon.settlement_positions}
    assert "0xCOND1" in pos_map
    assert pos_map["0xCOND1"].shares > 0
    assert abs(pos_map["0xCOND1"].entry_price - 0.960) < 1e-4


def test_preflight_auditor_local_sanity() -> None:
    auditor = PreflightAuditor(test_order=False, test_ctf=False, strict=False)
    auditor.run_phase_1()
    auditor.run_phase_5()
    report = auditor.generate_report()

    assert report.passed_count >= 3
    assert len(report.results) > 0


def test_settlement_radar_multi_stratum_paginated_scanner(tmp_path: Path, monkeypatch) -> None:
    """Verify that multi-stratum radar discovers targets across offsets, enforces Oracle gate, and uses batch CLOB."""
    from scripts.run_live_paper_soak import LivePaperSoakDaemon, SoakTelemetryDb, DEFAULT_GAMMA_HOST, DEFAULT_CLOB_HOST
    from datetime import datetime, timezone, timedelta
    import json

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="none", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "test_radar.db")

    # 10 minutes past the calendar end is inside the measured settlement horizon.
    expired_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mock markets
    mock_markets_page_0 = [
        # Target 1: FC Utrecht (expired 10m ago, 0.96 ask)
        {
            "conditionId": "0xUTR_COND",
            "question": "Will FC Utrecht win on 2026-11-08?",
            "endDate": expired_iso,
            "outcomePrices": json.dumps(["0.04", "0.96"]),
            "clobTokenIds": json.dumps(["tok_utr_0", "tok_utr_1"]),
        },
        # Unexpired market without UMA proposal -> MUST be rejected by Oracle Gate
        {
            "conditionId": "0xUNEXPIRED_COND",
            "question": "Will Human Land on Mars by 2030?",
            "endDate": "2030-01-01T00:00:00Z",
            "outcomePrices": json.dumps(["0.04", "0.96"]),
            "clobTokenIds": json.dumps(["tok_mars_0", "tok_mars_1"]),
        },
    ]

    mock_markets_page_100 = [
        # Target 2: Browns vs. Saints: O/U 42.5 (expired 10m ago, 0.96 ask)
        {
            "conditionId": "0xBR_COND",
            "question": "Browns vs. Saints: O/U 42.5",
            "endDate": expired_iso,
            "outcomePrices": json.dumps(["0.04", "0.96"]),
            "clobTokenIds": json.dumps(["tok_br_0", "tok_br_1"]),
        },
        # Target 3: Lions vs. Dolphins: O/U 48.5 (expired 10m ago, 0.977 ask)
        {
            "conditionId": "0xLIO_COND",
            "question": "Lions vs. Dolphins: O/U 48.5",
            "endDate": expired_iso,
            "outcomePrices": json.dumps(["0.023", "0.977"]),
            "clobTokenIds": json.dumps(["tok_lio_0", "tok_lio_1"]),
        },
        # Unexpired calendar end, but active UMA proposal -> MUST pass Oracle Gate!
        {
            "conditionId": "0xUMA_COND",
            "question": "Active UMA Dispute Market",
            "endDate": "2030-01-01T00:00:00Z",
            "umaResolutionStatus": "proposed",
            "outcomePrices": json.dumps(["0.02", "0.98"]),
            "clobTokenIds": json.dumps(["tok_uma_0", "tok_uma_1"]),
        },
    ]

    class MockResponse:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code

        def json(self):
            return self._data

    # Gamma rows carry the outcome names and venue minimum order size; the radar
    # now validates both, so add them to the fixture rows.
    for _m in mock_markets_page_0 + mock_markets_page_100:
        _m.setdefault("outcomes", json.dumps(["No", "Yes"]))
        _m.setdefault("orderMinSize", 5)

    def mock_get(url, *args, **kwargs):
        if "offset=0" in url and "order=volume24hr" in url:
            return MockResponse(mock_markets_page_0)
        elif "offset=100" in url and "order=volume24hr" in url:
            return MockResponse(mock_markets_page_100)
        elif DEFAULT_GAMMA_HOST in url:
            return MockResponse([])
        return MockResponse([], status_code=404)

    def mock_post(url, *args, **kwargs):
        if DEFAULT_CLOB_HOST in url and "books" in url:
            payload = kwargs.get("json", [])
            resp_books = []
            for item in payload:
                tid = item.get("token_id")
                if tid == "tok_utr_1":
                    resp_books.append({"asset_id": tid, "asks": [{"price": "0.960", "size": "13807.0"}]})
                elif tid == "tok_br_1":
                    resp_books.append({"asset_id": tid, "asks": [{"price": "0.960", "size": "5272.0"}]})
                elif tid == "tok_lio_1":
                    resp_books.append({"asset_id": tid, "asks": [{"price": "0.977", "size": "200.0"}]})
                elif tid == "tok_uma_1":
                    resp_books.append({"asset_id": tid, "asks": [{"price": "0.980", "size": "1000.0"}]})
            return MockResponse(resp_books)
        return MockResponse([], status_code=404)

    monkeypatch.setattr(daemon.http_session, "get", mock_get)
    monkeypatch.setattr(daemon.http_session, "post", mock_post)

    # Run radar loop for one iteration by launching and stopping
    daemon.running = True
    radar_thread = threading.Thread(target=daemon._run_settlement_radar_thread, daemon=True)
    radar_thread.start()
    time.sleep(0.3)
    daemon.running = False
    radar_thread.join(timeout=1.0)

    with daemon.queue_lock:
        queued = list(daemon.candidate_queue)

    # 4 markets must be queued (FC Utrecht, Browns, Lions, UMA proposal)
    assert len(queued) == 4
    q_map = {c.condition_id: c for c in queued}

    # Verify FC Utrecht
    assert "0xUTR_COND" in q_map
    utr = q_map["0xUTR_COND"]
    assert utr.best_ask == 0.960
    assert utr.ask_size == 13807.0
    assert "Will FC Utrecht win" in utr.question

    # Verify Browns vs Saints
    assert "0xBR_COND" in q_map
    br = q_map["0xBR_COND"]
    assert br.best_ask == 0.960
    assert br.ask_size == 5272.0

    # Verify Lions vs Dolphins
    assert "0xLIO_COND" in q_map
    lio = q_map["0xLIO_COND"]
    assert lio.best_ask == 0.977
    assert lio.ask_size == 200.0

    # Verify UMA proposed market
    assert "0xUMA_COND" in q_map
    uma = q_map["0xUMA_COND"]
    assert uma.best_ask == 0.980

    # Verify unexpired non-UMA market was rejected
    assert "0xUNEXPIRED_COND" not in q_map

    # No fixture is a final 0/1 outcome, so all are priced at their own implied
    # outcome probability net of hazard: none has a positive par edge. A closed
    # or proposed 0.98 market is still unresolved.
    assert all(c.annualized_arr <= 0.0 for c in queued)
    assert all(c.p_win <= c.best_ask + 1e-9 for c in queued)


def test_settlement_radar_unresolved_market_uses_outcome_probability(
    tmp_path: Path, monkeypatch
) -> None:
    """A 0.51 unresolved market must use P(outcome wins), not P(market resolves)."""
    from scripts.run_live_paper_soak import (
        LivePaperSoakDaemon,
        SoakTelemetryDb,
        SettlementCandidate,
        DEFAULT_GAMMA_HOST,
        DEFAULT_CLOB_HOST,
    )
    from datetime import datetime, timezone, timedelta
    import json

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="none", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "test_radar_unresolved.db")

    end_10m = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    market = {
        "conditionId": "0xLIMA_COND",
        "question": "Lima: Gonzalo Bueno vs Thiago Seyboth Wild",
        "endDate": end_10m,
        "outcomePrices": json.dumps(["0.49", "0.51"]),
        "clobTokenIds": json.dumps(["tok_lima_0", "tok_lima_1"]),
        "outcomes": json.dumps(["No", "Yes"]),
        "orderMinSize": 5,
        # Closed for trading but NOT resolved: 0.49/0.51 is still an
        # unresolved outcome, so it must not be treated as a certain win.
        "closed": True,
    }

    class MockResponse:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code

        def json(self):
            return self._data

    def mock_get(url, *args, **kwargs):
        if "offset=0" in url and "order=volume24hr" in url:
            return MockResponse([market])
        return MockResponse([])

    def mock_post(url, *args, **kwargs):
        payload = kwargs.get("json", [])
        books = []
        for item in payload:
            if item.get("token_id") == "tok_lima_1":
                books.append({
                    "asset_id": "tok_lima_1",
                    "bids": [{"price": "0.50", "size": "2000"}],
                    "asks": [{"price": "0.51", "size": "2000"}],
                })
        return MockResponse(books)

    monkeypatch.setattr(daemon.http_session, "get", mock_get)
    monkeypatch.setattr(daemon.http_session, "post", mock_post)

    daemon.running = True
    radar_thread = threading.Thread(target=daemon._run_settlement_radar_thread, daemon=True)
    radar_thread.start()
    time.sleep(0.6)
    daemon.running = False
    radar_thread.join(timeout=2.0)

    with daemon.queue_lock:
        queued = list(daemon.candidate_queue)
    cand = next((c for c in queued if c.condition_id == "0xLIMA_COND"), None)
    assert cand is not None, "unresolved past-calendar market should still be queued"
    # 0.51 outcome probability (minus the measured dispute hazard), NOT ~0.997
    assert cand.p_win < 0.60, f"p_win must reflect the outcome, got {cand.p_win}"

    # With p_win=0.51 the allocator has no positive expected edge at a 0.51 ask.
    settlement_cand = SettlementCandidate(
        condition_id=cand.condition_id,
        token_id=cand.token_id,
        question=cand.question,
        target_settle_sec=cand.target_settle_sec,
        ask_ladder=cand.ask_ladder,
        fee_schedule=cand.fee_schedule,
        p_win=cand.p_win,
        min_order_shares=cand.min_order_shares,
        category=cand.category,
    )
    res = daemon.settlement_allocator.allocate([settlement_cand], usable_capital=1000.0)
    assert res.active_markets_count == 0
    assert res.total_allocated_usd == 0.0


def test_virtual_book_tracker_ghost_liquidity_elimination() -> None:
    """Verify VirtualBookTracker prevents ghost liquidity churn on consumed rungs."""
    from scripts.run_live_paper_soak import VirtualBookTracker

    tracker = VirtualBookTracker()
    tid = "tok_test_42"

    raw_ladder = [(0.960, 500.0), (0.970, 1000.0)]

    # Initial state: full liquidity available
    filtered = tracker.filter_ladder(tid, raw_ladder)
    assert filtered == raw_ladder

    # Consume 300 shares at 0.960
    tracker.consume(tid, 0.960, 300.0)
    filtered2 = tracker.filter_ladder(tid, raw_ladder)
    assert len(filtered2) == 2
    assert filtered2[0] == (0.960, 200.0)
    assert filtered2[1] == (0.970, 1000.0)

    # Consume remaining 200 shares at 0.960
    tracker.consume(tid, 0.960, 200.0)
    filtered3 = tracker.filter_ladder(tid, raw_ladder)
    assert len(filtered3) == 1
    assert filtered3[0] == (0.970, 1000.0)


def test_settlement_radar_next_48h_expiring_markets(tmp_path: Path, monkeypatch) -> None:
    """Verify that markets expiring within the next 48h are discovered and priced with true remaining duration."""
    from scripts.run_live_paper_soak import LivePaperSoakDaemon, SoakTelemetryDb, DEFAULT_GAMMA_HOST, DEFAULT_CLOB_HOST
    from datetime import datetime, timezone, timedelta
    import json

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="none", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "test_radar_48h.db")

    now = datetime.now(timezone.utc)
    t_12h = (now + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_60h = (now + timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_markets_48h = [
        # Market 1: Expiring in 12 hours (within 48h)
        {
            "conditionId": "0x12H_COND",
            "question": "Tennis Match: Player A vs Player B",
            "endDate": t_12h,
            "outcomePrices": json.dumps(["0.04", "0.96"]),
            "clobTokenIds": json.dumps(["tok_12h_0", "tok_12h_1"]),
        },
        # Market 2: Expiring in 60 hours (beyond 48h, no UMA) -> MUST be excluded
        {
            "conditionId": "0x60H_COND",
            "question": "Future Game: Team X vs Team Y",
            "endDate": t_60h,
            "outcomePrices": json.dumps(["0.04", "0.96"]),
            "clobTokenIds": json.dumps(["tok_60h_0", "tok_60h_1"]),
        },
    ]

    class MockResponse:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code

        def json(self):
            return self._data

    for _m in mock_markets_48h:
        _m.setdefault("outcomes", json.dumps(["No", "Yes"]))
        _m.setdefault("orderMinSize", 5)

    def mock_get(url, *args, **kwargs):
        if "end_date_min" in url:
            return MockResponse(mock_markets_48h)
        elif DEFAULT_GAMMA_HOST in url:
            return MockResponse([])
        return MockResponse([], status_code=404)

    def mock_post(url, *args, **kwargs):
        if DEFAULT_CLOB_HOST in url and "books" in url:
            payload = kwargs.get("json", [])
            resp_books = []
            for item in payload:
                tid = item.get("token_id")
                if tid == "tok_12h_1":
                    resp_books.append({"asset_id": tid, "asks": [{"price": "0.960", "size": "500.0"}]})
            return MockResponse(resp_books)
        return MockResponse([], status_code=404)

    monkeypatch.setattr(daemon.http_session, "get", mock_get)
    monkeypatch.setattr(daemon.http_session, "post", mock_post)

    daemon.running = True
    radar_thread = threading.Thread(target=daemon._run_settlement_radar_thread, daemon=True)
    radar_thread.start()
    time.sleep(0.3)
    daemon.running = False
    radar_thread.join(timeout=1.0)

    with daemon.queue_lock:
        queued = list(daemon.candidate_queue)

    # Only the 12h market should be queued
    assert len(queued) == 1
    c = queued[0]
    assert c.condition_id == "0x12H_COND"
    assert c.best_ask == 0.960
    assert c.ask_size == 500.0
    # Target settle duration is the 12h time-to-resolution plus the on-chain
    # UMA/relayer/redeem cycle (~2.45h), not a fixed hold constant.
    assert 50000.0 < c.target_settle_sec < 55000.0
    # Unresolved in-play market: p_win is P(this outcome wins), not P(the market
    # resolves). At ask == p_est there is no positive par edge, so it is queued
    # but must not be funded.
    assert c.p_win < 1.0
    assert c.p_win <= c.best_ask
    assert c.annualized_arr <= 0.0



def test_reward_universe_scanner_multipage_and_upper_bounding(tmp_path: Path, monkeypatch) -> None:
    """Verifies that scan_reward_universe sweeps multi-page cursor pagination and discovers pools."""
    import base64
    from scripts.run_live_paper_soak import DEFAULT_CLOB_HOST, LivePaperSoakDaemon

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="none", duration=0.1)

    # Page 0 (offset 0): 2 pools with rate 15,000 and 500
    page_0 = [
        {
            "condition_id": "0xMEGA_COND",
            "question": "Mega Pool: Rams vs Commanders",
            "rewards_config": [{"rate_per_day": 15000.0}],
            "rewards_max_spread": 2.5,
            "rewards_min_size": 100.0,
            "tokens": [{"token_id": "mega_tok_0", "price": 0.5}, {"token_id": "mega_tok_1", "price": 0.5}],
        },
        {
            "condition_id": "0xNICHE_COND",
            "question": "Niche Pool: Will Local Town Council Pass Budget",
            "rewards_config": [{"rate_per_day": 500.0}],
            "rewards_max_spread": 2.5,
            "rewards_min_size": 100.0,
            "tokens": [{"token_id": "niche_tok_0", "price": 0.5}, {"token_id": "niche_tok_1", "price": 0.5}],
        },
    ]

    # Page 1 (offset 500): 1 pool with rate 50.0, 1 pool with rate 0.50 (sub-floor)
    page_1 = [
        {
            "condition_id": "0xMID_COND",
            "question": "Mid Pool: Tech Company Earnings",
            "rewards_config": [{"rate_per_day": 50.0}],
            "rewards_max_spread": 2.5,
            "rewards_min_size": 100.0,
            "tokens": [{"token_id": "mid_tok_0", "price": 0.5}, {"token_id": "mid_tok_1", "price": 0.5}],
        },
        {
            "condition_id": "0xSUB_COND",
            "question": "Subfloor Pool",
            "rewards_config": [{"rate_per_day": 0.50}],
            "rewards_max_spread": 2.5,
            "rewards_min_size": 100.0,
            "tokens": [{"token_id": "sub_tok_0", "price": 0.5}, {"token_id": "sub_tok_1", "price": 0.5}],
        },
    ]

    class MockResponse:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code

        def json(self):
            return self._data

    def mock_get(url, *args, **kwargs):
        if "rewards/markets/multi" in url:
            # The scanner derives the base64 cursor from the numeric offset:
            # "NTAw" == 500. Page 0 is short but carries a non-terminal cursor;
            # the scanner must follow it instead of treating the short page as
            # the end of the universe.
            if "next_cursor=NTAw" in url:
                return MockResponse({"data": page_1, "next_cursor": "LTE="})
            return MockResponse({"data": page_0, "next_cursor": "PAGE_2"})
        return MockResponse([], status_code=404)

    def mock_post(url, *args, **kwargs):
        if "books" in url:
            payload = kwargs.get("json", [])
            resp_books = []
            for it in payload:
                tid = it.get("token_id")
                # Mega pool has HUGE competition: 150,000 shares on bid and ask
                if "mega" in tid:
                    resp_books.append({"asset_id": tid, "bids": [{"price": "0.49", "size": "150000.0"}], "asks": [{"price": "0.51", "size": "150000.0"}]})
                # Niche pool has TINY competition: 500 shares
                elif "niche" in tid:
                    resp_books.append({"asset_id": tid, "bids": [{"price": "0.49", "size": "500.0"}], "asks": [{"price": "0.51", "size": "500.0"}]})
                elif "mid" in tid:
                    resp_books.append({"asset_id": tid, "bids": [{"price": "0.49", "size": "2000.0"}], "asks": [{"price": "0.51", "size": "2000.0"}]})
            return MockResponse(resp_books)
        return MockResponse([], status_code=404)

    monkeypatch.setattr(daemon.http_session, "get", mock_get)
    monkeypatch.setattr(daemon.http_session, "post", mock_post)

    candidates = daemon.scan_reward_universe(min_rate=1.00, page_size=500)

    # Subfloor pool (rate 0.50) must be excluded; 3 valid pools remain
    assert len(candidates) == 3
    cand_map = {c.pool_id: c for c in candidates}

    # Verify Niche pool has FAR HIGHER marginal yield mu(0) than Mega pool
    niche = cand_map["0xNICHE_COND"]
    mega = cand_map["0xMEGA_COND"]

    # Marginal return mu(0) = (rate * alpha) / competing_score
    assert niche.marginal_at_zero > mega.marginal_at_zero
    # Niche candidate is ranked #1 by marginal return despite lower daily pool rate!
    assert candidates[0].pool_id == "0xNICHE_COND"


def test_reward_radar_to_harvester_thread_integration(tmp_path: Path, monkeypatch) -> None:
    """Verifies that Reward Universe Radar primes candidate queue and Harvester allocates."""
    from scripts.run_live_paper_soak import LivePaperSoakDaemon, MarginalRewardAllocator

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="none", duration=0.1)

    mock_pool = [
        {
            "condition_id": "0xPRIME_COND",
            "question": "Prime Reward Pool",
            "rewards_config": [{"rate_per_day": 100.0}],
            "rewards_max_spread": 2.5,
            "rewards_min_size": 50.0,
            "tokens": [{"token_id": "prime_tok_0", "price": 0.5}, {"token_id": "prime_tok_1", "price": 0.5}],
        }
    ]

    class MockResp:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

    def mock_get(url, *args, **kwargs):
        if "rewards/markets/multi" in url:
            return MockResp({"data": mock_pool, "next_cursor": "LTE="})
        return MockResp({"data": []})

    def mock_post(url, *args, **kwargs):
        payload = kwargs.get("json", [])
        return MockResp([{"asset_id": it["token_id"], "bids": [{"price": "0.49", "size": "100.0"}], "asks": [{"price": "0.51", "size": "100.0"}]} for it in payload])

    monkeypatch.setattr(daemon.http_session, "get", mock_get)
    monkeypatch.setattr(daemon.http_session, "post", mock_post)

    candidates = daemon.scan_reward_universe(min_rate=1.00, page_size=500)
    assert len(candidates) == 1
    assert candidates[0].pool_id == "0xPRIME_COND"

    with daemon.reward_queue_lock:
        daemon.reward_candidate_queue = candidates

    # Verify Harvester allocation using MarginalRewardAllocator
    allocs = MarginalRewardAllocator.allocate(candidates, total_budget=1000.0)
    assert "0xPRIME_COND" in allocs
    assert allocs["0xPRIME_COND"] == 1000.0


def test_ctf_minter_multi_offset_universe_and_arbitrage_execution(tmp_path: Path, monkeypatch) -> None:
    """Verifies that CtfMinter scans multi-offset universe, respects real fee schedules, and captures mint/merge arbitrage."""
    import json
    from scripts.run_live_paper_soak import LivePaperSoakDaemon, SoakTelemetryDb, DEFAULT_GAMMA_HOST, DEFAULT_CLOB_HOST

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="bronze", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "test_ctf_soak.db")

    # Verify min_net_edge_usd is calibrated to 0.0015
    assert daemon.ctf_allocator.min_net_edge_usd == 0.0015

    # Market 1: Mint Arbitrage (Bids 0.52 + 0.51 = 1.03 > 1.00 par)
    m_mint = {
        "id": "1001",
        "conditionId": "0xMINT_COND",
        "question": "Dislocated Mint Market",
        "clobTokenIds": json.dumps(["tok_mint_0", "tok_mint_1"]),
        "feeSchedule": None,  # 0% fee
    }

    # Market 2: Merge Arbitrage (Asks 0.48 + 0.49 = 0.97 < 1.00 par)
    m_merge = {
        "id": "1002",
        "conditionId": "0xMERGE_COND",
        "question": "Dislocated Merge Market",
        "clobTokenIds": json.dumps(["tok_merge_0", "tok_merge_1"]),
        "feeSchedule": {"rate": 0.0, "exponent": 1.0, "takerOnly": True},
    }

    # Market 3: Pinned market (Bids 0.49 + 0.49 = 0.98, Asks 0.51 + 0.51 = 1.02)
    m_pinned = {
        "id": "1003",
        "conditionId": "0xPINNED_COND",
        "question": "Pinned Normal Market",
        "clobTokenIds": json.dumps(["tok_pin_0", "tok_pin_1"]),
        "feeSchedule": None,
    }

    class MockResp:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code

        def json(self):
            return self._data

    def mock_get(url, *args, **kwargs):
        if DEFAULT_GAMMA_HOST in url:
            # Full-universe discovery now uses stable id ordering. Page 0 holds
            # the three fixtures; every later page is short/empty so discovery stops.
            if "order=id" in url and "offset=0" in url:
                return MockResp([m_mint, m_merge, m_pinned])
            return MockResp([])
        if "data-api.polymarket.com/v2/trades" in url:
            # Measured taker-flow tape: SELL prints drive the empirical fill-size
            # model and the measured seller-initiated share.
            return MockResp({
                "data": [
                    {"side": "SELL", "size": 10.0, "price": 0.5},
                    {"side": "SELL", "size": 20.0, "price": 0.5},
                    {"side": "SELL", "size": 5.0, "price": 0.5},
                    {"side": "BUY", "size": 15.0, "price": 0.5},
                ]
            })
        return MockResp([], status_code=404)

    def mock_post(url, *args, **kwargs):
        if DEFAULT_CLOB_HOST in url and "books" in url:
            payload = kwargs.get("json", [])
            books = []
            for it in payload:
                tid = it.get("token_id")
                if tid == "tok_mint_0":
                    books.append({"asset_id": tid, "bids": [{"price": "0.52", "size": "100.0"}], "asks": [{"price": "0.53", "size": "100.0"}]})
                elif tid == "tok_mint_1":
                    books.append({"asset_id": tid, "bids": [{"price": "0.51", "size": "100.0"}], "asks": [{"price": "0.52", "size": "100.0"}]})
                elif tid == "tok_merge_0":
                    books.append({"asset_id": tid, "bids": [{"price": "0.47", "size": "100.0"}], "asks": [{"price": "0.48", "size": "100.0"}]})
                elif tid == "tok_merge_1":
                    books.append({"asset_id": tid, "bids": [{"price": "0.48", "size": "100.0"}], "asks": [{"price": "0.49", "size": "100.0"}]})
                elif tid in ("tok_pin_0", "tok_pin_1"):
                    books.append({"asset_id": tid, "bids": [{"price": "0.49", "size": "500.0"}], "asks": [{"price": "0.51", "size": "500.0"}]})
            return MockResp(books)
        return MockResp([], status_code=404)

    monkeypatch.setattr(daemon.http_session, "get", mock_get)
    monkeypatch.setattr(daemon.http_session, "post", mock_post)
    monkeypatch.setattr(daemon.ctf_http_session, "get", mock_get)
    monkeypatch.setattr(daemon.ctf_http_session, "post", mock_post)

    # Gas/POL are measured from venue oracles at refresh time; serve deterministic
    # measured values so the test does not depend on external network latency.
    import polymarketbot.trading.ctf as ctf_oracle
    monkeypatch.setattr(ctf_oracle, "get_dynamic_gas_price_gwei", lambda *a, **k: 40.0)
    monkeypatch.setattr(ctf_oracle, "get_dynamic_pol_usd_price", lambda *a, **k: 0.092)

    # Launch CtfMinter thread for 0.4s and stop
    daemon.running = True
    t = threading.Thread(target=daemon._run_ctf_minter_loop, daemon=True)
    t.start()
    time.sleep(0.4)
    daemon.running = False
    t.join(timeout=1.0)

    # Check metrics and trades in DB
    metrics = daemon.metrics["CtfMinter"]
    assert metrics.total_trades >= 2, f"Expected >= 2 trades, got {metrics.total_trades}"
    assert metrics.gross_pnl > 0.0
    assert metrics.net_pnl > 0.0

    conn = daemon.db._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT side, condition_id, gross_pnl, net_pnl FROM soak_trades WHERE strategy='CtfMinter'")
    trades = cur.fetchall()
    conn.close()

    assert len(trades) >= 2
    sides = {tr[0] for tr in trades}
    assert "CTF_SPLIT_MINT" in sides
    assert "CTF_MERGE_REDEEM" in sides


def test_gamma_universe_pagination_422_is_end_not_failure() -> None:
    """422 at a non-zero offset is end-of-universe; pages read before it are kept."""
    import re
    from scripts.run_live_paper_soak import _paginate_gamma_universe

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Session:
        def __init__(self):
            self.offsets = []

        def get(self, url, *args, **kwargs):
            offset = int(re.search(r"offset=(\d+)", url).group(1))
            self.offsets.append(offset)
            if offset >= 2300:
                return _Resp(422, {"error": "offset too large"})
            return _Resp(200, [{"id": str(offset + i)} for i in range(100)])

    session = _Session()
    markets, pages, reason = _paginate_gamma_universe(
        session, page_size=100, wave=8, is_running=lambda: True
    )

    assert markets is not None
    assert len(markets) == 2300
    assert pages == 24
    assert "end_of_universe" in reason and "2300" in reason
    assert 2300 in session.offsets

    # A failure before any successful page is still a failed refresh.
    class _FailSession:
        def get(self, url, *args, **kwargs):
            return _Resp(500, [])

    failed_markets, _, failed_reason = _paginate_gamma_universe(
        _FailSession(), page_size=100, wave=8, is_running=lambda: True
    )
    assert failed_markets is None
    assert failed_reason == "failure_before_first_page"


def test_settlement_radar_multi_stratum_uncapped_coverage(tmp_path: Path, monkeypatch) -> None:
    """Full pagination walks every stratum past the old fixed 800/600/400 caps."""
    import re as _re

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="none", duration=0.1)
    daemon.db = SoakTelemetryDb(tmp_path / "soak_test_strata.db")

    probed_urls = []

    class MockResponse:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code

        def json(self):
            return self._data

    def mock_get(url, *args, **kwargs):
        probed_urls.append(url)
        match = _re.search(r"[?&]offset=(\d+)", url)
        offset = int(match.group(1)) if match else 0
        if offset >= 2400:
            return MockResponse([])  # short page terminates the stratum
        return MockResponse([{"conditionId": f"0x{offset}_{i}"} for i in range(100)])

    monkeypatch.setattr(daemon.http_session, "get", mock_get)

    daemon.running = True
    radar_thread = threading.Thread(target=daemon._run_settlement_radar_thread, daemon=True)
    radar_thread.start()
    time.sleep(1.0)
    daemon.running = False
    radar_thread.join(timeout=2.0)

    # Five strata, each paginated through the old caps and past offset 2000
    # until the short page: no fixed 800/600/400 market caps.
    hits_2000 = [u for u in probed_urls if "offset=2000" in u]
    hits_2400 = [u for u in probed_urls if "offset=2400" in u]
    assert len(hits_2000) == 5, f"Expected 5 strata at offset=2000, got {len(hits_2000)}"
    assert len(hits_2400) == 5, f"Expected 5 strata at offset=2400, got {len(hits_2400)}"
    assert len(probed_urls) >= 5 * 25, f"Expected >= 125 pages, got {len(probed_urls)}"

    # Verify presence of all sub-horizon slices (0-6h, 6-24h, 24-48h)
    assert any("end_date_min=" in u and "end_date_max=" in u for u in probed_urls)
    assert any("end_date_max=" in u and "ascending=false" in u for u in probed_urls)
    assert any("end_date_max=" in u and "ascending=true" in u for u in probed_urls)




