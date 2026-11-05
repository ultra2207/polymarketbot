"""Live accounting tests for RealMoneyTradingEngine.

Everything here runs against local fakes: no CLOB REST, no RPC, no network.
"""

from __future__ import annotations

import time

import pytest

from polymarketbot.rewards.deskew import DeSkewDecision, DeSkewPathway
from polymarketbot.trading.client import ClobAPIError, OrderPlacementResult
from polymarketbot.trading.ctf import MergeExecutionResult
from polymarketbot.trading.engine import RealMoneyTradingEngine
from polymarketbot.trading.settlement import (
    NEGRISK_ADAPTER,
    NEGRISK_MERGE_SELECTOR,
    ROUTER,
    ROUTER_MERGE_SELECTOR,
    SettlementRoute,
)


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
        self.venue_orders: list[dict] | None = None
        self.order_by_id: dict[str, dict] = {}
        self.balance_error: Exception | None = None
        self.open_orders_error: Exception | None = None
        self.get_order_error: Exception | None = None
        self.cancel_order_result = True
        self.placed: list[dict] = []

    def get_balance(self) -> dict:
        if self.balance_error is not None:
            raise self.balance_error
        return {
            "available": self.available,
            "locked": 0.0,
            "allowance": 1_000_000.0,
            "raw": {},
        }

    def get_allowances(self) -> dict:
        return {"approved": True, "collateral": 1_000_000.0, "conditional": 0.0, "raw": {}}

    def get_open_orders(self, market=None, *, order_id=None, max_pages=20) -> list[dict]:
        if self.open_orders_error is not None:
            raise self.open_orders_error
        orders = self.venue_orders if self.venue_orders is not None else self.open_orders
        if order_id:
            return [o for o in orders if o.get("id") == order_id]
        if market:
            return [o for o in orders if o.get("market") in (None, market)]
        return list(orders)

    def get_order(self, order_id: str):
        if self.get_order_error is not None:
            raise self.get_order_error
        return self.order_by_id.get(order_id)

    def cancel_order(self, order_id: str) -> bool:
        if not self.cancel_order_result:
            return False
        self.open_orders = [o for o in self.open_orders if o.get("id") != order_id]
        self.order_by_id.pop(order_id, None)
        return True

    def place_order(self, token_id, side, price, size, **kwargs) -> OrderPlacementResult:
        order_id = f"ord_{len(self.placed) + 1}"
        self.placed.append(
            {
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                **kwargs,
            }
        )
        return OrderPlacementResult(
            success=True,
            order_id=order_id,
            status="LIVE",
            side=side,
            price=price,
            size=size,
            is_dry_run=False,
        )

    def get_total_rewards(self) -> dict:
        return {"reward_income_usd": 0.0, "maker_rebates_usd": 0.0, "source": "simulated"}

    def get_daily_rewards(self, date=None) -> dict:
        return {"daily_combined_usd": 0.0, "source": "simulated"}

    def get_order_scoring(self, order_id: str) -> dict:
        return {"scoring": False}


class FakeCtf:
    dry_run = False
    private_key = "0xPRIVATEKEYMUSTNEVERLEAK"
    is_settlement_enabled = False

    def __init__(self) -> None:
        self.chain_client = FakeChain()
        self.position_balances: dict[str, float] = {}
        self.balance_error: Exception | None = None
        self.merge_result: MergeExecutionResult | None = None
        self.merge_calls: list[dict] = []
        self.settle_calls: list[dict] = []

    def estimate_merge_cost_usd(self) -> float:
        return 0.01

    def calculate_optimal_merge_threshold(self, gas_cost_usd: float = 0.01) -> float:
        return 1.0

    def get_pol_balance(self, address: str) -> float:
        return 1.0

    def get_position_balance(self, token_id: str, address: str) -> float:
        if self.balance_error is not None:
            raise self.balance_error
        return float(self.position_balances.get(str(token_id), 0.0))

    def merge_positions(self, condition_id, amount_shares, **kwargs) -> MergeExecutionResult:
        self.merge_calls.append(
            {"condition_id": condition_id, "amount_shares": amount_shares, **kwargs}
        )
        if self.merge_result is not None:
            return self.merge_result
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

    def execute_settlement(
        self, calldata, *, to_address, condition_id, amount_shares, **kwargs
    ) -> MergeExecutionResult:
        self.settle_calls.append(
            {
                "calldata": calldata,
                "to_address": to_address,
                "condition_id": condition_id,
                "amount_shares": amount_shares,
                **kwargs,
            }
        )
        return MergeExecutionResult(
            success=True,
            tx_hash="0xroute",
            condition_id=condition_id,
            complete_sets_merged=amount_shares,
            usdc_unlocked=amount_shares,
            gas_fee_usd=0.02,
            gas_units_used=1,
            is_dry_run=False,
            status="success",
        )


