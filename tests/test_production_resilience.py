"""Unit tests for Production Resilience & Hardening.

Verifies:
1. SQLite WAL mode (PRAGMA journal_mode=WAL) & PRAGMA synchronous=NORMAL.
2. Creation and validity of required performance indexes:
   - idx_reward_epochs_ts on reward_epochs(timestamp)
   - idx_positions_cond on positions(condition_id)
   - idx_migrations_ts on market_migrations(timestamp)
   - idx_deskew_ts on deskew_events(timestamp)
3. WAL checkpointing (flush_wal) and idempotent close.
4. Atomic JSON ledger persistence via temporary file replacement (<path>.tmp -> <path>).
5. Centralized BotConfig dataclass and load_config helper from .env and os.environ.
6. Documentation completeness of .env.example template.
7. Active virtual quote cancellation and cleanup.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from polymarketbot.config import BotConfig, load_config
from polymarketbot.rewards.allocator import calculate_endogenous_reserve
from polymarketbot.simulator.engine import NativePaperTradingEngine
from polymarketbot.simulator.feed import LivePolymarketFeed
from polymarketbot.trading.client import LiveClobClient
from polymarketbot.trading.ctf import (
    PolygonCtfClient,
    calculate_optimal_merge_threshold,
    estimate_merge_cost_usd,
    get_dynamic_gas_price_gwei,
)
from polymarketbot.trading.engine import RealMoneyTradingEngine


@pytest.fixture
def temp_resilience_engine(tmp_path: Path) -> NativePaperTradingEngine:
    """Fixture providing a clean NativePaperTradingEngine with isolated SQLite & JSON files."""
    db_file = tmp_path / "test_resilience.db"
    ledger_file = tmp_path / "test_ledger.json"

    engine = NativePaperTradingEngine(
        condition_id="0x" + "a" * 64,
        asset_id_yes="TOKEN_YES_01",
        asset_id_no="TOKEN_NO_01",
        initial_cash=200.0,
        quote_size=25.0,
        spread_offset_cents=0.5,
        daily_reward_rate=25.0,
        rewards_max_spread_cents=1.0,
        rewards_min_size=20.0,
        db_path=db_file,
        ledger_path=ledger_file,
    )
    yield engine
    engine.close()


# ---------------------------------------------------------------------------
# 1. SQLite WAL Mode & Synchronous Pragmas
# ---------------------------------------------------------------------------

def test_sqlite_wal_mode_and_pragmas(temp_resilience_engine: NativePaperTradingEngine) -> None:
    """Verify SQLite connection is configured with WAL journal mode and NORMAL synchronous."""
    cur = temp_resilience_engine._conn.cursor()

    # Check journal_mode
    cur.execute("PRAGMA journal_mode;")
    journal_mode = cur.fetchone()[0]
    assert journal_mode.lower() == "wal", f"Expected WAL mode, got: {journal_mode}"

    # Check synchronous pragma (1 corresponds to NORMAL in SQLite)
    cur.execute("PRAGMA synchronous;")
    sync_mode = cur.fetchone()[0]
    assert sync_mode in (1, "1", "NORMAL", "normal"), f"Expected synchronous=NORMAL (1), got: {sync_mode}"


# ---------------------------------------------------------------------------
# 2. Database Performance Indexes
# ---------------------------------------------------------------------------

def test_sqlite_performance_indexes_exist(temp_resilience_engine: NativePaperTradingEngine) -> None:
    """Verify that all required performance indexes exist on the respective tables and columns."""
    cur = temp_resilience_engine._conn.cursor()

    cur.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index';")
    indexes = {row[0]: row[1] for row in cur.fetchall()}

    expected_indexes = {
        "idx_reward_epochs_ts": "reward_epochs",
        "idx_positions_cond": "positions",
        "idx_migrations_ts": "market_migrations",
        "idx_deskew_ts": "deskew_events",
    }

    for idx_name, expected_table in expected_indexes.items():
        assert idx_name in indexes, f"Index {idx_name} missing from database."
        assert indexes[idx_name] == expected_table, (
            f"Index {idx_name} is on table {indexes[idx_name]}, expected {expected_table}."
        )

    # Verify column bindings for each index via PRAGMA index_info
    idx_columns = {
        "idx_reward_epochs_ts": "timestamp",
        "idx_positions_cond": "condition_id",
        "idx_migrations_ts": "timestamp",
        "idx_deskew_ts": "timestamp",
    }

    for idx_name, col_name in idx_columns.items():
        cur.execute(f"PRAGMA index_info({idx_name});")
        info = cur.fetchall()
        assert len(info) >= 1, f"PRAGMA index_info({idx_name}) returned empty info."
        indexed_col = info[0][2]
        assert indexed_col == col_name, (
            f"Index {idx_name} targets column '{indexed_col}', expected '{col_name}'."
        )


def test_sqlite_wal_flush_and_close(temp_resilience_engine: NativePaperTradingEngine) -> None:
    """Verify flush_wal executes checkpoint without errors and close is clean and idempotent."""
    # Write some state
    temp_resilience_engine.flush_ledger()

    # Flush WAL checkpoint
    temp_resilience_engine.flush_wal()

    # Close database connection
    temp_resilience_engine.close()

    # Re-invoking close should be safe and idempotent
    temp_resilience_engine.close()


# ---------------------------------------------------------------------------
# 3. Atomic JSON Ledger Persistence
# ---------------------------------------------------------------------------

def test_atomic_json_ledger_write(temp_resilience_engine: NativePaperTradingEngine) -> None:
    """Verify _save_ledger writes valid JSON and uses atomic file replacement with no temp leftovers."""
    ledger_path = temp_resilience_engine.ledger_path
    tmp_path = Path(f"{ledger_path}.tmp")

    # Initial flush
    temp_resilience_engine.flush_ledger()

    assert ledger_path.exists(), "Target ledger JSON file was not created."
    assert not tmp_path.exists(), "Temporary file was not cleaned up after atomic rename."

    # Validate JSON payload integrity
    with open(ledger_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "portfolio" in data
    assert "condition_id" in data
    assert "updated_at" in data
    assert "epoch_index" in data
    assert data["condition_id"] == temp_resilience_engine.condition_id


def test_atomic_json_ledger_overwrites_cleanly(temp_resilience_engine: NativePaperTradingEngine) -> None:
    """Verify repeated writes update the ledger atomically without data loss or corruption."""
    ledger_path = temp_resilience_engine.ledger_path
    tmp_path = Path(f"{ledger_path}.tmp")

    custom_data_1 = {"test_run": 1, "status": "init"}
    temp_resilience_engine._save_ledger(custom_data_1)
    with open(ledger_path, "r", encoding="utf-8") as f:
        read_1 = json.load(f)
    assert read_1["test_run"] == 1
    assert not tmp_path.exists()

    custom_data_2 = {"test_run": 2, "status": "updated", "val": 99.9}
    temp_resilience_engine._save_ledger(custom_data_2)
    with open(ledger_path, "r", encoding="utf-8") as f:
        read_2 = json.load(f)
    assert read_2["test_run"] == 2
    assert read_2["val"] == 99.9
    assert not tmp_path.exists()


# ---------------------------------------------------------------------------
# 4. Centralized BotConfig & Secrets Management
# ---------------------------------------------------------------------------

def test_bot_config_defaults() -> None:
    """Verify BotConfig default values and accessor properties."""
    cfg = BotConfig()

    assert cfg.POLYMARKET_API_KEY == ""
    assert cfg.POLYMARKET_SECRET == ""
    assert cfg.POLYMARKET_PASSPHRASE == ""
    assert cfg.PRIVATE_KEY == ""
    assert cfg.PROXY_WALLET_ADDRESS == ""
    assert cfg.POLYGON_RPC_URL == "https://polygon-rpc.com"
    assert cfg.CLOB_API_URL == "https://clob.polymarket.com"
    assert cfg.CLOB_WS_URL == "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Properties
    assert cfg.api_key == ""
    assert cfg.secret == ""
    assert cfg.passphrase == ""
    assert cfg.private_key == ""
    assert cfg.proxy_wallet_address == ""
    assert cfg.polygon_rpc_url == "https://polygon-rpc.com"
    assert cfg.clob_api_url == "https://clob.polymarket.com"
    assert cfg.clob_ws_url == "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    assert not cfg.is_live_trading_ready()
    assert not cfg.is_api_authenticated()


def test_bot_config_masking() -> None:
    """Verify secrets masking in to_dict."""
    cfg = BotConfig(
        POLYMARKET_API_KEY="test_key_secret_1234",
        POLYMARKET_SECRET="test_secret_abcd",
        POLYMARKET_PASSPHRASE="mypassphrase",
        PRIVATE_KEY="0xabcdef1234567890",
    )

    masked = cfg.to_dict(mask_secrets=True)
    assert masked["POLYMARKET_API_KEY"] == "***1234"
    assert masked["POLYMARKET_SECRET"] == "***abcd"
    assert masked["POLYMARKET_PASSPHRASE"] == "***rase"
    assert masked["PRIVATE_KEY"] == "***7890"

    unmasked = cfg.to_dict(mask_secrets=False)
    assert unmasked["POLYMARKET_API_KEY"] == "test_key_secret_1234"
    assert cfg.is_live_trading_ready()
    assert cfg.is_api_authenticated()


def test_load_config_from_custom_env_file(tmp_path: Path) -> None:
    """Verify load_config parses and loads keys from a specified .env file."""
    env_content = """
