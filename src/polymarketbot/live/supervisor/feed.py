"""WebSocket plumbing, book seeding, and the do-not-quote-before-WS gate."""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING, Any

from polymarketbot.live.supervisor.helpers import (
    _close_engine_quietly,
    _thread_optional,
)
from polymarketbot.live.supervisor.markets import _format_open_order_ids
from polymarketbot.logs import get_logger

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")


def create_feed(ctx: RunContext) -> None:
    """Construct the live market feed this run subscribes to."""
    ctx.feed = ctx.deps.feed_cls(fallback_poll_interval=2.0)


def wire_feed(ctx: RunContext) -> None:
    """Route book and trade callbacks into the engine and subscribe both tokens."""
    # -- Feed plumbing -------------------------------------------------------
    def on_book(snapshot: Any) -> None:
        token = "YES" if snapshot.asset_id == ctx.engine.asset_id_yes else ("NO" if snapshot.asset_id == ctx.engine.asset_id_no else None)
        if token:
            ctx.engine.on_book_update(token, snapshot.bids, snapshot.asks)

    def on_trade(trade: Any) -> None:
        token = "YES" if trade.asset_id == ctx.engine.asset_id_yes else ("NO" if trade.asset_id == ctx.engine.asset_id_no else None)
        if token:
            ctx.engine.on_trade_update(token, trade.price, trade.size, trade.side, trade.timestamp)

    ctx.feed.add_book_listener(on_book)
    ctx.feed.add_trade_listener(on_trade)
    ctx.feed.subscribe(ctx.market["token_yes"])
    ctx.feed.subscribe(ctx.market["token_no"])


async def seed_books(ctx: RunContext) -> None:
    """Seed the books with REST marks while quoting stays disabled."""
    # Seed the books with REST marks, but do NOT quote yet: the engine must not
    # place an order before the WebSocket session is up.
    ctx.engine.trading_enabled = False
    for label, token in (("YES", ctx.market["token_yes"]), ("NO", ctx.market["token_no"])):
        try:
            snapshot = await asyncio.to_thread(ctx.feed.get_book_sync, token)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning(f"Initial {label} book sync failed: {exc}")
            continue
        if snapshot is not None and (snapshot.bids or snapshot.asks):
            await asyncio.to_thread(ctx.engine.on_book_update, label, snapshot.bids, snapshot.asks)


async def finalize_equity_baseline(ctx: RunContext) -> int | None:
    """Re-baseline equity once the books are seeded and before the first quote."""
    # Re-baseline equity after the books are seeded and before the first quote.
    finalize_baseline = getattr(ctx.engine, "finalize_initial_baseline", None)
    if callable(finalize_baseline) and not finalize_baseline():
        log.critical(
            "Refusing to quote: the initial equity baseline could not be finalized "
            "(inventory is held but a fresh book or cost basis is unavailable)."
        )
        await _close_engine_quietly(ctx.engine)
        return 2
    return None


def install_signal_handlers(ctx: RunContext) -> None:
    """Stop the loop on SIGINT/SIGTERM; the handler must not touch the network."""

    def handle_signal(*_: Any) -> None:
        # Signal handlers must not touch the network or SQLite.
        log.info("Shutdown signal received; stopping the supervision loop.")
        ctx.running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, TypeError, OSError):
            pass


async def start_and_enable(ctx: RunContext) -> None:
    """Start the feed and enable quoting only if the WebSocket is already up."""
    await ctx.feed.start()
    try:
        ws_ready = bool(ctx.feed.is_ws_connected)
    except Exception as exc:  # noqa: BLE001 - a liveness probe must not stop the loop
        ws_ready = False
        log.warning(f"Could not read feed connection state: {exc}")
    ctx.awaiting_ws_ready = not ws_ready
    if ws_ready:
        ctx.engine.trading_enabled = True
        log.info("[STARTUP] WebSocket connected; engine quoting enabled.")
    else:
        log.warning(
            "[STARTUP] WebSocket is not connected yet; engine stays disabled "
            "until the session is up."
        )


async def check_liveness(ctx: RunContext, now: float) -> bool:
    """Halt on a stale feed, resume when it recovers, and keep the book current."""
    # -- Feed liveness -------------------------------------------------
    try:
        feed_stale = ctx.feed.is_stale(ctx.args.max_feed_staleness)
    except Exception as exc:  # noqa: BLE001 - a liveness probe must not stop the loop
        feed_stale = False
        log.warning(f"Feed staleness check failed: {exc}")
    if not feed_stale:
        # A quiet market may send no book frame for minutes; while the
        # feed connection is alive its last snapshot is still current.
        refresh_liveness = getattr(ctx.engine, "refresh_book_liveness", None)
        if callable(refresh_liveness):
            refresh_liveness(now)
        # Quote from the seeded book even when no new event arrives
        # (startup after the WS gate, resume after a feed halt, quiet book).
        if ctx.engine.trading_enabled and (now - ctx.last_quote_eval) >= 5.0:
            try:
                await asyncio.to_thread(ctx.engine.evaluate_quotes, now)
            except Exception as exc:  # noqa: BLE001 - one failed tick must not stop the loop
                log.error(f"Periodic quote evaluation failed: {exc}")
            ctx.last_quote_eval = now
    if feed_stale and ctx.engine.trading_enabled and not ctx.feed_halt_active:
        # Deliberate: quotes cannot be repriced without the feed, but the
        # venue-side resting orders are still valid and are left live.
        ctx.engine.halt_trading("feed stale", cancel=False)
        ctx.feed_halt_active = True
        resting = await _thread_optional(
            "Open-order read after stale-feed halt",
            ctx.clob_client.get_open_orders,
            market=ctx.market["condition_id"],
        )
        log.critical(
            f"[FEED STALE] No frame for >{ctx.args.max_feed_staleness:.1f}s. Quoting halted. "
            "DELIBERATE: cancel=False, so RESTING ORDERS REMAIN LIVE ON THE VENUE. "
            f"Open order ids: {_format_open_order_ids(resting)}. "
            "They stay exposed until the feed recovers or an operator cancels them."
        )
    elif not feed_stale and ctx.feed_halt_active:
        halt_reason = getattr(ctx.engine, "halt_reason", None)
        if halt_reason in (None, "", "feed stale"):
            resume = getattr(ctx.engine, "resume_trading", None)
            if callable(resume):
                if resume():
                    log.info("[FEED RECOVERED] Quoting resumed.")
                else:
                    log.warning(
                        "[FEED RECOVERED] The engine refused to resume; a risk "
                        "breaker or kill switch is still active."
                    )
            else:
                ctx.engine.trading_enabled = True
                log.info("[FEED RECOVERED] Quoting resumed.")
        else:
            log.warning(f"[FEED RECOVERED] Quoting stays halted for another reason: {halt_reason}")
        ctx.feed_halt_active = False
    return feed_stale


async def await_ws_ready(ctx: RunContext) -> None:
    # Startup gate: enable quoting only once the WebSocket is actually up.
    if ctx.awaiting_ws_ready:
        if ctx.market_halt_done:
            ctx.awaiting_ws_ready = False
        else:
            try:
                ws_connected = bool(ctx.feed.is_ws_connected)
            except Exception:  # noqa: BLE001 - a liveness probe must not stop the loop
                ws_connected = False
            if ws_connected:
                ctx.engine.trading_enabled = True
                ctx.awaiting_ws_ready = False
                log.info("[STARTUP] WebSocket connected; engine quoting enabled.")
