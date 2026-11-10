"""Operator-runner gating and honesty tests for polymarketbot.live.runner.

No network: every client, feed and engine is a local fake.
"""

from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace

import pytest

import polymarketbot.live.runner as runner

END_DATE = "2030-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------
def test_dry_run_needs_no_confirmation_and_does_not_prompt():
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "START LIVE"

    assert runner.require_live_confirmation(False, False, False, ask=ask) is True
    assert asked == []


def test_live_with_yes_needs_no_tty_and_no_prompt():
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "nope"

    assert runner.require_live_confirmation(True, True, False, ask=ask) is True
    assert asked == []


def test_live_confirmation_requires_exact_phrase():
    assert runner.require_live_confirmation(True, False, True, ask=lambda _: "yes") is False
    assert runner.require_live_confirmation(True, False, True, ask=lambda _: "start live") is False
    assert runner.require_live_confirmation(True, False, True, ask=lambda _: " START LIVE ") is True


def test_live_without_tty_refuses_without_prompting():
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "START LIVE"

    assert runner.require_live_confirmation(True, False, False, ask=ask) is False
    assert asked == []


# ---------------------------------------------------------------------------
# Market resolution never fabricates tokens
# ---------------------------------------------------------------------------
class FakeFeed:
    def __init__(self, markets: list[dict] | None = None, book=None) -> None:
        self._markets = markets or []
        self._book = book

    def get_rewards_markets_sync(self, *args, **kwargs):
        return list(self._markets)

    def get_book_sync(self, token_id: str, *args, **kwargs):
        return self._book


class FakeClobMetadata:
    def get_market(self, condition_id: str):
        return {
            "neg_risk": False,
            "tick_size": 0.01,
            "fee_rate_bps": 0,
            "closed": False,
            "accepting_orders": True,
            "end_date_iso": END_DATE,
            "raw": {"question": "Will the test pass?"},
        }

    def get_tick_size(self, token_id: str) -> float:
        return 0.01

    def get_neg_risk(self, token_id: str) -> bool:
        return False


def _rewards_market(tokens: list[dict]) -> dict:
    return {
        "condition_id": "0xabc123",
        "question": "Will the test pass?",
        "tokens": tokens,
        "rewards_min_size": 20.0,
        "rewards_max_spread": 3.0,
        "rewards_daily_rate": 25.0,
        "closed": False,
        "archived": False,
        "accepting_orders": True,
        "end_date_iso": END_DATE,
        "neg_risk": False,
        "tick_size": 0.01,
    }


def test_resolve_market_details_raises_when_token_id_missing():
    feed = FakeFeed([_rewards_market([{"outcome": "YES", "price": 0.42}])])
    with pytest.raises(SystemExit) as excinfo:
        runner.resolve_market_details(feed, FakeClobMetadata(), requested_cid="0xabc123")
    assert "token id" in str(excinfo.value)


def test_resolve_market_details_raises_on_invalid_explicit_token():
    feed = FakeFeed([_rewards_market([{"outcome": "YES", "price": 0.42}])])
    with pytest.raises(SystemExit) as excinfo:
        runner.resolve_market_details(
            feed,
            FakeClobMetadata(),
            requested_cid="0xabc123",
            requested_yes="YES_TOKEN",
            requested_no="222",
        )
    assert "--token-yes" in str(excinfo.value)


def test_resolve_market_details_raises_when_no_market_available(monkeypatch):
    monkeypatch.setattr(runner, "find_optimal_lp_markets", lambda **kwargs: [])
    with pytest.raises(SystemExit) as excinfo:
        runner.resolve_market_details(FakeFeed([]), FakeClobMetadata())
    assert "No open rewards market" in str(excinfo.value)


