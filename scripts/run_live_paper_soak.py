#!/usr/bin/env python3
"""Unified 24/7 Live Paper Trading Soak Test Daemon across 3 Production Pillars.

Coordinates the 3 active production trading pillars connected directly to live Polymarket market data:
1. RewardHarvester ($1,000): Passive two-sided inside-touch maker quoting on active qualifying
   reward pools, earning quadratic liquidity rewards + maker volume fee rebates.
2. SettlementHarvester ($1,000): Pre-settlement discount par harvesting on resolving markets,
   acquiring discounted tokens on live CLOB books and holding to settlement redemption.
3. CtfMinter ($1,000): Conditional Tokens Framework (CTF) complete-set minting & parity arbitrage,
   monitoring binary books for sum(best_bids) > 1.000 + gas + taker_fees.
Total Virtual Portfolio: $3,000 ($1,000 allocated per pillar).

Telemetry & Resilience:
- Measures live network round-trip latency: Dublin -> London AWS eu-west-2 CLOB (ms) & Binance.
- Persists minute-level equity, drawdowns, and trade fills to SQLite: `data/paper_soak_telemetry.db`.
- SOTA storage sentinel: WAL mode, incremental auto-vacuum, rolling 30-day snapshot retention.
- Graceful shutdown on SIGTERM / SIGINT for zero-loss daemon recycling under systemd.

Usage:
    uv run python scripts/run_live_paper_soak.py
    uv run python scripts/run_live_paper_soak.py --capital 3000 --vip-tier none
    uv run python scripts/run_live_paper_soak.py --duration 300  # Run for 5 minutes and stop
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import math
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter

from polymarketbot.storage import (
    clean_ephemeral_caches,
    get_disk_status,
    prune_sqlite_table,
    tune_sqlite_connection,
)
from polymarketbot.fees import FeeSchedule, taker_fee, maker_fee_rebate
from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.rewards import (
    compute_competing_depth_score,
    compute_quadratic_score,
    normalise_two_sided,
)
from polymarketbot.trading.ctf_split_arbitrage import (
    CtfCrossSplitArbitrageEngine,
    CtfSplitArbConfig,
    CtfSplitLeg,
    CtfSplitOpportunity,
    compute_leg_taker_fee,
)
from polymarketbot.trading.ctf_allocator import (
    ArbitrageDirection,
    ExecutionMode,
    MarginalCtfAllocator,
    MarketAllocation,
    ParityLegDepth,
    PortfolioAllocationResult,
    SingleMarketSweepResult,
)
from polymarketbot.trading.domain2 import (
    MarginalSettlementAllocator,
    SettlementCandidate,
    SettlementAllocation,
    SettlementOptimizationResult,
)

log = get_logger("paper_soak")

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_GAMMA_HOST = "https://gamma-api.polymarket.com"
DEFAULT_BINANCE_HOST = "https://fapi.binance.com"
DB_PATH = Path("data/paper_soak_telemetry.db")

# ANSI Color codes for clean telemetry
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"


# ==============================================================================
# TELEMETRY & PERSISTENCE
# ==============================================================================

@dataclass
class OpenPosition:
    """Represents an open token position held until settlement or unwinding."""
    token_id: str
    condition_id: str
    question: str
    side: str
    shares: float
    entry_price: float
    cost_basis: float
    entry_time: float
    target_settlement_duration_sec: float
    settled: bool = False


@dataclass
class QueuedSettlementCandidate:
    """Pre-computed settlement opportunity held in radar queue for immediate capital redeployment."""
    condition_id: str
    token_id: str
    question: str
    best_ask: float
    ask_size: float
    target_settle_sec: float
    discovered_ts: float
    ask_ladder: List[Tuple[float, float]] = field(default_factory=list)

    @property
    def edge_pct(self) -> float:
        return (1.000 - self.best_ask) / max(0.01, self.best_ask)

    @property
    def annualized_arr(self) -> float:
        return self.edge_pct * (31_536_000.0 / max(1.0, self.target_settle_sec))

    def to_settlement_candidate(self, ladder: Optional[List[Tuple[float, float]]] = None) -> SettlementCandidate:
        levels = ladder if ladder is not None else (self.ask_ladder or [(self.best_ask, self.ask_size)])
        return SettlementCandidate(
            condition_id=self.condition_id,
            token_id=self.token_id,
            question=self.question,
            target_settle_sec=self.target_settle_sec,
            ask_ladder=levels,
        )


@dataclass
class RewardPoolCandidate:
    """Live reward pool metrics and quadratic score parameters."""
    pool_id: str
    question: str
    rate_per_day: float
    competing_score: float
    mid_price: float
    max_spread_cents: float
    min_size: float
    spread_offset_cents: float = 0.50
    raw_pool: Optional[dict] = None

    @property
    def alpha(self) -> float:
        """Quadratic score per dollar deployed under CTF binary collateral constraint.
        To quote Q shares two-sided requires Q * 1.00 USDC collateral (1 YES + 1 NO).
        Therefore, cost per share pair is strictly $1.00."""
        v = max(0.1, self.max_spread_cents)
        s = min(self.spread_offset_cents, v * 0.90)
        score_per_share = 2.0 * ((v - s) / v) ** 2  # Two-sided quoting
        cost_per_share = 1.00  # CTF invariant: 1 YES + 1 NO = $1.00 USDC
        return score_per_share / cost_per_share

    @property
    def min_capital(self) -> float:
        """Capital required to quote minimum eligible size two-sided."""
        return self.min_size * 1.00

    @property
    def floor_capital(self) -> float:
        """Closed-form minimum capital required to clear Polymarket $1.00 daily payout floor."""
        if self.rate_per_day <= 1.00:
            return float("inf")
        return self.competing_score / (self.alpha * (self.rate_per_day - 1.00))

    @property
    def threshold_capital(self) -> float:
        """Absolute minimum capital to deploy if pool is active."""
        return max(self.min_capital, self.floor_capital)

    @property
    def marginal_at_zero(self) -> float:
        """Initial marginal return per dollar: dR/dC |_{C=0} = P * alpha / D."""
        if self.competing_score <= 0:
            return 0.0
        return (self.rate_per_day * self.alpha) / self.competing_score

    def payout(self, capital: float) -> float:
        """Evaluates non-linear daily cash payout with $1.00 floor."""
        if capital < self.threshold_capital or capital <= 0:
            return 0.0
        s_my = self.alpha * capital
        denom = s_my + self.competing_score
        if denom <= 0:
            return 0.0
        raw_payout = self.rate_per_day * (s_my / denom)
        return raw_payout if raw_payout >= 1.00 else 0.0


class MarginalRewardAllocator:
    """Exact closed-form KKT water-filling capital allocator equalizing marginal return."""

    @staticmethod
    def allocate(
        candidates: List[RewardPoolCandidate],
        total_budget: float,
        max_active_pools: int = 2,
    ) -> Dict[str, float]:
        viable = [
            c for c in candidates
            if c.rate_per_day > 1.00 and c.competing_score > 0 and c.threshold_capital <= total_budget
        ]
        if not viable or total_budget <= 0:
            return {}

        # Sort candidates by initial marginal return M(0) descending
        viable.sort(key=lambda c: c.marginal_at_zero, reverse=True)

        best_alloc: Dict[str, float] = {}
        best_payout = -1.0

        max_k = min(max_active_pools, len(viable))
        for k in range(1, max_k + 1):
            subset = viable[:k]
            sum_thresh = sum(c.threshold_capital for c in subset)
            if sum_thresh > total_budget:
                continue

            sum_W = sum(math.sqrt(c.rate_per_day * c.competing_score / c.alpha) for c in subset)
            sum_K = sum(c.competing_score / c.alpha for c in subset)
            inv_sqrt_lambda = (total_budget + sum_K) / sum_W

            allocs: Dict[str, float] = {}
            feasible = True
            for c in subset:
                W_i = math.sqrt(c.rate_per_day * c.competing_score / c.alpha)
                K_i = c.competing_score / c.alpha
                c_opt = W_i * inv_sqrt_lambda - K_i
                if c_opt < c.threshold_capital:
                    feasible = False
                    break
                allocs[c.pool_id] = c_opt

            if feasible:
                total_payout = sum(c.payout(allocs[c.pool_id]) for c in subset)
                if total_payout > best_payout:
                    best_payout = total_payout
                    best_alloc = allocs

        return {k: round(v, 2) for k, v in best_alloc.items()}


@dataclass
class StrategyMetrics:
    name: str
    starting_cash: float
    available_cash: float
    allocated_capital: float
    total_trades: int = 0
    gross_pnl: float = 0.0
    fees_paid: float = 0.0
    rebates_earned: float = 0.0
    net_pnl: float = 0.0
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    locked_capital: float = 0.0

    @property
    def current_equity(self) -> float:
        return self.available_cash + self.locked_capital

    def update_pnl(self, delta_pnl: float, fee: float = 0.0, rebate: float = 0.0) -> None:
        self.gross_pnl += delta_pnl
        self.fees_paid += fee
        self.rebates_earned += rebate
        net_delta = delta_pnl - fee + rebate
        self.available_cash += net_delta
        self.net_pnl = self.current_equity - self.starting_cash
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        dd = (self.peak_equity - self.current_equity) / self.peak_equity if self.peak_equity > 0 else 0.0
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd

    def lock_capital(self, amount: float) -> None:
        if amount > 0:
            self.available_cash -= amount
            self.locked_capital += amount

    def release_capital(self, cost_amount: float, payout_amount: float, fee: float = 0.0) -> None:
        self.locked_capital = max(0.0, self.locked_capital - cost_amount)
        self.available_cash += payout_amount - fee
        self.fees_paid += fee
        delta_pnl = payout_amount - cost_amount
        self.gross_pnl += delta_pnl
        self.net_pnl = self.current_equity - self.starting_cash
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        dd = (self.peak_equity - self.current_equity) / self.peak_equity if self.peak_equity > 0 else 0.0
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd


class SoakTelemetryDb:
    """Manages thread-safe SQLite persistence for live soak telemetry with auto-pruning."""

    def __init__(self, db_path: Path = DB_PATH, retention_days: int = 30) -> None:
        self.db_path = db_path
        self.retention_days = retention_days
        self._last_prune = 0.0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def prune_old_records(self, force: bool = False) -> None:
        """Prune historical snapshots and trades beyond retention period and checkpoint WAL."""
        now = time.time()
        if not force and (now - self._last_prune) < 21600.0:  # Every 6 hours
            return
        self._last_prune = now
        try:
            with self._get_conn() as conn:
                prune_sqlite_table(conn, "soak_snapshots", "timestamp", self.retention_days * 86400.0, checkpoint_wal=False)
                prune_sqlite_table(conn, "soak_trades", "timestamp", max(60, self.retention_days * 2) * 86400.0, checkpoint_wal=True)
        except Exception as e:
            log.warning(f"Failed to prune old telemetry records: {e}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        tune_sqlite_connection(conn, auto_vacuum=True)
        return conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS soak_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    strategy TEXT,
                    condition_id TEXT,
                    side TEXT,
                    price REAL,
                    size REAL,
                    gross_pnl REAL,
                    fee REAL,
                    rebate REAL,
                    net_pnl REAL,
                    latency_us REAL,
                    details TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS soak_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    uptime_seconds REAL,
                    total_equity REAL,
                    total_net_pnl REAL,
                    pnl_latency_sniper REAL,
                    pnl_domain2 REAL,
                    pnl_ctf_splitter REAL,
                    pnl_settlement REAL,
                    clob_rtt_ms REAL,
                    binance_rtt_ms REAL,
                    trades_count INTEGER
                );
            """)
            conn.commit()

    def record_trade(
        self,
        strategy: str,
        condition_id: str,
        side: str,
        price: float,
        size: float,
        gross_pnl: float,
        fee: float,
        rebate: float,
        latency_us: float = 0.0,
        details: str = "",
    ) -> None:
        net = gross_pnl - fee + rebate
        try:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO soak_trades
                    (timestamp, strategy, condition_id, side, price, size, gross_pnl, fee, rebate, net_pnl, latency_us, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (time.time(), strategy, condition_id, side, price, size, gross_pnl, fee, rebate, net, latency_us, details))
                conn.commit()
            self.prune_old_records()
        except Exception as e:
            log.warning(f"Failed to record trade to SQLite: {e}")

    def record_snapshot(
        self,
        uptime_sec: float,
        total_equity: float,
        total_net_pnl: float,
        strats: Dict[str, StrategyMetrics],
        clob_rtt_ms: float,
        binance_rtt_ms: float,
        trades_count: int,
    ) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO soak_snapshots
                    (timestamp, uptime_seconds, total_equity, total_net_pnl,
                     pnl_latency_sniper, pnl_domain2, pnl_ctf_splitter, pnl_settlement,
                     clob_rtt_ms, binance_rtt_ms, trades_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    time.time(),
                    uptime_sec,
                    total_equity,
                    total_net_pnl,
                    0.0,  # decommissioned latency sniper
                    (strats.get("RewardHarvester") or strats.get("Domain2ARR") or strats.get("LatencySniper")).net_pnl,
                    (strats.get("CtfMinter") or strats.get("CtfSplitter")).net_pnl,
                    strats["SettlementHarvester"].net_pnl,
                    clob_rtt_ms,
                    binance_rtt_ms,
                    trades_count,
                ))
                conn.commit()
            self.prune_old_records()
        except Exception as e:
            log.warning(f"Failed to record snapshot to SQLite: {e}")


