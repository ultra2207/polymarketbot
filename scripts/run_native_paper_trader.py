#!/usr/bin/env python3
"""Production Native Paper Trading Runner for Polymarket CLOB.

Executes live self-hosted paper trading with:
1. Direct connection to Polymarket CLOB (REST and WebSocket) with zero order rate limits.
2. SyntheticOrderBook & MarketImpactEngine modeling queue ahead and Kyle's lambda impact.
3. Real-world CTF Tokenomics: Split ($1.00 = 1 YES + 1 NO) and Merge ($0.015 gas fee).
4. 2-Tier Dynamic De-Skew Optimizer (Passive Avellaneda-Stoikov fade vs Emergency synthetic merge).
5. Exact Quadratic Liquidity Rewards scoring across 1,440 daily epochs with $1.00 daily payout floor.
6. Real-time high-density terminal telemetry and SQLite/JSON persistence.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from polymarketbot.logs import get_logger
from polymarketbot.rewards.deskew import DynamicDeSkewOptimizer
from polymarketbot.simulator.engine import NativePaperTradingEngine
from polymarketbot.simulator.feed import LivePolymarketFeed
from polymarketbot.simulator.optimizer import derive_quote_size, optimize_spread_offset

log = get_logger("native_paper_trader")

# ANSI color codes for high-density terminal telemetry
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_MAGENTA = "\033[35m"
C_BLUE = "\033[34m"
C_WHITE = "\033[37m"
C_BG_BLUE = "\033[44m"
C_BG_DARK = "\033[40m"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Native Self-Hosted Polymarket Paper Trading Engine"
    )
    parser.add_argument(
        "--condition-id",
        type=str,
        default=None,
        help="Polymarket condition ID to trade (default: auto-select top rewards market).",
    )
    parser.add_argument(
        "--token-yes",
        type=str,
        default=None,
        help="Asset/Token ID for YES outcome (optional, auto-resolved if omitted).",
    )
    parser.add_argument(
        "--token-no",
        type=str,
        default=None,
        help="Asset/Token ID for NO outcome (optional, auto-resolved if omitted).",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=100.0,
        help="Initial simulated cash balance in USD (default: $100.00).",
    )
    parser.add_argument(
        "--quote-size",
        type=float,
        default=None,
        help="Virtual maker quote size in shares (default: dynamically derived from initial cash).",
    )
    parser.add_argument(
        "--spread-offset",
        type=float,
        default=None,
        help="Half-spread offset from mid in cents (default: dynamically optimized for target market).",
    )
    parser.add_argument(
        "--daily-reward",
        type=float,
        default=25.0,
        help="Total daily reward pool in USD (default: $25.00 or fetched from API).",
    )
    parser.add_argument(
        "--max-spread",
        type=float,
        default=1.0,
        help="Qualifying max spread in cents for rewards (default: 1.0c).",
    )
    parser.add_argument(
        "--min-size",
        type=float,
        default=20.0,
        help="Qualifying minimum order size in shares (default: 20.0).",
    )
    parser.add_argument(
        "--auto-merge-threshold",
        type=float,
        default=10.0,
        help="Threshold of complete sets before triggering CTF Merge (default: 10.0).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duration to run in seconds (default: continuous until Ctrl+C).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Loop polling/tick interval in seconds (default: 1.0s).",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="data/native_paper_trading.db",
        help="SQLite database path for state persistence.",
    )
    parser.add_argument(
        "--ledger-path",
        type=str,
        default="data/native_paper_trading_ledger.json",
        help="JSON ledger path for audit trail.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Disable ANSI terminal screen clearing for log-friendly streaming.",
    )
    return parser.parse_args()


def resolve_market_details(
    feed: LivePolymarketFeed,
    requested_cid: Optional[str] = None,
    requested_yes: Optional[str] = None,
    requested_no: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve condition ID, question, tokens, and rewards config from Polymarket CLOB."""
    log.info("Resolving market details from Polymarket CLOB API...")

    markets = feed.get_rewards_markets_sync()
    target_market: Optional[dict[str, Any]] = None

    def _get_rate(m: dict[str, Any]) -> float:
        cfg = m.get("rewards_config", [])
        if cfg and isinstance(cfg, list) and len(cfg) > 0:
            val = float(cfg[0].get("rate_per_day", 0.0) or 0.0)
            return (val / 100.0) if val >= 500.0 else val
        val = float(m.get("rewards_daily_rate", 0.0) or 0.0)
        return (val / 100.0) if val >= 500.0 else val

    if requested_cid:
        for m in markets:
            if m.get("condition_id", "").lower() == requested_cid.lower():
                target_market = m
                break
    elif markets:
        # Pick market with highest daily reward rate
        markets_sorted = sorted(markets, key=_get_rate, reverse=True)
        target_market = markets_sorted[0] if markets_sorted else None

    cid = requested_cid or (target_market.get("condition_id") if target_market else None) or "0x221319cd...demo"
    question = (target_market.get("question") if target_market else None) or f"Market Condition {cid[:14]}..."
    daily_reward = _get_rate(target_market) if target_market else 25.0
    max_spread = float(target_market.get("rewards_max_spread", 1.0)) if target_market else 1.0
    min_size = float(target_market.get("rewards_min_size", 20.0)) if target_market else 20.0

    # Tokens resolution
    tok_yes = requested_yes
    tok_no = requested_no

    mid_price = 0.50
    if target_market and target_market.get("tokens"):
        tokens = target_market["tokens"]
        for t in tokens:
            outcome = str(t.get("outcome", "")).upper()
            tid = str(t.get("token_id", ""))
            price = float(t.get("price", 0.50) or 0.50)
            if outcome == "YES":
                mid_price = price
                if not tok_yes:
                    tok_yes = tid
            elif outcome == "NO" and not tok_no:
                tok_no = tid

    # Fallback asset IDs if not returned
    tok_yes = tok_yes or f"{cid}_YES"
    tok_no = tok_no or f"{cid}_NO"

    # Spread and tick size
    market_spread_cents = float(target_market.get("spread", 0.01) or 0.01) * 100.0 if target_market else 1.0
    tick_size_cents = float(target_market.get("tick_size", 0.01) or 0.01) * 100.0 if target_market else 1.0

    # Market category inference
    q_lower = question.lower()
    if any(k in q_lower for k in ["senate", "house", "president", "election", "democrat", "republican", "tax", "vote"]):
        category = "politics"
    elif any(k in q_lower for k in ["btc", "eth", "crypto", "solana"]):
        category = "crypto"
    elif any(k in q_lower for k in ["nba", "nfl", "champion", "match", "cup"]):
        category = "sports"
    else:
        category = "default"

    return {
        "condition_id": cid,
        "question": question,
        "token_yes": tok_yes,
        "token_no": tok_no,
        "daily_reward": daily_reward,
        "max_spread": max_spread,
        "min_size": min_size,
        "mid_price": mid_price,
        "market_spread_cents": market_spread_cents,
        "tick_size_cents": tick_size_cents,
        "category": category,
    }


