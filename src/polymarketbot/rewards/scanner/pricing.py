"""Measured yield pricing: the fill/markout tables and the capture arithmetic.

``calculate_expected_yield`` reads the measured horizon-fill probability and the
measured single-leg markout, and prices spread capture and adverse selection
from them. A cell the measurement does not cover is refused, never estimated.
"""

from __future__ import annotations

from typing import Any, Optional

from polymarketbot.logs import get_logger
from polymarketbot.rewards.scoring import (
    quadratic_order_score,
    single_sided_score_fraction,
)
from polymarketbot.rewards.tape_regime import MarketRegime

logger = get_logger("rewards_scanner")


def _yield_refusal(reason: str) -> dict[str, Any]:
    """A refusal record: no estimate, and the reason it does not exist.

    ``daily_reward_payout is None`` is the gate every caller tests, so a
    refused market can never be ranked or adopted on a stand-in number.
    """
    return {
        "maker_score": 0.0,
        "gross_reward_payout": 0.0,
        "daily_reward_payout": None,
        "expected_spread_capture": 0.0,
        "expected_adverse_selection": 0.0,
        "expected_gas_fees": 0.0,
        "net_expected_yield": 0.0,
        "net_yield_per_dollar": 0.0,
        "passed_payout_floor": False,
        "gas_measured": True,
        "unmeasured_reason": reason,
        "p_fill": None,
        "priced_horizon_s": None,
        "markout_usd_per_share": None,
        "quote_size_shares": None,
        "expected_fills_per_day": None,
    }


_FILL_HORIZON: Optional[Any] = None
_FILL_HORIZON_TRIED = False
_MAKER_MARKOUT: Optional[Any] = None
_MAKER_MARKOUT_TRIED = False

# One day, a unit definition: fills per day from a per-horizon probability.
_SECONDS_PER_DAY = 86_400.0


def _fill_horizon_table() -> Optional[Any]:
    """The measured horizon fill table, or ``None`` when it cannot be loaded.

    Same artifact and same loader the CTF maker gate prices with
    (``data/derived/ctf_quote_fill_horizon.json``, 8,773,815 tape placements),
    so the scanner and the harvester cannot disagree about how often a resting
    quote is filled.
    """
    global _FILL_HORIZON, _FILL_HORIZON_TRIED
    if not _FILL_HORIZON_TRIED:
        _FILL_HORIZON_TRIED = True
        try:
            from polymarketbot.paper.pillars.ctf import load_quote_fill_horizon

            _FILL_HORIZON = load_quote_fill_horizon()
        except Exception as exc:  # noqa: BLE001 - a missing measurement is data
            logger.warning(
                f"fill probability unavailable, yield not estimable: {exc}"
            )
            _FILL_HORIZON = None
    return _FILL_HORIZON


def _maker_markout_table() -> Optional[Any]:
    """The measured single-leg markout, or ``None`` when it cannot be loaded."""
    global _MAKER_MARKOUT, _MAKER_MARKOUT_TRIED
    if not _MAKER_MARKOUT_TRIED:
        _MAKER_MARKOUT_TRIED = True
        try:
            from polymarketbot.paper.pillars.ctf import load_maker_markout

            _MAKER_MARKOUT = load_maker_markout()
        except Exception as exc:  # noqa: BLE001 - a missing measurement is data
            logger.warning(
                f"signed markout unavailable, yield not estimable: {exc}"
            )
            _MAKER_MARKOUT = None
    return _MAKER_MARKOUT


def reset_measured_tables() -> None:
    """Drop the cached measurement tables (test isolation)."""
    global _FILL_HORIZON, _FILL_HORIZON_TRIED, _MAKER_MARKOUT, _MAKER_MARKOUT_TRIED
    _FILL_HORIZON = None
    _FILL_HORIZON_TRIED = False
    _MAKER_MARKOUT = None
    _MAKER_MARKOUT_TRIED = False


