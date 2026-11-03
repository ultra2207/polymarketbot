#!/usr/bin/env python3
"""Multi-Tier Simultaneous Paper Trading Orchestrator for Polymarket CLOB.

Runs concurrent native paper trading portfolios across multiple bankroll tiers
within a single unified process, featuring:
1. Automatic Capital Diversification & Marginal Yield Elbow Allocation (MILP / KKT Water-Filling):
   - Lower tiers ($70) focus capital on 1-2 high-ROI pools clearing the $1.00 daily payout floor.
   - Higher tiers ($500 - $10,000) automatically diversify across multiple sweet-spot markets
     at each market's marginal yield elbow point, bypassing the single-market pool ceiling.
2. Single shared live WebSocket/REST market feed (zero duplicate connections/rate limits).
3. Shared 5-second in-memory opportunity scanner cache (zero redundant CLOB queries).
4. Dedicated independent portfolio ledgers, SQLite databases, and dynamic quote sizing for each engine.
5. Real-time comparative matrix dashboard displaying short-run ($70) vs diversified scale ($500 - $10k).
6. Mandatory 100% cash-settled liquidation audit for all tiers on session conclusion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.rewards.allocator import (
    LiquidityRewardsAllocator,
    MarketAllocation,
    calculate_endogenous_reserve,
)
from polymarketbot.rewards.queue_valuation import CapitalMigrationEngine
from polymarketbot.rewards.scanner import find_optimal_lp_markets, get_scanner_telemetry
from polymarketbot.simulator.engine import NativePaperTradingEngine
from polymarketbot.simulator.feed import LivePolymarketFeed
from polymarketbot.simulator.optimizer import derive_quote_size, optimize_spread_offset

log = get_logger("multi_tier_paper_trader")

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


@dataclass
class MarketInstance:
    """An active market trading instance within a tier's portfolio."""

    condition_id: str
    question: str
    token_yes: str
    token_no: str
    allocated_capital: float
    quote_size: float
    spread_offset_cents: float
    daily_reward_rate: float
    max_spread: float
    min_size: float
    competing_score: float
    engine: NativePaperTradingEngine
    db_path: Path
    ledger_path: Path
    market_entry_time: float


