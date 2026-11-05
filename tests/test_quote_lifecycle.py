"""Tests for quote lifecycle safety: tick-normalisation, reprice gating, and Q_min rewards."""

from __future__ import annotations

import pytest

from polymarketbot.trading.base import BaseMarketMakingEngine, PositionView


class _HarnessEngine(BaseMarketMakingEngine):
    """Minimal in-memory engine implementing the abstract execution hooks."""

    def __init__(self, tmp_path, *, cash: float = 1000.0, **kwargs) -> None:
        self.cash = float(cash)
        self.position = PositionView(condition_id="0xTEST")
        self.placed: list[tuple[str, float, float, float]] = []
        self.canceled: list[str] = []
        super().__init__(
            condition_id="0xTEST",
            db_path=tmp_path / "engine.db",
            ledger_path=tmp_path / "ledger.json",
            **kwargs,
        )

    def _set_quote(self, token_side, quote_side, price, size, ts) -> None:
        qid = f"{quote_side}_{token_side}".upper()
        self.placed.append((qid, price, size, ts))
        self.active_quotes[qid] = {
            "quote_id": qid,
            "token_side": token_side.upper(),
            "side": quote_side.upper(),
            "price": price,
            "size": size,
            "timestamp": ts,
            "status": "ACTIVE",
        }

    def _cancel_quote(self, qid: str) -> None:
        if qid in self.active_quotes:
            self.active_quotes.pop(qid)
            self.canceled.append(qid)

    def cancel_all_quotes(self) -> None:
        self.canceled.extend(self.active_quotes.keys())
        self.active_quotes.clear()

    def _execute_tier2_taker_deskew(self, decision, ts) -> None:
        return None

    def trigger_ctf_merge(self, amount_sets=None):
        return None

    def trigger_ctf_split(self, amount_usdc):
        return None

    def get_cash(self) -> float:
        return self.cash

    def get_position(self, condition_id=None) -> PositionView:
        return self.position

    def get_equity(self, prices=None) -> float:
        return self.cash


def _set_book(engine: _HarnessEngine, side: str, bid: float, ask: float) -> None:
    engine._best_bids[side] = bid
    engine._best_asks[side] = ask


@pytest.fixture
def engine(tmp_path):
    eng = _HarnessEngine(tmp_path)
    try:
        yield eng
    finally:
        eng.close()


# ---------------------------------------------------------------------------
# Reprice gate (S1)
# ---------------------------------------------------------------------------

def test_should_reprice_missing_quote_always_places(engine):
    assert engine.should_reprice(None, 0.50, 20.0, 100.0) is True


def test_should_reprice_interval_tick_and_size(engine):
    existing = {"price": 0.500, "size": 20.0, "timestamp": 100.0}

    # Inside the minimum interval: never reprice, even for a large move.
    assert engine.should_reprice(existing, 0.55, 30.0, 101.0) is False
    assert engine.should_reprice(existing, 0.55, 30.0, 101.9) is False

    # At/after the interval, a move within one tick and an unchanged size is ignored.
    assert engine.should_reprice(existing, 0.5005, 20.0, 102.0) is False
    assert engine.should_reprice(existing, 0.5005, 20.0 + 1e-7, 103.0) is False

    # A move beyond one tick needs a reprice.
    assert engine.should_reprice(existing, 0.502, 20.0, 103.0) is True

    # A material size change needs a reprice.
    assert engine.should_reprice(existing, 0.500, 20.5, 103.0) is True

    # Honour the configured tick size (1 cent market).
    engine.quote_tick_size = 0.01
    assert engine.should_reprice(existing, 0.505, 20.0, 103.0) is False
    assert engine.should_reprice(existing, 0.52, 20.0, 103.0) is True


