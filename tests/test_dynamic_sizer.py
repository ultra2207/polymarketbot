"""Tests for the live quote-distance sizer that feeds measured inputs.

The live caller owns the measurements: the L2 book's competing score, the
market's tape regime, the measured depth-indexed fill policy and the published
maker fee. When any of them cannot be measured the caller must refuse to size -
never price the quote on a stand-in.
"""

from __future__ import annotations

from types import SimpleNamespace

from polymarketbot.live.supervisor import sizing as sizing_mod
from polymarketbot.strategy.sizing import FillPolicyRow, FillPolicyTable

CID = "0xmarketcondition"


class _FakeMeasurement:
    def __init__(self, regimes: dict, reason: str = "unmeasured: no prints") -> None:
        self.regimes = regimes
        self._reason = reason

    def reason_for(self, condition_id: str) -> str:
        return self._reason


def _fake_policy(horizon_s: float = 400.0, depths: int = 5) -> FillPolicyTable:
    return FillPolicyTable(
        horizon_s=horizon_s,
        scope="tick=0.01|band=4.5",
        rows=tuple(
            FillPolicyRow(
                ticks=k,
                p_fill=0.004 / (k + 1),
                p_fill_stderr=0.001,
                p_fill_ci_low=0.0,
                p_fill_ci_high=0.01,
                adverse_markout_cents=0.5,
                adverse_markout_stderr_cents=0.2,
                markout_fills=4,
                trials=1_000,
            )
            for k in range(depths)
        ),
    )


def _install_tables(monkeypatch) -> None:
    monkeypatch.setattr(sizing_mod, "load_fill_policy_table", lambda **_: _fake_policy())
    monkeypatch.setattr(sizing_mod, "measured_maker_fee_usd_per_share", lambda: 0.0)
    monkeypatch.setattr(
        sizing_mod,
        "measure_regimes",
        lambda bands: _FakeMeasurement({CID: SimpleNamespace(trades_per_min=2.0)}),
    )


def _measure(**overrides):
    kwargs = {
        "condition_id": CID,
        "token_yes": "111",
        "mid_price": 0.50,
        "max_spread_cents": 4.5,
        "tick_size_cents": 1.0,
        "competing_score": 1000.0,
        "capital_usd": 150.0,
        "http_session": None,
    }
    kwargs.update(overrides)
    return sizing_mod.measure_spread_inputs(**kwargs)


def test_measured_inputs_are_all_provenanced(monkeypatch):
    _install_tables(monkeypatch)

    measured = _measure()

    assert measured is not None
    assert measured["competing_score"] == 1000.0
    assert measured["mid_price"] == 0.50
    assert measured["trades_per_min"] == 2.0
    assert measured["capital_usd"] == 150.0
    # The venue publishes a zero maker fee; it is passed as a measured value.
    assert measured["maker_fee_usd_per_share"] == 0.0
    # The depth-indexed fill policy travels with its own measured provenance.
    assert measured["fill_rows"].scope == "tick=0.01|band=4.5"
    assert measured["fill_rows"].missing_ticks(4) == []


def test_competing_score_is_measured_from_the_book_when_absent(monkeypatch):
    _install_tables(monkeypatch)
    monkeypatch.setattr(sizing_mod, "_compute_competing_score", lambda session, token_yes, band, token_no: 250.0)

    measured = _measure(competing_score=None, http_session=SimpleNamespace(close=lambda: None))

    assert measured is not None
    assert measured["competing_score"] == 250.0


def test_refuses_without_a_measured_mid(monkeypatch):
    _install_tables(monkeypatch)
    assert _measure(mid_price=None) is None


def test_refuses_without_a_measured_competing_score(monkeypatch):
    _install_tables(monkeypatch)
    monkeypatch.setattr(sizing_mod, "_compute_competing_score", lambda session, token_yes, band, token_no: None)

    assert _measure(competing_score=None) is None


def test_refuses_without_a_measured_regime(monkeypatch):
    _install_tables(monkeypatch)
    monkeypatch.setattr(
        sizing_mod,
        "measure_regimes",
        lambda bands: _FakeMeasurement({}, reason="unmeasured: no prints in the tape window"),
    )

    assert _measure() is None


def test_refuses_when_tape_measurement_fails(monkeypatch):
    _install_tables(monkeypatch)

    def _boom(bands):
        raise OSError("tape unreadable")

    monkeypatch.setattr(sizing_mod, "measure_regimes", _boom)

    assert _measure() is None


