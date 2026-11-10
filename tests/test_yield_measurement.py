"""Yield-measurement tests: fills, markouts, rebalances, reward payouts, report.

Everything runs against local fakes and a temp SQLite database: no CLOB REST,
no RPC, no network.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from polymarketbot.reports.market_yield import build_report
from polymarketbot.rewards.deskew import DeSkewDecision, DeSkewPathway
from polymarketbot.trading.client import OrderPlacementResult
from polymarketbot.trading.ctf import MergeExecutionResult
from polymarketbot.trading.engine import MarkoutTracker, RealMoneyTradingEngine


class FakeCredentials:
    is_configured = True
    has_signing_key = True
    address = "0xMAKER"

    def validate_formatting(self) -> list[str]:
        return []


class FakeChain:
    def block_number(self) -> int:
        return 1


class FakeClob:
    dry_run = False
    order_version = 2
    _order_version = 2
    maker_address = "0xMAKER"
    signer_address = "0xMAKER"

    def __init__(self, available: float = 100.0) -> None:
        self.available = available
        self.credentials = FakeCredentials()
        self.open_orders: list[dict] = []
        self.order_by_id: dict[str, dict] = {}
        self.placed: list[dict] = []
        self.daily_rewards: dict = {"daily_combined_usd": 0.0, "source": "simulated"}

    def get_balance(self) -> dict:
        return {
            "available": self.available,
            "locked": 0.0,
            "allowance": 1_000_000.0,
            "raw": {},
        }

    def get_allowances(self) -> dict:
        return {"approved": True, "collateral": 1_000_000.0, "conditional": 0.0, "raw": {}}

    def get_open_orders(self, market=None, *, order_id=None, max_pages=20):
        return list(self.open_orders)

    def get_order(self, order_id: str):
        return self.order_by_id.get(order_id)

    def cancel_order(self, order_id: str) -> bool:
        self.open_orders = [o for o in self.open_orders if o.get("id") != order_id]
        self.order_by_id.pop(order_id, None)
        return True

    def place_order(self, token_id, side, price, size, **kwargs) -> OrderPlacementResult:
        order_id = f"ord_{len(self.placed) + 1}"
        self.placed.append(
            {"token_id": token_id, "side": side, "price": price, "size": size, **kwargs}
        )
        return OrderPlacementResult(
            success=True,
            order_id=order_id,
            status="LIVE",
            side=side,
            price=price,
            size=size,
            is_dry_run=True,
        )

    def get_total_rewards(self) -> dict:
        return {"reward_income_usd": 0.0, "maker_rebates_usd": 0.0, "source": "simulated"}

    def get_daily_rewards(self, date=None) -> dict:
        return dict(self.daily_rewards)

    def get_order_scoring(self, order_id: str) -> dict:
        return {"scoring": False}


class FakeCtf:
    dry_run = False
    private_key = "0xPRIVATEKEYMUSTNEVERLEAK"
    is_settlement_enabled = False

    def __init__(self) -> None:
        self.chain_client = FakeChain()
        self.position_balances: dict[str, float] = {}
        self.merge_calls: list[dict] = []

    def estimate_merge_cost_usd(self) -> float:
        return 0.01

    def calculate_optimal_merge_threshold(self, gas_cost_usd: float = 0.01) -> float:
        return 1.0

    def get_pol_balance(self, address: str) -> float:
        return 1.0

    def get_position_balance(self, token_id: str, address: str) -> float:
        return float(self.position_balances.get(str(token_id), 0.0))

    def merge_positions(self, condition_id, amount_shares, **kwargs) -> MergeExecutionResult:
        self.merge_calls.append({"condition_id": condition_id, "amount_shares": amount_shares})
        return MergeExecutionResult(
            success=True,
            tx_hash="0xmock",
            condition_id=condition_id,
            complete_sets_merged=amount_shares,
            usdc_unlocked=amount_shares,
            gas_fee_usd=0.01,
            gas_units_used=1,
            is_dry_run=False,
        )


def make_engine(tmp_path, *, fee_rate_bps: int = 0, dry_run: bool = True):
    clob = FakeClob()
    ctf = FakeCtf()
    engine = RealMoneyTradingEngine(
        condition_id="0xCOND",
        asset_id_yes="1111",
        asset_id_no="2222",
        clob_client=clob,
        ctf_client=ctf,
        initial_cash=100.0,
        db_path=tmp_path / "engine.db",
        ledger_path=tmp_path / "ledger.json",
        dry_run=dry_run,
        fee_rate_bps=fee_rate_bps,
        gas_fee_merge=0.01,
        auto_merge_threshold=1.0,
        merge_cooldown_seconds=0.0,
    )
    return engine, clob, ctf


def prime_book(engine, tok: str = "YES", bid: float = 0.40, ask: float = 0.42) -> None:
    engine._best_bids[tok] = bid
    engine._best_asks[tok] = ask
    engine._book_ts[tok] = time.time()


def fetch(engine, sql: str, params: tuple = ()):
    engine._conn.row_factory = sqlite3.Row
    return list(engine._conn.execute(sql, params).fetchall())


def quote(order_id="ord_1", *, side="BUY", size=10.0, price=0.40, matched=0.0, is_taker=False):
    return {
        "quote_id": f"{side}_YES",
        "order_id": order_id,
        "market": "0xCOND",
        "token_side": "YES",
        "side": side,
        "price": price,
        "size": size,
        "size_matched": matched,
        "is_taker": is_taker,
        "status": "LIVE",
    }


@pytest.fixture
def engine(tmp_path):
    eng, clob, ctf = make_engine(tmp_path)
    yield eng
    eng.close()


def test_fill_row_written_once_per_fill_key(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path)
    try:
        prime_book(eng)
        qdata = quote()
        ts = time.time()
        eng._book_exchange_fill(qdata, "YES", "BUY", 10.0, 0.40, False, ts, "unit")
        eng._book_exchange_fill(qdata, "YES", "BUY", 10.0, 0.40, False, ts, "unit")

        rows = fetch(eng, "SELECT * FROM fills")
        assert len(rows) == 1
        row = rows[0]
        assert row["fill_key"].startswith("ord_1:")
        assert row["condition_id"] == "0xCOND"
        assert row["token_side"] == "YES"
        assert row["side"] == "BUY"
        assert row["price"] == pytest.approx(0.40)
        assert row["size"] == pytest.approx(10.0)
        assert row["is_maker"] == 1
        assert row["mode"] == "DRY_RUN"
        assert row["mid_at_fill"] == pytest.approx(0.41)
        assert row["best_bid_at_fill"] == pytest.approx(0.40)
        assert row["best_ask_at_fill"] == pytest.approx(0.42)
        assert row["markout_30s"] is None
    finally:
        eng.close()


def test_sync_live_orders_books_one_row_across_repeats(tmp_path):
    eng, clob, _ctf = make_engine(tmp_path)
    try:
        prime_book(eng)
        eng.active_quotes["BUY_YES"] = quote(matched=0.0)
        clob.open_orders = [
            {
                "id": "ord_1",
                "market": "0xCOND",
                "price": "0.40",
                "original_size": "10",
                "size_matched": "4",
                "status": "LIVE",
            }
        ]
        first = eng.sync_live_orders_and_fills(timestamp=time.time())
        second = eng.sync_live_orders_and_fills(timestamp=time.time() + 1.0)
        assert len(first) == 1
        assert second == []

        rows = fetch(eng, "SELECT * FROM fills")
        assert len(rows) == 1
        assert rows[0]["size"] == pytest.approx(4.0)
        assert rows[0]["price"] == pytest.approx(0.40)
        assert rows[0]["order_id"] == "ord_1"
    finally:
        eng.close()


def test_fill_marks_null_when_book_stale(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path)
    try:
        eng._best_bids["YES"] = 0.40
        eng._best_asks["YES"] = 0.42
        eng._book_ts["YES"] = time.time() - 3600.0
        eng._book_exchange_fill(quote(), "YES", "BUY", 10.0, 0.40, False, time.time(), "stale")
        row = fetch(eng, "SELECT * FROM fills")[0]
        assert row["mid_at_fill"] is None
        assert row["best_bid_at_fill"] is None
        assert row["best_ask_at_fill"] is None
    finally:
        eng.close()


def test_markout_tracker_writes_each_horizon_once(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path)
    try:
        prime_book(eng, "YES", 0.40, 0.42)
        now = 1_000_000.0
        clock = [now]
        tracker = MarkoutTracker(
            conn=eng._conn,
            mid_getter=eng._fresh_mid,
            lock=eng._lock,
            clock=lambda: clock[0],
            autostart=False,
        )
        eng._insert_yield_fill_row(
            {
                "fill_key": "k1",
                "ts": now,
                "condition_id": "0xCOND",
                "token_side": "YES",
                "side": "BUY",
                "price": 0.40,
                "size": 10.0,
                "is_maker": 1,
                "order_id": "ord_1",
                "fee_usd": 0.0,
                "mid_at_fill": 0.41,
                "markout_30s": None,
                "markout_5m": None,
                "markout_30m": None,
                "realized_pnl": 0.0,
                "mode": "DRY_RUN",
                "note": "test",
            }
        )
        tracker.record("k1", "YES", 0.40, "BUY", now=now)
        assert tracker.pending() == 3

        clock[0] = now + 31.0
        assert tracker.process_due() == 1
        row = fetch(eng, "SELECT * FROM fills WHERE fill_key='k1'")[0]
        assert row["markout_30s"] == pytest.approx(0.01)
        assert row["markout_5m"] is None
        assert row["markout_30m"] is None

        clock[0] = now + 301.0
        assert tracker.process_due() == 1
        row = fetch(eng, "SELECT * FROM fills WHERE fill_key='k1'")[0]
        assert row["markout_5m"] == pytest.approx(0.01)

        clock[0] = now + 1801.0
        assert tracker.process_due() == 1
        row = fetch(eng, "SELECT * FROM fills WHERE fill_key='k1'")[0]
        assert row["markout_30m"] == pytest.approx(0.01)
        assert tracker.pending() == 0

        # Never overwrite an existing markout.
        tracker.record("k1", "YES", 0.40, "BUY", now=now)
        clock[0] = now + 1900.0
        tracker.process_due()
        row = fetch(eng, "SELECT * FROM fills WHERE fill_key='k1'")[0]
        assert row["markout_30s"] == pytest.approx(0.01)
    finally:
        eng.close()


def test_markout_side_sign_and_unmeasurable_horizon(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path)
    try:
        prime_book(eng, "YES", 0.40, 0.42)
        now = 2_000_000.0
        clock = [now]
        tracker = MarkoutTracker(
            conn=eng._conn,
            mid_getter=eng._fresh_mid,
            lock=eng._lock,
            clock=lambda: clock[0],
            autostart=False,
        )
        eng._insert_yield_fill_row(
            {
                "fill_key": "k2",
                "ts": now,
                "condition_id": "0xCOND",
                "token_side": "YES",
                "side": "SELL",
                "price": 0.45,
                "size": 5.0,
                "markout_30s": None,
                "mode": "DRY_RUN",
            }
        )
        tracker.record("k2", "YES", 0.45, "SELL", now=now)
        clock[0] = now + 31.0
        tracker.process_due()
        row = fetch(eng, "SELECT * FROM fills WHERE fill_key='k2'")[0]
        assert row["markout_30s"] == pytest.approx(0.45 - 0.41)

        # Stale book at the due time -> markout stays NULL (no backfill).
        eng._book_ts["YES"] = time.time() - 3600.0
        clock[0] = now + 1801.0
        tracker.process_due()
        row = fetch(eng, "SELECT * FROM fills WHERE fill_key='k2'")[0]
        assert row["markout_5m"] is None
        assert row["markout_30m"] is None
    finally:
        eng.close()


def test_dry_run_tier2_records_fill_and_rebalance(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path, fee_rate_bps=100)
    try:
        prime_book(eng)
        eng.shares_yes = 50.0
        eng.avg_cost_yes = 0.30
        decision = DeSkewDecision(
            pathway=DeSkewPathway.DIRECT_CROSS,
            urgency_level="CRITICAL_PANIC",
            target_token="YES",
            action_side="SELL",
            order_type="MARKET_TAKER",
            target_price=0.40,
            target_size=5.0,
            expected_cost_bps=10.0,
            synthetic_savings_cents=2.0,
            reason="unit test cross",
        )
        eng._execute_tier2_dry_run(decision, time.time())

        fills = fetch(eng, "SELECT * FROM fills")
        assert len(fills) == 1
        assert fills[0]["side"] == "SELL"
        assert fills[0]["is_maker"] == 0

        rows = fetch(eng, "SELECT * FROM rebalances")
        assert len(rows) == 1
        row = rows[0]
        assert row["pathway"] == "TIER2_DIRECT_CROSS"
        assert row["crossed_cents"] == pytest.approx(0.0, abs=1e-9)
        assert row["taker_fee_usd"] == pytest.approx(5.0 * 0.40 * 0.01)
        assert row["gas_usd"] == pytest.approx(0.0)
        assert row["inventory_before"] == pytest.approx(50.0)
        assert row["inventory_after"] == pytest.approx(45.0)
    finally:
        eng.close()


def test_live_taker_fill_records_cross_cost(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path, fee_rate_bps=100)
    try:
        eng.dry_run = False
        eng._locked_already_in_balance = False
        eng._cost_basis_unknown = False
        prime_book(eng)
        qdata = quote(order_id="ord_t", side="BUY", price=0.42, size=10.0, is_taker=True)
        qdata["decision_target_price"] = 0.40
        eng._book_exchange_fill(qdata, "YES", "BUY", 10.0, 0.41, True, time.time(), "taker")

        fills = fetch(eng, "SELECT * FROM fills")
        assert len(fills) == 1
        assert fills[0]["mode"] == "LIVE"
        assert fills[0]["is_maker"] == 0
        assert fills[0]["fee_usd"] > 0.0

        rows = fetch(eng, "SELECT * FROM rebalances")
        assert len(rows) == 1
        assert rows[0]["pathway"] == "TIER2_TAKER"
        assert rows[0]["crossed_cents"] == pytest.approx(1.0)  # |0.41 - 0.40| * 100
        assert rows[0]["taker_fee_usd"] == pytest.approx(10.0 * 0.41 * 0.01)
        assert rows[0]["inventory_before"] == pytest.approx(0.0)
        assert rows[0]["inventory_after"] == pytest.approx(10.0)
    finally:
        eng.close()


def test_merge_records_gas_cost(tmp_path):
    eng, _clob, ctf = make_engine(tmp_path)
    try:
        eng.shares_yes = 10.0
        eng.shares_no = 10.0
        res = eng.trigger_ctf_merge()
        assert res is not None and res.success
        assert ctf.merge_calls, "merge was not broadcast to the CTF client"

        rows = fetch(eng, "SELECT * FROM rebalances")
        assert len(rows) == 1
        row = rows[0]
        assert row["pathway"] == "MERGE"
        assert row["gas_usd"] == pytest.approx(0.01)
        assert row["crossed_cents"] == pytest.approx(0.0)
        assert row["taker_fee_usd"] == pytest.approx(0.0)
        assert row["inventory_before"] == pytest.approx(10.0)
        assert row["inventory_after"] == pytest.approx(0.0)
        assert "merge" in row["note"]
    finally:
        eng.close()


def test_merge_cost_not_double_counted_for_same_tx(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path)
    try:
        for _ in range(2):
            eng.shares_yes = 10.0
            eng.shares_no = 10.0
            res = eng.trigger_ctf_merge()
            assert res is not None and res.success
        # The fake venue returns the same tx hash: one cost row, not two.
        assert len(fetch(eng, "SELECT * FROM rebalances")) == 1
    finally:
        eng.close()


def test_hold_stable_writes_no_rebalance(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path)
    try:
        decision = DeSkewDecision(
            pathway=DeSkewPathway.HOLD_STABLE,
            urgency_level="LOW",
            target_token="NONE",
            action_side="BUY",
            order_type="LIMIT_MAKER",
            target_price=0.50,
            target_size=0.0,
            expected_cost_bps=0.0,
            synthetic_savings_cents=0.0,
            reason="within neutrality zone",
        )
        eng._record_deskew_event(decision, time.time())
        assert fetch(eng, "SELECT * FROM rebalances") == []
    finally:
        eng.close()


def test_tier1_fade_records_reward_score_lost(tmp_path):
    eng, _clob, _ctf = make_engine(tmp_path)
    try:
        prime_book(eng, "YES", 0.40, 0.42)
        prime_book(eng, "NO", 0.58, 0.60)
        eng.shares_yes = 40.0
        eng.avg_cost_yes = 0.40
        decision = DeSkewDecision(
            pathway=DeSkewPathway.PASSIVE_FADE,
            urgency_level="MEDIUM",
            target_token="NO",
            action_side="BUY",
            order_type="LIMIT_MAKER",
            target_price=0.58,
            target_size=40.0,
            expected_cost_bps=0.0,
            synthetic_savings_cents=1.0,
            reason="long YES skew",
        )
        eng._execute_tier1_passive_fade(decision, 0.41, 0.40, 0.42, time.time())

        rows = fetch(eng, "SELECT * FROM rebalances")
        assert len(rows) == 1
        row = rows[0]
        assert row["pathway"] == "PASSIVE_FADE"
        assert row["reward_score_lost"] >= 0.0
        assert row["crossed_cents"] == pytest.approx(0.0)
        assert row["taker_fee_usd"] == pytest.approx(0.0)
        assert row["gas_usd"] == pytest.approx(0.0)
        assert row["note"].startswith("tier1 fade")
    finally:
        eng.close()


def test_reward_payouts_only_from_real_api_and_idempotent(tmp_path):
    eng, clob, _ctf = make_engine(tmp_path)
    try:
        clob.daily_rewards = {
            "date": "2026-01-02",
            "daily_combined_usd": 1.9,
            "source": "clob_api",
            "market_breakdown": [
                {"condition_id": "0xA", "earned_usd": 1.5},
                {"condition_id": "0xB", "earned_usd": 0.4},
            ],
        }
        eng.sync_rewards(force=True)
        rows = fetch(eng, "SELECT * FROM reward_payouts ORDER BY condition_id")
        assert len(rows) == 2
        assert rows[0]["condition_id"] == "0xA"
        assert rows[0]["usd"] == pytest.approx(1.5)
        assert rows[0]["date"] == "2026-01-02"
        assert rows[0]["source"] == "clob_api"
        assert rows[0]["observed_ts"] > 0.0

        # Same payload again -> idempotent.
        eng._last_reward_sync = 0.0
        eng.sync_rewards(force=True)
        assert len(fetch(eng, "SELECT * FROM reward_payouts")) == 2

        # Unavailable / simulated answers write nothing.
        clob.daily_rewards = {
            "date": "2026-01-03",
            "source": "unavailable",
            "market_breakdown": [{"condition_id": "0xC", "earned_usd": 9.9}],
        }
        eng._last_reward_sync = 0.0
        eng.sync_rewards(force=True)
        clob.daily_rewards = {
            "date": "2026-01-04",
            "source": "simulated",
            "market_breakdown": [{"condition_id": "0xD", "earned_usd": 9.9}],
        }
        eng._last_reward_sync = 0.0
        eng.sync_rewards(force=True)
        assert len(fetch(eng, "SELECT * FROM reward_payouts")) == 2
    finally:
        eng.close()


def test_report_empty_db_says_insufficient_data(tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE fills (fill_key TEXT PRIMARY KEY, ts REAL, condition_id TEXT, realized_pnl REAL)"
    )
    conn.execute(
        "CREATE TABLE rebalances (id INTEGER PRIMARY KEY, ts REAL, condition_id TEXT, "
        "crossed_cents REAL, taker_fee_usd REAL, gas_usd REAL, inventory_before REAL, "
        "inventory_after REAL)"
    )
    conn.commit()
    conn.close()
    report = build_report(db)
    assert "insufficient data (0 fills, 0 rebalances)" in report


def test_report_reward_only_prints_reward_yield(tmp_path):
    db = tmp_path / "rewards.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE reward_payouts (date TEXT, condition_id TEXT, usd REAL, "
        "source TEXT, observed_ts REAL, PRIMARY KEY (date, condition_id, source))"
    )
    conn.execute(
        "CREATE TABLE reward_epochs (id INTEGER PRIMARY KEY, epoch_index INTEGER, "
        "timestamp REAL, condition_id TEXT, user_score REAL, competing_score REAL, "
        "pool_share_pct REAL, epoch_payout_usd REAL, projected_daily_usd REAL, "
        "floor_met INTEGER, cumulative_rewards_usd REAL)"
    )
    ts = 1_767_312_000.0  # 2026-01-02T00:00:00Z
    conn.execute(
        "INSERT INTO reward_payouts VALUES (?, ?, ?, ?, ?)",
        ("2026-01-02", "0xC", 1.25, "clob_api", ts),
    )
    conn.execute(
        "INSERT INTO reward_epochs (epoch_index, timestamp, condition_id, user_score, "
        "competing_score, pool_share_pct, epoch_payout_usd, projected_daily_usd, "
        "floor_met, cumulative_rewards_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, ts, "0xC", 10.0, 90.0, 10.0, 0.75, 1.0, 1, 0.75),
    )
    conn.commit()
    conn.close()

    report = build_report(db)
    line = [ln for ln in report.splitlines() if ln.startswith("0xC")][0]
    assert "1.2500" in line
    assert "0.7500" in line
    assert "0.5000" in line
    assert "insufficient data (0 fills, 0 rebalances)" in line
