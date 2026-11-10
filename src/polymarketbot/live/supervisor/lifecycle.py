"""Market status and redemption."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from polymarketbot.logs import get_logger
from polymarketbot.live.runner_market import MARKET_STATUS_INTERVAL, _coerce_float
from polymarketbot.live.supervisor.helpers import _thread_optional

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")

REDEEM_RETRY_INTERVAL = 60.0


async def market_lifecycle(ctx: "RunContext", now: float) -> None:
    """Halt on a dead market, then redeem once it closes."""
    # -- Market lifecycle (every 60 s) ----------------------------------
    if now - ctx.last_market_check >= MARKET_STATUS_INTERVAL:
        ctx.last_market_check = now
        status = await _thread_optional(
            "Market status check", ctx.clob_client.get_market, ctx.market["condition_id"]
        )
        end_passed = now >= float(ctx.market["end_timestamp"])
        closed = bool((status or {}).get("closed"))
        accepting = (status or {}).get("accepting_orders")
        reason: Optional[str] = None
        if closed:
            reason = "market closed"
        elif accepting is False:
            reason = "market not accepting orders"
        elif end_passed:
            reason = "market end date passed"
        if reason is not None:
            if not ctx.market_halt_done:
                # cancel=False keeps the blocking venue cancel off the event loop.
                ctx.engine.halt_trading(reason, cancel=False)
                ctx.engine.market_closed = True
                ctx.market_halt_done = True
                log.warning(f"[MARKET LIFECYCLE] {reason}. Quoting halted.")
            elif ctx.engine.trading_enabled:
                # Never let a breaker reset re-enable quoting on a dead market.
                ctx.engine.halt_trading(reason, cancel=False)
            if not ctx.quotes_cancelled:
                ctx.quotes_cancelled = bool(await asyncio.to_thread(ctx.engine.cancel_all_quotes))
                if ctx.quotes_cancelled:
                    log.info("[MARKET LIFECYCLE] Quotes cancelled; the venue confirmed empty.")
                else:
                    log.critical(
                        "[MARKET LIFECYCLE] Quote cancellation is NOT confirmed by the venue."
                    )

        # Redemption: once a minute until it succeeds or the run ends.
        if (
            ctx.market_halt_done
            and ctx.args.redeem_on_close
            and not ctx.redeem_done
            and now - ctx.last_redeem_attempt >= REDEEM_RETRY_INTERVAL
        ):
            ctx.last_redeem_attempt = now
            if not ctx.settlement_available:
                if not ctx.redeem_skip_warned:
                    ctx.redeem_skip_warned = True
                    log.warning(
                        "--redeem-on-close: on-chain redemption is unavailable for this "
                        f"account (signature_type={getattr(ctx.creds, 'signature_type', 0)}, "
                        "dry-run, or no signing key). Positions can only be exited by "
                        "selling on the CLOB."
                    )
            else:
                state = await _thread_optional("Telemetry read", ctx.engine.get_telemetry)
                shares = 0.0
                if isinstance(state, dict):
                    shares = float(_coerce_float(state.get("shares_yes")) or 0.0) + float(
                        _coerce_float(state.get("shares_no")) or 0.0
                    )
                if shares <= 0.0:
                    log.info("[REDEEM] No outcome-token inventory remains; nothing to redeem.")
                    ctx.redeem_done = True
                else:
                    result = await _thread_optional(
                        "CTF redeems",
                        ctx.ctf_client.redeem_positions,
                        ctx.market["condition_id"],
                        index_sets=[1, 2],
                    )
                    if result is not None:
                        if bool(getattr(result, "success", False)):
                            log.info(
                                f"[REDEEM] Positions redeemed on-chain: "
                                f"tx={getattr(result, 'tx_hash', '') or 'n/a'}."
                            )
                            ctx.redeem_done = True
                        else:
                            log.warning(
                                f"[REDEEM] Redemption failed (retrying in "
                                f"{REDEEM_RETRY_INTERVAL:.0f}s): "
                                f"{getattr(result, 'error', None) or 'unknown error'}."
                            )