def test_resolve_market_details_returns_numeric_tokens_and_metadata():
    feed = FakeFeed(
        [
            _rewards_market(
                [
                    {"outcome": "YES", "token_id": "123456789", "price": 0.42},
                    {"outcome": "NO", "token_id": "987654321"},
                ]
            )
        ]
    )
    resolved = runner.resolve_market_details(feed, FakeClobMetadata(), requested_cid="0xabc123")

    assert resolved["condition_id"] == "0xabc123"
    assert resolved["token_yes"] == "123456789"
    assert resolved["token_no"] == "987654321"
    assert resolved["tick_size"] == pytest.approx(0.01)
    assert resolved["tick_size_cents"] == pytest.approx(1.0)
    assert resolved["neg_risk"] is False
    assert resolved["fee_rate_bps"] == 0
    assert resolved["end_date_iso"] == END_DATE
    assert resolved["days_to_resolution"] >= 1.0 / 24.0
    assert resolved["daily_reward"] == pytest.approx(25.0)
    assert resolved["max_spread"] == pytest.approx(3.0)
    assert resolved["min_size"] == pytest.approx(20.0)
    # The returned tokens are the numeric ids that were validated for live.
    returned_tokens = resolved["raw_market"]["tokens"]
    assert [token["token_id"] for token in returned_tokens] == ["123456789", "987654321"]


