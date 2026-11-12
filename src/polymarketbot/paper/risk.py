"""The paper engine's hard risk gates: the live ``RiskManager``, measured inputs.

Live runs every quote through one shared risk authority: ``RiskConfig``
thresholds, the daily-loss and max-drawdown breakers, the per-asset price-jump
cooldown, the kill switch, and the position / notional / order caps in
``RiskManager.validate_quote``. The paper engine owns a ledger instead of a
venue, so this module feeds that same manager the paper account's *measured*
state each tick and turns its decisions into quote cancellation and refusal.
Nothing here re-implements a threshold: every number is
``RiskConfig``'s (the same defaults live's real-money engine constructs
from) or read from the ledger and the book snapshot.

Configuration source
--------------------
``build_manager`` passes only the two fields paper has a different source for:
the bankroll (the paper ledger's capital) and long-only mode (the live
real-money engine is long-only, and the paper ledger only ever sells inventory
it owns). The breakers - 5 % daily loss, 10 % peak drawdown, 2 c jump over
60 s, 300 s cooldown, 15 s heartbeat - are ``RiskConfig``'s own defaults; the
live engine spells the same derived values out in its constructor. Paper does
not override ``max_quote_size_shares``: live's aggregate engine sets it from a
measured sizer quote size, and the paper planner has no equivalent separate
measurement, so the shared default applies. The correlated ``event_id`` caps
cannot be enforced because the paper ``Market`` payload carries no event id.

What this layer deliberately does not do
----------------------------------------
* Heartbeat timeout: the paper feed is a REST snapshot fetched inside the tick,
  not a long-lived WebSocket. A dead data path shows up as a missing/stale book
  and is handled per asset below; tripping a *permanent* disconnect halt on one
  slow snapshot would be less faithful than the stale-book block.
* Microstructure gating: ``RiskManager.set_micro_risk`` is not wired. Paper
  has no per-order flow/queue feed into ``UnifiedRiskEngine``; an unfitted or
  unwired gate is skipped by ``validate_quote`` itself rather than guessed.
* Reward accrual credits and locked collateral keep their existing paper
  accounting. Only the risk state reads them through marked equity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from polymarketbot.logs import get_logger
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig
from polymarketbot.strategy.books import mid

log = get_logger(__name__)

#: One breaker/cancel log line per asset per interval; a halt persists across
#: ticks and must not write the same warning on every one of them.
CANCEL_LOG_INTERVAL_S = 30.0


@dataclass(frozen=True)
class Account:
    """The paper account measured from the ledger's own books."""

    equity: float
    cash: float
    positions_value: float
    unrealized_pnl: float


def build_manager(capital: dict[str, float]) -> RiskManager:
    """The shared risk authority, configured from the live source.

    The bankroll is the paper ledger's total capital; everything else is
    ``RiskConfig``'s default, which is the same set the live real-money engine
    constructs (it spells the derived values out rather than changing them).
    """
    bankroll = float(sum(float(value) for value in capital.values()))
    if bankroll <= 0.0:
        raise ValueError(
            "paper risk layer needs a positive bankroll; refusing to build a "
            f"RiskConfig for {bankroll!r}"
        )
    base = RiskConfig(
        total_bankroll_usd=bankroll,
        # The live real-money engine is long-only; the paper ledger also
        # never sells shares it does not own, so this is the same cap.
        allow_short=False,
    )
    # Live's aggregate engine explicitly derives the per-order cap from the
    # measured sizer quote size, falling back to the same position-cost cap
    # when no measured quote size exists (``live/engine/trading.py``). Paper
    # has no separate sizer measurement, so it uses that documented fallback
    # instead of ``RiskConfig``'s unrelated 50-share default.
    cap = float(base.max_position_cost_per_market or bankroll)
    return RiskManager(
        RiskConfig(
            total_bankroll_usd=bankroll,
            allow_short=False,
            max_quote_size_shares=cap,
            max_position_cost_per_market=min(cap, bankroll),
        )
    )