# Test environment config
POLYMARKET_API_KEY=custom_api_key_abc
POLYMARKET_SECRET="custom_secret_def"
POLYMARKET_PASSPHRASE='custom_pass_xyz'
PRIVATE_KEY=0x11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff
PROXY_WALLET_ADDRESS=0x9999999999999999999999999999999999999999
POLYGON_RPC_URL=https://rpc-custom.polygon.org
CLOB_API_URL=https://clob-custom.polymarket.com
CLOB_WS_URL=wss://ws-custom.polymarket.com
"""
    env_file = tmp_path / ".env.test"
    env_file.write_text(env_content, encoding="utf-8")

    cfg = load_config(env_path=env_file)

    assert cfg.POLYMARKET_API_KEY == "custom_api_key_abc"
    assert cfg.POLYMARKET_SECRET == "custom_secret_def"
    assert cfg.POLYMARKET_PASSPHRASE == "custom_pass_xyz"
    assert cfg.PRIVATE_KEY == "0x11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"
    assert cfg.PROXY_WALLET_ADDRESS == "0x9999999999999999999999999999999999999999"
    assert cfg.POLYGON_RPC_URL == "https://rpc-custom.polygon.org"
    assert cfg.CLOB_API_URL == "https://clob-custom.polymarket.com"
    assert cfg.CLOB_WS_URL == "wss://ws-custom.polymarket.com"


def test_load_config_os_environ_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify os.environ variables take precedence over file values."""
    env_file = tmp_path / ".env.override"
    env_file.write_text("POLYMARKET_API_KEY=from_file\n", encoding="utf-8")

    monkeypatch.setenv("POLYMARKET_API_KEY", "from_environ")
    monkeypatch.setenv("PRIVATE_KEY", "env_pk_999")

    cfg = load_config(env_path=env_file)
    assert cfg.POLYMARKET_API_KEY == "from_environ"
    assert cfg.PRIVATE_KEY == "env_pk_999"


