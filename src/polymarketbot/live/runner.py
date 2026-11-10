"""Operator runner for the real-money Polymarket reward market-making engine.

This runner does exactly one job: connect the CLOB/CTF clients and the live
market feed to ``RealMoneyTradingEngine`` and supervise the loop. It does not
compound capital or derive APR from a single sample. An auto-selected rewards
market is re-scanned on the market-lifecycle cadence and migrated only when the
measured yield improvement clears ``rewards.switching``'s own switching cost
(gas + merge gas + crossing friction); a pinned ``--condition-id`` never moves.

Safety model:
1. Real orders are placed only when ``--live`` is supplied. Without it the run
   is a dry-run even when credentials are configured.
2. ``--live`` without ``--yes`` requires an interactive TTY and the exact
   confirmation string ``START LIVE``; anything else aborts with exit code 2.
3. After startup reconciliation and preflight, a live run aborts with exit
   code 1 when preflight is not ready. The runner never downgrades to dry-run.
4. The CLOB collateral balance is the wallet. ``--budget-pct``/``--max-budget``
   only produce a sizing cap, never cash. On-chain USDC/POL reads are gas
   diagnostics only.
5. Token ids are never fabricated. A market that cannot be resolved aborts.
6. Quoting halts when the feed goes stale or the market stops accepting orders;
   resting orders are reported as still live because a halt does not cancel them.
7. A market migration is refused unless the old market's quotes are confirmed
   cancelled and its outcome-token inventory is verified flat after a complete-set
   merge; the single-market engine would otherwise misattribute those shares.

Exit codes: 0 = clean shutdown with cancellation confirmed by the venue;
1 = aborted (configuration, balance, preflight or API failure); 2 = live
confirmation or market gate refused; 3 = shutdown cancellation could NOT be
confirmed, so orders may still be live.

The pieces live next to this module: ``runner_cli`` parses the flags and holds
the live gate, ``runner_market`` resolves/switches the market, ``runner_ui``
renders the terminal. This module owns the process loop and is the entry point.

Usage::

    uv run python -m polymarketbot.live.runner --live --yes --budget-pct 0.20
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from typing import Any, Callable, List, Optional

import requests

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.scanner import find_optimal_lp_markets, get_real_l2_score
from polymarketbot.strategy.reward.switching import (
    OpportunisticSwitchingEngine,
    SwitchDecision,
    SwitchEvent,
)
from polymarketbot.strategy.sizing import derive_quote_size, optimize_spread_offset
from polymarketbot.strategy.venue.clob import ClobAuthCredentials, LiveClobClient
from polymarketbot.strategy.onchain.ctf import PolygonCtfClient
from polymarketbot.live.engine import RealMoneyTradingEngine
from polymarketbot.strategy.venue.feed import LivePolymarketFeed

from polymarketbot.live.runner_cli import parse_args, require_live_confirmation
from polymarketbot.live.runner_market import (
    MARKET_STATUS_INTERVAL,
    MarketSwitchBlocked,
    _coerce_float,
    _settlement_available,
    build_switching_engine,
    rebind_engine_to_market,
    resolve_market_details as _resolve_market_details,
)
from polymarketbot.live.runner_ui import render_dashboard, write_banner, write_final_summary

log = get_logger("real_money_trader")

# Seconds between blocking maintenance tasks (all executed off the event loop).
ORDERS_SYNC_INTERVAL = 5.0
BALANCE_SYNC_INTERVAL = 15.0
LOOP_SLEEP_SECONDS = 0.2
REDEEM_RETRY_INTERVAL = 60.0
# Shutdown cancellation must be proven; otherwise the operator gets exit code 3.
CANCEL_RETRY_ATTEMPTS = 3
CANCEL_RETRY_BACKOFF_SECONDS = 1.5
EXIT_ORDERS_UNCONFIRMED = 3
# APR is only shown once the reward sampler has at least this many epochs.
MIN_APR_SAMPLES = 60


# ---------------------------------------------------------------------------
# Market resolution
# ---------------------------------------------------------------------------
def resolve_market_details(
    feed: LivePolymarketFeed,
    clob: Any,
    requested_cid: Optional[str] = None,
    requested_yes: Optional[str] = None,
    requested_no: Optional[str] = None,
    bankroll: float = 150.0,
    min_size: Optional[float] = None,
    max_spread: Optional[float] = None,
    daily_reward: Optional[float] = None,
) -> dict[str, Any]:
    """Resolve the market, real token ids and execution metadata.

    Delegates to :mod:`polymarketbot.live.runner_market`. The rewards-universe
    scan is passed explicitly so overriding ``find_optimal_lp_markets`` on this
    module still redirects the scan.
    """
    return _resolve_market_details(
        feed,
        clob,
        requested_cid,
        requested_yes,
        requested_no,
        bankroll,
        min_size,
        max_spread,
        daily_reward,
        find_markets=find_optimal_lp_markets,
    )


# ---------------------------------------------------------------------------
# Blocking helpers routed through asyncio.to_thread
# ---------------------------------------------------------------------------
def _format_open_order_ids(orders: Any) -> str:
    """Name the venue's open orders for the operator, or say why they are unknown."""
    if orders is None:
        return "unavailable (open-order read failed)"
    if not isinstance(orders, list):
        return f"unavailable (unexpected payload {type(orders).__name__})"
    ids: List[str] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_id = order.get("id") or order.get("order_id") or order.get("orderId")
        ids.append(str(order_id) if order_id else "<no id in payload>")
    return ", ".join(ids) if ids else "none listed by the venue"