def attach(engine, risk_manager: RiskManager | None = None) -> RiskManager:
    """Create (or adopt) the engine's risk manager and seed it from the ledger.

    The starting equity is the day's opening and peak equity, so a resumed
    ledger does not instantly look like a drawdown against a capital number it
    never held. A breaker that trips later registers the cancellation callback
    installed here.
    """
    rm = risk_manager if risk_manager is not None else build_manager(engine.capital)
    rm.register_unquote_callback(
        lambda asset_id, reason: cancel_quotes(engine, asset_id, reason)
    )
    acc = account(engine)
    state = rm.portfolio_state
    state.bankroll_usd = acc.equity
    state.day_start_equity_usd = acc.equity
    state.peak_equity_usd = acc.equity
    state.current_equity_usd = acc.equity
    state.current_cash_usd = acc.cash
    state.current_positions_value_usd = acc.positions_value
    #: Equity this session's realized PnL is measured from. ``None`` is never
    #: used: ``attach`` always runs before the first tick.
    engine._risk_baseline_equity = acc.equity
    engine._risk_cancel_log = {}
    log.info(
        f"paper risk layer: bankroll ${acc.equity:.2f}, daily loss limit "
        f"${rm.config.daily_loss_limit_usd:.2f} "
        f"({rm.config.daily_loss_limit_pct * 100.0:.1f}%), max drawdown "
        f"${rm.config.max_drawdown_usd:.2f} "
        f"({rm.config.max_drawdown_pct * 100.0:.1f}%), price jump "
        f"{rm.config.price_jump_threshold_cents:.1f}c/"
        f"{rm.config.price_jump_window_seconds:.0f}s -> "
        f"{rm.config.cooldown_duration_seconds:.0f}s cooldown, "
        f"order cap {rm.config.max_quote_size_shares:.0f} shares"
    )
    return rm


def manager(engine) -> RiskManager | None:
    """The engine's shared risk manager, or ``None`` for a non-engine caller.

    ``PaperEngine`` always constructs one in ``__init__``; this ``None`` path
    exists only so the reward runner can still be driven against the narrow
    test doubles in the strategy tests, which are not paper engines.
    """
    rm = getattr(engine, "risk_manager", None)
    return rm if isinstance(rm, RiskManager) else None


def account(engine) -> Account:
    """Cash, marked positions and PnL measured from the ledger."""
    equity_by_strategy = engine.ledger.equity()
    cash = 0.0
    positions = 0.0
    cost = 0.0
    for name in equity_by_strategy:
        cash += float(engine.ledger.cash(name))
        for lot in engine.ledger.positions(name):
            positions += lot.shares * lot.mark()
            cost += lot.cost_usd
    return Account(
        equity=float(sum(equity_by_strategy.values())),
        cash=cash,
        positions_value=positions,
        unrealized_pnl=positions - cost,
    )


def token_position(engine, token_id: str) -> tuple[float, float]:
    """``(shares, cost basis)`` held of one token across every strategy book."""
    shares = 0.0
    cost = 0.0
    for name in engine.ledger.equity():
        for lot in engine.ledger.positions(name):
            if lot.token_id != token_id:
                continue
            shares += lot.shares
            cost += lot.cost_usd
    return shares, cost


def reserved_cash(engine, *, exclude_token: str | None = None) -> float:
    """Cash the resting bids already committed, optionally excluding one order.

    The excluded token is the quote under validation: it is being replaced, so
    counting its own reserve would refuse every reprice of a fully deployed
    book.
    """
    total = 0.0
    for token_id, quote in engine.reward_quotes.items():
        if exclude_token is not None and token_id == exclude_token:
            continue
        if quote.cancel_at is not None or quote.shares <= 0.0:
            continue
        total += quote.shares * quote.price
    return total


def blocks_actions(engine) -> bool:
    """True when a permanent breaker (kill switch or halt) forbids new orders."""
    rm = manager(engine)
    if rm is None:
        return False
    state = rm.portfolio_state
    return bool(state.kill_switch_active or state.is_halted)


def blocks_fills(engine) -> bool:
    """True when a resting maker quote may no longer book a fill.

    Fills of maker quotes are cancelled with the quotes; a taker exit that was
    already submitted is an explicit exit and still lands. The engine's fill
    pass is stopped here so a tape replay cannot book a fill after the halt.
    """
    return blocks_actions(engine)


