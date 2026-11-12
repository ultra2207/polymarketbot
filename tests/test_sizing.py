"""Unit tests for dynamic quote sizing and the measured tick-depth spread policy."""

from __future__ import annotations

import inspect
import json
import math
from types import SimpleNamespace

import pytest

from polymarketbot.strategy.sizing import (
    FillPolicyRow,
    FillPolicyTable,
    SizingInputsUnmeasured,
    calculate_dynamic_cycles_budget,
    calibrate_cycles_budget,
    derive_quote_size,
    load_fill_policy_table,
    optimize_spread_offset,
    spread_policy_table,
)

# Every input the spread optimizer prices with, as a measured caller would
# supply them. The fill table is a deliberately shaped measurement: fills get
# rarer and the drift worse as the quote rests deeper.
BASE = {
    "max_spread_cents": 3.5,
    "tick_size_cents": 1.0,
    "daily_reward_pool": 400.0,
    "quote_size": 50.0,
    "competing_score": 5_000.0,
    "mid_price": 0.50,
    "capital_usd": 3_000.0,
    "maker_fee_usd_per_share": 0.0,
    "trades_per_min": 2.0,
}

# The fabricated defaults issue #2 removes: none may be reachable implicitly.
# ``adverse_markout_cents`` moved into the measured depth table, so it is no
# longer a scalar input at all.
FORMERLY_FABRICATED = (
    "competing_score",
    "mid_price",
    "trades_per_min",
)

HORIZON_S = 400.0


def _row(
    ticks: int,
    p_fill: float,
    markout_cents: float,
    *,
    trials: int = 4_000,
    markout_fills: int | None = None,
    p_fill_stderr: float | None = None,
    markout_stderr_cents: float | None = None,
) -> FillPolicyRow:
    """One measured depth row, with binomial / sampling errors as measured."""
    stderr = (
        p_fill_stderr
        if p_fill_stderr is not None
        else math.sqrt(max(0.0, p_fill * (1.0 - p_fill)) / trials)
    )
    fills = markout_fills if markout_fills is not None else max(1, round(p_fill * trials))
    markout_se = (
        markout_stderr_cents
        if markout_stderr_cents is not None
        else (abs(markout_cents) / math.sqrt(fills) if fills else 0.0)
    )
    return FillPolicyRow(
        ticks=ticks,
        p_fill=p_fill,
        p_fill_stderr=stderr,
        p_fill_ci_low=max(0.0, p_fill - 2.0 * stderr),
        p_fill_ci_high=min(1.0, p_fill + 2.0 * stderr),
        adverse_markout_cents=markout_cents,
        adverse_markout_stderr_cents=markout_se,
        markout_fills=fills,
        trials=trials,
        entry_edge_cents=float(ticks),
    )


def _table(rows: list[FillPolicyRow], *, scope: str = "tick=0.01") -> FillPolicyTable:
    return FillPolicyTable(horizon_s=HORIZON_S, scope=scope, rows=tuple(rows))


def _base_table() -> FillPolicyTable:
    return _table(
        [
            _row(0, 0.0100, 0.5),
            _row(1, 0.0050, 0.5),
            _row(2, 0.0020, 1.0),
            _row(3, 0.0, 0.0, markout_fills=0),
        ]
    )


def _measured(**overrides) -> dict:
    payload = dict(BASE)
    payload["fill_rows"] = _base_table()
    payload.update(overrides)
    return payload