def test_round_quote_price_rounds_to_tick_and_clamps(engine):
    assert engine.round_quote_price(0.4134, "YES") == pytest.approx(0.413)
    assert engine.round_quote_price(0.0, "YES") == pytest.approx(0.001)
    assert engine.round_quote_price(1.0, "YES") == pytest.approx(0.999)

    engine.quote_tick_size = 0.01
    assert engine.round_quote_price(0.4134, "YES") == pytest.approx(0.41)
    assert engine.round_quote_price(0.004, "YES") == pytest.approx(0.01)
    assert engine.round_quote_price(0.999, "YES") == pytest.approx(0.99)


def test_neutral_quotes_untouched_inside_reprice_interval(engine):
    _set_book(engine, "YES", 0.49, 0.51)
    _set_book(engine, "NO", 0.49, 0.51)

    engine._execute_neutral_maker_quoting(0.50, 0.49, 0.51, 1000.0)
    first = {qid: dict(q) for qid, q in engine.active_quotes.items()}
    placed_after_first = len(engine.placed)
    assert set(first) == {"BUY_YES", "BUY_NO"}

    # Same book half a second later: quotes must not be cancelled/replaced.
    engine._execute_neutral_maker_quoting(0.50, 0.49, 0.51, 1000.5)

    assert len(engine.placed) == placed_after_first
    assert engine.active_quotes == first
    assert engine.canceled == []


# ---------------------------------------------------------------------------
# Tick-aware quoting / no crossing (C13 base half)
# ---------------------------------------------------------------------------

def test_neutral_no_leg_never_crosses_no_book(engine):
    # The derived NO bid (= 1 - synthetic YES ask) would be 0.29 and cross the NO ask.
    _set_book(engine, "YES", 0.70, 0.71)
    _set_book(engine, "NO", 0.27, 0.28)

    engine._execute_neutral_maker_quoting(0.705, 0.70, 0.71, 1000.0)

    assert engine.round_quote_price(0.29, "NO") > 0.28
    no_quote = engine.active_quotes["BUY_NO"]
    tick = engine.quote_tick_size
    assert no_quote["price"] <= 0.28 - tick + 1e-9
    assert no_quote["price"] < 0.29

    yes_quote = engine.active_quotes["BUY_YES"]
    assert yes_quote["price"] <= 0.71 - tick + 1e-9


def test_sub_min_size_cash_suppresses_quotes_instead_of_quoting(engine):
    engine.cash = 5.0  # Cannot fund rewards_min_size=20 at a ~1.00 combined price.
    _set_book(engine, "YES", 0.49, 0.51)
    _set_book(engine, "NO", 0.49, 0.51)
    engine._set_quote("YES", "BUY", 0.499, 20.0, 990.0)
    engine._set_quote("NO", "BUY", 0.499, 20.0, 990.0)
    seeded_placements = len(engine.placed)

    engine._execute_neutral_maker_quoting(0.50, 0.49, 0.51, 1000.0)

    assert engine.active_quotes == {}
    assert len(engine.placed) == seeded_placements


# ---------------------------------------------------------------------------
# Official Q_min reward scoring (S5 / C8 reward part)
# ---------------------------------------------------------------------------

def test_compute_quote_scores_uses_q_min_for_one_sided_liquidity(engine):
    _set_book(engine, "YES", 0.49, 0.51)
    _set_book(engine, "NO", 0.49, 0.51)
    engine.rewards_max_spread = 1.0
    engine.rewards_min_size = 20.0
    engine.active_quotes = {
        "BUY_YES": {
            "token_side": "YES",
            "side": "BUY",
            "price": 0.499,
            "size": 100.0,
            "timestamp": 0.0,
        }
    }

    q_min, q_one, q_two = engine.compute_quote_scores()

    # spread = 0.1c, v = 1.0c, size = 100 -> ((1.0 - 0.1)/1.0)^2 * 100 = 81
    expected_q_one = ((1.0 - 0.1) / 1.0) ** 2 * 100.0
    assert q_one == pytest.approx(expected_q_one)
    assert q_two == pytest.approx(0.0)
    # Inside [0.10, 0.90] one-sided liquidity scores max(Q)/3, never Q1 + Q2.
    assert q_min == pytest.approx(expected_q_one / 3.0)
    assert q_min != pytest.approx(q_one + q_two)