def render_dashboard(
    engine: NativePaperTradingEngine,
    market_info: dict[str, Any],
    start_time: float,
    clear_screen: bool = True,
) -> None:
    """Render high-density, real-time terminal telemetry dashboard."""
    t = engine.get_telemetry()
    elapsed = time.time() - start_time
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Format numbers
    equity_color = C_GREEN if t["total_pnl"] >= 0 else C_RED
    net_q = t["net_exposure"]
    q_color = C_GREEN if abs(net_q) <= 15.0 else (C_YELLOW if abs(net_q) <= 50.0 else C_RED)

    # De-skew info
    deskew = t.get("latest_deskew") or {}
    pathway = deskew.get("pathway", "HOLD_STABLE")
    urgency = deskew.get("urgency_level", "LOW")
    urgency_color = C_GREEN if urgency == "LOW" else (C_YELLOW if urgency == "MEDIUM" else C_RED)
    savings = deskew.get("synthetic_savings_cents", 0.0)

    # Floor status
    proj_daily = (t["total_rewards"] / max(1, t["epoch_index"])) * 1440.0 if t["epoch_index"] > 0 else 0.0
    floor_met = proj_daily >= 1.0
    floor_str = f"{C_GREEN}MET (>= $1.00){C_RESET}" if floor_met else f"{C_YELLOW}UNMET (< $1.00){C_RESET}"

    lines = []
    if clear_screen:
        lines.append("\033[H\033[J")  # Clear terminal screen

    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}{C_WHITE}[MM] POLYMARKET NATIVE PAPER TRADING ENGINE | 24/7 CLOB & CTF SIMULATION{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}Market:{C_RESET} {market_info['question'][:76]}")
    lines.append(
        f"{C_DIM}Condition:{C_RESET} {t['condition_id'][:18]}... | "
        f"{C_DIM}Time:{C_RESET} {now_str} | "
        f"{C_DIM}Runtime:{C_RESET} {elapsed:.1f}s | "
        f"{C_BOLD}Dynamic Size:{C_RESET} {engine.quote_size:.1f}sh | "
        f"{C_BOLD}Offset:{C_RESET} {engine.spread_offset_cents:.2f}c"
    )
    lines.append(f"{C_DIM}Tokens:{C_RESET} YES: {market_info['token_yes'][:18]}... | NO: {market_info['token_no'][:18]}...")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[PORTFOLIO] PORTFOLIO LEDGER & CTF ACCOUNTING{C_RESET}")
    lines.append(
        f"  {C_BOLD}Equity:{C_RESET}        {equity_color}${t['equity']:>8.2f}{C_RESET}    "
        f"{C_BOLD}Cash:{C_RESET}          ${t['cash']:>8.2f}     "
        f"{C_BOLD}Market Val:{C_RESET}    ${t['market_value']:>8.2f}"
    )
    lines.append(
        f"  {C_BOLD}Shares YES:{C_RESET}    {t['shares_yes']:>8.2f}    "
        f"{C_BOLD}Avg Cost YES:{C_RESET}  ${t['avg_cost_yes']:>7.4f}     "
        f"{C_BOLD}Realized PnL:{C_RESET}  ${t['realized_pnl']:>+8.4f}"
    )
    lines.append(
        f"  {C_BOLD}Shares NO:{C_RESET}     {t['shares_no']:>8.2f}    "
        f"{C_BOLD}Avg Cost NO:{C_RESET}   ${t['avg_cost_no']:>7.4f}     "
        f"{C_BOLD}Unrealized PnL:{C_RESET}${t['unrealized_pnl']:>+8.4f}"
    )
    lines.append(
        f"  {C_BOLD}Net Exposure:{C_RESET}  {q_color}{net_q:>+8.2f}{C_RESET}    "
        f"{C_BOLD}Complete Sets:{C_RESET} {t['complete_sets']:>8.2f}     "
        f"{C_BOLD}TOTAL PnL:{C_RESET}     {equity_color}${t['total_pnl']:>+8.4f}{C_RESET}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[DE-SKEW] 2-TIER DYNAMIC DE-SKEW OPTIMIZER{C_RESET}")
    lines.append(
        f"  {C_BOLD}Pathway:{C_RESET}       [{C_BOLD}{pathway:<16s}{C_RESET}]   "
        f"{C_BOLD}Urgency:{C_RESET} {urgency_color}{urgency:<10s}{C_RESET}   "
        f"{C_BOLD}Synthetic Savings:{C_RESET} {C_GREEN}+{savings:.2f}c/sh{C_RESET}"
    )
    if deskew.get("reason"):
        lines.append(f"  {C_DIM}Sentinel Action:{C_RESET} {deskew['reason'][:74]}")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[REWARDS] POLYMARKET QUADRATIC REWARDS (1,440 Daily Epochs){C_RESET}")
    user_score = sum(q.get("score", 0.0) for q in t["active_quotes"])
    lines.append(
        f"  {C_BOLD}Epoch Index:{C_RESET}   {t['epoch_index']:>4d} / 1440    "
        f"{C_BOLD}User Score:{C_RESET}    {user_score:>8.2f}   "
        f"{C_BOLD}Floor Status:{C_RESET}    [{floor_str}]"
    )
    lines.append(
        f"  {C_BOLD}Daily Pool:{C_RESET}    ${market_info['daily_reward']:>7.2f}     "
        f"{C_BOLD}Proj. Daily:{C_RESET}   ${proj_daily:>8.2f}   "
        f"{C_BOLD}Rewards Earned:{C_RESET}  {C_GREEN}${t['total_rewards']:>8.4f}{C_RESET}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[CTF] CTF SETTLEMENT & REAL-WORLD EXECUTION{C_RESET}")
    lines.append(
        f"  {C_BOLD}Total Merges:{C_RESET}  {t['total_merges']:>4d}          "
        f"{C_BOLD}Total Splits:{C_RESET}  {t['total_splits']:>4d}          "
        f"{C_BOLD}Polygon Gas Fees:{C_RESET} ${t['total_gas_fees']:>8.3f}"
    )
    lines.append(
        f"  {C_BOLD}Total Fills:{C_RESET}   {t['fills_count']:>4d}          "
        f"{C_BOLD}Traded Volume:{C_RESET} ${t['total_volume_usd']:>8.2f}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[QUOTES] ACTIVE RESTING VIRTUAL QUOTES (FIFO Queue & Quadratic Scoring){C_RESET}")
    lines.append(f"  {'Side':<6s} {'Outcome':<8s} {'Price':<8s} {'Size':<8s} {'Queue Ahead':<14s} {'Score S(v,s)':<14s} {'Status'}")
    if t["active_quotes"]:
        for q in t["active_quotes"]:
            side_color = C_GREEN if q["side"] == "BUY" else C_RED
            lines.append(
                f"  {side_color}{q['side']:<6s}{C_RESET} "
                f"{q.get('token_side', 'YES'):<8s} "
                f"${q['price']:<7.3f} "
                f"{q['size']:<8.1f} "
                f"{q['queue_ahead']:<14.1f} "
                f"{q['score']:<14.4f} "
                f"{C_GREEN}{q.get('status', 'ACTIVE')}{C_RESET}"
            )
    else:
        lines.append(f"  {C_DIM}No active resting quotes in book.{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


async def run_paper_trading_async(args: argparse.Namespace) -> int:
    """Async main execution loop for native Polymarket paper trader with live WebSocket and REST streaming."""
    feed = LivePolymarketFeed(fallback_poll_interval=2.0)
    market = resolve_market_details(
        feed=feed,
        requested_cid=args.condition_id,
        requested_yes=args.token_yes,
        requested_no=args.token_no,
    )

    daily_rew = args.daily_reward or market["daily_reward"]
    max_spr = args.max_spread or market["max_spread"]
    min_sz = args.min_size or market["min_size"]

    # 1. Dynamically derive quote size from initial cash if not explicitly passed
    if args.quote_size is not None:
        quote_sz = args.quote_size
        log.info(f"Using manual quote size override: {quote_sz:.1f} shares")
    else:
        quote_sz = derive_quote_size(
            initial_cash=args.initial_cash,
            mid_price=market["mid_price"],
            min_size=min_sz,
            reserve_pct=0.25,
        )
        log.info(
            f"[DYNAMIC SIZING] Derived quote size from ${args.initial_cash:.2f} initial cash: "
            f"{quote_sz:.1f} shares (mid=${market['mid_price']:.3f}, min_size={min_sz:.1f})"
        )

    # 2. Dynamically optimize spread offset for this specific market if not explicitly passed
    if args.spread_offset is not None:
        spread_off = args.spread_offset
        log.info(f"Using manual spread offset override: {spread_off:.2f}c")
    else:
        spread_off, opt_meta = optimize_spread_offset(
            max_spread_cents=max_spr,
            market_spread_cents=market["market_spread_cents"],
            tick_size_cents=market.get("tick_size_cents", 1.0),
            daily_reward_pool=daily_rew,
            quote_size=quote_sz,
            category=market.get("category", "default"),
        )
        log.info(
            f"[DYNAMIC SPREAD OPTIMIZER] Optimized spread offset for market: "
            f"s*={spread_off:.2f}c (Max Spread={max_spr:.1f}c, Mkt Spread={market['market_spread_cents']:.1f}c) -> "
            f"Net Utility=${opt_meta.get('net_utility', 0.0):.2f}/d, Proj Rewards=${opt_meta.get('payout_daily', 0.0):.2f}/d"
        )

    engine = NativePaperTradingEngine(
        condition_id=market["condition_id"],
        asset_id_yes=market["token_yes"],
        asset_id_no=market["token_no"],
        initial_cash=args.initial_cash,
        quote_size=quote_sz,
        spread_offset_cents=spread_off,
        daily_reward_rate=daily_rew,
        rewards_max_spread_cents=max_spr,
        rewards_min_size=min_sz,
        auto_merge_threshold=args.auto_merge_threshold,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
    )

    # 1. Register live book listener
    def on_book_update(snapshot: Any) -> None:
        tok_side = "YES" if snapshot.asset_id == market["token_yes"] else "NO"
        engine.update_orderbook_snapshot(
            token_side=tok_side,
            bids=snapshot.bids,
            asks=snapshot.asks,
        )
        engine.evaluate_quotes()

    feed.add_book_listener(on_book_update)

    # 2. Register live trade listener (drains queue_ahead and executes fills!)
    def on_trade_update(trade: Any) -> None:
        tok_side = "YES" if trade.asset_id == market["token_yes"] else "NO"
        fills = engine.process_public_trade(
            token_side=tok_side,
            price=trade.price,
            size=trade.size,
            side=trade.side,
            timestamp=trade.timestamp,
        )
        if fills:
            for f in fills:
                log.info(
                    f"[SIMULATED FILL] {f.token_side} {f.side} {f.fill_size:.1f}sh @ "
                    f"${f.fill_price:.4f} (Queue Depleted: {f.queue_depleted:.1f}, Drift: {f.post_fill_drift:+.4f})"
                )
            engine.evaluate_quotes()

    feed.add_trade_listener(on_trade_update)

    # 3. Subscribe tokens to feed
    feed.subscribe(market["token_yes"])
    feed.subscribe(market["token_no"])

    # 4. Initial book snapshot via REST
    try:
        book_yes = feed.get_book_sync(market["token_yes"])
        if book_yes and (book_yes.bids or book_yes.asks):
            engine.update_orderbook_snapshot("YES", book_yes.bids, book_yes.asks)
        book_no = feed.get_book_sync(market["token_no"])
        if book_no and (book_no.bids or book_no.asks):
            engine.update_orderbook_snapshot("NO", book_no.bids, book_no.asks)
    except Exception as e:
        log.warning(f"Initial REST orderbook fetch warning: {e}")

    # 5. Start live feed (WebSocket + REST fallback)
    await feed.start()
    log.info("Live Polymarket Feed started (WebSocket streaming + trade listener active).")

    start_time = time.time()
    last_epoch_check = time.time()
    running = True

    def handle_sigint(sig: int, frame: Any) -> None:
        nonlocal running
        log.warning("Interrupt signal received. Shutting down paper trader...")
        running = False

    signal.signal(signal.SIGINT, handle_sigint)

    log.info("Paper trading execution loop engaged.")
    try:
        while running:
            loop_now = time.time()
            if args.duration and (loop_now - start_time) >= args.duration:
                log.info(f"Requested duration ({args.duration}s) reached. Stopping.")
                break

            try:
                # Periodic 60-second reward epoch
                if (loop_now - last_epoch_check) >= 60.0:
                    engine.process_reward_epoch(timestamp=loop_now)
                    last_epoch_check = loop_now

                # Auto complete-set merge check
                pos = engine.portfolio.get_position(market["condition_id"])
                if pos.complete_sets >= args.auto_merge_threshold:
                    m_res = engine.portfolio.merge(market["condition_id"], pos.complete_sets)
                    log.info(
                        f"[CTF MERGE] Merged {m_res.shares_merged:.1f} complete sets -> "
                        f"Unlocked ${m_res.cash_unlocked:.2f} USDC (Gas fee: ${m_res.gas_fee:.3f})"
                    )

                # Quoting & de-skew evaluation
                engine.evaluate_quotes(timestamp=loop_now)

                # Render dashboard
                render_dashboard(
                    engine=engine,
                    market_info=market,
                    start_time=start_time,
                    clear_screen=not args.no_clear,
                )

                # Flush persistence
                engine.flush_ledger()

            except Exception as e:
                log.error(f"Error in paper trading iteration: {e}")

            await asyncio.sleep(args.poll_interval)
    finally:
        log.info("Stopping live feed and flushing final ledger...")
        await feed.stop()
        engine.flush_ledger()
        log.info(f"Native paper trading completed. State saved to {args.db_path} and {args.ledger_path}.")

    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    return asyncio.run(run_paper_trading_async(args))


if __name__ == "__main__":
    sys.exit(main())
