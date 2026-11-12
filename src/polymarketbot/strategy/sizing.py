"""Dynamic Quote Sizing and Optimal Spread Optimizer for Polymarket CLOB.

1. Dynamic Quote Sizing:
   - Derives safe maker quote size directly from initial cash bankroll.
   - Preserves a liquid reserve buffer for synthetic de-skew crossing and CTF
     gas fees (the measured endogenous reserve).
   - Respects Polymarket rewards minimum size threshold (min_size) so quotes
     qualify for reward pools.
2. Optimal Spread, in whole ticks:
   - Maximizes the net USD of the quote per unit capital per unit time over the
     integer half-spread ``k`` in ``[0, floor(max_spread / tick)]``::

         net(k) = reward_accrual(k)
                  - P(fill | k) * E[adverse markout | fill, k]
                  - fees

     with ``reward_accrual(k) = pool * S(k) / (S(k) + competing)`` and the
     venue's quadratic order score ``S(k) = ((v - s_k) / v)^2 * size`` at the
     distance ``s_k`` that the tick grid actually realises.
   - ``P(fill | k)`` and ``E[adverse markout | fill, k]`` are **measured by a
     FIFO replay over the L2 tape at every integer depth**
     (``data/derived/spread_policy_fill_by_ticks.json``, built by
     ``python -m polymarketbot.tools.fit_spread_policy``): a resting bid's
     position is reconstructed from the displayed size ahead of it, every later
     change to that level and every print at or through it, so neither is a
     fitted decay constant. Each row carries its binomial / sampling error.
   - The $1.00 minimum payout is used only to compare a candidate day's rate
     while choosing the depth; the per-UTC-day accrual and its rollover floor
     live in the reward accounting, never here.
   - Every input is measured or supplied by the caller; nothing is defaulted.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.allocator import MIN_RESERVE_PCT
from polymarketbot.strategy.reward.marginal import PROTOCOL_MIN_DAILY_PAYOUT_USD

logger = get_logger("strategy.sizing")


class SizingInputsUnmeasured(ValueError):
    """A quote distance was requested without every measured input.

    The spread we rest at is the strategy, so each input to it is either
    measured on the market's own book/tape or supplied by the caller. There is
    no stand-in value: a caller that cannot state one is refused here rather
    than priced at an invented number.
    """


def _measured_input(name: str, value: float | None) -> float:
    """Coerce one required measured input, refusing ``None`` and non-finite values."""
    if value is None:
        raise SizingInputsUnmeasured(
            f"{name} was not supplied: the quote distance is priced from measured "
            "market/tape inputs, and this one was missing. Refusing to size."
        )
    try:
        measured = float(value)
    except (TypeError, ValueError) as exc:
        raise SizingInputsUnmeasured(f"{name} is not a number: {value!r}") from exc
    if not math.isfinite(measured):
        raise SizingInputsUnmeasured(f"{name} is not finite: {value!r}")
    return measured


# One day, a unit definition: measured per-minute rates are scaled to per-day.
MINUTES_PER_DAY = 1_440.0
SECONDS_PER_DAY = 86_400.0

# src/polymarketbot/strategy/sizing.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FILL_POLICY_PATH = _REPO_ROOT / "data" / "derived" / "spread_policy_fill_by_ticks.json"


def calculate_dynamic_cycles_budget(
    alloc_capital: float,
    quote_size: float,
    price: float,
) -> float:
    """Scale cycles budget dynamically based on allocated capital, quote size, and price.

    Formula:
        cycles_budget = max(1.5, min(5.0, alloc_capital / (2.0 * quote_size * price)))

    Args:
        alloc_capital: Available or allocated capital in USD.
        quote_size: Maker quote size in shares.
        price: Price of the asset (bounded in [0.01, 0.99]).

    Returns:
        Calibrated cycles budget bounded in [1.5, 5.0].
    """
    if alloc_capital <= 0.0:
        return 1.5

    p = max(0.01, min(0.99, float(price)))
    qs = max(1.0, float(quote_size))
    denom = 2.0 * qs * p
    if denom <= 0.0:
        return 1.5

    raw_budget = float(alloc_capital) / denom
    return float(max(1.5, min(5.0, raw_budget)))


calibrate_cycles_budget = calculate_dynamic_cycles_budget


def derive_quote_size(
    initial_cash: float,
    mid_price: float,
    min_size: float = 0.0,
    reserve_pct: float | None = None,
    cycles_budget: float | None = None,
    quote_size: float | None = None,
    default_min_size: float = 5.0,
    order_min_usd: float | None = None,
) -> float:
    """Derive maker quote size dynamically from bankroll and price.

    Args:
        initial_cash: Total initial bankroll in USD (e.g. $150.00).
        mid_price: Current mid-price of the primary token (e.g. 0.74).
        min_size: Polymarket rewards minimum size requirement (e.g. 20.0 or 50.0).
        reserve_pct: Fraction of capital held liquid. ``None`` applies the
            operator's 5% minimum reserve; the allocator decides above it.
        cycles_budget: Number of unhedged fill cycles to withstand without cashout.
            If None, calibrated dynamically via calculate_dynamic_cycles_budget.
        quote_size: Optional target quote size in shares used for dynamic cycles budget calibration.
        default_min_size: Minimum quote size floor in shares when min_size is not specified (default 5.0).
        order_min_usd: The venue's published ``orderMinSize``, a USDC notional
            per order. The optimizer treats it as a hard constraint: a sub-minimum
            order is rejected, so the returned size is at least
            ``ceil(order_min_usd / cheap_leg_price)`` and zero when the bankroll
            cannot fund that matched pair.

    Returns:
        Safe quote size in shares (float, 1 decimal place).
    """
    if initial_cash <= 0.0:
        return min_size if min_size > 0.0 else default_min_size

    p_yes = max(0.01, min(0.99, mid_price))
    p_no = 1.0 - p_yes
    worst_side_price = max(p_yes, p_no)

    # Available capital after the operator's minimum liquid reserve. Above
    # the 5% floor the allocator decides how much to deploy.
    if reserve_pct is None:
        avail_cash = initial_cash * (1.0 - MIN_RESERVE_PCT)
    else:
        avail_cash = initial_cash * max(0.10, 1.0 - reserve_pct)

    # Collateral for 2-sided quoting ~ $1.00 per share set
    # Dividing by cycles_budget prevents depleting bankroll on first asymmetric fill
    if cycles_budget is None:
        target_size = quote_size if quote_size is not None else (min_size if min_size > 0.0 else 50.0)
        cycles_budget = calculate_dynamic_cycles_budget(
            alloc_capital=avail_cash,
            quote_size=target_size,
            price=worst_side_price,
        )
    else:
        cycles_budget = max(1.0, float(cycles_budget))

    budget_size = avail_cash / max(1.0, cycles_budget)

    # The venue's order minimum is a USDC notional per order, and both legs
    # quote the same share count. A matched pair at that count costs
    # q*(p_yes+p_no); if the bankroll cannot fund it, no order is placeable
    # and the optimizer returns zero rather than a sub-minimum size.
    if order_min_usd is not None and order_min_usd > 0.0:
        cheap = min(p_yes, p_no)
        if cheap <= 0.0:
            return 0.0
        q_floor = max(min_size, float(math.ceil(order_min_usd / cheap)))
        pair_cost = q_floor * (p_yes + p_no)
        if avail_cash + 1e-9 < pair_cost:
            return 0.0
        return round(max(q_floor, min(budget_size, avail_cash / (p_yes + p_no))), 1)

    floor_size = min_size if min_size > 0.0 else default_min_size

    if min_size > 0.0:
        worst_side_cost = min_size * worst_side_price
        # If available cash comfortably covers min_size
        if avail_cash >= worst_side_cost:
            return round(max(min_size, budget_size), 1)
        else:
            # Bankroll is constrained: scale down to maximum safe affordable size
            affordable = avail_cash / worst_side_price
            return round(max(1.0, affordable), 1)

    return round(max(floor_size, budget_size), 1)


# ---------------------------------------------------------------------------
# Measured depth-indexed fill policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillPolicyRow:
    """One measured depth: P(fill) and the adverse markout of a fill there.

    ``ticks`` is the integer distance from the mid, ``p_fill`` the share of
    FIFO-replayed placements filled within ``FillPolicyTable.horizon_s`` and
    ``adverse_markout_cents`` the measured ``mid(at fill) - mid(at fill +
    horizon)`` in cents per share (positive = the drift a buy pays). The errors
    are the measurement's own: binomial for the proportion, ``sd / sqrt(fills)``
    for the markout.
    """

    ticks: int
    p_fill: float
    p_fill_stderr: float
    p_fill_ci_low: float
    p_fill_ci_high: float
    adverse_markout_cents: float
    adverse_markout_stderr_cents: float
    markout_fills: int
    trials: int
    entry_edge_cents: float = 0.0


@dataclass(frozen=True)
class FillPolicyTable:
    """The measured P(fill) / markout surface, keyed by depth in ticks."""

    horizon_s: float
    scope: str
    rows: tuple[FillPolicyRow, ...]
    partial: bool = False

    def row(self, ticks: int) -> FillPolicyRow | None:
        for row in self.rows:
            if row.ticks == int(ticks):
                return row
        return None

    def missing_ticks(self, max_ticks: int) -> list[int]:
        """Depths in ``0..max_ticks`` this table does not carry."""
        present = {row.ticks for row in self.rows}
        return [k for k in range(int(max_ticks) + 1) if k not in present]


def _scope_tick(scope: dict[str, Any]) -> float | None:
    value = scope.get("tick")
    return float(value) if value is not None else None


def load_fill_policy_table(
    *,
    tick_size_cents: float,
    max_spread_cents: float,
    path: Path | None = None,
) -> FillPolicyTable | None:
    """Load the measured fill/markout rows for this ``(tick, band)`` market.

    Selection, in order: the scope measured on the market's own band, then the
    scope pooled across bands for its tick, then ``None``. A pooled row is a
    measured quantity over more markets (with its own error), never an invented
    decay: a market whose tick was never measured is refused by the caller.

    ``None`` also means the artifact is absent or unusable; the caller must
    refuse to size rather than substitute a probability.
    """
    model_path = Path(path) if path is not None else DEFAULT_FILL_POLICY_PATH
    try:
        payload = json.loads(model_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"Fill policy table {model_path} unreadable: {exc}")
        return None
    try:
        horizon = float(payload["horizon_s"])
    except (KeyError, TypeError, ValueError):
        logger.warning(f"Fill policy table {model_path} carries no fill horizon")
        return None
    tick = float(tick_size_cents) / 100.0
    band = float(max_spread_cents)
    scopes = [scope for scope in payload.get("scopes") or [] if isinstance(scope, dict)]
    exact = [
        scope
        for scope in scopes
        if _scope_tick(scope) is not None
        and abs(float(_scope_tick(scope)) - tick) < 1e-12
        and scope.get("max_spread_cents") is not None
        and abs(float(scope["max_spread_cents"]) - band) < 1e-9
    ]
    pooled = [
        scope
        for scope in scopes
        if _scope_tick(scope) is not None
        and abs(float(_scope_tick(scope)) - tick) < 1e-12
        and scope.get("max_spread_cents") is None
    ]
    for scope in (*exact, *pooled):
        rows = []
        for row in scope.get("rows") or []:
            try:
                rows.append(
                    FillPolicyRow(
                        ticks=int(row["ticks"]),
                        p_fill=float(row["p_fill"]),
                        p_fill_stderr=float(row.get("p_fill_stderr") or 0.0),
                        p_fill_ci_low=float(row.get("p_fill_ci_low") or 0.0),
                        p_fill_ci_high=float(row.get("p_fill_ci_high") or 0.0),
                        adverse_markout_cents=float(row.get("adverse_markout_cents") or 0.0),
                        adverse_markout_stderr_cents=float(
                            row.get("adverse_markout_stderr_cents") or 0.0
                        ),
                        markout_fills=int(row.get("markout_fills") or 0),
                        trials=int(row.get("trials") or 0),
                        entry_edge_cents=float(row.get("entry_edge_cents") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not rows:
            continue
        return FillPolicyTable(
            horizon_s=horizon,
            scope=str(scope.get("scope") or ""),
            rows=tuple(sorted(rows, key=lambda row: row.ticks)),
            partial=bool(payload.get("partial")),
        )
    return None


def _grid_price(mid_price: float, ticks: int, tick: float) -> float:
    """Venue-grid bid price ``ticks`` below the mid (floored, never above the mid).

    The epsilon absorbs binary division such as ``0.505 / 0.001`` evaluating to
    ``504.9999...``; flooring is deliberate because a bid may not cross the mid.
    """
    requested = float(mid_price) - float(ticks) * tick
    return round(math.floor(requested / tick + 1e-6) * tick, 6)


def _placeable_ticks(mid_price: float, max_spread_cents: float, tick_cents: float) -> int:
    """Deepest integer depth the band and the price domain both allow."""
    tick = tick_cents / 100.0
    band_ticks = math.floor(max_spread_cents / tick_cents + 1e-9)
    # mid - k*tick must stay on a tradeable grid point (>= one tick).
    price_ticks = math.floor((mid_price - tick) / tick + 1e-9)
    return int(min(band_ticks, price_ticks))


def spread_policy_table(
    *,
    max_spread_cents: float,
    tick_size_cents: float,
    daily_reward_pool: float,
    quote_size: float,
    competing_score: float,
    mid_price: float,
    capital_usd: float,
    maker_fee_usd_per_share: float,
    trades_per_min: float,
    fill_rows: FillPolicyTable,
    min_daily_payout_usd: float = PROTOCOL_MIN_DAILY_PAYOUT_USD,
) -> list[dict[str, Any]]:
    """Price every integer tick depth in the reward band; the optimization table.

    ``net`` is the caller's stated objective in USD per day::

        reward_accrual - P(fill) * quote_size * markout - P(fill) * quote_size * fee

    where the fill rate is ``P(fill)`` per ``fill_horizon_s`` placement scaled
    to the day and bounded by the market's own measured print rate (a quote can
    fill at most once per print). ``net_per_capital_per_day`` divides by the
    capital the caller states it deploys in this market.

    ``adverse_markout_stderr`` prices the sampling error of both measured
    factors into ``net_stderr``; it is the error of the *measurement*, not of
    the model.
    """
    v = _measured_input("max_spread_cents", max_spread_cents)
    tick_cents = _measured_input("tick_size_cents", tick_size_cents)
    reward_pool = _measured_input("daily_reward_pool", daily_reward_pool)
    size = _measured_input("quote_size", quote_size)
    competing = _measured_input("competing_score", competing_score)
    mid = _measured_input("mid_price", mid_price)
    capital = _measured_input("capital_usd", capital_usd)
    fee = _measured_input("maker_fee_usd_per_share", maker_fee_usd_per_share)
    prints_per_min = _measured_input("trades_per_min", trades_per_min)
    if fill_rows is None:
        raise SizingInputsUnmeasured(
            "no measured fill policy table was supplied: P(fill) and the adverse "
            "markout are only defined by data/derived/spread_policy_fill_by_ticks.json"
        )
    horizon = _measured_input("fill_horizon_s", fill_rows.horizon_s)

    if tick_cents <= 0.0:
        raise SizingInputsUnmeasured(f"tick_size_cents must be positive, got {tick_size_cents!r}")
    if competing <= 0.0:
        raise SizingInputsUnmeasured(
            f"competing_score must be positive, got {competing_score!r}: a measured "
            "zero cannot be told apart from an unmeasured book and would pay the "
            "whole pool on any size."
        )
    if not 0.0 < mid < 1.0:
        raise SizingInputsUnmeasured(f"mid_price must be inside (0, 1), got {mid_price!r}")
    if size <= 0.0:
        raise SizingInputsUnmeasured(f"quote_size must be positive, got {quote_size!r}")
    if capital <= 0.0:
        raise SizingInputsUnmeasured(f"capital_usd must be positive, got {capital_usd!r}")
    if fee < 0.0:
        raise SizingInputsUnmeasured(f"maker_fee_usd_per_share must be non-negative, got {fee!r}")
    if horizon <= 0.0:
        raise SizingInputsUnmeasured(
            f"fill_horizon_s must be positive, got {fill_rows.horizon_s!r}"
        )
    if prints_per_min < 0.0:
        raise SizingInputsUnmeasured(
            f"trades_per_min must be non-negative, got {trades_per_min!r}"
        )
    if v <= 0.0:
        raise SizingInputsUnmeasured(f"max_spread_cents must be positive, got {max_spread_cents!r}")

    max_ticks = _placeable_ticks(mid, v, tick_cents)
    if max_ticks < 0:
        raise SizingInputsUnmeasured(
            f"no tradeable bid exists inside a {v:g}c band at mid {mid:g} with a "
            f"{tick_cents:g}c tick"
        )
    missing = fill_rows.missing_ticks(max_ticks)
    if missing:
        raise SizingInputsUnmeasured(
            f"the measured fill policy ({fill_rows.scope or 'no scope'}) carries no "
            f"P(fill) for depth(s) {missing[:5]}{'...' if len(missing) > 5 else ''} "
            f"of 0..{max_ticks}; refusing to price those depths from a stand-in."
        )

    tick = tick_cents / 100.0
    prints_per_day = max(0.0, prints_per_min) * MINUTES_PER_DAY
    rows: list[dict[str, Any]] = []
    for k in range(max_ticks + 1):
        measured = fill_rows.row(k)
        if measured is None:  # guarded by ``missing_ticks`` above; never priced blind
            raise SizingInputsUnmeasured(
                f"the measured fill policy carries no row for depth {k}"
            )
        price = _grid_price(mid, k, tick)
        s_cents = max(0.0, (mid - price) * 100.0)

        ratio = max(0.0, (v - s_cents) / v)
        score = (ratio**2) * size
        if score > 0.0:
            gross = reward_pool * (score / (score + competing))
            reward = gross if gross >= min_daily_payout_usd else 0.0
        else:
            gross = 0.0
            reward = 0.0

        # Fill rate per day, bounded by the market's own measured print rate:
        # one print can fill a resting quote at most once.
        raw_fills_per_day = measured.p_fill * (SECONDS_PER_DAY / horizon)
        fills_per_day = min(raw_fills_per_day, prints_per_day) if prints_per_day > 0.0 else 0.0
        capped = fills_per_day + 1e-12 < raw_fills_per_day

        markout_usd = measured.adverse_markout_cents / 100.0
        adverse = fills_per_day * size * markout_usd
        fees = fills_per_day * size * fee
        net = reward - adverse - fees

        se_fills = measured.p_fill_stderr * (SECONDS_PER_DAY / horizon)
        if capped:
            se_fills = 0.0
        stderr_markout_usd = measured.adverse_markout_stderr_cents / 100.0
        net_stderr = size * math.sqrt(
            (se_fills * markout_usd) ** 2 + (fills_per_day * stderr_markout_usd) ** 2
        )

        rows.append(
            {
                "k_ticks": k,
                "price": price,
                "s_cents": round(s_cents, 6),
                "p_fill": measured.p_fill,
                "p_fill_stderr": measured.p_fill_stderr,
                "p_fill_ci": [measured.p_fill_ci_low, measured.p_fill_ci_high],
                "fills_per_day": round(fills_per_day, 6),
                "fills_capped_by_prints": capped,
                "reward_daily": round(reward, 6),
                "reward_unfloored_daily": round(gross, 6),
                "adverse_markout_cents": measured.adverse_markout_cents,
                "adverse_daily": round(adverse, 6),
                "fees_daily": round(fees, 6),
                "net_daily": round(net, 6),
                "net_per_capital_per_day": round(net / capital, 9),
                "net_stderr_daily": round(net_stderr, 6),
                "fill_trials": measured.trials,
                "fill_markout_fills": measured.markout_fills,
                "entry_edge_cents": measured.entry_edge_cents,
            }
        )
    return rows


def _choose_depth(rows: list[dict[str, Any]], tolerance: float = 1e-9) -> tuple[int, list[int]]:
    """Argmax depth, taking the interior of a plateau of numerically tied depths.

    A plateau is a set of depths whose nets tie to ``tolerance``: the reward
    scores sit on a tick grid and a measured zero fill probability can be
    identical at several depths, so the tie is real rather than noise. The
    interior is the rounded midpoint of the plateau, which avoids resting at a
    plateau edge (the shallowest edge fills more cheaply, the deepest edge earns
    less reward).
    """
    best = max(row["net_daily"] for row in rows)
    plateau = [int(row["k_ticks"]) for row in rows if best - row["net_daily"] <= tolerance]
    interior = math.floor((plateau[0] + plateau[-1]) / 2.0 + 0.5)
    return interior, plateau


def optimize_spread_offset(
    *,
    max_spread_cents: float,
    tick_size_cents: float,
    daily_reward_pool: float,
    quote_size: float,
    competing_score: float,
    mid_price: float,
    capital_usd: float,
    maker_fee_usd_per_share: float,
    trades_per_min: float,
    fill_rows: FillPolicyTable,
) -> tuple[float, dict[str, Any]]:
    """Pick the half-spread to rest at, in whole ticks, from measured inputs.

    Maximizes the net USD per unit capital per unit time over the integer depth
    ``k`` in ``[0, floor(max_spread_cents / tick_size_cents)]``::

        net(k) = reward_accrual(k) - P(fill|k) * size * E[adverse markout|fill,k] - fees

    Every argument is keyword-only and required, and every one is a
    measurement:

    Args:
        max_spread_cents: Qualifying spread threshold (v) in cents, from the market.
        tick_size_cents: Resolved minimum price tick in cents (e.g. 1.0c or 0.1c).
        daily_reward_pool: Daily reward budget in USD for this market.
        quote_size: Order size in shares.
        competing_score: Measured in-band competing score for the market, summed
            over both outcome-token books. Must be positive.
        mid_price: Measured mid-price of the token, in (0, 1); it fixes the
            tradeable price grid and therefore the realised distance ``s_k``.
        capital_usd: Capital deployed in this market, USD. The objective is
            divided by it to give a rate, so markets can be compared.
        maker_fee_usd_per_share: Measured maker fee per share (the venue charges
            makers nothing today, which is a measured zero, not a default).
        trades_per_min: Measured print rate; it bounds the fill rate (a quote
            cannot fill more often than the market prints).
        fill_rows: The measured depth-indexed fill/markout table
            (:func:`load_fill_policy_table`). ``None`` (no artifact or no
            matching scope) is refused: every depth is priced from it.

    Returns:
        (achieved_half_spread_cents, telemetry). The offset is a multiple of the
        tick below the mid, so it is already quantized to what the book can
        quote.

    Raises:
        SizingInputsUnmeasured: when an input is absent, non-numeric,
            non-finite, outside its measured domain, the fill table is missing,
            or it does not cover the band's depths.
    """
    rows = spread_policy_table(
        max_spread_cents=max_spread_cents,
        tick_size_cents=tick_size_cents,
        daily_reward_pool=daily_reward_pool,
        quote_size=quote_size,
        competing_score=competing_score,
        mid_price=mid_price,
        capital_usd=capital_usd,
        maker_fee_usd_per_share=maker_fee_usd_per_share,
        trades_per_min=trades_per_min,
        fill_rows=fill_rows,
    )
    best_k, plateau = _choose_depth(rows)
    chosen = rows[best_k]
    by_depth = {int(row["k_ticks"]): row for row in rows}
    lower = by_depth.get(best_k - 1)
    upper = by_depth.get(best_k + 1)

    telemetry: dict[str, Any] = {
        "k_ticks": best_k,
        "s_cents": chosen["s_cents"],
        "price": chosen["price"],
        "p_fill": chosen["p_fill"],
        "p_fill_stderr": chosen["p_fill_stderr"],
        "fills_per_day": chosen["fills_per_day"],
        "reward_daily": chosen["reward_daily"],
        "adverse_daily": chosen["adverse_daily"],
        "fees_daily": chosen["fees_daily"],
        "net_utility": chosen["net_daily"],
        "net_per_capital_per_day": chosen["net_per_capital_per_day"],
        "net_stderr_daily": chosen["net_stderr_daily"],
        "plateau_ticks": plateau,
        "net_down_1": lower["net_daily"] if lower else None,
        "net_up_1": upper["net_daily"] if upper else None,
        "max_ticks": rows[-1]["k_ticks"],
        "tick_size_cents": float(tick_size_cents),
        "max_spread_cents": float(max_spread_cents),
        "capital_usd": float(capital_usd),
        "maker_fee_usd_per_share": float(maker_fee_usd_per_share),
        "competing_score": float(competing_score),
        "fill_scope": fill_rows.scope,
        "fill_horizon_s": fill_rows.horizon_s,
        "fill_table_partial": fill_rows.partial,
        "table": rows,
        "objective": (
            "maximize reward accrual - P(fill|k) * size * E[adverse markout|fill,k] "
            "- fees, per USD of capital per day, over integer ticks from the mid"
        ),
    }
    mounted = " | ".join(
        f"k={row['k_ticks']}: net=${row['net_daily']:.4f}/d" for row in rows
    )
    logger.info(
        f"Spread policy: k*={best_k} tick(s) = {chosen['s_cents']:.2f}c from mid "
        f"(band {max_spread_cents:g}c, tick {tick_size_cents:g}c, "
        f"competing score {competing_score:.1f}) -> "
        f"net ${chosen['net_daily']:.4f}/d, reward ${chosen['reward_daily']:.4f}/d, "
        f"adverse ${chosen['adverse_daily']:.4f}/d, P(fill)={chosen['p_fill']:.5f}. {mounted}"
    )
    return chosen["s_cents"], telemetry
