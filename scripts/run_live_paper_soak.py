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
from bisect import bisect_left, bisect_right
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import math
import os
import random
import re
import signal
import sqlite3
import statistics
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
from polymarketbot.fees import FeeSchedule, era_schedule, taker_fee, maker_fee_rebate
from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.rewards.scoring import compute_minimum_score
from polymarketbot.rewards.queue_valuation import CapitalMigrationEngine, QueueParameterFit, YEAR_SECONDS
from polymarketbot.paper_trading.rewards import (
    compute_quadratic_score,
)
from polymarketbot.trading.ctf_allocator import (
    ArbitrageDirection,
    ExecutionMode,
    MarginalCtfAllocator,
    ParityLegDepth,
    ParityTranche,
    SingleMarketSweepResult,
)
from polymarketbot.trading.domain2 import (
    MarginalSettlementAllocator,
    SettlementCandidate,
)
from polymarketbot.trading.domain2.robust_hazard import (
    RobustHazardModel,
    SettlementSurvivalModel,
    calculate_physical_cycle_time,
)
from polymarketbot.execution.presigned_cache import PreSignedOrderCache
from polymarketbot.trading.latency_sniper import (
    BinanceLatencySniperEngine,
    LatencySniperConfig,
)

log = get_logger("paper_soak")

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_GAMMA_HOST = "https://gamma-api.polymarket.com"
DEFAULT_BINANCE_HOST = "https://fapi.binance.com"
DB_PATH = Path("data/paper_soak_telemetry.db")


def process_rss_mb() -> float:
    """Current resident set size from /proc; 0.0 when unavailable."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def host_available_mb() -> float:
    """MemAvailable from /proc/meminfo; 0.0 when unavailable."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def host_total_mb() -> float:
    """MemTotal from /proc/meminfo; 0.0 when unavailable."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def release_free_memory() -> None:
    """Return free glibc arenas to the OS after large chunk churn."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

# Polymarket reward-methodology constants. These are venue rules, not tuning
# knobs: c is currently 3.0 on every market, the minimum daily payout is $1,
# and the standard CLOB tick for binary reward markets is one cent. Per-market
# values are preferred when the API supplies them.
REWARD_C_PENALTY = 3.0
REWARD_MIN_PAYOUT_USD = 1.00
DEFAULT_TICK_CENTS = 1.0

# ANSI Color codes for clean telemetry
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"

# Strip terminal colour codes before records reach app.log / per-file logs.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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
    fee_schedule: Optional[FeeSchedule] = None
    category: str = "settlement"
    p_win: float = 1.0
    settled: bool = False
    stale_warned: bool = False
    data_stale: bool = False
    missing_since: Optional[float] = None


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
    fee_schedule: Optional[FeeSchedule] = None
    p_win: float = 1.0
    min_order_shares: float = 0.0
    category: str = "settlement"

    @property
    def expected_edge_pct(self) -> float:
        """Expected edge per dollar net of survival hazard and taker fee."""
        fee_per_share = 0.0
        if self.fee_schedule is not None and self.fee_schedule.rate > 0.0:
            p = self.best_ask
            fee_per_share = self.fee_schedule.rate * ((p * (1.0 - p)) ** self.fee_schedule.exponent)
        return (self.p_win - self.best_ask - fee_per_share) / max(0.01, self.best_ask)

    @property
    def annualized_arr(self) -> float:
        return self.expected_edge_pct * (31_536_000.0 / max(1.0, self.target_settle_sec))


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
    spread_offset_cents: float = DEFAULT_TICK_CENTS
    # Measured book aggregates: Q_one/Q_two are the official book-side scores
    # computed from the live L2 snapshot; competing_score stores Q_one+Q_two,
    # a rigorous upper bound on the competitors' total Q_min (see allocator).
    q_one_total: float = 0.0
    q_two_total: float = 0.0
    queue_ahead_bid: float = 0.0
    queue_ahead_ask: float = 0.0
    touch_bid_depth: float = 0.0
    touch_ask_depth: float = 0.0
    volume_24h_shares: float = 0.0
    raw_pool: Optional[dict] = None

    @property
    def alpha(self) -> float:
        """Official quadratic score per dollar deployed under the CTF collateral constraint.

        Collateralising Q shares per side costs Q * $1.00 (1 YES + 1 NO = $1.00).
        The strategy rests a YES bid and a NO bid at spread s. Under the official
        methodology (docs.polymarket.com/programs/liquidity-rewards):
            Q_one = S(v, Spread_YES_bid) * Q
            Q_two = S(v, Spread_NO_bid)  * Q      (NO bid is book two)
        so Q_min = max(min(Q_one, Q_two), max(Q_one, Q_two)/c) = S * Q.
        The full one-side score is earned; there is no /3 on a two-sided pair.
        """
        v = self.max_spread_cents
        if v <= 0.0:
            return 0.0
        s = min(max(0.0, self.spread_offset_cents), v)
        score_per_share = ((v - s) / v) ** 2
        cost_per_share = 1.00  # CTF invariant: 1 YES + 1 NO = $1.00 USDC
        return score_per_share / cost_per_share

    @property
    def min_capital(self) -> float:
        """Capital required to quote minimum eligible size two-sided."""
        return self.min_size * 1.00

    @property
    def floor_capital(self) -> float:
        """Closed-form minimum capital required to clear the venue $1.00 daily payout floor."""
        alpha = self.alpha
        if self.rate_per_day <= REWARD_MIN_PAYOUT_USD or alpha <= 0.0:
            return float("inf")
        return self.competing_score / (alpha * (self.rate_per_day - REWARD_MIN_PAYOUT_USD))

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

    def my_q_min(self, capital: float) -> float:
        """Official Q_min earned by a collateralised YES-bid + NO-bid pair.

        Both legs carry the same size and spread, so Q_one == Q_two == alpha*capital
        and the official minimum-score rule returns the full one-side score.
        """
        side_score = self.alpha * capital
        return compute_minimum_score(side_score, side_score, self.mid_price, REWARD_C_PENALTY)

    def payout(self, capital: float) -> float:
        """Evaluates non-linear daily cash payout with the venue $1.00 floor."""
        if capital < self.threshold_capital or capital <= 0:
            return 0.0
        s_my = self.alpha * capital
        denom = s_my + self.competing_score
        if denom <= 0:
            return 0.0
        raw_payout = self.rate_per_day * (s_my / denom)
        return raw_payout if raw_payout >= REWARD_MIN_PAYOUT_USD else 0.0


class MarginalRewardAllocator:
    """Full-universe KKT water-filling allocator (no pool cap, no subset search).

    Primal problem::

        maximise    sum_i P_i * a_i*c_i / (a_i*c_i + D_i)
        subject to  sum_i c_i <= B,   c_i in {0} U [t_i, inf)

    ``t_i`` is the measured capital at which pool i clears the venue's $1/day
    payout floor (or the venue minimum order size when that binds). The payoff
    is concave and increasing on each active branch, so the first-order
    conditions are water-filling::

        dP_i/dc_i = P_i*a_i*D_i / (a_i*c_i + D_i)^2 = lambda

    hence c_i(lambda) = W_i/sqrt(lambda) - K_i with
    W_i = sqrt(P_i*D_i/a_i) and K_i = D_i/a_i.

    The only non-convexity is the fixed activation lot t_i. It is handled by
    the exact per-pool best response to the price of capital lambda::

        c_i(lambda) = c_i(lambda)   if c_i(lambda) >= t_i
                    = t_i           if raw_i(t_i) > lambda*t_i  (lot worth buying)
                    = 0             otherwise

    lambda is found by bisection over the demand curve of the *entire* eligible
    set, so the solver scales to the full ~2,400-pool universe with no top-N
    truncation and no subset enumeration. O(n log n + n*iters); the bisection
    bound is numeric (relative lambda error <= 2^-96), not an economic cap.
    Any demand that overshoots the budget at a discrete lot switch is trimmed
    and the leftover is reported as reserve instead of silently violating it.
    Zero-depth pools (D=0, reward share is 1 for any capital) take the venue
    minimum lot first, ordered by measured return density.
    """

    _BISECTION_ITERS = 96  # numeric precision: relative lambda error <= 2^-96

    @staticmethod
    def _params(c: RewardPoolCandidate) -> Tuple[float, float, float, float, float]:
        """(W, K, lot, lot_value, lot_x) for the KKT demand curve."""
        a = c.alpha
        if a <= 0.0:
            return 0.0, 0.0, c.threshold_capital, 0.0, float("inf")
        W = math.sqrt(c.rate_per_day * c.competing_score / a)
        K = c.competing_score / a
        lot = c.threshold_capital
        lot_value = c.payout(lot)
        lot_x = math.sqrt(lot / lot_value) if lot > 0.0 and lot_value > 0.0 else float("inf")
        return W, K, lot, lot_value, lot_x

    @classmethod
    def _demand(cls, c: RewardPoolCandidate, x: float, params: Tuple[float, float, float, float, float]) -> float:
        """Capital pool c demands at x = 1/sqrt(lambda)."""
        W, K, lot, _lot_value, lot_x = params
        if W <= 0.0:
            return 0.0
        c_unc = W * x - K
        if c_unc >= lot:
            return c_unc
        if x >= lot_x:
            return lot
        return 0.0

    @classmethod
    def _feasible(
        cls,
        raw: Dict[str, float],
        pools: List[RewardPoolCandidate],
        params: Dict[str, Tuple[float, float, float, float, float]],
        budget: float,
    ) -> Dict[str, float]:
        """Trim dual demand to the budget without violating the activation lots."""
        gap = sum(raw.values()) - budget
        if gap <= 1e-9:
            return raw

        # 1. Remove whole activation lots (all-or-nothing), least value per
        #    dollar first, until the overshoot is covered.
        lots = [
            c for c in pools
            if raw[c.pool_id] > 0.0 and raw[c.pool_id] <= c.threshold_capital + 1e-9
        ]
        lots.sort(key=lambda c: (params[c.pool_id][3] / c.threshold_capital, -c.threshold_capital))
        for c in lots:
            if gap <= 1e-9:
                break
            gap -= raw[c.pool_id]
            raw[c.pool_id] = 0.0

        # 2. Scale the capital above each flexible pool's minimum lot.
        if gap > 1e-9:
            flex = [c for c in pools if raw[c.pool_id] > c.threshold_capital + 1e-9]
            excess = sum(raw[c.pool_id] - c.threshold_capital for c in flex)
            if excess > 0.0:
                scale = max(0.0, (excess - gap) / excess)
                for c in flex:
                    raw[c.pool_id] = c.threshold_capital + (raw[c.pool_id] - c.threshold_capital) * scale
                gap = sum(raw.values()) - budget

        # 3. If the flexible minimum lots alone still exceed the budget, drop
        #    whole pools, least efficient first.
        while sum(raw.values()) > budget + 1e-9:
            positive = [c for c in pools if raw[c.pool_id] > 0.0]
            if not positive:
                break
            worst = min(
                positive,
                key=lambda c: (params[c.pool_id][3] / c.threshold_capital, -c.threshold_capital),
            )
            raw[worst.pool_id] = 0.0
        return raw

    @staticmethod
    def _finalize(
        alloc: Dict[str, float],
        cand_map: Dict[str, RewardPoolCandidate],
        budget: float,
    ) -> Dict[str, float]:
        """Round to integer cents, keep every allocation at or above its
        activation lot, and never exceed the capital envelope.

        A lot must be rounded *up* to the next cent or it falls below the venue
        payout floor and pays zero; flexible capital is rounded down. If the
        ceiling on the lots overshoots the budget by a cent or two, the excess
        is shaved from flexible capital first. Whole lots are dropped only if
        the minimum lots alone cannot fit.
        """
        budget_cents = int(math.floor(budget * 100.0 + 1e-9))
        out: Dict[str, int] = {}
        lot_cents: Dict[str, int] = {}
        for pid, amt in alloc.items():
            if amt <= 0.0:
                continue
            c = cand_map[pid]
            lot = int(math.ceil(c.threshold_capital * 100.0 - 1e-9))
            lot_cents[pid] = lot
            if amt <= c.threshold_capital + 1e-9:
                out[pid] = lot
            else:
                out[pid] = max(lot, int(math.floor(amt * 100.0 + 1e-9)))

        total = sum(out.values())
        while total > budget_cents:
            reducible = [(pid, out[pid] - lot_cents[pid]) for pid in out if out[pid] > lot_cents[pid]]
            if not reducible:
                break
            pid, room = max(reducible, key=lambda item: item[1])
            cut = min(total - budget_cents, room)
            out[pid] -= cut
            total = sum(out.values())

        while total > budget_cents and out:
            pid = min(
                out,
                key=lambda p: (cand_map[p].payout(out[p] / 100.0) / max(out[p], 1), -out[p]),
            )
            total -= out.pop(pid)

        return {pid: cents / 100.0 for pid, cents in out.items()}

    @classmethod
    def _solve_active(
        cls,
        active: List[RewardPoolCandidate],
        budget: float,
    ) -> Tuple[Dict[str, float], float, float]:
        """Exact KKT water-filling for a fixed active set with lower bounds t_i.

        Returns (alloc, capital_price_lambda, unused_budget). All pools are
        forced active at or above their lot, so the demand curve
        c_i(x) = max(t_i, W_i*x - K_i), x = 1/sqrt(lambda), is continuous and
        monotone in x. Requires sum(t_i) <= budget.
        """
        rows = []
        sum_lot = 0.0
        for c in active:
            W, K, lot, _lot_value, _lot_x = cls._params(c)
            rows.append((c, W, K, lot))
            sum_lot += lot
        if sum_lot > budget + 1e-9:
            return {}, float("inf"), budget

        def demand(x: float) -> float:
            total = 0.0
            for _c, W, K, lot in rows:
                if W <= 0.0:
                    total += lot
                else:
                    total += max(lot, W * x - K)
            return total

        if not rows:
            return {}, float("inf"), budget
        x_hi = max(
            ((budget + K) / W for _c, W, K, _lot in rows if W > 0.0),
            default=0.0,
        )
        lo, hi = 0.0, x_hi
        for _ in range(cls._BISECTION_ITERS):
            mid = 0.5 * (lo + hi)
            if demand(mid) >= budget:
                hi = mid
            else:
                lo = mid
        x = hi
        allocated = {c.pool_id: max(lot, W * x - K) if W > 0.0 else lot for c, W, K, lot in rows}
        total = sum(allocated.values())
        if total > budget:  # numeric only; the demand curve is continuous
            scale = budget / total
            allocated = {pid: amt * scale for pid, amt in allocated.items()}
            total = budget
        lam = 1.0 / (x * x) if x > 0.0 else float("inf")
        return allocated, lam, budget - total

    @classmethod
    def _improve(
        cls,
        alloc: Dict[str, float],
        pool_map: Dict[str, RewardPoolCandidate],
        budget: float,
        lam: float,
    ) -> Dict[str, float]:
        """Activate lots that clear the marginal price of capital, then re-solve.

        Each accepted step strictly increases total payout, and every step adds
        at least one pool to the active set, so this terminates in at most
        |pools| accepted steps without enumerating subsets.
        """
        active_ids = [pid for pid, amt in alloc.items() if amt > 0.0 and pid in pool_map]
        current_value = sum(pool_map[pid].payout(alloc[pid]) for pid in active_ids)

        # Compute budget: additions raise the active count, swaps keep it fixed
        # but each accepted step strictly increases payout, so the local search
        # is finite. The bound is derived from the problem size (not a pool cap)
        # and leaves room for both phases; hitting it is logged by the caller's
        # reserve/gap telemetry rather than silently truncating a pool.
        for _ in range(2 * len(pool_map) + 2):
            active = [pool_map[pid] for pid in active_ids]
            solved, solved_lam, unused = cls._solve_active(active, budget) if active else ({}, float("inf"), budget)
            if active and solved:
                alloc = solved
                lam = solved_lam
                current_value = sum(
                    pool_map[pid].payout(alloc[pid]) for pid in active_ids if alloc.get(pid, 0.0) > 0.0
                )
            else:
                unused = budget - sum(alloc.values())
            # Idle capital has zero price: any affordable positive-payout lot is
            # worth buying without displacing anything.
            effective_lam = 0.0 if unused > 1e-6 else lam

            sum_lot = sum(pool_map[pid].threshold_capital for pid in active_ids)
            capacity = budget - sum_lot
            addable: List[Tuple[float, RewardPoolCandidate]] = []
            active_set = set(active_ids)
            for c in pool_map.values():
                if c.pool_id in active_set:
                    continue
                lot = c.threshold_capital
                if lot > capacity + 1e-9:
                    continue
                surplus = c.payout(lot) - effective_lam * lot
                if surplus > 1e-9:
                    addable.append((surplus, c))
            accepted = False
            if addable:
                addable.sort(key=lambda item: item[0], reverse=True)
                # Batch every profitable affordable lot first (the lot capacity
                # constraint is respected by construction), then fall back to
                # single activations.
                batch: List[RewardPoolCandidate] = []
                cap_left = capacity
                for _surplus, c in addable:
                    if c.threshold_capital <= cap_left + 1e-9:
                        batch.append(c)
                        cap_left -= c.threshold_capital
                trials = [batch] if batch else []
                trials.extend([c] for _s, c in addable)
                seen = set()
                for trial_pools in trials:
                    key = tuple(sorted(c.pool_id for c in trial_pools))
                    if not trial_pools or key in seen:
                        continue
                    seen.add(key)
                    trial_active = active + trial_pools
                    trial, _tl, _unused = cls._solve_active(trial_active, budget)
                    trial_value = sum(c.payout(trial[c.pool_id]) for c in trial_active if trial.get(c.pool_id, 0.0) > 0.0)
                    if trial_value > current_value + 1e-9:
                        alloc = trial
                        active_ids = [c.pool_id for c in trial_active if trial.get(c.pool_id, 0.0) > 0.0]
                        current_value = trial_value
                        accepted = True
                        break
            if accepted:
                continue

            # Swap phase: when the budget is fully committed, replacing the
            # least productive active lot with a better inactive lot can still
            # increase payout. Only lots clearing the capital price are tried.
            if not active_ids:
                break
            best_inactive: Optional[Tuple[float, RewardPoolCandidate]] = None
            for c in pool_map.values():
                if c.pool_id in active_set:
                    continue
                surplus = c.payout(c.threshold_capital) - effective_lam * c.threshold_capital
                if surplus > 1e-9 and (best_inactive is None or surplus > best_inactive[0]):
                    best_inactive = (surplus, c)
            swap_c = best_inactive[1] if best_inactive is not None else None
            swapped = False
            order = sorted(
                active_ids,
                key=lambda pid: pool_map[pid].payout(alloc[pid]) / max(alloc[pid], 1e-9),
            )
            if swap_c is not None:
                for drop_pid in order:
                    remain_ids = [pid for pid in active_ids if pid != drop_pid]
                    remain_lots = sum(pool_map[pid].threshold_capital for pid in remain_ids)
                    if swap_c.threshold_capital > budget - remain_lots + 1e-9:
                        continue
                    trial_active = [pool_map[pid] for pid in remain_ids] + [swap_c]
                    trial, _tl, _u = cls._solve_active(trial_active, budget)
                    trial_value = sum(
                        c.payout(trial[c.pool_id]) for c in trial_active if trial.get(c.pool_id, 0.0) > 0.0
                    )
                    if trial_value > current_value + 1e-9:
                        alloc = trial
                        active_ids = [c.pool_id for c in trial_active if trial.get(c.pool_id, 0.0) > 0.0]
                        current_value = trial_value
                        swapped = True
                        break
            if not swapped:
                # Drop phase: a lot can be worth retiring even when no better
                # inactive lot exists, because its capital can be water-filled
                # into the remaining active pools.
                dropped = False
                for drop_pid in order:
                    remain = [pool_map[pid] for pid in active_ids if pid != drop_pid]
                    if not remain:
                        continue
                    trial, _tl, _u = cls._solve_active(remain, budget)
                    trial_value = sum(
                        c.payout(trial[c.pool_id]) for c in remain if trial.get(c.pool_id, 0.0) > 0.0
                    )
                    if trial_value > current_value + 1e-9:
                        alloc = trial
                        active_ids = [c.pool_id for c in remain if trial.get(c.pool_id, 0.0) > 0.0]
                        current_value = trial_value
                        dropped = True
                        break
                if not dropped:
                    break

        return alloc

    @classmethod
    def allocate(
        cls,
        candidates: List[RewardPoolCandidate],
        total_budget: float,
    ) -> Dict[str, float]:
        """Solve the full eligible universe. No pool/candidate cap arguments."""
        if total_budget <= 0.0:
            return {}

        eligible = [
            c for c in candidates
            if c.rate_per_day > REWARD_MIN_PAYOUT_USD
            and c.max_spread_cents > 0.0
            and c.threshold_capital <= total_budget
        ]
        if not eligible:
            return {}

        cand_map = {c.pool_id: c for c in eligible}
        alloc: Dict[str, float] = {}
        remaining = total_budget

        # Pools with no observed competing depth earn the whole pool rate for
        # any capital; buy the cheapest venue lot first (highest measured yield
        # per dollar), then leave them out of the water-filling.
        zero_depth = [c for c in eligible if c.competing_score <= 0.0]
        zero_depth.sort(key=lambda c: c.rate_per_day / max(c.min_capital, 1e-9), reverse=True)
        for c in zero_depth:
            if c.min_capital <= remaining:
                alloc[c.pool_id] = c.min_capital
                remaining -= c.min_capital

        pools = [c for c in eligible if c.competing_score > 0.0]
        shadow_price = float("inf")
        if pools and remaining > 0.0:
            params = {c.pool_id: cls._params(c) for c in pools}
            if any(p[0] > 0.0 for p in params.values()):
                # x_hi makes the most efficient pool demand the whole budget,
                # so total demand at x_hi is guaranteed >= budget.
                x_hi = max((remaining + p[1]) / p[0] for p in params.values() if p[0] > 0.0)
                lo, hi = 0.0, x_hi
                for _ in range(cls._BISECTION_ITERS):
                    mid = 0.5 * (lo + hi)
                    demand = 0.0
                    for c in pools:
                        demand += cls._demand(c, mid, params[c.pool_id])
                    if demand >= remaining:
                        hi = mid
                    else:
                        lo = mid
                x = hi
                shadow_price = 1.0 / (x * x) if x > 0.0 else float("inf")
                raw = {c.pool_id: cls._demand(c, x, params[c.pool_id]) for c in pools}
                alloc.update(cls._feasible(raw, pools, params, remaining))

            # The dual solution above ignores the discrete fixed charge and can
            # stop at a feasible point with idle capital. Improve it by
            # activating pools whose lot clears the current capital price and
            # re-solving the exact KKT system over the enlarged active set.
            pool_map = {c.pool_id: c for c in pools}
            alloc = cls._improve(alloc, pool_map, remaining, shadow_price)

        final = cls._finalize(alloc, cand_map, total_budget)
        allocated = sum(final.values())
        lots = [cand_map[pid].threshold_capital for pid in final]
        log.info(
            f"Reward allocator: {len(eligible)} eligible pools, budget ${total_budget:.2f}, "
            f"selected {len(final)}, allocated ${allocated:.2f}, "
            f"reserve ${total_budget - allocated:.2f}, "
            f"capital price lambda={shadow_price:.6g}, "
            + (f"activation lots ${min(lots):.2f}-${max(lots):.2f}" if lots else "no lots")
        )
        return final


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
    unrealized_par_edge: float = 0.0
    model_only: bool = False

    @property
    def current_equity(self) -> float:
        return self.available_cash + self.locked_capital + self.unrealized_par_edge

    @property
    def realized_pnl(self) -> float:
        """Cash PnL: excludes mark-to-par uplift and model-only accruals."""
        if self.model_only:
            return 0.0
        return self.available_cash + self.locked_capital - self.starting_cash

    def recalculate_equity(self) -> None:
        self.net_pnl = self.current_equity - self.starting_cash
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        dd = (self.peak_equity - self.current_equity) / self.peak_equity if self.peak_equity > 0 else 0.0
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd

    def update_pnl(self, delta_pnl: float, fee: float = 0.0, rebate: float = 0.0) -> None:
        self.gross_pnl += delta_pnl
        self.fees_paid += fee
        self.rebates_earned += rebate
        net_delta = delta_pnl - fee + rebate
        self.available_cash += net_delta
        self.recalculate_equity()

    def lock_capital(self, amount: float) -> None:
        if amount > 0:
            self.available_cash -= amount
            self.locked_capital += amount
            self.recalculate_equity()

    def release_capital(self, cost_amount: float, payout_amount: float, fee: float = 0.0) -> None:
        self.locked_capital = max(0.0, self.locked_capital - cost_amount)
        self.available_cash += payout_amount - fee
        self.fees_paid += fee
        delta_pnl = payout_amount - cost_amount
        self.gross_pnl += delta_pnl
        self.recalculate_equity()