class TestDynamicCyclesBudgetCalibration:
    """Tests for calculate_dynamic_cycles_budget."""

    def test_cycles_budget_formula_in_bounds(self):
        # alloc_capital = 60.0, quote_size = 20.0, price = 0.50 -> denom = 2 * 20 * 0.50 = 20.0
        # budget = 60.0 / 20.0 = 3.0
        cb = calculate_dynamic_cycles_budget(alloc_capital=60.0, quote_size=20.0, price=0.50)
        assert cb == 3.0

    def test_cycles_budget_clamped_to_ceiling(self):
        # Abundant capital: 500 / 20 = 25.0 -> clamped to 5.0
        cb = calculate_dynamic_cycles_budget(alloc_capital=500.0, quote_size=20.0, price=0.50)
        assert cb == 5.0

    def test_cycles_budget_clamped_to_floor(self):
        # Constrained capital: 15 / 20 = 0.75 -> clamped to 1.5
        cb = calculate_dynamic_cycles_budget(alloc_capital=15.0, quote_size=20.0, price=0.50)
        assert cb == 1.5

    def test_cycles_budget_zero_and_negative_capital(self):
        assert calculate_dynamic_cycles_budget(alloc_capital=0.0, quote_size=20.0, price=0.50) == 1.5
        assert calculate_dynamic_cycles_budget(alloc_capital=-100.0, quote_size=20.0, price=0.50) == 1.5

    def test_cycles_budget_alias(self):
        assert calibrate_cycles_budget == calculate_dynamic_cycles_budget


class TestDynamicQuoteSizing:
    """Tests for derive_quote_size."""

    def test_derive_quote_size_default(self):
        # Bankroll $150, mid $0.74, min_size 20
        # Endogenous single-market reserve based dynamic cycles budget sizing
        size = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=20.0)
        # The reserve is now the measured fit (its jump quantile and gas
        # actions), so the affordable size is derived rather than fixed; it
        # must still respect the pool minimum and never exceed the bankroll.
        assert size >= 20.0
        assert size <= 150.0

        # Explicit override reserve_pct=0.25 and cycles_budget=2.5 yields legacy 45.0
        size_legacy = derive_quote_size(
            initial_cash=150.0, mid_price=0.74, min_size=20.0, reserve_pct=0.25, cycles_budget=2.5
        )
        assert size_legacy == 45.0

        # Endogenous single-market reserve with dynamic gas buffer and legacy cycles_budget=2.5
        size_dyn_legacy = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=20.0, cycles_budget=2.5)
        assert size_dyn_legacy == 57.0

        # The default reserve is the operator's 5% floor, so the affordable
        # size is larger than the old endogenous reserve implied.
        assert size_dyn_legacy >= 45.0

    def test_derive_quote_size_bumps_to_pool_min_size(self):
        # Bankroll $150, min_size 50
        size = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=50.0)
        assert size >= 50.0

        # With cycles_budget=3.0 and a 5% reserve, the budget is $142.5 / 3 =
        # $47.5, below the 50-share pool minimum, so the floor bumps it to 50.
        size_override = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=50.0, cycles_budget=3.0)
        assert size_override == 50.0

    def test_derive_quote_size_handles_oversized_pool_min(self):
        # Bankroll $100, min_size 200
        # Cannot afford 200 without leverage; caps to affordable size
        size = derive_quote_size(initial_cash=100.0, mid_price=0.75, min_size=200.0)
        assert 0.0 < size < 200.0
        # Collateral is $1 per complete set, so the affordable cap is the cash
        # left after the measured reserve divided by the binding side's price.
        assert size <= 100.0 / 0.75 + 1e-6

        size_default = derive_quote_size(initial_cash=100.0, mid_price=0.75, min_size=200.0)
        assert 0.0 < size_default <= 100.0 / 0.75 + 1e-6

        # With explicit 0.25 reserve: 75.0 / 0.75 = 100.0
        size_legacy = derive_quote_size(initial_cash=100.0, mid_price=0.75, min_size=200.0, reserve_pct=0.25)
        assert size_legacy == 100.0

    def test_derive_quote_size_holds_only_the_five_percent_floor(self):
        # $1,000, 5% reserve -> $950 deployable; the dynamic cycles budget is
        # 5, so the derived size is 950 / 5 = 190 shares, not a reserve-sized
        # fraction of the bankroll.
        size = derive_quote_size(initial_cash=1000.0, mid_price=0.50)
        assert size == 190.0

    def test_derive_quote_size_zero_cash_fallback(self):
        size = derive_quote_size(initial_cash=0.0, mid_price=0.50)
        assert size == 5.0