@dataclass
class TierContext:
    """Encapsulates state, engine portfolio, persistence, and telemetry for a single bankroll tier."""

    tier_name: str
    target_bankroll: float
    initial_cash: float
    reserve_cash: float
    markets: dict[str, MarketInstance]  # condition_id -> MarketInstance
    master_ledger_path: Path
    migration_state: dict[str, Any]

    @property
    def engine(self) -> NativePaperTradingEngine:
        """Backward-compatibility accessor returning the primary market engine."""
        if not self.markets:
            raise RuntimeError(f"Tier {self.tier_name} has no active market engines.")
        return next(iter(self.markets.values())).engine

    @property
    def db_path(self) -> Path:
        """Backward-compatibility accessor for primary DB path."""
        if self.markets:
            return next(iter(self.markets.values())).db_path
        return self.master_ledger_path.with_suffix(".db")

    @property
    def ledger_path(self) -> Path:
        """Backward-compatibility accessor for ledger path."""
        return self.master_ledger_path

    @property
    def market_entry_time(self) -> float:
        if self.markets:
            return next(iter(self.markets.values())).market_entry_time
        return time.time()

    def get_total_cash(self) -> float:
        return self.reserve_cash + sum(m.engine.portfolio.cash for m in self.markets.values())

    def get_total_equity(self) -> float:
        return self.reserve_cash + sum(m.engine.get_equity() for m in self.markets.values())

    def get_total_rewards(self) -> float:
        return sum(m.engine.portfolio.total_rewards_earned for m in self.markets.values())

    def get_total_pnl(self) -> float:
        return self.get_total_equity() - self.initial_cash

    def get_total_realized_pnl(self) -> float:
        return sum(m.engine.portfolio.total_realized_pnl for m in self.markets.values())

    def get_total_fills(self) -> int:
        return sum(getattr(m.engine, "fills_count", len(getattr(m.engine, "fills_history", []))) for m in self.markets.values())

    def get_total_merges(self) -> int:
        return sum(m.engine.portfolio.total_merges_count for m in self.markets.values())

    def get_total_user_score(self) -> float:
        return sum(
            sum(q.get("score", 0.0) for q in m.engine.get_telemetry()["active_quotes"])
            for m in self.markets.values()
        )

    def get_projected_daily_rewards(self) -> float:
        return sum(
            (m.engine.portfolio.total_rewards_earned / max(1, m.engine.epoch_index)) * 1440.0
            if m.engine.epoch_index > 0
            else m.engine.get_current_daily_yield()
            for m in self.markets.values()
        )

    def flush_master_ledger(self) -> None:
        """Atomically persist aggregated portfolio statement for this tier."""
        positions_map = {}
        market_summaries = []
        tot_mkt_val = 0.0
        tot_unrealized = 0.0

        for cid, m in self.markets.items():
            t = m.engine.get_telemetry()
            pos = m.engine.portfolio.get_position(cid)
            tot_mkt_val += t.get("market_value", 0.0)
            tot_unrealized += t.get("unrealized_pnl", 0.0)
            positions_map[cid] = {
                "condition_id": cid,
                "shares_yes": pos.shares_yes,
                "shares_no": pos.shares_no,
                "avg_cost_yes": pos.avg_cost_yes,
                "avg_cost_no": pos.avg_cost_no,
                "net_exposure": pos.net_exposure,
                "complete_sets": pos.complete_sets,
                "realized_pnl": pos.realized_pnl,
            }
            market_summaries.append({
                "condition_id": cid,
                "question": m.question,
                "allocated_capital": m.allocated_capital,
                "quote_size": m.quote_size,
                "cash": round(t.get("cash", m.engine.portfolio.cash), 4),
                "equity": round(t.get("equity", m.engine.get_equity()), 4),
                "rewards_earned": round(m.engine.portfolio.total_rewards_earned, 4),
                "daily_yield": round(m.engine.get_current_daily_yield(), 2),
                "active_quotes": t.get("active_quotes", []),
            })

        data = {
            "updated_at": time.time(),
            "tier_name": self.tier_name,
            "target_bankroll": self.target_bankroll,
            "portfolio": {
                "initial_cash": round(self.initial_cash, 4),
                "cash": round(self.get_total_cash(), 4),
                "market_value": round(tot_mkt_val, 4),
                "equity": round(self.get_total_equity(), 4),
                "unrealized_pnl": round(tot_unrealized, 4),
                "realized_pnl": round(self.get_total_realized_pnl(), 4),
                "total_rewards_earned": round(self.get_total_rewards(), 4),
                "total_gas_fees": round(sum(m.engine.portfolio.total_gas_fees for m in self.markets.values()), 4),
                "total_pnl": round(self.get_total_pnl(), 4),
                "total_merges_count": self.get_total_merges(),
                "total_splits_count": sum(m.engine.portfolio.total_splits_count for m in self.markets.values()),
                "total_volume_traded_usd": sum(m.engine.portfolio.total_volume_traded_usd for m in self.markets.values()),
                "positions": positions_map,
            },
            "active_markets_count": len(self.markets),
            "reserve_cash": round(self.reserve_cash, 4),
            "markets": market_summaries,
        }

        tmp_path = self.master_ledger_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.master_ledger_path)
        except Exception as e:
            log.error(f"Failed writing master ledger for {self.tier_name}: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simultaneous Multi-Tier Polymarket Native Paper Trading Orchestrator"
    )
    parser.add_argument(
        "--bankrolls",
        type=str,
        default="70,500,1000,2000,5000,10000",
        help="Comma-separated bankroll levels in USD (default: '70,500,1000,2000,5000,10000').",
    )
    parser.add_argument(
        "--condition-id",
        type=str,
        default=None,
        help="Polymarket condition ID to force across all tiers (default: auto-select diversified portfolios).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=600.0,
        help="Duration to run in seconds (default: 600.0s = 10 minutes per block).",
    )
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=30.0,
        help="Continuous opportunity scan interval in seconds (default: 30.0s).",
    )
    parser.add_argument(
        "--min-residency-seconds",
        type=float,
        default=180.0,
        help="Minimum duration in seconds to hold market before migrating (default: 180.0s).",
    )
    parser.add_argument(
        "--auto-merge-threshold",
        type=float,
        default=10.0,
        help="Threshold of complete sets before triggering CTF Merge (default: 10.0).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Loop tick sleep interval in seconds (default: 1.0s).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume state from existing per-tier ledger files if available.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Disable ANSI terminal screen clearing for log-friendly streaming.",
    )
    return parser.parse_args()