# C10: measured maker markout from the repo's own cycle-4 fill study (see
# docs/audit-cycle4.md). Every other flow/gas input below is measured at runtime.
MEASURED_MAKER_MARKOUT = 0.0063

_WARN_LOCK = threading.Lock()
_WARN_LAST: Dict[str, float] = {}


def _warn_rate_limited(channel: str, message: str, interval_s: float = 60.0) -> None:
    """C12: surface a failure without flooding the log on a persistent fault."""
    now = time.time()
    with _WARN_LOCK:
        if now - _WARN_LAST.get(channel, -interval_s) < interval_s:
            return
        _WARN_LAST[channel] = now
    log.warning(message)


def _level_px_size(level: Any) -> Tuple[float, float]:
    if isinstance(level, dict):
        return float(level.get("price", 0.0)), float(level.get("size", 0.0))
    if isinstance(level, (tuple, list)) and len(level) >= 2:
        return float(level[0]), float(level[1])
    return float(level.price), float(level.size)


def _best_price(levels: Any, highest: bool) -> float:
    """Best valid price from a raw ladder; malformed levels are skipped."""
    best: Optional[float] = None
    for lv in levels or []:
        try:
            px, _ = _level_px_size(lv)
        except Exception:  # noqa: BLE001 - malformed level
            continue
        if not (0.0 < px <= 1.0):
            continue
        if best is None or (px > best if highest else px < best):
            best = px
    return best if best is not None else 0.0


def _fetch_gamma_market_page(
    session: Any,
    page_offset: int,
    page_size: int,
) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """Fetch one active-market page: ``("ok"|"end"|"error", payload)``.

    Gamma returns HTTP 400/422 when ``offset`` is past the end of the universe
    (observed at offset 2300). That is end-of-pagination, not a data-health
    failure, so it is separated from real errors.
    """
    url = (
        f"{DEFAULT_GAMMA_HOST}/markets?limit={page_size}&active=true&closed=false"
        f"&order=id&ascending=true&offset={page_offset}"
    )
    try:
        r = session.get(url, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - network failure, caller decides
        _warn_rate_limited(
            "ctf_gamma_page",
            f"CtfMinter Gamma page offset={page_offset} failed: {exc}",
        )
        return "error", None
    if r.status_code == 200:
        try:
            payload = r.json()
        except Exception as exc:  # noqa: BLE001 - malformed payload
            _warn_rate_limited(
                "ctf_gamma_page",
                f"CtfMinter Gamma page offset={page_offset} returned unparseable JSON: {exc}",
            )
            return "error", None
        if isinstance(payload, list):
            return "ok", payload
        _warn_rate_limited(
            "ctf_gamma_page",
            f"CtfMinter Gamma page offset={page_offset} returned {type(payload).__name__}, not a list",
        )
        return "error", None
    if r.status_code in (400, 422) and page_offset > 0:
        # Past the end of the universe: stop and keep the pages already collected.
        return "end", []
    _warn_rate_limited(
        "ctf_gamma_page",
        f"CtfMinter Gamma page offset={page_offset} returned HTTP {r.status_code}",
    )
    return "error", None


def _paginate_gamma_universe(
    session: Any,
    page_size: int = 100,
    wave: int = 8,
    is_running: Any = None,
) -> Tuple[Optional[List[Dict[str, Any]]], int, str]:
    """Paginate the full active-market universe until a short page or 4xx end.

    Returns ``(markets, pages, reason)``. ``markets`` is ``None`` only when the
    very first attempted page failed; a failure or 4xx after any successful page
    returns the pages collected so far (partial universes are usable).
    """
    should_continue = is_running or (lambda: True)
    markets: List[Dict[str, Any]] = []
    pages = 0
    offset = 0
    saw_ok = False
    while should_continue():
        offsets = [offset + i * page_size for i in range(wave)]
        with ThreadPoolExecutor(max_workers=wave) as ex:
            results = list(ex.map(lambda o: _fetch_gamma_market_page(session, o, page_size), offsets))
        reason = ""
        for idx, (status, payload) in enumerate(results):
            pages += 1
            page_offset = offsets[idx]
            if status == "end":
                reason = f"end_of_universe_http_4xx_at_offset={page_offset}"
                break
            if status == "error":
                if saw_ok:
                    _warn_rate_limited(
                        "ctf_universe_partial",
                        f"CtfMinter Gamma pagination failed at offset={page_offset} after "
                        f"{pages - 1} pages; using {len(markets)} collected markets.",
                    )
                    return markets, pages, "partial_failure"
                return None, pages, "failure_before_first_page"
            saw_ok = True
            page = payload or []
            if page:
                markets.extend(page)
            if len(page) < page_size:
                reason = "short_page"
                break
        if reason:
            return markets, pages, reason
        offset += page_size * wave
    return markets, pages, "stopped"


def _walk_bid_exit(
    levels: Any,
    shares: float,
    fee_schedule: FeeSchedule,
    tick: float,
    markout: float = MEASURED_MAKER_MARKOUT,
) -> Tuple[float, float, float, float]:
    """C4: walk the full resting bid ladder to liquidate ``shares``.

    One price level cannot absorb an arbitrarily large residual. Each level's
    executable price is charged the measured maker markout (adverse selection)
    or one tick, whichever is larger, and the taker fee is computed per level
    because the venue fee curve is non-linear in price.

    Returns ``(proceeds_usd, fee_usd, filled_shares, vwap)``. Any part of the
    residual beyond the visible ladder is reported as unfilled (zero proceeds).
    """
    if shares <= 0.0:
        return 0.0, 0.0, 0.0, 0.0
    try:
        ordered = sorted(levels, key=lambda lv: _level_px_size(lv)[0], reverse=True)
    except Exception:  # noqa: BLE001 - malformed ladder: treat as no depth
        ordered = []
    remaining = float(shares)
    proceeds = 0.0
    fees = 0.0
    for lv in ordered:
        try:
            px, sz = _level_px_size(lv)
        except Exception:  # noqa: BLE001
            continue
        if not (0.0 < px <= 1.0) or sz <= 0.0:
            continue
        haircut = max(tick, markout * px)
        # The venue tick is the lowest legal price; never book a sub-tick exit.
        exit_px = max(tick, px - haircut)
        take = min(remaining, sz)
        if take <= 0.0:
            break
        proceeds += take * exit_px
        fees += taker_fee(take, exit_px, fee_schedule)
        remaining -= take
        if remaining <= 1e-9:
            break
    filled = float(shares) - remaining
    vwap = (proceeds / filled) if filled > 0.0 else 0.0
    return proceeds, fees, filled, vwap


class _MeasuredFillModel:
    """C3: empirical taker-fill-size distribution measured from the venue trade tape.

    Sizes are the public Data API trade prints. Draws are bootstrap samples from
    the truncated measured distribution (``min_size <= size <= cap``), so there
    is no artificial point mass at the venue minimum. If the measured sample has
    no print in range, the fallback is a shifted exponential conditional on the
    venue minimum, truncated at the capital cap.
    """

    def __init__(
        self,
        sizes: Any,
        mean_notional: float,
        mean_size: float = 0.0,
        sell_notional_share: float = 0.0,
        sell_count_share: float = 0.0,
    ) -> None:
        self.sizes = sorted(float(s) for s in sizes if s and s > 0.0)
        self.prefix = [0.0]
        for s in self.sizes:
            self.prefix.append(self.prefix[-1] + s)
        self.mean_notional = float(mean_notional) if mean_notional > 0.0 else 0.0
        # Measured across the full tape sample, including prints below the venue
        # minimum, so the fallback scale is data-driven rather than a literal.
        self.mean_size = float(mean_size) if mean_size > 0.0 else 0.0
        # Measured seller-initiated fraction of the tape (taker SELL prints).
        self.sell_notional_share = min(1.0, max(0.0, float(sell_notional_share)))
        self.sell_count_share = min(1.0, max(0.0, float(sell_count_share)))

    def __len__(self) -> int:
        return len(self.sizes)

    def _bounds(self, min_size: float, cap: Optional[float] = None) -> Tuple[int, int]:
        lo = bisect_left(self.sizes, min_size)
        hi = bisect_right(self.sizes, cap) if cap is not None else len(self.sizes)
        return lo, hi

    def stats(self, min_size: float) -> Tuple[float, float]:
        """Measured (mean, p99) of prints at or above the venue minimum."""
        lo, hi = self._bounds(min_size)
        if hi <= lo:
            return 0.0, 0.0
        n = hi - lo
        mean = (self.prefix[hi] - self.prefix[lo]) / n
        p99 = self.sizes[lo + min(n - 1, int(0.99 * (n - 1)))]
        return mean, p99

    def draw(self, min_size: float, cap: float) -> float:
        if cap <= 0.0 or cap < min_size:
            return 0.0
        lo, hi = self._bounds(min_size, cap)
        if hi > lo:
            return self.sizes[random.randrange(lo, hi)]
        lo_all, hi_all = self._bounds(min_size)
        if hi_all > lo_all:
            # Every measured fill is larger than the capital cap: honest partial fill.
            return cap
        # No measured print at or above the venue minimum. Last-resort fallback:
        # shifted exponential whose scale is the measured mean print size (or the
        # venue minimum when the tape itself is empty). Surfaced, not silent.
        scale = self.mean_size if self.mean_size >= min_size else float(min_size)
        if self.mean_size <= 0.0:
            _warn_rate_limited(
                "ctf_fill_scale",
                "CtfMinter fill-size model has no measured prints; using the venue "
                f"minimum ({min_size:g} shares) as the shifted-exponential scale.",
            )
        length = cap - min_size
        if length <= 1e-9:
            return float(min_size)
        u = random.random()
        return min_size + (-scale * math.log1p(-u * (1.0 - math.exp(-length / scale))))


class _StallWatchdog:
    """C7: surfaces worker-loop stalls from measured inter-beat intervals.

    Each channel records a heartbeat after every successful iteration. The
    watchdog never uses a hand-set timeout: once enough intervals are observed,
    a stall is a gap beyond ``max(p95, mean + 4*sigma, max_observed)`` of that
    channel's own distribution. A pathological interval is not folded back into
    the distribution, so one stall cannot normalise the next.
    """

    def __init__(self, min_samples: int = 8, poll_interval_s: float = 1.0) -> None:
        self.min_samples = min_samples
        self.poll_interval_s = poll_interval_s  # watchdog scheduling cadence, not a threshold
        self._lock = threading.Lock()
        self._last: Dict[str, float] = {}
        self._intervals: Dict[str, deque] = {}
        self._last_warn: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None

    def _threshold_locked(self, channel: str) -> Optional[float]:
        ivs = self._intervals.get(channel)
        if ivs is None or len(ivs) < self.min_samples:
            return None
        mean = statistics.fmean(ivs)
        sigma = statistics.pstdev(ivs) if len(ivs) > 1 else 0.0
        ordered = sorted(ivs)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))]
        return max(p95, mean + 4.0 * sigma, max(ivs))

    def beat(self, channel: str) -> None:
        now = time.time()
        with self._lock:
            prev = self._last.get(channel)
            self._last[channel] = now
            if prev is None:
                return
            gap = now - prev
            threshold = self._threshold_locked(channel)
            if threshold is not None and gap > threshold:
                return  # stall, already surfaced; do not pollute the cadence model
            self._intervals.setdefault(channel, deque(maxlen=240)).append(gap)

    def start(self, is_running: Any, on_stall: Any) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, args=(is_running, on_stall), daemon=True, name="StallWatchdog"
        )
        self._thread.start()

    def _run(self, is_running: Any, on_stall: Any) -> None:
        while is_running():
            time.sleep(self.poll_interval_s)
            now = time.time()
            stale: List[Tuple[str, float, float]] = []
            with self._lock:
                for channel, last in list(self._last.items()):
                    threshold = self._threshold_locked(channel)
                    if threshold is None:
                        continue
                    elapsed = now - last
                    if elapsed > threshold and now - self._last_warn.get(channel, 0.0) > threshold:
                        self._last_warn[channel] = now
                        stale.append((channel, elapsed, threshold))
            for channel, elapsed, threshold in stale:
                try:
                    on_stall(channel, elapsed, threshold)
                except Exception:  # noqa: BLE001 - a watchdog must never die
                    log.exception("Stall watchdog callback failed")


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
                    (strats.get("RewardHarvester") or strats.get("Domain2ARR") or strats.get("LatencySniper")).realized_pnl,
                    (strats.get("CtfMinter") or strats.get("CtfSplitter")).realized_pnl,
                    strats["SettlementHarvester"].realized_pnl,
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


