"""The sizing cap and the quote sizing it feeds. Never cash."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from polymarketbot.logs import get_logger
from polymarketbot.live.runner_market import _coerce_float

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")


def apply_sizing_cap(ctx: "RunContext") -> Optional[int]:
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


def apply_quote_sizing(ctx: "RunContext") -> Optional[int]:
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
        )
    if ctx.quote_sz <= 0.0:
        log.critical(f"Derived quote size is {ctx.quote_sz}. Aborting rather than quoting zero.")
        return 1

    if ctx.args.spread_offset is not None:
        ctx.spread_off = float(ctx.args.spread_offset)
    else:
        ctx.spread_off, _ = ctx.deps.optimize_spread_offset(
            max_spread_cents=ctx.max_spr,
            market_spread_cents=ctx.market["market_spread_cents"],
            tick_size_cents=ctx.market.get("tick_size_cents", 1.0),
            daily_reward_pool=ctx.daily_rew,
            quote_size=ctx.quote_sz,
            category=ctx.market.get("category", "default"),
        )
    # Sizing telemetry for the dashboard (not engine state).
    ctx.market["quote_size"] = ctx.quote_sz
    ctx.market["spread_offset_cents"] = ctx.spread_off
    return None