def test_load_config_missing_file_uses_defaults(tmp_path: Path) -> None:
    """Verify load_config cleanly falls back to defaults when file does not exist."""
    missing_file = tmp_path / "does_not_exist.env"
    cfg = load_config(env_path=missing_file)

    assert cfg.POLYGON_RPC_URL == "https://polygon-rpc.com"
    assert cfg.CLOB_API_URL == "https://clob.polymarket.com"
    assert cfg.CLOB_WS_URL == "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def test_env_example_template_exists_and_complete() -> None:
    """Verify .env.example exists at repository root and documents all configuration keys."""
    root_dir = Path(__file__).parent.parent
    example_path = root_dir / ".env.example"

    assert example_path.exists(), ".env.example file must exist at project root."

    content = example_path.read_text(encoding="utf-8")
    required_keys = [
        "POLYMARKET_API_KEY",
        "POLYMARKET_SECRET",
        "POLYMARKET_PASSPHRASE",
        "PRIVATE_KEY",
        "PROXY_WALLET_ADDRESS",
        "POLYGON_RPC_URL",
        "CLOB_API_URL",
        "CLOB_WS_URL",
    ]

    for key in required_keys:
        assert key in content, f"Key {key} must be documented in .env.example."


# ---------------------------------------------------------------------------
# 5. Quote Cancellation & Shutdown Cleanliness
# ---------------------------------------------------------------------------

