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
        "--bankrolls",
        type=str,
        default=None,
        help="Optional comma-separated list of bankrolls (e.g. '70,500,1000,2000,5000,10000') to run in simultaneous multi-tier mode.",
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
        help="Threshold of complete sets before triggering CTF Merge (default: 10.0).",
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
        "--min-residency-seconds",
        type=float,
        default=180.0,
        help="Minimum duration in seconds to quote a market before migrating to prevent churn (default: 180.0s).",
    )
    parser.add_argument(
        "--disable-switching",
        action="store_true",
        help="Disable automatic opportunistic market switching.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume starting cash balance and cumulative state from existing ledger.",
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
        # Fallback sorting
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

    # If competing score override is not yet known, audit live L2 depth directly
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
    engine: NativePaperTradingEngine,
    market_info: dict[str, Any],
    start_time: float,
    clear_screen: bool = True,
    migration_state: Optional[dict[str, Any]] = None,
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

    # Endogenous Reserve & Compounding APR
    reserve_cash = t.get("reserve_cash_usd", t["cash"])
    reserve_pct = t.get("reserve_ratio_pct", (t["cash"] / max(1e-4, t["equity"])) * 100.0)
    nom_apr = t.get("nominal_apr_pct", 0.0)
    comp_apr = t.get("compounding_apr_pct", 0.0)
    v_queue = t.get("resting_queue_value_usd", 0.0)
    mig_count = t.get("migration_count", 0)

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
        f"{C_BOLD}Cash Reserve:{C_RESET}  ${reserve_cash:>8.2f} ({reserve_pct:.1f}% buffer) "
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
    lines.append(
        f"  {C_BOLD}Compounding APR:{C_RESET} {C_GREEN}+{nom_apr:.1f}% APR{C_RESET} "
        f"({C_GREEN}+{comp_apr:.1f}% APY compounded{C_RESET}) | "
        f"{C_BOLD}Resting Queue Value ($V_queue):{C_RESET} {C_CYAN}${v_queue:.4f}{C_RESET}"
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
        f"  {C_BOLD}Next Scan:{C_RESET}    {sec_rem:>5.1f}s / 300s   "
        f"{C_BOLD}Total Migrations:{C_RESET} {mig_count:>2d}   "
        f"{C_BOLD}Hurdle Status:{C_RESET} [{st_color}{hurdle_st}{C_RESET}]"
    )
    lines.append(
        f"  {C_BOLD}Active Yield (M_A):{C_RESET} ${m_a_y:>6.2f}/d   "
        f"{C_BOLD}Candidate Yield (M_B):{C_RESET} ${m_b_y:>6.2f}/d   "
        f"{C_BOLD}Switching Cost:{C_RESET} ${cost_val:>6.4f}"
    )
    if cand_q and cand_q != "None":
        lines.append(f"  {C_DIM}Top Candidate:{C_RESET} {cand_q[:72]}")
    if reason_txt:
        lines.append(f"  {C_DIM}Hurdle Audit:{C_RESET}  {reason_txt[:72]}")

    # Scanner Cache & Health Telemetry
    try:
        from polymarketbot.rewards.scanner import get_scanner_telemetry
        s_telemetry = get_scanner_telemetry()
        s_scans = s_telemetry.total_scans
        s_hits = s_telemetry.cache_hits
        s_ratio = (s_hits / (s_scans or 1)) * 100.0
        err_429 = s_telemetry.http_429_errors
        net_err = s_telemetry.network_errors
        err_color = C_GREEN if (err_429 == 0 and net_err == 0) else C_RED
        lines.append(
            f"  {C_BOLD}Scanner Health:{C_RESET} {s_scans} scans | "
            f"{C_GREEN}{s_hits} cache hits ({s_ratio:.0f}% 5s TTL){C_RESET} | "
            f"{err_color}HTTP 429: {err_429}{C_RESET} | "
            f"{err_color}Net Errs: {net_err}{C_RESET}"
        )
    except Exception:
        pass
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")


    lines.append(f"{C_BOLD}[QUOTES] ACTIVE RESTING VIRTUAL QUOTES (FIFO Queue & Quadratic Scoring){C_RESET}")
    lines.append(f"  {'Side':<6s} {'Outcome':<8s} {'Price':<8s} {'Size':<8s} {'Queue Ahead':<14s} {'Score S(v,s)':<14s} {'$V_queue':<10s} {'Status'}")
    if t["active_quotes"]:
        for q in t["active_quotes"]:
            side_color = C_GREEN if q["side"] == "BUY" else C_RED
            vq_val = q.get("v_queue", 0.0)
            lines.append(
                f"  {side_color}{q['side']:<6s}{C_RESET} "
                f"{q.get('token_side', 'YES'):<8s} "
                f"${q['price']:<7.3f} "
                f"{q['size']:<8.1f} "
                f"{q['queue_ahead']:<14.1f} "
                f"{q['score']:<14.4f} "
                f"${vq_val:<9.4f} "
                f"{C_GREEN}{q.get('status', 'ACTIVE')}{C_RESET}"
            )
    else:
        lines.append(f"  {C_DIM}No active resting quotes in book.{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


async def run_paper_trading_async(args: argparse.Namespace) -> int:
    """Async main execution loop for native Polymarket paper trader with live WebSocket and REST streaming."""
    active_cash = args.initial_cash
    if getattr(args, "resume", False) and Path(args.ledger_path).exists():
        try:
            with open(args.ledger_path, "r", encoding="utf-8") as f:
                prev_ledger = json.load(f)
                prev_port = prev_ledger.get("portfolio", {})
                prev_cash = float(prev_port.get("cash", 0.0))
                if prev_cash > 0.0:
                    active_cash = prev_cash
                    log.info(f"[RESUME ACTIVE SESSION] Resuming from ledger {args.ledger_path} with starting cash: ${active_cash:.2f}")
        except Exception as e:
            log.warning(f"Resume ledger load warning: {e}")

    feed = LivePolymarketFeed(fallback_poll_interval=2.0)
    market = resolve_market_details(
        feed=feed,
        requested_cid=args.condition_id,
        requested_yes=args.token_yes,
        requested_no=args.token_no,
        initial_cash=active_cash,
    )

    daily_rew = args.daily_reward if args.daily_reward is not None else market["daily_reward"]
    max_spr = args.max_spread if args.max_spread is not None else market["max_spread"]
    min_sz = args.min_size if args.min_size is not None else market["min_size"]
    comp_score = args.competing_score if args.competing_score is not None else market.get("competing_score_override")

    # 1. Dynamically derive quote size from initial cash if not explicitly passed
    if args.quote_size is not None:
        quote_sz = args.quote_size
        log.info(f"Using manual quote size override: {quote_sz:.1f} shares")
    else:
        quote_sz = derive_quote_size(
            initial_cash=active_cash,
            mid_price=market["mid_price"],
            min_size=min_sz,
        )
        log.info(
            f"[DYNAMIC SIZING] Derived quote size from ${active_cash:.2f} initial cash: "
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

    migration_engine = CapitalMigrationEngine()
    engine = NativePaperTradingEngine(
        condition_id=market["condition_id"],
        asset_id_yes=market["token_yes"],
        asset_id_no=market["token_no"],
        initial_cash=active_cash,
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
        migration_engine=migration_engine,
    )

    # 1. Register live book listener
    def on_book_update(snapshot: Any) -> None:
        if snapshot.asset_id == engine.asset_id_yes:
            tok_side = "YES"
        elif snapshot.asset_id == engine.asset_id_no:
            tok_side = "NO"
        else:
            return
        engine.update_orderbook_snapshot(
            token_side=tok_side,
            bids=snapshot.bids,
            asks=snapshot.asks,
        )
        engine.evaluate_quotes()

    feed.add_book_listener(on_book_update)

    # 2. Register live trade listener (drains queue_ahead and executes fills!)
    def on_trade_update(trade: Any) -> None:
        if trade.asset_id == engine.asset_id_yes:
            tok_side = "YES"
        elif trade.asset_id == engine.asset_id_no:
            tok_side = "NO"
        else:
            return
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
    market_entry_time = time.time()
    last_epoch_check = time.time()
    last_l2_audit_time = time.time()
    last_opportunity_scan_time = time.time()
    last_render_time = 0.0
    running = True

    migration_state: dict[str, Any] = {
        "next_scan_remaining": getattr(args, "scan_interval", 60.0),
        "hurdle_status": "MONITORING",
        "y_net_a": 0.0,
        "y_net_b": 0.0,
        "switching_cost": 0.0,
        "candidate_question": "None",
        "decision_reason": f"Next scan in {getattr(args, 'scan_interval', 60.0):.0f}s",
    }

    def handle_shutdown_signal(sig: int, frame: Any) -> None:
        nonlocal running
        sig_name = "SIGINT" if sig == signal.SIGINT else (
            "SIGTERM" if hasattr(signal, "SIGTERM") and sig == signal.SIGTERM else str(sig)
        )
        log.warning(f"Shutdown signal ({sig_name}) received. Gracefully terminating execution loop...")
        running = False

    signal.signal(signal.SIGINT, handle_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handle_shutdown_signal)
        except (ValueError, OSError):
            pass

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

                # Periodic 300-second live L2 depth refresh
                if (loop_now - last_l2_audit_time) >= 300.0:
                    try:
                        from polymarketbot.rewards.scanner import get_real_l2_score
                        new_comp_score, _, _ = get_real_l2_score(feed.session, market["token_yes"], max_spr)
                        if new_comp_score > 0:
                            engine.competing_score_override = new_comp_score
                            log.info(f"[L2 DEPTH REFRESH] Updated live competing score: {new_comp_score:,.1f}")
                    except Exception as e:
                        log.debug(f"Periodic L2 audit error: {e}")
                    last_l2_audit_time = loop_now

                # Instant Cooldown Scanner Trigger: when market enters hold or volatility cooldown, immediately trigger scan
                if hasattr(engine, "pop_instant_scan_trigger") and engine.pop_instant_scan_trigger():
                    log.warning(
                        "[INSTANT COOLDOWN SCANNER TRIGGERED] Volatility cooldown / hold active on current market. "
                        "Triggering immediate opportunity rescan for dynamic market rotation..."
                    )
                    last_opportunity_scan_time = 0.0

                # Continuous endogenous opportunity scan & market switching evaluation
                scan_interval = getattr(args, "scan_interval", 20.0)
                time_since_last_scan = loop_now - last_opportunity_scan_time
                migration_state["next_scan_remaining"] = max(0.0, scan_interval - time_since_last_scan)

                if not getattr(args, "disable_switching", False) and time_since_last_scan >= scan_interval:
                    last_opportunity_scan_time = loop_now
                    try:
                        from polymarketbot.rewards.scanner import find_optimal_lp_markets
                        log.info(
                            f"[CONTINUOUS OPPORTUNITY SCANNER] Scanning universe (Scan Interval: {scan_interval:.0f}s)..."
                        )
                        candidates = find_optimal_lp_markets(
                            bankroll_usd=engine.portfolio.cash,
                            limit_output=5,
                        )
                        other_candidates = [
                            c for c in candidates
                            if str(c.get("condition_id", "")).lower() != str(engine.condition_id).lower()
                        ]
                        if other_candidates:
                            top_cand = other_candidates[0]
                            y_net_b = float(top_cand.get("real_daily_payout", 0.0) or top_cand.get("raw_daily_payout", 0.0))
                            y_net_a = engine.get_current_daily_yield()

                            # Compute exact switching friction (incorporating real-time V_queue) & evaluate Net Alpha Hurdle
                            holding_h = getattr(args, "holding_horizon_hours", 4.0)
                            should_switch, decision, cost = engine.evaluate_migration_hurdle(
                                candidate_daily_yield=y_net_b,
                                holding_horizon_hours=holding_h,
                                daily_yield_a=y_net_a,
                            )

                            migration_state["y_net_a"] = y_net_a
                            migration_state["y_net_b"] = y_net_b
                            migration_state["candidate_question"] = top_cand.get("question", "")
                            migration_state["switching_cost"] = cost.total_switching_cost_usd
                            migration_state["hurdle_status"] = "POSITIVE ALPHA" if should_switch else "HURDLE REJECTED"
                            migration_state["decision_reason"] = decision.reason
                            # Enforce minimum residency duration before executing migration
                            time_in_market = loop_now - market_entry_time
                            min_residency = getattr(args, "min_residency_seconds", 180.0)
                            if should_switch and time_in_market < min_residency:
                                is_cooldown = getattr(engine, "is_volatility_cooldown_active", False) or (
                                    loop_now < getattr(engine, "_volatility_cooldown_until", 0.0)
                                )
                                if is_cooldown:
                                    log.warning(
                                        f"[RESIDENCY BYPASS ON HOLD] Active market is in volatility cooldown "
                                        f"({getattr(engine, '_volatility_cooldown_until', 0.0) - loop_now:.1f}s remaining). "
                                        f"Bypassing minimum residency hold ({time_in_market:.0f}s < {min_residency:.0f}s) to rotate capital!"
                                    )
                                else:
                                    should_switch = False
                                    migration_state["hurdle_status"] = "RESIDENCY HOLD"
                                    migration_state["decision_reason"] = (
                                        f"Minimum residency {time_in_market:.0f}s < {min_residency:.0f}s (Holding to earn rewards)"
                                    )


                            if should_switch:
                                log.warning(f"[MARKET MIGRATION TRIGGERED] {decision.reason}")
                                market_entry_time = loop_now
                                # 1. Unsubscribe old tokens from live feed
                                feed.unsubscribe([market["token_yes"], market["token_no"]])

                                # 2. Extract new candidate parameters
                                new_cid = top_cand["condition_id"]
                                new_yes = top_cand.get("yes_token") or f"{new_cid}_YES"
                                new_no = top_cand.get("no_token") or f"{new_cid}_NO"
                                new_daily_reward = float(top_cand.get("daily_pool", 20.0))
                                new_max_spread = float(top_cand.get("max_spread", 1.0))
                                new_min_size = float(top_cand.get("min_size", 20.0))
                                new_comp_score = top_cand.get("competing_score")

                                # Dynamically derive new quote size and spread offset
                                new_quote_sz = derive_quote_size(
                                    initial_cash=engine.portfolio.cash,
                                    mid_price=float(top_cand.get("real_mid", 0.50)),
                                    min_size=new_min_size,
                                )
                                new_spread_off, _ = optimize_spread_offset(
                                    max_spread_cents=new_max_spread,
                                    market_spread_cents=1.0,
                                    tick_size_cents=1.0,
                                    daily_reward_pool=new_daily_reward,
                                    quote_size=new_quote_sz,
                                    category="default",
                                )

                                # 3. Clean market migration in engine (cancel quotes on M_A, merge complete sets on M_A, register M_B)
                                engine.migrate_to_market(
                                    new_condition_id=new_cid,
                                    new_asset_id_yes=new_yes,
                                    new_asset_id_no=new_no,
                                    new_daily_reward_rate=new_daily_reward,
                                    new_rewards_max_spread_cents=new_max_spread,
                                    new_rewards_min_size=new_min_size,
                                    new_competing_score=new_comp_score,
                                    new_quote_size=new_quote_sz,
                                    new_spread_offset_cents=new_spread_off,
                                    reason=decision.reason,
                                    timestamp=loop_now,
                                )

                                # 4. Update local market metadata
                                market["condition_id"] = new_cid
                                market["question"] = top_cand.get("question", f"Market {new_cid[:14]}")
                                market["token_yes"] = new_yes
                                market["token_no"] = new_no
                                market["daily_reward"] = new_daily_reward
                                market["max_spread"] = new_max_spread
                                market["min_size"] = new_min_size
                                market["mid_price"] = float(top_cand.get("real_mid", 0.50))

                                # 5. Subscribe new tokens and fetch initial order book snapshot
                                feed.subscribe([new_yes, new_no])
                                try:
                                    book_y = feed.get_book_sync(new_yes)
                                    if book_y and (book_y.bids or book_y.asks):
                                        engine.update_orderbook_snapshot("YES", book_y.bids, book_y.asks)
                                    book_n = feed.get_book_sync(new_no)
                                    if book_n and (book_n.bids or book_n.asks):
                                        engine.update_orderbook_snapshot("NO", book_n.bids, book_n.asks)
                                except Exception as e:
                                    log.warning(f"REST fetch for newly migrated market failed: {e}")

                                # 6. Start quoting M_B
                                engine.evaluate_quotes(timestamp=loop_now)
                                log.info(f"[MARKET MIGRATION COMPLETED] Actively quoting new market: {market['question'][:50]}")
                            else:
                                log.info(f"[MARKET MIGRATION AUDIT] Hurdle rejected: {decision.reason} | Maintaining M_A.")
                    except Exception as e:
                        log.warning(f"Opportunistic market scan error: {e}")

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
                render_cadence = 10.0 if args.no_clear else 1.0
                if (loop_now - last_render_time) >= render_cadence:
                    render_dashboard(
                        engine=engine,
                        market_info=market,
                        start_time=start_time,
                        clear_screen=not args.no_clear,
                        migration_state=migration_state,
                    )
                    last_render_time = loop_now

                # Flush persistence
                engine.flush_ledger()

            except Exception as e:
                log.error(f"Error in paper trading iteration: {e}")

            await asyncio.sleep(args.poll_interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Execution loop interrupted. Proceeding with clean shutdown...")
    finally:
        log.info("Session concluding. Initiating graceful shutdown sequence...")
        log.info("Canceling active virtual maker quotes...")
        engine.cancel_all_virtual_quotes()

        log.info("Executing mandatory liquidation of all open positions...")
        liq = engine.liquidate_all_positions()

        log.info("Stopping live feed...")
        await feed.stop()

        log.info("Flushing atomic ledger...")
        engine.flush_ledger()

        log.info("Flushing SQLite database WAL journal and closing connections...")
        engine.flush_wal()
        engine.close()

        # Write final settlement summary to log
        total_vol = liq.get("total_volume_traded_usd", getattr(engine.portfolio, "total_volume_traded_usd", 0.0))
        log.info(
            f"[SESSION FINAL SETTLEMENT] Initial: ${liq['initial_cash']:.2f} | "
            f"Final Cash: ${liq['final_cash_settlement']:.2f} | "
            f"Net Realized PnL: ${liq['total_realized_pnl']:+.4f} ({liq['net_return_pct']:+.2f}%) | "
            f"Total Volume: ${total_vol:.2f} | "
            f"Fills: {len(engine.fills_history)} | Open Inventory: {liq['final_open_positions_count']} shares"
        )

        # Print final 100% Cash-Settlement Report
        sys.stdout.write("\n" + "=" * 88 + "\n")
        sys.stdout.write("  [SESSION FINAL SETTLEMENT] 100% CASH-SETTLED AUDIT REPORT (ZERO OPEN INVENTORY)\n")
        sys.stdout.write("=" * 88 + "\n")
        sys.stdout.write(f"  Initial Bankroll:             ${liq['initial_cash']:.4f} USDC\n")
        sys.stdout.write(f"  Pre-Liquidation Equity:       ${liq['pre_liquidation_equity']:.4f} USDC (Mark-to-Mid)\n")
        sys.stdout.write(f"  Pre-Liquidation Cash:         ${liq['pre_liquidation_cash']:.4f} USDC\n")
        sys.stdout.write(f"  Shares YES Liquidated:        {liq['shares_yes_liquidated']:.2f} shares\n")
        sys.stdout.write(f"  Shares NO Liquidated:         {liq['shares_no_liquidated']:.2f} shares\n")
        sys.stdout.write(f"  Spread Crossing Friction:    -${liq['spread_crossing_loss']:.4f} USDC\n")
        sys.stdout.write(f"  Liquidation Gas Fees:        -${liq['total_gas_fees_liq']:.4f} USDC\n")
        sys.stdout.write(f"  Total Liquidation Costs:     -${liq['liquidation_costs_total']:.4f} USDC\n")
        sys.stdout.write("  " + "-" * 84 + "\n")
        sys.stdout.write(f"  FINAL CASH SETTLEMENT:        ${liq['final_cash_settlement']:.4f} USDC (100% Cash)\n")
        sys.stdout.write(f"  FINAL OPEN POSITIONS:         {liq['final_open_positions_count']} shares\n")
        sys.stdout.write(f"  NET REALIZED PnL:             ${liq['total_realized_pnl']:+.4f} USDC ({liq['net_return_pct']:+.2f}%)\n")
        sys.stdout.write("=" * 88 + "\n\n")
        sys.stdout.flush()
        log.info(f"Native paper trading completed. State saved to {args.db_path} and {args.ledger_path}.")

    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    if getattr(args, "bankrolls", None):
        from scripts.run_multi_tier_paper_trader import run_multi_tier_async
        try:
            return asyncio.run(run_multi_tier_async(args))
        except KeyboardInterrupt:
            log.info("Multi-tier process interrupted by keyboard signal. Exiting cleanly with code 0.")
            return 0
    try:
        return asyncio.run(run_paper_trading_async(args))
    except KeyboardInterrupt:
        log.info("Process interrupted by keyboard signal. Exiting cleanly with code 0.")
        return 0



if __name__ == "__main__":
    sys.exit(main())