def fetch_l2_books_batch(
    session: requests.Session,
    token_ids: List[str],
    chunk_size: int = 400,
    max_workers: int = 16,
    trim_cents: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fetches Level-2 orderbooks in an atomic batch via POST /books using concurrent chunking.

    Failed chunks are counted and logged exactly once per call so a market-wide
    CLOB outage does not spam the log or silently masquerade as empty books.

    ``trim_cents`` is an exact-for-scoring memory bound: reward scoring only
    reads levels within the venue's maximum spread of the midpoint, so levels
    farther out are dropped before they are retained across the full universe.
    Settlement and CTF callers leave it None and receive full depth.
    """
    books_by_token: Dict[str, Dict[str, Any]] = {}
    if not token_ids:
        return books_by_token

    def _trim_book(book: Dict[str, Any]) -> Dict[str, Any]:
        if trim_cents is None or trim_cents <= 0.0:
            return book
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        try:
            bb = max(float(b["price"]) for b in bids if isinstance(b, dict))
            ba = min(float(a["price"]) for a in asks if isinstance(a, dict))
        except (TypeError, ValueError):
            return book
        if not (0.0 < bb < ba < 1.0):
            return book
        mid = (bb + ba) * 0.5
        limit = trim_cents / 100.0
        lo = mid - limit
        hi = mid + limit
        book["bids"] = [
            b for b in bids
            if isinstance(b, dict) and float(b.get("price", 0.0)) >= lo
        ]
        book["asks"] = [
            a for a in asks
            if isinstance(a, dict) and float(a.get("price", 1.0)) <= hi
        ]
        return book

    # Preserve caller order while de-duplicating.
    unique_ids = list(dict.fromkeys(tid for tid in token_ids if tid))
    if not unique_ids:
        return books_by_token
    chunks = [unique_ids[i : i + chunk_size] for i in range(0, len(unique_ids), chunk_size)]
    failures = [0]

    def _fetch_chunk(chunk: List[str]) -> Dict[str, Dict[str, Any]]:
        payload = [{"token_id": tid} for tid in chunk]
        try:
            resp = session.post(f"{DEFAULT_CLOB_HOST}/books", json=payload, timeout=8.0)
            if resp.status_code == 200:
                return {
                    str(item.get("asset_id", "")): _trim_book(item)
                    for item in resp.json()
                    if item.get("asset_id")
                }
            failures[0] += 1
        except Exception:
            failures[0] += 1
        return {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for chunk_res in pool.map(_fetch_chunk, chunks):
            books_by_token.update(chunk_res)

    if failures[0]:
        log.warning(
            f"Batch /books: {failures[0]}/{len(chunks)} chunks failed "
            f"({len(unique_ids)} tokens requested, {len(books_by_token)} returned)."
        )
    return books_by_token


class LazyBookCache:
    """Bounded L2 book cache for the full reward universe.

    Fetching all 15.7k token books at once costs several hundred MB and made a
    908 MB host swap until it was unresponsive. Reward scoring only needs the
    books for the pool it is currently evaluating, and pools arrive in the same
    order as their token IDs, so load one 500-token chunk at a time and keep at
    most two chunks resident (the second covers a pool straddling a boundary).
    """

    def __init__(
        self,
        session: requests.Session,
        token_ids: List[str],
        chunk_size: int = 500,
        trim_cents: Optional[float] = None,
    ) -> None:
        self._session = session
        self._ids = list(dict.fromkeys(tid for tid in token_ids if tid))
        self._chunks = [
            self._ids[i : i + chunk_size] for i in range(0, len(self._ids), chunk_size)
        ]
        self._chunk_by_id = {
            tid: idx for idx, chunk in enumerate(self._chunks) for tid in chunk
        }
        self._chunk_cache: Dict[int, Dict[str, Dict[str, Any]]] = {}
        self._order: List[int] = []
        self._trim_cents = trim_cents
        self.fetched_count = 0

    def _load(self, idx: int) -> None:
        if idx in self._chunk_cache:
            if idx in self._order:
                self._order.remove(idx)
            self._order.append(idx)
            return
        chunk = self._chunks[idx]
        books = fetch_l2_books_batch(
            self._session,
            chunk,
            chunk_size=len(chunk),
            max_workers=1,
            trim_cents=self._trim_cents,
        )
        self._chunk_cache[idx] = books
        self._order.append(idx)
        self.fetched_count += len(books)
        while len(self._order) > 2:
            old = self._order.pop(0)
            self._chunk_cache.pop(old, None)
        release_free_memory()

    def __contains__(self, token_id: str) -> bool:
        idx = self._chunk_by_id.get(token_id)
        if idx is None:
            return False
        self._load(idx)
        return token_id in self._chunk_cache[idx]

    def get(self, token_id: str, default: Any = None) -> Any:
        idx = self._chunk_by_id.get(token_id)
        if idx is None:
            return default
        self._load(idx)
        return self._chunk_cache[idx].get(token_id, default)

    def __len__(self) -> int:
        return len(self._ids)


def competing_side_scores(
    bids: List[Any],
    asks: List[Any],
    mid_price: float,
    max_spread_cents: float,
    min_size: float = 0.0,
) -> Tuple[float, float]:
    """Official bid-side and ask-side quadratic scores for one token's book.

    Returns (bid_score, ask_score). Progressively walks levels from the touch
    outward and stops once the qualifying spread boundary is crossed.
    """
    v = max_spread_cents
    if mid_price <= 0.0 or v <= 0.0:
        return 0.0, 0.0
    bid_score = 0.0
    ask_score = 0.0
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
                if sz >= min_size:
                    bid_score += ((v - s) / v) ** 2 * sz
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
                if sz >= min_size:
                    ask_score += ((v - s) / v) ** 2 * sz
    return bid_score, ask_score


def queue_ahead_shares(
    levels: List[Any],
    mid_price: float,
    side: str,
    quote_spread_cents: float,
) -> float:
    """Shares resting at better prices than our prospective inside-touch quote.

    This is the FIFO queue that must fill before our order at the same price
    level. Levels deeper than the quote are irrelevant.
    """
    if not levels:
        return 0.0
    is_dict = isinstance(levels[0], dict)
    total = 0.0
    for level in levels:
        p = float(level["price"] if is_dict else level[0])
        sz = float(level["size"] if is_dict else level[1])
        if sz <= 0.0:
            continue
        if side.upper() == "BUY":
            if (mid_price - p) * 100.0 < quote_spread_cents:
                total += sz
        else:
            if (p - mid_price) * 100.0 < quote_spread_cents:
                total += sz
    return total


def qualifying_depth_shares(
    levels: List[Any],
    mid_price: float,
    max_spread_cents: float,
    side: str,
) -> float:
    """Share depth inside the qualifying spread on one book side.

    Used to measure queue churn between radar snapshots. Levels outside the
    venue max spread do not qualify for rewards and are excluded.
    """
    if not levels or mid_price <= 0.0 or max_spread_cents <= 0.0:
        return 0.0
    is_dict = isinstance(levels[0], dict)
    total = 0.0
    for level in levels:
        p = float(level["price"] if is_dict else level[0])
        sz = float(level["size"] if is_dict else level[1])
        if sz <= 0.0:
            continue
        spread = (mid_price - p) * 100.0 if side.upper() == "BUY" else (p - mid_price) * 100.0
        if 0.0 <= spread <= max_spread_cents:
            total += sz
    return total


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
        enable_latency_sniper: bool = False,
    ) -> None:
        self.capital_per_strategy = capital_per_strategy
        self.vip_tier = vip_tier.lower()
        self.duration = duration
        self.interval = max(1.0, interval)
        self.enable_latency_sniper = enable_latency_sniper
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
            "RewardHarvester": StrategyMetrics(
                "RewardHarvester", capital_per_strategy, capital_per_strategy, capital_per_strategy,
                model_only=True,
            ),
            "SettlementHarvester": StrategyMetrics("SettlementHarvester", capital_per_strategy, capital_per_strategy, capital_per_strategy),
            "CtfMinter": StrategyMetrics("CtfMinter", capital_per_strategy, capital_per_strategy, capital_per_strategy),
        }
        if self.enable_latency_sniper:
            self.metrics["LatencySniper"] = StrategyMetrics(
                "LatencySniper [BENCHED]", capital_per_strategy, capital_per_strategy, capital_per_strategy
            )

        # Subsystems & State
        self.settlement_positions: List[OpenPosition] = []
        self.candidate_queue: List[QueuedSettlementCandidate] = []
        self.reward_candidate_queue: List[RewardPoolCandidate] = []
        self.reward_queue_lock = threading.Lock()
        # Measured radar refresh distribution: drives the harvester's staleness
        # gate (p95 + statistical margin) and event-driven re-solves.
        self.reward_radar_intervals: deque = deque(maxlen=240)
        self.reward_radar_last_success: float = 0.0
        self.reward_radar_version: int = 0
        self.reward_radar_stats: Dict[str, Any] = {}
        self.reward_hold_durations: deque = deque(maxlen=240)
        self.reward_radar_ready = threading.Event()
        self._rate_limited_warn_at: Dict[str, float] = {}
        # Measured FIFO queue-priority / downtime valuation used by the reward
        # switching decision (replaces a hand-set dollar hurdle).
        self.reward_migration_engine = CapitalMigrationEngine()
        # Live-fitted queue-valuation inputs (no hardcoded model defaults).
        self.reward_queue_fit = QueueParameterFit()
        # token_id -> (timestamp, bid_depth, ask_depth, mid) from the previous
        # radar sweep, used to measure turnover/cancellation/impact/volatility.
        self._reward_prev_books: Dict[str, Tuple[float, float, float, float]] = {}
        self.virtual_book = VirtualBookTracker()
        self.queue_lock = threading.Lock()
        self.settlement_radar_event = threading.Event()

        # Marginal allocator owns slot count and per-market capital: it consumes the
        # whole ladder under the available budget, survival probability and fees.
        # Water-filling is exact for the concave ladder relaxation and O(L log N),
        # keeping the full-universe solve inside the 1 s settlement loop.
        self.settlement_allocator = MarginalSettlementAllocator(solver_backend="water_filling")
        self.hazard_model = RobustHazardModel()
        self.settlement_survival = SettlementSurvivalModel()
        self.settlement_clearing_velocity = 0.0  # Last measured allocator clearing mu

        # Mathematical Marginal Capital Allocator for Strategy 3 (CTF Parity & Arbitrage).
        # C5: no per-market dollar cap. The optimizer allocates the whole usable
        # bankroll against the full measured ladder; capital is the only bound.
        # C10: gas/POL inputs are measured from the venue oracles inside the
        # allocator, not hand-set here.
        self.ctf_allocator = MarginalCtfAllocator(
            vip_tier=self.vip_tier,
            hurdle_rate=0.0,
            min_net_edge_usd=0.0015,
        )
        # Dedicated connection pool for CTF discovery/books/tape so a book storm
        # in another pillar cannot starve the CTF hot path of sockets. This is a
        # compute budget, not a discovery/economic cap.
        self.ctf_http_session = requests.Session()
        ctf_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.ctf_http_session.mount("https://", ctf_adapter)
        self.ctf_http_session.mount("http://", ctf_adapter)
        self.ctf_http_session.headers.update({"User-Agent": "polymarketbot/live-paper-soak-ctf"})
        if self.enable_latency_sniper:
            self.presigned_cache = PreSignedOrderCache()
            self.sniper_engine = BinanceLatencySniperEngine(
                config=LatencySniperConfig(vip_tier=self.vip_tier, min_jump_bps=12.0),
                presigned_cache=self.presigned_cache,
            )

        # Network latency monitors
        self.last_clob_rtt_ms = 0.0
        self.last_binance_rtt_ms = 0.0

        # C7: stall watchdog. Channels are registered by their worker loops; the
        # threshold is derived from the measured inter-beat distribution.
        self.stall_watchdog = _StallWatchdog()
        self._watchdog_started = False

    def _record_event(self, text: str) -> None:
        """Stores a recent trading/latency event for the live console dashboard."""
        now_str = time.strftime("%H:%M:%S")
        self.recent_events.append(f"[{now_str}] {text}")

    def _warn_rate_limited(self, key: str, message: str, interval_s: float = 60.0) -> None:
        """Emit a warning at most once per ``interval_s`` for a repeated fault.

        ``interval_s`` throttles log volume only; it never suppresses a state
        change or an accrual decision.
        """
        now = time.time()
        if now - self._rate_limited_warn_at.get(key, 0.0) >= interval_s:
            self._rate_limited_warn_at[key] = now
            log.warning(message)

    def _reward_depth_staleness_threshold(self) -> Optional[float]:
        """Measured staleness bound for the reward radar snapshot.

        The threshold is the larger of the observed 95th percentile, the
        mean + 3 sigma control limit, and the largest observed refresh interval
        (an empirical support bound). No fixed refresh multiple is used.
        """
        with self.reward_queue_lock:
            intervals = list(self.reward_radar_intervals)
        if not intervals:
            return None
        if len(intervals) < 3:
            # Bootstrap: a single sample is not a distribution. Accrual is
            # allowed until the first three measured refresh intervals exist.
            return None
        ordered = sorted(intervals)
        p95 = ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)]
        mean = statistics.fmean(intervals)
        sigma = statistics.pstdev(intervals)
        return max(p95, mean + 3.0 * sigma, max(intervals))

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
        if self.duration is not None:
            # The worker threads use non-daemon ThreadPoolExecutor pools, so a
            # normal interpreter return can wait minutes for in-flight HTTP
            # tasks. A hard deadline preserves the operator's --duration and
            # keeps the foreground command inspectable.
            deadline = max(0.0, float(self.duration)) + 10.0

            def _force_deadline() -> None:
                time.sleep(deadline)
                log.warning(
                    f"Soak duration {self.duration:.1f}s reached; forcing process exit "
                    f"after a 10s grace."
                )
                self.stop()
                try:
                    from loguru import logger as _loguru_logger
                    _loguru_logger.complete()
                except Exception:
                    pass
                os._exit(0)

            threading.Thread(target=_force_deadline, daemon=True, name="SoakDeadline").start()

        # A 1 GB host has ~320 MB of resident agent harness and can host a
        # ~500 MB soak only if a real reserve remains. Guard on the host's
        # available memory and a 60% process ceiling; measured peak is ~451 MB
        # after compaction and lazy book caching.
        def _memory_guard() -> None:
            low_samples = 0
            total_mb = host_total_mb() or 1024.0
            rss_limit = total_mb * 0.60
            while self.running:
                release_free_memory()
                available = host_available_mb()
                rss = process_rss_mb()
                # Baseline MemAvailable is already low because the agent
                # harness shares the host; guard on this process's own RSS and
                # only treat a near-zero host reserve as an immediate danger.
                if rss > rss_limit or (available and available < 32.0):
                    low_samples += 1
                else:
                    low_samples = 0
                if low_samples >= 2:
                    log.error(
                        f"Memory guard: process RSS {rss:.0f}MB (limit {rss_limit:.0f}MB), "
                        f"host available {available:.0f}MB; stopping before swap thrash."
                    )
                    self.stop()
                    break
                time.sleep(5.0)

        threading.Thread(target=_memory_guard, daemon=True, name="SoakMemoryGuard").start()
        active_count = len(self.metrics)
        log.info(
            f"Starting Live Paper Soak Daemon | Capital: ${self.capital_per_strategy * len(self.metrics):,.2f} "
            f"($1,000 x {len(self.metrics)}) | VIP Tier: {self.vip_tier.upper()} | "
            f"Update Interval: {self.interval:.1f}s"
        )
        if not self.enable_latency_sniper:
            log.info("Note: LatencySniper is BENCHED (23.9% ARR vs 2,554% 3-pillar ARR). Focus is 100% on Maker, Settlement & Parity.")

        # Pre-flight venue ping so initial render has live RTT instead of 0.0ms
        self._ping_venues()

        # Register signals
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Prime the full-universe reward snapshot before the other full-universe
        # radars begin their own multi-page sweeps. On the shared single CPU a
        # concurrent first sweep spent ~38 s on the book batch alone; the wait
        # ends as soon as the first reward sweep completes (typically a few
        # seconds), with a safety grace so a slow request cannot deadlock startup.
        reward_radar_thread = threading.Thread(
            target=self._run_reward_universe_radar_loop, daemon=True, name="RewardRadarLoop"
        )
        reward_radar_thread.start()
        if not self.reward_radar_ready.wait(timeout=60.0):
            log.warning(
                "Reward radar first sweep exceeded the 60.0s startup grace; "
                "starting the remaining workers while it continues."
            )

        # Launch the remaining strategy workers.
        threads = [
            threading.Thread(target=self._run_reward_harvester_loop, daemon=True, name="RewardHarvesterLoop"),
            threading.Thread(target=self._run_settlement_radar_thread, daemon=True, name="SettlementRadarLoop"),
            threading.Thread(target=self._run_settlement_harvester_loop, daemon=True, name="SettlementLoop"),
            threading.Thread(target=self._run_ctf_minter_loop, daemon=True, name="CtfMinterLoop"),
            threading.Thread(target=self._run_network_ping_loop, daemon=True, name="NetworkPingLoop"),
        ]
        if self.enable_latency_sniper:
            threads.append(threading.Thread(target=self._run_latency_sniper_loop, daemon=True, name="SniperLoop"))

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
        min_rate: float = REWARD_MIN_PAYOUT_USD,
        page_size: int = 500,
    ) -> Optional[List[RewardPoolCandidate]]:
        """Sweep the ENTIRE rewarded-market universe.

        Returns the full eligible set, or ``None`` when the sweep was incomplete
        (HTTP/network failure or an unannounced truncation). The caller keeps the
        previous snapshot on ``None``, and the harvester's measured staleness
        gate then refuses to accrue against an over-age snapshot.

        Pagination follows the API's own ``next_cursor`` until its ``"LTE="``
        terminator. There is no page cap and no candidate cap. Because the
        endpoint is sorted by ``rate_per_day`` descending, the sweep may stop
        once a page crosses the venue's $1/day payout floor: every later pool
        pays below the floor and can never be selected by the optimizer.
        """
        t0 = time.time()
        # The reward endpoint's next_cursor is the base64 offset it returns
        # (e.g. "NTAw" == 500). Deriving offsets keeps the sweep deterministic.

        def _offset_cursor(offset: int) -> str:
            return base64.b64encode(str(offset).encode()).decode()

        def _compact_pool(p: Dict[str, Any]) -> Dict[str, Any]:
            """Keep only fields the radar/scoring path reads.

            The raw Gamma reward row carries dozens of unused fields; retaining
            7.9k of them costs ~100 MB on a 1 GB host. Project once per page.
            """
            toks = p.get("tokens") or []
            compact: Dict[str, Any] = {
                "condition_id": p.get("condition_id", ""),
                "question": p.get("question", "Unknown"),
                "rewards_config": p.get("rewards_config") or [],
                "tokens": [
                    {"token_id": t.get("token_id", "")}
                    for t in toks[:2]
                    if isinstance(t, dict) and t.get("token_id")
                ],
                "rewards_max_spread": p.get("rewards_max_spread"),
                "rewards_min_size": p.get("rewards_min_size"),
                "end_date": p.get("end_date"),
            }
            for key in (
                "orderPriceMinTickSize", "orderMinTickSize", "tickSize",
                "min_tick_size", "tick_size", "volume24hr", "volume24h", "volume",
            ):
                if p.get(key) is not None:
                    compact[key] = p[key]
            return compact

        def _fetch_reward_page(
            offset: int,
        ) -> Optional[Tuple[List[Dict[str, Any]], Optional[str], float]]:
            cursor = _offset_cursor(offset)
            url = (
                f"{DEFAULT_CLOB_HOST}/rewards/markets/multi"
                f"?order_by=rate_per_day&position=DESC&page_size={page_size}"
                f"&next_cursor={cursor}"
            )
            page_t0 = time.time()
            for attempt in (1, 2):
                try:
                    resp = self.http_session.get(url, timeout=8.0)
                    if resp.status_code == 200:
                        payload = resp.json()
                        return payload.get("data", []), payload.get("next_cursor"), time.time() - page_t0
                    self._warn_rate_limited(
                        f"reward_page_http_{resp.status_code}",
                        f"Reward page HTTP {resp.status_code} at offset {offset} (attempt {attempt})",
                    )
                except Exception as err:
                    self._warn_rate_limited(
                        "reward_page_error",
                        f"Reward page fetch error at offset {offset} (attempt {attempt}): {err}",
                    )
            return None

        all_eligible_pools: List[Dict[str, Any]] = []
        pages = 0
        complete = False
        seen_pool_ids: Set[str] = set()
        offset = 0
        while True:
            fetched = _fetch_reward_page(offset)
            if fetched is None:
                log.error(
                    f"Reward Universe Radar: page at offset {offset} failed; "
                    f"incomplete sweep after {pages} pages; retaining previous snapshot."
                )
                return None
            page, next_cursor, page_elapsed = fetched
            pages += 1
            log.debug(
                f"Reward page offset {offset}: {len(page)} rows in {page_elapsed:.2f}s"
            )
            if not page:
                complete = True
                break
            crossed_floor = False
            for p in page:
                cfg = p.get("rewards_config", [])
                rate = float(cfg[0].get("rate_per_day", 0.0)) if cfg else 0.0
                # Strictly above the venue floor is the only selectable set:
                # a pool paying exactly $1/day can never clear the $1 floor.
                if rate <= min_rate:
                    crossed_floor = True
                    break
                pid = str(p.get("condition_id", ""))
                if pid in seen_pool_ids:
                    continue
                seen_pool_ids.add(pid)
                if len(p.get("tokens", [])) >= 2:
                    all_eligible_pools.append(_compact_pool(p))
            if crossed_floor:
                complete = True
                break
            # A short page is not necessarily terminal: the venue may return a
            # non-terminal next_cursor when it filters rows. Only the terminal
            # cursor ends the sweep; otherwise continue from the next offset.
            if next_cursor in (None, "", "LTE="):
                complete = True
                break
            offset += page_size
            log.info(
                f"Reward Universe Radar: page {pages} at offset {offset} "
                f"({page_elapsed:.2f}s, {len(page)} rows); "
                f"eligible pools={len(all_eligible_pools)}, elapsed={time.time() - t0:.2f}s"
            )

        if not complete:
            log.error(
                f"Reward Universe Radar: sweep did not reach a terminal cursor after "
                f"{pages} pages; retaining previous snapshot."
            )
            return None
        if not all_eligible_pools:
            log.info(
                f"Reward Universe Radar: complete sweep, 0 eligible pools "
                f"({pages} pages) in {time.time() - t0:.2f}s"
            )
            return []

        # Venue configuration is normally present on every pool. When a field is
        # absent, fall back to the measured median across the observed universe
        # rather than a hand-set constant.
        observed_spreads: List[float] = []
        observed_min_sizes: List[float] = []
        for p in all_eligible_pools:
            try:
                if p.get("rewards_max_spread") is not None:
                    observed_spreads.append(float(p["rewards_max_spread"]))
                if p.get("rewards_min_size") is not None:
                    observed_min_sizes.append(float(p["rewards_min_size"]))
            except (TypeError, ValueError):
                continue
        median_spread = statistics.median(observed_spreads) if observed_spreads else None
        median_min_size = statistics.median(observed_min_sizes) if observed_min_sizes else None

        # Collect ALL token IDs across ALL eligible pools, without truncation.
        all_token_ids: List[str] = []
        for p in all_eligible_pools:
            for tok in p.get("tokens", [])[:2]:
                tid = tok.get("token_id", "")
                if tid:
                    all_token_ids.append(tid)

        # Stream L2 books in 500-token chunks instead of retaining the full
        # 15.7k-book universe. This is exact for scoring (each pool sees its
        # complete trimmed books) and keeps peak RSS bounded on the 908 MB host.
        reward_trim_cents = max(observed_spreads) if observed_spreads else None
        log.info(
            f"Reward Universe Radar: streaming {len(set(all_token_ids))} token books "
            f"for {len(all_eligible_pools)} eligible pools in 500-token chunks "
            f"(trim to {reward_trim_cents:.2f}c around mid)..."
        )
        books_t0 = time.time()
        books_map = LazyBookCache(
            self.http_session,
            all_token_ids,
            chunk_size=500,
            trim_cents=reward_trim_cents,
        )

        scoring_t0 = time.time()
        scan_ts = time.time()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        candidates: List[RewardPoolCandidate] = []
        missing_books = 0
        missing_mid = 0
        # Aggregated once per sweep so the queue-valuation EWMA adapts over time
        # rather than being frozen by thousands of token samples at once.
        sweep_deltas: List[Tuple[float, float, float, float, float, float]] = []
        sweep_volume_rates: List[float] = []
        for p in all_eligible_pools:
            cfg = p.get("rewards_config", [])
            rate = float(cfg[0].get("rate_per_day", 0.0)) if cfg else 0.0
            tokens = p.get("tokens", [])
            if len(tokens) < 2:
                continue
            t0_id = tokens[0].get("token_id", "")
            t1_id = tokens[1].get("token_id", "")
            if t0_id not in books_map or t1_id not in books_map:
                missing_books += 1
                continue

            try:
                max_spread = float(p["rewards_max_spread"]) if p.get("rewards_max_spread") is not None else median_spread
            except (TypeError, ValueError):
                max_spread = median_spread
            try:
                min_size = float(p["rewards_min_size"]) if p.get("rewards_min_size") is not None else median_min_size
            except (TypeError, ValueError):
                min_size = median_min_size
            if max_spread is None or min_size is None or max_spread <= 0.0:
                self._warn_rate_limited(
                    "reward_pool_missing_config",
                    f"Reward pool {p.get('condition_id', '')[:14]} lacks a usable "
                    f"spread/size configuration; skipped.",
                )
                continue

            # Remaining reward epoch: scale the daily rate by the measured
            # fraction of a UTC day the market will still be live. This replaces
            # the old arbitrary "< 2 hours to expiry" hand filter.
            end_date_str = p.get("end_date") or ""
            if end_date_str:
                try:
                    end_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    remaining_s = (end_dt - now_utc).total_seconds()
                    if remaining_s <= 0.0:
                        continue
                    rate *= min(1.0, remaining_s / 86_400.0)
                except (TypeError, ValueError, OverflowError):
                    self._warn_rate_limited(
                        "reward_pool_bad_end_date",
                        f"Reward pool {p.get('condition_id', '')[:14]} has an unparseable "
                        f"end_date {end_date_str!r}; using the stated daily rate.",
                    )
            if rate <= REWARD_MIN_PAYOUT_USD:
                continue

            # Per-market tick if the venue exposes it. Polymarket documents
            # orderPriceMinTickSize / orderMinTickSize / tickSize in decimal
            # dollars (e.g. 0.01), so convert to cents directly; no unit guessing.
            tick_raw = next(
                (
                    p.get(key)
                    for key in ("orderPriceMinTickSize", "orderMinTickSize", "tickSize", "min_tick_size", "tick_size")
                    if p.get(key) is not None
                ),
                None,
            )
            spread_offset = DEFAULT_TICK_CENTS
            if tick_raw is not None:
                try:
                    tick_cents = float(tick_raw) * 100.0
                    if 0.0 < tick_cents <= max_spread:
                        spread_offset = tick_cents
                    else:
                        self._warn_rate_limited(
                            "reward_pool_bad_tick",
                            f"Reward pool {p.get('condition_id', '')[:14]} has tick "
                            f"{tick_raw!r} outside (0, {max_spread:.2f}] cents; using the "
                            f"standard {DEFAULT_TICK_CENTS:.2f}c venue tick.",
                        )
                except (TypeError, ValueError):
                    self._warn_rate_limited(
                        "reward_pool_bad_tick",
                        f"Reward pool {p.get('condition_id', '')[:14]} has an unparseable "
                        f"tick {tick_raw!r}; using the standard {DEFAULT_TICK_CENTS:.2f}c venue tick.",
                    )
            spread_offset = min(spread_offset, max_spread)

            side_scores: List[Tuple[float, float]] = []
            token_measurements: List[Tuple[str, float, float, float]] = []
            primary_mid: Optional[float] = None
            queue_ahead_bid = 0.0
            queue_ahead_ask = 0.0
            invalid_mid = False
            for idx, tok in enumerate(tokens[:2]):
                tid = tok.get("token_id", "")
                bk = books_map.get(tid, {})
                bids = bk.get("bids", [])
                asks = bk.get("asks", [])
                bb = max((float(b["price"]) for b in bids if isinstance(b, dict)), default=0.0)
                ba = min((float(a["price"]) for a in asks if isinstance(a, dict)), default=1.0)
                if not (0.0 < bb < ba < 1.0):
                    # No valid two-sided book mid: never price a pool at a
                    # hand-set 0.50. Flag it and skip the pool.
                    invalid_mid = True
                    break
                valid_mid = (bb + ba) * 0.5
                if idx == 0:
                    primary_mid = valid_mid
                    queue_ahead_bid = queue_ahead_shares(bids, valid_mid, "BUY", spread_offset)
                else:
                    queue_ahead_ask = queue_ahead_shares(bids, valid_mid, "BUY", spread_offset)
                side_scores.append(
                    competing_side_scores(bids, asks, valid_mid, max_spread, min_size=min_size)
                )
                token_measurements.append(
                    (
                        tid,
                        qualifying_depth_shares(bids, valid_mid, max_spread, "BUY"),
                        qualifying_depth_shares(asks, valid_mid, max_spread, "SELL"),
                        valid_mid,
                    )
                )
            if invalid_mid or primary_mid is None or len(side_scores) != 2:
                missing_mid += 1
                continue

            # Measured 24h share flow for this market, if the venue exposes it.
            volume_raw = p.get("volume24hr", p.get("volume24h", p.get("volume")))
            volume_shares = 0.0
            if volume_raw is not None and primary_mid > 0.0:
                try:
                    volume_shares = max(0.0, float(volume_raw)) / primary_mid
                except (TypeError, ValueError):
                    volume_shares = 0.0
            volume_rate = volume_shares / 86_400.0 if volume_shares > 0.0 else None

            # Collect the pairwise book deltas; the sweep aggregate is folded
            # into the measured queue-valuation fit after the pool loop.
            for tid, depth_bid, depth_ask, token_mid in token_measurements:
                previous = self._reward_prev_books.get(tid)
                if previous is not None:
                    prev_ts, prev_bid, prev_ask, prev_mid = previous
                    delta_t = scan_ts - prev_ts
                    if delta_t > 0.0 and 0.0 < prev_mid < 1.0:
                        log_return = math.log(token_mid / prev_mid)
                        sweep_deltas.append(
                            (
                                delta_t,
                                depth_bid - prev_bid,
                                depth_ask - prev_ask,
                                prev_bid + prev_ask,
                                token_mid - prev_mid,
                                log_return * log_return / delta_t,
                            )
                        )
                self._reward_prev_books[tid] = (scan_ts, depth_bid, depth_ask, token_mid)
            if volume_rate is not None:
                sweep_volume_rates.append(volume_rate)

            (bid0, ask0), (bid1, ask1) = side_scores
            # Official book-side scores, with token 1 as the complement:
            #   Q_one = YES bids + NO asks
            #   Q_two = YES asks + NO bids
            q_one_total = bid0 + ask1
            q_two_total = ask0 + bid1
            # Per-maker Q_min is not recoverable from a price-aggregated L2
            # feed. Q_one + Q_two is a rigorous upper bound on the competitors'
            # total Q_min (min(x,y) <= x+y), so using it understates our share:
            # the projection is conservative rather than optimistic.
            competing = q_one_total + q_two_total

            candidates.append(
                RewardPoolCandidate(
                    pool_id=p.get("condition_id", ""),
                    question=p.get("question", "Unknown"),
                    rate_per_day=rate,
                    competing_score=competing,
                    mid_price=primary_mid,
                    max_spread_cents=max_spread,
                    min_size=min_size,
                    spread_offset_cents=spread_offset,
                    q_one_total=q_one_total,
                    q_two_total=q_two_total,
                    queue_ahead_bid=queue_ahead_bid,
                    queue_ahead_ask=queue_ahead_ask,
                    touch_bid_depth=token_measurements[0][1],
                    touch_ask_depth=token_measurements[0][2],
                    volume_24h_shares=volume_shares,
                )
            )

        # One measured microstructure update per sweep (not per token).
        if sweep_deltas:
            dt_median = statistics.median(item[0] for item in sweep_deltas)
            if dt_median > 0.0:
                cancelled_shares = sum(
                    max(0.0, -item[1]) + max(0.0, -item[2]) for item in sweep_deltas
                )
                previous_depth = sum(max(0.0, item[3]) for item in sweep_deltas)
                abs_mid_change = sum(abs(item[4]) for item in sweep_deltas)
                abs_signed_flow = sum(abs(item[1] - item[2]) for item in sweep_deltas)
                churn_rate = (
                    sum(abs(item[1]) + abs(item[2]) for item in sweep_deltas) / dt_median
                )
                var_per_second = statistics.fmean(item[5] for item in sweep_deltas)
                trade_rate_sample = (
                    statistics.median(sweep_volume_rates) if sweep_volume_rates else churn_rate
                )
                self.reward_queue_fit.observe_sweep(
                    dt=dt_median,
                    trade_rate_sample=trade_rate_sample,
                    cancelled_shares=cancelled_shares,
                    previous_depth=previous_depth,
                    abs_mid_change=abs_mid_change,
                    abs_signed_flow_shares=abs_signed_flow,
                    var_per_second_sample=var_per_second,
                )

        # Keep the pairwise-snapshot history bounded to the current universe.
        current_token_ids = set(all_token_ids)
        self._reward_prev_books = {
            tid: state for tid, state in self._reward_prev_books.items() if tid in current_token_ids
        }

        # Rank by measured marginal return only for telemetry and deterministic
        # iteration; the optimizer consumes the whole list.
        candidates.sort(key=lambda c: c.marginal_at_zero, reverse=True)
        elapsed = time.time() - t0
        score_elapsed = time.time() - scoring_t0
        top_mu = candidates[0].marginal_at_zero if candidates else 0.0
        fit = self.reward_queue_fit
        self.reward_radar_stats = {
            "pages": pages,
            "eligible_pools": len(all_eligible_pools),
            "tokens": len(set(all_token_ids)),
            "books": books_map.fetched_count,
            "missing_books": missing_books,
            "missing_mid": missing_mid,
            "scored": len(candidates),
            "sweep_seconds": round(elapsed, 3),
            "score_seconds": round(score_elapsed, 3),
            "ms_per_pool": round((score_elapsed * 1000.0) / max(len(candidates), 1), 3),
            "top_marginal_yield": round(top_mu, 4),
            "median_spread": median_spread,
            "median_min_size": median_min_size,
            "fit_samples": fit.samples,
            "fit_trade_rate": fit.trade_rate_shares_sec,
            "fit_cancel_intensity": fit.cancel_intensity_per_sec,
            "fit_kyle_lambda": fit.kyle_lambda,
            "fit_volatility": fit.volatility_annualized,
            "fit_capital_hurdle": fit.capital_hurdle_annual,
        }
        log.info(
            f"Reward Universe Radar: {pages} pages, {len(all_eligible_pools)} eligible pools, "
            f"{len(set(all_token_ids))} tokens, {books_map.fetched_count} books "
            f"({missing_books} skipped for missing books, {missing_mid} for invalid mids), "
            f"{len(candidates)} scored, "
            f"{self.reward_radar_stats['ms_per_pool']:.2f} ms/pool, "
            f"total {elapsed:.2f}s (score {score_elapsed:.3f}s), top mu(0)={top_mu:.2f}; "
            f"queue fit samples={fit.samples}, trade={fit.trade_rate_shares_sec}, "
            f"cancel={fit.cancel_intensity_per_sec}, kyle={fit.kyle_lambda}, "
            f"vol={fit.volatility_annualized}"
        )
        return candidates

    def _run_reward_universe_radar_loop(self) -> None:
        """Continuously sweep the full reward universe and publish measured snapshots.

        The loop records every completed refresh interval. The harvester uses
        that measured distribution as its staleness bound; the loop paces itself
        from the measured sweep duration rather than a hand-set refresh cadence.
        """
        log.info("Starting live Reward Universe Radar thread (full-universe cursor sweep)...")
        first_sweep = True
        while self.running:
            t0 = time.time()
            candidates: Optional[List[RewardPoolCandidate]] = None
            try:
                candidates = self.scan_reward_universe(min_rate=REWARD_MIN_PAYOUT_USD)
            except Exception as e:
                log.exception(f"Error in Reward Universe Radar: {e}")

            if first_sweep:
                # Release the remaining full-universe workers once the first
                # reward sweep (pages + books + scoring) has completed, whether
                # or not it succeeded. A failure is visible to the harvester via
                # the empty/stale snapshot and the measured staleness gate.
                self.reward_radar_ready.set()
                first_sweep = False

            duration = time.time() - t0
            now = time.time()
            if candidates is not None:
                with self.reward_queue_lock:
                    self.reward_candidate_queue = candidates
                    previous_success = self.reward_radar_last_success
                    self.reward_radar_last_success = now
                    if previous_success > 0.0:
                        self.reward_radar_intervals.append(now - previous_success)
                    self.reward_radar_version += 1
                    intervals = list(self.reward_radar_intervals)
                if intervals:
                    ewma = statistics.fmean(intervals)
                    ordered = sorted(intervals)
                    p95 = ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)]
                    log.info(
                        f"Reward Universe Radar: primed {len(candidates)} pools "
                        f"(rate >= ${REWARD_MIN_PAYOUT_USD:.2f}) in {duration:.2f}s; "
                        f"measured refresh EWMA {ewma:.2f}s, p95 {p95:.2f}s (n={len(intervals)})"
                    )
                else:
                    log.info(
                        f"Reward Universe Radar: primed {len(candidates)} pools "
                        f"(rate >= ${REWARD_MIN_PAYOUT_USD:.2f}) in {duration:.2f}s; "
                        f"refresh distribution warming up"
                    )
            try:
                self.stall_watchdog.beat("reward_radar")
            except Exception:
                log.exception("Reward radar watchdog beat failed")

            # Adaptive pacing from the measured sweep duration: equal sweep and
            # rest time (a 50% duty cycle) with no fixed refresh multiple.
            sleep_for = max(1.0, duration)
            deadline = time.time() + sleep_for
            while self.running and time.time() < deadline:
                time.sleep(min(1.0, max(0.05, deadline - time.time())))

    def _reward_microstructure_proxies(
        self,
        cand_map: Dict[str, RewardPoolCandidate],
    ) -> Dict[str, float]:
        """Live proxies for any queue parameter without a fitted observation.

        Every proxy is computed from the current measured universe (24h share
        flow, touch depth, quoted spread, marginal yield, measured refresh
        interval). None of them is a QueuePriorityValuationModel default.
        """
        proxies: Dict[str, float] = {}
        cands = list(cand_map.values())
        if not cands:
            return proxies
        mids = [c.mid_price for c in cands if 0.0 < c.mid_price < 1.0]
        spreads = [c.spread_offset_cents / 100.0 for c in cands if c.spread_offset_cents > 0.0]
        depths = [
            c.touch_bid_depth + c.touch_ask_depth
            for c in cands
            if (c.touch_bid_depth + c.touch_ask_depth) > 0.0
        ]
        volumes = [c.volume_24h_shares for c in cands if c.volume_24h_shares > 0.0]
        median_mid = statistics.median(mids) if mids else None
        median_spread = statistics.median(spreads) if spreads else None
        median_depth = statistics.median(depths) if depths else None
        refresh_s = max(1.0, self._reward_depth_staleness_threshold() or 1.0)

        if volumes:
            proxies["trade_rate_shares_sec"] = statistics.median(volumes) / 86_400.0
        elif median_depth is not None:
            # Queue turnover once per measured refresh is the live proxy when
            # the venue exposes no volume field.
            proxies["trade_rate_shares_sec"] = median_depth / refresh_s
        if proxies.get("trade_rate_shares_sec") and median_depth:
            proxies["cancel_intensity_per_sec"] = proxies["trade_rate_shares_sec"] / max(median_depth, 1e-9)
        if median_spread is not None and median_depth:
            proxies["kyle_lambda"] = median_spread / max(median_depth / 1000.0, 1e-9)
        if median_spread is not None and median_mid:
            proxies["volatility_annualized"] = (
                median_spread / max(median_mid, 1e-9)
            ) * math.sqrt(YEAR_SECONDS / refresh_s)
        top_mu = max((c.marginal_at_zero for c in cands), default=0.0)
        if top_mu > 0.0:
            proxies["capital_hurdle_annual"] = top_mu * 365.25
        return proxies

    def _reward_switching_opportunity_cost(
        self,
        old_allocs: Dict[str, float],
        cand_map: Dict[str, RewardPoolCandidate],
        current_daily_payout: float,
    ) -> float:
        """Measured opportunity cost of cancelling and re-quoting now.

        Queue priority is valued with a model whose parameters are fitted from
        live L2 snapshots (trade rate, cancellation intensity, Kyle impact,
        realised volatility) and the allocator's measured marginal yield as the
        capital hurdle. The fitted model is injected into the migration engine;
        class defaults are never used silently. When a required input has
        neither a measurement nor a live proxy, discretionary switching is
        deferred (infinite cost) and the missing inputs are logged.
        """
        if not old_allocs:
            return 0.0

        proxies = self._reward_microstructure_proxies(cand_map)
        model, fit_info = self.reward_queue_fit.build_model(proxies)
        if model is None:
            missing = [name for name, src in fit_info["sources"].items() if src == "unavailable"]
            self._warn_rate_limited(
                "reward_queue_fit_unavailable",
                f"Reward queue fit unavailable (missing measured inputs: {missing}, "
                f"samples={fit_info['samples']}); discretionary switching deferred.",
            )
            return float("inf")

        self.reward_migration_engine.queue_model = model
        log.info(
            f"Reward queue fit (samples={fit_info['samples']}): "
            + ", ".join(
                f"{name}={value:.6g} ({fit_info['sources'][name]})"
                for name, value in fit_info["values"].items()
            )
        )

        queue_cost = 0.0
        for cid, amt in old_allocs.items():
            c = cand_map.get(cid)
            if c is None or amt <= 0.0:
                continue
            queue_cost += self.reward_migration_engine.compute_queue_loss(
                resting_bid_size=amt,
                resting_ask_size=amt,
                queue_ahead_bid=c.queue_ahead_bid,
                queue_ahead_ask=c.queue_ahead_ask,
                half_spread_cents=max(0.0, c.spread_offset_cents),
                mid_price=c.mid_price,
                queue_model=model,
            )
        downtime = self.reward_migration_engine.compute_downtime_cost(
            daily_yield_a=max(0.0, current_daily_payout),
            latency_seconds=max(0.0, self.last_clob_rtt_ms / 1000.0),
        )
        return queue_cost + downtime

    @staticmethod
    def _measured_reward_marginal(
        allocs: Dict[str, float],
        cand_map: Dict[str, RewardPoolCandidate],
        eligible: List[RewardPoolCandidate],
    ) -> float:
        """Measured marginal daily reward yield ($/day per $) of the allocation.

        Uses the KKT derivative at each above-lot allocation; falls back to the
        realised payout density of the selected set, then to the best measured
        marginal-at-zero in the eligible universe.
        """
        marginal = 0.0
        for cid, amt in allocs.items():
            c = cand_map.get(cid)
            if c is None or amt <= 0.0:
                continue
            alpha = c.alpha
            depth = c.competing_score
            if amt > c.threshold_capital + 1e-9 and depth > 0.0 and alpha > 0.0:
                marginal = max(
                    marginal,
                    c.rate_per_day * alpha * depth / (alpha * amt + depth) ** 2,
                )
        if marginal <= 0.0:
            for cid, amt in allocs.items():
                c = cand_map.get(cid)
                if c is not None and amt > 0.0:
                    marginal = max(marginal, c.payout(amt) / amt)
        if marginal <= 0.0:
            marginal = max((c.marginal_at_zero for c in eligible), default=0.0)
        return marginal

    def _reward_hold_horizon_days(self) -> float:
        """Measured horizon used to amortise a one-time switching cost.

        Prefers the observed hold-duration distribution of past allocations
        (95th percentile). Before any switch has been observed, uses the
        measured radar staleness bound as the minimum plausible holding period.
        """
        with self.reward_queue_lock:
            holds = list(self.reward_hold_durations)
        if holds:
            ordered = sorted(holds)
            horizon_s = ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)]
        else:
            horizon_s = self._reward_depth_staleness_threshold() or 1.0
        return max(1.0, horizon_s) / 86_400.0

    def _run_reward_harvester_loop(self) -> None:
        """Accrue official quadratic rewards from the full measured radar snapshot.

        Allocation is re-solved only when the radar publishes a new measured
        snapshot, so there is no fixed refresh cadence. A quote is never accrued
        against a snapshot older than the measured radar staleness bound. Capital
        moves only when the projected yield gain beats the measured FIFO
        queue-priority and downtime opportunity cost.
        """
        log.info("Starting live RewardHarvester worker thread (full-universe KKT water-filling)...")
        active_candidates: List[RewardPoolCandidate] = []
        reward_allocations: Dict[str, float] = {}
        pool_entry_times: Dict[str, float] = {}
        last_solved_version = -1
        last_quote_time = time.time()
        hold_started = time.time()
        strat = self.metrics["RewardHarvester"]

        while self.running:
            try:
                now = time.time()
                with self.reward_queue_lock:
                    snapshot = list(self.reward_candidate_queue)
                    version = self.reward_radar_version
                    last_success = self.reward_radar_last_success

                # ---- Measured staleness gate (R6) --------------------------------
                threshold = self._reward_depth_staleness_threshold()
                depth_age = (now - last_success) if last_success > 0.0 else None
                if threshold is not None and depth_age is not None and depth_age > threshold:
                    if reward_allocations:
                        strat.release_capital(strat.locked_capital, strat.locked_capital)
                        reward_allocations = {}
                        active_candidates = []
                        pool_entry_times.clear()
                    self._warn_rate_limited(
                        "reward_depth_stale",
                        f"RewardHarvester: radar depth age {depth_age:.1f}s exceeds the measured "
                        f"staleness bound {threshold:.1f}s (n={len(self.reward_radar_intervals)}); "
                        f"accrual stopped and capital released.",
                    )
                    last_quote_time = now
                    time.sleep(1.0)
                    continue

                depth_note = (
                    f"measured depth bound {threshold:.1f}s (age {depth_age:.1f}s)"
                    if threshold is not None and depth_age is not None
                    else "depth staleness bound warming up"
                )

                # ---- Event-driven re-solve from the newest measured snapshot ----
                if version != last_solved_version or not reward_allocations:
                    if not snapshot:
                        if reward_allocations:
                            strat.release_capital(strat.locked_capital, strat.locked_capital)
                            reward_allocations = {}
                            active_candidates = []
                            pool_entry_times.clear()
                            log.warning("RewardHarvester: radar snapshot empty; released allocations.")
                        last_solved_version = version
                        last_quote_time = now
                        time.sleep(1.0)
                        continue

                    # The allocator owns the full measured equity envelope; the
                    # solver already leaves any sub-cent rounding residual. No
                    # hand-set cash reserve is withheld from the reward budget.
                    budget = max(0.0, strat.current_equity)
                    eligible = [c for c in snapshot if c.threshold_capital <= budget]
                    if not eligible:
                        if reward_allocations:
                            strat.release_capital(strat.locked_capital, strat.locked_capital)
                            reward_allocations = {}
                            active_candidates = []
                            pool_entry_times.clear()
                            log.warning("RewardHarvester: no eligible pools; released all locked capital.")
                        last_solved_version = version
                        last_quote_time = now
                        time.sleep(1.0)
                        continue

                    cand_map = {c.pool_id: c for c in eligible}
                    allocs = MarginalRewardAllocator.allocate(eligible, total_budget=budget)
                    # Capital hurdle for the queue model is the measured marginal
                    # daily reward yield of this allocation, annualised; never a
                    # hand-set 12% cost of capital.
                    measured_marginal = self._measured_reward_marginal(allocs, cand_map, eligible)
                    self.reward_queue_fit.set_capital_hurdle(measured_marginal * 365.25)
                    missing = [cid for cid in reward_allocations if cid not in cand_map]

                    if allocs != reward_allocations:
                        projected_new = sum(
                            cand_map[cid].payout(amt) for cid, amt in allocs.items() if cid in cand_map
                        )
                        current_proj = sum(
                            cand_map[cid].payout(amt)
                            for cid, amt in reward_allocations.items()
                            if cid in cand_map
                        )
                        switch_gain = projected_new - current_proj
                        switch_cost = self._reward_switching_opportunity_cost(
                            reward_allocations, cand_map, current_proj
                        )
                        horizon_days = self._reward_hold_horizon_days()
                        # A disappeared pool must be unwound regardless of cost.
                        if (
                            missing
                            or not reward_allocations
                            or switch_gain * horizon_days > switch_cost
                        ):
                            if reward_allocations:
                                self.reward_hold_durations.append(max(0.0, now - hold_started))
                                hold_started = now
                            new_total_locked = sum(allocs.values())
                            cost_delta = new_total_locked - strat.locked_capital
                            if cost_delta > 0:
                                strat.lock_capital(cost_delta)
                            elif cost_delta < 0:
                                strat.release_capital(abs(cost_delta), abs(cost_delta))

                            for cid in allocs:
                                if cid not in reward_allocations:
                                    pool_entry_times[cid] = now
                            for old_cid in list(pool_entry_times.keys()):
                                if old_cid not in allocs:
                                    pool_entry_times.pop(old_cid, None)

                            reward_allocations = allocs
                            lots = [cand_map[cid].threshold_capital for cid in allocs]
                            if lots:
                                log.info(
                                    f"RewardHarvester allocated {len(allocs)} pools: {allocs} | "
                                    f"budget ${budget:.2f}, activation lots "
                                    f"${min(lots):.2f}-${max(lots):.2f}, "
                                    f"switch gain ${switch_gain:+.4f}/d x measured hold "
                                    f"horizon {horizon_days * 24.0:.2f}h vs measured "
                                    f"queue/downtime cost ${switch_cost:.4f}; {depth_note}"
                                )
                            else:
                                log.info(
                                    f"RewardHarvester allocated 0 pools (budget ${budget:.2f}, "
                                    f"no pool clears its measured activation lot)"
                                )
                        else:
                            log.info(
                                f"RewardHarvester held allocation: switch gain "
                                f"${switch_gain:+.4f}/d x measured hold horizon "
                                f"{horizon_days * 24.0:.2f}h does not clear measured FIFO "
                                f"queue + downtime cost ${switch_cost:.4f}"
                            )

                    # Refresh measured candidate objects for held or new allocations.
                    active_candidates = [
                        cand_map[cid] for cid in reward_allocations if cid in cand_map
                    ]
                    last_solved_version = version
                    # Clock reset: accrual starts when the current quotes are live,
                    # not while the snapshot was being fetched and solved.
                    last_quote_time = time.time()
                    now = last_quote_time

                if not active_candidates:
                    last_quote_time = now
                    time.sleep(1.0)
                    continue

                dt = now - last_quote_time
                last_quote_time = now
                if dt <= 0.0:
                    time.sleep(1.0)
                    continue

                # ---- Accrue the official Q_min share per active pool -----------
                total_reward_dt = 0.0
                for cand in active_candidates:
                    cond_id = cand.pool_id
                    allocated_usd = reward_allocations.get(cond_id, 0.0)
                    if allocated_usd <= 0.0:
                        continue
                    # Venue rule: an order must rest at least 3.5 s to qualify.
                    if (now - pool_entry_times.get(cond_id, now)) < 3.5:
                        continue
                    if cand.rate_per_day <= 0.0:
                        continue

                    quote_size = allocated_usd  # $1.00 collateral per YES/NO share pair
                    if quote_size < cand.min_size:
                        continue

                    # Q_one and Q_two each receive the same one-side score for a
                    # collateralised YES-bid + NO-bid pair, so the official Q_min
                    # is the full one-side score (subject to the venue midpoint rule).
                    side_score = compute_quadratic_score(
                        cand.max_spread_cents, cand.spread_offset_cents, quote_size
                    )
                    my_score = compute_minimum_score(
                        side_score, side_score, cand.mid_price, REWARD_C_PENALTY
                    )
                    if my_score <= 0.0:
                        continue
                    tot_score = my_score + max(0.0, cand.competing_score)
                    if tot_score <= 0.0:
                        continue
                    share = my_score / tot_score
                    if cand.rate_per_day * share < REWARD_MIN_PAYOUT_USD:
                        continue
                    total_reward_dt += (cand.rate_per_day / 86_400.0) * share * dt

                if total_reward_dt > 0.0:
                    strat.update_pnl(total_reward_dt)

                time.sleep(1.0)
            except Exception as e:
                log.exception(f"Error in RewardHarvester worker loop: {e}")
                time.sleep(2.0)

    # ==========================================================================
    # Pillar 2: SettlementHarvester (Non-Stop Dynamic Pipeline & Par Redemption)
    # ==========================================================================
    def _run_settlement_radar_thread(self) -> None:
        """Background radar: high-speed multi-stratum paginated scanner across active Gamma & CLOB universe."""
        log.info("Starting live Settlement Candidate Radar thread (full-universe multi-stratum scanner)...")
        last_hazard_error = 0.0
        last_min_order_warn = 0.0
        while self.running:
            try:
                now_dt = datetime.datetime.now(datetime.timezone.utc)
                now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                m7d_iso = (now_dt - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                t6_iso = (now_dt + datetime.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
                t24_iso = (now_dt + datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
                now_plus_48h_dt = now_dt + datetime.timedelta(hours=48)
                now_plus_48h_iso = now_plus_48h_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                # Full pagination per stratum: walk offsets in concurrent waves until
                # the API returns a short page. No fixed 800/600/400 market caps.
                SETTLEMENT_PAGE_SIZE = 100
                SETTLEMENT_DISCOVERY_WAVE = 8  # pages per stratum per batch
                SETTLEMENT_HTTP_CONCURRENCY = 16  # HTTP concurrency only; not a discovery cap
                strata: List[str] = [
                    f"closed=false&active=true&end_date_min={m7d_iso}&end_date_max={now_iso}&order=endDate&ascending=false",
                    f"closed=false&active=true&end_date_min={now_iso}&end_date_max={t6_iso}&order=endDate&ascending=true",
                    f"closed=false&active=true&end_date_min={t6_iso}&end_date_max={t24_iso}&order=endDate&ascending=true",
                    f"closed=false&active=true&end_date_min={t24_iso}&end_date_max={now_plus_48h_iso}&order=endDate&ascending=true",
                    "closed=false&active=true&order=volume24hr&ascending=false",
                ]

                def _fetch_stratum_page(query: str, off: int) -> Optional[List[Dict[str, Any]]]:
                    url = f"{DEFAULT_GAMMA_HOST}/markets?limit={SETTLEMENT_PAGE_SIZE}&offset={off}&{query}"
                    try:
                        resp = self.http_session.get(url, timeout=5.0)
                        if resp.status_code == 200:
                            payload = resp.json()
                            if isinstance(payload, list):
                                return payload
                    except Exception:
                        pass
                    return None

                markets: List[Dict[str, Any]] = []
                seen_cids: Set[str] = set()
                held_cids = {p.condition_id for p in self.settlement_positions}
                total_pages = 0
                failed_pages = 0
                rows_requested = 0
                sweep_started = time.time()

                pending_offsets: Dict[int, int] = {i: 0 for i in range(len(strata))}

                def _fetch_task(task: Tuple[int, int]) -> Tuple[int, Optional[List[Dict[str, Any]]]]:
                    stratum_idx, off = task
                    return stratum_idx, _fetch_stratum_page(strata[stratum_idx], off)

                # All strata advance in parallel: fetch one wave per stratum per
                # round, stop a stratum on a short or failed page, repeat until
                # every stratum is exhausted. No fixed page/market cap.
                while pending_offsets and self.running:
                    tasks: List[Tuple[int, int]] = []
                    for stratum_idx, off in pending_offsets.items():
                        tasks.extend(
                            (stratum_idx, off + w * SETTLEMENT_PAGE_SIZE)
                            for w in range(SETTLEMENT_DISCOVERY_WAVE)
                        )
                    with ThreadPoolExecutor(max_workers=SETTLEMENT_HTTP_CONCURRENCY) as pool:
                        results = list(pool.map(_fetch_task, tasks))

                    grouped: Dict[int, List[Optional[List[Dict[str, Any]]]]] = {
                        stratum_idx: [] for stratum_idx in pending_offsets
                    }
                    for stratum_idx, page in results:
                        grouped[stratum_idx].append(page)

                    for stratum_idx in list(pending_offsets):
                        short_page = False
                        wave_failed = False
                        for page in grouped[stratum_idx]:
                            total_pages += 1
                            rows_requested += SETTLEMENT_PAGE_SIZE
                            if page is None:
                                failed_pages += 1
                                wave_failed = True
                                continue
                            if len(page) < SETTLEMENT_PAGE_SIZE:
                                short_page = True
                            for m in page:
                                cid = m.get("conditionId")
                                if cid and cid not in seen_cids and cid not in held_cids:
                                    seen_cids.add(cid)
                                    markets.append(m)
                        if wave_failed or short_page:
                            del pending_offsets[stratum_idx]
                        else:
                            pending_offsets[stratum_idx] += (
                                SETTLEMENT_PAGE_SIZE * SETTLEMENT_DISCOVERY_WAVE
                            )

                sweep_elapsed = time.time() - sweep_started
                log.info(
                    f"Settlement Radar sweep: {len(strata)} strata, {total_pages} Gamma pages "
                    f"(<= {rows_requested} rows), {len(markets)} unique markets in {sweep_elapsed:.2f}s."
                )
                if failed_pages:
                    log.warning(
                        f"Settlement Radar: {failed_pages} Gamma page(s) failed; "
                        f"affected strata stopped at the failed wave."
                    )

                # Pre-filter candidate tokens passing Oracle Expiration Gate or 48h settlement window
                eligible_tokens: List[Tuple[Dict[str, Any], str, float, float, float, str]] = []
                missing_min_order = 0
                for m in markets:
                    end_date_str = m.get("endDate") or ""
                    uma_end_str = m.get("umaEndTime") or m.get("umaEndDate") or ""
                    uma_status = str(m.get("umaResolutionStatus") or "").lower()
                    uma_statuses = [str(st).lower() for st in (m.get("umaResolutionStatuses") or [])]

                    # Exclude toxic disputed/rejected UMA markets
                    if uma_status in ("disputed", "rejected") or any(st in ("disputed", "rejected") for st in uma_statuses):
                        continue

                    is_closed_market = bool(m.get("closed"))
                    question_l = str(m.get("question") or "").lower()
                    slug_l = str(m.get("market_slug") or m.get("slug") or "").lower()
                    is_crypto_candle = ("up or down" in question_l) or ("up-or-down" in slug_l)
                    if not is_crypto_candle and re.search(r"\b\d{1,2}\s*(am|pm)\b", question_l):
                        is_crypto_candle = any(
                            k in question_l for k in ("above", "below", "dip to", "reach")
                        )
                    is_short_candle = False
                    try:
                        start_dt = datetime.datetime.fromisoformat(
                            str(m.get("startDate") or "").replace("Z", "+00:00")
                        )
                        end_dt = datetime.datetime.fromisoformat(
                            end_date_str.replace("Z", "+00:00")
                        )
                        is_short_candle = 0 < (end_dt - start_dt).total_seconds() <= 6 * 3600
                    except Exception:
                        pass

                    is_past_calendar = bool(end_date_str and end_date_str <= now_iso)
                    is_uma_proposed = bool(
                        uma_end_str
                        or uma_status in ("proposed", "resolved")
                        or any(st in ("proposed", "resolved") for st in uma_statuses)
                    )
                    is_expiring_soon = bool(end_date_str and now_iso < end_date_str <= now_plus_48h_iso)
                    category = str(m.get("category") or "").strip().lower() or "settlement"

                    if not (is_past_calendar or is_uma_proposed or is_expiring_soon):
                        continue

                    # A market whose calendar date passed with no UMA proposal and
                    # no closure is an unresolved-in-fact directional bet, not a
                    # settlement discount — but only once it has outlived the
                    # measured settlement horizon (survival p95 / physical cycle +
                    # Bayesian dispute hazard). No fixed 2h gate.
                    if is_past_calendar and not (is_uma_proposed or is_closed_market):
                        try:
                            end_dt = datetime.datetime.fromisoformat(
                                end_date_str.replace("Z", "+00:00")
                            )
                            physical_cycle_sec = calculate_physical_cycle_time(
                                0.0, is_deterministic_conclusion=True
                            ) * 3600.0
                            horizon_sec = self._measured_horizon_sec(physical_cycle_sec, category)
                            if (now_dt - end_dt).total_seconds() > horizon_sec:
                                continue
                        except Exception:
                            continue
                    # A short-dated candle at 0.94-0.99 is the market's honest
                    # probability on an unresolved event, not a UMA settlement
                    # discount. Only consider it once it has closed or been proposed.
                    if (is_crypto_candle or is_short_candle) and not (is_closed_market or is_uma_proposed):
                        continue

                    # Ground-truth physical capital cycle time from on-chain venue
                    # physics (UMA liveness + relayer queue + redeem + reinvest).
                    hours_to_resolution = 0.0
                    if is_crypto_candle:
                        target_settle_sec = 300.0
                    elif is_uma_proposed:
                        hours_left = 2.0
                        if uma_end_str:
                            try:
                                uma_dt = datetime.datetime.fromisoformat(uma_end_str.replace("Z", "+00:00"))
                                hours_left = max(600.0, (uma_dt - now_dt).total_seconds()) / 3600.0
                            except Exception:
                                pass
                        hours_to_resolution = hours_left
                        target_settle_sec = calculate_physical_cycle_time(
                            hours_left, is_uma_proposed=True
                        ) * 3600.0
                    elif is_past_calendar:
                        target_settle_sec = calculate_physical_cycle_time(
                            0.0, is_deterministic_conclusion=True
                        ) * 3600.0
                    else:
                        try:
                            m_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            sec_left = max(0.0, (m_dt - now_dt).total_seconds())
                            hours_to_resolution = sec_left / 3600.0
                            target_settle_sec = calculate_physical_cycle_time(
                                hours_to_resolution
                            ) * 3600.0
                        except Exception:
                            # Unparseable end date is a data-health failure: skip
                            # rather than inventing a duration.
                            continue

                    raw_prices = m.get("outcomePrices")
                    raw_toks = m.get("clobTokenIds")
                    raw_outcomes = m.get("outcomes")
                    if not raw_prices or not raw_toks or not raw_outcomes:
                        continue

                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                    toks = json.loads(raw_toks) if isinstance(raw_toks, str) else raw_toks
                    outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                    if not isinstance(prices, list) or not isinstance(toks, list) or not isinstance(outcomes, list):
                        continue
                    if not (len(prices) == len(toks) == len(outcomes)):
                        continue
                    if len(set(toks)) != len(toks):
                        continue
                    if not all(isinstance(o, str) and o.strip() for o in outcomes):
                        continue
                    if len(set(outcomes)) != len(outcomes):
                        continue
                    # Validate the (token, outcome, price) mapping as a single unit.
                    token_outcome = dict(zip(toks, outcomes))
                    if len(token_outcome) != len(toks):
                        continue

                    parsed_prices: List[float] = []
                    for p_str in prices:
                        try:
                            parsed_prices.append(float(p_str))
                        except (ValueError, TypeError):
                            parsed_prices = []
                            break
                    if len(parsed_prices) != len(prices):
                        continue

                    # Data-derived outcome selection: the market's own plurality
                    # outcome. No fixed price band chooses the token; the hazard
                    # model and allocator decide whether it is worth funding.
                    win_idx = max(range(len(parsed_prices)), key=lambda j: parsed_prices[j])
                    tid = toks[win_idx]
                    if tid not in token_outcome:
                        continue
                    p_est = parsed_prices[win_idx]

                    # Venue minimum order size (shares) from the same Gamma row.
                    order_min = GammaClient.min_order_size(m)
                    if order_min is None:
                        missing_min_order += 1
                        continue

                    # p_win must be P(the selected outcome wins), not P(the market
                    # resolves). Only a final 0/1 outcome may use the near-certain
                    # settlement probability; a closed or proposed market trading
                    # at 0.51 is still unresolved and uses its implied price net of
                    # the measured dispute hazard.
                    final_outcome = any(p >= 0.999 for p in parsed_prices) or any(
                        p <= 0.001 for p in parsed_prices
                    )
                    deterministic_conclusion = bool(final_outcome and len(outcomes) == 2)
                    if deterministic_conclusion:
                        try:
                            p_win, _h_robust, _hourly_vel = self.hazard_model.evaluate_candidate(
                                category=category,
                                price=p_est,
                                hours_to_resolution=hours_to_resolution,
                                is_uma_proposed=is_uma_proposed,
                                is_deterministic_conclusion=True,
                            )
                        except Exception as hazard_err:
                            error_now = time.time()
                            if error_now - last_hazard_error > 60.0:
                                log.warning(f"Settlement hazard evaluation error: {hazard_err}")
                                last_hazard_error = error_now
                            continue
                    else:
                        p_win = p_est - self.hazard_model.dispute_tracker.get_credible_upper_hazard(category)
                        p_win = max(0.0, min(1.0, p_win))

                    eligible_tokens.append((m, tid, p_win, target_settle_sec, float(order_min), category))

                if missing_min_order:
                    warn_now = time.time()
                    if warn_now - last_min_order_warn > 300.0:
                        log.warning(
                            f"Settlement Radar: {missing_min_order} markets skipped for missing Gamma orderMinSize "
                            f"(cannot validate the venue minimum order)."
                        )
                        last_min_order_warn = warn_now

                # Atomic batch orderbook fetch through the shared /books helper.
                # 100-token chunks / 4 workers bound transient RSS on the
                # 1 GB host; the full eligible universe is still fetched.
                books_by_token = fetch_l2_books_batch(
                    self.http_session,
                    [item[1] for item in eligible_tokens],
                    chunk_size=100,
                    max_workers=4,
                )

                new_candidates: List[QueuedSettlementCandidate] = []
                queued_cids: Set[str] = set()

                for m, tid, p_win, target_settle_sec, order_min_shares, category in eligible_tokens:
                    cid = m.get("conditionId", "")
                    if not cid or cid in queued_cids:
                        continue

                    book = books_by_token.get(tid)
                    if not book:
                        continue
                    raw_asks = book.get("asks") or []

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
                    if not (0.0 < best_ask < 1.0):
                        continue
                    # Genuine data-health checks only: finite/in-range prices and a
                    # non-crossed live book. No price band; the optimizer decides
                    # whether a cheap outcome is worth funding.
                    book_bid = max(
                        (float(b.get("price", 0.0)) for b in (book.get("bids") or []) if isinstance(b, dict)),
                        default=0.0,
                    )
                    if book_bid > 0.0 and best_ask <= book_bid:
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
                            fee_schedule=(
                                FeeSchedule.from_gamma(m["feeSchedule"])
                                if m.get("feeSchedule")
                                else None
                            ),
                            p_win=p_win,
                            min_order_shares=order_min_shares,
                            category=category,
                        )
                    )
                    queued_cids.add(cid)

                new_candidates.sort(key=lambda c: c.annualized_arr, reverse=True)
                with self.queue_lock:
                    self.candidate_queue = new_candidates
                self.settlement_radar_event.set()

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
        """Deploys capital according to the Marginal Settlement Allocator.

        The allocator decides how many markets and how much capital per market from
        the full live ladder, budget, fee schedules and survival probability; this
        method executes the plan and reserves the exact taker fee in local cash
        before committing the order.
        """
        deployed_count = 0
        strat = self.metrics["SettlementHarvester"]
        # No hand-set reserve: the allocator funds the full local cash and each
        # order reserves its exact taker fee before committing.
        usable_cash = max(0.0, strat.available_cash)
        if usable_cash <= 0.0:
            return 0

        with self.queue_lock:
            candidates = list(self.candidate_queue)

        held_cids = {p.condition_id for p in self.settlement_positions}
        pending = [c for c in candidates if c.condition_id not in held_cids]
        if not pending:
            return 0

        # One atomic batch /books sweep through the shared helper.
        book_map = fetch_l2_books_batch(self.http_session, [c.token_id for c in pending])

        # Build live SettlementCandidate objects with full ask ladders.
        alloc_candidates: List[SettlementCandidate] = []
        for cand in pending:
            b_data = book_map.get(cand.token_id)
            if not b_data:
                continue
            raw_asks = b_data.get("asks") or []

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
                continue
            if not (0.0 < ladder[0][0] < 1.0):
                continue
            # Genuine data-health checks only: in-range prices, non-crossed book.
            best_bid = max(
                (float(b.get("price", 0.0)) for b in (b_data.get("bids") or []) if isinstance(b, dict)),
                default=0.0,
            )
            if best_bid > 0.0 and ladder[0][0] <= best_bid:
                continue

            fee_sched = cand.fee_schedule or era_schedule(
                cand.category,
                datetime.datetime.fromtimestamp(cand.discovered_ts, tz=datetime.timezone.utc),
            )

            alloc_candidates.append(
                SettlementCandidate(
                    condition_id=cand.condition_id,
                    token_id=cand.token_id,
                    question=cand.question,
                    target_settle_sec=cand.target_settle_sec,
                    ask_ladder=ladder,
                    fee_schedule=fee_sched,
                    p_win=cand.p_win,
                    min_order_shares=cand.min_order_shares,
                    category=cand.category,
                )
            )

        if not alloc_candidates:
            return 0

        # The allocator owns slot count and per-market capital.
        opt_res = self.settlement_allocator.allocate(
            candidates=alloc_candidates,
            usable_capital=usable_cash,
        )
        # Measured marginal opportunity cost of a freed dollar, used by the
        # early-exit rule instead of a fixed price/gain threshold.
        self.settlement_clearing_velocity = float(opt_res.equalized_marginal_velocity)

        cand_by_cid = {c.condition_id: c for c in alloc_candidates}
        for alloc in opt_res.allocations:
            alloc_cand = cand_by_cid.get(alloc.condition_id)
            if alloc_cand is None:
                continue
            if alloc.allocated_usd <= 0.0 or alloc.shares <= 0.0:
                continue

            fee_sched = alloc_cand.fee_schedule
            scale_ref = alloc.shares

            def _exact_fee(cost: float, shares: float) -> float:
                """Exact taker fee for a scaled execution of this allocation."""
                if alloc.level_fills:
                    ratio = shares / scale_ref if scale_ref > 0 else 0.0
                    return sum(taker_fee(shs * ratio, px, fee_sched) for px, shs, _ in alloc.level_fills)
                return taker_fee(shares, alloc.effective_avg_price, fee_sched)

            fill_shares = alloc.shares
            deploy_cost = alloc.allocated_usd
            taker_fee_usd = _exact_fee(deploy_cost, fill_shares)

            # Reserve the exact taker fee in local cash before committing.
            total_outlay = deploy_cost + taker_fee_usd
            if total_outlay > usable_cash:
                scale = usable_cash / total_outlay
                deploy_cost *= scale
                fill_shares *= scale
                taker_fee_usd = _exact_fee(deploy_cost, fill_shares)
                if deploy_cost + taker_fee_usd > usable_cash:
                    # 5-dp rounding residual: shave the cost by the overflow.
                    overflow = deploy_cost + taker_fee_usd - usable_cash
                    if overflow >= deploy_cost:
                        continue
                    shrink = (deploy_cost - overflow) / deploy_cost
                    deploy_cost *= shrink
                    fill_shares *= shrink
                    taker_fee_usd = _exact_fee(deploy_cost, fill_shares)

            if deploy_cost <= 0.0 or fill_shares + 1e-9 < alloc_cand.min_order_shares:
                continue

            # Mark consumed depth in VirtualBookTracker to eliminate ghost liquidity.
            ratio = fill_shares / alloc.shares if alloc.shares > 0 else 0.0
            for px, sz, _ in alloc.level_fills:
                self.virtual_book.consume(alloc.token_id, px, sz * ratio)
            if not alloc.level_fills and fill_shares > 0:
                self.virtual_book.consume(alloc.token_id, alloc.effective_avg_price, fill_shares)

            strat.lock_capital(deploy_cost)
            strat.update_pnl(0.0, fee=taker_fee_usd, rebate=0.0)
            strat.total_trades += 1
            usable_cash -= deploy_cost + taker_fee_usd
            held_cids.add(alloc.condition_id)

            self.db.record_trade(
                "SettlementHarvester",
                alloc.condition_id,
                "BUY_ASK",
                alloc.effective_avg_price,
                fill_shares,
                gross_pnl=0.0,
                fee=taker_fee_usd,
                rebate=0.0,
                latency_us=25.0,
                details=f"Bought {fill_shares:.1f} shs @ ${alloc.effective_avg_price:.3f} | Taker fee: ${taker_fee_usd:.5f}",
            )

            new_pos = OpenPosition(
                token_id=alloc.token_id,
                condition_id=alloc.condition_id,
                question=alloc.question,
                side="BUY",
                shares=fill_shares,
                entry_price=alloc.effective_avg_price,
                cost_basis=deploy_cost,
                entry_time=time.time(),
                target_settlement_duration_sec=alloc.duration_sec,
                fee_schedule=fee_sched,
                category=alloc_cand.category,
                p_win=alloc_cand.p_win,
            )
            self.settlement_positions.append(new_pos)
            strat.unrealized_par_edge = sum(p.shares * 1.000 - p.cost_basis for p in self.settlement_positions)
            strat.recalculate_equity()
            deployed_count += 1
            par_gain = (fill_shares * 1.000) - deploy_cost
            self._record_event(
                f"{C_YELLOW}[Marginal Settle]{C_RESET} Bought {fill_shares:.0f} shs @ avg ${alloc.effective_avg_price:.3f} "
                f"| Cost: ${deploy_cost:.2f} -> Par: ${fill_shares * 1.000:.2f} (+${par_gain:.2f} par edge, fee: ${taker_fee_usd:.4f}) | "
                f"ARR: {alloc.annualized_arr:,.0f}% ({alloc.question[:15]}...)"
            )

        return deployed_count

    def _measured_horizon_sec(
        self,
        physical_cycle_sec: float,
        category: str = "settlement",
        is_disputed: bool = False,
    ) -> float:
        """Measured settlement horizon for a physical on-chain cycle.

        Extension: Bayesian upper-95 dispute hazard's expected DVM duration, plus
        the full DVM vote cycle when already disputed.  When enough live
        settlements have been observed, the empirical p95 upper-confidence bound
        replaces the prior.  No fixed multiple of the market's own duration.
        """
        prior = self.hazard_model.settlement_horizon_sec(
            physical_cycle_sec,
            category,
            is_disputed=is_disputed,
        )
        return self.settlement_survival.p95_horizon_sec(prior)

    def _settlement_horizon_sec(self, pos: OpenPosition, is_disputed: bool = False) -> float:
        """Measured settlement horizon for an open position."""
        return self._measured_horizon_sec(
            pos.target_settlement_duration_sec,
            pos.category,
            is_disputed=is_disputed,
        )

    def _realize_settlement_loss(self, pos: OpenPosition, now: float, reason: str) -> None:
        """Write off a genuinely unrecoverable position and free its capital."""
        pos.settled = True
        try:
            self.settlement_positions.remove(pos)
        except ValueError:
            pass

        strat = self.metrics["SettlementHarvester"]
        strat.release_capital(cost_amount=pos.cost_basis, payout_amount=0.0, fee=0.0)
        strat.unrealized_par_edge = sum(p.shares * 1.000 - p.cost_basis for p in self.settlement_positions)
        strat.recalculate_equity()
        strat.total_trades += 1

        age_sec = max(0.0, now - pos.entry_time)
        self.db.record_trade(
            "SettlementHarvester",
            pos.condition_id,
            "PAR_LOSS",
            0.0,
            pos.shares,
            -pos.cost_basis,
            fee=0.0,
            rebate=0.0,
            latency_us=18.0,
            details=f"Unrecoverable: {reason} | Write-off: -${pos.cost_basis:.2f} after {age_sec / 60.0:.1f}m",
        )
        log.warning(
            f"Settlement position {pos.condition_id[:14]} unrecoverable: {reason}; "
            f"writing off ${pos.cost_basis:.2f} and freeing capital."
        )
        self._record_event(
            f"{C_RED}[Par Writeoff]{C_RESET} {reason} | Loss: -${pos.cost_basis:.2f}"
        )

    def _run_settlement_harvester_loop(self) -> None:
        """High-utilization settlement harvester with instantaneous recycling upon redemption."""
        log.info("Starting live SettlementHarvester worker thread (Non-Stop Dynamic Pipeline)...")
        last_batch_poll_error = 0.0
        last_outcome_error = 0.0

        while self.running:
            try:
                now = time.time()
                capital_unlocked = False

                # 1. Process existing open positions for settlement redemption (batched resolution check)
                unique_cids = list({p.condition_id for p in self.settlement_positions if p.condition_id})
                market_batch_map: Dict[str, Dict[str, Any]] = {}
                poll_ok = True
                if unique_cids:
                    try:
                        params = [("condition_ids", cid) for cid in unique_cids]
                        m_resp = self.http_session.get(f"{DEFAULT_GAMMA_HOST}/markets", params=params, timeout=5.0)
                        if m_resp.status_code == 200:
                            for item in m_resp.json():
                                cid_val = str(item.get("conditionId") or item.get("condition_id") or "").lower()
                                if cid_val:
                                    market_batch_map[cid_val] = item
                        else:
                            poll_ok = False
                            if now - last_batch_poll_error > 60.0:
                                log.warning(f"Batch resolution poll returned HTTP {m_resp.status_code}")
                                last_batch_poll_error = now
                    except Exception as exc:
                        poll_ok = False
                        if now - last_batch_poll_error > 60.0:
                            log.warning(f"Batch resolution poll error: {exc}")
                            last_batch_poll_error = now

                open_token_ids = [p.token_id for p in self.settlement_positions if not p.settled]
                position_books = (
                    fetch_l2_books_batch(self.http_session, open_token_ids, chunk_size=50, max_workers=4)
                    if open_token_ids else {}
                )

                for pos in list(self.settlement_positions):
                    if pos.settled:
                        continue

                    # Data-health gate: a failed poll is NOT evidence that the
                    # market is missing. Never book a loss from one failed poll.
                    if not poll_ok:
                        if not pos.data_stale:
                            pos.data_stale = True
                            log.warning(
                                f"DATA_STALE Settlement position {pos.condition_id[:14]} "
                                f"(age {(now - pos.entry_time) / 60.0:.1f}m): Gamma poll failed; "
                                f"holding position, slot released for redeployment."
                            )
                            self._record_event(
                                f"{C_YELLOW}[DATA_STALE]{C_RESET} Gamma poll failed; holding "
                                f"{pos.shares:.0f} shs of {pos.condition_id[:12]}..."
                            )
                        continue
                    if pos.data_stale:
                        pos.data_stale = False
                        log.info(
                            f"Settlement data recovered for {pos.condition_id[:14]} "
                            f"after {(now - pos.entry_time) / 60.0:.1f}m."
                        )

                    m_info = market_batch_map.get(pos.condition_id.lower())
                    if not m_info:
                        # Successful poll, market absent: start the missing clock
                        # and only realise once the position has outlived the
                        # measured survival horizon.
                        if pos.missing_since is None:
                            pos.missing_since = now
                        age = now - pos.entry_time
                        horizon = self._settlement_horizon_sec(pos)
                        if age >= horizon:
                            self._realize_settlement_loss(
                                pos,
                                now,
                                f"missing from Gamma (age {age / 60.0:.1f}m >= measured horizon {horizon / 60.0:.1f}m)",
                            )
                            capital_unlocked = True
                            continue
                        if not pos.stale_warned:
                            log.warning(
                                f"DATA_STALE Settlement position {pos.condition_id[:14]} missing from "
                                f"Gamma; age {age / 60.0:.1f}m < measured horizon "
                                f"{horizon / 60.0:.1f}m (n={self.settlement_survival.sample_size}); "
                                f"capital locked, slot released."
                            )
                            pos.stale_warned = True
                        continue
                    pos.missing_since = None
                    pos.stale_warned = False

                    is_closed = bool(m_info.get("closed"))
                    uma_status = str(m_info.get("umaResolutionStatus") or "").lower()
                    raw_prices = m_info.get("outcomePrices")
                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])
                    raw_toks = m_info.get("clobTokenIds")
                    toks = json.loads(raw_toks) if isinstance(raw_toks, str) else (raw_toks or [])
                    raw_outcomes = m_info.get("outcomes")
                    outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or [])
                    if (
                        not isinstance(prices, list)
                        or not isinstance(toks, list)
                        or not isinstance(outcomes, list)
                        or not (len(prices) == len(toks) == len(outcomes))
                        or len(set(toks)) != len(toks)
                        or not all(isinstance(o, str) and o.strip() for o in outcomes)
                        or len(set(outcomes)) != len(outcomes)
                    ):
                        if now - last_outcome_error > 60.0:
                            log.warning(
                                f"Settlement outcome/token/price mismatch for {pos.condition_id[:14]}: "
                                f"rejecting resolution check this poll."
                            )
                            last_outcome_error = now
                        continue
                    # Validate the (token, outcome, price) mapping as a single unit.
                    token_outcome = dict(zip(toks, outcomes))
                    if len(token_outcome) != len(toks) or pos.token_id not in token_outcome:
                        if now - last_outcome_error > 60.0:
                            log.warning(
                                f"Settlement position token is not in the market's validated token/outcome "
                                f"mapping for {pos.condition_id[:14]}; rejecting resolution check."
                            )
                            last_outcome_error = now
                        continue

                    # 1. Early Harvest Check via Secondary CLOB Market:
                    # Exit only when the live bid, net of the exact taker fee, is
                    # profitable AND beats holding to settlement once the freed
                    # capital is redeployed at the allocator's measured clearing
                    # velocity. No fixed price or par-gain threshold.
                    best_bid = 0.0
                    book = position_books.get(pos.token_id, {})
                    bids = book.get("bids", [])
                    if bids:
                        best_bid = max(
                            (float(b.get("price", 0.0)) for b in bids if isinstance(b, dict)),
                            default=0.0,
                        )

                    exit_fee = 0.0
                    should_exit = False
                    if best_bid > 0.0 and pos.p_win > 0.0 and pos.shares > 0.0:
                        exit_sched = pos.fee_schedule or era_schedule(
                            pos.category,
                            datetime.datetime.fromtimestamp(pos.entry_time, tz=datetime.timezone.utc),
                        )
                        exit_fee = taker_fee(pos.shares, best_bid, exit_sched)
                        fee_per_share = exit_fee / pos.shares
                        net_bid = best_bid - fee_per_share
                        remaining_sec = max(
                            0.0, pos.target_settlement_duration_sec - (now - pos.entry_time)
                        )
                        recycle_yield = self.settlement_clearing_velocity * (
                            remaining_sec / 31_536_000.0
                        )
                        should_exit = net_bid > pos.entry_price and net_bid * (1.0 + recycle_yield) > pos.p_win

                    if should_exit:
                        exit_proceeds = pos.shares * best_bid
                        gross_pnl = exit_proceeds - pos.cost_basis
                        net_pnl = gross_pnl - exit_fee

                        pos.settled = True
                        self.settlement_positions.remove(pos)
                        strat = self.metrics["SettlementHarvester"]
                        strat.release_capital(
                            cost_amount=pos.cost_basis,
                            payout_amount=exit_proceeds,
                            fee=exit_fee,
                        )
                        strat.unrealized_par_edge = sum(p.shares * 1.000 - p.cost_basis for p in self.settlement_positions)
                        strat.recalculate_equity()
                        strat.total_trades += 1
                        self.db.record_trade(
                            "SettlementHarvester",
                            pos.condition_id,
                            "CLOB_EXIT",
                            best_bid,
                            pos.shares,
                            gross_pnl,
                            fee=exit_fee,
                            rebate=0.0,
                            latency_us=20.0,
                            details=f"Early harvest {pos.shares:.1f} shs @ ${best_bid:.3f} | Net: +${net_pnl:.4f}",
                        )
                        self._record_event(
                            f"{C_CYAN}[Early Settle Exit]{C_RESET} Sold {pos.shares:.0f} shs @ ${best_bid:.3f} | Net: +${net_pnl:.4f}"
                        )
                        capital_unlocked = True
                        continue

                    # 2. On-Chain Resolution / Final Outcome Check
                    # `outcomePrices` on a live market is the last traded price, not a
                    # resolution event. Requiring `closed` plus final 0/1 prices stops
                    # open positions being force-settled by an indicative 0.99 print.
                    if uma_status in ("disputed", "rejected"):
                        # A dispute can legitimately consume a full DVM vote cycle;
                        # use the dispute-inclusive measured horizon.
                        if (now - pos.entry_time) >= self._settlement_horizon_sec(pos, is_disputed=True):
                            self._realize_settlement_loss(
                                pos,
                                now,
                                f"disputed for {(now - pos.entry_time) / 3600.0:.1f}h >= dispute-inclusive measured horizon",
                            )
                            capital_unlocked = True
                        continue

                    final_prices: List[float] = []
                    for p_val in prices:
                        try:
                            final_prices.append(float(p_val))
                        except (ValueError, TypeError):
                            pass
                    if len(final_prices) != len(prices):
                        if now - last_outcome_error > 60.0:
                            log.warning(
                                f"Unparseable outcome price for {pos.condition_id[:14]}; "
                                f"rejecting resolution check this poll."
                            )
                            last_outcome_error = now
                        continue
                    has_resolved_outcome = (
                        is_closed
                        and len(final_prices) >= 2
                        and all(p <= 1e-6 or p >= 1.0 - 1e-6 for p in final_prices)
                    )
                    if not has_resolved_outcome:
                        # Not finalised yet. Only realise when the position has
                        # outlived the measured settlement survival horizon.
                        horizon_sec = self._settlement_horizon_sec(pos)
                        if (now - pos.entry_time) >= horizon_sec:
                            self._realize_settlement_loss(
                                pos,
                                now,
                                f"unresolved after {(now - pos.entry_time) / 3600.0:.1f}h >= measured horizon {horizon_sec / 3600.0:.1f}h",
                            )
                            capital_unlocked = True
                        continue

                    # Authentic winning outcome verification: match position token against outcome with price >= 0.999
                    is_win = False
                    for p_i, p_val in enumerate(final_prices):
                        if p_val >= 0.999 and toks[p_i] == pos.token_id:
                            is_win = True
                            break

                    self.settlement_survival.record(now - pos.entry_time)
                    pos.settled = True
                    self.settlement_positions.remove(pos)
                    strat = self.metrics["SettlementHarvester"]
                    strat.unrealized_par_edge = sum(p.shares * 1.000 - p.cost_basis for p in self.settlement_positions)
                    strat.recalculate_equity()

                    if is_win:
                        unit_payout = 1.000
                    else:
                        tok_idx = toks.index(pos.token_id)
                        unit_payout = final_prices[tok_idx]

                    if unit_payout > 0.0:
                        par_proceeds = pos.shares * unit_payout
                        gross_pnl = par_proceeds - pos.cost_basis
                        gas_fee = 0.00145
                        net_pnl = gross_pnl - gas_fee

                        self.metrics["SettlementHarvester"].release_capital(
                            cost_amount=pos.cost_basis,
                            payout_amount=par_proceeds,
                            fee=gas_fee,
                        )
                        self.metrics["SettlementHarvester"].total_trades += 1
                        side_tag = "PAR_REDEEM" if is_win else "PAR_REFUND"
                        self.db.record_trade(
                            "SettlementHarvester",
                            pos.condition_id,
                            side_tag,
                            unit_payout,
                            pos.shares,
                            gross_pnl,
                            fee=gas_fee,
                            rebate=0.0,
                            latency_us=18.0,
                            details=f"Redeemed {pos.shares:.1f} shs @ par ${unit_payout:.3f} (entry ${pos.entry_price:.3f}) | Gas: ${gas_fee:.5f}",
                        )
                        self._record_event(
                            f"{C_CYAN}[Par Harvest]{C_RESET} Redeemed {pos.shares:.0f} shs @ ${unit_payout:.3f} (entry ${pos.entry_price:.3f}) | Net: +${net_pnl:.4f}"
                        )
                        capital_unlocked = True
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

                # 2. Instantaneous non-stop redeployment into queued candidates.
                # The allocator decides the slot count from capital and ladder depth;
                # deployment fires when capital is freed or the radar refreshed.
                strat = self.metrics["SettlementHarvester"]
                usable_cash = max(0.0, strat.available_cash)

                if capital_unlocked or self.settlement_radar_event.is_set():
                    self.settlement_radar_event.clear()
                    if usable_cash > 0.0:
                        self._deploy_pipeline_capital()

                time.sleep(1.0)
            except Exception:
                log.exception("Error in SettlementHarvester worker loop")
                time.sleep(2.0)

    # ==========================================================================
    # Pillar 3: CtfMinter (Real-Time Binary Complete-Set Parity Arbitrage)
    # ==========================================================================
    def _run_ctf_minter_loop(self, max_iterations: Optional[int] = None) -> None:
        """Continuously scans the full active binary universe for complete-set parity arbitrage.

        Execution architecture:
        1. Instantaneous Taker Sweeping: executes when resting books dislocate
           (sum bids > $1.000 or sum asks < $1.000), sweeping the full ladder.
        2. Selective Maker Parity Quoting: quotes inside-spread complementary bids,
           with fill probability and fill size measured from the venue trade tape.
           Broken legs are unwound by walking the actual bid ladder (C4).
        """
        log.info("Starting live CtfMinter worker thread with full-universe dual-mode parity engine...")
        self._ensure_stall_watchdog()
        last_scan_time = 0.0
        last_scan_error_log = 0.0
        last_scan_stats_log = 0.0
        last_market_refresh = 0.0
        last_measurement_refresh = 0.0
        cached_market_tokens: List[Tuple[Dict[str, Any], List[str], FeeSchedule]] = []
        cached_all_token_ids: List[str] = []
        sweep_cache: Dict[
            str, Tuple[Any, SingleMarketSweepResult, SingleMarketSweepResult, Optional[ParityLegDepth], Optional[ParityLegDepth]]
        ] = {}
        # Rolling measured-tape history: 5,000 prints is ~10 refresh windows of
        # the 500-print page, a memory/compute bound on the empirical distribution.
        flow_sizes: deque = deque(maxlen=5000)
        flow_notionals: deque = deque(maxlen=5000)
        flow_all_sizes: deque = deque(maxlen=5000)
        flow_all_notionals: deque = deque(maxlen=5000)
        fill_model = _MeasuredFillModel((), 0.0)
        measured_gwei: Optional[float] = None
        measured_pol: Optional[float] = None

        # Book-scan scheduling cadence (not an economic threshold). It is also the
        # minimum quote lifetime used by the Poisson fill model.
        SCAN_CADENCE_S = 1.5
        # Operational refresh cadences: the universe and measured flow/oracles
        # refresh independently so a slow/failed universe cannot re-fetch the
        # tape on every scan.
        UNIVERSE_REFRESH_S = 60.0
        MEASUREMENT_REFRESH_S = 60.0
        STATS_LOG_INTERVAL_S = 60.0
        # Data API page size (same default as DataAPIClient.paginate).
        TAPE_PAGE_SIZE = 500

        # Venue page size is 100 regardless of a larger requested limit. Discovery
        # continues until the API returns a short page: there is no top-N cap.
        GAMMA_PAGE_SIZE = 100
        DISCOVERY_WAVE = 8  # HTTP concurrency only; not a discovery cap

        def _refresh_market_universe() -> Optional[Tuple[List[Tuple[Dict[str, Any], List[str], FeeSchedule]], List[str]]]:
            started = time.time()
            raw_markets, pages, reason = _paginate_gamma_universe(
                self.ctf_http_session,
                page_size=GAMMA_PAGE_SIZE,
                wave=DISCOVERY_WAVE,
                is_running=lambda: self.running,
            )
            if raw_markets is None:
                _warn_rate_limited(
                    "ctf_universe",
                    "CtfMinter Gamma pagination failed before any page; keeping previous universe.",
                )
                return None
            seen_ids: Set[str] = set()
            m_tokens: List[Tuple[Dict[str, Any], List[str], FeeSchedule]] = []
            all_toks: List[str] = []
            for m in raw_markets:
                mid = str(m.get("id", ""))
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                raw_toks = m.get("clobTokenIds")
                try:
                    toks = json.loads(raw_toks) if isinstance(raw_toks, str) else (raw_toks or [])
                except (TypeError, ValueError) as exc:
                    _warn_rate_limited(
                        "ctf_market_parse",
                        f"CtfMinter skipped market {mid} with unparseable clobTokenIds: {exc}",
                    )
                    continue
                if len(toks) == 2 and toks[0] and toks[1]:
                    sched_dict = m.get("feeSchedule")
                    fee_sched = FeeSchedule.from_gamma(sched_dict) if sched_dict else FeeSchedule.zero()
                    m_tokens.append((m, toks, fee_sched))
                    all_toks.extend(toks)
            log.info(
                f"CtfMinter refreshed FULL universe: {len(m_tokens)} active binary markets "
                f"({len(set(all_toks))} unique tokens) over {pages} Gamma pages "
                f"({reason}) in {time.time() - started:.2f}s"
            )
            return m_tokens, list(set(all_toks))

        def _refresh_measurements() -> Tuple[Optional[float], Optional[float]]:
            """C10: measure taker flow, seller share, fill sizes and gas/POL live."""
            nonlocal fill_model
            sell_sizes: List[float] = []
            sell_notionals: List[float] = []
            all_sizes: List[float] = []
            all_notionals: List[float] = []
            try:
                r = self.ctf_http_session.get(
                    f"https://data-api.polymarket.com/v2/trades?limit={TAPE_PAGE_SIZE}",
                    timeout=5.0,
                )
                if r.status_code == 200:
                    rows = (r.json() or {}).get("data") or []
                    for t in rows:
                        try:
                            sz = float(t.get("size") or 0.0)
                            px = float(t.get("price") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if sz <= 0.0 or not (0.0 < px <= 1.0):
                            continue
                        notional = sz * px
                        all_sizes.append(sz)
                        all_notionals.append(notional)
                        if str(t.get("side", "")).upper() == "SELL":
                            sell_sizes.append(sz)
                            sell_notionals.append(notional)
                else:
                    _warn_rate_limited(
                        "ctf_trade_tape",
                        f"Data API trade tape returned HTTP {r.status_code}; keeping previous measured fill model.",
                    )
            except Exception as exc:  # noqa: BLE001 - keep the last measured model
                _warn_rate_limited(
                    "ctf_trade_tape",
                    f"Data API trade tape failed; keeping previous measured fill model: {exc}",
                )
            if sell_sizes and all_sizes:
                flow_sizes.extend(sell_sizes)
                flow_notionals.extend(sell_notionals)
                flow_all_sizes.extend(all_sizes)
                flow_all_notionals.extend(all_notionals)
                sell_count_share = (
                    len(flow_sizes) / len(flow_all_sizes) if len(flow_all_sizes) else 0.0
                )
                sell_notional_share = (
                    sum(flow_notionals) / sum(flow_all_notionals)
                    if sum(flow_all_notionals) > 0.0 else 0.0
                )
                mean_size = (
                    statistics.fmean(flow_all_sizes) if len(flow_all_sizes) else 0.0
                )
                fill_model = _MeasuredFillModel(
                    flow_sizes,
                    statistics.fmean(flow_notionals) if flow_notionals else 0.0,
                    mean_size=mean_size,
                    sell_notional_share=sell_notional_share,
                    sell_count_share=sell_count_share,
                )
                mean_sz, p99_sz = fill_model.stats(5.0)
                log.info(
                    f"CtfMinter measured taker flow: {len(fill_model)} SELL prints "
                    f"of {len(flow_all_sizes)} | mean {mean_sz:.2f} shs | p99 {p99_sz:.2f} shs | "
                    f"mean SELL notional ${fill_model.mean_notional:.2f} | "
                    f"seller share: {fill_model.sell_count_share * 100:.1f}% by count, "
                    f"{fill_model.sell_notional_share * 100:.1f}% by notional"
                )
            gwei: Optional[float] = None
            pol: Optional[float] = None
            try:
                from polymarketbot.trading.ctf import (
                    get_dynamic_gas_price_gwei,
                    get_dynamic_pol_usd_price,
                )

                gwei = float(get_dynamic_gas_price_gwei())
                pol = float(get_dynamic_pol_usd_price())
            except Exception as exc:  # noqa: BLE001 - allocator keeps its own cached oracle
                _warn_rate_limited(
                    "ctf_gas_oracle",
                    f"CtfMinter gas/POL oracle unavailable; allocator cache will be used: {exc}",
                )
            return gwei, pol


        iterations_run = 0
        while self.running:
            if max_iterations is not None and iterations_run >= max_iterations:
                break
            iterations_run += 1
            try:
                now = time.time()

                # C7: heartbeat each iteration; a blocked scan stops beating.
                self.stall_watchdog.beat("ctf_scan")

                # Refresh the FULL universe on its own cadence.
                if now - last_market_refresh >= UNIVERSE_REFRESH_S or not cached_market_tokens:
                    try:
                        refreshed = _refresh_market_universe()
                        if refreshed is not None:
                            new_tokens, new_ids = refreshed
                            if new_tokens:
                                cached_market_tokens = new_tokens
                                cached_all_token_ids = new_ids
                                sweep_cache.clear()
                            else:
                                _warn_rate_limited(
                                    "ctf_universe",
                                    "CtfMinter universe refresh returned 0 markets; keeping previous universe.",
                                )
                    except Exception as exc:  # noqa: BLE001 - keep the previous universe
                        _warn_rate_limited(
                            "ctf_refresh",
                            f"CtfMinter universe refresh error; keeping previous state: {exc}",
                        )
                    last_market_refresh = now

                # Measure the flow tape and gas/POL oracles on their own cadence,
                # independent of whether the universe cache is populated.
                if now - last_measurement_refresh >= MEASUREMENT_REFRESH_S:
                    try:
                        gwei, pol = _refresh_measurements()
                        if gwei is not None:
                            measured_gwei = gwei
                        if pol is not None:
                            measured_pol = pol
                    except Exception as exc:  # noqa: BLE001 - keep the last measured values
                        _warn_rate_limited(
                            "ctf_measurements",
                            f"CtfMinter measurement refresh error; keeping previous values: {exc}",
                        )
                    last_measurement_refresh = now

                # Fast book scanning cadence (1.5 seconds)
                if now - last_scan_time >= SCAN_CADENCE_S and cached_market_tokens:
                    dt = max(SCAN_CADENCE_S, now - last_scan_time)
                    last_scan_time = now
                    try:
                        scan_started = time.time()
                        # Full-universe batch POST /books; complete ladders are
                        # retained. 16 concurrent chunks is an I/O compute budget,
                        # not a discovery cap.
                        books = fetch_l2_books_batch(
                            self.ctf_http_session, cached_all_token_ids, chunk_size=125, max_workers=4
                        )
                        books_done = time.time()
                        if cached_all_token_ids and not books:
                            _warn_rate_limited(
                                "ctf_books",
                                f"CtfMinter batch book fetch returned 0/{len(cached_all_token_ids)} books; "
                                "retrying next scan.",
                            )

                        # Maker flow inputs are exclusively the measured tape model
                        # (C10). If it is empty, no maker quote can be priced from
                        # measured flow and the maker path stays idle.
                        local_fill_model = fill_model
                        if len(fill_model) == 0:
                            _warn_rate_limited(
                                "ctf_fill_model_empty",
                                "CtfMinter maker path idle: no measured trade-tape fill model "
                                "yet (tape fetch unavailable or no SELL prints).",
                            )

                        def _levels_sig(levels: Any) -> Tuple[Tuple[float, float], ...]:
                            out: List[Tuple[float, float]] = []
                            for lv in levels or []:
                                try:
                                    px, sz = _level_px_size(lv)
                                except Exception:  # noqa: BLE001
                                    continue
                                out.append((round(px, 6), round(sz, 4)))
                            return tuple(out)

                        def _make_maker_sweep(
                            market_id: str,
                            cond_id: str,
                            q0: float,
                            q1: float,
                            cap: float,
                            ev_per_share: float,
                            gas_merge: float,
                        ) -> SingleMarketSweepResult:
                            cps = q0 + q1
                            net_before = cap * ev_per_share
                            tranche = ParityTranche(
                                tranche_index=0,
                                start_shares=0.0,
                                end_shares=cap,
                                capacity_shares=cap,
                                leg_prices=[q0, q1],
                                leg_unit_fees=[0.0, 0.0],
                                capital_per_set=cps,
                                gross_proceeds_per_set=1.0,
                                net_proceeds_per_set=1.0,
                                marginal_profit_per_set=ev_per_share,
                                marginal_return_per_dollar=(ev_per_share / cps) if cps > 0 else 0.0,
                                cumulative_shares=cap,
                                cumulative_capital=cap * cps,
                                cumulative_gross_profit=cap * (1.0 - cps),
                                cumulative_net_fees=0.0,
                                cumulative_net_profit_before_gas=net_before,
                            )
                            net = round(net_before - gas_merge, 4)
                            roi = ((net / (cap * cps)) * 10_000.0) if cap > 0 and cps > 0 else 0.0
                            return SingleMarketSweepResult(
                                market_id=market_id,
                                condition_id=cond_id,
                                direction=ArbitrageDirection.MERGE_UNWIND,
                                execution_mode=ExecutionMode.SELECTIVE_MAKER,
                                tranches=[tranche],
                                gas_cost_usd=gas_merge,
                                optimal_shares=cap,
                                optimal_capital=cap * cps,
                                optimal_gross_profit=round(cap * (1.0 - cps), 4),
                                optimal_net_fees=0.0,
                                optimal_rebates=0.0,
                                optimal_net_profit=net,
                                optimal_roi_bps=round(roi, 2),
                                marginal_return_at_opt=(ev_per_share / cps) if cps > 0 else 0.0,
                                breakeven_shares=0.0,
                                breakeven_capital=0.0,
                                is_viable=(cap > 0.0 and net >= self.ctf_allocator.min_net_edge_usd),
                                taker_leg_index=None,
                                summary=(
                                    f"Maker parity q0=${q0:.4f} q1=${q1:.4f} cap={cap:.1f} shs "
                                    f"| p_fill={ev_per_share:.6f}/sh EV"
                                ),
                            )

                        parity_candidates: List[SingleMarketSweepResult] = []
                        candidate_meta: Dict[str, Tuple[Dict[str, Any], str, float, float]] = {}
                        maker_meta: Dict[str, Dict[str, Any]] = {}
                        new_sweep_cache: Dict[
                            str, Tuple[Any, SingleMarketSweepResult, SingleMarketSweepResult, Optional[ParityLegDepth], Optional[ParityLegDepth]]
                        ] = {}

                        for m, toks, fee_sched in cached_market_tokens:
                            cond_id = str(m.get("conditionId") or "")
                            if not cond_id:
                                continue
                            q_title = str(m.get("question") or "Unknown")
                            raw_tick = m.get("orderPriceMinTickSize")
                            try:
                                tick = float(raw_tick)
                            except (TypeError, ValueError):
                                tick = 0.0
                            raw_min_order = m.get("orderMinSize")
                            try:
                                min_order_size = float(raw_min_order)
                            except (TypeError, ValueError):
                                min_order_size = 0.0
                            if not math.isfinite(tick) or tick <= 0.0:
                                tick = 0.01  # venue default tick
                                _warn_rate_limited(
                                    "ctf_venue_tick",
                                    f"CtfMinter market {cond_id[:12]} has missing/invalid "
                                    f"orderPriceMinTickSize={raw_tick!r}; using venue default 0.01.",
                                )
                            if not math.isfinite(min_order_size) or min_order_size <= 0.0:
                                min_order_size = 5.0  # venue default minimum order
                                _warn_rate_limited(
                                    "ctf_venue_min_order",
                                    f"CtfMinter market {cond_id[:12]} has missing/invalid "
                                    f"orderMinSize={raw_min_order!r}; using venue default 5.0 shares.",
                                )

                            bk_0 = books.get(toks[0], {})
                            bk_1 = books.get(toks[1], {})
                            bids0 = bk_0.get("bids", [])
                            bids1 = bk_1.get("bids", [])
                            asks0 = bk_0.get("asks", [])
                            asks1 = bk_1.get("asks", [])

                            if not (bids0 or asks0) or not (bids1 or asks1):
                                continue

                            bb0 = _best_price(bids0, highest=True)
                            bb1 = _best_price(bids1, highest=True)

                            ba0 = _best_price(asks0, highest=False)
                            ba1 = _best_price(asks1, highest=False)
                            q0 = round((math.floor(bb0 / tick) + 1) * tick, 6)
                            q1 = round((math.floor(bb1 / tick) + 1) * tick, 6)
                            quote_cost = q0 + q1
                            vol24 = float(m.get("volume24hr", 0) or 0)
                            avg_notional = local_fill_model.mean_notional
                            sell_notional_share = local_fill_model.sell_notional_share
                            # Analytic dominance bounds (zero-fee, zero-gas upper bound):
                            # deeper levels are never better than the top of book, so a
                            # market whose best-bid sum <= par can never fund a mint, and
                            # one whose best-ask sum >= par can never fund a merge.
                            mint_possible = bool(bids0) and bool(bids1) and (bb0 + bb1) > 1.0
                            merge_possible = bool(asks0) and bool(asks1) and (ba0 + ba1) < 1.0
                            maker_price_ok = (
                                bb0 > 0.0 and bb1 > 0.0
                                and quote_cost < 1.0
                                and not ((ba0 > 0.0 and q0 >= ba0) or (ba1 > 0.0 and q1 >= ba1))
                                and vol24 > 0.0 and avg_notional > 0.0 and sell_notional_share > 0.0
                            )
                            p_leg = 0.0
                            ref = 0.0
                            ref_p99 = 0.0
                            gas_merge = 0.0
                            rebate_ref = 0.0
                            ev_upper = -1.0
                            if maker_price_ok:
                                lam = (vol24 * sell_notional_share / avg_notional) / 86400.0
                                p_leg = 1.0 - math.exp(-lam * dt)
                                if p_leg > 0.0:
                                    ref_mean, ref_p99 = local_fill_model.stats(min_order_size)
                                    if ref_mean <= 0.0:
                                        ref_mean = local_fill_model.mean_size
                                    if ref_mean <= 0.0:
                                        _warn_rate_limited(
                                            "ctf_fill_ref",
                                            "CtfMinter has no measured fill size at or above the "
                                            f"venue minimum ({min_order_size:g}) for {cond_id[:12]}; "
                                            "using the venue minimum as the size reference for this scan.",
                                        )
                                        ref_mean = min_order_size
                                    # orderMinSize is the venue floor for the resting order.
                                    ref = max(min_order_size, ref_mean)
                                    if ref_p99 <= 0.0:
                                        ref_p99 = ref
                                    gas_merge = self.ctf_allocator.compute_gas_cost_usd(
                                        ArbitrageDirection.MERGE_UNWIND, measured_gwei, measured_pol
                                    )
                                    rebate_ref = maker_fee_rebate(q0, ref, fee_sched) + maker_fee_rebate(
                                        q1, ref, fee_sched
                                    )
                                    # Upper bound: a broken leg always exits below our inside
                                    # quote, so loss >= 0. If EV with zero broken-leg loss is
                                    # not positive, the real EV cannot be; no bid parse/walk needed.
                                    ev_upper = (
                                        p_leg * p_leg * (1.0 - quote_cost) * ref
                                        + p_leg * rebate_ref
                                        - gas_merge
                                    )
                            maker_possible = maker_price_ok and ev_upper > 0.0
                            need_bid_legs = mint_possible or maker_possible

                            sig = (
                                _levels_sig(bids0) if need_bid_legs else None,
                                _levels_sig(bids1) if need_bid_legs else None,
                                _levels_sig(asks0) if merge_possible else None,
                                _levels_sig(asks1) if merge_possible else None,
                                measured_gwei,
                                measured_pol,
                            )
                            cached = sweep_cache.get(cond_id)
                            if cached is not None and cached[0] == sig:
                                sweep_mint, sweep_merge, leg0_bid, leg1_bid = cached[1], cached[2], cached[3], cached[4]
                            else:
                                leg0_bid = leg1_bid = None
                                if need_bid_legs:
                                    # 1. Mint parity: mint $1.00 complete sets, sell the full bid ladder.
                                    leg0_bid = ParityLegDepth.from_raw(
                                        toks[0], cond_id, "BID", bids0, title="YES", fee_schedule=fee_sched
                                    )
                                    leg1_bid = ParityLegDepth.from_raw(
                                        toks[1], cond_id, "BID", bids1, title="NO", fee_schedule=fee_sched
                                    )
                                if mint_possible:
                                    sweep_mint = self.ctf_allocator.compute_sweep_curve(
                                        market_id=f"{cond_id}_MINT",
                                        condition_id=cond_id,
                                        legs=[leg0_bid, leg1_bid],
                                        direction=ArbitrageDirection.MINT_SPLIT,
                                        execution_mode=ExecutionMode.ALL_TAKER,
                                        gas_price_gwei=measured_gwei,
                                        pol_usd_price=measured_pol,
                                    )
                                else:
                                    sweep_mint = self.ctf_allocator._empty_result(
                                        f"{cond_id}_MINT",
                                        cond_id,
                                        ArbitrageDirection.MINT_SPLIT,
                                        ExecutionMode.ALL_TAKER,
                                        self.ctf_allocator.compute_gas_cost_usd(
                                            ArbitrageDirection.MINT_SPLIT, measured_gwei, measured_pol
                                        ),
                                        "top-of-book bid sum <= $1.000 (analytic dominance bound)",
                                    )
                                if merge_possible:
                                    # 2. Merge parity: buy the full ask ladder under par, merge for $1.00.
                                    leg0_ask = ParityLegDepth.from_raw(
                                        toks[0], cond_id, "ASK", asks0, title="YES", fee_schedule=fee_sched
                                    )
                                    leg1_ask = ParityLegDepth.from_raw(
                                        toks[1], cond_id, "ASK", asks1, title="NO", fee_schedule=fee_sched
                                    )
                                    sweep_merge = self.ctf_allocator.compute_sweep_curve(
                                        market_id=f"{cond_id}_MERGE",
                                        condition_id=cond_id,
                                        legs=[leg0_ask, leg1_ask],
                                        direction=ArbitrageDirection.MERGE_UNWIND,
                                        execution_mode=ExecutionMode.ALL_TAKER,
                                        gas_price_gwei=measured_gwei,
                                        pol_usd_price=measured_pol,
                                    )
                                else:
                                    sweep_merge = self.ctf_allocator._empty_result(
                                        f"{cond_id}_MERGE",
                                        cond_id,
                                        ArbitrageDirection.MERGE_UNWIND,
                                        ExecutionMode.ALL_TAKER,
                                        self.ctf_allocator.compute_gas_cost_usd(
                                            ArbitrageDirection.MERGE_UNWIND, measured_gwei, measured_pol
                                        ),
                                        "top-of-book ask sum >= $1.000 (analytic dominance bound)",
                                    )
                                cached = (sig, sweep_mint, sweep_merge, leg0_bid, leg1_bid)
                            new_sweep_cache[cond_id] = cached

                            # C11: feed the ENTIRE universe sweep to the optimizer.
                            # The allocator, not a hand filter, decides viability.
                            parity_candidates.append(sweep_mint)
                            parity_candidates.append(sweep_merge)
                            candidate_meta[sweep_mint.market_id] = (m, q_title, tick, min_order_size)
                            candidate_meta[sweep_merge.market_id] = (m, q_title, tick, min_order_size)

                            # 3. Maker parity quoting candidate. Price-time priority at a
                            # new inside tick means any seller crossing to us fills first,
                            # so the arrival probability is the measured sell-side flow.
                            if not maker_possible or leg0_bid is None or leg1_bid is None:
                                continue
                            depth0 = sum(lv.size for lv in leg0_bid.levels)
                            depth1 = sum(lv.size for lv in leg1_bid.levels)
                            cap_shares = min(ref_p99, depth0, depth1)
                            if cap_shares < min_order_size:
                                continue
                            bids_sorted0 = leg0_bid.sorted_levels()
                            bids_sorted1 = leg1_bid.sorted_levels()
                            exit0_proceeds, exit0_fee, _, _ = _walk_bid_exit(
                                bids_sorted0, ref, fee_sched, tick
                            )
                            exit1_proceeds, exit1_fee, _, _ = _walk_bid_exit(
                                bids_sorted1, ref, fee_sched, tick
                            )
                            loss0 = q0 * ref - exit0_proceeds + exit0_fee
                            loss1 = q1 * ref - exit1_proceeds + exit1_fee
                            ev_gross = (
                                p_leg * p_leg * (1.0 - quote_cost) * ref
                                - p_leg * (1.0 - p_leg) * (loss0 + loss1)
                                + p_leg * rebate_ref
                            )
                            if ev_gross - gas_merge <= 0.0:
                                continue
                            ev_per_share = ev_gross / ref
                            synth = _make_maker_sweep(
                                f"{cond_id}_MAKER", cond_id, q0, q1, cap_shares, ev_per_share, gas_merge
                            )
                            parity_candidates.append(synth)
                            maker_meta[synth.market_id] = {
                                "q0": q0,
                                "q1": q1,
                                "p": p_leg,
                                "tick": tick,
                                "min_order_size": min_order_size,
                                "fs": fee_sched,
                                "bids0": bids_sorted0,
                                "bids1": bids_sorted1,
                                "gas": gas_merge,
                                "vol24": vol24,
                                "q_title": q_title,
                            }

                        sweep_cache = new_sweep_cache

                        # C11: measured compute telemetry for the full-universe sweep.
                        # Logged once a minute; the elapsed time is the observed
                        # compute bound, not a hand-set cap on discovery.
                        if now - last_scan_stats_log >= STATS_LOG_INTERVAL_S:
                            scan_done = time.time()
                            log.info(
                                f"CtfMinter full-universe scan: {len(cached_market_tokens)} markets | "
                                f"{len(parity_candidates)} candidates | {len(maker_meta)} maker quotes | "
                                f"{len(sweep_cache)} sweep cache entries | "
                                f"compute {scan_done - scan_started:.2f}s "
                                f"(books {books_done - scan_started:.2f}s, "
                                f"sweep+alloc {scan_done - books_done:.2f}s)"
                            )
                            last_scan_stats_log = now

                        # ==============================================================
                        # C5: one portfolio optimizer sizes every candidate from the
                        # whole usable bankroll. No $15/$60 or % per-market cap.
                        # ==============================================================
                        # C5: the whole available bankroll is optimizable. Gas and
                        # fees are already priced inside every candidate; no hand-set
                        # reserve percentage.
                        usable_cash = max(
                            0.0,
                            self.metrics["CtfMinter"].available_cash,
                        )
                        if parity_candidates and usable_cash > 0.0:
                            portfolio_res = self.ctf_allocator.allocate_portfolio(
                                candidates=parity_candidates,
                                total_budget=usable_cash,
                            )
                            strat = self.metrics["CtfMinter"]

                            for alloc in portfolio_res.allocations.values():
                                # C9: the same net-edge gate for maker and taker.
                                if alloc.expected_net_profit < self.ctf_allocator.min_net_edge_usd:
                                    continue

                                if alloc.market_id in maker_meta:
                                    mm = maker_meta[alloc.market_id]
                                    cap_shares = alloc.allocated_shares
                                    if cap_shares < mm["min_order_size"]:
                                        continue
                                    fill0 = random.random() < mm["p"]
                                    fill1 = random.random() < mm["p"]
                                    if not (fill0 or fill1):
                                        continue
                                    size0 = (
                                        local_fill_model.draw(mm["min_order_size"], cap_shares)
                                        if fill0 else 0.0
                                    )
                                    size1 = (
                                        local_fill_model.draw(mm["min_order_size"], cap_shares)
                                        if fill1 else 0.0
                                    )
                                    if size0 < mm["min_order_size"]:
                                        size0 = 0.0
                                    if size1 < mm["min_order_size"]:
                                        size1 = 0.0
                                    if size0 <= 0.0 and size1 <= 0.0:
                                        continue
                                    total_cost = size0 * mm["q0"] + size1 * mm["q1"]
                                    if total_cost <= 0.0 or strat.available_cash < total_cost:
                                        continue

                                    merged = min(size0, size1)
                                    residual0 = size0 - merged
                                    residual1 = size1 - merged
                                    # C4: unwind the residual by walking the real ladder.
                                    exit0_proceeds, exit0_fee, exit0_filled, exit0_vwap = _walk_bid_exit(
                                        mm["bids0"], residual0, mm["fs"], mm["tick"]
                                    )
                                    exit1_proceeds, exit1_fee, exit1_filled, exit1_vwap = _walk_bid_exit(
                                        mm["bids1"], residual1, mm["fs"], mm["tick"]
                                    )
                                    if residual0 - exit0_filled > 1e-6 or residual1 - exit1_filled > 1e-6:
                                        _warn_rate_limited(
                                            "ctf_exit_depth",
                                            "CtfMinter residual exceeded visible bid depth; "
                                            f"YES unfilled {max(0.0, residual0 - exit0_filled):.2f}, "
                                            f"NO unfilled {max(0.0, residual1 - exit1_filled):.2f}",
                                        )
                                    proceeds = merged * 1.0000 + exit0_proceeds + exit1_proceeds
                                    gross_pnl = proceeds - total_cost
                                    rebate = (
                                        (maker_fee_rebate(mm["q0"], size0, mm["fs"]) if size0 > 0 else 0.0)
                                        + (maker_fee_rebate(mm["q1"], size1, mm["fs"]) if size1 > 0 else 0.0)
                                    )
                                    residual_shares = residual0 + residual1
                                    exit_fee = exit0_fee + exit1_fee
                                    gas_cost = mm["gas"]
                                    net_profit = gross_pnl + rebate - gas_cost - exit_fee
                                    if net_profit < self.ctf_allocator.min_net_edge_usd:
                                        continue
                                    cond_id = alloc.condition_id
                                    side_tag = (
                                        "CTF_MAKER_MERGE"
                                        if merged > 0 and residual_shares <= 1e-9
                                        else "CTF_BROKEN_LEG"
                                    )

                                    strat.lock_capital(total_cost)
                                    strat.release_capital(
                                        cost_amount=total_cost,
                                        payout_amount=proceeds,
                                        fee=gas_cost + exit_fee,
                                    )
                                    strat.rebates_earned += rebate
                                    strat.available_cash += rebate
                                    strat.recalculate_equity()
                                    strat.total_trades += 1

                                    self.db.record_trade(
                                        "CtfMinter",
                                        cond_id,
                                        side_tag,
                                        price=round(mm["q0"] + mm["q1"], 4),
                                        size=round(size0 + size1, 2),
                                        gross_pnl=round(gross_pnl, 4),
                                        fee=round(gas_cost + exit_fee, 4),
                                        rebate=round(rebate, 4),
                                        latency_us=max(1.0, self.last_clob_rtt_ms * 1000.0),
                                        details=(
                                            f"Parity legs: YES {size0:.2f} @ ${mm['q0']:.4f}, "
                                            f"NO {size1:.2f} @ ${mm['q1']:.4f}; merged {merged:.2f}, "
                                            f"broken YES {residual0:.2f} (vwap ${exit0_vwap:.4f}) / "
                                            f"NO {residual1:.2f} (vwap ${exit1_vwap:.4f}) "
                                            f"| p_fill={mm['p']:.4f} | Net: {net_profit:+.4f} "
                                            f"| Question: {mm['q_title'][:30]}"
                                        ),
                                    )
                                    self._record_event(
                                        f"{C_MAGENTA}[CTF {'Maker' if side_tag == 'CTF_MAKER_MERGE' else 'Broken Leg'}]{C_RESET} "
                                        f"{merged:.0f} merged / {residual_shares:.0f} broken @ ${mm['q0'] + mm['q1']:.4f} "
                                        f"| Net: {net_profit:+.4f} ({mm['q_title'][:20]}...)"
                                    )
                                    continue

                                # Taker dislocation sweep (book liquidity is atomic).
                                meta = candidate_meta.get(alloc.market_id)
                                if meta is None:
                                    continue
                                m_data, q_text, alloc_tick, alloc_min_size = meta
                                if alloc.allocated_shares < alloc_min_size:
                                    continue
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
                                    latency_us=max(1.0, self.last_clob_rtt_ms * 1000.0),
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

                    except Exception:
                        # A DEBUG-level handler hid a missing `random` import for a
                        # whole commit. Surface execution bugs, rate-limited.
                        now_err = time.time()
                        if now_err - last_scan_error_log > 60.0:
                            log.exception("CTF parity scanner error")
                            last_scan_error_log = now_err

                time.sleep(0.5)
            except Exception as e:
                _warn_rate_limited("ctf_loop", f"Error in CtfMinter worker loop: {e}")
                time.sleep(2.0)

    def _ensure_stall_watchdog(self) -> None:
        """C7: start the process-wide watchdog once, seeded with both heartbeats."""
        if self._watchdog_started:
            return
        self._watchdog_started = True
        self.stall_watchdog.beat("ctf_scan")
        self.stall_watchdog.beat("telemetry")
        self.stall_watchdog.start(lambda: self.running, self._on_stall)

    def _on_stall(self, channel: str, elapsed: float, threshold: float) -> None:
        msg = (
            f"WATCHDOG: {channel} heartbeat stalled for {elapsed:.1f}s "
            f"(measured p95+4sigma threshold {threshold:.1f}s)"
        )
        log.warning(msg)
        self._record_event(
            f"{C_RED}[WATCHDOG]{C_RESET} {channel} stalled {elapsed:.0f}s "
            f"(threshold {threshold:.0f}s)"
        )

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
        total_net_pnl = sum(m.realized_pnl for m in self.metrics.values())

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
        rss_mb = process_rss_mb()
        avail_mb = host_available_mb()

        lines = [
            f"{C_BOLD}=============================================================================={C_RESET}",
            f"{C_BOLD}{C_CYAN}POLYMARKETBOT LIVE PAPER SOAK TEST{C_RESET} | "
            f"Uptime: {hours}h {minutes}m {seconds}s | "
            f"VIP Tier: {C_BOLD}{self.vip_tier.upper()}{C_RESET}",
            f"Network RTT: CLOB (London) {C_GREEN}{self.last_clob_rtt_ms:.1f}ms{C_RESET} | "
            f"Binance {C_GREEN}{self.last_binance_rtt_ms:.1f}ms{C_RESET} | "
            f"Disk: {disk_color}{disk.free_gb:.1f}GB Free ({disk.free_pct:.0f}%){C_RESET} | "
            f"Mem: RSS {rss_mb:.0f}MB / host avail {avail_mb:.0f}MB",
            f"{C_DIM}------------------------------------------------------------------------------{C_RESET}",
            f"Strategy Pillar          Starting     Current Equity   Net PnL      Max DD   Fills",
            f"{C_DIM}------------------------------------------------------------------------------{C_RESET}",
        ]
        for m in self.metrics.values():
            pnl_color = C_GREEN if m.realized_pnl >= 0 else C_RED
            extra_col = ""
            if m.model_only:
                extra_col = f" {C_YELLOW}[model accrual: +${m.net_pnl:.2f}; no orders]{C_RESET}"
            if m.name == "SettlementHarvester" and self.settlement_positions:
                pending_edge = sum(p.shares * 1.000 - p.cost_basis for p in self.settlement_positions)
                extra_col = f" {C_YELLOW}[{len(self.settlement_positions)} open: +${pending_edge:.2f} par edge]{C_RESET}"
            lines.append(
                f"{m.name:<24} ${m.starting_cash:<11.2f} ${m.current_equity:<15.4f} "
                f"{pnl_color}{m.realized_pnl:>+10.4f}{C_RESET} {m.max_drawdown_pct * 100:>6.2f}% {m.total_trades:>6}{extra_col}"
            )
        lines.append(f"{C_DIM}------------------------------------------------------------------------------{C_RESET}")
        lines.append(
            f"{C_BOLD}TOTAL PORTFOLIO{C_RESET}          ${total_bankroll:,.2f}   "
            f"${total_equity:<15.4f} {tot_color}{total_net_pnl:>+10.4f}{C_RESET} "
            f"Net Return: {tot_color}{(total_net_pnl / total_bankroll) * 100:+.4f}%{C_RESET}"
        )
        lines.append(f"{C_BOLD}=============================================================================={C_RESET}")
        if self.recent_events:
            lines.append(f"{C_DIM}Recent Activity & Fills:{C_RESET}")
            for ev in list(self.recent_events)[-4:]:
                lines.append(f"  {ev}")
            lines.append(f"{C_DIM}------------------------------------------------------------------------------{C_RESET}")
        return "\n".join(lines)

    def _run_telemetry_loop(self) -> None:
        """Logs the live performance scorecard every interval and snapshots it to SQLite."""
        is_tty = sys.stdout.isatty()
        if is_tty:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

        while self.running:
            now = time.time()
            uptime = now - self.start_time

            total_equity = sum(m.current_equity for m in self.metrics.values())
            total_net_pnl = sum(m.realized_pnl for m in self.metrics.values())
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
            self.stall_watchdog.beat("telemetry")

            # High-density scorecard: colours belong on the terminal only, never in
            # app.log or the per-file log.
            card = _ANSI_RE.sub("", self._render_dashboard(uptime))
            log.info("\n" + card)

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
    parser.add_argument("--fresh", action="store_true", help="Start fresh with clean $1,000 allocations without resuming previous PnL state")
    args = parser.parse_args()

    capital_each = args.capital / 3.0
    daemon = LivePaperSoakDaemon(
        capital_per_strategy=capital_each,
        vip_tier=args.vip_tier,
        duration=args.duration,
        interval=args.interval,
    )
    try:
        daemon.start(resume=not args.fresh)
    finally:
        daemon.stop()
        try:
            from loguru import logger as _loguru_logger
            _loguru_logger.complete()
        except Exception:
            pass
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