def resolve_starter_market(
    feed: LivePolymarketFeed,
    requested_cid: Optional[str] = None,
    lowest_bankroll: float = 70.0,
) -> dict[str, Any]:
    """Resolve initial high-ROI sweet spot market that works even for the lowest bankroll."""
    install_doh_fallback()
    log.info("Resolving starter market details from Polymarket CLOB API...")

    if requested_cid:
        url = "https://clob.polymarket.com/rewards/markets/multi"
        resp = feed.session.get(url, params={"condition_ids": requested_cid}, timeout=10.0)
        if resp.status_code == 200:
            mkts = resp.json().get("data", [])
            if mkts:
                target_mkt = mkts[0]
                q = target_mkt.get("question", "Custom Market")
                rew_cfg = target_mkt.get("rewards_config", [])
                daily_rew = float(rew_cfg[0].get("rate_per_day", 20.0)) if rew_cfg else 20.0
                max_spr = float(target_mkt.get("rewards_max_spread", 1.0))
                min_sz = float(target_mkt.get("rewards_min_size", 20.0))
                toks = target_mkt.get("tokens", [])
                tok_y = toks[0]["token_id"] if len(toks) > 0 else f"{requested_cid}_YES"
                tok_n = toks[1]["token_id"] if len(toks) > 1 else f"{requested_cid}_NO"
                mid = float(toks[0].get("price", 0.50)) if toks else 0.50
                return {
                    "condition_id": requested_cid,
                    "question": q,
                    "token_yes": tok_y,
                    "token_no": tok_n,
                    "daily_reward": daily_rew,
                    "max_spread": max_spr,
                    "min_size": min_sz,
                    "mid_price": mid,
                    "market_spread_cents": 1.0,
                    "tick_size_cents": 1.0,
                    "category": "default",
                }

    log.info(f"Scanning Polymarket universe for optimal starter market for ${lowest_bankroll:.2f}...")
    candidates = find_optimal_lp_markets(bankroll_usd=lowest_bankroll, limit_output=5)
    if not candidates:
        raise RuntimeError("No qualifying sweet-spot liquidity reward pools found on Polymarket CLOB.")

    top = candidates[0]
    log.info(
        f"[OPTIMAL STARTER MARKET] Selected: '{top['question']}' | "
        f"Daily Pool: ${top['daily_pool']:.2f} | Max Spread: {top['max_spread']:.1f}c | "
        f"Mid YES: ${top['mid_yes']:.3f} | ROI: +{top['daily_roi_pct']:.2f}%/day"
    )

    tok_y = top.get("yes_token") or f"{top['condition_id']}_YES"
    tok_n = top.get("no_token") or f"{top['condition_id']}_NO"

    return {
        "condition_id": top["condition_id"],
        "question": top["question"],
        "token_yes": tok_y,
        "token_no": tok_n,
        "daily_reward": float(top.get("daily_pool", 20.0)),
        "max_spread": float(top.get("max_spread", 1.0)),
        "min_size": float(top.get("min_size", 20.0)),
        "mid_price": float(top.get("mid_yes", 0.50)),
        "market_spread_cents": 1.0,
        "tick_size_cents": 1.0,
        "category": "default",
        "competing_score_override": top.get("competing_score"),
    }


