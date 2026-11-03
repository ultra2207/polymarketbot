#!/usr/bin/env python3
"""Production Real-Money Polymarket Trading Runner for CLOB & Polygon CTF.

Executes live algorithmic market making on Polymarket:
1. Live CLOB REST & WebSocket Order Management with EIP-712 signing (Chain ID 137).
2. On-Chain Settlement: mergePositions on Conditional Token Framework contract.
3. 2-Tier Dynamic De-Skew Optimizer (Avellaneda-Stoikov fade & synthetic cross-hedging).
4. Master RiskManager Circuit Breakers: price jump breaker (>2.0c in 60s), daily loss ($15/5%),
   and drawdown ($30/10%) unquote kill-switches.
5. Dynamic Capital Compounding via ContinuousOptimizationGovernor.
6. Opportunistic 300s Market Switching Hurdle via CapitalMigrationEngine.
7. Real-time high-density terminal telemetry with pre-flight diagnostics & safe dry-run inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
from polymarketbot.rewards.queue_valuation import CapitalMigrationEngine
from polymarketbot.simulator.feed import LivePolymarketFeed
from polymarketbot.simulator.optimizer import derive_quote_size, optimize_spread_offset
from polymarketbot.trading.client import ClobAuthCredentials, LiveClobClient
from polymarketbot.trading.ctf import PolygonCtfClient
from polymarketbot.trading.engine import RealMoneyTradingEngine

log = get_logger("real_money_trader")

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
        description="Production Real-Money Polymarket Trading Engine"
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
        help="Initial reference cash balance in USD (default: $100.00).",
    )
    parser.add_argument(
        "--quote-size",
        type=float,
        default=None,
        help="Maker quote size in shares (default: dynamically derived from cash balance).",
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
        default=None,
        help="Total daily reward pool in USD (default: fetched from market rewards config).",
    )
    parser.add_argument(
        "--max-spread",
        type=float,
        default=None,
        help="Qualifying max spread in cents for rewards (default: fetched from market).",
    )
    parser.add_argument(
        "--min-size",
        type=float,
        default=None,
        help="Qualifying minimum order size in shares (default: fetched from market).",
    )
    parser.add_argument(
        "--competing-score",
        type=float,
        default=None,
        help="Hardcoded competing L2 score override (default: audited from live CLOB REST L2 depth).",
    )
    parser.add_argument(
        "--auto-merge-threshold",
        type=float,
        default=10.0,
        help="Threshold of complete sets before triggering CTF Merge on Polygon (default: 10.0).",
    )
    parser.add_argument(
        "--legacy-mode",
        action="store_true",
        help="Run legacy single-token quoting (BUY YES + SELL YES) instead of Jane Street pure dual-cash bidding (BUY YES + BUY NO).",
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
        default="data/real_money_trading.db",
        help="SQLite database path for state persistence.",
    )
    parser.add_argument(
        "--ledger-path",
        type=str,
        default="data/real_money_trading_ledger.json",
        help="JSON ledger path for audit trail.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Disable ANSI terminal screen clearing for log-friendly streaming.",
    )
    parser.add_argument(
        "--scan-interval",
        "--switch-interval",
        dest="scan_interval",
        type=float,
        default=20.0,
        help="Continuous interval in seconds to scan the universe for high-yield opportunities (default: 20.0s).",
    )
    parser.add_argument(
        "--holding-horizon-hours",
        type=float,
        default=4.0,
        help="Economic holding horizon in hours for amortizing switching costs (default: 4.0h).",
    )
    parser.add_argument(
        "--disable-switching",
        action="store_true",
        help="Disable automatic opportunistic market switching.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Run in dry-run inspection mode without sending real orders or transactions.",
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        default=None,
        help="Polygon JSON-RPC endpoint URL override.",
    )
    return parser.parse_args()


def resolve_market_details(
    feed: LivePolymarketFeed,
    requested_cid: Optional[str] = None,
    requested_yes: Optional[str] = None,
    requested_no: Optional[str] = None,
    initial_cash: float = 150.0,
) -> dict[str, Any]:
    """Resolve condition ID, question, tokens, and rewards config from Polymarket CLOB."""
    log.info("Resolving market details from Polymarket CLOB API...")

    markets = feed.get_rewards_markets_sync()
    target_market: Optional[dict[str, Any]] = None

    def _get_rate(m: dict[str, Any]) -> float:
        cfg = m.get("rewards_config", [])
        if cfg and isinstance(cfg, list) and len(cfg) > 0:
            return float(cfg[0].get("rate_per_day", 0.0) or 0.0)
        return float(m.get("rewards_daily_rate", 0.0) or 0.0)

    competing_score_override: Optional[float] = None
    daily_reward_override: Optional[float] = None
    if requested_cid:
        for m in markets:
            if m.get("condition_id", "").lower() == requested_cid.lower():
                target_market = m
                break
    else:
        log.info(f"Auto-scanning Polymarket universe for optimal high-ROI sweet spot market for ${initial_cash:.2f}...")
        try:
            from polymarketbot.rewards.scanner import find_optimal_lp_markets
            optimal_markets = find_optimal_lp_markets(bankroll_usd=initial_cash, limit_output=5)
            if optimal_markets:
                best_opt = optimal_markets[0]
                target_market = best_opt.get("raw_market")
                competing_score_override = best_opt.get("competing_score")
                daily_reward_override = best_opt.get("daily_pool")
                log.info(
                    f"[OPTIMAL SWEET SPOT DETECTED] Auto-selected: '{best_opt['question']}' "
                    f"(Pool: ${best_opt['daily_pool']:,.2f}/day, Competing Depth: {best_opt['qualifying_shares']:,.0f} sh, "
                    f"Expected Real Payout: ${best_opt['real_daily_payout']:.2f}/day, ROI: +{best_opt['daily_roi_pct']:.1f}%/day)"
                )
        except Exception as e:
            log.warning(f"Optimal market scan fallback to raw rate sort: {e}")

    if not target_market and markets:
        markets_sorted = sorted(markets, key=_get_rate, reverse=True)
        for m in markets_sorted:
            if m.get("closed") is True or m.get("active") is False:
                continue
            tokens = m.get("tokens", [])
            if len(tokens) >= 2 and tokens[0].get("token_id"):
                target_market = m
                break

    cid = requested_cid or (target_market.get("condition_id") if target_market else None) or "0x221319cd...demo"
    question = (target_market.get("question") if target_market else None) or f"Market Condition {cid[:14]}..."
    daily_reward = daily_reward_override if daily_reward_override is not None else (_get_rate(target_market) if target_market else 25.0)
    max_spread = float(target_market.get("rewards_max_spread", 1.0)) if target_market else 1.0
    min_size = float(target_market.get("rewards_min_size", 20.0)) if target_market else 20.0

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

    tok_yes = tok_yes or f"{cid}_YES"
    tok_no = tok_no or f"{cid}_NO"

    market_spread_cents = float(target_market.get("spread", 0.01) or 0.01) * 100.0 if target_market else 1.0
    tick_size_cents = float(target_market.get("tick_size", 0.01) or 0.01) * 100.0 if target_market else 1.0

    q_lower = question.lower()
    if any(k in q_lower for k in ["senate", "house", "president", "election", "democrat", "republican", "tax", "vote"]):
        category = "politics"
    elif any(k in q_lower for k in ["btc", "eth", "crypto", "solana"]):
        category = "crypto"
    elif any(k in q_lower for k in ["nba", "nfl", "champion", "match", "cup"]):
        category = "sports"
    else:
        category = "default"

    if competing_score_override is None and tok_yes and max_spread:
        try:
            from polymarketbot.rewards.scanner import get_real_l2_score
            comp_score, _, _ = get_real_l2_score(feed.session, tok_yes, max_spread)
            if comp_score > 0:
                competing_score_override = comp_score
                log.info(f"[L2 DEPTH AUDIT] Audited live L2 competing score for {tok_yes[:14]}: {comp_score:,.1f}")
        except Exception as e:
            log.warning(f"L2 audit fallback: {e}")

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
        "competing_score_override": competing_score_override,
    }


def render_dashboard(
    engine: RealMoneyTradingEngine,
    market_info: dict[str, Any],
    start_time: float,
    clear_screen: bool = True,
    migration_state: Optional[dict[str, Any]] = None,
) -> None:
    """Render high-density, real-time terminal telemetry dashboard for Real-Money engine."""
    t = engine.get_telemetry()
    elapsed = time.time() - start_time
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    mode_str = f"{C_GREEN}[LIVE REAL-MONEY TRADING]{C_RESET}" if not engine.dry_run else f"{C_YELLOW}[DRY-RUN / SAFETY INSPECTION]{C_RESET}"
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

    # Endogenous Reserve & Compounding APR
    reserve_cash = t.get("reserve_cash_usd", t["cash"])
    reserve_pct = t.get("reserve_ratio_pct", 100.0)
    nom_apr = t.get("nominal_apr_pct", 0.0)
    comp_apr = t.get("compounding_apr_pct", 0.0)
    v_queue = t.get("resting_queue_value_usd", 0.0)
    mig_count = t.get("migration_count", 0)

    lines = []
    if clear_screen:
        lines.append("\033[H\033[J")

    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}{C_WHITE}[MM] POLYMARKET REAL-MONEY TRADING ENGINE | CLOB & ON-CHAIN CTF {mode_str}{C_RESET}")
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

    lines.append(f"{C_BOLD}[WALLET & BALANCES] LIVE WALLET & CLOB ACCOUNTING{C_RESET}")
    lines.append(
        f"  {C_BOLD}USDC Balance:{C_RESET}   {equity_color}${t['cash']:>8.2f}{C_RESET}    "
        f"{C_BOLD}POL Gas Balance:{C_RESET}{t['pol_balance']:>8.4f} POL  "
        f"{C_BOLD}Total Equity:{C_RESET}   {equity_color}${t['equity']:>8.2f}{C_RESET}"
    )
    lines.append(
        f"  {C_BOLD}Shares YES:{C_RESET}     {t['shares_yes']:>8.2f}    "
        f"{C_BOLD}Shares NO:{C_RESET}       {t['shares_no']:>8.2f}        "
        f"{C_BOLD}Complete Sets:{C_RESET}  {t['complete_sets']:>8.2f}"
    )
    lines.append(
        f"  {C_BOLD}Net Exposure:{C_RESET}   {q_color}{net_q:>+8.2f}{C_RESET}    "
        f"{C_BOLD}Cash Reserve:{C_RESET}    ${reserve_cash:>8.2f} ({reserve_pct:.1f}%) "
        f"{C_BOLD}TOTAL PnL:{C_RESET}      {equity_color}${t['total_pnl']:>+8.4f}{C_RESET}"
    )
    comp_apr_str = f"+{comp_apr:.1f}%" if comp_apr < 10000.0 else ">9999%"
    lines.append(
        f"  {C_BOLD}Compounding APR:{C_RESET}{C_GREEN}+{nom_apr:.1f}% APR{C_RESET} "
        f"({C_GREEN}{comp_apr_str} APY compounded{C_RESET}) | "
        f"{C_BOLD}Resting Queue Value ($V_queue):{C_RESET} {C_CYAN}${v_queue:.4f}{C_RESET}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[DE-SKEW] 2-TIER DYNAMIC DE-SKEW OPTIMIZER{C_RESET}")
    lines.append(
        f"  {C_BOLD}Pathway:{C_RESET}        [{C_BOLD}{pathway:<16s}{C_RESET}]   "
        f"{C_BOLD}Urgency:{C_RESET} {urgency_color}{urgency:<10s}{C_RESET}   "
        f"{C_BOLD}Synthetic Savings:{C_RESET} {C_GREEN}+{savings:.2f}c/sh{C_RESET}"
    )
    if deskew.get("reason"):
        lines.append(f"  {C_DIM}Sentinel Action:{C_RESET} {deskew['reason'][:74]}")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[REWARDS] POLYMARKET QUADRATIC REWARDS (1,440 Daily Epochs){C_RESET}")
    user_score = sum(q.get("score", 0.0) for q in t["active_quotes"])
    lines.append(
        f"  {C_BOLD}Epoch Index:{C_RESET}    {t['epoch_index']:>4d} / 1440    "
        f"{C_BOLD}User Score:{C_RESET}     {user_score:>8.2f}   "
        f"{C_BOLD}Floor Status:{C_RESET}     [{floor_str}]"
    )
    lines.append(
        f"  {C_BOLD}Daily Pool:{C_RESET}     ${market_info['daily_reward']:>7.2f}     "
        f"{C_BOLD}Proj. Daily:{C_RESET}    ${proj_daily:>8.2f}   "
        f"{C_BOLD}Rewards Earned:{C_RESET}   {C_GREEN}${t['total_rewards']:>8.4f}{C_RESET}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[ON-CHAIN CTF] POLYGON POS SETTLEMENT & COMPLETE-SET MERGES{C_RESET}")
    lines.append(
        f"  {C_BOLD}Total Merges:{C_RESET}   {t['total_merges']:>4d}          "
        f"{C_BOLD}Total Splits:{C_RESET}   {t['total_splits']:>4d}          "
        f"{C_BOLD}Polygon Gas Fees:{C_RESET} ${t['total_gas_fees']:>8.3f}"
    )
    lines.append(
        f"  {C_BOLD}Total Fills:{C_RESET}    {t['fills_count']:>4d}          "
        f"{C_BOLD}Traded Volume:{C_RESET}  ${t['total_volume_usd']:>8.2f}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    # Migration hurdle status
    hurdle_info = migration_state or {}
    sec_rem = hurdle_info.get("next_scan_remaining", 300.0)
    m_a_y = hurdle_info.get("y_net_a", t.get("daily_yield_usd", proj_daily))
    m_b_y = hurdle_info.get("y_net_b", 0.0)
    hurdle_st = hurdle_info.get("hurdle_status", "MONITORING")
    st_color = C_GREEN if hurdle_st == "POSITIVE ALPHA" else (C_YELLOW if hurdle_st == "HURDLE REJECTED" else C_CYAN)
    cand_q = hurdle_info.get("candidate_question", "None")
    cost_val = hurdle_info.get("switching_cost", 0.0)
    reason_txt = hurdle_info.get("decision_reason", "Awaiting initial 300s opportunity scan")

    lines.append(f"{C_BOLD}[MIGRATION] DYNAMIC CAPITAL MIGRATION & HURDLE TELEMETRY{C_RESET}")
    lines.append(
        f"  {C_BOLD}Next Scan:{C_RESET}     {sec_rem:>5.1f}s / 300s   "
        f"{C_BOLD}Total Migrations:{C_RESET} {mig_count:>2d}   "
        f"{C_BOLD}Hurdle Status:{C_RESET}  [{st_color}{hurdle_st}{C_RESET}]"
    )
    lines.append(
        f"  {C_BOLD}Active Yield (M_A):{C_RESET} ${m_a_y:>6.2f}/d   "
        f"{C_BOLD}Candidate Yield (M_B):{C_RESET} ${m_b_y:>6.2f}/d   "
        f"{C_BOLD}Switching Cost:{C_RESET}  ${cost_val:>6.4f}"
    )
    if cand_q and cand_q != "None":
        lines.append(f"  {C_DIM}Top Candidate:{C_RESET}  {cand_q[:72]}")
    if reason_txt:
        lines.append(f"  {C_DIM}Hurdle Audit:{C_RESET}   {reason_txt[:72]}")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[CLOB QUOTES] ACTIVE SIGNED ORDERS ON POLYMARKET CLOB{C_RESET}")
    lines.append(f"  {'Side':<6s} {'Outcome':<8s} {'Price':<8s} {'Size':<8s} {'Order ID':<20s} {'Score S(v,s)':<14s} {'Status'}")
    if t["active_quotes"]:
        for q in t["active_quotes"]:
            side_color = C_GREEN if q["side"] == "BUY" else C_RED
            ord_id = q.get("order_id", "N/A")[:18]
            lines.append(
                f"  {side_color}{q['side']:<6s}{C_RESET} "
                f"{q.get('token_side', 'YES'):<8s} "
                f"${q['price']:<7.3f} "
                f"{q['size']:<8.1f} "
                f"{ord_id:<20s} "
                f"{q['score']:<14.4f} "
                f"{C_GREEN}{q.get('status', 'LIVE')}{C_RESET}"
            )
    else:
        lines.append(f"  {C_DIM}No active resting orders on CLOB.{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


async def run_real_money_trader_async(args: argparse.Namespace) -> int:
    """Async main execution loop for Polymarket Real-Money Trading Engine."""
    feed = LivePolymarketFeed(fallback_poll_interval=2.0)
    market = resolve_market_details(
        feed=feed,
        requested_cid=args.condition_id,
        requested_yes=args.token_yes,
        requested_no=args.token_no,
        initial_cash=args.initial_cash,
    )

    daily_rew = args.daily_reward if args.daily_reward is not None else market["daily_reward"]
    max_spr = args.max_spread if args.max_spread is not None else market["max_spread"]
    min_sz = args.min_size if args.min_size is not None else market["min_size"]
    comp_score = args.competing_score if args.competing_score is not None else market.get("competing_score_override")

    # Sizing derivation
    if args.quote_size is not None:
        quote_sz = args.quote_size
    else:
        quote_sz = derive_quote_size(
            initial_cash=args.initial_cash,
            mid_price=market["mid_price"],
            min_size=min_sz,
        )

    # Spread optimization
    if args.spread_offset is not None:
        spread_off = args.spread_offset
    else:
        spread_off, _ = optimize_spread_offset(
            max_spread_cents=max_spr,
            market_spread_cents=market["market_spread_cents"],
            tick_size_cents=market.get("tick_size_cents", 1.0),
            daily_reward_pool=daily_rew,
            quote_size=quote_sz,
            category=market.get("category", "default"),
        )

    # Initialize clients
    creds = ClobAuthCredentials.from_env()
    dry_run = args.dry_run if args.dry_run is not None else (not creds.is_configured)

    clob_client = LiveClobClient(
        credentials=creds,
        dry_run=dry_run,
    )
    ctf_client = PolygonCtfClient(
        rpc_url=args.rpc_url,
        private_key=creds.private_key,
        dry_run=dry_run,
    )

    engine = RealMoneyTradingEngine(
        condition_id=market["condition_id"],
        asset_id_yes=market["token_yes"],
        asset_id_no=market["token_no"],
        clob_client=clob_client,
        ctf_client=ctf_client,
        initial_cash=args.initial_cash,
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
        dry_run=dry_run,
    )

    # Run Pre-flight verification
    preflight = engine.preflight_check()
    sys.stdout.write(f"\n{preflight.summary()}\n\n")
    sys.stdout.flush()

    if not preflight.ready_for_live and not dry_run:
        log.warning(
            "[PREFLIGHT CAUTION] Credentials or balances unverified for live trading. "
            "Switching engine to SAFE DRY-RUN mode."
        )
        engine.dry_run = True
        clob_client.dry_run = True
        ctf_client.dry_run = True

    # Register listeners
    def on_book(snapshot: Any) -> None:
        tok = "YES" if snapshot.asset_id == engine.asset_id_yes else ("NO" if snapshot.asset_id == engine.asset_id_no else None)
        if tok:
            engine.on_book_update(tok, snapshot.bids, snapshot.asks)

    def on_trade(trade: Any) -> None:
        tok = "YES" if trade.asset_id == engine.asset_id_yes else ("NO" if trade.asset_id == engine.asset_id_no else None)
        if tok:
            engine.on_trade_update(tok, trade.price, trade.size, trade.side, trade.timestamp)

    feed.add_book_listener(on_book)
    feed.add_trade_listener(on_trade)
    feed.subscribe(market["token_yes"])
    feed.subscribe(market["token_no"])

    # Initial snapshot
    try:
        bk_yes = feed.get_book_sync(market["token_yes"])
        if bk_yes and (bk_yes.bids or bk_yes.asks):
            engine.on_book_update("YES", bk_yes.bids, bk_yes.asks)
        bk_no = feed.get_book_sync(market["token_no"])
        if bk_no and (bk_no.bids or bk_no.asks):
            engine.on_book_update("NO", bk_no.bids, bk_no.asks)
    except Exception as e:
        log.warning(f"Initial order book sync warning: {e}")

    await feed.start()
    log.info("Live Polymarket Feed connected.")

    start_time = time.time()
    last_epoch_check = time.time()
    last_render_time = 0.0
    running = True

    def handle_signal(*_: Any) -> None:
        nonlocal running
        log.info("Shutdown signal received: cancelling all resting CLOB quotes...")
        running = False
        engine.cancel_all_quotes()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handle_signal)
        except Exception:
            pass

    try:
        while running:
            now = time.time()

            # Duration check
            if args.duration and (now - start_time) >= args.duration:
                log.info(f"Target duration of {args.duration:.1f}s reached.")
                break

            # 60s reward sampling epoch
            if now - last_epoch_check >= 60.0:
                engine.process_reward_epoch(timestamp=now)
                last_epoch_check = now

            # Dashboard render
            if now - last_render_time >= args.poll_interval:
                render_dashboard(
                    engine=engine,
                    market_info=market,
                    start_time=start_time,
                    clear_screen=not args.no_clear,
                )
                last_render_time = now

            await asyncio.sleep(0.2)

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown requested by operator or system interrupt.")
    finally:
        engine.cancel_all_quotes()
        engine.flush_ledger()
        engine.close()
        await feed.stop()
        log.info("Real-Money Trading Engine safely shut down.")

    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(run_real_money_trader_async(args))


if __name__ == "__main__":
    sys.exit(main())