def quote_block_reason(engine, token_id: str, now: float | None = None) -> str | None:
    """Why ``token_id`` may not rest a new quote now, or ``None`` when it may."""
    rm = manager(engine)
    if rm is None:
        return None
    state = rm.portfolio_state
    if state.kill_switch_active:
        return "Emergency kill switch is active"
    if state.is_halted:
        return f"Risk engine is halted: {state.status.value}"
    if rm.is_in_cooldown(token_id, now):
        remaining = rm.volatility_monitor.get_remaining_cooldown(token_id, now)
        return f"Market in volatility cooldown ({remaining:.1f}s remaining)"
    return None


def max_quote_shares(engine) -> float:
    """The shared per-order share cap, or 0.0 when the manager is absent."""
    rm = manager(engine)
    if rm is None:
        return 0.0
    return float(getattr(rm.config, "max_quote_size_shares", 0.0) or 0.0)


def cancel_quotes(engine, asset_id: str | None, reason: str) -> None:
    """Cancel resting maker quotes (all, or one asset) through ``ActionBook``.

    The cancellation travels the same measured round trip as every other paper
    action; the quote record is marked ``cancel_at`` now so the fill pass stops
    consuming it immediately. De-skew fade targets for a blocked asset are
    dropped with its quotes, or the planner would re-rest the same side next
    tick.
    """
    now = time.time()
    cancelled = 0
    for token_id, quote in list(getattr(engine, "reward_quotes", {}).items()):
        if asset_id is not None and token_id != asset_id:
            continue
        if quote.cancel_at is not None:
            continue
        quote.cancel_at = now
        engine.actions.submit("reward_cancel", quote.strategy, {"token_id": token_id})
        cancelled += 1

    state = getattr(engine, "deskew", None)
    skews = getattr(state, "skews", None)
    if isinstance(skews, dict):
        if asset_id is None:
            skews.clear()
        else:
            for legs in list(skews.values()):
                legs.pop(asset_id, None)
            for condition_id in [cid for cid, legs in skews.items() if not legs]:
                skews.pop(condition_id, None)

    _log_cancel(engine, asset_id or "*", cancelled, reason)


def _log_cancel(engine, key: str, cancelled: int, reason: str) -> None:
    now = time.time()
    log_ts = getattr(engine, "_risk_cancel_log", None)
    if log_ts is None:
        log_ts = engine._risk_cancel_log = {}
    if now - log_ts.get(key, 0.0) < CANCEL_LOG_INTERVAL_S:
        return
    log_ts[key] = now
    log.warning(
        f"[PAPER RISK] cancelled {cancelled} resting quote(s) on {key}: {reason}"
    )


def tick(engine, books: dict[str, dict], now: float) -> None:
    """Feed one tick's measured state to the shared risk manager and enforce it.

    Order matters: every measurable mid is observed first (heartbeat, per-asset
    price-jump cooldown), then marked equity and PnL are published, so the
    daily-loss / drawdown breakers see this tick's marks before the planner
    runs. A breaker cancels quotes through the registered callback; a quote
    whose token no longer has a two-sided book is cancelled here because the
    book, not a clock, is paper's staleness evidence.
    """
    rm = manager(engine)
    if rm is None:
        return

    for condition_id, market in engine.by_condition.items():
        for token_id in market.token_ids:
            price = mid(books.get(token_id) or {})
            if price is None:
                continue
            healthy, message = rm.update_market_price(
                token_id, condition_id, price, timestamp=now
            )
            if not healthy and message:
                log.warning(f"[PAPER RISK] {token_id[:12]} cooled down: {message}")

    if blocks_actions(engine):
        # Live skips PnL evaluation while halted (``evaluate_quotes`` returns
        # first); re-tripping the same breaker every tick would only spam the
        # audit log. An operator resumes through ``RiskManager.resume``.
        cancel_quotes(engine, None, "risk engine halted or killed")
        return

    acc = account(engine)
    baseline = float(getattr(engine, "_risk_baseline_equity", acc.equity))
    rm.update_portfolio_pnl(
        current_equity=acc.equity,
        # Realized here means "everything not open-lot mark movement since this
        # session's opening equity": sell PnL, fees, rebates and settled reward
        # credits all land in cash. The split is informational; the breakers
        # read equity, peak and day start.
        realized_pnl=acc.equity - baseline - acc.unrealized_pnl,
        unrealized_pnl=acc.unrealized_pnl,
        current_cash=acc.cash,
        positions_market_value=acc.positions_value,
        timestamp=now,
    )
    if blocks_actions(engine):
        cancel_quotes(
            engine, None, f"breaker tripped ({rm.portfolio_state.status.value})"
        )
        return

    for token_id in list(engine.reward_quotes):
        if mid(books.get(token_id) or {}) is None:
            cancel_quotes(engine, token_id, "stale book: no two-sided mid")


