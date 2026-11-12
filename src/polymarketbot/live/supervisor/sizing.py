"""The sizing cap and the quote sizing it feeds. Never cash."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import requests
import yaml

from polymarketbot import fees as fees_config
from polymarketbot.live.runner_market import _coerce_float
from polymarketbot.live.supervisor.markets import _compute_competing_score
from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.regime import measure_regimes
from polymarketbot.strategy.sizing import load_fill_policy_table

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")


def measured_maker_fee_usd_per_share() -> float | None:
    """Maker fee per share from the venue's published fee table, or ``None``.

    Polymarket charges makers nothing (``formula.maker_fee: 0.0`` in
    ``configs/fees.yaml``, and every per-market schedule is ``takerOnly``).
    The zero is therefore a *measured* published parameter rather than a
    default: an unreadable table returns ``None`` and the caller refuses to
    size, because a maker fee that cannot be read could be non-zero.
    """
    try:
        payload = yaml.safe_load(fees_config.CONFIG.read_text(encoding="utf-8"))
        value = (payload or {}).get("formula", {}).get("maker_fee")
    except (OSError, ValueError, AttributeError) as exc:
        log.warning(f"[SIZING] Published fee table unreadable ({exc}); refusing to size.")
        return None
    fee = _coerce_float(value)
    if fee is None or fee < 0.0:
        log.warning(
            f"[SIZING] Published fee table carries no maker fee ({value!r}); refusing to size."
        )
        return None
    return fee


def measure_spread_inputs(
    *,
    condition_id: str,
    token_yes: str,
    token_no: str | None = None,
    mid_price: float | None,
    max_spread_cents: float,
    tick_size_cents: float,
    competing_score: float | None,
    capital_usd: float,
    http_session: requests.Session | None,
) -> dict[str, Any] | None:
    """Measure every input the spread optimizer prices with, or return ``None``.

    Sources, all measured rather than assumed:

    * ``competing_score`` - the caller's measured L2 value, otherwise
      :func:`_compute_competing_score` over the live book.
    * ``trades_per_min`` - the market's tape regime
      (:func:`polymarketbot.strategy.reward.regime.measure_regimes`), a
      window-stamped measurement over the recorded prints. It bounds the fill
      rate: a quote cannot fill more often than the market prints.
    * ``fill_rows`` - ``data/derived/spread_policy_fill_by_ticks.json``, the
      FIFO-replayed P(fill) and adverse markout at **every integer depth** in
      the band, for this market's tick and reward band (falling back to the
      scope pooled across bands for the same tick, which reports its own
      error). A depth with no measured row refuses the market instead of being
      priced at an invented probability.
    * ``maker_fee_usd_per_share`` - the published maker fee (zero today).

    ``None`` means the market's data could not be measured; the caller must
    refuse to size instead of substituting a number.
    """
    cid = str(condition_id or "")
    mid = _coerce_float(mid_price)
    if not cid or mid is None or not 0.0 < mid < 1.0:
        log.warning(
            f"[SIZING] No measured mid for {cid or '<no condition id>'}; "
            "refusing to derive a quote distance."
        )
        return None
    capital = _coerce_float(capital_usd)
    if capital is None or capital <= 0.0:
        log.warning(f"[SIZING] No positive capital for {cid[:12]}; refusing to derive a quote distance.")
        return None

    competing = _coerce_float(competing_score)
    session = http_session
    owns_session = False
    if session is None and (competing is None or competing <= 0.0):
        session = requests.Session()
        owns_session = True
    try:
        if competing is None or competing <= 0.0:
            competing = _compute_competing_score(session, token_yes, max_spread_cents, token_no)
        if competing is None or competing <= 0.0:
            log.warning(
                f"[SIZING] The L2 book carries no measured competing score for {cid[:12]}; "
                "refusing to derive a quote distance."
            )
            return None

        try:
            measurement = measure_regimes({cid: float(max_spread_cents)})
        except Exception as exc:  # noqa: BLE001 - a failed tape read is missing data
            log.warning(
                f"[SIZING] Tape regime measurement failed for {cid[:12]}: {exc}. "
                "Refusing to derive a quote distance."
            )
            return None
        regime = measurement.regimes.get(cid)
        if regime is None:
            log.warning(
                f"[SIZING] {measurement.reason_for(cid)} for {cid[:12]}; "
                "refusing to derive a quote distance."
            )
            return None

        fill_rows = load_fill_policy_table(
            tick_size_cents=float(tick_size_cents),
            max_spread_cents=float(max_spread_cents),
        )
        if fill_rows is None:
            log.warning(
                f"[SIZING] No measured fill policy for tick {float(tick_size_cents):g}c "
                f"and band {float(max_spread_cents):g}c; refusing to derive a quote distance."
            )
            return None
        max_ticks = math.floor(float(max_spread_cents) / float(tick_size_cents) + 1e-9)
        missing = fill_rows.missing_ticks(max_ticks)
        if missing:
            log.warning(
                f"[SIZING] The measured fill policy ({fill_rows.scope}) has no P(fill) "
                f"for depth(s) {missing[:5]} of 0..{max_ticks}; refusing to derive a quote distance."
            )
            return None

        maker_fee = measured_maker_fee_usd_per_share()
        if maker_fee is None:
            return None
    finally:
        if owns_session:
            session.close()

    return {
        "competing_score": float(competing),
        "mid_price": float(mid),
        "trades_per_min": float(regime.trades_per_min),
        "capital_usd": float(capital),
        "maker_fee_usd_per_share": float(maker_fee),
        "fill_rows": fill_rows,
    }


def apply_sizing_cap(ctx: RunContext) -> int | None:
    """Turn the wallet and the budget flags into the one sizing limit."""
    # -- Sizing cap (never cash) --------------------------------------------
    raw_budget_pct = _coerce_float(ctx.args.budget_pct)
    if raw_budget_pct is None:
        log.critical(f"--budget-pct must be a finite number, got {ctx.args.budget_pct!r}. Aborting.")
        return 1
    if raw_budget_pct > 1.0:
        raw_budget_pct /= 100.0
    ctx.budget_pct = max(0.01, min(1.0, raw_budget_pct))
    ctx.max_budget = _coerce_float(ctx.args.max_budget) if ctx.args.max_budget is not None else None
    if ctx.args.max_budget is not None and ctx.max_budget is None:
        log.critical(f"--max-budget must be a finite number, got {ctx.args.max_budget!r}. Aborting.")
        return 1
    if ctx.max_budget is not None and ctx.max_budget <= 0.0:
        log.critical(f"--max-budget must be positive, got {ctx.max_budget:.2f}. Aborting.")
        return 1

    ctx.trading_bankroll = ctx.total_wallet * ctx.budget_pct
    if ctx.max_budget is not None:
        ctx.trading_bankroll = min(ctx.trading_bankroll, ctx.max_budget)
    ctx.trading_bankroll = round(max(0.0, ctx.trading_bankroll), 2)
    if ctx.trading_bankroll <= 0.0:
        log.critical(
            f"Sizing cap is ${ctx.trading_bankroll:.2f} from a ${ctx.total_wallet:.2f} wallet at "
            f"{ctx.budget_pct * 100.0:.1f}%. Nothing can be quoted; aborting."
        )
        return 1
    log.info(
        f"[SIZING CAP] ${ctx.trading_bankroll:.2f} = {ctx.budget_pct * 100.0:.1f}% of "
        f"${ctx.total_wallet:.2f} wallet"
        + (f", capped at --max-budget ${ctx.max_budget:.2f}" if ctx.max_budget is not None else "")
        + ". This is a sizing limit, not cash."
    )
    if ctx.dry_run:
        ctx.clob_client._mock_balance_usd = ctx.total_wallet
    return None


def matched_pair_quote_size(
    *,
    configured_size: float,
    mid_price: float,
    spread_offset_cents: float,
    tick_size: float,
    min_order_usd: float,
) -> tuple[float, float]:
    """Shares per leg for one notional-clearing matched pair, and its cost.

    The venue's ``orderMinSize`` is a USDC notional, not a share count, so the
    size that clears it on the expensive leg is sub-minimum on the cheap one.
    Both legs are quoted at a single share count: the one that clears the
    minimum on the cheap leg. Sizing the legs by equal dollars instead leaves
    an unhedged residual in the cheap leg — a directional bet, not a pair.

    Prices mirror the engine's quoting (``mid - offset`` on YES,
    ``1 - mid - offset`` on NO, rounded to the market tick), so the returned
    size is the one the engine will actually submit. The returned cost is the
    matched pair's notional, which the caller compares with the sizing cap.
    """
    tick = float(tick_size) if float(tick_size) > 0.0 else 0.01
    offset = max(0.0, float(spread_offset_cents)) / 100.0
    min_price = max(tick, 0.001)
    max_price = min(1.0 - tick, 0.999)

    def _round(price: float) -> float:
        rounded = round(round(price / tick) * tick, 6)
        return round(min(max(rounded, min_price), max_price), 6)

    yes_bid = _round(float(mid_price) - offset)
    no_bid = _round(1.0 - float(mid_price) - offset)
    # The engine clamps each bid to ``best_ask - tick`` (and rounds to the
    # tick), so a modeled bid can land one tick lower. Size against that floor:
    # under-sizing by a tick fails the venue's notional check on the cheap leg.
    cheap = max(min_price, min(yes_bid, no_bid) - tick)
    if min_order_usd > 0.0 and cheap > 0.0:
        required = math.ceil(float(min_order_usd) / cheap - 1e-9)
    else:
        required = 0
    size = max(float(configured_size), float(required))
    return size, size * (yes_bid + no_bid)


def apply_quote_sizing(ctx: RunContext) -> int | None:
    """Derive the quote size and spread offset, and record them for the dashboard."""
    ctx.daily_rew = ctx.args.daily_reward if ctx.args.daily_reward is not None else ctx.market["daily_reward"]
    ctx.max_spr = ctx.args.max_spread if ctx.args.max_spread is not None else ctx.market["max_spread"]
    ctx.min_sz = ctx.args.min_size if ctx.args.min_size is not None else ctx.market["min_size"]
    ctx.competing_override = ctx.args.competing_score is not None
    ctx.comp_score = ctx.args.competing_score if ctx.competing_override else ctx.market.get("competing_score_override")

    if ctx.args.quote_size is not None:
        ctx.quote_sz = float(ctx.args.quote_size)
    else:
        ctx.quote_sz = ctx.deps.derive_quote_size(
            initial_cash=ctx.trading_bankroll,
            mid_price=ctx.market["mid_price"],
            min_size=ctx.min_sz,
            order_min_usd=ctx.market.get("order_min_usd"),
        )
    if ctx.quote_sz <= 0.0:
        reason = ""
        if ctx.market.get("order_min_usd"):
            reason = (
                f": the ${float(ctx.market['order_min_usd']):.2f} per-order minimum "
                "cannot be funded by the sizing cap"
            )
        log.critical(
            f"Derived quote size is {ctx.quote_sz}{reason}. Aborting rather than "
            "quoting zero or a sub-minimum order."
        )
        return 1

    if ctx.args.spread_offset is not None:
        # An operator-stated distance is the required input, not a fallback.
        ctx.spread_off = float(ctx.args.spread_offset)
    else:
        measured = measure_spread_inputs(
            condition_id=ctx.market["condition_id"],
            token_yes=ctx.market["token_yes"],
            token_no=ctx.market.get("token_no"),
            mid_price=ctx.market["mid_price"],
            max_spread_cents=ctx.max_spr,
            tick_size_cents=ctx.market["tick_size_cents"],
            competing_score=ctx.comp_score,
            capital_usd=ctx.trading_bankroll,
            http_session=ctx.l2_session,
        )
        if measured is None:
            log.critical(
                "[SIZING] The spread offset cannot be derived from measured market "
                "inputs. Refusing to size; pass --spread-offset to state the distance "
                "explicitly."
            )
            return 1
        ctx.spread_off, ctx.spread_policy = ctx.deps.optimize_spread_offset(
            max_spread_cents=ctx.max_spr,
            tick_size_cents=ctx.market["tick_size_cents"],
            daily_reward_pool=ctx.daily_rew,
            quote_size=ctx.quote_sz,
            **measured,
        )
    # The venue's order minimum is a dollar notional, so the configured share
    # count may not be placeable on the cheap leg. Raise both legs to the one
    # count that clears it, and refuse rather than quote two unequal sizes.
    order_min_usd = float(ctx.market.get("order_min_usd") or 0.0)
    pair_size, pair_cost = matched_pair_quote_size(
        configured_size=ctx.quote_sz,
        mid_price=float(ctx.market["mid_price"]),
        spread_offset_cents=ctx.spread_off,
        tick_size=float(ctx.market.get("tick_size") or 0.01),
        min_order_usd=order_min_usd,
    )
    if pair_size > ctx.quote_sz + 1e-9:
        log.info(
            f"[SIZING] quote size raised {ctx.quote_sz:.2f} -> {pair_size:.0f} shares: "
            f"the venue's ${order_min_usd:.2f} order minimum is a notional, and the "
            "cheap leg needs this many shares to clear it."
        )
    ctx.quote_sz = pair_size
    if pair_cost > ctx.trading_bankroll + 1e-9:
        log.critical(
            f"[SIZING] One matched pair costs ${pair_cost:.2f} ({pair_size:.0f} shares "
            f"x ${(pair_cost / pair_size if pair_size > 0 else 0.0):.3f}), above the "
            f"${ctx.trading_bankroll:.2f} sizing cap. Refusing to quote the two legs "
            f"at different share counts; raise the cap to at least ${pair_cost:.2f}."
        )
        return 1
    # Sizing telemetry for the dashboard (not engine state).
    ctx.market["quote_size"] = ctx.quote_sz
    ctx.market["spread_offset_cents"] = ctx.spread_off
    return None


def measure_crossing_friction_pct(
    *,
    condition_id: str,
    token_yes: str,
    http_session: requests.Session | None,
    gamma: Any | None = None,
) -> float | None:
    """Measured cost of crossing into a candidate market, as a fraction of capital.

    Two measured components:

    * half the candidate's spread at the touch, relative to its mid
      (``(ask - mid) / mid``) — what a marketable buy pays above mid;
    * the candidate's own taker fee at that price, from its published fee
      schedule (``schedule_for_market``), relative to the notional crossed.

    Returns ``None`` when either input is missing, and says which one: an
    unmeasured crossing cost makes the switching cost infinite, which is the
    correct refusal (the old hand-set 25 bps priced crossings as cheaper than
    the venue's own curve on a 1¢ spread).
    """
    if not token_yes:
        log.warning("[MARKET SWITCH] Crossing friction unmeasured: candidate has no YES token")
        return None

    book = None
    feed = getattr(gamma, "feed", None) if gamma is not None else None
    if feed is None:
        # No injected feed: fall back to the runner's live REST reader.
        try:
            from polymarketbot.strategy.venue.feed import LivePolymarketFeed

            feed = LivePolymarketFeed()
        except Exception as exc:  # noqa: BLE001 - a missing book is a refusal
            log.warning(f"[MARKET SWITCH] Crossing friction unmeasured: no book reader ({exc})")
            return None
    try:
        book = feed.get_book_sync(token_yes)
    except Exception as exc:  # noqa: BLE001 - a missing book is a refusal
        log.warning(f"[MARKET SWITCH] Crossing friction unmeasured: book read failed ({exc})")
        return None
    if book is None:
        log.warning(
            f"[MARKET SWITCH] Crossing friction unmeasured: no book for candidate token "
            f"{token_yes[:12]}"
        )
        return None
    bid = float(getattr(book, "best_bid", 0.0) or 0.0)
    ask = float(getattr(book, "best_ask", 1.0) or 1.0)
    if not (0.0 < bid < ask < 1.0):
        log.warning(
            f"[MARKET SWITCH] Crossing friction unmeasured: candidate book at the touch is "
            f"not two-sided (bid={bid}, ask={ask})"
        )
        return None
    mid = (bid + ask) / 2.0
    half_spread_pct = (ask - mid) / mid

    try:
        from polymarketbot.fees import (
            FeeScheduleUnreadable,
            schedule_for_market,
            taker_fee,
        )
        from polymarketbot.gamma import GammaClient

        row = (gamma or GammaClient()).market(condition_id)
        if not row:
            log.warning(
                f"[MARKET SWITCH] Crossing friction unmeasured: Gamma published no market "
                f"row for {condition_id[:12]}"
            )
            return None
        schedule = schedule_for_market(row)
    except FeeScheduleUnreadable as exc:
        log.warning(f"[MARKET SWITCH] Crossing friction unmeasured: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - an unreadable schedule is a refusal
        log.warning(f"[MARKET SWITCH] Crossing friction unmeasured: fee schedule read failed ({exc})")
        return None

    # One share crossed at the ask: fee is charged on the notional (price).
    fee_usd = float(taker_fee(1.0, ask, schedule))
    fee_pct = fee_usd / ask if ask > 0.0 else 0.0
    friction = half_spread_pct + fee_pct
    log.info(
        f"[MARKET SWITCH] Crossing friction for {condition_id[:12]}: "
        f"half-spread {half_spread_pct * 100:.3f}% + taker fee {fee_pct * 100:.3f}% "
        f"= {friction * 100:.3f}% (bid {bid:.4f} ask {ask:.4f}, rate {schedule.rate})"
    )
    return friction