class TestSpreadPolicyInputs:
    """The optimizer refuses anything it was not given as a measurement."""

    def test_every_input_is_keyword_only_and_required(self):
        signature = inspect.signature(optimize_spread_offset)
        expected = (
            *FORMERLY_FABRICATED,
            "daily_reward_pool",
            "quote_size",
            "tick_size_cents",
            "capital_usd",
            "maker_fee_usd_per_share",
            "fill_rows",
        )
        for name in expected:
            parameter = signature.parameters[name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert parameter.default is inspect.Parameter.empty, f"{name} still has a default"

    def test_none_for_any_measured_input_is_refused(self):
        for name in (
            "competing_score",
            "mid_price",
            "trades_per_min",
            "daily_reward_pool",
            "quote_size",
            "capital_usd",
            "maker_fee_usd_per_share",
            "tick_size_cents",
            "max_spread_cents",
        ):
            with pytest.raises(SizingInputsUnmeasured, match=name):
                optimize_spread_offset(**_measured(**{name: None}))

    def test_refusal_is_a_value_error(self):
        assert issubclass(SizingInputsUnmeasured, ValueError)

    def test_omitted_measured_input_is_not_defaulted(self):
        # The old call site signature (only the market facts) is not priceable.
        with pytest.raises(TypeError):
            optimize_spread_offset(max_spread_cents=4.5, tick_size_cents=1.0)

        missing_fill_table = {k: v for k, v in _measured().items() if k != "fill_rows"}
        with pytest.raises(TypeError):
            optimize_spread_offset(**missing_fill_table)

    def test_impossible_measured_domains_are_refused(self):
        for name, bad in (
            ("competing_score", 0.0),
            ("competing_score", -5.0),
            ("mid_price", 0.0),
            ("mid_price", 1.0),
            ("trades_per_min", -0.5),
            ("quote_size", 0.0),
            ("capital_usd", 0.0),
            ("maker_fee_usd_per_share", -0.01),
            ("tick_size_cents", 0.0),
            ("max_spread_cents", 0.0),
            ("trades_per_min", float("nan")),
            ("capital_usd", float("inf")),
        ):
            with pytest.raises(SizingInputsUnmeasured):
                optimize_spread_offset(**_measured(**{name: bad}))

    def test_unmeasured_depth_is_refused(self):
        # A 4.5c band at a 1c tick needs depths 0..4; the table stops at 3.
        with pytest.raises(SizingInputsUnmeasured, match=r"depth\(s\) \[4\]"):
            optimize_spread_offset(**_measured(max_spread_cents=4.5))

    def test_missing_fill_table_is_refused(self):
        # ``load_fill_policy_table`` returns ``None`` when the artifact or the
        # market's scope is absent; the optimizer must refuse, not price a
        # stand-in fill probability.
        with pytest.raises(SizingInputsUnmeasured, match="fill policy table"):
            optimize_spread_offset(**_measured(fill_rows=None))

    def test_a_partial_depth_set_is_refused(self):
        hollow = _table([_row(0, 0.01, 0.5), _row(2, 0.002, 1.0), _row(3, 0.0, 0.0)])
        with pytest.raises(SizingInputsUnmeasured, match=r"depth\(s\) \[1\]"):
            optimize_spread_offset(**_measured(fill_rows=hollow))

    def test_unplaceable_band_is_refused(self):
        # A 0.005 mid cannot hold any bid on a 1c grid: the quote would be off-book.
        with pytest.raises(SizingInputsUnmeasured, match="no tradeable bid"):
            optimize_spread_offset(**_measured(mid_price=0.005, tick_size_cents=1.0))


class TestSpreadPolicyTable:
    """The priced table: quantized depths, the objective's three terms, errors."""

    def _table_rows(self, **overrides) -> list[dict]:
        args = _measured(**overrides)
        return spread_policy_table(**args)

    def test_depths_are_quantized_to_the_tick_grid(self):
        rows = self._table_rows()
        assert [row["k_ticks"] for row in rows] == [0, 1, 2, 3]
        for row in rows:
            price = row["price"]
            assert abs(price * 100.0 - round(price * 100.0)) < 1e-9, "price off the 1c grid"
            assert row["s_cents"] == pytest.approx((BASE["mid_price"] - price) * 100.0)
            assert row["s_cents"] <= BASE["max_spread_cents"]

    def test_net_is_reward_minus_adverse_minus_fees(self):
        for row in self._table_rows():
            assert row["net_daily"] == pytest.approx(
                row["reward_daily"] - row["adverse_daily"] - row["fees_daily"], abs=1e-6
            )
            assert row["net_per_capital_per_day"] == pytest.approx(
                row["net_daily"] / BASE["capital_usd"], abs=1e-9
            )

    def test_reward_is_the_quadratic_score_share_of_the_pool(self):
        row = self._table_rows()[1]  # k = 1 tick = 1.0c from mid
        ratio = (3.5 - row["s_cents"]) / 3.5
        score = ratio**2 * BASE["quote_size"]
        expected = 400.0 * score / (score + BASE["competing_score"])
        assert row["reward_daily"] == pytest.approx(expected, abs=1e-6)

    def test_fill_rate_is_the_measured_probability_scaled_to_the_day(self):
        rows = self._table_rows()
        assert rows[0]["fills_per_day"] == pytest.approx(rows[0]["p_fill"] * 86_400.0 / HORIZON_S)
        assert rows[0]["adverse_daily"] == pytest.approx(
            rows[0]["fills_per_day"] * BASE["quote_size"] * rows[0]["adverse_markout_cents"] / 100.0
        )

    def test_daily_payout_floor_zeroes_a_sub_dollar_reward(self):
        # A deep quote earns a share of the pool below the venue's $1/day floor.
        rows = self._table_rows()
        assert rows[2]["reward_unfloored_daily"] < 1.0
        assert rows[2]["reward_daily"] == 0.0

    def test_print_rate_caps_the_fill_rate(self):
        rows = self._table_rows(trades_per_min=0.0001)
        row = rows[0]
        assert row["fills_capped_by_prints"] is True
        assert row["fills_per_day"] == pytest.approx(0.0001 * 1_440.0)
        # The capped fill rate contributes no binomial error; the measured
        # markout's sampling error is still carried.
        expected = (
            row["fills_per_day"]
            * BASE["quote_size"]
            * _base_table().row(0).adverse_markout_stderr_cents
            / 100.0
        )
        assert row["net_stderr_daily"] == pytest.approx(expected, abs=1e-6)

    def test_net_carries_the_measurement_error(self):
        row = self._table_rows()[0]
        assert row["net_stderr_daily"] > 0.0
        # A zero-fill depth has no sampling noise to propagate.
        assert self._table_rows()[3]["net_stderr_daily"] == 0.0

    def test_fees_are_charged_per_filled_share(self):
        free = self._table_rows(maker_fee_usd_per_share=0.0)[0]
        charged = self._table_rows(maker_fee_usd_per_share=0.002)[0]
        assert charged["fees_daily"] == pytest.approx(
            charged["fills_per_day"] * BASE["quote_size"] * 0.002
        )
        assert charged["net_daily"] == pytest.approx(free["net_daily"] - charged["fees_daily"])


class TestSpreadPolicyChoice:
    """The argmax, its plateau rule and the reported margins."""

    def test_argmax_is_the_highest_net_depth(self):
        # The deepest measured drifts make the tight quote lose to the second.
        table = _table(
            [
                _row(0, 0.0200, 1.0),
                _row(1, 0.0040, 0.2),
                _row(2, 0.0, 0.0, markout_fills=0),
                _row(3, 0.0, 0.0, markout_fills=0),
            ]
        )
        offset, meta = optimize_spread_offset(**_measured(fill_rows=table))
        assert meta["k_ticks"] == 1
        assert offset == pytest.approx(1.0)
        assert meta["net_down_1"] == pytest.approx(meta["table"][0]["net_daily"])
        assert meta["net_up_1"] == pytest.approx(meta["table"][2]["net_daily"])
        assert meta["net_utility"] == pytest.approx(meta["table"][1]["net_daily"])

    def test_plateau_chooses_its_interior(self):
        # Depths 1..3 all earn nothing and fill never: a three-wide plateau.
        table = _table(
            [
                _row(0, 0.0100, 5.0),
                _row(1, 0.0, 0.0, markout_fills=0),
                _row(2, 0.0, 0.0, markout_fills=0),
                _row(3, 0.0, 0.0, markout_fills=0),
            ]
        )
        # A pool too thin to clear the venue's $1/day floor at any depth makes
        # 1..3 earn exactly nothing: a three-wide plateau with no fills.
        _, meta = optimize_spread_offset(
            **_measured(fill_rows=table, daily_reward_pool=10.0)
        )
        assert meta["plateau_ticks"] == [1, 2, 3]
        assert meta["k_ticks"] == 2
        assert meta["s_cents"] == pytest.approx(2.0)

    def test_a_measured_markout_widens_the_quote(self):
        cheap = _table([_row(k, 0.01, 0.0) for k in range(4)])
        dear = _table([_row(k, 0.01, 4.0) for k in range(4)])
        tight, _ = optimize_spread_offset(**_measured(fill_rows=cheap))
        wide, _ = optimize_spread_offset(**_measured(fill_rows=dear))
        assert wide >= tight

    def test_measured_fill_probability_is_reported_with_its_scope(self):
        _, meta = optimize_spread_offset(**_measured())
        assert meta["fill_scope"] == "tick=0.01"
        assert meta["fill_horizon_s"] == HORIZON_S
        assert meta["p_fill"] == pytest.approx(_base_table().row(meta["k_ticks"]).p_fill)
        assert meta["objective"].startswith("maximize reward accrual")

    def test_zero_measured_fill_probability_earns_the_reward_only(self):
        # Nothing fills anywhere: the reward is the whole objective and it is
        # maximal at the tightest quote that still qualifies.
        table = _table([_row(k, 0.0, 0.0, markout_fills=0) for k in range(4)])
        offset, meta = optimize_spread_offset(**_measured(fill_rows=table))
        assert offset == pytest.approx(0.0)
        assert meta["adverse_daily"] == 0.0
        assert meta["net_utility"] == pytest.approx(meta["reward_daily"])


class TestFillPolicyLoader:
    """Scope selection from the measured artifact, and its refusals."""

    def _write(self, tmp_path, payload: dict):
        path = tmp_path / "policy.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _payload(self) -> dict:
        def rows(p_fill: float) -> list[dict]:
            return [
                {
                    "ticks": k,
                    "p_fill": p_fill,
                    "p_fill_stderr": 0.001,
                    "p_fill_ci_low": max(0.0, p_fill - 0.002),
                    "p_fill_ci_high": p_fill + 0.002,
                    "adverse_markout_cents": 0.5,
                    "adverse_markout_stderr_cents": 0.1,
                    "markout_fills": 4,
                    "trials": 1_000,
                    "entry_edge_cents": float(k),
                }
                for k in range(4)
            ]

        return {
            "horizon_s": HORIZON_S,
            "partial": False,
            "scopes": [
                {"scope": "tick=0.01", "tick": 0.01, "max_spread_cents": None, "rows": rows(0.004)},
                {
                    "scope": "tick=0.01|band=3.5",
                    "tick": 0.01,
                    "max_spread_cents": 3.5,
                    "rows": rows(0.008),
                },
            ],
        }

    def test_exact_band_scope_wins(self, tmp_path):
        table = load_fill_policy_table(
            tick_size_cents=1.0,
            max_spread_cents=3.5,
            path=self._write(tmp_path, self._payload()),
        )
        assert table is not None
        assert table.scope == "tick=0.01|band=3.5"
        assert table.row(0).p_fill == pytest.approx(0.008)

    def test_pooled_tick_scope_is_the_measured_fallback(self, tmp_path):
        table = load_fill_policy_table(
            tick_size_cents=1.0,
            max_spread_cents=6.5,
            path=self._write(tmp_path, self._payload()),
        )
        assert table is not None
        assert table.scope == "tick=0.01"
        assert table.row(0).p_fill == pytest.approx(0.004)

    def test_unknown_tick_is_refused(self, tmp_path):
        assert (
            load_fill_policy_table(
                tick_size_cents=0.1,
                max_spread_cents=3.5,
                path=self._write(tmp_path, self._payload()),
            )
            is None
        )

    def test_missing_artifact_is_refused(self, tmp_path):
        assert (
            load_fill_policy_table(
                tick_size_cents=1.0,
                max_spread_cents=3.5,
                path=tmp_path / "absent.json",
            )
            is None
        )

    def test_missing_ticks_are_reported(self, tmp_path):
        payload = self._payload()
        payload["scopes"] = payload["scopes"][:1]
        payload["scopes"][0]["rows"] = payload["scopes"][0]["rows"][:2]
        table = load_fill_policy_table(
            tick_size_cents=1.0,
            max_spread_cents=3.5,
            path=self._write(tmp_path, payload),
        )
        assert table is not None
        assert table.missing_ticks(3) == [2, 3]


class TestSupervisorWiring:
    """The supervisor keeps the operator override and the measured refusals."""

    def _ctx(self, *, spread_offset, measured):
        from polymarketbot.live.supervisor.context import Deps, RunContext

        calls: list[dict] = []

        def fake_optimize(**kwargs):
            calls.append(kwargs)
            return 2.0, {"k_ticks": 2}

        deps = Deps(
            credentials_cls=object,
            clob_cls=object,
            ctf_cls=object,
            feed_cls=object,
            engine_cls=object,
            resolve_market_details=lambda **_: {},
            derive_quote_size=lambda **_: 50.0,
            optimize_spread_offset=fake_optimize,
            cancel_attempts=3,
            cancel_backoff=0.5,
        )
        args = SimpleNamespace(
            daily_reward=None,
            max_spread=None,
            min_size=None,
            competing_score=None,
            quote_size=None,
            spread_offset=spread_offset,
        )
        market = {
            "condition_id": "0xabc",
            "question": "q",
            "token_yes": "t1",
            "token_no": "t2",
            "mid_price": 0.50,
            "tick_size_cents": 1.0,
            "market_spread_cents": 1.0,
            "daily_reward": 400.0,
            "max_spread": 3.5,
            "min_size": 20.0,
        }
        ctx = RunContext(args=args, deps=deps, dry_run=True)
        ctx.market = market
        ctx.trading_bankroll = 3_000.0
        ctx.l2_session = None
        return ctx, calls, measured

    def test_operator_offset_skips_the_optimizer(self, monkeypatch):
        from polymarketbot.live.supervisor import sizing as supervisor_sizing

        ctx, calls, _ = self._ctx(spread_offset=2.5, measured=None)

        def explode(**_):
            raise AssertionError("the operator's own offset must not be re-priced")

        monkeypatch.setattr(supervisor_sizing, "measure_spread_inputs", explode)
        assert supervisor_sizing.apply_quote_sizing(ctx) is None
        assert ctx.spread_off == 2.5
        assert calls == []

    def test_unmeasured_inputs_refuse_to_size(self, monkeypatch):
        from polymarketbot.live.supervisor import sizing as supervisor_sizing

        ctx, calls, _ = self._ctx(spread_offset=None, measured=None)
        monkeypatch.setattr(
            supervisor_sizing, "measure_spread_inputs", lambda **_: None
        )
        assert supervisor_sizing.apply_quote_sizing(ctx) == 1
        assert calls == []

    def test_measured_inputs_reach_the_optimizer(self, monkeypatch):
        from polymarketbot.live.supervisor import sizing as supervisor_sizing

        ctx, calls, _ = self._ctx(spread_offset=None, measured=None)
        table = _base_table()
        monkeypatch.setattr(
            supervisor_sizing,
            "measure_spread_inputs",
            lambda **_: {
                "competing_score": 5_000.0,
                "mid_price": 0.50,
                "trades_per_min": 2.0,
                "capital_usd": 3_000.0,
                "maker_fee_usd_per_share": 0.0,
                "fill_rows": table,
            },
        )
        assert supervisor_sizing.apply_quote_sizing(ctx) is None
        assert len(calls) == 1
        assert calls[0]["fill_rows"] is table
        assert calls[0]["capital_usd"] == 3_000.0
        assert "market_spread_cents" not in calls[0]
        assert ctx.spread_off == 2.0
        assert ctx.spread_policy == {"k_ticks": 2}

    def test_published_maker_fee_is_a_measured_zero(self):
        from polymarketbot.live.supervisor.sizing import (
            measured_maker_fee_usd_per_share,
        )

        assert measured_maker_fee_usd_per_share() == 0.0