def quote_denial(
    engine,
    *,
    condition_id: str,
    token_id: str,
    price: float,
    shares: float,
    books: dict[str, dict],
    tick_size: float,
    now: float,
) -> tuple[bool, str, float, float]:
    """Run one proposed reward quote through the shared ``validate_quote``.

    Returns ``(allowed, reason, approved_size, approved_price)`` exactly as the
    manager does. The account state is measured, never defaulted: a missing mid
    is a refusal (a limit price is not a mid), cash excludes the collateral
    already resting behind other bids, and the quote being replaced is excluded
    from that reserve because it is cancelled by the same action.
    """
    rm = manager(engine)
    if rm is None:
        return True, "no risk manager (non-engine caller)", float(shares), float(price)
    mid_price = mid(books.get(token_id) or {})
    if mid_price is None:
        return (
            False,
            "no measured mid (book missing or one-sided)",
            0.0,
            float(price),
        )
    pos_shares, pos_cost = token_position(engine, token_id)
    acc = account(engine)
    free_cash = acc.cash - reserved_cash(engine, exclude_token=token_id)
    return rm.validate_quote(
        asset_id=token_id,
        condition_id=condition_id,
        side="BUY",
        price=float(price),
        size=float(shares),
        current_pos_shares=pos_shares,
        current_pos_cost=pos_cost,
        current_portfolio_inventory_usd=acc.positions_value,
        current_cash_usd=max(0.0, free_cash),
        mid_price=mid_price,
        tick_size=float(tick_size),
        timestamp=now,
    )


def taker_refusal(
    engine,
    *,
    strategy: str,
    token_id: str,
    side: str,
    shares: float,
    price: float,
) -> str | None:
    """Capital/position approval for a de-skew taker leg, or a refusal reason.

    Mirrors the live Tier-2 gate: a halted/killed engine refuses the cross; a
    BUY may only spend free cash and may not push the token cost or the
    portfolio inventory past the shared caps. The cap is a refusal, not a
    resize, so an emergency exit is never silently changed.
    """
    rm = manager(engine)
    if rm is None:
        return None
    state = rm.portfolio_state
    if state.kill_switch_active or state.is_halted:
        return f"risk manager is halted or kill-switched ({state.status.value})"
    if side.upper() != "BUY":
        return None
    notional = float(shares) * float(price)
    acc = account(engine)
    free_cash = acc.cash - reserved_cash(engine)
    if notional > free_cash + 1e-9:
        return f"taker BUY ${notional:.4f} exceeds free cash ${free_cash:.4f}"
    _, pos_cost = token_position(engine, token_id)
    cap = getattr(rm.config, "max_position_cost_per_market", None)
    if cap is not None and pos_cost + notional > float(cap) + 1e-9:
        return (
            f"taker BUY would take {token_id[:12]} cost to "
            f"${pos_cost + notional:.2f} (cap ${float(cap):.2f})"
        )
    portfolio_cap = getattr(rm.config, "max_portfolio_inventory_usd", None)
    if portfolio_cap is not None:
        after = acc.positions_value + notional
        if after > float(portfolio_cap) + 1e-9:
            return (
                f"taker BUY would take inventory to ${after:.2f} "
                f"(cap ${float(portfolio_cap):.2f})"
            )
    return None