def make_engine(tmp_path, *, cash: float = 100.0, clob=None, ctf=None,
                condition_id: str = "0xCOND"):
    clob = clob or FakeClob(available=cash)
    ctf = ctf or FakeCtf()
    engine = RealMoneyTradingEngine(
        condition_id=condition_id,
        asset_id_yes="1111",
        asset_id_no="2222",
        clob_client=clob,
        ctf_client=ctf,
        initial_cash=cash,
        db_path=tmp_path / "engine.db",
        ledger_path=tmp_path / "ledger.json",
        dry_run=True,
        gas_fee_merge=0.01,
        auto_merge_threshold=1.0,
    )
    return engine, clob, ctf


def go_live(engine: RealMoneyTradingEngine) -> None:
    engine.dry_run = False
    engine._locked_already_in_balance = False
    engine._cost_basis_unknown = False


def quote(order_id="ord_1", *, side="BUY", size=10.0, price=0.4, matched=0.0):
    return {
        "quote_id": f"{side}_YES",
        "order_id": order_id,
        "market": "0xCOND",
        "token_side": "YES",
        "side": side,
        "price": price,
        "size": size,
        "size_matched": matched,
        "is_taker": False,
        "status": "LIVE",
    }


# ---------------------------------------------------------------------------
# Order reconciliation: confirmed facts only
# ---------------------------------------------------------------------------