def test_compute_quote_scores_requires_both_sides_outside_band(engine):
    _set_book(engine, "YES", 0.04, 0.06)
    _set_book(engine, "NO", 0.94, 0.96)
    engine.rewards_max_spread = 1.0
    engine.rewards_min_size = 20.0
    engine.active_quotes = {
        "BUY_YES": {
            "token_side": "YES",
            "side": "BUY",
            "price": 0.049,
            "size": 100.0,
            "timestamp": 0.0,
        }
    }

    q_min, q_one, q_two = engine.compute_quote_scores()

    assert q_one > 0.0
    assert q_two == pytest.approx(0.0)
    # Outside [0.10, 0.90] Q_min = min(Q_one, Q_two) == 0 for one-sided liquidity.
    assert q_min == 0.0


def test_daily_yield_does_not_double_single_sided_score(tmp_path):
    eng = _HarnessEngine(
        tmp_path,
        competing_score_override=5.0,
        daily_reward_rate=20.0,
        spread_offset_cents=0.5,
        rewards_max_spread_cents=1.0,
        quote_size=20.0,
    )
    try:
        expected_user_score = ((1.0 - 0.5) / 1.0) ** 2 * 20.0  # 5.0
        expected_yield = (expected_user_score / (expected_user_score + 5.0)) * 20.0
        assert expected_yield == pytest.approx(10.0)
        assert eng.get_current_daily_yield() == pytest.approx(expected_yield)
    finally:
        eng.close()


def test_reward_epoch_risk_pnl_excludes_modelled_rewards(engine):
    engine.position.realized_pnl = -3.0
    engine.total_rewards_credited = 123.45
    engine.governor.evaluate_governor_cycle = lambda **kwargs: []
    _set_book(engine, "YES", 0.49, 0.51)
    _set_book(engine, "NO", 0.49, 0.51)

    captured = []

    def fake_update(**kwargs):
        captured.append(kwargs)
        return None

    engine.risk_manager.update_portfolio_pnl = fake_update

    engine.process_reward_epoch(timestamp=1_700_000_000.0)

    assert captured
    assert captured[0]["realized_pnl"] == pytest.approx(-3.0)


def test_evaluate_quotes_risk_pnl_excludes_modelled_rewards(engine):
    engine.position.realized_pnl = -3.0
    engine.total_rewards_credited = 123.45
    _set_book(engine, "YES", 0.49, 0.51)
    _set_book(engine, "NO", 0.49, 0.51)

    captured = []

    def fake_update(**kwargs):
        captured.append(kwargs)
        return None

    engine.risk_manager.update_portfolio_pnl = fake_update

    engine.evaluate_quotes(timestamp=1000.0)

    assert captured
    assert captured[0]["realized_pnl"] == pytest.approx(-3.0)


def test_reward_epoch_index_aligns_to_wall_clock_and_does_not_extrapolate(engine):
    engine.governor.evaluate_governor_cycle = lambda **kwargs: []
    _set_book(engine, "YES", 0.49, 0.51)
    _set_book(engine, "NO", 0.49, 0.51)
    engine.active_quotes = {
        "BUY_YES": {
            "token_side": "YES",
            "side": "BUY",
            "price": 0.499,
            "size": 20.0,
            "timestamp": 1_699_999_999.0,
        }
    }

    ts = 1_700_000_000.0
    result = engine.process_reward_epoch(timestamp=ts)

    assert result["epoch_index"] == int(ts // 60) % 1440
    assert len(engine._epoch_samples) == 1
    # The displayed projection is the current run rate (one minute × 1440), while
    # the $1/day floor is judged on the accrued day, so one sample pays nothing.
    assert result["projected_daily_usd"] == pytest.approx(
        round(engine._epoch_samples[0] * 1440.0, 2)
    )
    assert result["floor_met"] is False
    assert result["epoch_payout_usd"] == 0.0