def test_refuses_without_a_measured_fill_policy(monkeypatch):
    _install_tables(monkeypatch)
    monkeypatch.setattr(sizing_mod, "load_fill_policy_table", lambda **_: None)

    assert _measure() is None


def test_refuses_when_the_tick_is_not_measured(monkeypatch):
    _install_tables(monkeypatch)
    monkeypatch.setattr(
        sizing_mod,
        "load_fill_policy_table",
        lambda **_: FillPolicyTable(horizon_s=400.0, scope="tick=0.001", rows=()),
    )

    assert _measure() is None


def test_refuses_when_a_band_depth_is_unmeasured(monkeypatch):
    _install_tables(monkeypatch)
    # A 4.5c band at a 1c tick needs depths 0..4; this table stops at 3.
    monkeypatch.setattr(
        sizing_mod, "load_fill_policy_table", lambda **_: _fake_policy(depths=3)
    )

    assert _measure() is None


def test_refuses_without_a_measured_maker_fee(monkeypatch):
    _install_tables(monkeypatch)
    monkeypatch.setattr(sizing_mod, "measured_maker_fee_usd_per_share", lambda: None)

    assert _measure() is None


def test_refuses_without_capital(monkeypatch):
    _install_tables(monkeypatch)

    assert _measure(capital_usd=0.0) is None


def _ctx(**overrides):
    args = SimpleNamespace(
        daily_reward=None,
        max_spread=None,
        min_size=None,
        competing_score=None,
        quote_size=None,
        spread_offset=None,
    )
    market = {
        "condition_id": CID,
        "token_yes": "111",
        "mid_price": 0.50,
        "market_spread_cents": 1.0,
        "tick_size_cents": 1.0,
        "daily_reward": 25.0,
        "max_spread": 4.5,
        "min_size": 20.0,
        "competing_score_override": 900.0,
    }
    calls: dict = {}

    def _optimize(**kwargs):
        calls.update(kwargs)
        return 1.0, {}

    deps = SimpleNamespace(
        derive_quote_size=lambda **kwargs: 45.0,
        optimize_spread_offset=_optimize,
    )
    ctx = SimpleNamespace(
        args=args,
        market=market,
        deps=deps,
        trading_bankroll=150.0,
        l2_session=None,
        comp_score=900.0,
        quote_sz=0.0,
        spread_off=0.0,
        daily_rew=0.0,
        max_spr=0.0,
        min_sz=0.0,
        competing_override=False,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx, calls


def test_apply_quote_sizing_passes_every_measured_input(monkeypatch):
    table = _fake_policy()
    measured = {
        "competing_score": 900.0,
        "mid_price": 0.50,
        "trades_per_min": 2.0,
        "capital_usd": 150.0,
        "maker_fee_usd_per_share": 0.0,
        "fill_rows": table,
    }
    seen: dict = {}

    def _measure(**kwargs):
        seen.update(kwargs)
        return measured

    monkeypatch.setattr(sizing_mod, "measure_spread_inputs", _measure)
    ctx, calls = _ctx()

    assert sizing_mod.apply_quote_sizing(ctx) is None

    # The caller measured from the market it is quoting, not from a default.
    assert seen["condition_id"] == CID
    assert seen["mid_price"] == 0.50
    assert seen["competing_score"] == 900.0
    assert seen["tick_size_cents"] == 1.0
    assert seen["capital_usd"] == 150.0
    # Every measured value reached the optimizer; no fabricated `category`.
    for key, value in measured.items():
        assert calls[key] == value
    assert "category" not in calls
    assert "market_spread_cents" not in calls
    assert ctx.spread_off == 1.0


def test_apply_quote_sizing_refuses_when_measurement_is_missing(monkeypatch):
    monkeypatch.setattr(sizing_mod, "measure_spread_inputs", lambda **kwargs: None)
    ctx, calls = _ctx()

    assert sizing_mod.apply_quote_sizing(ctx) == 1
    assert calls == {}  # the optimizer was never asked to price a stand-in


def test_operator_stated_offset_bypasses_measurement(monkeypatch):
    def _must_not_measure(**kwargs):
        raise AssertionError("measurement must not run when the caller stated the distance")

    monkeypatch.setattr(sizing_mod, "measure_spread_inputs", _must_not_measure)
    ctx, calls = _ctx()
    ctx.args.spread_offset = 1.25

    assert sizing_mod.apply_quote_sizing(ctx) is None
    assert ctx.spread_off == 1.25
    assert calls == {}