def test_open_order_api_failure_books_no_phantom_fill(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.active_quotes["BUY_YES"] = quote(matched=0.0)
    clob.open_orders_error = ClobAPIError("429 Too Many Requests", status_code=429, path="/data/orders")

    fills = engine.sync_live_orders_and_fills()

    assert fills == []
    assert engine.cash == pytest.approx(100.0)
    assert engine.shares_yes == 0.0
    assert "BUY_YES" in engine.active_quotes
    assert engine.fills_history == []


def test_vanished_order_unverifiable_stays_tracked(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.active_quotes["BUY_YES"] = quote(matched=2.0)
    clob.open_orders = []
    clob.get_order_error = ClobAPIError("503 unavailable", status_code=503, path="/data/orders")

    fills = engine.sync_live_orders_and_fills()

    assert fills == []
    assert "BUY_YES" in engine.active_quotes
    assert engine.active_quotes["BUY_YES"]["size_matched"] == pytest.approx(2.0)
    assert engine.shares_yes == 0.0


def test_vanished_order_matched_books_exact_delta_at_exchange_price(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine._locked_already_in_balance = True
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.avg_cost_yes = 0.0
    engine.active_quotes["BUY_YES"] = quote(price=0.40, size=10.0, matched=2.0)
    clob.open_orders = []
    clob.order_by_id["ord_1"] = {
        "id": "ord_1",
        "market": "0xCOND",
        "status": "MATCHED",
        "size_matched": "5",
        "original_size": "10",
        "price": "0.42",
    }

    fills = engine.sync_live_orders_and_fills()

    assert len(fills) == 1
    assert fills[0].fill_size == pytest.approx(3.0)
    assert fills[0].fill_price == pytest.approx(0.42)
    assert engine.shares_yes == pytest.approx(3.0)
    assert engine.avg_cost_yes == pytest.approx(0.42)
    assert engine.cash == pytest.approx(100.0 - 3.0 * 0.42)
    assert "BUY_YES" not in engine.active_quotes


def test_vanished_matched_but_not_terminal_stays_tracked(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.active_quotes["BUY_YES"] = quote(price=0.40, size=10.0, matched=2.0)
    clob.open_orders = []
    clob.order_by_id["ord_1"] = {
        "id": "ord_1",
        "market": "0xCOND",
        "status": "LIVE",
        "size_matched": "4",
        "original_size": "10",
        "price": "0.40",
    }

    fills = engine.sync_live_orders_and_fills()

    assert len(fills) == 1
    assert "BUY_YES" in engine.active_quotes
    assert engine.active_quotes["BUY_YES"]["size_matched"] == pytest.approx(4.0)


def test_open_order_fill_is_booked_at_exchange_price_not_local_quote(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.active_quotes["BUY_YES"] = quote(price=0.40, size=10.0, matched=1.0)
    clob.open_orders = [
        {
            "id": "ord_1",
            "market": "0xCOND",
            "status": "LIVE",
            "size_matched": "4",
            "original_size": "10",
            "price": "0.38",
        }
    ]

    fills = engine.sync_live_orders_and_fills()

    assert len(fills) == 1
    assert fills[0].fill_price == pytest.approx(0.38)
    assert fills[0].fill_size == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Cash / locked / equity
# ---------------------------------------------------------------------------


def test_balance_api_failure_leaves_cash_unchanged(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    clob.balance_error = ClobAPIError("429 rate limited", status_code=429, path="/balance-allowance")

    balances = engine.sync_live_balances()

    assert engine.cash == pytest.approx(100.0)
    assert balances["cash"] == pytest.approx(100.0)


def test_locked_cash_is_counted_once_both_calibrations(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.shares_no = 0.0
    engine.active_quotes["BUY_YES"] = quote(price=0.5, size=10.0, matched=2.0)

    assert engine._compute_locked_cash() == pytest.approx(4.0)

    engine._locked_already_in_balance = False
    assert engine.get_locked_cash() == pytest.approx(4.0)
    assert engine.get_equity() == pytest.approx(104.0)

    engine._locked_already_in_balance = True
    # Raw endpoint balance already includes the reserve: free cash is net of it.
    assert engine.get_cash() == pytest.approx(96.0)
    assert engine.get_locked_cash() == pytest.approx(4.0)
    assert engine.get_equity() == pytest.approx(100.0)
    assert engine.locked_cash == pytest.approx(4.0)


def prime_yes_book(engine: RealMoneyTradingEngine) -> None:
    engine._book_ts["YES"] = time.time()
    engine._best_bids["YES"] = 0.48
    engine._best_asks["YES"] = 0.50


@pytest.mark.parametrize("includes_locked", [False, True])
def test_cash_ledger_place_and_fill_equity_invariant(tmp_path, includes_locked):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine._locked_already_in_balance = includes_locked
    engine.cash = 100.0
    prime_yes_book(engine)

    engine._set_quote("YES", "BUY", 0.49, 20.0, time.time())

    qdata = engine.active_quotes["BUY_YES"]
    reserve = qdata["size"] * qdata["price"]
    assert engine.get_locked_cash() == pytest.approx(reserve)
    if includes_locked:
        # Endpoint balance includes the reserve: raw cash unchanged, free drops.
        assert engine.cash == pytest.approx(100.0)
        assert engine.get_cash() == pytest.approx(100.0 - reserve)
    else:
        # Endpoint balance is net: local cash reserved at placement.
        assert engine.cash == pytest.approx(100.0 - reserve)
        assert engine.get_cash() == pytest.approx(100.0 - reserve)
    assert engine.get_equity() == pytest.approx(100.0)

    qdata["size_matched"] = qdata["size"]
    engine._apply_fill("YES", "BUY", qdata["size"], qdata["price"], ts=time.time())

    # A fill consumes the reserve in both modes; free cash and equity are unchanged.
    assert engine.cash == pytest.approx(100.0 - reserve)
    assert engine.get_locked_cash() == pytest.approx(0.0)
    assert engine.get_cash() == pytest.approx(100.0 - reserve)
    assert engine.get_equity() == pytest.approx(100.0)


@pytest.mark.parametrize("includes_locked", [False, True])
def test_cash_ledger_cancel_restores_reservation(tmp_path, includes_locked):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine._locked_already_in_balance = includes_locked
    engine.cash = 100.0
    prime_yes_book(engine)
    engine._set_quote("YES", "BUY", 0.49, 20.0, time.time())
    assert "BUY_YES" in engine.active_quotes

    engine._cancel_quote("BUY_YES")

    assert "BUY_YES" not in engine.active_quotes
    assert engine.cash == pytest.approx(100.0)
    assert engine.get_locked_cash() == pytest.approx(0.0)
    assert engine.get_cash() == pytest.approx(100.0)
    assert engine.get_equity() == pytest.approx(100.0)


def test_cancel_all_quotes_restores_mode_b_reservation(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine._locked_already_in_balance = False
    engine.cash = 100.0
    prime_yes_book(engine)
    engine._set_quote("YES", "BUY", 0.49, 20.0, time.time())
    qdata = engine.active_quotes["BUY_YES"]
    clob.open_orders = [{"id": qdata["order_id"], "market": "0xCOND", "status": "LIVE"}]

    assert engine.cancel_all_quotes() is True
    assert engine.cash == pytest.approx(100.0)
    assert engine.get_equity() == pytest.approx(100.0)


def test_equity_uses_avg_cost_when_book_is_stale(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.shares_yes = 10.0
    engine.avg_cost_yes = 0.40
    engine.shares_no = 0.0
    engine._book_ts["YES"] = 0.0
    engine._book_ts["NO"] = 0.0

    assert engine.get_equity() == pytest.approx(104.0)


def test_equity_uses_fresh_mid_and_normalises_complementary_book(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.shares_yes = 10.0
    engine.shares_no = 10.0
    engine.avg_cost_yes = 0.10
    engine.avg_cost_no = 0.10
    engine._book_ts["YES"] = time.time()
    engine._book_ts["NO"] = time.time()
    engine._best_bids["YES"], engine._best_asks["YES"] = 0.39, 0.41
    engine._best_bids["NO"], engine._best_asks["NO"] = 0.51, 0.53

    # |0.40 + 0.52 - 1| = 0.08 > 0.02 -> NO marked at 1 - 0.40 = 0.60
    assert engine.get_equity() == pytest.approx(100.0 + 10.0 * 0.40 + 10.0 * 0.60)


# ---------------------------------------------------------------------------
# Fill accounting
# ---------------------------------------------------------------------------


def test_buy_fills_track_weighted_average_cost(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine._locked_already_in_balance = True
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.avg_cost_yes = 0.0

    engine._apply_fill("YES", "BUY", 10.0, 0.40, ts=time.time(), is_taker=True)
    engine._apply_fill("YES", "BUY", 10.0, 0.60, ts=time.time(), is_taker=True)

    assert engine.shares_yes == pytest.approx(20.0)
    assert engine.avg_cost_yes == pytest.approx(0.50)
    assert engine.cash == pytest.approx(90.0)
    assert engine.total_volume_traded_usd == pytest.approx(10.0)


def test_sell_fill_books_realized_pnl_against_average_cost(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 90.0
    engine.shares_yes = 20.0
    engine.avg_cost_yes = 0.50

    engine._apply_fill("YES", "SELL", 5.0, 0.70, ts=time.time())

    assert engine.realized_pnl == pytest.approx(1.0)
    assert engine.shares_yes == pytest.approx(15.0)
    assert engine.avg_cost_yes == pytest.approx(0.50)
    assert engine.cash == pytest.approx(93.5)


def test_oversell_is_clamped_and_never_creates_a_short(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.shares_yes = 5.0
    engine.avg_cost_yes = 0.40

    engine._apply_fill("YES", "SELL", 10.0, 0.60, ts=time.time())

    assert engine.shares_yes == pytest.approx(0.0)
    assert engine.shares_yes >= 0.0
    assert engine.cash == pytest.approx(103.0)
    assert engine.total_volume_traded_usd == pytest.approx(3.0)
    assert engine.realized_pnl == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tier-2 de-skew
# ---------------------------------------------------------------------------


def test_tier2_places_order_but_books_no_fill(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.shares_no = 0.0
    engine._book_ts["YES"] = time.time()
    engine._best_bids["YES"], engine._best_asks["YES"] = 0.48, 0.50
    engine._deskew_order = None
    engine._last_deskew_attempt_ts = 0.0

    decision = DeSkewDecision(
        pathway=DeSkewPathway.SYNTHETIC_MERGE,
        urgency_level="LOW",
        target_token="YES",
        action_side="BUY",
        order_type="MARKET_TAKER",
        target_price=0.52,
        target_size=5.0,
        expected_cost_bps=0.0,
        synthetic_savings_cents=0.2,
        reason="test",
    )
    engine._execute_tier2_taker_deskew(decision, time.time())

    assert len(clob.placed) == 1
    assert clob.placed[0]["post_only"] is False
    assert engine.fills_history == []
    # Placement reserves collateral (net-balance mode) but books no fill.
    assert engine.cash == pytest.approx(100.0 - 5.0 * 0.50)
    assert engine.get_locked_cash() == pytest.approx(5.0 * 0.50)
    assert engine.get_equity() == pytest.approx(100.0)
    assert engine.shares_yes == 0.0
    assert engine._deskew_order is not None
    assert any(qid.startswith("DESKEW_YES_") for qid in engine.active_quotes)


def test_tier2_does_not_stack_while_order_is_unresolved(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine._book_ts["YES"] = time.time()
    engine._best_bids["YES"], engine._best_asks["YES"] = 0.48, 0.50
    engine._deskew_order = None
    engine._last_deskew_attempt_ts = 0.0
    decision = DeSkewDecision(
        pathway=DeSkewPathway.DIRECT_CROSS,
        urgency_level="LOW",
        target_token="YES",
        action_side="BUY",
        order_type="MARKET_TAKER",
        target_price=0.50,
        target_size=5.0,
        expected_cost_bps=0.0,
        synthetic_savings_cents=0.1,
        reason="test",
    )
    now = time.time()
    engine._execute_tier2_taker_deskew(decision, now)
    assert len(clob.placed) == 1

    engine._execute_tier2_taker_deskew(decision, now + 1.0)
    assert len(clob.placed) == 1


def test_tier2_dry_run_still_books_simulated_fill(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine._book_ts["YES"] = time.time()
    engine._best_bids["YES"], engine._best_asks["YES"] = 0.48, 0.50
    decision = DeSkewDecision(
        pathway=DeSkewPathway.DIRECT_CROSS,
        urgency_level="LOW",
        target_token="YES",
        action_side="BUY",
        order_type="MARKET_TAKER",
        target_price=0.50,
        target_size=4.0,
        expected_cost_bps=0.0,
        synthetic_savings_cents=0.1,
        reason="test",
    )

    engine._execute_tier2_taker_deskew(decision, time.time())

    assert engine.shares_yes == pytest.approx(4.0)
    assert engine.cash == pytest.approx(98.0)
    assert len(engine.fills_history) == 1


# ---------------------------------------------------------------------------
# Merges / splits
# ---------------------------------------------------------------------------


def test_merge_disabled_does_not_mutate_state(tmp_path):
    engine, _, ctf = make_engine(tmp_path)
    go_live(engine)
    ctf.is_settlement_enabled = False
    engine.cash = 100.0
    engine.shares_yes = 40.0
    engine.shares_no = 40.0

    result = engine.trigger_ctf_merge()

    assert result is None
    assert engine.cash == pytest.approx(100.0)
    assert engine.shares_yes == pytest.approx(40.0)
    assert engine.shares_no == pytest.approx(40.0)
    assert engine.total_merges == 0
    assert ctf.merge_calls == []


def test_merge_failure_does_not_mutate_state(tmp_path):
    engine, _, ctf = make_engine(tmp_path)
    go_live(engine)
    ctf.is_settlement_enabled = True
    ctf.position_balances = {"1111": 40.0, "2222": 40.0}
    ctf.merge_result = MergeExecutionResult(
        success=False,
        tx_hash="",
        condition_id="0xCOND",
        complete_sets_merged=0.0,
        usdc_unlocked=0.0,
        gas_fee_usd=0.0,
        gas_units_used=0,
        error="rpc failed",
    )
    engine.cash = 100.0
    engine.shares_yes = 40.0
    engine.shares_no = 40.0

    result = engine.trigger_ctf_merge()

    assert result is not None and result.success is False
    assert engine.cash == pytest.approx(100.0)
    assert engine.shares_yes == pytest.approx(40.0)
    assert engine.shares_no == pytest.approx(40.0)
    assert engine.total_merges == 0
    assert len(ctf.merge_calls) == 1


def test_merge_unverifiable_balances_aborts_without_mutation(tmp_path):
    engine, _, ctf = make_engine(tmp_path)
    go_live(engine)
    ctf.is_settlement_enabled = True
    ctf.balance_error = RuntimeError("rpc down")
    engine.cash = 100.0
    engine.shares_yes = 40.0
    engine.shares_no = 40.0

    assert engine.trigger_ctf_merge() is None
    assert engine.total_merges == 0
    assert ctf.merge_calls == []
    assert engine.shares_yes == pytest.approx(40.0)


def test_live_split_refuses_without_mutation(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.shares_no = 0.0

    result = engine.trigger_ctf_split(10.0)

    assert result["success"] is False
    assert engine.cash == pytest.approx(100.0)
    assert engine.shares_yes == 0.0
    assert engine.shares_no == 0.0


# ---------------------------------------------------------------------------
# Rewards, preflight, cancellation
# ---------------------------------------------------------------------------


def test_credit_reward_does_not_change_live_cash(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    go_live(engine)
    engine.cash = 100.0

    engine.credit_reward(12.5, "0xCOND", note="epoch")

    assert engine.cash == pytest.approx(100.0)


def test_credit_reward_still_credits_simulated_cash(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    engine.cash = 100.0

    engine.credit_reward(12.5, "0xCOND", note="epoch")

    assert engine.cash == pytest.approx(112.5)


def test_preflight_never_mutates_dry_run_and_never_exposes_key(tmp_path):
    engine, clob, ctf = make_engine(tmp_path)
    engine.dry_run = False
    clob.available = 123.45

    report = engine.preflight_check()

    assert engine.dry_run is False
    assert report.dry_run_mode is False
    assert report.order_version == 2
    assert report.free_cash == pytest.approx(123.45)
    assert report.wallet_address == "0xMAKER"
    assert ctf.private_key not in report.summary()
    assert "PRIVATEKEYMUSTNEVERLEAK" not in report.summary()


def test_preflight_balance_failure_is_not_ready_without_mutation(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    engine.dry_run = False
    clob.balance_error = ClobAPIError("429 rate limited", status_code=429, path="/balance-allowance")

    report = engine.preflight_check()

    assert engine.dry_run is False
    assert report.ready_for_live is False
    assert any("balance query failed" in rec for rec in report.recommendations)


def test_cancel_all_quotes_returns_false_when_venue_still_lists_order(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.active_quotes["BUY_YES"] = quote()
    still_listed = {"id": "ord_1", "market": "0xCOND", "status": "LIVE"}
    clob.open_orders = [still_listed]
    clob.venue_orders = [still_listed]

    assert engine.cancel_all_quotes() is False
    assert "BUY_YES" in engine.active_quotes
    assert engine.active_quotes["BUY_YES"]["status"] == "CANCELLING"


def test_cancel_all_quotes_returns_true_when_venue_confirms_empty(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    go_live(engine)
    engine.active_quotes["BUY_YES"] = quote()
    still_listed = {"id": "ord_1", "market": "0xCOND", "status": "LIVE"}
    clob.open_orders = [still_listed]
    clob.order_by_id["ord_1"] = still_listed

    assert engine.cancel_all_quotes() is True
    assert engine.active_quotes == {}


# ---------------------------------------------------------------------------
# Dry-run fill model
# ---------------------------------------------------------------------------


def test_dry_run_quote_fills_only_remaining_size_once(tmp_path):
    engine, clob, _ = make_engine(tmp_path)
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.active_quotes["BUY_YES"] = quote(size=10.0, price=0.50, matched=0.0)
    engine.active_quotes["BUY_YES"]["order_id"] = "mock_1"
    engine._book_ts["YES"] = 0.0
    engine._book_ts["NO"] = 0.0

    now = time.time()
    engine.on_trade_update("YES", 0.49, 4.0, "SELL", now)
    engine.on_trade_update("YES", 0.49, 4.0, "SELL", now + 1.0)
    engine.on_trade_update("YES", 0.49, 4.0, "SELL", now + 2.0)

    sizes = [f.fill_size for f in engine.fills_history]
    assert sizes == pytest.approx([4.0, 4.0, 2.0])
    assert engine.shares_yes == pytest.approx(10.0)
    assert "BUY_YES" not in engine.active_quotes


def test_dry_run_quote_requires_correct_taker_direction(tmp_path):
    engine, _, _ = make_engine(tmp_path)
    engine.cash = 100.0
    engine.shares_yes = 0.0
    engine.active_quotes["BUY_YES"] = quote(size=10.0, price=0.50, matched=0.0)
    engine._book_ts["YES"] = 0.0

    # A taker BUY printing through the bid must not fill our bid.
    engine.on_trade_update("YES", 0.40, 5.0, "BUY", time.time())

    assert engine.fills_history == []
    assert engine.shares_yes == 0.0


# ---------------------------------------------------------------------------
# Settlement routing: the engine must target the ledger that minted the
# position, not always CTF.
# ---------------------------------------------------------------------------

COND_V2 = "0x" + "11" * 31 + "00"
COND_NEGRISK = "0x" + "44" * 32


def _ready_live_merge(engine, ctf, monkeypatch, route, meta):
    go_live(engine)
    ctf.is_settlement_enabled = True
    engine._settlement_route_cache = (route, meta)
    engine.shares_yes = engine.shares_no = 5.0
    engine._last_merge_ts = 0.0
    monkeypatch.setattr(
        "polymarketbot.trading.engine.verify_route",
        lambda *args, **kwargs: (True, "ok"),
    )


def test_live_merge_dispatches_v2_to_router(tmp_path, monkeypatch):
    engine, _, ctf = make_engine(tmp_path, condition_id=COND_V2)
    _ready_live_merge(
        engine, ctf, monkeypatch, SettlementRoute.ROUTER_MERGE, {"version": "v2"}
    )
    try:
        res = engine.trigger_ctf_merge()
        assert res is not None and res.success
        assert ctf.merge_calls == []
        call = ctf.settle_calls[0]
        assert call["to_address"] == ROUTER
        assert call["calldata"].startswith("0x" + ROUTER_MERGE_SELECTOR)
        assert call["amount_shares"] == pytest.approx(5.0)
        assert engine.total_merges == 1
        assert engine.shares_yes == pytest.approx(0.0)
    finally:
        engine.close()


def test_live_merge_dispatches_v1_negrisk_to_adapter(tmp_path, monkeypatch):
    engine, _, ctf = make_engine(tmp_path, condition_id=COND_NEGRISK)
    _ready_live_merge(
        engine,
        ctf,
        monkeypatch,
        SettlementRoute.NEGRISK_MERGE,
        {
            "version": "v1",
            "neg_risk": True,
            "neg_risk_market_id": COND_NEGRISK,
        },
    )
    try:
        res = engine.trigger_ctf_merge()
        assert res is not None and res.success
        assert ctf.merge_calls == []
        call = ctf.settle_calls[0]
        assert call["to_address"] == NEGRISK_ADAPTER
        assert call["calldata"].startswith("0x" + NEGRISK_MERGE_SELECTOR)
    finally:
        engine.close()


def test_live_merge_refuses_unencodable_condition(tmp_path, monkeypatch):
    engine, _, ctf = make_engine(tmp_path)  # 0xCOND is not a valid bytes31 id
    _ready_live_merge(
        engine, ctf, monkeypatch, SettlementRoute.ROUTER_MERGE, {"version": "v2"}
    )
    try:
        assert engine.trigger_ctf_merge() is None
        assert ctf.settle_calls == []
        assert ctf.merge_calls == []
    finally:
        engine.close()
