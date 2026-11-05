"""Live-trading session recorder.

Runs inside the real-money runner as two daemon threads: a poller that reads
the engine's SQLite ledger and the private CLOB/Data API endpoints, and a
writer that flushes Parquet parts. The quote loop never waits on it.

Why it exists: the public collector records anonymous market data only. The
fill model, queue assumptions and reward accounting cannot be validated
against our own orders without the private session: fills, cancels, quote
intent, risk blocks, account snapshots and actual reward accrual.

Output layout::

    data/live_tape/<session_id>/session.json
    data/live_tape/<session_id>/<table>/part-000001.parquet

Every row carries ``session_id`` and ``recv_ts_ns``. ``source`` says whether the
row came from the engine's SQLite ledger, the venue REST API, the Data API or
an in-process event hook.

The recorder is best-effort: it never raises into the trading loop. Engine and
venue rows are queued; if the bounded queue is full the poller blocks (and the
watermark does not advance), while snapshot rows may be dropped and counted in
``session_health``.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from polymarketbot.data.universe_metadata import load_rows
from polymarketbot.logs import get_logger

log = get_logger("trading.session_recorder")

# Append-only tables we tail by SQLite rowid. Missing tables are skipped.
ENGINE_TABLES = (
    "fills",
    "rebalances",
    "reward_payouts",
    "portfolio_snapshots",
    "positions",
    "active_quotes",
    "deskew_events",
    "reward_epochs",
    "ctf_merges",
    "ctf_splits",
    "market_migrations",
    "pending_settlements",
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _now_ns() -> int:
    return time.time_ns()


def _iso_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def _short_hash(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:8] or None


def _hex_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def _parse_user_event(payload: Any) -> Optional[tuple[str, dict[str, Any]]]:
    """Map one CLOB user-channel message to ``(table, row)``.

    The user channel emits order and trade JSON objects (sometimes wrapped in a
    list). Unknown event types are kept verbatim in ``user_events`` so a
    protocol addition is recorded rather than dropped.
    """
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return None
    event_type = str(
        payload.get("type") or payload.get("event_type") or payload.get("eventType") or "unknown"
    )
    lowered = event_type.lower()
    if "order" in lowered:
        table = "user_orders"
    elif "trade" in lowered or "fill" in lowered:
        table = "user_fills"
    else:
        table = "user_events"
    row: dict[str, Any] = {
        "source": "user_ws",
        "event_type": event_type,
        "condition_id": payload.get("market")
        or payload.get("condition_id")
        or payload.get("conditionId"),
        "asset_id": payload.get("asset_id") or payload.get("assetId"),
        "order_id": payload.get("order_id") or payload.get("orderId") or payload.get("id"),
        "status": payload.get("status") or payload.get("event_type"),
        "raw_json": json.dumps(payload, default=str),
    }
    return table, row


class LiveSessionRecorder:
    """Record the private side of one real-money (or dry-run) session."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        clob: Any,
        market: dict[str, Any],
        dry_run: bool,
        out_root: str | Path = "data/live_tape",
        engine_params: Optional[dict[str, Any]] = None,
        public_tape_run: Optional[str] = None,
        session_id: Optional[str] = None,
        wallet: Optional[str] = None,
        ctf_client: Any = None,
        feed_age_fn: Optional[Callable[[], Optional[float]]] = None,
        user_ws: bool = True,
        user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        engine_interval: float = 1.0,
        venue_interval: float = 5.0,
        account_interval: float = 15.0,
        positions_interval: float = 15.0,
        rewards_interval: float = 60.0,
        chain_interval: float = 30.0,
        scoring_interval: float = 15.0,
        health_interval: float = 10.0,
        flush_interval: float = 1.0,
        max_queue: int = 10_000,
        use_data_api: bool = True,
    ) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.session_id = session_id or f"{stamp}-{uuid.uuid4().hex[:6]}"
        self.out_dir = Path(out_root) / self.session_id
        self.db_path = Path(db_path)
        self.clob = clob
        self.market = dict(market)
        self.dry_run = bool(dry_run)
        self.wallet = wallet or getattr(clob, "maker_address", "") or ""
        self.public_tape_run = public_tape_run or self._latest_public_run()
        self.engine_params = dict(engine_params or {})
        self.ctf_client = ctf_client
        self.feed_age_fn = feed_age_fn
        self._user_ws_enabled = bool(user_ws) and not self.dry_run
        self._user_ws_url = user_ws_url
        self._user_ws_status = "idle" if self._user_ws_enabled else "disabled"
        self._user_ws_events = 0
        self._user_ws_thread: Optional[threading.Thread] = None

        self.engine_interval = max(0.05, float(engine_interval))
        self.venue_interval = max(0.1, float(venue_interval))
        self.account_interval = max(0.1, float(account_interval))
        self.positions_interval = max(0.1, float(positions_interval))
        self.rewards_interval = max(0.1, float(rewards_interval))
        self.chain_interval = max(1.0, float(chain_interval))
        self.scoring_interval = max(1.0, float(scoring_interval))
        self.health_interval = max(0.1, float(health_interval))
        self.flush_interval = max(0.05, float(flush_interval))
        self.use_data_api = use_data_api

        self._queue: queue.Queue[tuple[str, dict[str, Any], bool]] = queue.Queue(
            maxsize=max(100, int(max_queue))
        )
        self._stop = threading.Event()
        self._poller: Optional[threading.Thread] = None
        self._writer: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._watermarks: dict[str, int] = {}
        self._seen_orders: set[tuple[Any, ...]] = set()
        self._seen_trades: set[tuple[Any, ...]] = set()
        self._dropped = 0
        self._written: dict[str, int] = {}
        self._errors = 0
        self._last_poll: dict[str, float] = {}
        self._seq = 0
        self._started_ns: Optional[int] = None
        self._stopped_ns: Optional[int] = None
        self._connections_open = False
        self._engine_conn: Optional[sqlite3.Connection] = None
        self._engine_tables: list[str] = []
        self._data_api: Any = None
        self._pending_tx: set[str] = set()
        self._user_ws_last_message_ts: Optional[float] = None
        self._last_orders: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ setup
    def _latest_public_run(self) -> Optional[str]:
        runs = sorted((REPO_ROOT / "data" / "tape" / "runs").glob("*.json"))
        if not runs:
            return None
        try:
            payload = json.loads(runs[-1].read_text(encoding="utf-8"))
            return str(payload.get("run_id") or runs[-1].stem)
        except Exception:  # noqa: BLE001
            return runs[-1].stem

    def _git_commit(self) -> Optional[str]:
        try:
            return subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip() or None
        except Exception:  # noqa: BLE001
            return None

    def _open_engine_db(self) -> None:
        if not self.db_path.exists():
            log.warning(f"engine DB {self.db_path} missing; engine ledger rows will be skipped")
            return
        try:
            self._engine_conn = sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro",
                uri=True,
                timeout=2.0,
                check_same_thread=False,
            )
            self._engine_conn.execute("PRAGMA busy_timeout = 2000")
            rows = self._engine_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            present = {str(r[0]) for r in rows}
            self._engine_tables = [t for t in ENGINE_TABLES if t in present]
            # Only rows written *during this session* belong to the session. The
            # engine DB is reused across runs (and carries the previous market's
            # ledger), so tailing from rowid 0 would mix a prior condition's
            # fills/quotes into this session's tape. Start at the current max.
            for table in self._engine_tables:
                try:
                    (max_id,) = self._engine_conn.execute(
                        f"SELECT COALESCE(MAX(rowid), 0) FROM {table}"
                    ).fetchone()
                    self._watermarks[table] = int(max_id or 0)
                except Exception as exc:  # noqa: BLE001
                    self._errors += 1
                    log.debug(f"engine table {table} watermark init failed: {exc}")
            log.info(f"session recorder engine tables: {', '.join(self._engine_tables) or 'none'}")
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            log.warning(f"engine DB read-only open failed: {exc}")
            self._engine_conn = None

    def _session_manifest(self) -> dict[str, Any]:
        meta = {}
        try:
            rows = load_rows()
            meta = rows.get(str(self.market.get("condition_id", "")).lower(), {})
        except Exception:  # noqa: BLE001
            meta = {}
        return {
            "session_id": self.session_id,
            "started_at": _iso_ns(self._started_ns or _now_ns()),
            "host": None,  # filled from the environment below
            "pid": None,
            "git_commit": self._git_commit(),
            "dry_run": self.dry_run,
            "wallet": self.wallet,
            "api_key_id": _short_hash(getattr(getattr(self.clob, "credentials", None), "api_key", "")),
            "condition_id": self.market.get("condition_id"),
            "question": self.market.get("question"),
            "event_id": self.market.get("event_id") or meta.get("event_id"),
            "event_title": meta.get("event_title"),
            "tokens": [self.market.get("token_yes"), self.market.get("token_no")],
            "fee_type": meta.get("fee_type"),
            "tick_size": self.market.get("tick_size"),
            "neg_risk": self.market.get("neg_risk"),
            "rewards_min_size": self.market.get("min_size"),
            "rewards_max_spread": self.market.get("max_spread"),
            "daily_reward_rate": self.market.get("daily_reward"),
            "engine": self.engine_params,
            "public_tape_run": self.public_tape_run,
            "user_ws": self._user_ws_status,
            "recorder_version": 2,
        }

    # ---------------------------------------------------------------- public API
    def start(self) -> None:
        """Create the session, take one initial snapshot, and start the threads."""
        if self._started_ns is not None:
            return
        self._started_ns = _now_ns()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        try:
            import socket

            manifest = self._session_manifest()
            manifest["host"] = socket.gethostname()
            manifest["pid"] = __import__("os").getpid()
            (self.out_dir / "session.json").write_text(
                json.dumps(manifest, indent=1, default=str), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            log.warning(f"session manifest write failed: {exc}")

        self._open_engine_db()
        if self.use_data_api:
            try:
                from polymarketbot.dataapi import DataAPIClient

                self._data_api = DataAPIClient()
            except Exception as exc:  # noqa: BLE001
                self._data_api = None
                log.warning(f"Data API client unavailable: {exc}")

        # Initial state before the first quote. Failures are logged, not fatal.
        try:
            self._poll_engine()
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            log.warning(f"initial engine snapshot failed: {exc}")
        try:
            self._poll_venue()
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            log.warning(f"initial venue snapshot failed: {exc}")

        self._connections_open = True
        self._poller = threading.Thread(
            target=self._poll_loop, name="live-recorder-poller", daemon=True
        )
        self._writer = threading.Thread(
            target=self._write_loop, name="live-recorder-writer", daemon=True
        )
        self._writer.start()
        self._poller.start()
        if self._user_ws_enabled:
            self._user_ws_thread = threading.Thread(
                target=self._user_ws_loop, name="live-recorder-user-ws", daemon=True
            )
            self._user_ws_thread.start()
        log.info(f"live session recorder started: {self.out_dir}")

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        """In-process hook: quote intent, risk blocks, cancels. Never blocks."""
        table = "risk_events" if kind in {"risk_blocked", "circuit_breaker"} else "quotes"
        self._emit(table, {"kind": kind, **payload}, critical=False)
        for field, label in (("place_rtt_ms", "place"), ("cancel_rtt_ms", "cancel")):
            value = payload.get(field)
            if value is not None:
                self._emit(
                    "latency",
                    {"source": "engine", "kind": f"{label}_rtt", "ms": float(value)},
                    critical=False,
                )

    def stop(self, reason: str = "stopped") -> None:
        """Stop polling, drain the queue, and finalize the session manifest."""
        if self._started_ns is None or self._stop.is_set():
            return
        self._stop.set()
        for thread in (self._poller, self._writer, self._user_ws_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=10.0)
        try:
            self._poll_engine()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._poll_venue()
        except Exception:  # noqa: BLE001
            pass
        self._drain_and_write()
        self._stopped_ns = _now_ns()
        try:
            path = self.out_dir / "session.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["user_ws"] = self._user_ws_status
            payload["end"] = {
                "stopped_at": _iso_ns(self._stopped_ns),
                "reason": reason,
                "rows_written": dict(self._written),
                "dropped": self._dropped,
                "errors": self._errors,
                "user_ws_events": self._user_ws_events,
            }
            path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"session manifest finalize failed: {exc}")
        try:
            if self._engine_conn is not None:
                self._engine_conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._engine_conn = None
        self._connections_open = False
        log.info(
            f"live session recorder stopped ({reason}): "
            f"{sum(self._written.values())} rows, {self._dropped} dropped, {self._errors} errors"
        )

    # ------------------------------------------------------------------ polling
    def _emit(self, table: str, row: dict[str, Any], *, critical: bool) -> None:
        row.setdefault("session_id", self.session_id)
        row.setdefault("recv_ts_ns", _now_ns())
        try:
            if critical:
                self._queue.put((table, row, True), timeout=2.0)
            else:
                self._queue.put_nowait((table, row, False))
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def _poll_loop(self) -> None:
        last_engine = last_venue = last_account = last_positions = last_rewards = 0.0
        last_chain = last_scoring = last_health = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now - last_engine >= self.engine_interval:
                    self._poll_engine()
                    last_engine = now
                if now - last_venue >= self.venue_interval:
                    self._poll_venue()
                    last_venue = now
                if now - last_account >= self.account_interval:
                    self._poll_account()
                    last_account = now
                if now - last_positions >= self.positions_interval:
                    self._poll_positions()
                    last_positions = now
                if now - last_rewards >= self.rewards_interval:
                    self._poll_rewards()
                    last_rewards = now
                if now - last_chain >= self.chain_interval:
                    self._poll_chain()
                    last_chain = now
                if now - last_scoring >= self.scoring_interval:
                    self._poll_order_scoring()
                    last_scoring = now
                if now - last_health >= self.health_interval:
                    self._emit_health()
                    last_health = now
            except Exception as exc:  # noqa: BLE001 - the recorder must survive
                self._errors += 1
                log.warning(f"session recorder poll error: {exc}")
            self._stop.wait(min(self.engine_interval, 0.5))

    def _poll_engine(self) -> None:
        conn = self._engine_conn
        if conn is None:
            return
        for table in self._engine_tables:
            last = self._watermarks.get(table, 0)
            try:
                cursor = conn.execute(
                    f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT 5000",
                    (last,),
                )
                columns = [d[0] for d in cursor.description]
                fetched = cursor.fetchall()
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                log.debug(f"engine table {table} read failed: {exc}")
                continue
            for values in fetched:
                row = dict(zip(columns, values))
                self._watermarks[table] = int(row.pop("_rowid"))
                row["source"] = "engine"
                if table == "pending_settlements":
                    tx_hash = row.get("tx_hash")
                    if tx_hash and str(row.get("status") or "").upper() not in {
                        "SUCCESS",
                        "REVERTED",
                        "FAILED",
                    }:
                        self._pending_tx.add(str(tx_hash))
                self._emit(table, row, critical=True)
        self._last_poll["engine"] = time.time()

    def _timed(self, kind: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call a REST endpoint and record its wall-clock latency."""
        started = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            self._emit(
                "latency",
                {
                    "source": "rest",
                    "kind": kind,
                    "ms": round((time.perf_counter() - started) * 1000.0, 3),
                },
                critical=False,
            )

    def _poll_venue(self) -> None:
        condition = str(self.market.get("condition_id") or "")
        if not condition:
            return
        try:
            orders = self._timed(
                "get_open_orders", self.clob.get_open_orders, market=condition
            )
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            log.debug(f"open orders poll failed: {exc}")
            orders = []
        self._last_orders = [dict(o) for o in (orders or []) if isinstance(o, dict)]
        for order in orders or []:
            key = (
                order.get("id") or order.get("orderID") or order.get("order_id"),
                order.get("status"),
                order.get("original_size") or order.get("size"),
                order.get("price"),
            )
            if key in self._seen_orders:
                continue
            self._seen_orders.add(key)
            row = {str(k): v for k, v in dict(order).items()}
            row["source"] = "venue"
            row["raw_json"] = json.dumps(order, default=str)
            self._emit("orders", row, critical=False)
        try:
            trades = self._timed("get_trades", self.clob.get_trades, market=condition)
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            log.debug(f"user trades poll failed: {exc}")
            trades = []
        for trade in trades or []:
            key = (
                trade.get("id") or trade.get("trade_id") or trade.get("transactionHash"),
                trade.get("asset_id") or trade.get("assetId"),
                trade.get("price"),
                trade.get("size"),
            )
            if key in self._seen_trades:
                continue
            self._seen_trades.add(key)
            row = {str(k): v for k, v in dict(trade).items()}
            row["source"] = "venue"
            row["raw_json"] = json.dumps(trade, default=str)
            self._emit("fills", row, critical=False)
        self._last_poll["venue"] = time.time()

    def _poll_account(self) -> None:
        row: dict[str, Any] = {"source": "venue"}
        try:
            balance = self._timed("get_balance", self.clob.get_balance)
            row["usdc_balance"] = balance.get("balance") if isinstance(balance, dict) else balance
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            row["balance_error"] = str(exc)
        try:
            allowances = self._timed("get_allowances", self.clob.get_allowances)
            row["allowances"] = json.dumps(allowances, default=str)
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            row["allowance_error"] = str(exc)
        row["open_orders"] = len(self._seen_orders)
        if len(row) > 1:
            self._emit("account", row, critical=False)
        self._last_poll["account"] = time.time()

    def _poll_positions(self) -> None:
        if self._data_api is None or not self.wallet:
            return
        try:
            rows = self._timed(
                "data_api_positions", self._data_api.positions, self.wallet, max_pages=5
            )
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            log.debug(f"Data API positions poll failed: {exc}")
            return
        for position in rows or []:
            row = {str(k): v for k, v in dict(position).items()}
            row["source"] = "data-api"
            self._emit("positions", row, critical=False)
        self._last_poll["positions"] = time.time()

    def _poll_rewards(self) -> None:
        for name, fn in (
            ("total_rewards", getattr(self.clob, "get_total_rewards", None)),
            ("daily_rewards", getattr(self.clob, "get_daily_rewards", None)),
        ):
            if not callable(fn):
                continue
            try:
                payload = self._timed(name, fn)
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                log.debug(f"{name} poll failed: {exc}")
                continue
            row = {"source": "venue", "kind": name, "payload": json.dumps(payload, default=str)}
            self._emit("rewards", row, critical=False)
        self._last_poll["rewards"] = time.time()

    def _poll_chain(self) -> None:
        """On-chain balances and settlement receipts (one RPC batch per interval)."""
        if self.ctf_client is None or not self.wallet:
            return
        row: dict[str, Any] = {"source": "chain", "wallet": self.wallet}
        for field, fn, arg in (
            ("pol_balance", getattr(self.ctf_client, "get_pol_balance", None), self.wallet),
            ("usdc_balance", getattr(self.ctf_client, "get_usdc_balance", None), self.wallet),
        ):
            if not callable(fn):
                continue
            try:
                row[field] = float(fn(arg))
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                row[f"{field}_error"] = str(exc)
        for field, token in (
            ("ctf_yes", self.market.get("token_yes")),
            ("ctf_no", self.market.get("token_no")),
        ):
            fn = getattr(self.ctf_client, "get_position_balance", None)
            if not callable(fn) or not token:
                continue
            token_arg: Any = int(str(token), 10) if str(token).isdigit() else str(token)
            try:
                row[field] = float(fn(token_arg, self.wallet))
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                row[f"{field}_error"] = str(exc)
        if len(row) > 2:
            self._emit("chain_account", row, critical=False)

        chain = getattr(self.ctf_client, "chain_client", None)
        if chain is not None:
            for tx_hash in list(self._pending_tx)[:20]:
                try:
                    receipt = chain.get_transaction_receipt(tx_hash)
                except Exception as exc:  # noqa: BLE001
                    self._errors += 1
                    log.debug(f"receipt lookup {tx_hash} failed: {exc}")
                    continue
                if not isinstance(receipt, dict) or not receipt:
                    continue
                self._pending_tx.discard(tx_hash)
                self._emit(
                    "chain_tx",
                    {
                        "source": "chain",
                        "tx_hash": tx_hash,
                        "block": _hex_int(receipt.get("blockNumber")),
                        "status": _hex_int(receipt.get("status")),
                        "gas_used": _hex_int(receipt.get("gasUsed")),
                        "effective_gas_price": _hex_int(receipt.get("effectiveGasPrice")),
                        "raw_json": json.dumps(receipt, default=str),
                    },
                    critical=False,
                )
        self._last_poll["chain"] = time.time()

    def _poll_order_scoring(self) -> None:
        """Poll the venue's reward-scoring verdict for each resting order."""
        if self.dry_run or not self._last_orders:
            return
        scoring_fn = getattr(self.clob, "get_order_scoring", None)
        if not callable(scoring_fn):
            return
        for order in list(self._last_orders)[:20]:
            order_id = order.get("id") or order.get("orderID") or order.get("order_id")
            if not order_id:
                continue
            try:
                scoring = self._timed("get_order_scoring", scoring_fn, str(order_id))
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                log.debug(f"order scoring poll failed for {order_id}: {exc}")
                continue
            self._emit(
                "order_scoring",
                {
                    "source": "venue",
                    "order_id": str(order_id),
                    "asset_id": order.get("asset_id") or order.get("assetId"),
                    "side": order.get("side"),
                    "price": order.get("price"),
                    "scoring": scoring.get("scoring") if isinstance(scoring, dict) else None,
                    "raw_json": json.dumps(scoring, default=str),
                },
                critical=False,
            )
        self._last_poll["scoring"] = time.time()

    def _user_ws_loop(self) -> None:
        """Authenticated CLOB user channel: order and trade events in real time."""
        creds = getattr(self.clob, "credentials", None)
        api_key = str(getattr(creds, "api_key", "") or "")
        secret = str(getattr(creds, "api_secret", "") or "")
        passphrase = str(getattr(creds, "passphrase", "") or "")
        if not (api_key and secret and passphrase):
            self._user_ws_status = "disabled(no-credentials)"
            log.info("user websocket not started: CLOB API credentials absent")
            return
        condition = str(self.market.get("condition_id") or "")
        if not condition:
            self._user_ws_status = "disabled(no-condition)"
            return
        url = self._user_ws_url
        try:
            from websockets.sync.client import connect
        except Exception as exc:  # noqa: BLE001
            self._user_ws_status = "disabled(no-websockets)"
            log.warning(f"user websocket unavailable: {exc}")
            return
        backoff = 2.0
        while not self._stop.is_set():
            try:
                with connect(
                    url,
                    open_timeout=10.0,
                    close_timeout=2.0,
                    ping_interval=None,
                    max_size=2**22,
                ) as ws:
                    self._user_ws_status = "connected"
                    ws.send(
                        json.dumps(
                            {
                                "type": "user",
                                "auth": {
                                    "apiKey": api_key,
                                    "secret": secret,
                                    "passphrase": passphrase,
                                },
                                "markets": [condition],
                            }
                        )
                    )
                    last_ping = time.monotonic()
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            raw = None
                        if raw:
                            self._handle_user_ws_message(raw)
                        if time.monotonic() - last_ping >= 10.0:
                            ws.send("PING")
                            last_ping = time.monotonic()
                self._user_ws_status = "reconnecting"
                backoff = 2.0
            except Exception as exc:  # noqa: BLE001 - reconnect, never crash the session
                if self._stop.is_set():
                    break
                self._user_ws_status = "error"
                self._errors += 1
                log.warning(f"user websocket disconnected: {exc}; retrying in {backoff:.0f}s")
                self._stop.wait(backoff)
                backoff = min(30.0, backoff * 2.0)
        self._user_ws_status = "stopped"

    def _handle_user_ws_message(self, raw: Any) -> None:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if text.strip() in {"PONG", "PING"}:
            return
        try:
            payload = json.loads(text)
        except Exception:  # noqa: BLE001
            self._errors += 1
            return
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            parsed = _parse_user_event(event)
            if parsed is None:
                continue
            table, row = parsed
            self._emit(table, row, critical=True)
            self._user_ws_events += 1
        self._user_ws_last_message_ts = time.time()

    def _emit_health(self) -> None:
        feed_age = None
        if callable(self.feed_age_fn):
            try:
                feed_age = self.feed_age_fn()
            except Exception:  # noqa: BLE001
                feed_age = None
        self._emit(
            "session_health",
            {
                "source": "recorder",
                "queue_depth": self._queue.qsize(),
                "dropped": self._dropped,
                "errors": self._errors,
                "user_ws": self._user_ws_status,
                "user_ws_events": self._user_ws_events,
                "user_ws_age_s": (
                    time.time() - self._user_ws_last_message_ts
                    if self._user_ws_last_message_ts
                    else None
                ),
                "feed_age_s": feed_age,
                "pending_tx": len(self._pending_tx),
                "last_poll_engine_age_s": (
                    time.time() - self._last_poll["engine"] if "engine" in self._last_poll else None
                ),
                "last_poll_venue_age_s": (
                    time.time() - self._last_poll["venue"] if "venue" in self._last_poll else None
                ),
                "last_poll_chain_age_s": (
                    time.time() - self._last_poll["chain"] if "chain" in self._last_poll else None
                ),
            },
            critical=False,
        )

    # ------------------------------------------------------------------- writer
    def _write_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_and_write()
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                log.warning(f"session recorder write error: {exc}")
            time.sleep(self.flush_interval)
        self._drain_and_write()

    def _drain_and_write(self) -> None:
        batches: dict[str, list[dict[str, Any]]] = {}
        while True:
            try:
                table, row, _critical = self._queue.get_nowait()
            except queue.Empty:
                break
            batches.setdefault(table, []).append(row)
        for table, rows in batches.items():
            if not rows:
                continue
            target = self.out_dir / table
            target.mkdir(parents=True, exist_ok=True)
            # Engine rows and venue rows share a table but not a schema. Group
            # by key set so PyArrow does not silently drop the extra columns.
            groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for row in rows:
                groups.setdefault(tuple(row.keys()), []).append(row)
            for group in groups.values():
                self._seq += 1
                part = target / f"part-{self._seq:06d}.parquet"
                table_obj = pa.Table.from_pylist(group)
                pq.write_table(table_obj, part, compression="zstd")
                with self._lock:
                    self._written[table] = self._written.get(table, 0) + len(group)


def _latest_public_tape_run() -> Optional[str]:
    runs = sorted((REPO_ROOT / "data" / "tape" / "runs").glob("*.json"))
    if not runs:
        return None
    try:
        payload = json.loads(runs[-1].read_text(encoding="utf-8"))
        return str(payload.get("run_id") or runs[-1].stem)
    except Exception:  # noqa: BLE001
        return runs[-1].stem


class PaperSessionRecorder:
    """Write a paper-trading engine session under the live recorder's columns.

    Paper sessions are offline/simulated, so this is a synchronous snapshot
    writer (no threads): the engine's positions, fills, virtual quotes, epoch
    snapshots and portfolio summary are written at the start and end of a paper
    run. The manifest marks ``mode="paper"`` and ``dry_run=true`` so a paper
    session can never be mistaken for a real one, while sharing the table
    schema used for paper-vs-live comparison.
    """

    def __init__(
        self,
        engine: Any,
        *,
        out_root: str | Path = "data/live_tape",
        session_id: Optional[str] = None,
        engine_params: Optional[dict[str, Any]] = None,
    ) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.session_id = session_id or f"paper-{stamp}-{uuid.uuid4().hex[:6]}"
        self.out_dir = Path(out_root) / self.session_id
        self.engine = engine
        self.engine_params = dict(engine_params or {})
        self._started_ns = _now_ns()
        self._stopped_ns: Optional[int] = None
        self._seq = 0
        self._written: dict[str, int] = {}

    def _manifest(self) -> dict[str, Any]:
        markets = []
        for condition_id, config in getattr(self.engine, "markets", {}).items():
            markets.append(
                {
                    "condition_id": condition_id,
                    "question": getattr(config, "question", None),
                }
            )
        return {
            "session_id": self.session_id,
            "started_at": _iso_ns(self._started_ns),
            "mode": "paper",
            "dry_run": True,
            "wallet": "",
            "markets": markets,
            "engine": self.engine_params,
            "public_tape_run": _latest_public_tape_run(),
            "recorder_version": 2,
        }

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        try:
            (self.out_dir / "session.json").write_text(
                json.dumps(self._manifest(), indent=1, default=str), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"paper session manifest write failed: {exc}")
        self._snapshot()
        log.info(f"paper session recorder started: {self.out_dir}")

    def _snapshot(self) -> None:
        tables: dict[str, list[dict[str, Any]]] = {
            "positions": [
                {**p.to_dict(), "source": "paper"}
                for p in getattr(self.engine, "positions", {}).values()
                if hasattr(p, "to_dict")
            ],
            "fills": [
                {**f.to_dict(), "source": "paper"}
                for f in getattr(self.engine, "fills", [])
                if hasattr(f, "to_dict")
            ],
            "quotes": [],
            "reward_epochs": [
                {**e.to_dict(), "source": "paper"}
                for e in getattr(self.engine, "epoch_snapshots", [])
                if hasattr(e, "to_dict")
            ],
        }
        for asset_id, sides in getattr(self.engine, "virtual_quotes", {}).items():
            for quote_side, quote in (sides or {}).items():
                if quote is None or not hasattr(quote, "to_dict"):
                    continue
                tables["quotes"].append(
                    {
                        **quote.to_dict(),
                        "asset_id": asset_id,
                        "quote_side": quote_side,
                        "kind": "virtual_quote",
                        "source": "paper",
                    }
                )
        try:
            summary = self.engine.get_portfolio_summary()
        except Exception as exc:  # noqa: BLE001
            summary = {"error": str(exc)}
        tables["portfolio_snapshots"] = [
            {
                "source": "paper",
                "timestamp": time.time(),
                "summary_json": json.dumps(summary, default=str),
            }
        ]
        health = {"source": "paper", "mode": "paper"}
        health.update({f"rows_{name}": len(rows) for name, rows in tables.items()})
        tables["session_health"] = [health]
        for name, rows in tables.items():
            if not rows:
                continue
            self._seq += 1
            target = self.out_dir / name
            target.mkdir(parents=True, exist_ok=True)
            stamped = [
                {
                    **row,
                    "session_id": self.session_id,
                    "recv_ts_ns": _now_ns(),
                }
                for row in rows
            ]
            pq.write_table(
                pa.Table.from_pylist(stamped),
                target / f"part-{self._seq:06d}.parquet",
                compression="zstd",
            )
            self._written[name] = self._written.get(name, 0) + len(stamped)

    def stop(self, reason: str = "stopped") -> None:
        if self._stopped_ns is not None:
            return
        self._snapshot()
        self._stopped_ns = _now_ns()
        try:
            path = self.out_dir / "session.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["end"] = {
                "stopped_at": _iso_ns(self._stopped_ns),
                "reason": reason,
                "rows_written": dict(self._written),
            }
            path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"paper session manifest finalize failed: {exc}")
        log.info(f"paper session recorder stopped ({reason}): {sum(self._written.values())} rows")
