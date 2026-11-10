from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq

from polymarketbot.trading.session_recorder import LiveSessionRecorder


class FakeClob:
    maker_address = "0xabc"
    credentials = SimpleNamespace(api_key="abcdef123456", api_secret="secret", passphrase="pass")

    def get_open_orders(self, market=None):
        return [{"id": "order-1", "status": "LIVE", "size": 10.0, "price": 0.5}]

    def get_trades(self, market=None):
        return [{"id": "trade-1", "asset_id": "yes", "price": 0.5, "size": 10.0}]

    def get_balance(self):
        return {"balance": 100.0, "available": 100.0}

    def get_allowances(self):
        return {"allowance": 100.0}

    def get_total_rewards(self):
        return {"total": 0.0}

    def get_daily_rewards(self):
        return []


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE fills (
               fill_key TEXT PRIMARY KEY, ts REAL, condition_id TEXT, side TEXT,
               price REAL, size REAL, order_id TEXT, fee_usd REAL)"""
    )
    conn.execute(
        """CREATE TABLE portfolio_snapshots (
               timestamp REAL, cash REAL, market_value REAL, equity REAL)"""
    )
    conn.commit()
    conn.close()


def _rows(folder: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(folder.glob("*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    return rows


def test_recorder_writes_engine_venue_and_hook_rows(tmp_path) -> None:
    db = tmp_path / "engine.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fills VALUES ('fill-stale', 0.5, '0xold', 'BUY', 0.5, 1.0, 'old', 0.0)"
    )
    conn.commit()
    conn.close()
    recorder = LiveSessionRecorder(
        db_path=db,
        clob=FakeClob(),
        market={
            "condition_id": "0xcid",
            "question": "q",
            "token_yes": "yes",
            "token_no": "no",
            "event_id": "123",
            "min_size": 5.0,
            "max_spread": 3.0,
            "daily_reward": 10.0,
            "neg_risk": False,
            "tick_size": 0.01,
        },
        dry_run=True,
        out_root=tmp_path / "live",
        session_id="test-session",
        engine_interval=0.05,
        venue_interval=0.05,
        account_interval=0.05,
        positions_interval=10.0,
        rewards_interval=10.0,
        health_interval=0.05,
        flush_interval=0.05,
        use_data_api=False,
    )
    recorder.start()
    # Rows written before the session start must be skipped; rows written after
    # it (by the engine) must be tailed.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fills VALUES ('fill-1', 1.0, '0xcid', 'BUY', 0.5, 10.0, 'order-1', 0.0)"
    )
    conn.execute("INSERT INTO portfolio_snapshots VALUES (1.0, 100.0, 50.0, 150.0)")
    conn.commit()
    conn.close()
    recorder.record_event("quote_placed", {"quote_id": "q1", "price": 0.5, "size": 10})
    recorder.record_event("risk_blocked", {"reason": "event cap", "requested_size": 10})
    time.sleep(0.4)
    recorder.stop("test")

    root = tmp_path / "live" / "test-session"
    manifest = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert manifest["condition_id"] == "0xcid"
    assert manifest["event_id"] == "123"
    assert manifest["wallet"] == "0xabc"
    assert "api_secret" not in json.dumps(manifest)
    assert manifest["api_key_id"] == "abcdef12"
    assert manifest["end"]["reason"] == "test"

    fills = _rows(root / "fills")
    sources = {row["source"] for row in fills}
    assert "engine" in sources and "venue" in sources
    assert any(row.get("fill_key") == "fill-1" for row in fills)
    assert any(row.get("id") == "trade-1" for row in fills)
    assert all(row.get("fill_key") != "fill-stale" for row in fills)

    quotes = _rows(root / "quotes")
    assert any(row["kind"] == "quote_placed" and row["quote_id"] == "q1" for row in quotes)

    risk = _rows(root / "risk_events")
    assert any(row["kind"] == "risk_blocked" and row["reason"] == "event cap" for row in risk)

    account = _rows(root / "account")
    assert any(row.get("usdc_balance") == 100.0 for row in account)


def test_user_event_parser_maps_orders_trades_and_unknowns() -> None:
    from polymarketbot.trading.session_recorder import _parse_user_event

    table, row = _parse_user_event(
        {"type": "order", "market": "0xcid", "asset_id": "yes", "id": "o-1", "status": "LIVE"}
    )
    assert table == "user_orders"
    assert row["condition_id"] == "0xcid"
    assert row["order_id"] == "o-1"
    assert row["status"] == "LIVE"

    table, row = _parse_user_event(
        [{"type": "trade", "market": "0xcid", "asset_id": "yes", "id": "t-1"}]
    )
    assert table == "user_fills"
    assert row["order_id"] == "t-1"

    table, row = _parse_user_event({"type": "mystery", "foo": 1})
    assert table == "user_events"
    assert json.loads(row["raw_json"])["foo"] == 1
    assert _parse_user_event("PONG") is None


def test_chain_snapshot_and_receipt_and_latency(tmp_path) -> None:
    class FakeCtf:
        chain_client = SimpleNamespace(
            get_transaction_receipt=lambda tx: {
                "status": "0x1",
                "gasUsed": "0x5208",
                "effectiveGasPrice": "0x3b9aca00",
                "blockNumber": "0x10",
            }
        )

        def get_pol_balance(self, address):
            return 1.5

        def get_usdc_balance(self, address):
            return 100.0

        def get_position_balance(self, token, address):
            return 5.0 if token == "yes" else 7.0

    class ScoringClob(FakeClob):
        def get_order_scoring(self, order_id):
            return {"order_id": order_id, "scoring": True}

    recorder = LiveSessionRecorder(
        db_path=tmp_path / "missing.db",
        clob=ScoringClob(),
        market={"condition_id": "0xcid", "token_yes": "yes", "token_no": "no"},
        dry_run=False,
        out_root=tmp_path / "live",
        session_id="chain-session",
        ctf_client=FakeCtf(),
    )
    recorder._pending_tx.add("0xdead")
    recorder._poll_chain()
    recorder._last_orders = [{"id": "o-1", "asset_id": "yes", "side": "BUY", "price": 0.5}]
    recorder._poll_order_scoring()
    recorder.record_event("quote_placed", {"quote_id": "q1", "place_rtt_ms": 12.5})
    recorder._drain_and_write()

    root = tmp_path / "live" / "chain-session"
    chain = _rows(root / "chain_account")[0]
    assert chain["pol_balance"] == 1.5
    assert chain["usdc_balance"] == 100.0
    assert chain["ctf_yes"] == 5.0 and chain["ctf_no"] == 7.0

    tx = _rows(root / "chain_tx")[0]
    assert tx["status"] == 1
    assert tx["gas_used"] == 21000
    assert tx["effective_gas_price"] == 1_000_000_000

    scoring = _rows(root / "order_scoring")[0]
    assert scoring["order_id"] == "o-1" and scoring["scoring"] is True

    latency = _rows(root / "latency")
    assert any(row["kind"] == "get_order_scoring" for row in latency)
    assert any(row["kind"] == "place_rtt" and row["ms"] == 12.5 for row in latency)


def test_user_ws_subscribes_with_auth_and_records_events(tmp_path) -> None:
    import socket
    import threading
    from websockets.sync.server import serve

    subscription: dict = {}

    def handler(ws) -> None:
        subscription.update(json.loads(ws.recv()))
        ws.send(
            json.dumps(
                {
                    "type": "trade",
                    "market": "0xcid",
                    "asset_id": "yes",
                    "id": "t-1",
                    "price": "0.5",
                    "size": "10",
                }
            )
        )
        try:
            while True:
                ws.recv()
        except Exception:  # noqa: BLE001 - client closed
            return

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with serve(handler, "127.0.0.1", port) as server:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        recorder = LiveSessionRecorder(
            db_path=tmp_path / "missing.db",
            clob=FakeClob(),
            market={"condition_id": "0xcid", "token_yes": "yes", "token_no": "no"},
            dry_run=False,
            out_root=tmp_path / "live",
            session_id="ws-session",
            user_ws=True,
            user_ws_url=f"ws://127.0.0.1:{port}",
            engine_interval=10.0,
            venue_interval=10.0,
            account_interval=10.0,
            positions_interval=10.0,
            rewards_interval=10.0,
            chain_interval=3600.0,
            scoring_interval=3600.0,
            health_interval=10.0,
            flush_interval=0.05,
            use_data_api=False,
        )
        recorder.start()
        deadline = time.time() + 5.0
        root = tmp_path / "live" / "ws-session"
        while time.time() < deadline:
            if (root / "user_fills").is_dir() and _rows(root / "user_fills"):
                break
            time.sleep(0.05)
        recorder.stop("test")

    assert subscription["type"] == "user"
    assert subscription["markets"] == ["0xcid"]
    assert subscription["auth"] == {
        "apiKey": "abcdef123456",
        "secret": "secret",
        "passphrase": "pass",
    }
    fills = _rows(root / "user_fills")
    assert any(row["source"] == "user_ws" and row["order_id"] == "t-1" for row in fills)