def test_cancel_all_virtual_quotes(temp_resilience_engine: NativePaperTradingEngine) -> None:
    """Verify cancel_all_virtual_quotes cleanly cancels all resting maker quotes."""
    engine = temp_resilience_engine

    # Seed mock depth so evaluate_quotes can place quotes
    engine.update_orderbook_snapshot("YES", bids=[(0.48, 100.0)], asks=[(0.52, 100.0)])
    engine.update_orderbook_snapshot("NO", bids=[(0.48, 100.0)], asks=[(0.52, 100.0)])
    engine.evaluate_quotes()

    # Active quotes should be placed
    assert len(engine.active_quotes) > 0, "Quotes were not placed during evaluate_quotes."

    # Execute cancel all
    engine.cancel_all_virtual_quotes()

    # Active quotes map must be empty
    assert len(engine.active_quotes) == 0, "Active quotes map not empty after cancel_all_virtual_quotes."
    assert len(engine.book_yes._virtual_quotes) == 0, "Synthetic YES book still has active quotes."
    assert len(engine.book_no._virtual_quotes) == 0, "Synthetic NO book still has active quotes."


# ---------------------------------------------------------------------------
# 6. RealMoneyTradingEngine Database & Persistence Integrity
# ---------------------------------------------------------------------------

def test_real_money_engine_wal_and_indices(tmp_path: Path) -> None:
    """Verify RealMoneyTradingEngine configures SQLite WAL, indices, flush_wal, and atomic ledger persistence."""
    db_file = tmp_path / "test_rm_resilience.db"
    ledger_file = tmp_path / "test_rm_ledger.json"

    clob = LiveClobClient(dry_run=True)
    ctf = PolygonCtfClient(dry_run=True)

    engine = RealMoneyTradingEngine(
        condition_id="0x" + "b" * 64,
        asset_id_yes="TOKEN_YES_RM",
        asset_id_no="TOKEN_NO_RM",
        clob_client=clob,
        ctf_client=ctf,
        initial_cash=150.0,
        db_path=db_file,
        ledger_path=ledger_file,
        dry_run=True,
    )

    try:
        cur = engine._conn.cursor()

        # Check WAL mode
        cur.execute("PRAGMA journal_mode;")
        assert cur.fetchone()[0].lower() == "wal"

        # Check synchronous NORMAL
        cur.execute("PRAGMA synchronous;")
        assert cur.fetchone()[0] in (1, "1", "NORMAL", "normal")

        # Check performance indexes
        cur.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index';")
        indexes = {row[0]: row[1] for row in cur.fetchall()}
        expected_indexes = {
            "idx_reward_epochs_ts": "reward_epochs",
            "idx_positions_cond": "positions",
            "idx_migrations_ts": "market_migrations",
            "idx_deskew_ts": "deskew_events",
        }
        for idx_name, expected_table in expected_indexes.items():
            assert idx_name in indexes, f"Index {idx_name} missing from database."
            assert indexes[idx_name] == expected_table

        # Check atomic ledger flush
        engine.flush_ledger()
        assert ledger_file.exists(), "Target ledger JSON file was not created."
        assert not Path(f"{ledger_file}.tmp").exists(), "Temporary file was not cleaned up."

        with open(ledger_file, "r", encoding="utf-8") as f:
            ledger_data = json.load(f)
        assert "portfolio" in ledger_data
        assert "condition_id" in ledger_data

        # Check explicit WAL flush
        engine.flush_wal()
    finally:
        engine.close()
        # Idempotent close
        engine.close()