def test_resolve_market_details_uses_cli_reward_config_for_manual_market():
    class ManualClob(FakeClobMetadata):
        def get_market(self, condition_id: str):
            return {
                "neg_risk": True,
                "tick_size": 0.001,
                "fee_rate_bps": 25,
                "closed": False,
                "accepting_orders": True,
                "end_date_iso": END_DATE,
                "raw": {"question": "Manual market?"},
            }

    feed = FakeFeed(
        [],
        book=SimpleNamespace(
            bids=[SimpleNamespace(price=0.48)],
            asks=[SimpleNamespace(price=0.52)],
        ),
    )
    resolved = runner.resolve_market_details(
        feed,
        ManualClob(),
        requested_cid="0xmanual",
        requested_yes="111",
        requested_no="222",
        min_size=50.0,
        max_spread=2.0,
        daily_reward=10.0,
    )
    assert resolved["token_yes"] == "111"
    assert resolved["token_no"] == "222"
    assert resolved["neg_risk"] is True
    assert resolved["tick_size"] == pytest.approx(0.001)
    assert resolved["fee_rate_bps"] == 25
    assert resolved["daily_reward"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Live preflight abort (fake engine)
# ---------------------------------------------------------------------------
def _market_info() -> dict:
    return {
        "condition_id": "0xcond",
        "question": "Will the test pass?",
        "token_yes": "111",
        "token_no": "222",
        "daily_reward": 25.0,
        "max_spread": 3.0,
        "min_size": 20.0,
        "mid_price": 0.5,
        "market_spread_cents": 1.0,
        "tick_size": 0.01,
        "tick_size_cents": 1.0,
        "neg_risk": False,
        "fee_rate_bps": 0,
        "end_date_iso": END_DATE,
        "end_timestamp": 1893456000.0,
        "days_to_resolution": 30.0,
        "category": "default",
        "competing_score_override": None,
        "raw_market": {},
    }


class FakeCreds:
    signature_type = 0
    private_key = ""
    address = "0xmaker"
    is_configured = True


class FakeLiveClob:
    def __init__(self, *args, **kwargs) -> None:
        self.dry_run = bool(kwargs.get("dry_run", True))
        self.maker_address = "0xmaker"
        self.signer_address = "0xmaker"
        self.credentials = FakeCreds()
        self._mock_balance_usd = 0.0
        self.order_version = 2
        self.open_orders_reads = 0

    def get_balance(self):
        return {"available": 100.0, "locked": 0.0, "allowance": 100.0, "raw": {}}

    def get_market(self, condition_id: str):
        return {
            "closed": False,
            "accepting_orders": True,
            "end_date_iso": END_DATE,
            "tick_size": 0.01,
            "neg_risk": False,
            "fee_rate_bps": 0,
            "raw": {},
        }

    def get_open_orders(self, market=None):
        self.open_orders_reads += 1
        return [{"id": "0xsurvivor"}]


class FakeCtf:
    is_settlement_enabled = True

    def __init__(self, *args, **kwargs) -> None:
        self.dry_run = bool(kwargs.get("dry_run", True))

    def get_pol_balance(self, address: str) -> float:
        return 0.5

    def get_usdc_balance(self, address: str) -> float:
        return 12.0


class FakeLiveFeed:
    instances: list["FakeLiveFeed"] = []
    book = None
    ws_connected = True

    def __init__(self, *args, **kwargs) -> None:
        self.started = False
        self.stopped = False
        FakeLiveFeed.instances.append(self)

    def add_book_listener(self, callback) -> None:
        pass

    def add_trade_listener(self, callback) -> None:
        pass

    def subscribe(self, tokens) -> None:
        pass

    def get_book_sync(self, token_id: str, *args, **kwargs):
        return self.book

    def is_stale(self, max_age: float = 30.0) -> bool:
        return False

    @property
    def is_ws_connected(self) -> bool:
        return self.ws_connected

    @property
    def last_message_age_seconds(self) -> float:
        return 0.1

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class NotReadyEngine:
    created: list["NotReadyEngine"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.dry_run = bool(kwargs.get("dry_run", False))
        self.trading_enabled = True
        self.halt_reason = None
        self.market_closed = False
        self.asset_id_yes = kwargs.get("asset_id_yes")
        self.asset_id_no = kwargs.get("asset_id_no")
        self.quote_size = kwargs.get("quote_size", 0.0)
        self.spread_offset_cents = kwargs.get("spread_offset_cents", 0.0)
        self.closed = False
        NotReadyEngine.created.append(self)

    def reconcile_startup(self) -> None:
        pass

    def preflight_check(self):
        return SimpleNamespace(ready_for_live=False, summary=lambda: "Preflight Check: NOT READY")

    def get_telemetry(self):
        return {}

    def close(self) -> None:
        self.closed = True


class LoopEngine(NotReadyEngine):
    """Ready engine for loop/shutdown tests."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cancel_attempts = 0

    def preflight_check(self):
        return SimpleNamespace(ready_for_live=True, summary=lambda: "Preflight Check: READY")

    def on_book_update(self, *args, **kwargs) -> None:
        pass

    def on_trade_update(self, *args, **kwargs) -> None:
        pass

    def sync_live_orders_and_fills(self, timestamp=None) -> None:
        pass

    def sync_live_balances(self) -> None:
        pass

    def process_reward_epoch(self, timestamp=None, competing_score=0.0) -> None:
        pass

    def cancel_all_quotes(self) -> bool:
        self.cancel_attempts += 1
        return False

    def flush_ledger(self) -> None:
        pass


class NoSettlementCtf(FakeCtf):
    is_settlement_enabled = False


class RefusingBaselineEngine(LoopEngine):
    def finalize_initial_baseline(self) -> bool:
        return False


class RecordingEngine(LoopEngine):
    """Records whether quoting was enabled during book seeding and baseline."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.book_update_trading_states: list[bool] = []
        self.baseline_trading_state: bool | None = None

    def on_book_update(self, *args, **kwargs) -> None:
        self.book_update_trading_states.append(self.trading_enabled)

    def finalize_initial_baseline(self) -> bool:
        self.baseline_trading_state = self.trading_enabled
        return True

    def cancel_all_quotes(self) -> bool:
        self.cancel_attempts += 1
        return True


def _install_live_fakes(
    monkeypatch, market_factory, ctf_cls=FakeCtf, engine_cls=NotReadyEngine
) -> None:
    monkeypatch.setattr(
        runner.ClobAuthCredentials, "from_env", staticmethod(lambda: FakeCreds())
    )
    monkeypatch.setattr(runner, "LiveClobClient", FakeLiveClob)
    monkeypatch.setattr(runner, "PolygonCtfClient", ctf_cls)
    monkeypatch.setattr(runner, "LivePolymarketFeed", FakeLiveFeed)
    monkeypatch.setattr(runner, "RealMoneyTradingEngine", engine_cls)
    monkeypatch.setattr(runner, "resolve_market_details", lambda *a, **k: market_factory())
    monkeypatch.setattr(runner, "derive_quote_size", lambda **kwargs: 10.0)
    monkeypatch.setattr(runner, "optimize_spread_offset", lambda **kwargs: (1.0, {}))


def _live_args(tmp_path, *extra: str) -> "argparse.Namespace":
    return runner.parse_args(
        [
            "--live",
            "--yes",
            "--db-path",
            str(tmp_path / "runner.db"),
            "--ledger-path",
            str(tmp_path / "runner_ledger.json"),
            *extra,
        ]
    )


def test_live_with_unready_preflight_aborts_without_downgrade(monkeypatch, tmp_path):
    NotReadyEngine.created.clear()
    FakeLiveFeed.instances.clear()
    _install_live_fakes(monkeypatch, _market_info)

    exit_code = asyncio.run(runner.run_live_runner_async(_live_args(tmp_path)))

    assert exit_code == 1
    assert len(NotReadyEngine.created) == 1
    engine = NotReadyEngine.created[0]
    # The runner must never silently flip the engine to dry-run.
    assert engine.dry_run is False
    assert engine.closed is True
    # Abort happens before the feed is started or any quote is placed.
    assert all(not feed.started for feed in FakeLiveFeed.instances)
    # Execution metadata reaches the engine.
    assert engine.kwargs["tick_size"] == pytest.approx(0.01)
    assert engine.kwargs["neg_risk"] is False
    assert engine.kwargs["fee_rate_bps"] == 0
    assert engine.kwargs["book_stale_seconds"] == pytest.approx(30.0)
    assert engine.kwargs["days_to_resolution"] >= 1.0 / 24.0
    # The sizing cap, not the whole wallet, is the engine's cash seed.
    assert engine.kwargs["initial_cash"] == pytest.approx(100.0)
    assert engine.kwargs["total_wallet_balance"] == pytest.approx(100.0)


def test_live_neg_risk_market_is_refused(monkeypatch, tmp_path):
    NotReadyEngine.created.clear()
    _install_live_fakes(monkeypatch, lambda: {**_market_info(), "neg_risk": True})

    exit_code = asyncio.run(runner.run_live_runner_async(_live_args(tmp_path)))

    assert exit_code == 2
    assert NotReadyEngine.created == []


def test_live_without_settlement_is_refused_unless_allowed(monkeypatch, tmp_path):
    NotReadyEngine.created.clear()
    _install_live_fakes(monkeypatch, _market_info, ctf_cls=NoSettlementCtf)

    exit_code = asyncio.run(runner.run_live_runner_async(_live_args(tmp_path)))
    assert exit_code == 2
    assert NotReadyEngine.created == []

    # The explicit override proceeds to preflight (which is not ready in the fake).
    NotReadyEngine.created.clear()
    exit_code = asyncio.run(
        runner.run_live_runner_async(_live_args(tmp_path, "--allow-no-settlement"))
    )
    assert exit_code == 1
    assert len(NotReadyEngine.created) == 1
    assert NotReadyEngine.created[0].dry_run is False


def test_shutdown_unconfirmed_cancel_retries_then_exits_3(monkeypatch, tmp_path):
    """An unproven final cancel must retry, name the survivors, and exit non-zero."""
    NotReadyEngine.created.clear()
    FakeLiveFeed.instances.clear()
    _install_live_fakes(monkeypatch, _market_info, engine_cls=LoopEngine)

    clobs: list[FakeLiveClob] = []

    def clob_factory(*args, **kwargs):
        instance = FakeLiveClob(*args, **kwargs)
        clobs.append(instance)
        return instance

    monkeypatch.setattr(runner, "LiveClobClient", clob_factory)
    monkeypatch.setattr(runner, "CANCEL_RETRY_BACKOFF_SECONDS", 0.0)

    args = _live_args(tmp_path, "--duration", "0.05", "--poll-interval", "0.05")
    exit_code = asyncio.run(runner.run_live_runner_async(args))

    engine = LoopEngine.created[0]
    assert exit_code == 3
    assert engine.cancel_attempts == runner.CANCEL_RETRY_ATTEMPTS
    # The surviving order list is re-read from the venue before giving up.
    assert clobs[0].open_orders_reads >= 1
    # The feed is stopped only after the cancellation retries/verification.
    assert FakeLiveFeed.instances[0].stopped is True
    assert engine.closed is True


def test_startup_seeds_books_with_quoting_disabled_and_enables_after_ws(monkeypatch, tmp_path):
    NotReadyEngine.created.clear()
    FakeLiveFeed.instances.clear()
    _install_live_fakes(monkeypatch, _market_info, engine_cls=RecordingEngine)
    monkeypatch.setattr(
        FakeLiveFeed,
        "book",
        SimpleNamespace(
            bids=[SimpleNamespace(price=0.48)],
            asks=[SimpleNamespace(price=0.52)],
        ),
    )

    args = _live_args(tmp_path, "--duration", "0.05", "--poll-interval", "0.05")
    exit_code = asyncio.run(runner.run_live_runner_async(args))

    engine = RecordingEngine.created[0]
    assert exit_code == 0
    # Both initial REST snapshots were seeded while quoting stayed disabled.
    assert engine.book_update_trading_states == [False, False]
    # The baseline finalizer also ran with quoting still disabled.
    assert engine.baseline_trading_state is False
    # Only the connected WebSocket re-enabled quoting.
    assert engine.trading_enabled is True


def test_baseline_refusal_aborts_before_feed_start(monkeypatch, tmp_path):
    NotReadyEngine.created.clear()
    FakeLiveFeed.instances.clear()
    _install_live_fakes(monkeypatch, _market_info, engine_cls=RefusingBaselineEngine)

    exit_code = asyncio.run(runner.run_live_runner_async(_live_args(tmp_path)))

    assert exit_code == 2
    engine = RefusingBaselineEngine.created[0]
    assert engine.closed is True
    assert all(not feed.started for feed in FakeLiveFeed.instances)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def _full_telemetry() -> dict:
    return {
        "mode": "REAL_MONEY",
        "order_version": 2,
        "condition_id": "0xcond",
        "equity": 101.5,
        "initial_equity": 100.0,
        "free_cash": 50.0,
        "locked_cash": 10.0,
        "cash": 50.0,
        "total_wallet_balance": 100.0,
        "budget_pct": 100.0,
        "trading_bankroll": 100.0,
        "pol_balance": 0.5,
        "fees_paid": 0.01,
        "total_gas_fees": 0.02,
        "shares_yes": 1.0,
        "shares_no": 0.5,
        "complete_sets": 0.0,
        "net_exposure": 0.5,
        "total_pnl": 1.5,
        "realized_pnl": 0.2,
        "reserve_cash_usd": 50.0,
        "latest_deskew": {"pathway": "HOLD_STABLE"},
        "epoch_index": 60,
        "modelled_rewards": 0.25,
        "actual_rewards_usd": 0.1,
        "actual_rebates_usd": 0.0,
        "actual_daily_rewards_usd": 0.05,
        "actual_total_payout_usd": 0.1,
        "total_merges": 1,
        "total_splits": 0,
        "fills_count": 4,
        "total_volume_usd": 20.0,
        "active_quotes": [],
        "trading_enabled": True,
        "halt_reason": None,
        "book_stale": False,
        "book_age_seconds": 1.2,
        "market_closed": False,
        # A poisoned field proves the dashboard never renders client secrets.
        "private_key": "SUPERSECRET",
    }


def test_dashboard_renders_full_telemetry_and_never_leaks_keys(capsys):
    runner.render_dashboard(
        _full_telemetry(), _market_info(), start_time=time.time() - 5.0, clear_screen=False
    )
    output = capsys.readouterr().out

    assert "LIVE REAL-MONEY TRADING" in output
    assert "Order version:" in output
    assert "Free Cash:" in output
    assert "Locked Cash:" in output
    assert "Sizing Cap:" in output
    assert "Modelled" in output
    assert "Actual" in output
    assert "Proj. Daily" in output
    assert "Floor accrued" in output
    assert "ENABLED" in output
    assert "SUPERSECRET" not in output


def test_dashboard_tolerates_partial_and_empty_telemetry(capsys):
    runner.render_dashboard({"mode": "DRY_RUN"}, {}, start_time=0.0, clear_screen=False)
    partial = capsys.readouterr().out
    assert "DRY-RUN / SAFETY INSPECTION" in partial
    assert "n/a" in partial

    runner.render_dashboard({}, {}, start_time=0.0, clear_screen=False)
    empty = capsys.readouterr().out
    assert "MODE n/a" in empty or "n/a" in empty


def test_dashboard_projection_is_withheld_without_a_run_rate(capsys):
    telemetry = _full_telemetry()
    telemetry.pop("projected_daily_usd", None)
    telemetry.pop("accrued_daily_usd", None)
    telemetry["epoch_index"] = 5
    runner.render_dashboard(telemetry, _market_info(), start_time=0.0, clear_screen=False)
    output = capsys.readouterr().out
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    assert "Proj. Daily:    n/a" in plain
    assert "Floor accrued: n/a" in plain


# ---------------------------------------------------------------------------
# Removed / renamed flags
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "flag",
    ["--scan-interval", "--switch-interval", "--holding-horizon-hours", "--disable-switching"],
)
def test_removed_flags_are_not_parsed(flag):
    with pytest.raises(SystemExit):
        runner.parse_args([flag, "1"])


def test_new_flags_and_no_removed_attributes():
    args = runner.parse_args([])

    assert args.live is False
    assert args.yes is False
    assert args.reward_refresh_interval == pytest.approx(60.0)
    assert args.max_feed_staleness == pytest.approx(30.0)
    assert args.redeem_on_close is True
    assert args.allow_no_settlement is False
    assert not hasattr(args, "scan_interval")
    assert not hasattr(args, "switch_interval")
    assert not hasattr(args, "holding_horizon_hours")
    assert not hasattr(args, "disable_switching")


def test_module_imports_without_running_main():
    namespace = vars(runner)
    assert callable(namespace["main"])
    assert callable(namespace["require_live_confirmation"])
    assert callable(namespace["resolve_market_details"])
    assert callable(namespace["render_dashboard"])


def test_resolve_market_details_refuses_stale_requested_token():
    feed = FakeFeed(
        [
            _rewards_market(
                [
                    {"outcome": "YES", "token_id": "111"},
                    {"outcome": "NO", "token_id": "222"},
                ]
            )
        ]
    )
    with pytest.raises(SystemExit) as excinfo:
        runner.resolve_market_details(
            feed,
            FakeClobMetadata(),
            requested_cid="0xabc123",
            requested_yes="111",
            requested_no="999",
        )
    assert "does not match" in str(excinfo.value)


def test_clob_detail_tokens_override_stale_rewards_payload():
    class ClobWithTokens(FakeClobMetadata):
        def get_market(self, condition_id: str):
            detail = super().get_market(condition_id)
            detail = dict(detail)
            detail["raw"] = {
                "question": "Token market?",
                "tokens": [
                    {"outcome": "Yes", "token_id": "111", "price": 0.42},
                    {"outcome": "No", "token_id": "222"},
                ],
            }
            return detail

    feed = FakeFeed(
        [
            _rewards_market(
                [
                    {"outcome": "YES", "token_id": "111"},
                    {"outcome": "NO", "token_id": "999"},
                ]
            )
        ]
    )
    resolved = runner.resolve_market_details(feed, ClobWithTokens(), requested_cid="0xabc123")
    assert resolved["token_yes"] == "111"
    assert resolved["token_no"] == "222"