async def _thread_optional(label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking work in a thread; return None (logged) on failure."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        log.warning(f"{label} failed: {exc}")
        return None


async def _close_engine_quietly(engine: Any) -> None:
    """Close the engine off the event loop, never masking the caller's abort reason."""
    try:
        await asyncio.to_thread(engine.close)
    except Exception as exc:
        log.error(f"Engine close failed: {exc}")


def _compute_competing_score(
    session: requests.Session, token_yes: str, max_spread_cents: float
) -> Optional[float]:
    try:
        score, _, _ = get_real_l2_score(session, token_yes, max_spread_cents)
    except Exception as exc:
        log.warning(f"L2 competing-score refresh failed: {exc}")
        return None
    return score if score is not None and score > 0 else None


# ---------------------------------------------------------------------------
# Async loop
# ---------------------------------------------------------------------------
async def run_live_runner_async(args: argparse.Namespace) -> int:
    """Async supervision loop for the real-money engine."""
    dry_run = not bool(args.live)
    if args.live and args.dry_run:
        log.warning("Both --live and --dry-run were supplied: forcing dry-run. No real orders.")
        dry_run = True

    creds = ClobAuthCredentials.from_env()
    clob_client = LiveClobClient(credentials=creds, dry_run=dry_run)
    ctf_client = PolygonCtfClient(
        rpc_url=args.rpc_url,
        private_key=creds.private_key,
        dry_run=dry_run,
        signature_type=creds.signature_type,
    )
    settlement_available = _settlement_available(ctf_client)
    log.info(
        "[SETTLEMENT] On-chain CTF merge/redeem path: "
        + (
            "ENABLED (EOA signer)."
            if settlement_available
            else "REFUSED (proxy wallet, no signing key, or dry-run)."
        )
    )
    if not dry_run and not settlement_available and not args.allow_no_settlement:
        log.critical(
            "Refusing live trading: CTF merge/redeem is EOA-only and this account cannot "
            f"settle (signature_type={creds.signature_type}). Complete sets would hit the "
            "risk caps with no merge path. Use an EOA signer, or re-run with "
            "--allow-no-settlement to quote anyway (inventory can then only be exited by "
            "selling on the CLOB)."
        )
        return 2
    if not dry_run and not settlement_available:
        log.critical(
            "[SETTLEMENT] Live quoting allowed by --allow-no-settlement, but on-chain "
            "merge/redeem is unavailable; inventory can only be exited by selling on the CLOB."
        )

    # -- Wallet: the CLOB collateral balance is the wallet. -----------------
    if dry_run:
        dry_initial = _coerce_float(args.initial_cash)
        if dry_initial is None or dry_initial <= 0.0:
            log.critical(
                f"--initial-cash must be a positive number for dry-run, got {args.initial_cash!r}."
            )
            return 1
        total_wallet = dry_initial
        log.info(
            f"[DRY-RUN WALLET] Using --initial-cash ${total_wallet:.2f} as the simulated "
            "wallet. No CLOB collateral is queried for sizing."
        )
    else:
        try:
            balance = await asyncio.to_thread(clob_client.get_balance)
        except Exception as exc:
            log.critical(f"Cannot read the CLOB collateral balance; refusing to trade live: {exc}")
            return 1
        available_cash = _coerce_float((balance or {}).get("available"))
        locked_raw = _coerce_float((balance or {}).get("locked"))
        if available_cash is None:
            log.critical(
                f"CLOB /balance returned no usable 'available' amount: {balance!r}. Refusing to size."
            )
            return 1
        locked_cash = locked_raw or 0.0
        total_wallet = available_cash
        log.info(
            f"[LIVE WALLET] CLOB available collateral ${available_cash:.2f} "
            f"(locked ${locked_cash:.2f})."
        )

    # On-chain reads are gas diagnostics only; they never size the position.
    wallet_addr = str(getattr(clob_client, "maker_address", "") or "")
    if wallet_addr and not dry_run:
        pol_balance, onchain_usdc = await asyncio.gather(
            _thread_optional("POL balance read", ctf_client.get_pol_balance, wallet_addr),
            _thread_optional("on-chain USDC read", ctf_client.get_usdc_balance, wallet_addr),
        )
        log.info(
            f"[GAS DIAGNOSTICS] POL {pol_balance} | on-chain USDC.e ${onchain_usdc} "
            "(diagnostics only; not used for sizing)."
        )

    # -- Sizing cap (never cash) --------------------------------------------
    raw_budget_pct = _coerce_float(args.budget_pct)
    if raw_budget_pct is None:
        log.critical(f"--budget-pct must be a finite number, got {args.budget_pct!r}. Aborting.")
        return 1
    if raw_budget_pct > 1.0:
        raw_budget_pct /= 100.0
    budget_pct = max(0.01, min(1.0, raw_budget_pct))
    max_budget = _coerce_float(args.max_budget) if args.max_budget is not None else None
    if args.max_budget is not None and max_budget is None:
        log.critical(f"--max-budget must be a finite number, got {args.max_budget!r}. Aborting.")
        return 1
    if max_budget is not None and max_budget <= 0.0:
        log.critical(f"--max-budget must be positive, got {max_budget:.2f}. Aborting.")
        return 1

    trading_bankroll = total_wallet * budget_pct
    if max_budget is not None:
        trading_bankroll = min(trading_bankroll, max_budget)
    trading_bankroll = round(max(0.0, trading_bankroll), 2)
    if trading_bankroll <= 0.0:
        log.critical(
            f"Sizing cap is ${trading_bankroll:.2f} from a ${total_wallet:.2f} wallet at "
            f"{budget_pct * 100.0:.1f}%. Nothing can be quoted; aborting."
        )
        return 1
    log.info(
        f"[SIZING CAP] ${trading_bankroll:.2f} = {budget_pct * 100.0:.1f}% of "
        f"${total_wallet:.2f} wallet"
        + (f", capped at --max-budget ${max_budget:.2f}" if max_budget is not None else "")
        + ". This is a sizing limit, not cash."
    )
    if dry_run:
        clob_client._mock_balance_usd = total_wallet

    feed = LivePolymarketFeed(fallback_poll_interval=2.0)
    market = await asyncio.to_thread(
        resolve_market_details,
        feed,
        clob_client,
        args.condition_id,
        args.token_yes,
        args.token_no,
        trading_bankroll,
        args.min_size,
        args.max_spread,
        args.daily_reward,
    )

    if market["neg_risk"] and not dry_run:
        log.critical(
            "Refusing live trading on a neg-risk market: CTF mergePositions does not "
            "settle neg-risk positions (PolygonCtfClient has no NegRiskAdapter path). "
            f"Condition {market['condition_id']} has neg_risk=True. Pick a non-neg-risk "
            "market or run --dry-run."
        )
        return 2

    daily_rew = args.daily_reward if args.daily_reward is not None else market["daily_reward"]
    max_spr = args.max_spread if args.max_spread is not None else market["max_spread"]
    min_sz = args.min_size if args.min_size is not None else market["min_size"]
    competing_override = args.competing_score is not None
    comp_score = args.competing_score if competing_override else market.get("competing_score_override")

    if args.quote_size is not None:
        quote_sz = float(args.quote_size)
    else:
        quote_sz = derive_quote_size(
            initial_cash=trading_bankroll,
            mid_price=market["mid_price"],
            min_size=min_sz,
        )
    if quote_sz <= 0.0:
        log.critical(f"Derived quote size is {quote_sz}. Aborting rather than quoting zero.")
        return 1

    if args.spread_offset is not None:
        spread_off = float(args.spread_offset)
    else:
        spread_off, _ = optimize_spread_offset(
            max_spread_cents=max_spr,
            market_spread_cents=market["market_spread_cents"],
            tick_size_cents=market.get("tick_size_cents", 1.0),
            daily_reward_pool=daily_rew,
            quote_size=quote_sz,
            category=market.get("category", "default"),
        )
    # Sizing telemetry for the dashboard (not engine state).
    market["quote_size"] = quote_sz
    market["spread_offset_cents"] = spread_off

    engine = RealMoneyTradingEngine(
        condition_id=market["condition_id"],
        asset_id_yes=market["token_yes"],
        asset_id_no=market["token_no"],
        clob_client=clob_client,
        ctf_client=ctf_client,
        # Sizing seed for RiskConfig / quote sizing only. reconcile_startup()
        # replaces cash and the equity baseline with the venue's figures in live.
        initial_cash=trading_bankroll,
        total_wallet_balance=total_wallet,
        budget_pct=budget_pct,
        max_budget=max_budget,
        quote_size=quote_sz,
        spread_offset_cents=spread_off,
        daily_reward_rate=daily_rew,
        rewards_max_spread_cents=max_spr,
        rewards_min_size=min_sz,
        auto_merge_threshold=args.auto_merge_threshold,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        institutional_mode=not args.legacy_mode,
        competing_score_override=comp_score,
        tick_size=market["tick_size"],
        neg_risk=market["neg_risk"],
        event_id=market.get("event_id"),
        fee_rate_bps=market["fee_rate_bps"],
        days_to_resolution=market["days_to_resolution"],
        book_stale_seconds=args.max_feed_staleness,
        dry_run=dry_run,
    )

    # -- Startup reconciliation (venue truth) -------------------------------
    try:
        await asyncio.to_thread(engine.reconcile_startup)
    except Exception as exc:
        if dry_run:
            log.warning(f"Startup reconciliation failed in dry-run ({exc}); continuing to inspect.")
        else:
            log.critical(f"Startup reconciliation failed; refusing to trade live: {exc}")
            await _close_engine_quietly(engine)
            return 1

    preflight = await asyncio.to_thread(engine.preflight_check)
    sys.stdout.write(f"\n{preflight.summary()}\n")
    sys.stdout.flush()
    if not dry_run and not preflight.ready_for_live:
        log.critical(
            "Preflight is NOT ready for live trading. Aborting without placing orders; "
            "the runner does not downgrade to dry-run."
        )
        await _close_engine_quietly(engine)
        return 1

    # -- Private session recording (non-blocking; the quote loop never waits) --
    recorder = None
    record_live_data = (
        args.record_live_data if args.record_live_data is not None else not dry_run
    )
    if record_live_data:
        try:
            from polymarketbot.live.recorder import LiveSessionRecorder

            recorder = LiveSessionRecorder(
                db_path=args.db_path,
                clob=clob_client,
                market=market,
                dry_run=dry_run,
                ctf_client=ctf_client,
                feed_age_fn=lambda: feed.last_message_age_seconds,
                engine_params={
                    "quote_size": quote_sz,
                    "spread_offset_cents": spread_off,
                    "bankroll_usd": trading_bankroll,
                    "budget_pct": budget_pct,
                    "institutional_mode": not args.legacy_mode,
                },
            )
            await asyncio.to_thread(recorder.start)
            engine.event_hook = recorder.record_event
            log.info(f"Live session recording -> {recorder.out_dir}")
        except Exception as exc:
            log.error(f"Live session recorder failed to start; continuing without it: {exc}")
            recorder = None

    order_version: Optional[int] = None
    try:
        if dry_run:
            # Never trigger GET /version in a dry-run: previews stay offline.
            cached_version = getattr(clob_client, "_order_version", None)
            order_version = int(cached_version) if isinstance(cached_version, int) else None
        else:
            order_version = await asyncio.to_thread(lambda: clob_client.order_version)
    except Exception as exc:
        log.warning(f"Could not resolve the CLOB order version ({exc}); orders will resolve it on first use.")
    write_banner(
        dry_run=dry_run,
        order_version=order_version,
        signature_type=int(getattr(creds, "signature_type", 0) or 0),
        settlement_available=settlement_available,
        neg_risk=bool(market["neg_risk"]),
        total_wallet=total_wallet,
        trading_bankroll=trading_bankroll,
        budget_pct=budget_pct,
        max_budget=max_budget,
        condition_id=market["condition_id"],
    )

    # -- Feed plumbing -------------------------------------------------------
    def on_book(snapshot: Any) -> None:
        token = "YES" if snapshot.asset_id == engine.asset_id_yes else ("NO" if snapshot.asset_id == engine.asset_id_no else None)
        if token:
            engine.on_book_update(token, snapshot.bids, snapshot.asks)

    def on_trade(trade: Any) -> None:
        token = "YES" if trade.asset_id == engine.asset_id_yes else ("NO" if trade.asset_id == engine.asset_id_no else None)
        if token:
            engine.on_trade_update(token, trade.price, trade.size, trade.side, trade.timestamp)

    feed.add_book_listener(on_book)
    feed.add_trade_listener(on_trade)
    feed.subscribe(market["token_yes"])
    feed.subscribe(market["token_no"])

    # Seed the books with REST marks, but do NOT quote yet: the engine must not
    # place an order before the WebSocket session is up.
    engine.trading_enabled = False
    for label, token in (("YES", market["token_yes"]), ("NO", market["token_no"])):
        try:
            snapshot = await asyncio.to_thread(feed.get_book_sync, token)
        except Exception as exc:
            log.warning(f"Initial {label} book sync failed: {exc}")
            continue
        if snapshot is not None and (snapshot.bids or snapshot.asks):
            await asyncio.to_thread(engine.on_book_update, label, snapshot.bids, snapshot.asks)

    # Re-baseline equity after the books are seeded and before the first quote.
    finalize_baseline = getattr(engine, "finalize_initial_baseline", None)
    if callable(finalize_baseline):
        if not finalize_baseline():
            log.critical(
                "Refusing to quote: the initial equity baseline could not be finalized "
                "(inventory is held but a fresh book or cost basis is unavailable)."
            )
            await _close_engine_quietly(engine)
            return 2

    l2_session = requests.Session()
    start_time = time.time()
    last_orders_sync = 0.0
    last_balance_sync = 0.0
    last_reward_refresh = 0.0
    last_market_check = 0.0
    # The rewards re-scan reuses the market-lifecycle cadence (60 s) and waits
    # one full interval before its first pass so startup selection is not redone.
    last_market_switch_scan = start_time
    last_render = 0.0
    last_quote_eval = 0.0
    last_redeem_attempt = 0.0
    feed_halt_active = False
    market_halt_done = False
    quotes_cancelled = False
    redeem_done = False
    redeem_skip_warned = False
    awaiting_ws_ready = False
    running = True

    switcher: Optional[OpportunisticSwitchingEngine] = None
    # Candidates the live engine refused (neg-risk, invalid, unsizable) are
    # filtered from later scans so the next-best market can be considered.
    switch_excluded_conditions: set[str] = set()
    if market.get("auto_selected"):
        try:
            live_yield = float(engine.get_current_daily_yield())
        except Exception as exc:
            live_yield = 0.0
            log.warning(f"[MARKET SWITCH] Live yield measurement failed: {exc}")
        switcher = build_switching_engine(
            market,
            bankroll_usd=trading_bankroll,
            live_yield=live_yield,
        )
        if switcher is not None:
            log.info(
                f"[MARKET SWITCH] Rewards universe re-scan every "
                f"{MARKET_STATUS_INTERVAL:.0f}s on the market-lifecycle timer. "
                "The measured switching cost is the hurdle; no extra margin is applied."
            )

    def handle_signal(*_: Any) -> None:
        # Signal handlers must not touch the network or SQLite.
        nonlocal running
        log.info("Shutdown signal received; stopping the supervision loop.")
        running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception:
            pass

    final_telemetry: Optional[dict[str, Any]] = None
    exit_code = 0
    try:
        await feed.start()
        try:
            ws_ready = bool(feed.is_ws_connected)
        except Exception as exc:
            ws_ready = False
            log.warning(f"Could not read feed connection state: {exc}")
        awaiting_ws_ready = not ws_ready
        if ws_ready:
            engine.trading_enabled = True
            log.info("[STARTUP] WebSocket connected; engine quoting enabled.")
        else:
            log.warning(
                "[STARTUP] WebSocket is not connected yet; engine stays disabled "
                "until the session is up."
            )
        while running:
            now = time.time()

            if args.duration and (now - start_time) >= args.duration:
                log.info(f"Target duration of {args.duration:.1f}s reached.")
                break

            # -- Feed liveness -------------------------------------------------
            try:
                feed_stale = feed.is_stale(args.max_feed_staleness)
            except Exception as exc:
                feed_stale = False
                log.warning(f"Feed staleness check failed: {exc}")
            if not feed_stale:
                # A quiet market may send no book frame for minutes; while the
                # feed connection is alive its last snapshot is still current.
                refresh_liveness = getattr(engine, "refresh_book_liveness", None)
                if callable(refresh_liveness):
                    refresh_liveness(now)
                # Quote from the seeded book even when no new event arrives
                # (startup after the WS gate, resume after a feed halt, quiet book).
                if engine.trading_enabled and (now - last_quote_eval) >= 5.0:
                    try:
                        await asyncio.to_thread(engine.evaluate_quotes, now)
                    except Exception as exc:
                        log.error(f"Periodic quote evaluation failed: {exc}")
                    last_quote_eval = now
            if feed_stale and engine.trading_enabled and not feed_halt_active:
                # Deliberate: quotes cannot be repriced without the feed, but the
                # venue-side resting orders are still valid and are left live.
                engine.halt_trading("feed stale", cancel=False)
                feed_halt_active = True
                resting = await _thread_optional(
                    "Open-order read after stale-feed halt",
                    clob_client.get_open_orders,
                    market=market["condition_id"],
                )
                log.critical(
                    f"[FEED STALE] No frame for >{args.max_feed_staleness:.1f}s. Quoting halted. "
                    "DELIBERATE: cancel=False, so RESTING ORDERS REMAIN LIVE ON THE VENUE. "
                    f"Open order ids: {_format_open_order_ids(resting)}. "
                    "They stay exposed until the feed recovers or an operator cancels them."
                )
            elif not feed_stale and feed_halt_active:
                halt_reason = getattr(engine, "halt_reason", None)
                if halt_reason in (None, "", "feed stale"):
                    resume = getattr(engine, "resume_trading", None)
                    if callable(resume):
                        if resume():
                            log.info("[FEED RECOVERED] Quoting resumed.")
                        else:
                            log.warning(
                                "[FEED RECOVERED] The engine refused to resume; a risk "
                                "breaker or kill switch is still active."
                            )
                    else:
                        engine.trading_enabled = True
                        log.info("[FEED RECOVERED] Quoting resumed.")
                else:
                    log.warning(f"[FEED RECOVERED] Quoting stays halted for another reason: {halt_reason}")
                feed_halt_active = False

            # Startup gate: enable quoting only once the WebSocket is actually up.
            if awaiting_ws_ready:
                if market_halt_done:
                    awaiting_ws_ready = False
                else:
                    try:
                        ws_connected = bool(feed.is_ws_connected)
                    except Exception:
                        ws_connected = False
                    if ws_connected:
                        engine.trading_enabled = True
                        awaiting_ws_ready = False
                        log.info("[STARTUP] WebSocket connected; engine quoting enabled.")

            # -- Orders & fills (every 5 s) ------------------------------------
            if now - last_orders_sync >= ORDERS_SYNC_INTERVAL:
                try:
                    await asyncio.to_thread(engine.sync_live_orders_and_fills, timestamp=now)
                except Exception as exc:
                    log.error(f"Order/fill sync failed; keeping the last confirmed state: {exc}")
                last_orders_sync = now

            # -- Balances (every 15 s) -----------------------------------------
            if now - last_balance_sync >= BALANCE_SYNC_INTERVAL:
                try:
                    await asyncio.to_thread(engine.sync_live_balances)
                except Exception as exc:
                    log.error(f"Balance sync failed; keeping the last confirmed state: {exc}")
                last_balance_sync = now

            # -- Rewards + competing score ---------------------------------------
            if now - last_reward_refresh >= args.reward_refresh_interval:
                if not competing_override:
                    refreshed = await asyncio.to_thread(
                        _compute_competing_score, l2_session, market["token_yes"], max_spr
                    )
                    if refreshed is not None:
                        comp_score = refreshed
                try:
                    await asyncio.to_thread(
                        engine.process_reward_epoch,
                        timestamp=now,
                        competing_score=float(comp_score or 0.0),
                    )
                except Exception as exc:
                    log.error(f"Reward epoch processing failed: {exc}")
                last_reward_refresh = now

            # -- Rewards re-scan & opportunistic switch (every 60 s) -------------
            if switcher is not None and now - last_market_switch_scan >= MARKET_STATUS_INTERVAL:
                last_market_switch_scan = now
                if feed_stale or market_halt_done:
                    log.debug(
                        "[MARKET SWITCH] Re-scan skipped while quoting is halted "
                        f"(feed_stale={feed_stale}, market_halt_done={market_halt_done})."
                    )
                else:
                    try:
                        live_yield = float(engine.get_current_daily_yield())
                    except Exception as exc:
                        live_yield = None
                        log.warning(f"[MARKET SWITCH] Live yield measurement failed: {exc}")
                    try:
                        decision: Optional[SwitchDecision] = await asyncio.to_thread(
                            switcher.rescan_and_evaluate,
                            live_yield,
                            switch_excluded_conditions,
                        )
                    except Exception as exc:
                        decision = None
                        log.error(
                            "[MARKET SWITCH] Rewards re-scan failed; keeping "
                            f"{market['condition_id'][:12]}: {exc}"
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
                            and candidate.condition_id != market["condition_id"]
                        ):
                            try:
                                new_details: Optional[dict[str, Any]] = await asyncio.to_thread(
                                    resolve_market_details,
                                    feed,
                                    clob_client,
                                    candidate.condition_id,
                                    None,
                                    None,
                                    trading_bankroll,
                                    None,
                                    None,
                                    None,
                                )
                            except SystemExit as exc:
                                new_details = None
                                switch_excluded_conditions.add(candidate.condition_id)
                                log.warning(
                                    f"[MARKET SWITCH] Candidate {candidate.condition_id[:12]} "
                                    f"could not be resolved; keeping the current market: {exc}"
                                )
                            except Exception as exc:
                                new_details = None
                                log.error(
                                    f"[MARKET SWITCH] Candidate resolution failed; keeping "
                                    f"the current market: {exc}"
                                )
                            if new_details is not None and new_details["neg_risk"] and not dry_run:
                                log.warning(
                                    "[MARKET SWITCH] Candidate "
                                    f"{new_details['condition_id'][:12]} is neg-risk and the "
                                    "live engine cannot settle it; keeping the current market."
                                )
                                switch_excluded_conditions.add(candidate.condition_id)
                                new_details = None
                            if new_details is not None:
                                new_quote_size = (
                                    float(args.quote_size)
                                    if args.quote_size is not None
                                    else derive_quote_size(
                                        initial_cash=trading_bankroll,
                                        mid_price=new_details["mid_price"],
                                        min_size=new_details["min_size"],
                                    )
                                )
                                if new_quote_size <= 0.0:
                                    switch_excluded_conditions.add(candidate.condition_id)
                                    log.warning(
                                        "[MARKET SWITCH] Candidate quote size resolved to "
                                        f"{new_quote_size}; keeping the current market."
                                    )
                                else:
                                    if args.spread_offset is not None:
                                        new_spread_offset = float(args.spread_offset)
                                    else:
                                        new_spread_offset, _ = optimize_spread_offset(
                                            max_spread_cents=new_details["max_spread"],
                                            market_spread_cents=new_details["market_spread_cents"],
                                            tick_size_cents=new_details.get("tick_size_cents", 1.0),
                                            daily_reward_pool=new_details["daily_reward"],
                                            quote_size=new_quote_size,
                                            category=new_details.get("category", "default"),
                                        )
                                    old_cid = market["condition_id"]
                                    old_question = market["question"]
                                    old_yes = market["token_yes"]
                                    old_no = market["token_no"]
                                    was_trading = bool(engine.trading_enabled)
                                    engine.halt_trading("market switch", cancel=False)
                                    try:
                                        cancelled = bool(
                                            await asyncio.to_thread(engine.cancel_all_quotes)
                                        )
                                    except Exception as exc:
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
                                            engine.resume_trading()
                                        elif feed_stale:
                                            engine.halt_reason = "feed stale"
                                        else:
                                            engine.halt_reason = None
                                    else:
                                        try:
                                            unlocked_cash = await asyncio.to_thread(
                                                rebind_engine_to_market,
                                                engine,
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
                                                engine.resume_trading()
                                            elif feed_stale:
                                                engine.halt_reason = "feed stale"
                                            else:
                                                engine.halt_reason = None
                                        except Exception as exc:
                                            log.critical(
                                                "[MARKET SWITCH] Rebinding the engine failed after "
                                                "quoting was halted; stopping instead of quoting an "
                                                f"inconsistent engine: {exc}"
                                            )
                                            running = False
                                        else:
                                            unsubscribe = getattr(feed, "unsubscribe", None)
                                            if callable(unsubscribe):
                                                unsubscribe([old_yes, old_no])
                                            feed.subscribe(
                                                [new_details["token_yes"], new_details["token_no"]]
                                            )
                                            for label, token in (
                                                ("YES", new_details["token_yes"]),
                                                ("NO", new_details["token_no"]),
                                            ):
                                                try:
                                                    snapshot = await asyncio.to_thread(
                                                        feed.get_book_sync, token
                                                    )
                                                except Exception as exc:
                                                    log.warning(
                                                        f"[MARKET SWITCH] {label} book sync failed: {exc}"
                                                    )
                                                    continue
                                                if snapshot is not None and (
                                                    snapshot.bids or snapshot.asks
                                                ):
                                                    await asyncio.to_thread(
                                                        engine.on_book_update,
                                                        label,
                                                        snapshot.bids,
                                                        snapshot.asks,
                                                    )
                                            refresh = getattr(engine, "refresh_book_liveness", None)
                                            if callable(refresh):
                                                refresh(time.time())

                                            market.update(new_details)
                                            market["auto_selected"] = True
                                            market["quote_size"] = new_quote_size
                                            market["spread_offset_cents"] = new_spread_offset
                                            daily_rew = new_details["daily_reward"]
                                            max_spr = new_details["max_spread"]
                                            min_sz = new_details["min_size"]
                                            quote_sz = new_quote_size
                                            spread_off = new_spread_offset
                                            competing_override = args.competing_score is not None
                                            comp_score = (
                                                args.competing_score
                                                if competing_override
                                                else new_details.get("competing_score_override")
                                            )
                                            if recorder is not None:
                                                recorder.market = dict(market)
                                            switcher.refresh_active_market(candidate)
                                            switcher.history.append(
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
                                                if not engine.resume_trading():
                                                    log.warning(
                                                        "[MARKET SWITCH] Quoting stays halted: a risk "
                                                        "breaker or kill switch is active."
                                                    )
                                            elif feed_stale:
                                                engine.halt_reason = "feed stale"
                                            else:
                                                engine.halt_reason = None
                                            log.info(
                                                f"[MARKET SWITCH] Quoting {market['question'][:40]} "
                                                f"({market['condition_id'][:12]}) with quote size "
                                                f"{new_quote_size:.2f} @ offset {new_spread_offset:.2f}c. "
                                                f"Unlocked ${unlocked_cash:.2f} USDC from {old_cid[:12]}."
                                            )

            # -- Market lifecycle (every 60 s) ----------------------------------
            if now - last_market_check >= MARKET_STATUS_INTERVAL:
                last_market_check = now
                status = await _thread_optional(
                    "Market status check", clob_client.get_market, market["condition_id"]
                )
                end_passed = now >= float(market["end_timestamp"])
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
                    if not market_halt_done:
                        # cancel=False keeps the blocking venue cancel off the event loop.
                        engine.halt_trading(reason, cancel=False)
                        engine.market_closed = True
                        market_halt_done = True
                        log.warning(f"[MARKET LIFECYCLE] {reason}. Quoting halted.")
                    elif engine.trading_enabled:
                        # Never let a breaker reset re-enable quoting on a dead market.
                        engine.halt_trading(reason, cancel=False)
                    if not quotes_cancelled:
                        quotes_cancelled = bool(await asyncio.to_thread(engine.cancel_all_quotes))
                        if quotes_cancelled:
                            log.info("[MARKET LIFECYCLE] Quotes cancelled; the venue confirmed empty.")
                        else:
                            log.critical(
                                "[MARKET LIFECYCLE] Quote cancellation is NOT confirmed by the venue."
                            )

                # Redemption: once a minute until it succeeds or the run ends.
                if (
                    market_halt_done
                    and args.redeem_on_close
                    and not redeem_done
                    and now - last_redeem_attempt >= REDEEM_RETRY_INTERVAL
                ):
                    last_redeem_attempt = now
                    if not settlement_available:
                        if not redeem_skip_warned:
                            redeem_skip_warned = True
                            log.warning(
                                "--redeem-on-close: on-chain redemption is unavailable for this "
                                f"account (signature_type={getattr(creds, 'signature_type', 0)}, "
                                "dry-run, or no signing key). Positions can only be exited by "
                                "selling on the CLOB."
                            )
                    else:
                        state = await _thread_optional("Telemetry read", engine.get_telemetry)
                        shares = 0.0
                        if isinstance(state, dict):
                            shares = float(_coerce_float(state.get("shares_yes")) or 0.0) + float(
                                _coerce_float(state.get("shares_no")) or 0.0
                            )
                        if shares <= 0.0:
                            log.info("[REDEEM] No outcome-token inventory remains; nothing to redeem.")
                            redeem_done = True
                        else:
                            result = await _thread_optional(
                                "CTF redeems",
                                ctf_client.redeem_positions,
                                market["condition_id"],
                                index_sets=[1, 2],
                            )
                            if result is not None:
                                if bool(getattr(result, "success", False)):
                                    log.info(
                                        f"[REDEEM] Positions redeemed on-chain: "
                                        f"tx={getattr(result, 'tx_hash', '') or 'n/a'}."
                                    )
                                    redeem_done = True
                                else:
                                    log.warning(
                                        f"[REDEEM] Redemption failed (retrying in "
                                        f"{REDEEM_RETRY_INTERVAL:.0f}s): "
                                        f"{getattr(result, 'error', None) or 'unknown error'}."
                                    )

            # -- Dashboard ------------------------------------------------------
            if now - last_render >= args.poll_interval:
                try:
                    market["feed_age_seconds"] = feed.last_message_age_seconds
                except Exception:
                    market["feed_age_seconds"] = None
                telemetry = await asyncio.to_thread(engine.get_telemetry)
                final_telemetry = telemetry
                render_dashboard(
                    telemetry,
                    market,
                    start_time,
                    clear_screen=not args.no_clear,
                )
                last_render = now

            await asyncio.sleep(LOOP_SLEEP_SECONDS)

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown requested by operator or system interrupt.")
    finally:
        try:
            final_telemetry = await asyncio.to_thread(engine.get_telemetry)
        except Exception as exc:
            log.warning(f"Final telemetry read failed: {exc}")

        # Cancellation must be proven before the feed is stopped or the run returns.
        confirmed = False
        last_cancel_error: Optional[str] = None
        for attempt in range(1, CANCEL_RETRY_ATTEMPTS + 1):
            try:
                if await asyncio.to_thread(engine.cancel_all_quotes):
                    confirmed = True
                    break
                log.warning(
                    f"[SHUTDOWN] Quote cancellation not confirmed by the venue "
                    f"(attempt {attempt}/{CANCEL_RETRY_ATTEMPTS})."
                )
            except Exception as exc:
                last_cancel_error = str(exc)
                log.error(
                    f"[SHUTDOWN] Cancel attempt {attempt}/{CANCEL_RETRY_ATTEMPTS} failed: {exc}"
                )
            if attempt < CANCEL_RETRY_ATTEMPTS and CANCEL_RETRY_BACKOFF_SECONDS > 0.0:
                await asyncio.sleep(CANCEL_RETRY_BACKOFF_SECONDS)

        if confirmed:
            log.info("All resting quotes cancelled; the venue reports no open orders.")
        else:
            survivors = await _thread_optional(
                "Open-order read at shutdown",
                clob_client.get_open_orders,
                market=market["condition_id"],
            )
            log.critical(
                f"[SHUTDOWN] QUOTE CANCELLATION UNCONFIRMED after {CANCEL_RETRY_ATTEMPTS} "
                "attempts"
                + (f" (last error: {last_cancel_error})" if last_cancel_error else "")
                + f". Orders may still be live: {_format_open_order_ids(survivors)}. "
                f"Exiting with code {EXIT_ORDERS_UNCONFIRMED}; verify and cancel manually."
            )
            exit_code = EXIT_ORDERS_UNCONFIRMED

        try:
            await asyncio.to_thread(engine.flush_ledger)
        except Exception as exc:
            log.error(f"Ledger flush failed: {exc}")
        if recorder is not None:
            try:
                await asyncio.to_thread(recorder.stop, "shutdown")
            except Exception as exc:
                log.error(f"Live session recorder stop failed: {exc}")
        # Cancellation is already proven above; stop the feed before closing
        # the engine so a late book callback cannot touch a closed ledger.
        try:
            await feed.stop()
        except Exception as exc:
            log.error(f"Feed stop failed: {exc}")
        await _close_engine_quietly(engine)
        try:
            l2_session.close()
        except Exception:
            pass
        write_final_summary(final_telemetry)
        if exit_code != 0:
            log.critical(
                "[SHUTDOWN] Inventory/orders are NOT confirmed safe; exit code "
                f"{exit_code} signals that orders may still be live."
            )
        log.info("Real-Money Trading Engine shut down.")

    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.yes and not args.live:
        log.warning("--yes has no effect without --live; running dry-run.")
    live_requested = bool(args.live) and not bool(args.dry_run)
    if not require_live_confirmation(live_requested, args.yes, sys.stdin.isatty()):
        log.critical("Live trading aborted: live confirmation was not given.")
        return 2
    return asyncio.run(run_live_runner_async(args))


if __name__ == "__main__":
    sys.exit(main())