def calculate_expected_yield(
    daily_rate: float,
    max_spread_cents: float,
    min_size: float,
    *,
    allocated_capital_usd: float,
    tick_size: float,
    quote_horizon_s: float,
    spread_cents: Optional[float] = None,
    mid_price: Optional[float] = None,
    competing_score: Optional[float] = None,
    regime: Optional[MarketRegime] = None,
    merge_threshold: Optional[float] = None,
    gas_fee_per_merge: Optional[float] = None,
    quote_sides: int = 2,
) -> dict[str, Any]:
    """Net expected marginal yield on the capital **the caller** allocates.

    Y_net = reward payout + E[spread capture] - E[adverse selection] - gas

    with the venue's strict $1.00 daily payout floor (``Y_gross < $1.00 ->
    $0.00``).

    Capital
    -------
    ``allocated_capital_usd`` is required and there is no default: capital
    allocation belongs to ``allocator.py`` (its MILP plus the KKT shadow price),
    not to a scanner. The old ``bankroll_usd=300 * concentration_pct=0.75`` pair
    decided here that three quarters of a hard-coded bankroll went into every
    market independently, so the scanner "allocated" 75 % of the bankroll to
    every one of fifty markets at once. The caller now states the capital for
    *this* market; a missing or non-positive allocation is refused (logged),
    never replaced by a default.

    Quote size is ``max(min_size, allocated_capital / (quote_sides * mid))``:
    ``quote_sides`` legs each resting ``quote_size`` shares at the measured mid,
    and the venue's published ``min_size`` is the smallest order that scores at
    all.

    Measured microstructure
    -----------------------
    ``tick_size`` and ``quote_horizon_s`` (the lifetime a quote rests before it
    is replaced) select a cell of the measured fill table:
    ``data/derived/ctf_quote_fill_horizon.json``, built from 8,773,815 tape
    placements over the recorded L2 tape and keyed by ``(horizon, tick,
    required flow)``; the required flow is the venue's published ``min_size``,
    the smallest print that can fill our order. ``p_fill`` is that cell's
    measured ``p_fill_exact`` probability, read through
    ``QuoteFillHorizon.clamped_p_fill`` (the same call the harvester's gate
    makes). A cell the table does not cover is **missing data** and the market
    is refused: the old model instead invented ``touch_depth=1000`` and capped
    ``quote_size / (touch_depth + quote_size)`` at ``0.30``, i.e. it claimed a
    30 % fill share for every market regardless of its tick, flow or lifetime.

    ``regime`` is the market's own tape statistics
    (``polymarketbot.rewards.tape_regime``): prints per minute, mean print size,
    realised per-minute volatility and jump frequency, measured over the
    trailing 24 h of ``data/tape/clob_trades``. Nothing here reads a category
    table any more - the old ``CATEGORY_REGIMES`` priced every market in a
    category off the same hand-set ``vol_1m_bps``/``jump_prob_1h``/
    ``trade_rate_per_min`` with no source and no window. A market whose prints
    cannot identify its mean size is refused (the regime is ``None``).

    Per day: ``fills = p_fill * 86400 / priced_horizon``, and each fill takes
    ``min(quote_size, regime.mean_trade_size_shares)`` - the model never claims
    to sweep more shares in one fill than the market's measured average print.
    Spread capture is those shares times the measured quote distance ``s``.
    Adverse selection is those shares times the **measured** markout
    (``data/derived/ctf_maker_markout.json``, 44,068 measured fills, per-share
    best-bid drift over the same ``(horizon, tick)``) - replacing the invented
    ``jump_prob_1h * jump_mean_cents * decay_300s_ratio`` triple, which priced a
    jump loss from a table with no measurement behind it.

    Unchanged measured inputs
    -------------------------
    ``spread_cents`` (``s``) and ``mid_price`` have no defaults: the old
    0.5c/0.50 pair priced every caller as if the book were one tick wide around
    50 %.

    ``competing_score`` is the market's measured in-band competing depth summed
    over both outcome-token books; the payout share is
    ``our_score / (competing_score + our_score)``, so a missing denominator
    would either fabricate a floor or - worse - pay the whole pool.

    ``quote_sides`` states how many sides of the binary the plan rests on. The
    venue pays ``Q_min = min(Q_one, Q_two)`` to a two-sided maker and only
    ``1/c`` of its raw score (``c = 3``) to a one-sided one inside the
    [0.10, 0.90] midpoint band, nothing outside it.
    """
    if spread_cents is None or mid_price is None or competing_score is None or competing_score <= 0.0:
        reason = (
            "unmeasured: unmeasured quote distance, mid or competing score "
            f"(spread_cents={spread_cents!r}, mid_price={mid_price!r}, "
            f"competing_score={competing_score!r})"
        )
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)
    if allocated_capital_usd is None or float(allocated_capital_usd) <= 0.0:
        reason = (
            "unmeasured: caller allocated no capital "
            f"(allocated_capital_usd={allocated_capital_usd!r}); sizing belongs "
            "to allocator.py"
        )
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)
    v = max_spread_cents
    # The measured distance is used as measured: beyond the band the venue
    # scores nothing, which ``quadratic_order_score`` already returns.
    s = spread_cents
    if (
        v <= 0.0
        or daily_rate <= 0.0
        or not 0.0 < mid_price < 1.0
        or tick_size is None
        or float(tick_size) <= 0.0
        or quote_horizon_s is None
        or float(quote_horizon_s) <= 0.0
    ):
        reason = (
            "unmeasured: campaign or quote geometry not priceable "
            f"(max_spread={v!r}, daily_rate={daily_rate!r}, mid={mid_price!r}, "
            f"tick={tick_size!r}, horizon={quote_horizon_s!r})"
        )
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)

    # --- measured fill probability: the same cell the harvester's gate prices --
    fill_table = _fill_horizon_table()
    if fill_table is None:
        reason = "unmeasured: horizon fill measurement unavailable"
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)
    pinned = fill_table.clamped_p_fill(
        float(quote_horizon_s), float(tick_size), float(min_size)
    )
    if pinned is None:
        supported = fill_table.supported_horizons(float(tick_size), float(min_size))
        reason = (
            "unmeasured: no measured fill probability for "
            f"tick={float(tick_size):g}, required flow={float(min_size):g} shares, "
            f"horizon={float(quote_horizon_s):g}s "
            f"(measured horizons for this cell: {supported or 'none'})"
        )
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)
    p_fill, priced_horizon_s = float(pinned[0]), float(pinned[1])

    markout_table = _maker_markout_table()
    if markout_table is None:
        reason = "unmeasured: single-leg markout measurement unavailable"
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)
    markout_usd_per_share = markout_table.markout(priced_horizon_s, float(tick_size))
    if markout_usd_per_share is None:
        reason = (
            "unmeasured: no measured markout for "
            f"tick={float(tick_size):g} at horizon {priced_horizon_s:g}s "
            f"(measured: {markout_table.supported_horizons(float(tick_size)) or 'none'})"
        )
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)

    if regime is None:
        reason = "unmeasured: no tape regime for this market"
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)
    if regime.mean_trade_size_shares is None or regime.mean_trade_size_shares <= 0.0:
        reason = (
            "unmeasured: tape regime carries no mean print size "
            f"({regime.mean_trade_size_shares!r})"
        )
        logger.warning(f"yield not estimable: {reason}")
        return _yield_refusal(reason)

    # Capital committed to quoting this market: ``quote_sides`` legs, each
    # ``quote_size`` shares at the measured mid.
    allocated_capital = float(allocated_capital_usd)
    quote_size = max(min_size, allocated_capital / (float(quote_sides) * mid_price))

    # 1. Quadratic Maker Score, net of the venue's two-sided rule
    score_fraction = (
        1.0 if quote_sides >= 2 else single_sided_score_fraction(mid_price)
    )
    maker_score = quadratic_order_score(
        max_spread_cents=v,
        spread_cents=s,
        size=quote_size,
    ) * score_fraction

    # 2. Competing Liquidity Score (D_comp): the measured denominator, never a
    # ladder guess. ``competing_score`` was validated above.
    d_comp = float(competing_score)

    total_score = maker_score + d_comp
    reward_share = (maker_score / total_score) if total_score > 0.0 else 0.0

    # Gross payout
    gross_payout = reward_share * daily_rate

    # Strict $1.00 floor enforcement
    passed_floor = gross_payout >= 1.00
    daily_reward_payout = gross_payout if passed_floor else 0.0

    # 3. Expected Spread Capture E[Spread Capture]
    #
    # Fills per day is the measured arrival probability over the quote's own
    # lifetime, repeated: ``p_fill * 86400 / horizon``. ``p_fill`` is read at
    # the venue's published minimum order size - the smallest print that can
    # meet our resting order - so a market with no such flow in the measured
    # cells was refused above rather than given a fill share.
    fills_per_day = p_fill * (_SECONDS_PER_DAY / priced_horizon_s)
    # One fill cannot take more shares than the market's own measured average
    # print, and cannot exceed what we display.
    shares_per_fill = min(quote_size, regime.mean_trade_size_shares)
    spread_capture = fills_per_day * shares_per_fill * (s / 100.0)

    # 4. Expected Adverse Selection E[Adverse Selection]
    #
    # The measured per-share best-bid drift over the same (horizon, tick) cell,
    # applied to the shares each fill takes. No jump probability, no jump mean,
    # no decay ratio: those were category constants with no measurement.
    adverse_selection = fills_per_day * shares_per_fill * markout_usd_per_share

    logger.info(
        "measured yield inputs: capital=$%.2f quote_size=%.2f shares "
        "tick=%g horizon=%gs flow=%g shares p_fill=%.3e fills/day=%.4f "
        "shares/fill=%.2f markout=%.6f $/share spread=%.4fc "
        f"tape[prints={regime.trades} rate=%.3f/min mean_size=%.2f "
        "vol=%.1f bps/min jump_freq=%.4f window=%.1f h]"
        % (
            allocated_capital,
            quote_size,
            float(tick_size),
            priced_horizon_s,
            float(min_size),
            p_fill,
            fills_per_day,
            shares_per_fill,
            markout_usd_per_share,
            s,
            regime.trades_per_min,
            regime.mean_trade_size_shares,
            regime.realised_vol_bps_per_min
            if regime.realised_vol_bps_per_min is not None
            else float("nan"),
            regime.jump_freq,
            regime.window_hours,
        )
    )

    # 5. Expected Gas Fees (dynamic merge cost and optimal threshold)
    #
    # ``0.015`` used to double as a sentinel value and as the fallback when the
    # live gas query failed, so a rate-limited RPC silently priced every merge
    # at $0.015 - the exact failure this window logged (`eth_blockNumber rate
    # limited, cooldown 921s`). The sentinel is gone: ``None`` is the only "not
    # supplied" value, and an estimator that cannot be queried leaves gas
    # unmeasured rather than invented. A trade whose gas cost is unknown has no
    # net yield, so it is refused (``net_expected_yield`` is -inf, floor not
    # passed) instead of ranked on a fabricated number.
    gas_measured = True
    if gas_fee_per_merge is None:
        try:
            from polymarketbot.trading.ctf import estimate_merge_cost_usd
            eff_gas_fee = float(estimate_merge_cost_usd())
        except Exception as exc:  # noqa: BLE001 - an unmeasured gas price is a refusal
            logger.warning(f"gas not measurable, refusing yield estimate: {exc}")
            eff_gas_fee = None
            gas_measured = False
    else:
        eff_gas_fee = float(gas_fee_per_merge)

    if eff_gas_fee is None:
        eff_merge_threshold = None
    elif merge_threshold is None:
        try:
            from polymarketbot.trading.ctf import calculate_optimal_merge_threshold
            eff_merge_threshold = float(calculate_optimal_merge_threshold(gas_cost_usd=eff_gas_fee))
        except Exception:  # noqa: BLE001 - no threshold without a measured gas cost
            eff_merge_threshold = None
            gas_measured = False
    else:
        eff_merge_threshold = float(merge_threshold)

    filled_shares_daily = fills_per_day * shares_per_fill
    if gas_measured and eff_merge_threshold is not None:
        expected_merges = filled_shares_daily / max(1.0, eff_merge_threshold)
        gas_fees = expected_merges * eff_gas_fee
    else:
        gas_fees = float("nan")

    # Net Yield
    if gas_measured:
        net_yield = daily_reward_payout + spread_capture - adverse_selection - gas_fees
        net_yield_per_dollar = net_yield / allocated_capital
    else:
        net_yield = float("-inf")
        net_yield_per_dollar = float("-inf")

    return {
        "maker_score": maker_score,
        "gross_reward_payout": gross_payout,
        "daily_reward_payout": daily_reward_payout,
        "expected_spread_capture": spread_capture,
        "expected_adverse_selection": adverse_selection,
        "expected_gas_fees": gas_fees,
        "net_expected_yield": net_yield,
        "net_yield_per_dollar": net_yield_per_dollar,
        "passed_payout_floor": passed_floor and gas_measured,
        "gas_measured": gas_measured,
        "unmeasured_reason": None,
        "p_fill": p_fill,
        "priced_horizon_s": priced_horizon_s,
        "markout_usd_per_share": markout_usd_per_share,
        "quote_size_shares": quote_size,
        "expected_fills_per_day": fills_per_day,
    }
