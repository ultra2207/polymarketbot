"""The supervision tick loop and the proven-cancel shutdown."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import requests

from polymarketbot.live.base import reload_fit_artifacts
from polymarketbot.live.runner_ui import render_dashboard, write_final_summary
from polymarketbot.live.supervisor import feed, lifecycle, refit, switch
from polymarketbot.live.supervisor.helpers import (
    _close_engine_quietly,
    _thread_optional,
)
from polymarketbot.live.supervisor.markets import (
    _compute_competing_score,
    _format_open_order_ids,
)
from polymarketbot.logs import get_logger

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")

# Seconds between blocking maintenance tasks (all executed off the event loop).
ORDERS_SYNC_INTERVAL = 5.0
BALANCE_SYNC_INTERVAL = 15.0
LOOP_SLEEP_SECONDS = 0.2
# Shutdown cancellation must be proven; otherwise the operator gets exit code 3.
EXIT_ORDERS_UNCONFIRMED = 3


def init_state(ctx: RunContext) -> None:
    """Reset the per-run session and every timer/latch before the first tick."""
    ctx.l2_session = requests.Session()
    ctx.start_time = time.time()
    ctx.last_orders_sync = 0.0
    ctx.last_balance_sync = 0.0
    ctx.last_reward_refresh = 0.0
    ctx.last_market_check = 0.0
    # The rewards re-scan reuses the market-lifecycle cadence (60 s) and waits
    # one full interval before its first pass so startup selection is not redone.
    ctx.last_market_switch_scan = ctx.start_time
    ctx.last_render = 0.0
    ctx.last_quote_eval = 0.0
    ctx.last_redeem_attempt = 0.0
    ctx.last_refit_at = 0.0
    ctx.refit_task = None
    ctx.refit_process = None
    ctx.refit_failures = 0
    ctx.feed_halt_active = False
    ctx.market_halt_done = False
    ctx.quotes_cancelled = False
    ctx.redeem_done = False
    ctx.redeem_skip_warned = False
    ctx.awaiting_ws_ready = False
    ctx.running = True
    ctx.final_telemetry = None
    ctx.exit_code = 0


async def sync_orders(ctx: RunContext, now: float) -> None:
    """Sync orders and fills from the venue, keeping the last confirmed state."""
    # -- Orders & fills (every 5 s) ------------------------------------
    if now - ctx.last_orders_sync >= ORDERS_SYNC_INTERVAL:
        try:
            await asyncio.to_thread(ctx.engine.sync_live_orders_and_fills, timestamp=now)
        except Exception as exc:  # noqa: BLE001 - keep the last confirmed state and keep looping
            log.error(f"Order/fill sync failed; keeping the last confirmed state: {exc}")
        ctx.last_orders_sync = now


async def sync_balances(ctx: RunContext, now: float) -> None:
    """Sync balances from the venue, keeping the last confirmed state."""
    # -- Balances (every 15 s) -----------------------------------------
    if now - ctx.last_balance_sync >= BALANCE_SYNC_INTERVAL:
        try:
            await asyncio.to_thread(ctx.engine.sync_live_balances)
        except Exception as exc:  # noqa: BLE001 - keep the last confirmed state and keep looping
            log.error(f"Balance sync failed; keeping the last confirmed state: {exc}")
        ctx.last_balance_sync = now


async def refresh_rewards(ctx: RunContext, now: float) -> None:
    """Refresh the competing score and book the reward epoch."""
    # -- Rewards + competing score ---------------------------------------
    if now - ctx.last_reward_refresh >= ctx.args.reward_refresh_interval:
        if not ctx.competing_override:
            refreshed = await asyncio.to_thread(
                _compute_competing_score,
                ctx.l2_session,
                ctx.market["token_yes"],
                ctx.max_spr,
                ctx.market.get("token_no"),
            )
            if refreshed is not None:
                ctx.comp_score = refreshed
        try:
            await asyncio.to_thread(
                ctx.engine.process_reward_epoch,
                timestamp=now,
                competing_score=float(ctx.comp_score or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 - one failed epoch must not stop supervision
            log.error(f"Reward epoch processing failed: {exc}")
        ctx.last_reward_refresh = now


async def render_tick(ctx: RunContext, now: float) -> None:
    """Render the dashboard from the engine's latest telemetry."""
    # -- Dashboard ------------------------------------------------------
    if now - ctx.last_render >= ctx.args.poll_interval:
        try:
            ctx.market["feed_age_seconds"] = ctx.feed.last_message_age_seconds
        except Exception:  # noqa: BLE001 - a dashboard field must not stop supervision
            ctx.market["feed_age_seconds"] = None
        telemetry = await asyncio.to_thread(ctx.engine.get_telemetry)
        ctx.final_telemetry = telemetry
        render_dashboard(
            telemetry,
            ctx.market,
            ctx.start_time,
            clear_screen=not ctx.args.no_clear,
        )
        ctx.last_render = now