def render_multi_tier_dashboard(
    tiers: list[TierContext],
    start_time: float,
    scan_interval: float,
    clear_screen: bool = True,
) -> None:
    """Render comprehensive comparative terminal dashboard across all simultaneous bankroll tiers."""
    elapsed = time.time() - start_time
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = []
    if clear_screen:
        lines.append("\033[H\033[J")

    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}{C_WHITE}[MULTI-TIER MM] POLYMARKET DIVERSIFIED MULTI-BANKROLL SIMULATOR ({len(tiers)} CONCURRENT TIERS){C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================================================{C_RESET}")
    lines.append(
        f"{C_DIM}Time:{C_RESET} {now_str} | "
        f"{C_DIM}Runtime:{C_RESET} {elapsed:.1f}s | "
        f"{C_BOLD}Diversification Engine:{C_RESET} Google OR-Tools SCIP MILP / KKT Water-Filling (Marginal Yield Elbow)"
    )
    lines.append(f"{C_CYAN}------------------------------------------------------------------------------------------------------------------------{C_RESET}")

    # Comparative Matrix Header
    lines.append(
        f"{C_BOLD}{'Bankroll Tier':<14s} {'Mkts':<6s} {'Allocated':<12s} {'Reserve':<10s} {'Equity':<11s} "
        f"{'Cash':<11s} {'Daily Proj':<13s} {'Rewards Earned':<16s} {'Net PnL':<12s} {'Floor':<8s} {'ROI/day'}{C_RESET}"
    )
    lines.append(
        f"{C_DIM}{'-'*14} {'-'*6} {'-'*12} {'-'*10} {'-'*11} {'-'*11} {'-'*13} {'-'*16} {'-'*12} {'-'*8} {'-'*8}{C_RESET}"
    )

    for ctx in tiers:
        tot_eq = ctx.get_total_equity()
        tot_cash = ctx.get_total_cash()
        tot_rew = ctx.get_total_rewards()
        tot_pnl = ctx.get_total_pnl()
        m_count = len(ctx.markets)
        tot_alloc = sum(m.allocated_capital for m in ctx.markets.values())

        c_eq = C_GREEN if tot_pnl >= 0 else C_RED
        proj_daily = ctx.get_projected_daily_rewards()
        floor_met = proj_daily >= (1.00 * max(1, m_count)) or ctx.get_total_user_score() >= 1.0
        floor_str = f"{C_GREEN}MET{C_RESET}" if floor_met else f"{C_YELLOW}UNMET{C_RESET}"

        daily_roi = (proj_daily / max(1.0, ctx.target_bankroll)) * 100.0
        pnl_str = f"{c_eq}${tot_pnl:>+7.4f}{C_RESET}"

        lines.append(
            f"{C_BOLD}{ctx.tier_name:<14s}{C_RESET} "
            f"{m_count:<6d} "
            f"${tot_alloc:>9.2f}  "
            f"${ctx.reserve_cash:>8.2f} "
            f"{c_eq}${tot_eq:>9.2f}{C_RESET} "
            f"${tot_cash:>9.2f} "
            f"${proj_daily:>9.2f}/d   "
            f"{C_GREEN}${tot_rew:>10.4f}{C_RESET}     "
            f"{pnl_str:<21s} "
            f"[{floor_str}]  "
            f"{C_GREEN}+{daily_roi:.2f}%/d{C_RESET}"
        )

    lines.append(f"{C_CYAN}------------------------------------------------------------------------------------------------------------------------{C_RESET}")

    # Detailed Portfolio Allocation Breakdown for Entry Tier ($70) vs Whale Tier ($10,000)
    entry_tier = tiers[0]
    whale_tier = tiers[-1]

    lines.append(f"{C_BOLD}[PORTFOLIO DIVERSIFICATION: ENTRY ({entry_tier.tier_name}) vs SCALE ({whale_tier.tier_name})]{C_RESET}")
    lines.append(
        f"  {C_BOLD}Starter Portfolio ({entry_tier.tier_name}):{C_RESET} "
        f"{len(entry_tier.markets)} Active Markets | Usable: ${sum(m.allocated_capital for m in entry_tier.markets.values()):.2f} | "
        f"Reserve: ${entry_tier.reserve_cash:.2f} | Fills: {entry_tier.get_total_fills()} | Merges: {entry_tier.get_total_merges()}"
    )
    for m in list(entry_tier.markets.values())[:2]:
        lines.append(
            f"    -> {C_CYAN}{m.question[:56]:<56s}{C_RESET} | Sized: ${m.allocated_capital:>6.2f} "
            f"({m.quote_size:.0f} sh) | Pool: ${m.daily_reward_rate:.0f}/d | Yield: ${m.engine.get_current_daily_yield():.2f}/d"
        )

    lines.append(
        f"  {C_BOLD}Whale Scale Portfolio ({whale_tier.tier_name}):{C_RESET} "
        f"{len(whale_tier.markets)} Active Markets | Usable: ${sum(m.allocated_capital for m in whale_tier.markets.values()):.2f} | "
        f"Reserve: ${whale_tier.reserve_cash:.2f} | Fills: {whale_tier.get_total_fills()} | Merges: {whale_tier.get_total_merges()}"
    )
    for m in list(whale_tier.markets.values())[:3]:
        lines.append(
            f"    -> {C_CYAN}{m.question[:56]:<56s}{C_RESET} | Sized: ${m.allocated_capital:>6.2f} "
            f"({m.quote_size:.0f} sh) | Pool: ${m.daily_reward_rate:.0f}/d | Yield: ${m.engine.get_current_daily_yield():.2f}/d"
        )
    if len(whale_tier.markets) > 3:
        lines.append(f"    ... and {len(whale_tier.markets) - 3} more diversified sweet-spot markets at marginal elbow points.")

    lines.append(f"{C_CYAN}------------------------------------------------------------------------------------------------------------------------{C_RESET}")

    # Shared Opportunity Scanner & Infrastructure Health
    s_telemetry = get_scanner_telemetry()
    s_scans = s_telemetry.total_scans
    s_hits = s_telemetry.cache_hits
    s_ratio = (s_hits / (s_scans or 1)) * 100.0
    err_429 = s_telemetry.http_429_errors
    net_err = s_telemetry.network_errors
    err_color = C_GREEN if (err_429 == 0 and net_err == 0) else C_RED

    lines.append(f"{C_BOLD}[SHARED SCANNER & INFRASTRUCTURE HEALTH]{C_RESET}")
    lines.append(
        f"  {C_BOLD}Scanner Activity:{C_RESET} {s_scans} scans | "
        f"{C_GREEN}{s_hits} cache hits ({s_ratio:.0f}% 5s TTL reuse across all {len(tiers)} tiers){C_RESET} | "
        f"{err_color}HTTP 429 Errors: {err_429}{C_RESET} | "
        f"{err_color}Network Errors: {net_err}{C_RESET}"
    )
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================================================{C_RESET}")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


async def run_multi_tier_async(args: argparse.Namespace) -> int:
    """Async orchestrator managing multiple simultaneous paper trading engines."""
    install_doh_fallback()
    bankroll_vals = [float(b.strip()) for b in args.bankrolls.split(",") if b.strip()]
    if not bankroll_vals:
        bankroll_vals = [70.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0]

    log.info(
        f"Initializing Multi-Tier Diversified Orchestrator with {len(bankroll_vals)} tiers: {bankroll_vals}"
    )

    feed = LivePolymarketFeed(fallback_poll_interval=2.0)
    allocator = LiquidityRewardsAllocator()
    migration_engine = CapitalMigrationEngine()

    # Query candidate sweet-spot markets
    log.info(f"Scanning Polymarket universe for candidate sweet-spot pools (max bankroll ${max(bankroll_vals):,.0f})...")
    candidates = find_optimal_lp_markets(bankroll_usd=max(bankroll_vals), limit_output=35)
    if not candidates:
        raise RuntimeError("No qualifying sweet-spot liquidity reward pools found on Polymarket CLOB.")

    log.info(f"Retrieved {len(candidates)} viable candidate pools. Allocating capital per tier at marginal elbow points...")

    tiers: list[TierContext] = []

    for b in bankroll_vals:
        tier_label = f"${int(b):,}" if b >= 1.0 else f"${b:.2f}"
        master_ledg_p = Path(f"data/native_paper_trading_ledger_{int(b)}.json")

        active_cash = b
        if args.resume and master_ledg_p.exists():
            try:
                with open(master_ledg_p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    p_cash = float(data.get("portfolio", {}).get("cash", 0.0))
                    if p_cash > 0:
                        active_cash = p_cash
                        log.info(f"[{tier_label}] Resumed active cash balance: ${active_cash:.2f}")
            except Exception as e:
                log.warning(f"Failed loading resume ledger for {tier_label}: {e}")

        # Optimize multi-market portfolio for this tier
        if args.condition_id:
            # Single forced market if user specified
            starter = resolve_starter_market(feed=feed, requested_cid=args.condition_id, lowest_bankroll=b)
            res_usd = calculate_endogenous_reserve(
                allocations=[{"capital": active_cash, "mid": starter["mid_price"]}],
                bankroll_usd=active_cash,
            )
            alloc_cap = max(10.0, round(active_cash - res_usd, 2))
            allocs = [
                MarketAllocation(
                    condition_id=starter["condition_id"],
                    question=starter["question"],
                    allocated_capital=alloc_cap,
                    quote_size=alloc_cap,
                    daily_reward_payout=round(starter["daily_reward"] * 0.1, 2),
                    daily_roi_pct=5.0,
                    min_floor_capital=20.0,
                    clears_floor=True,
                    daily_pool=starter["daily_reward"],
                    competing_depth=1000.0,
                    mid_price=starter["mid_price"],
                    tokens=[],
                    yes_token=starter["token_yes"],
                    no_token=starter["token_no"],
                    max_spread=starter["max_spread"],
                    min_size=starter["min_size"],
                )
            ]
        else:
            alloc_res = allocator.optimize_allocation_milp(
                candidates,
                bankroll_usd=active_cash,
                floor_target=1.35,
                use_endogenous_reserve=True,
            )
            allocs = alloc_res.allocations
            if not allocs:
                # Fallback to top candidate if usable capital is very low
                top = candidates[0]
                fallback_reserve = calculate_endogenous_reserve(
                    allocations=[{"capital": active_cash, "mid": float(top.get("mid_yes", 0.50))}],
                    bankroll_usd=active_cash,
                )
                alloc_cap = round(active_cash - fallback_reserve, 2)
                allocs = [
                    MarketAllocation(
                        condition_id=top["condition_id"],
                        question=top["question"],
                        allocated_capital=alloc_cap,
                        quote_size=round(alloc_cap / 1.0, 1),
                        daily_reward_payout=round(top.get("real_daily_payout", 2.0), 2),
                        daily_roi_pct=round(top.get("daily_roi_pct", 5.0), 2),
                        min_floor_capital=20.0,
                        clears_floor=True,
                        daily_pool=float(top.get("daily_pool", 20.0)),
                        competing_depth=float(top.get("competing_score", 1000.0)),
                        mid_price=float(top.get("mid_yes", 0.50)),
                        tokens=[],
                        yes_token=top.get("yes_token") or f"{top['condition_id']}_YES",
                        no_token=top.get("no_token") or f"{top['condition_id']}_NO",
                        max_spread=float(top.get("max_spread", 3.0)),
                        min_size=float(top.get("min_size", 20.0)),
                    )
                ]

        total_alloc_cap = sum(a.allocated_capital for a in allocs)
        reserve_cash = round(max(0.0, active_cash - total_alloc_cap), 2)

        log.info(
            f"[{tier_label}] Optimizer allocated ${total_alloc_cap:.2f} across {len(allocs)} markets "
            f"(Reserve: ${reserve_cash:.2f})"
        )

        tier_markets: dict[str, MarketInstance] = {}
        for a in allocs:
            cid = a.condition_id
            mkt_db = Path(f"data/native_paper_trading_{int(b)}_{cid[:8]}.db")
            mkt_ledg = Path(f"data/native_paper_trading_ledger_{int(b)}_{cid[:8]}.json")

            q_sz = derive_quote_size(
                initial_cash=a.allocated_capital,
                mid_price=a.mid_price,
                min_size=a.min_size,
                reserve_pct=0.0,  # Cash reserve is managed endogenously at the portfolio tier level
            )

            spread_off, _ = optimize_spread_offset(
                max_spread_cents=a.max_spread,
                market_spread_cents=1.0,
                tick_size_cents=1.0,
                daily_reward_pool=a.daily_pool,
                quote_size=q_sz,
                category="default",
            )

            tok_y = a.token_yes
            tok_n = a.token_no

            eng = NativePaperTradingEngine(
                condition_id=cid,
                asset_id_yes=tok_y,
                asset_id_no=tok_n,
                initial_cash=a.allocated_capital,
                quote_size=q_sz,
                spread_offset_cents=spread_off,
                daily_reward_rate=a.daily_pool,
                rewards_max_spread_cents=a.max_spread,
                rewards_min_size=a.min_size,
                auto_merge_threshold=args.auto_merge_threshold,
                db_path=mkt_db,
                ledger_path=mkt_ledg,
                institutional_mode=True,
                competing_score_override=a.competing_depth,
                migration_engine=migration_engine,
            )

            m_inst = MarketInstance(
                condition_id=cid,
                question=a.question,
                token_yes=tok_y,
                token_no=tok_n,
                allocated_capital=a.allocated_capital,
                quote_size=q_sz,
                spread_offset_cents=spread_off,
                daily_reward_rate=a.daily_pool,
                max_spread=a.max_spread,
                min_size=a.min_size,
                competing_score=a.competing_depth,
                engine=eng,
                db_path=mkt_db,
                ledger_path=mkt_ledg,
                market_entry_time=time.time(),
            )
            tier_markets[cid] = m_inst

        t_ctx = TierContext(
            tier_name=tier_label,
            target_bankroll=b,
            initial_cash=active_cash,
            reserve_cash=reserve_cash,
            markets=tier_markets,
            master_ledger_path=master_ledg_p,
            migration_state={
                "next_scan_remaining": args.scan_interval,
                "hurdle_status": "MONITORING",
                "y_net_a": 0.0,
                "y_net_b": 0.0,
            },
        )
        t_ctx.flush_master_ledger()
        tiers.append(t_ctx)

    # Build token dispatch table: token_id -> list of (TierContext, MarketInstance, token_side)
    token_to_listeners: dict[str, list[tuple[TierContext, MarketInstance, str]]] = {}
    for tier in tiers:
        for m in tier.markets.values():
            token_to_listeners.setdefault(m.token_yes, []).append((tier, m, "YES"))
            token_to_listeners.setdefault(m.token_no, []).append((tier, m, "NO"))

    # Register live feed event listeners
    def on_book_update(snapshot: Any) -> None:
        listeners = token_to_listeners.get(snapshot.asset_id, [])
        for tier, m, tok_side in listeners:
            m.engine.update_orderbook_snapshot(
                token_side=tok_side,
                bids=snapshot.bids,
                asks=snapshot.asks,
            )
            m.engine.evaluate_quotes()

    feed.add_book_listener(on_book_update)

    def on_trade_update(trade: Any) -> None:
        listeners = token_to_listeners.get(trade.asset_id, [])
        for tier, m, tok_side in listeners:
            fills = m.engine.process_public_trade(
                token_side=tok_side,
                price=trade.price,
                size=trade.size,
                side=trade.side,
                timestamp=trade.timestamp,
            )
            if fills:
                m.engine.evaluate_quotes()

    feed.add_trade_listener(on_trade_update)

    # Subscribe all unique tokens across all active markets
    all_tokens = list(token_to_listeners.keys())
    log.info(f"Subscribing shared feed to {len(all_tokens)} unique tokens across {len(tiers)} tiers...")
    for tok_id in all_tokens:
        feed.subscribe(tok_id)

    # Pre-warm book snapshots via REST
    for tok_id, listeners in token_to_listeners.items():
        try:
            b_snap = feed.get_book_sync(tok_id)
            if b_snap and (b_snap.bids or b_snap.asks):
                for tier, m, tok_side in listeners:
                    m.engine.update_orderbook_snapshot(tok_side, b_snap.bids, b_snap.asks)
        except Exception as e:
            log.warning(f"Initial REST book fetch for {tok_id[:16]}: {e}")

    await feed.start()
    log.info(f"Live feed engaged for {len(tiers)} simultaneous bankroll tiers across {len(all_tokens) // 2} markets.")

    start_time = time.time()
    last_epoch_check = time.time()
    last_opportunity_scan_time = time.time()
    last_render_time = 0.0
    running = True

    def handle_shutdown(sig: int, frame: Any) -> None:
        nonlocal running
        log.warning(f"Shutdown signal received. Gracefully concluding all {len(tiers)} tiers...")
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handle_shutdown)
        except (ValueError, OSError):
            pass

    try:
        while running:
            loop_now = time.time()
            if args.duration and (loop_now - start_time) >= args.duration:
                log.info(f"Requested duration ({args.duration}s) reached. Stopping.")
                break

            try:
                # 1. Instant scan check (e.g. volatility cooldown on any market)
                instant_scan_needed = False
                for tier in tiers:
                    for m in tier.markets.values():
                        if m.engine.pop_instant_scan_trigger():
                            instant_scan_needed = True

                if instant_scan_needed:
                    log.warning(
                        "[INSTANT COOLDOWN SCANNER TRIGGERED] Volatility cooldown active on one or more markets. "
                        "Triggering immediate opportunity rescan for dynamic rotation..."
                    )
                    last_opportunity_scan_time = 0.0

                # 2. Periodic 60-second reward epoch calculation
                if (loop_now - last_epoch_check) >= 60.0:
                    for tier in tiers:
                        for m in tier.markets.values():
                            m.engine.process_reward_epoch(timestamp=loop_now)
                        tier.flush_master_ledger()
                    last_epoch_check = loop_now

                # 3. Continuous opportunity scanning (shared 5s TTL cache)
                time_since_scan = loop_now - last_opportunity_scan_time
                if time_since_scan >= args.scan_interval:
                    last_opportunity_scan_time = loop_now
                    # Re-scan candidate pools using lowest bankroll; cached for 5s across all checks
                    candidates = find_optimal_lp_markets(
                        bankroll_usd=min(t.initial_cash for t in tiers),
                        limit_output=10,
                    )

                # 4. Quoting, auto-merge, and de-skew evaluation for all markets across all tiers
                for tier in tiers:
                    for m in tier.markets.values():
                        pos = m.engine.portfolio.get_position(m.condition_id)
                        if pos.complete_sets >= args.auto_merge_threshold:
                            m.engine.portfolio.merge(m.condition_id, pos.complete_sets)
                        m.engine.evaluate_quotes(timestamp=loop_now)
                        m.engine.flush_ledger()

                # 5. Render comparative matrix dashboard
                render_cadence = 10.0 if args.no_clear else 1.0
                if (loop_now - last_render_time) >= render_cadence:
                    render_multi_tier_dashboard(
                        tiers=tiers,
                        start_time=start_time,
                        scan_interval=args.scan_interval,
                        clear_screen=not args.no_clear,
                    )
                    last_render_time = loop_now

            except Exception as e:
                log.error(f"Error in multi-tier loop iteration: {e}")

            await asyncio.sleep(args.poll_interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Multi-tier execution interrupted. Proceeding with clean shutdown...")
    finally:
        log.info("Session concluding. Initiating mandatory 100% cash-settlement across all tiers...")

        # 1. Cancel quotes across all market engines
        for tier in tiers:
            for m in tier.markets.values():
                m.engine.cancel_all_virtual_quotes()

        # 2. Liquidate all open positions to 100% pure cash across all market engines
        settlements = []
        for tier in tiers:
            tier_settled_cash = tier.reserve_cash
            tier_rewards = 0.0
            tier_realized_pnl = 0.0
            open_positions_count = 0

            for m in tier.markets.values():
                liq = m.engine.liquidate_all_positions()
                tier_settled_cash += liq["final_cash_settlement"]
                tier_rewards += getattr(m.engine.portfolio, "total_rewards_earned", 0.0)
                tier_realized_pnl += liq["total_realized_pnl"]
                open_positions_count += liq["final_open_positions_count"]
                m.engine.flush_ledger()
                m.engine.flush_wal()
                m.engine.close()

            tier.flush_master_ledger()
            ret_pct = ((tier_settled_cash - tier.initial_cash) / max(1.0, tier.initial_cash)) * 100.0
            settlements.append((tier, tier.initial_cash, tier_settled_cash, tier_rewards, tier_realized_pnl, ret_pct, open_positions_count))

        # 3. Stop shared live feed
        await feed.stop()

        # 4. Print Final Comparative 100% Cash-Settlement Audit Report
        sys.stdout.write("\n" + "=" * 108 + "\n")
        sys.stdout.write("  [MULTI-TIER DIVERSIFIED SETTLEMENT] 100% CASH-SETTLED AUDIT REPORT ACROSS ALL BANKROLL TIERS\n")
        sys.stdout.write("=" * 108 + "\n")
        sys.stdout.write(
            f"  {'Tier':<12s} {'Initial Bankroll':<18s} {'Final Cash (100%)':<20s} "
            f"{'Rewards Earned':<18s} {'Net Realized PnL':<20s} {'Return %':<10s} {'Open Pos'}\n"
        )
        sys.stdout.write(f"  {'-'*12} {'-'*18} {'-'*20} {'-'*18} {'-'*20} {'-'*10} {'-'*8}\n")

        for tier, init_c, fin_c, rew_c, pnl_c, ret_pct, open_p in settlements:
            c_color = C_GREEN if pnl_c >= 0 else C_RED
            sys.stdout.write(
                f"  {C_BOLD}{tier.tier_name:<12s}{C_RESET} "
                f"${init_c:>14.2f} USDC   "
                f"{C_GREEN}${fin_c:>14.2f} USDC{C_RESET}   "
                f"${rew_c:>13.4f} USDC   "
                f"{c_color}${pnl_c:>+14.4f} USDC{C_RESET}   "
                f"{c_color}{ret_pct:>+7.2f}%{C_RESET}   "
                f"{open_p:>6d} sh\n"
            )

        sys.stdout.write("=" * 108 + "\n\n")
        sys.stdout.flush()
        log.info("Multi-tier paper trading session completed cleanly. All ledgers persisted.")

    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    try:
        return asyncio.run(run_multi_tier_async(args))
    except KeyboardInterrupt:
        log.info("Process interrupted by keyboard signal. Exiting cleanly with code 0.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