# ==============================================================================
# REAL-MARKET UTILITIES & REST CLIENT
# ==============================================================================

def compute_maker_rebate(price: float, shares: float, vip_tier: str) -> float:
    """Computes resting maker volume rebate from the Polymarket fee pool."""
    fee_pool = shares * 0.02 * (price * (1.0 - price))
    rebate_share = 0.20 + (0.10 if vip_tier.lower() in ("gold", "platinum", "diamond", "obsidian") else 0.05)
    return round(fee_pool * rebate_share, 5)


def fetch_l2_book(session: requests.Session, token_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, float, float]:
    """Fetches Level-2 orderbook from Polymarket CLOB and returns (bids, asks, best_bid, best_ask, mid)."""
    try:
        resp = session.get(f"{DEFAULT_CLOB_HOST}/book?token_id={token_id}", timeout=4.0)
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            best_bid = max((float(b["price"]) for b in bids), default=0.0)
            best_ask = min((float(a["price"]) for a in asks), default=1.0)
            mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask < 1.0) else (best_bid or best_ask or 0.50)
            return bids, asks, best_bid, best_ask, mid
    except Exception as exc:
        log.debug(f"Error fetching book for token {token_id}: {exc}")
    return [], [], 0.0, 1.0, 0.50


def fetch_l2_books_batch(
    session: requests.Session,
    token_ids: List[str],
    chunk_size: int = 400,
    max_workers: int = 16,
) -> Dict[str, Dict[str, Any]]:
    """Fetches Level-2 orderbooks in an atomic batch via POST /books using concurrent chunking."""
    books_by_token: Dict[str, Dict[str, Any]] = {}
    if not token_ids:
        return books_by_token
    try:
        unique_ids = list(set(tid for tid in token_ids if tid))
        chunks = [unique_ids[i : i + chunk_size] for i in range(0, len(unique_ids), chunk_size)]

        def _fetch_chunk(chunk: List[str]) -> Dict[str, Dict[str, Any]]:
            payload = [{"token_id": tid} for tid in chunk]
            try:
                resp = session.post(f"{DEFAULT_CLOB_HOST}/books", json=payload, timeout=8.0)
                if resp.status_code == 200:
                    return {str(item.get("asset_id", "")): item for item in resp.json() if item.get("asset_id")}
            except Exception as exc:
                log.debug(f"Batch book chunk fetch error: {exc}")
            return {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for chunk_res in pool.map(_fetch_chunk, chunks):
                books_by_token.update(chunk_res)
    except Exception as exc:
        log.debug(f"Batch book fetch error: {exc}")
    return books_by_token


def fast_competing_depth_score(
    bids: List[Any],
    asks: List[Any],
    mid_price: float,
    max_spread_cents: float,
) -> float:
    """Computes aggregated competing quadratic score with early-break spread boundary optimization.
    Equivalently matches official Polymarket quadratic formula while skipping out-of-band depth."""
    v = max_spread_cents
    if mid_price <= 0.0 or v <= 0.0:
        return 0.0
    tot = 0.0
    if bids:
        is_dict = isinstance(bids[0], dict)
        p_first = float(bids[0]["price"] if is_dict else bids[0][0])
        p_last = float(bids[-1]["price"] if is_dict else bids[-1][0])
        it = reversed(bids) if p_last >= p_first else bids
        for b in it:
            p = float(b["price"] if is_dict else b[0])
            s = (mid_price - p) * 100.0
            if s > v:
                break
            if s >= 0.0:
                sz = float(b["size"] if is_dict else b[1])
                tot += ((v - s) / v) ** 2 * sz
    if asks:
        is_dict = isinstance(asks[0], dict)
        p_first = float(asks[0]["price"] if is_dict else asks[0][0])
        p_last = float(asks[-1]["price"] if is_dict else asks[-1][0])
        it = reversed(asks) if p_first >= p_last else asks
        for a in it:
            p = float(a["price"] if is_dict else a[0])
            s = (p - mid_price) * 100.0
            if s > v:
                break
            if s >= 0.0:
                sz = float(a["size"] if is_dict else a[1])
                tot += ((v - s) / v) ** 2 * sz
    return tot


def fetch_market_competing_score(
    session: requests.Session,
    tokens: List[Dict[str, Any]],
    max_spread_cents: float,
) -> Tuple[float, float]:
    """Computes authentic aggregated competing quadratic depth across ALL binary tokens (YES & NO)."""
    if not tokens or max_spread_cents <= 0:
        return 1.0, 0.50

    total_score = 0.0
    primary_mid = 0.50

    for idx, tok in enumerate(tokens[:2]):
        tid = tok.get("token_id", "")
        if not tid:
            continue
        bids, asks, best_bid, best_ask, mid = fetch_l2_book(session, tid)
        fallback_px = float(tok.get("price", 0.50))
        valid_mid = mid if (best_bid > 0 and best_ask < 1.0) else fallback_px
        if idx == 0:
            primary_mid = valid_mid

        score_tok, _, _ = compute_competing_depth_score(bids, asks, valid_mid, max_spread_cents)
        total_score += score_tok

    return max(total_score, 1.0), primary_mid


class VirtualBookTracker:
    """Tracks consumed orderbook depth to prevent ghost liquidity churn in paper simulation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # token_id -> {rounded_price: consumed_shares}
        self._consumed: Dict[str, Dict[float, float]] = {}

    def filter_ladder(self, token_id: str, ladder: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Subtracts consumed paper volume from raw live orderbook asks."""
        with self._lock:
            tok_consumed = self._consumed.get(token_id, {})
            if not tok_consumed:
                return ladder

            filtered: List[Tuple[float, float]] = []
            for px, sz in ladder:
                p_round = round(px, 4)
                cons = tok_consumed.get(p_round, 0.0)
                avail = max(0.0, sz - cons)
                if avail >= 1.0:  # Minimum 1 share left
                    filtered.append((px, avail))
            return filtered

    def consume(self, token_id: str, price: float, shares: float) -> None:
        """Permanently marks orderbook ask depth as consumed."""
        with self._lock:
            p_round = round(price, 4)
            tok_map = self._consumed.setdefault(token_id, {})
            tok_map[p_round] = tok_map.get(p_round, 0.0) + shares


# ==============================================================================
# 3-STRATEGY PRODUCTION SOAK CONTROLLER
# ==============================================================================

class LivePaperSoakDaemon:
    """Master controller managing the 3 active trading pillars connected to live market data."""

    def __init__(
        self,
        capital_per_strategy: float = 1000.0,
        vip_tier: str = "none",
        duration: Optional[float] = None,
        interval: float = 10.0,
        settle_hold_sec: float = 135.0,
    ) -> None:
        self.capital_per_strategy = capital_per_strategy
        self.vip_tier = vip_tier.lower()
        self.duration = duration
        self.interval = max(1.0, interval)
        self.settle_hold_sec = max(10.0, float(settle_hold_sec))
        self.running = False
        self.db = SoakTelemetryDb()
        self.start_time = 0.0
        self.recent_events: deque = deque(maxlen=6)
        self.http_session = requests.Session()
        adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
        self.http_session.mount("https://", adapter)
        self.http_session.mount("http://", adapter)
        self.http_session.headers.update({"User-Agent": "polymarketbot/live-paper-soak"})

        # Performance tracking metrics (3 active production pillars: $1,000 each = $3,000 Total)
        self.metrics = {
            "RewardHarvester": StrategyMetrics("RewardHarvester", capital_per_strategy, capital_per_strategy, capital_per_strategy),
            "SettlementHarvester": StrategyMetrics("SettlementHarvester", capital_per_strategy, capital_per_strategy, capital_per_strategy),
            "CtfMinter": StrategyMetrics("CtfMinter", capital_per_strategy, capital_per_strategy, capital_per_strategy),
        }

        # Subsystems & State
        self.ctf_engine = CtfCrossSplitArbitrageEngine(
            config=CtfSplitArbConfig(vip_tier=self.vip_tier, min_net_edge_usd=0.0015, max_mint_size_usd=capital_per_strategy * 0.50),
        )
        self.settlement_positions: List[OpenPosition] = []
        self.candidate_queue: List[QueuedSettlementCandidate] = []
        self.reward_candidate_queue: List[RewardPoolCandidate] = []
        self.reward_queue_lock = threading.Lock()
        self.virtual_book = VirtualBookTracker()
        self.queue_lock = threading.Lock()
        self.target_settle_slots = 5
        self.reserve_pct = 0.05        # 5% cash buffer ($50 on $1,000)
        self.max_market_pct = 0.25     # Max 25% single market cap ($250 on $1,000)
        self.min_ticket_usd = 25.0     # Minimum deployable tranche

        # Mathematical Marginal Capital Allocator for Strategy 2
        self.settlement_allocator = MarginalSettlementAllocator(
            min_ticket_usd=self.min_ticket_usd,
            max_market_pct=self.max_market_pct,
            max_open_slots=self.target_settle_slots,
            solver_backend="auto",
        )

        # Mathematical Marginal Capital Allocator for Strategy 3 (CTF Parity & Arbitrage)
        self.ctf_allocator = MarginalCtfAllocator(
            vip_tier=self.vip_tier,
            hurdle_rate=0.0,
            min_net_edge_usd=0.0015,
            default_gas_price_gwei=40.0,
            default_pol_usd_price=0.40,
            max_capital_per_market=capital_per_strategy * 0.40,
        )

        # Network latency monitors
        self.last_clob_rtt_ms = 0.0
        self.last_binance_rtt_ms = 0.0

    def _record_event(self, text: str) -> None:
        """Stores a recent trading/latency event for the live console dashboard."""
        now_str = time.strftime("%H:%M:%S")
        self.recent_events.append(f"[{now_str}] {text}")

    def load_previous_state(self) -> None:
        """Seamlessly resume accumulated PnL and trade counts from existing SQLite snapshots."""
        try:
            with self.db._get_conn() as conn:
                row = conn.execute(
                    "SELECT pnl_domain2, pnl_ctf_splitter, pnl_settlement, trades_count "
                    "FROM soak_snapshots ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    pnl_rw, pnl_ctf, pnl_settle, trades = row
                    pnl_rw = pnl_rw or 0.0
                    pnl_ctf = pnl_ctf or 0.0
                    pnl_settle = pnl_settle or 0.0
                    if "RewardHarvester" in self.metrics:
                        self.metrics["RewardHarvester"].available_cash += pnl_rw
                        self.metrics["RewardHarvester"].net_pnl = pnl_rw
                    if "CtfMinter" in self.metrics:
                        self.metrics["CtfMinter"].available_cash += pnl_ctf
                        self.metrics["CtfMinter"].net_pnl = pnl_ctf
                    if "SettlementHarvester" in self.metrics:
                        self.metrics["SettlementHarvester"].available_cash += pnl_settle
                        self.metrics["SettlementHarvester"].net_pnl = pnl_settle
                    log.info(
                        f"Resumed previous paper soak state: net_pnl={pnl_rw + pnl_ctf + pnl_settle:+.2f}, "
                        f"historical_trades={trades or 0}"
                    )
        except Exception as e:
            log.debug(f"Could not load previous session state: {e}")

    def start(self, resume: bool = True) -> None:
        if resume:
            self.load_previous_state()
        self.running = True
        self.start_time = time.time()
        log.info(
            f"Starting Live Paper Soak Daemon | Capital: ${self.capital_per_strategy * len(self.metrics):,.2f} "
            f"($1,000 x {len(self.metrics)}) | VIP Tier: {self.vip_tier.upper()} | "
            f"Update Interval: {self.interval:.1f}s"
        )

        # Pre-flight venue ping so initial render has live RTT instead of 0.0ms
        self._ping_venues()

        # Register signals
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Launch background strategy worker threads (3 real active engines + 2 radars + latency ping loop)
        threads = [
            threading.Thread(target=self._run_reward_universe_radar_loop, daemon=True, name="RewardRadarLoop"),
            threading.Thread(target=self._run_reward_harvester_loop, daemon=True, name="RewardHarvesterLoop"),
            threading.Thread(target=self._run_settlement_radar_thread, daemon=True, name="SettlementRadarLoop"),
            threading.Thread(target=self._run_settlement_harvester_loop, daemon=True, name="SettlementLoop"),
            threading.Thread(target=self._run_ctf_minter_loop, daemon=True, name="CtfMinterLoop"),
            threading.Thread(target=self._run_network_ping_loop, daemon=True, name="NetworkPingLoop"),
        ]
        for t in threads:
            t.start()

        self._run_telemetry_loop()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        log.info("Shutdown signal received. Stopping live soak daemon...")

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info(f"Caught signal {signum}. Initiating graceful shutdown...")
        self.stop()

    # ==========================================================================
    # Pillar 1: RewardHarvester (Full-Universe Multi-Page Scanner & Water-Filling)
    # ==========================================================================
    def scan_reward_universe(
        self,
        min_rate: float = 1.00,
        page_size: int = 500,
        max_batch_pages: int = 10,
        candidate_limit: Optional[int] = None,
    ) -> List[RewardPoolCandidate]:
        """Scans the entire Polymarket reward universe across all pages for pools with rate >= min_rate,
        evaluates live multi-token L2 orderbook competition across ALL active pools without candidate truncation,
        and constructs RewardPoolCandidate objects for global water-filling optimization."""
        t0 = time.time()
        all_eligible_pools: List[Dict[str, Any]] = []
        current_offset = 0
        hit_floor = False

        def _fetch_reward_page(offset: int) -> List[Dict[str, Any]]:
            cursor = base64.b64encode(str(offset).encode()).decode()
            url = f"{DEFAULT_CLOB_HOST}/rewards/markets/multi?order_by=rate_per_day&position=DESC&page_size={page_size}&next_cursor={cursor}"
            try:
                resp = self.http_session.get(url, timeout=8.0)
                if resp.status_code == 200:
                    return resp.json().get("data", [])
            except Exception as err:
                log.debug(f"Reward page fetch error (offset {offset}): {err}")
            return []

        # Multi-page parallel sweep across the entire exchange
        for _ in range(10):  # Up to 50,000 pools (covers all active Polymarket reward pools)
            offsets = [current_offset + i * page_size for i in range(max_batch_pages)]
            with ThreadPoolExecutor(max_workers=max_batch_pages) as pool:
                pages = list(pool.map(_fetch_reward_page, offsets))

            for page in pages:
                if not page:
                    hit_floor = True
                    break
                for p in page:
                    cfg = p.get("rewards_config", [])
                    rate = float(cfg[0].get("rate_per_day", 0.0)) if cfg else 0.0
                    tokens = p.get("tokens", [])
                    if rate >= min_rate and len(tokens) >= 2:
                        all_eligible_pools.append(p)
                    elif rate < min_rate:
                        hit_floor = True
                        break
                if hit_floor:
                    break
            if hit_floor:
                break
            current_offset += max_batch_pages * page_size

        if not all_eligible_pools:
            return []

        # Collect ALL unique token IDs across ALL eligible pools without candidate truncation
        all_token_ids: List[str] = []
        for p in all_eligible_pools:
            tokens = p.get("tokens", [])
            all_token_ids.extend([tokens[0].get("token_id"), tokens[1].get("token_id")])

        # Batch fetch authentic L2 orderbooks for ALL tokens across the entire eligible universe
        books_map = fetch_l2_books_batch(self.http_session, all_token_ids, chunk_size=400, max_workers=16)

        # Compute authentic multi-token competing depth score for EVERY eligible pool
        candidates: List[RewardPoolCandidate] = []
        for p in all_eligible_pools:
            cfg = p.get("rewards_config", [])
            rate = float(cfg[0].get("rate_per_day", 0.0)) if cfg else 0.0
            tokens = p.get("tokens", [])
            min_size = float(p.get("rewards_min_size", 100.0))
            max_spread = float(p.get("rewards_max_spread", 2.5))
            total_d = 0.0
            primary_mid = 0.50

            for idx, tok in enumerate(tokens[:2]):
                tid = tok.get("token_id", "")
                bk = books_map.get(tid, {})
                bids = bk.get("bids", [])
                asks = bk.get("asks", [])
                bb = float(bids[-1]["price"]) if bids and isinstance(bids[-1], dict) else 0.0
                ba = float(asks[-1]["price"]) if asks and isinstance(asks[-1], dict) else 1.0
                valid_mid = (bb + ba) * 0.5 if (bb > 0 and ba < 1.0) else float(tok.get("price", 0.50))
                if idx == 0:
                    primary_mid = valid_mid
                score_tok = fast_competing_depth_score(bids, asks, valid_mid, max_spread)
                total_d += score_tok

            comp_score = max(total_d, 1.0)
            cand = RewardPoolCandidate(
                pool_id=p.get("condition_id", ""),
                question=p.get("question", "Unknown"),
                rate_per_day=rate,
                competing_score=comp_score,
                mid_price=primary_mid,
                max_spread_cents=max_spread,
                min_size=min_size,
                raw_pool=p,
            )
            candidates.append(cand)

        # Sort candidate pools by authentic marginal return at zero capital descending
        candidates.sort(key=lambda c: c.marginal_at_zero, reverse=True)
        if candidate_limit is not None and candidate_limit > 0:
            return candidates[:candidate_limit]
        return candidates

    def _run_reward_universe_radar_loop(self) -> None:
        """Background radar: continuously sweeps full Polymarket reward universe across all pages,
        computes authentic multi-token competing scores, and maintains primed candidate queue."""
        log.info("Starting live Reward Universe Radar thread (full-universe multi-page scanner)...")
        while self.running:
            try:
                t0 = time.time()
                candidates = self.scan_reward_universe(min_rate=1.00)
                if candidates:
                    with self.reward_queue_lock:
                        self.reward_candidate_queue = candidates
                    top_mu = candidates[0].marginal_at_zero if candidates else 0.0
                    log.info(
                        f"Reward Universe Radar: scanned full exchange, primed ALL {len(candidates)} active pools "
                        f"(rate >= $1.00) with live L2 depth (top marginal yield mu(0)={top_mu:.2f}) in {time.time() - t0:.2f}s"
                    )
            except Exception as e:
                log.error(f"Error in Reward Universe Radar: {e}")

            # Refresh full universe every 60 seconds
            for _ in range(60):
                if not self.running:
                    break
                time.sleep(1.0)

    def _run_reward_harvester_loop(self) -> None:
        """Accrues authentic quadratic rewards on optimal pools selected by full-universe water-filling."""
        log.info("Starting live RewardHarvester worker thread (Equal-Marginal Water-Filling)...")
        active_pools: List[Dict[str, Any]] = []
        reward_allocations: Dict[str, float] = {}
        last_pool_refresh = 0.0
        last_quote_time = time.time()
        quote_start_time = time.time()

        while self.running:
            try:
                now = time.time()

                # 1. Refresh active head reward pools using candidates from full-universe radar
                if now - last_pool_refresh >= 10.0 or not active_pools:
                    last_pool_refresh = now
                    try:
                        usable_capital = self.metrics["RewardHarvester"].available_cash

                        with self.reward_queue_lock:
                            candidates_snapshot = list(self.reward_candidate_queue)

                        # If radar hasn't populated yet (e.g. at boot), do immediate sweep
                        if not candidates_snapshot:
                            candidates_snapshot = self.scan_reward_universe(min_rate=1.00)
                            with self.reward_queue_lock:
                                self.reward_candidate_queue = candidates_snapshot

                        eligible = [c for c in candidates_snapshot if c.threshold_capital <= usable_capital]
                        max_pools = 1 if usable_capital < 2000.0 else 2
                        allocs = MarginalRewardAllocator.allocate(eligible, total_budget=usable_capital, max_active_pools=max_pools)
                        if allocs:
                            reward_allocations = allocs
                            active_pools = [c.raw_pool for c in eligible if c.pool_id in allocs and allocs[c.pool_id] > 0]
                            log.debug(f"RewardHarvester water-filling allocated: {allocs}")
                    except Exception as e:
                        log.debug(f"Failed to refresh reward pools: {e}")

                if not active_pools:
                    time.sleep(1.0)
                    continue

                dt = max(0.001, now - last_quote_time)
                last_quote_time = now

                # 2. Iterate across allocated pools and accrue continuous quadratic rewards
                total_reward_dt = 0.0

                for pool in active_pools:
                    cond_id = pool.get("condition_id", "")
                    allocated_usd = reward_allocations.get(cond_id, 0.0)
                    if allocated_usd <= 0.0:
                        continue

                    tokens = pool.get("tokens", [])
                    max_spread = float(pool.get("rewards_max_spread", 2.5))
                    min_size = float(pool.get("rewards_min_size", 100.0))
                    cfg = pool.get("rewards_config", [])
                    rate_per_day = float(cfg[0].get("rate_per_day", 0.0)) if cfg else 0.0

                    if not tokens or rate_per_day <= 0:
                        continue

                    # Multi-token orderbook depth aggregation across BOTH tokens
                    comp_score, mid = fetch_market_competing_score(self.http_session, tokens, max_spread)
                    if mid <= 0:
                        continue

                    # Strict CTF Binary Collateral Constraint: quote_size = allocated_usd / 1.00
                    # Under CTF (1 YES + 1 NO = $1.00), quoting two-sided requires $1.00 collateral per share pair
                    quote_size = allocated_usd / 1.00
                    if quote_size < min_size:
                        continue

                    # 3.5s minimum resting duration
                    if (now - quote_start_time) < 3.5:
                        continue

                    s_cents = min(0.5, max_spread * 0.25)
                    score_bid = compute_quadratic_score(max_spread, s_cents, quote_size)
                    score_ask = compute_quadratic_score(max_spread, s_cents, quote_size)
                    my_score = normalise_two_sided(score_bid, score_ask)

                    tot_score = my_score + comp_score
                    if tot_score <= 0.0 or my_score <= 0.0:
                        continue

                    share = my_score / tot_score
                    daily_projected = rate_per_day * share

                    # Polymarket $1.00 daily payout floor check
                    if daily_projected < 1.00:
                        continue

                    payout_dt = (rate_per_day / 86400.0) * share * dt
                    total_reward_dt += payout_dt

                if total_reward_dt > 0.0:
                    self.metrics["RewardHarvester"].update_pnl(total_reward_dt)

                time.sleep(1.0)
            except Exception as e:
                log.error(f"Error in RewardHarvester worker loop: {e}")
                time.sleep(2.0)

    # ==========================================================================
    # Pillar 2: SettlementHarvester (Non-Stop Dynamic Pipeline & Par Redemption)
    # ==========================================================================
    def _run_settlement_radar_thread(self) -> None:
        """Background radar: high-speed multi-stratum paginated scanner across active Gamma & CLOB universe."""
        log.info("Starting live Settlement Candidate Radar thread (full-universe multi-stratum scanner)...")
        while self.running:
            try:
                now_dt = datetime.datetime.now(datetime.timezone.utc)
                now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                t6_iso = (now_dt + datetime.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
                t24_iso = (now_dt + datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
                now_plus_48h_dt = now_dt + datetime.timedelta(hours=48)
                now_plus_48h_iso = now_plus_48h_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                urls: List[str] = []
                # Stratum 1: Expired unclosed markets (newest first, 0 to 2000, 21 pages)
                for off in range(0, 2100, 100):
                    urls.append(
                        f"{DEFAULT_GAMMA_HOST}/markets?limit=100&offset={off}&closed=false&active=true&end_date_max={now_iso}&order=endDate&ascending=false"
                    )
                # Stratum 2: Expired unclosed markets (oldest first, 0 to 2000, 21 pages) for full bidirectional tail coverage
                for off in range(0, 2100, 100):
                    urls.append(
                        f"{DEFAULT_GAMMA_HOST}/markets?limit=100&offset={off}&closed=false&active=true&end_date_max={now_iso}&order=endDate&ascending=true"
                    )
                # Stratum 3A: Imminent expiring markets (0 to 6 hours, 0 to 2000, 21 pages)
                for off in range(0, 2100, 100):
                    urls.append(
                        f"{DEFAULT_GAMMA_HOST}/markets?limit=100&offset={off}&closed=false&active=true&end_date_min={now_iso}&end_date_max={t6_iso}&order=endDate&ascending=true"
                    )
                # Stratum 3B: Short-term expiring markets (6 to 24 hours, 0 to 2000, 21 pages)
                for off in range(0, 2100, 100):
                    urls.append(
                        f"{DEFAULT_GAMMA_HOST}/markets?limit=100&offset={off}&closed=false&active=true&end_date_min={t6_iso}&end_date_max={t24_iso}&order=endDate&ascending=true"
                    )
                # Stratum 3C: Medium-term expiring markets (24 to 48 hours, 0 to 2000, 21 pages)
                for off in range(0, 2100, 100):
                    urls.append(
                        f"{DEFAULT_GAMMA_HOST}/markets?limit=100&offset={off}&closed=false&active=true&end_date_min={t24_iso}&end_date_max={now_plus_48h_iso}&order=endDate&ascending=true"
                    )
                # Stratum 4: Top volume active markets (0 to 500, 6 pages)
                for off in range(0, 500, 100):
                    urls.append(
                        f"{DEFAULT_GAMMA_HOST}/markets?limit=100&offset={off}&closed=false&active=true&order=volume24hr&ascending=false"
                    )

                def _fetch_stratum_page(u: str) -> List[Dict[str, Any]]:
                    try:
                        resp = self.http_session.get(u, timeout=5.0)
                        if resp.status_code == 200:
                            return resp.json()
                    except Exception as err:
                        log.debug(f"Stratum page fetch error ({u}): {err}")
                    return []

                # Concurrent sweep of all strata in parallel (110 pages across entire active universe)
                with ThreadPoolExecutor(max_workers=20) as pool:
                    pages = list(pool.map(_fetch_stratum_page, urls))

                markets: List[Dict[str, Any]] = []
                seen_cids: Set[str] = set()
                held_cids = {p.condition_id for p in self.settlement_positions}

                for page in pages:
                    for m in page:
                        cid = m.get("conditionId")
                        if cid and cid not in seen_cids and cid not in held_cids:
                            seen_cids.add(cid)
                            markets.append(m)

                # Pre-filter candidate tokens passing Oracle Expiration Gate or 48h settlement window
                eligible_tokens: List[Tuple[Dict[str, Any], str, int, float, float]] = []
                for m in markets:
                    end_date_str = m.get("endDate") or ""
                    uma_end_str = m.get("umaEndTime") or m.get("umaEndDate") or ""
                    uma_status = str(m.get("umaResolutionStatus") or "").lower()
                    uma_statuses = [str(st).lower() for st in (m.get("umaResolutionStatuses") or [])]

                    is_past_calendar = bool(end_date_str and end_date_str <= now_iso)
                    is_uma_proposed = bool(
                        uma_end_str
                        or uma_status in ("proposed", "resolved", "disputed")
                        or any(st in ("proposed", "resolved", "disputed") for st in uma_statuses)
                    )
                    is_expiring_soon = bool(end_date_str and now_iso < end_date_str <= now_plus_48h_iso)

                    if not (is_past_calendar or is_uma_proposed or is_expiring_soon):
                        continue

                    if is_past_calendar or is_uma_proposed:
                        target_settle_sec = self.settle_hold_sec
                    else:
                        try:
                            m_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            sec_left = max(self.settle_hold_sec, (m_dt - now_dt).total_seconds())
                            target_settle_sec = sec_left + self.settle_hold_sec
                        except Exception:
                            target_settle_sec = 86400.0

                    raw_prices = m.get("outcomePrices")
                    raw_toks = m.get("clobTokenIds")
                    if not raw_prices or not raw_toks:
                        continue

                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                    toks = json.loads(raw_toks) if isinstance(raw_toks, str) else raw_toks

                    for p_idx, p_str in enumerate(prices):
                        try:
                            p_est = float(p_str)
                        except (ValueError, TypeError):
                            continue

                        # Indicative winning price pre-filter
                        if 0.930 <= p_est <= 0.998 and len(toks) > p_idx:
                            eligible_tokens.append((m, toks[p_idx], p_idx, p_est, target_settle_sec))

                # Atomic batch orderbook fetch via POST /books in parallel
                batch_size = 50
                chunks = [eligible_tokens[i : i + batch_size] for i in range(0, len(eligible_tokens), batch_size)]

                def _fetch_book_chunk(chunk: List[Tuple[Dict[str, Any], str, int, float, float]]) -> List[Dict[str, Any]]:
                    payload = [{"token_id": item[1]} for item in chunk]
                    try:
                        resp = self.http_session.post(f"{DEFAULT_CLOB_HOST}/books", json=payload, timeout=5.0)
                        if resp.status_code == 200:
                            return resp.json()
                    except Exception as b_err:
                        log.debug(f"Batch book fetch error: {b_err}")
                    return []

                books_by_token: Dict[str, Dict[str, Any]] = {}
                with ThreadPoolExecutor(max_workers=16) as pool:
                    book_chunks = list(pool.map(_fetch_book_chunk, chunks))

                for chunk_res in book_chunks:
                    for b in chunk_res:
                        books_by_token[b.get("asset_id")] = b

                new_candidates: List[QueuedSettlementCandidate] = []
                queued_cids: Set[str] = set()

                for m, tid, p_idx, p_est, target_settle_sec in eligible_tokens:
                    cid = m.get("conditionId", "")
                    if not cid or cid in queued_cids:
                        continue

                    book = books_by_token.get(tid)
                    if book and "asks" in book:
                        raw_asks = book.get("asks", [])
                    else:
                        _, raw_asks, _, _, _ = fetch_l2_book(self.http_session, tid)

                    raw_ladder = sorted(
                        [
                            (float(a["price"]), float(a["size"]))
                            for a in raw_asks
                            if 0.0 < float(a.get("price", 0.0)) < 1.0 and float(a.get("size", 0.0)) > 0
                        ],
                        key=lambda x: x[0],
                    )

                    ladder = self.virtual_book.filter_ladder(tid, raw_ladder)
                    if not ladder:
                        continue

                    best_ask = ladder[0][0]
                    ask_size = ladder[0][1]
                    if not (0.940 <= best_ask <= 0.995) or ask_size < 10.0:
                        continue

                    new_candidates.append(
                        QueuedSettlementCandidate(
                            condition_id=cid,
                            token_id=tid,
                            question=m.get("question", "Unknown"),
                            best_ask=best_ask,
                            ask_size=ask_size,
                            target_settle_sec=target_settle_sec,
                            discovered_ts=time.time(),
                            ask_ladder=ladder,
                        )
                    )
                    queued_cids.add(cid)

                new_candidates.sort(key=lambda c: c.annualized_arr, reverse=True)
                with self.queue_lock:
                    self.candidate_queue = new_candidates

                if new_candidates:
                    top_c = new_candidates[0]
                    log.info(
                        f"Settlement Radar: scanned {len(markets)} markets across multi-strata, queued {len(new_candidates)} "
                        f"resolving candidates (Top ARR: {top_c.annualized_arr*100:.1f}% on '{top_c.question[:40]}')"
                    )

                time.sleep(10.0)
            except Exception as e:
                log.warning(f"Settlement radar refresh error: {e}")
                time.sleep(5.0)

    def _deploy_pipeline_capital(self) -> int:
        """Fills available slots using Marginal Settlement Allocation across the orderbook ladder."""
        deployed_count = 0
        strat = self.metrics["SettlementHarvester"]
        equity = strat.current_equity
        usable_cash = max(0.0, strat.available_cash - (equity * self.reserve_pct))

        open_slots = max(0, self.target_settle_slots - len(self.settlement_positions))
        if open_slots == 0 or usable_cash < self.min_ticket_usd:
            return 0

        with self.queue_lock:
            candidates = list(self.candidate_queue)

        held_cids = {p.condition_id for p in self.settlement_positions}

        # Build live SettlementCandidate objects with live full ask ladders
        alloc_candidates: List[SettlementCandidate] = []
        cand_toks = [c.token_id for c in candidates if c.condition_id not in held_cids]
        book_map: Dict[str, Dict[str, Any]] = {}
        if cand_toks:
            for i in range(0, len(cand_toks), 50):
                chunk = cand_toks[i : i + 50]
                payload = [{"token_id": tid} for tid in chunk]
                try:
                    resp = self.http_session.post(f"{DEFAULT_CLOB_HOST}/books", json=payload, timeout=4.0)
                    if resp.status_code == 200:
                        for b in resp.json():
                            book_map[b.get("asset_id")] = b
                except Exception:
                    pass

        for cand in candidates:
            if cand.condition_id in held_cids:
                continue

            b_data = book_map.get(cand.token_id)
            if b_data and "asks" in b_data:
                raw_asks = b_data.get("asks", [])
            else:
                _, raw_asks, _, _, _ = fetch_l2_book(self.http_session, cand.token_id)

            raw_ladder = sorted(
                [
                    (float(a["price"]), float(a["size"]))
                    for a in raw_asks
                    if 0.0 < float(a.get("price", 0.0)) < 1.0 and float(a.get("size", 0.0)) > 0
                ],
                key=lambda x: x[0],
            )
            ladder = self.virtual_book.filter_ladder(cand.token_id, raw_ladder)
            if not ladder:
                cur_ask_size = sum(float(a.get("size", 0.0)) for a in raw_asks if float(a.get("price", 0.0)) == cand.best_ask)
                ladder = self.virtual_book.filter_ladder(cand.token_id, [(cand.best_ask, cur_ask_size)])
                if not ladder or ladder[0][1] < 10.0:
                    continue

            best_ask = ladder[0][0]
            if not (0.940 <= best_ask <= 0.995) or ladder[0][1] < 10.0:
                continue

            alloc_candidates.append(
                SettlementCandidate(
                    condition_id=cand.condition_id,
                    token_id=cand.token_id,
                    question=cand.question,
                    target_settle_sec=cand.target_settle_sec,
                    ask_ladder=ladder,
                    gas_usd=0.00145,
                    fee_rate=0.04 * (best_ask * (1.0 - best_ask)),
                )
            )

        if not alloc_candidates:
            return 0

        # Execute optimal marginal capital allocation
        opt_res = self.settlement_allocator.allocate(
            candidates=alloc_candidates,
            usable_capital=usable_cash,
            bankroll_usd=equity,
            open_slots=open_slots,
        )

        for alloc in opt_res.allocations:
            if alloc.allocated_usd < self.min_ticket_usd or alloc.allocated_usd > usable_cash + 1e-4:
                continue

            # Mark level fills as consumed in VirtualBookTracker to eliminate ghost liquidity churn
            for px, sz, _ in alloc.level_fills:
                self.virtual_book.consume(alloc.token_id, px, sz)
            if not alloc.level_fills and alloc.shares > 0:
                self.virtual_book.consume(alloc.token_id, alloc.effective_avg_price, alloc.shares)

            deploy_cost = min(alloc.allocated_usd, usable_cash)
            # Deduct exact Polymarket taker fee on aggressive buy of resting asks
            taker_fee_usd = taker_fee(alloc.shares, alloc.effective_avg_price, FeeSchedule(rate=0.04, exponent=1.0))
            strat.lock_capital(deploy_cost)
            strat.update_pnl(0.0, fee=taker_fee_usd, rebate=0.0)
            strat.total_trades += 1
            usable_cash -= deploy_cost
            held_cids.add(alloc.condition_id)

            self.db.record_trade(
                "SettlementHarvester",
                alloc.condition_id,
                "BUY_ASK",
                alloc.effective_avg_price,
                alloc.shares,
                gross_pnl=0.0,
                fee=taker_fee_usd,
                rebate=0.0,
                latency_us=25.0,
                details=f"Bought {alloc.shares:.1f} shs @ ${alloc.effective_avg_price:.3f} | Taker fee: ${taker_fee_usd:.5f}",
            )

            new_pos = OpenPosition(
                token_id=alloc.token_id,
                condition_id=alloc.condition_id,
                question=alloc.question,
                side="BUY",
                shares=alloc.shares,
                entry_price=alloc.effective_avg_price,
                cost_basis=deploy_cost,
                entry_time=time.time(),
                target_settlement_duration_sec=alloc.duration_sec,
            )
            self.settlement_positions.append(new_pos)
            deployed_count += 1
            par_gain = (alloc.shares * 1.000) - deploy_cost
            self._record_event(
                f"{C_YELLOW}[Marginal Settle]{C_RESET} Bought {alloc.shares:.0f} shs @ avg ${alloc.effective_avg_price:.3f} "
                f"| Cost: ${deploy_cost:.2f} -> Par: ${alloc.shares * 1.000:.2f} (+${par_gain:.2f} par edge, fee: ${taker_fee_usd:.4f}) | "
                f"ARR: {alloc.annualized_arr:,.0f}% ({alloc.question[:15]}...)"
            )

        return deployed_count

    def _run_settlement_harvester_loop(self) -> None:
        """High-utilization settlement harvester with instantaneous recycling upon redemption."""
        log.info("Starting live SettlementHarvester worker thread (Non-Stop Dynamic Pipeline)...")

        while self.running:
            try:
                now = time.time()
                capital_unlocked = False

                # 1. Process existing open positions for settlement redemption
                for pos in list(self.settlement_positions):
                    holding_time = now - pos.entry_time
                    if holding_time >= pos.target_settlement_duration_sec and not pos.settled:
                        # Verify actual oracle resolution status via Gamma API
                        is_resolved = False
                        is_win = True
                        try:
                            m_resp = self.http_session.get(
                                f"{DEFAULT_GAMMA_HOST}/markets?condition_ids={pos.condition_id}",
                                timeout=4.0,
                            )
                            if m_resp.status_code == 200:
                                m_items = m_resp.json()
                                if m_items:
                                    m_info = m_items[0]
                                    is_closed = bool(m_info.get("closed"))
                                    uma_status = str(m_info.get("umaResolutionStatus") or "").lower()
                                    raw_prices = m_info.get("outcomePrices")
                                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])
                                    raw_toks = m_info.get("clobTokenIds")
                                    toks = json.loads(raw_toks) if isinstance(raw_toks, str) else (raw_toks or [])

                                    winning_idx = None
                                    for p_i, p_val in enumerate(prices):
                                        try:
                                            if float(p_val) >= 0.99:
                                                winning_idx = p_i
                                                break
                                        except Exception:
                                            pass

                                    if not is_closed and uma_status != "resolved" and winning_idx is None:
                                        # Market has not finalized on-chain; defer redemption by 30s
                                        pos.target_settlement_duration_sec += 30.0
                                        continue

                                    is_resolved = True
                                    if winning_idx is not None and len(toks) > winning_idx:
                                        is_win = (toks[winning_idx] == pos.token_id)
                                    else:
                                        is_win = (pos.entry_price >= 0.985)
                        except Exception as exc:
                            log.debug(f"Resolution check deferred: {exc}")
                            pos.target_settlement_duration_sec += 30.0
                            continue

                        pos.settled = True
                        self.settlement_positions.remove(pos)

                        if is_win:
                            par_proceeds = pos.shares * 1.000
                            gross_pnl = par_proceeds - pos.cost_basis
                            gas_fee = 0.00145
                            net_pnl = gross_pnl - gas_fee

                            self.metrics["SettlementHarvester"].release_capital(
                                cost_amount=pos.cost_basis,
                                payout_amount=par_proceeds,
                                fee=gas_fee,
                            )
                            self.metrics["SettlementHarvester"].total_trades += 1
                            self.db.record_trade(
                                "SettlementHarvester",
                                pos.condition_id,
                                "PAR_REDEEM",
                                1.000,
                                pos.shares,
                                gross_pnl,
                                fee=gas_fee,
                                rebate=0.0,
                                latency_us=18.0,
                                details=f"Redeemed {pos.shares:.1f} shs @ par $1.000 (entry ${pos.entry_price:.3f}) | Gas: ${gas_fee:.5f}",
                            )
                            self._record_event(
                                f"{C_CYAN}[Par Harvest]{C_RESET} Redeemed {pos.shares:.0f} shs @ $1.000 (entry ${pos.entry_price:.3f}) | Net: +${net_pnl:.4f}"
                            )
                        else:
                            # Downside loss accounting when outcome resolves against position
                            gross_pnl = -pos.cost_basis
                            net_pnl = -pos.cost_basis
                            self.metrics["SettlementHarvester"].release_capital(
                                cost_amount=pos.cost_basis,
                                payout_amount=0.0,
                                fee=0.0,
                            )
                            self.metrics["SettlementHarvester"].total_trades += 1
                            self.db.record_trade(
                                "SettlementHarvester",
                                pos.condition_id,
                                "PAR_LOSS",
                                0.0,
                                pos.shares,
                                gross_pnl,
                                fee=0.0,
                                rebate=0.0,
                                latency_us=18.0,
                                details=f"Outcome resolved against position! Loss: -${pos.cost_basis:.2f}",
                            )
                            self._record_event(
                                f"{C_RED}[Par Loss]{C_RESET} Resolved against position! Net Loss: -${pos.cost_basis:.2f}"
                            )

                        capital_unlocked = True

                # 2. Instantaneous non-stop redeployment into queued candidates
                open_slots = self.target_settle_slots - len(self.settlement_positions)
                strat = self.metrics["SettlementHarvester"]
                usable_cash = max(0.0, strat.available_cash - (strat.current_equity * self.reserve_pct))

                if (capital_unlocked or open_slots > 0) and usable_cash >= self.min_ticket_usd:
                    self._deploy_pipeline_capital()

                time.sleep(1.0)
            except Exception as e:
                log.error(f"Error in SettlementHarvester worker loop: {e}")
                time.sleep(2.0)

    # ==========================================================================
    # Pillar 3: CtfMinter (Real-Time Binary Complete-Set Parity Arbitrage)
    # ==========================================================================
    def _run_ctf_minter_loop(self, max_iterations: Optional[int] = None) -> None:
        """Continuously scans live binary books across 580+ markets for complete-set parity arbitrage.
        
        Dual-mode execution architecture:
        1. Instantaneous Taker Sweeping: Immediately executes when resting books dislocate (sum bids > 1.000 or sum asks < 1.000).
        2. Selective Maker Parity Quoting: Quotes inside-spread complementary bids on high-flow binary markets (cost <= $0.9990),
           accumulating complete sets and merging for $1.0000 par + earning maker fee rebates.
        """
        log.info("Starting live CtfMinter worker thread with full-universe dual-mode parity engine...")
        last_scan_time = 0.0
        last_market_refresh = 0.0
        cached_market_tokens: List[Tuple[Dict[str, Any], List[str], FeeSchedule]] = []
        cached_all_token_ids: List[str] = []

        # Broad multi-offset Gamma discovery queries covering 580+ to 1,500 active binary markets
        gamma_queries = [
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset=0",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset=100",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset=200",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset=300",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false&offset=400",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=liquidity&ascending=false&offset=0",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=liquidity&ascending=false&offset=100",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=liquidity&ascending=false&offset=200",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=volume&ascending=false&offset=0",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=volume&ascending=false&offset=100",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=createdAt&ascending=false&offset=0",
            f"{DEFAULT_GAMMA_HOST}/markets?limit=100&active=true&closed=false&order=endDate&ascending=true&offset=0",
        ]

        def _refresh_market_universe() -> Tuple[List[Tuple[Dict[str, Any], List[str], FeeSchedule]], List[str]]:
            def _fetch_url(u: str) -> List[Dict[str, Any]]:
                try:
                    r = self.http_session.get(u, timeout=5.0)
                    if r.status_code == 200:
                        return r.json()
                except Exception:
                    pass
                return []

            seen_ids: Set[str] = set()
            m_tokens: List[Tuple[Dict[str, Any], List[str], FeeSchedule]] = []
            all_toks: List[str] = []

            with ThreadPoolExecutor(max_workers=min(10, len(gamma_queries))) as ex:
                for batch in ex.map(_fetch_url, gamma_queries):
                    for m in batch:
                        mid = str(m.get("id", ""))
                        if not mid or mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                        raw_toks = m.get("clobTokenIds")
                        toks = json.loads(raw_toks) if isinstance(raw_toks, str) else (raw_toks or [])
                        if len(toks) == 2 and toks[0] and toks[1]:
                            sched_dict = m.get("feeSchedule")
                            fee_sched = FeeSchedule.from_gamma(sched_dict) if sched_dict else FeeSchedule.zero()
                            m_tokens.append((m, toks, fee_sched))
                            all_toks.extend(toks)

            log.info(f"CtfMinter refreshed universe: {len(m_tokens)} active binary markets ({len(set(all_toks))} unique tokens)")
            return m_tokens, list(set(all_toks))

        iterations_run = 0
        while self.running:
            if max_iterations is not None and iterations_run >= max_iterations:
                break
            iterations_run += 1
            try:
                now = time.time()

                # Refresh market universe every 60 seconds
                if now - last_market_refresh >= 60.0 or not cached_market_tokens:
                    try:
                        cached_market_tokens, cached_all_token_ids = _refresh_market_universe()
                        last_market_refresh = now
                    except Exception as exc:
                        log.debug(f"Market universe refresh error: {exc}")

                # Fast book scanning cadence (1.5 seconds)
                if now - last_scan_time >= 1.5 and cached_market_tokens:
                    dt = max(0.5, now - last_scan_time)
                    last_scan_time = now
                    try:
                        # Parallel batch fetch of L2 orderbooks across all active markets (< 250ms)
                        books = fetch_l2_books_batch(self.http_session, cached_all_token_ids, chunk_size=50, max_workers=8)

                        parity_candidates: List[SingleMarketSweepResult] = []
                        candidate_meta: Dict[str, Tuple[Dict[str, Any], str]] = {}
                        maker_candidates: List[Tuple[float, float, float, str, Dict[str, Any], List[str], FeeSchedule]] = []

                        for m, toks, fee_sched in cached_market_tokens:
                            cond_id = m.get("conditionId", "")
                            q_title = m.get("question", "Unknown")

                            bk_0 = books.get(toks[0], {})
                            bk_1 = books.get(toks[1], {})
                            bids0 = bk_0.get("bids", [])
                            bids1 = bk_1.get("bids", [])
                            asks0 = bk_0.get("asks", [])
                            asks1 = bk_1.get("asks", [])

                            if not (bids0 or asks0) or not (bids1 or asks1):
                                continue

                            bb0 = max((float(b["price"]) for b in bids0), default=0.0)
                            bb1 = max((float(b["price"]) for b in bids1), default=0.0)

                            # 1. CTF Mint Arbitrage sweep curve: mint $1.00 complete sets, sell bids
                            leg0_bid = ParityLegDepth.from_raw(toks[0], cond_id, "BID", bids0, title="YES", fee_schedule=fee_sched)
                            leg1_bid = ParityLegDepth.from_raw(toks[1], cond_id, "BID", bids1, title="NO", fee_schedule=fee_sched)
                            sweep_mint = self.ctf_allocator.compute_sweep_curve(
                                market_id=f"{cond_id}_MINT",
                                condition_id=cond_id,
                                legs=[leg0_bid, leg1_bid],
                                direction=ArbitrageDirection.MINT_SPLIT,
                                execution_mode=ExecutionMode.ALL_TAKER,
                            )
                            if not sweep_mint.is_viable:
                                sweep_mint = self.ctf_allocator.compute_sweep_curve(
                                    market_id=f"{cond_id}_MINT",
                                    condition_id=cond_id,
                                    legs=[leg0_bid, leg1_bid],
                                    direction=ArbitrageDirection.MINT_SPLIT,
                                    execution_mode=ExecutionMode.SELECTIVE_MAKER,
                                )
                            if sweep_mint.is_viable:
                                parity_candidates.append(sweep_mint)
                                candidate_meta[sweep_mint.market_id] = (m, q_title)

                            # 2. CTF Merge Arbitrage sweep curve: buy asks under par, merge for $1.00
                            leg0_ask = ParityLegDepth.from_raw(toks[0], cond_id, "ASK", asks0, title="YES", fee_schedule=fee_sched)
                            leg1_ask = ParityLegDepth.from_raw(toks[1], cond_id, "ASK", asks1, title="NO", fee_schedule=fee_sched)
                            sweep_merge = self.ctf_allocator.compute_sweep_curve(
                                market_id=f"{cond_id}_MERGE",
                                condition_id=cond_id,
                                legs=[leg0_ask, leg1_ask],
                                direction=ArbitrageDirection.MERGE_UNWIND,
                                execution_mode=ExecutionMode.ALL_TAKER,
                            )
                            if not sweep_merge.is_viable:
                                sweep_merge = self.ctf_allocator.compute_sweep_curve(
                                    market_id=f"{cond_id}_MERGE",
                                    condition_id=cond_id,
                                    legs=[leg0_ask, leg1_ask],
                                    direction=ArbitrageDirection.MERGE_UNWIND,
                                    execution_mode=ExecutionMode.SELECTIVE_MAKER,
                                )
                            if sweep_merge.is_viable:
                                parity_candidates.append(sweep_merge)
                                candidate_meta[sweep_merge.market_id] = (m, q_title)

                            # 3. Selective Maker Complete-Set Parity Quoting Screen
                            cost_per_set = bb0 + bb1
                            vol24 = float(m.get("volume24hr", 0) or 0)
                            if 0.980 <= cost_per_set <= 0.9995 and vol24 >= 500.0:
                                spread_bps = (1.0000 - cost_per_set) * 10_000.0
                                maker_candidates.append((cost_per_set, spread_bps, vol24, q_title, m, toks, fee_sched))

                        # ==============================================================
                        # Action A: Execute Instantaneous Taker Dislocation Sweeps
                        # ==============================================================
                        usable_cash = max(0.0, self.metrics["CtfMinter"].available_cash * 0.50)
                        if parity_candidates and usable_cash >= 1.0:
                            portfolio_res = self.ctf_allocator.allocate_portfolio(
                                candidates=parity_candidates,
                                total_budget=usable_cash,
                                max_capital_per_market=usable_cash * 0.50,
                            )

                            for alloc in portfolio_res.allocations.values():
                                if alloc.allocated_shares < 0.1 or alloc.expected_net_profit < self.ctf_allocator.min_net_edge_usd:
                                    continue

                                m_data, q_text = candidate_meta.get(alloc.market_id, ({}, "Unknown"))
                                is_mint = (alloc.direction == ArbitrageDirection.MINT_SPLIT)
                                side_tag = "CTF_SPLIT_MINT" if is_mint else "CTF_MERGE_REDEEM"

                                self.metrics["CtfMinter"].update_pnl(
                                    alloc.expected_gross_profit,
                                    fee=alloc.expected_gas_cost + alloc.expected_net_fees,
                                    rebate=0.0,
                                )
                                self.metrics["CtfMinter"].total_trades += 1
                                self.db.record_trade(
                                    "CtfMinter",
                                    alloc.condition_id,
                                    side_tag,
                                    price=1.000 if is_mint else round(alloc.allocated_capital / max(1.0, alloc.allocated_shares), 4),
                                    size=alloc.allocated_shares,
                                    gross_pnl=alloc.expected_gross_profit,
                                    fee=alloc.expected_gas_cost + alloc.expected_net_fees,
                                    rebate=0.0,
                                    latency_us=18.0,
                                    details=(
                                        f"{side_tag} allocated ${alloc.allocated_capital:.2f} ({alloc.allocated_shares:.1f} shs) "
                                        f"| Marginal: {alloc.marginal_return * 10_000:.1f} bps | Net: +${alloc.expected_net_profit:.4f} "
                                        f"| Question: {q_text[:30]}"
                                    ),
                                )
                                self._record_event(
                                    f"{C_MAGENTA}[{side_tag}]{C_RESET} Swept {alloc.allocated_shares:.0f} shs (${alloc.allocated_capital:.2f}) "
                                    f"| Marginal {alloc.marginal_return * 10_000:.1f} bps | Net: +${alloc.expected_net_profit:.4f}"
                                )

                        # ==============================================================
                        # Action B: Execute Selective Maker Parity Quoting & Fills
                        # ==============================================================
                        elif maker_candidates and usable_cash >= 10.0:
                            # Rank candidates by expected flow turnover = spread_bps * volume24hr
                            maker_candidates.sort(key=lambda x: (x[1] * x[2]), reverse=True)
                            active_maker_set = maker_candidates[:5]
                            cap_per_quote = min(100.0, usable_cash / len(active_maker_set))

                            for cost, spread_bps, vol24, q_text, m_obj, toks, fs in active_maker_set:
                                # Poisson retail flow arrival rate: inside touch captures ~8% of taker volume
                                flow_rate = (vol24 / 86400.0) * 0.08
                                fill_shares = max(2.0, min(cap_per_quote, flow_rate * dt))
                                gross_pnl = fill_shares * (1.0000 - cost)
                                rebate = maker_fee_rebate(0.50, fill_shares, fs, maker_rebate_share_pct=0.20)
                                gas_cost = 0.00136
                                net_profit = gross_pnl + rebate - gas_cost

                                if net_profit >= self.ctf_allocator.min_net_edge_usd:
                                    cond_id = m_obj.get("conditionId", "")
                                    side_tag = "CTF_MAKER_MERGE"

                                    self.metrics["CtfMinter"].update_pnl(
                                        gross_pnl,
                                        fee=gas_cost,
                                        rebate=rebate,
                                    )
                                    self.metrics["CtfMinter"].total_trades += 1
                                    self.db.record_trade(
                                        "CtfMinter",
                                        cond_id,
                                        side_tag,
                                        price=round(cost, 4),
                                        size=round(fill_shares, 2),
                                        gross_pnl=round(gross_pnl, 4),
                                        fee=gas_cost,
                                        rebate=round(rebate, 4),
                                        latency_us=18.0,
                                        details=(
                                            f"Selective Maker Parity: filled {fill_shares:.1f} shs @ ${cost:.4f} "
                                            f"(+{spread_bps:.1f} bps spread + ${rebate:.4f} rebate) -> Merged $1.0000 | Net: +${net_profit:.4f} "
                                            f"| Question: {q_text[:30]}"
                                        ),
                                    )
                                    self._record_event(
                                        f"{C_MAGENTA}[CTF Maker]{C_RESET} Merged {fill_shares:.0f} shs @ ${cost:.4f} (+{spread_bps:.1f} bps) "
                                        f"| Net: +${net_profit:.4f} ({q_text[:20]}...)"
                                    )

                    except Exception as exc:
                        log.debug(f"CTF parity scanner error: {exc}")

                time.sleep(0.5)
            except Exception as e:
                log.error(f"Error in CtfMinter worker loop: {e}")
                time.sleep(2.0)

    # ==========================================================================
    # Network Latency Ping Loop
    # ==========================================================================
    def _ping_venues(self) -> None:
        """Benchmarks network RTT to Polymarket CLOB and Binance."""
        try:
            t0 = time.perf_counter()
            resp = self.http_session.get(f"{DEFAULT_CLOB_HOST}/time", timeout=3.0)
            if resp.status_code == 200:
                self.last_clob_rtt_ms = (time.perf_counter() - t0) * 1000.0
        except Exception:
            pass

        try:
            t1 = time.perf_counter()
            resp_b = self.http_session.get(f"{DEFAULT_BINANCE_HOST}/fapi/v1/time", timeout=3.0)
            if resp_b.status_code == 200:
                self.last_binance_rtt_ms = (time.perf_counter() - t1) * 1000.0
        except Exception:
            pass

    def _run_network_ping_loop(self) -> None:
        """Continuously benchmarks network RTT to Polymarket CLOB and Binance."""
        while self.running:
            self._ping_venues()
            time.sleep(self.interval)

    # ==========================================================================
    # Master Telemetry & Console Dashboard Loop
    # ==========================================================================
    def _render_dashboard(self, uptime: float) -> str:
        """Generates formatted high-density ASCII scorecard for console display."""
        total_equity = sum(m.current_equity for m in self.metrics.values())
        total_net_pnl = sum(m.net_pnl for m in self.metrics.values())

        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)
        disk = get_disk_status()
        if disk.is_critical:
            log.warning(f"CRITICAL DISK SPACE: only {disk.free_gb:.2f}GB remaining! Forcing telemetry pruning.")
            self.db.prune_old_records(force=True)
            clean_ephemeral_caches()

        disk_color = C_GREEN if disk.is_safe else (C_YELLOW if not disk.is_critical else C_RED)
        tot_color = C_GREEN if total_net_pnl >= 0 else C_RED
        total_bankroll = self.capital_per_strategy * len(self.metrics)

        lines = [
            f"{C_BOLD}=============================================================================={C_RESET}",
            f"{C_BOLD}{C_CYAN}POLYMARKETBOT LIVE PAPER SOAK TEST{C_RESET} | "
            f"Uptime: {hours}h {minutes}m {seconds}s | "
            f"VIP Tier: {C_BOLD}{self.vip_tier.upper()}{C_RESET}",
            f"Network RTT: CLOB (London) {C_GREEN}{self.last_clob_rtt_ms:.1f}ms{C_RESET} | "
            f"Binance {C_GREEN}{self.last_binance_rtt_ms:.1f}ms{C_RESET} | "
            f"Disk: {disk_color}{disk.free_gb:.1f}GB Free ({disk.free_pct:.0f}%){C_RESET}",
            f"{C_DIM}------------------------------------------------------------------------------{C_RESET}",
            f"Strategy Pillar          Starting     Current Equity   Net PnL      Max DD   Fills",
            f"{C_DIM}------------------------------------------------------------------------------{C_RESET}",
        ]
        for m in self.metrics.values():
            pnl_color = C_GREEN if m.net_pnl >= 0 else C_RED
            extra_col = ""
            if m.name == "SettlementHarvester" and self.settlement_positions:
                pending_par = sum(p.shares * 1.000 for p in self.settlement_positions)
                pending_cost = sum(p.cost_basis for p in self.settlement_positions)
                pending_edge = pending_par - pending_cost
                proj_net = m.net_pnl + pending_edge
                proj_color = C_GREEN if proj_net >= 0 else C_RED
                extra_col = f" {C_YELLOW}[{len(self.settlement_positions)} open: +${pending_edge:.2f} par edge -> Proj: {proj_color}{proj_net:+.2f}{C_RESET}{C_YELLOW}]{C_RESET}"
            lines.append(
                f"{m.name:<24} ${m.starting_cash:<11.2f} ${m.current_equity:<15.4f} "
                f"{pnl_color}{m.net_pnl:>+10.4f}{C_RESET} {m.max_drawdown_pct * 100:>6.2f}% {m.total_trades:>6}{extra_col}"
            )
        lines.append(f"{C_DIM}------------------------------------------------------------------------------{C_RESET}")
        pending_total_edge = sum(p.shares * 1.000 - p.cost_basis for p in self.settlement_positions)
        proj_tot_pnl = total_net_pnl + pending_total_edge
        proj_tot_color = C_GREEN if proj_tot_pnl >= 0 else C_RED
        proj_str = f" {C_YELLOW}(Proj w/ Par: {proj_tot_color}{proj_tot_pnl:+.2f}{C_RESET}{C_YELLOW}){C_RESET}" if pending_total_edge > 0 else ""
        lines.append(
            f"{C_BOLD}TOTAL PORTFOLIO{C_RESET}          ${total_bankroll:,.2f}   "
            f"${total_equity:<15.4f} {tot_color}{total_net_pnl:>+10.4f}{C_RESET} "
            f"Max Return: {tot_color}{(total_net_pnl / total_bankroll) * 100:+.4f}%{C_RESET}{proj_str}"
        )
        lines.append(f"{C_BOLD}=============================================================================={C_RESET}")
        if self.recent_events:
            lines.append(f"{C_DIM}Recent Activity & Fills:{C_RESET}")
            for ev in list(self.recent_events)[-4:]:
                lines.append(f"  {ev}")
            lines.append(f"{C_DIM}------------------------------------------------------------------------------{C_RESET}")
        return "\n".join(lines)

    def _run_telemetry_loop(self) -> None:
        """Prints live performance scorecard every 10 seconds and logs snapshots to SQLite."""
        is_tty = sys.stdout.isatty()
        if is_tty:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

        while self.running:
            now = time.time()
            uptime = now - self.start_time

            total_equity = sum(m.current_equity for m in self.metrics.values())
            total_net_pnl = sum(m.net_pnl for m in self.metrics.values())
            total_trades = sum(m.total_trades for m in self.metrics.values())

            # Log snapshot to database
            self.db.record_snapshot(
                uptime,
                total_equity,
                total_net_pnl,
                self.metrics,
                self.last_clob_rtt_ms,
                self.last_binance_rtt_ms,
                total_trades,
            )

            # High-density terminal status
            card = self._render_dashboard(uptime)
            if is_tty:
                sys.stdout.write(f"\033[H{card}\033[J\n")
                sys.stdout.flush()
            else:
                sys.stdout.write(f"\n{card}\n\n")
                sys.stdout.flush()

            if self.duration and uptime >= self.duration:
                log.info(f"Target duration of {self.duration}s reached. Stopping daemon.")
                self.stop()
                break

            # Sleep in responsive 0.2s slices up to self.interval so shutdown is immediate on Ctrl+C
            end_sleep = time.time() + self.interval
            while self.running and time.time() < end_sleep:
                time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description="24/7 Live Paper Trading Soak Daemon")
    parser.add_argument("--capital", type=float, default=3000.0, help="Total virtual bankroll across 3 active engines (default: $3,000)")
    parser.add_argument("--vip-tier", type=str, default="none", help="Polymarket VIP Rebate Tier (none, bronze, silver, gold, platinum)")
    parser.add_argument("--duration", type=float, default=None, help="Optional duration in seconds to run before halting")
    parser.add_argument("--interval", type=float, default=10.0, help="Live dashboard refresh interval in seconds (default: 10.0s)")
    parser.add_argument("--settle-hold-sec", type=float, default=135.0, help="Target holding duration in seconds for settlement harvesting simulation (default: 135.0s, real-world: 7200.0s+)")
    parser.add_argument("--fresh", action="store_true", help="Start fresh with clean $1,000 allocations without resuming previous PnL state")
    args = parser.parse_args()

    capital_each = args.capital / 3.0
    daemon = LivePaperSoakDaemon(
        capital_per_strategy=capital_each,
        vip_tier=args.vip_tier,
        duration=args.duration,
        interval=args.interval,
        settle_hold_sec=args.settle_hold_sec,
    )
    daemon.start(resume=not args.fresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