async def run_loop(ctx: RunContext) -> int:
    """Start the feed and supervise the run until it stops, then shut down for good."""
    try:
        await feed.start_and_enable(ctx)
        # One bounded refit at startup, then one every --refit-every. Artifact
        # reloads happen at the top of a tick, never mid-tick.
        refit.start(ctx)
        while ctx.running:
            now = time.time()

            reloaded = reload_fit_artifacts(ctx.engine)
            if reloaded:
                log.info(f"[REFIT] live components reloaded: {', '.join(reloaded)}")
            await refit.supervise(ctx, now)

            if ctx.args.duration and (now - ctx.start_time) >= ctx.args.duration:
                log.info(f"Target duration of {ctx.args.duration:.1f}s reached.")
                break

            feed_stale = await feed.check_liveness(ctx, now)
            await feed.await_ws_ready(ctx)
            await sync_orders(ctx, now)
            await sync_balances(ctx, now)
            await refresh_rewards(ctx, now)
            await switch.rewards_rescan(ctx, now, feed_stale)
            await lifecycle.market_lifecycle(ctx, now)
            await render_tick(ctx, now)

            await asyncio.sleep(LOOP_SLEEP_SECONDS)

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown requested by operator or system interrupt.")
    finally:
        await refit.shutdown(ctx)
        try:
            ctx.final_telemetry = await asyncio.to_thread(ctx.engine.get_telemetry)
        except Exception as exc:  # noqa: BLE001 - shutdown must continue without telemetry
            log.warning(f"Final telemetry read failed: {exc}")

        # Cancellation must be proven before the feed is stopped or the run returns.
        confirmed = False
        last_cancel_error: str | None = None
        for attempt in range(1, ctx.deps.cancel_attempts + 1):
            try:
                if await asyncio.to_thread(ctx.engine.cancel_all_quotes):
                    confirmed = True
                    break
                log.warning(
                    f"[SHUTDOWN] Quote cancellation not confirmed by the venue "
                    f"(attempt {attempt}/{ctx.deps.cancel_attempts})."
                )
            except Exception as exc:  # noqa: BLE001 - record the attempt and keep retrying
                last_cancel_error = str(exc)
                log.error(
                    f"[SHUTDOWN] Cancel attempt {attempt}/{ctx.deps.cancel_attempts} failed: {exc}"
                )
            if attempt < ctx.deps.cancel_attempts and ctx.deps.cancel_backoff > 0.0:
                await asyncio.sleep(ctx.deps.cancel_backoff)

        if confirmed:
            log.info("All resting quotes cancelled; the venue reports no open orders.")
        else:
            survivors = await _thread_optional(
                "Open-order read at shutdown",
                ctx.clob_client.get_open_orders,
                market=ctx.market["condition_id"],
            )
            log.critical(
                f"[SHUTDOWN] QUOTE CANCELLATION UNCONFIRMED after {ctx.deps.cancel_attempts} "
                "attempts"
                + (f" (last error: {last_cancel_error})" if last_cancel_error else "")
                + f". Orders may still be live: {_format_open_order_ids(survivors)}. "
                f"Exiting with code {EXIT_ORDERS_UNCONFIRMED}; verify and cancel manually."
            )
            ctx.exit_code = EXIT_ORDERS_UNCONFIRMED

        try:
            await asyncio.to_thread(ctx.engine.flush_ledger)
        except Exception as exc:  # noqa: BLE001 - shutdown must still stop the feed and engine
            log.error(f"Ledger flush failed: {exc}")
        if ctx.recorder is not None:
            try:
                await asyncio.to_thread(ctx.recorder.stop, "shutdown")
            except Exception as exc:  # noqa: BLE001 - recording must not block shutdown
                log.error(f"Live session recorder stop failed: {exc}")
        # Cancellation is already proven above; stop the feed before closing
        # the engine so a late book callback cannot touch a closed ledger.
        try:
            await ctx.feed.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown must still close the engine
            log.error(f"Feed stop failed: {exc}")
        await _close_engine_quietly(ctx.engine)
        try:
            ctx.l2_session.close()
        except Exception as exc:  # noqa: BLE001 - best-effort session close at shutdown
            log.debug(f"L2 session close failed: {exc}")
        write_final_summary(ctx.final_telemetry)
        if ctx.exit_code != 0:
            log.critical(
                "[SHUTDOWN] Inventory/orders are NOT confirmed safe; exit code "
                f"{ctx.exit_code} signals that orders may still be live."
            )
        log.info("Real-Money Trading Engine shut down.")

    return ctx.exit_code