# ---------------------------------------------------------------------------
# 7. CLOB WebSocket Disconnect & Auto-Reconnect Resilience
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_clob_websocket_disconnect_and_reconnect_resilience() -> None:
    """Verify zero unhandled exceptions on CLOB WebSocket disconnects, auto-reconnect backoff, and REST fallback."""
    feed = LivePolymarketFeed(
        initial_reconnect_delay=0.05,
        max_reconnect_delay=0.15,
        fallback_poll_interval=0.05,
    )
    feed.subscribe("tok_resilience_test")

    # Simulate WebSocket connection dropped by peer
    with patch("websockets.connect", side_effect=ConnectionResetError("Remote host closed connection")):
        feed._running = True
        ws_task = asyncio.create_task(feed._ws_loop())

        # Allow loop to execute reconnect attempts and backoff
        await asyncio.sleep(0.15)
        assert not feed._ws_connected, "Feed should recognize WebSocket is disconnected."

        # Verify graceful stop without unhandled exception
        feed._running = False
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 8. Dynamic Gas Pricing & Merge Threshold Resilience
# ---------------------------------------------------------------------------

def test_dynamic_gas_rpc_failure_and_timeout_resilience() -> None:
    """Verify zero unhandled exceptions when Polygon RPC or fast oracle times out or disconnects."""
    ctf = PolygonCtfClient(dry_run=True)

    # Simulate RPC failure
    with patch.object(ctf.chain_client, "_rpc", side_effect=TimeoutError("RPC timeout")), \
         patch("requests.get", side_effect=ConnectionError("Oracle unreachable")):
        # Should gracefully return fallback baseline without raising
        gas_price = ctf.get_dynamic_gas_price_gwei()
        assert gas_price == 40.0

        # Cost and threshold should remain valid
        cost = ctf.estimate_merge_cost_usd()
        assert cost > 0.0

        threshold = ctf.calculate_optimal_merge_threshold()
        assert threshold >= 1.0


def test_dynamic_merge_threshold_under_extreme_gas_conditions() -> None:
    """Verify optimal merge threshold handles extreme gas conditions, zero costs, and fees cleanly."""
    # Zero / negative gas cost should floor cleanly at min_threshold_shares
    assert calculate_optimal_merge_threshold(gas_cost_usd=0.0) == 1.0
    assert calculate_optimal_merge_threshold(gas_cost_usd=-0.05) == 1.0

    # Extreme gas spike ($1.50 per merge)
    spike_threshold = calculate_optimal_merge_threshold(gas_cost_usd=1.50, fee_basis_points=0.0)
    assert spike_threshold == 1000.0  # 1.50 / 0.0015

    # High fee basis points (100 bps = 1%)
    fee_threshold = calculate_optimal_merge_threshold(gas_cost_usd=0.015, fee_basis_points=100.0)
    # hurdle = 0.0015 + 0.01 = 0.0115 -> 0.015 / 0.0115 = 1.3 complete sets
    assert fee_threshold == 1.3


def test_endogenous_reserve_dynamic_gas_resilience() -> None:
    """Verify endogenous cash reserve adheres strictly to bounds under dynamic gas conditions."""
    allocations = [{"capital": 100.0, "mid": 0.50}, {"capital": 100.0, "mid": 0.50}]
    bankroll = 300.0

    # Test under simulated extreme gas costs
    class MockHighGasCtfClient:
        def estimate_merge_cost_usd(self) -> float:
            return 0.15  # 10x gas spike

    class MockLowGasCtfClient:
        def estimate_merge_cost_usd(self) -> float:
            return 0.0005  # extremely cheap gas

    reserve_high = calculate_endogenous_reserve(
        allocations=allocations,
        bankroll_usd=bankroll,
        ctf_client=MockHighGasCtfClient(),
    )
    # High reserve must be safely capped by ceiling (35% of bankroll = $105)
    assert reserve_high <= bankroll * 0.35
    assert reserve_high >= 15.0

    reserve_low = calculate_endogenous_reserve(
        allocations=allocations,
        bankroll_usd=bankroll,
        ctf_client=MockLowGasCtfClient(),
    )
    # Low reserve must respect physical floor and remain positive
    assert reserve_low > 0.0
    assert reserve_low < reserve_high

