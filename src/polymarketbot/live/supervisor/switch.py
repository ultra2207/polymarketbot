"""The rewards re-scan and the market migration it drives.

The measured switching cost is the hurdle, never an extra margin the runner
invents, and a migration is refused unless the old market's quotes are proven
cancelled and its inventory verified flat.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from polymarketbot.live.runner_market import (
    MARKET_STATUS_INTERVAL,
    MarketSwitchBlocked,
    build_switching_engine,
    rebind_engine_to_market,
)
from polymarketbot.live.supervisor.sizing import (
    matched_pair_quote_size,
    measure_crossing_friction_pct,
    measure_spread_inputs,
)
from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.switching import SwitchDecision, SwitchEvent

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")


def init_switcher(ctx: RunContext) -> None:
    """Build the re-scan engine for an auto-selected market (a pinned one never moves)."""
    ctx.switcher = None
    # Candidates the live engine refused (neg-risk, invalid, unsizable) are
    # filtered from later scans so the next-best market can be considered.
    ctx.switch_excluded_conditions = set()
    if ctx.market.get("auto_selected"):
        try:
            live_yield = float(ctx.engine.get_current_daily_yield())
        except Exception as exc:  # noqa: BLE001 - a measurement must not stop startup
            live_yield = 0.0
            log.warning(f"[MARKET SWITCH] Live yield measurement failed: {exc}")
        ctx.switcher = build_switching_engine(
            ctx.market,
            bankroll_usd=ctx.trading_bankroll,
            live_yield=live_yield,
        )
        if ctx.switcher is not None:
            log.info(
                f"[MARKET SWITCH] Rewards universe re-scan every "
                f"{MARKET_STATUS_INTERVAL:.0f}s on the market-lifecycle timer. "
                "The measured switching cost is the hurdle; no extra margin is applied."
            )


def _rescan_with_measured_friction(
    ctx: RunContext, live_yield: float | None
) -> SwitchDecision | None:
    """Rank candidates, price the crossing into the best one, then apply the hurdle.

    ``SwitchingConfig.crossing_spread_friction_pct`` is a measurement of the
    *candidate*, not a property of the active market, so it is re-measured for
    the top candidate on every scan. A missing book or fee schedule leaves it
    ``None``, which makes the switching cost infinite and keeps the current
    market: the refusal is the correct answer, and the log names the missing
    input.
    """
    switcher = ctx.switcher
    if switcher is None:
        return None
    ranked = switcher.scanner.scan_and_rank_markets(
        allocated_capital_usd=switcher.allocated_capital_usd,
        quote_horizon_s=switcher.quote_horizon_s,
    )
    # Keep the active market's own yield current exactly as the engine's
    # rescan does, so both sides of the comparison come from one scan.
    active = switcher.active_opportunity
    if active is not None:
        measured = next(
            (opp for opp in ranked if opp.condition_id == active.condition_id), None
        )
        if measured is not None:
            switcher.refresh_active_market(measured)
        elif live_yield is not None:
            active.net_expected_yield = float(live_yield)

    excluded = ctx.switch_excluded_conditions
    top = next((opp for opp in ranked if opp.condition_id not in excluded), None)
    if top is not None and (active is None or top.condition_id != active.condition_id):
        token_yes = str(top.tokens[0].token_id) if top.tokens else ""
        switcher.config.crossing_spread_friction_pct = measure_crossing_friction_pct(
            condition_id=top.condition_id,
            token_yes=token_yes,
            http_session=ctx.l2_session,
        )
    switcher.check_opportunity_switch(ranked, exclude_condition_ids=excluded)
    return switcher.last_decision


async def rewards_rescan(ctx: RunContext, now: float, feed_stale: bool) -> None:
    """Re-scan the rewards universe on the market-lifecycle cadence."""
    # -- Rewards re-scan & opportunistic switch (every 60 s) -------------
    if ctx.switcher is not None and now - ctx.last_market_switch_scan >= MARKET_STATUS_INTERVAL:
        ctx.last_market_switch_scan = now
        if feed_stale or ctx.market_halt_done:
            log.debug(
                "[MARKET SWITCH] Re-scan skipped while quoting is halted "
                f"(feed_stale={feed_stale}, market_halt_done={ctx.market_halt_done})."
            )
        else:
            try:
                live_yield = float(ctx.engine.get_current_daily_yield())
            except Exception as exc:  # noqa: BLE001 - a measurement must not stop supervision
                live_yield = None
                log.warning(f"[MARKET SWITCH] Live yield measurement failed: {exc}")
            try:
                decision: SwitchDecision | None = await asyncio.to_thread(
                    _rescan_with_measured_friction, ctx, live_yield
                )
            except Exception as exc:  # noqa: BLE001 - keep the current market and keep looping
                decision = None
                log.error(
                    "[MARKET SWITCH] Rewards re-scan failed; keeping "
                    f"{ctx.market['condition_id'][:12]}: {exc}"
                )
            if decision is not None:
                log.info(
                    f"[MARKET SWITCH] {'SWITCH' if decision.should_switch else 'HOLD'} "
                    f"'{decision.from_question[:40]}' -> '{decision.to_question[:40]}' "
                    f"({decision.from_condition_id or 'none'} -> "
                    f"{decision.to_condition_id or 'none'}) | "
                    f"expected gain ${decision.expected_gain_per_day:+.2f}/day | "
                    f"switching cost ${decision.switching_cost_usd:.4f} "
                    f"(${decision.amortized_cost_per_day:.2f}/day amortized) | "
                    f"{decision.reason}"
                )
                candidate = decision.candidate
                if (
                    decision.should_switch
                    and candidate is not None
                    and candidate.condition_id != ctx.market["condition_id"]
                ):
                    try:
                        new_details: dict[str, Any] | None = await asyncio.to_thread(
                            ctx.deps.resolve_market_details,
                            ctx.feed,
                            ctx.clob_client,
                            candidate.condition_id,
                            None,
                            None,
                            ctx.trading_bankroll,
                            None,
                            None,
                            None,
                        )
                    except SystemExit as exc:
                        new_details = None
                        ctx.switch_excluded_conditions.add(candidate.condition_id)
                        log.warning(
                            f"[MARKET SWITCH] Candidate {candidate.condition_id[:12]} "
                            f"could not be resolved; keeping the current market: {exc}"
                        )
                    except Exception as exc:  # noqa: BLE001 - keep the current market
                        new_details = None
                        log.error(
                            f"[MARKET SWITCH] Candidate resolution failed; keeping "
                            f"the current market: {exc}"
                        )
                    if new_details is not None and new_details["neg_risk"] and not ctx.dry_run:
                        log.warning(
                            "[MARKET SWITCH] Candidate "
                            f"{new_details['condition_id'][:12]} is neg-risk and the "
                            "live engine cannot settle it; keeping the current market."
                        )
                        ctx.switch_excluded_conditions.add(candidate.condition_id)
                        new_details = None
                    measured_inputs: dict[str, float] | None = None
                    if new_details is not None and ctx.args.spread_offset is None:
                        measured_inputs = measure_spread_inputs(
                            condition_id=new_details["condition_id"],
                            token_yes=new_details["token_yes"],
                            token_no=new_details.get("token_no"),
                            mid_price=new_details["mid_price"],
                            max_spread_cents=new_details["max_spread"],
                            tick_size_cents=new_details["tick_size_cents"],
                            competing_score=new_details.get("competing_score_override"),
                            capital_usd=ctx.trading_bankroll,
                            http_session=ctx.l2_session,
                        )
                        if measured_inputs is None:
                            ctx.switch_excluded_conditions.add(candidate.condition_id)
                            log.warning(
                                "[MARKET SWITCH] Candidate "
                                f"{candidate.condition_id[:12]} has no measured spread "
                                "inputs; keeping the current market."
                            )
                            new_details = None
                    if new_details is not None:
                        new_quote_size = (
                            float(ctx.args.quote_size)
                            if ctx.args.quote_size is not None
                            else ctx.deps.derive_quote_size(
                                initial_cash=ctx.trading_bankroll,
                                mid_price=new_details["mid_price"],
                                min_size=new_details["min_size"],
                                order_min_usd=new_details.get("order_min_usd"),
                            )
                        )
                        if new_quote_size <= 0.0:
                            ctx.switch_excluded_conditions.add(candidate.condition_id)
                            log.warning(
                                "[MARKET SWITCH] Candidate quote size resolved to "
                                f"{new_quote_size}; keeping the current market."
                            )
                        else:
                            new_spread_policy: dict[str, Any] | None = None
                            if ctx.args.spread_offset is not None:
                                new_spread_offset = float(ctx.args.spread_offset)
                            else:
                                assert measured_inputs is not None  # guarded above
                                new_spread_offset, new_spread_policy = ctx.deps.optimize_spread_offset(
                                    max_spread_cents=new_details["max_spread"],
                                    tick_size_cents=new_details["tick_size_cents"],
                                    daily_reward_pool=new_details["daily_reward"],
                                    quote_size=new_quote_size,
                                    **measured_inputs,
                                )
                            # The candidate's venue minimum is a notional too:
                            # raise both legs to one notional-clearing count,
                            # and keep the current market if the pair cannot be
                            # funded at that count. Never quote unequal legs.
                            pair_size, pair_cost = matched_pair_quote_size(
                                configured_size=new_quote_size,
                                mid_price=float(new_details["mid_price"]),
                                spread_offset_cents=new_spread_offset,
                                tick_size=float(new_details["tick_size"]),
                                min_order_usd=float(
                                    new_details.get("order_min_usd") or 0.0
                                ),
                            )
                            if pair_cost > ctx.trading_bankroll + 1e-9:
                                ctx.switch_excluded_conditions.add(
                                    candidate.condition_id
                                )
                                log.warning(
                                    "[MARKET SWITCH] Candidate "
                                    f"{candidate.condition_id[:12]} needs ${pair_cost:.2f} "
                                    f"for one matched pair ({pair_size:.0f} shares), above "
                                    f"the ${ctx.trading_bankroll:.2f} sizing cap; keeping "
                                    "the current market."
                                )
                                return
                            new_quote_size = pair_size
                            old_cid = ctx.market["condition_id"]
                            old_question = ctx.market["question"]
                            old_yes = ctx.market["token_yes"]
                            old_no = ctx.market["token_no"]
                            was_trading = bool(ctx.engine.trading_enabled)
                            ctx.engine.halt_trading("market switch", cancel=False)
                            try:
                                cancelled = bool(
                                    await asyncio.to_thread(ctx.engine.cancel_all_quotes)
                                )
                            except Exception as exc:  # noqa: BLE001 - an unproven cancel defers
                                cancelled = False
                                log.error(
                                    f"[MARKET SWITCH] Quote cancellation failed: {exc}"
                                )
                            if not cancelled:
                                log.warning(
                                    "[MARKET SWITCH] Quote cancellation is NOT confirmed "
                                    "by the venue; keeping the current market and "
                                    "deferring the migration."
                                )
                                if was_trading:
                                    ctx.engine.resume_trading()
                                elif feed_stale:
                                    ctx.engine.halt_reason = "feed stale"
                                else:
                                    ctx.engine.halt_reason = None
                            else:
                                try:
                                    unlocked_cash = await asyncio.to_thread(
                                        rebind_engine_to_market,
                                        ctx.engine,
                                        new_details,
                                        quote_size=new_quote_size,
                                        spread_offset_cents=new_spread_offset,
                                    )
                                except MarketSwitchBlocked as exc:
                                    log.warning(
                                        "[MARKET SWITCH] Migration deferred; keeping "
                                        f"{old_cid[:12]}: {exc}."
                                    )
                                    if was_trading:
                                        ctx.engine.resume_trading()
                                    elif feed_stale:
                                        ctx.engine.halt_reason = "feed stale"
                                    else:
                                        ctx.engine.halt_reason = None
                                except Exception as exc:  # noqa: BLE001 - stop rather than quote
                                    log.critical(
                                        "[MARKET SWITCH] Rebinding the engine failed after "
                                        "quoting was halted; stopping instead of quoting an "
                                        f"inconsistent engine: {exc}"
                                    )
                                    ctx.running = False
                                else:
                                    unsubscribe = getattr(ctx.feed, "unsubscribe", None)
                                    if callable(unsubscribe):
                                        unsubscribe([old_yes, old_no])
                                    ctx.feed.subscribe(
                                        [new_details["token_yes"], new_details["token_no"]]
                                    )
                                    for label, token in (
                                        ("YES", new_details["token_yes"]),
                                        ("NO", new_details["token_no"]),
                                    ):
                                        try:
                                            snapshot = await asyncio.to_thread(
                                                ctx.feed.get_book_sync, token
                                            )
                                        except (OSError, ValueError, KeyError, TypeError) as exc:
                                            log.warning(
                                                f"[MARKET SWITCH] {label} book sync failed: {exc}"
                                            )
                                            continue
                                        if snapshot is not None and (
                                            snapshot.bids or snapshot.asks
                                        ):
                                            await asyncio.to_thread(
                                                ctx.engine.on_book_update,
                                                label,
                                                snapshot.bids,
                                                snapshot.asks,
                                            )
                                    refresh = getattr(ctx.engine, "refresh_book_liveness", None)
                                    if callable(refresh):
                                        refresh(time.time())

                                    ctx.market.update(new_details)
                                    ctx.market["auto_selected"] = True
                                    ctx.market["quote_size"] = new_quote_size
                                    ctx.market["spread_offset_cents"] = new_spread_offset
                                    ctx.daily_rew = new_details["daily_reward"]
                                    ctx.max_spr = new_details["max_spread"]
                                    ctx.min_sz = new_details["min_size"]
                                    ctx.quote_sz = new_quote_size
                                    ctx.spread_off = new_spread_offset
                                    ctx.spread_policy = new_spread_policy
                                    ctx.competing_override = ctx.args.competing_score is not None
                                    ctx.comp_score = (
                                        ctx.args.competing_score
                                        if ctx.competing_override
                                        else new_details.get("competing_score_override")
                                    )
                                    if ctx.recorder is not None:
                                        ctx.recorder.market = dict(ctx.market)
                                    ctx.switcher.refresh_active_market(candidate)
                                    ctx.switcher.history.append(
                                        SwitchEvent(
                                            timestamp=time.time(),
                                            from_condition_id=old_cid,
                                            from_question=old_question,
                                            to_condition_id=new_details["condition_id"],
                                            to_question=new_details["question"],
                                            y_net_from=decision.y_net_from,
                                            y_net_to=decision.y_net_to,
                                            switching_cost=decision.switching_cost_usd,
                                            hurdle_threshold=decision.hurdle_threshold,
                                            reason=decision.reason,
                                            unlocked_cash_usd=unlocked_cash,
                                        )
                                    )
                                    if was_trading:
                                        if not ctx.engine.resume_trading():
                                            log.warning(
                                                "[MARKET SWITCH] Quoting stays halted: a risk "
                                                "breaker or kill switch is active."
                                            )
                                    elif feed_stale:
                                        ctx.engine.halt_reason = "feed stale"
                                    else:
                                        ctx.engine.halt_reason = None
                                    log.info(
                                        f"[MARKET SWITCH] Quoting {ctx.market['question'][:40]} "
                                        f"({ctx.market['condition_id'][:12]}) with quote size "
                                        f"{new_quote_size:.2f} @ offset {new_spread_offset:.2f}c. "
                                        f"Unlocked ${unlocked_cash:.2f} USDC from {old_cid[:12]}."
                                    )
