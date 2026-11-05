#!/usr/bin/env python3
"""Empirical Live Queue Monitoring & Real-World Quadratic Reward Calculation Engine.

Connects to Polymarket CLOB WebSocket and REST API to monitor top liquid incentivized markets:
1. Ingests live order book and trade events.
2. Tracks touch queue depths and exact trade depletion velocity.
3. Records empirical time to deplete 5k, 10k, 17k, and 30k share checkpoints.
4. Computes the real-world competitor quadratic score denominator D_total and $10k capital payout.
5. Persists durable incremental telemetry to data/processed/live_liquid_queues_30min.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from polymarketbot.logs import IST, get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.rewards.scoring import quadratic_order_score
from polymarketbot.simulator.feed import LivePolymarketFeed, MarketTrade, OrderBookSnapshot

log = get_logger("monitor_liquid_queues")

OUTPUT_PATH = Path("data/processed/live_liquid_queues_30min.json")
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class MarketTracked:
    condition_id: str
    question: str
    rate_per_day: float
    rewards_max_spread: float
    rewards_min_size: float
    token_yes: str
    token_no: str
    label_yes: str = "YES"
    label_no: str = "NO"


@dataclass
class TouchCheckpointTracker:
    target_shares: float
    shares_chewed: float = 0.0
    start_time: float = field(default_factory=time.time)
    completed_seconds: Optional[float] = None


class LiveQueueMonitor:
    def __init__(
        self,
        markets: List[MarketTracked],
        duration_seconds: float = 1800.0,
        output_file: Path = OUTPUT_PATH,
        checkpoint_levels: Tuple[float, ...] = (5000.0, 10000.0, 17000.0, 30000.0),
        resume: bool = False,
    ) -> None:
        self.markets = {m.condition_id: m for m in markets}
        self.duration_seconds = duration_seconds
        self.output_file = output_file
        self.checkpoint_levels = checkpoint_levels
        self.resume = resume
        self.prior_elapsed_seconds = 0.0
        
        # Token to market mapping
        self.token_to_cid: Dict[str, str] = {}
        self.token_to_side: Dict[str, str] = {}
        for m in markets:
            self.token_to_cid[m.token_yes] = m.condition_id
            self.token_to_side[m.token_yes] = m.label_yes
            self.token_to_cid[m.token_no] = m.condition_id
            self.token_to_side[m.token_no] = m.label_no

        self.feed = LivePolymarketFeed(fallback_poll_interval=3.0)
        
        # Real-time state
        self.start_time = time.time()
        self.is_running = False
        
        # Telemetry storage
        self.touch_snapshots: Dict[str, List[Dict[str, Any]]] = {m.condition_id: [] for m in markets}
        self.trades_history: Dict[str, List[Dict[str, Any]]] = {m.condition_id: [] for m in markets}
        self.last_trade_time: Dict[str, float] = {m.condition_id: 0.0 for m in markets}
        
        # Checkpoint trackers per market
        self.checkpoints: Dict[str, Dict[float, List[float]]] = {
            m.condition_id: {lvl: [] for lvl in checkpoint_levels} for m in markets
        }
        self.active_checkpoint_trackers: Dict[str, Dict[float, TouchCheckpointTracker]] = {
            m.condition_id: {lvl: TouchCheckpointTracker(target_shares=lvl) for lvl in checkpoint_levels}
            for m in markets
        }

        # Latest order book states
        self.latest_books: Dict[str, Dict[str, Any]] = {}
        
        # Denominator & Rewards calculation state
        self.latest_competitor_denominator: Dict[str, float] = {}
        self.latest_rewards_calculation: Dict[str, Dict[str, Any]] = {}

        if self.resume and self.output_file.exists():
            self._load_previous_state()

    def _load_previous_state(self) -> None:
        try:
            with open(self.output_file, "r", encoding="utf-8") as f:
                prev = json.load(f)
            self.prior_elapsed_seconds = float(prev.get("elapsed_seconds", 0.0))
            markets_data = prev.get("markets", {})
            for cid, m_data in markets_data.items():
                if cid in self.trades_history:
                    # Restore checkpoint stats
                    cp_data = m_data.get("queue_depletion_checkpoints", {})
                    for lvl in self.checkpoint_levels:
                        key = f"{int(lvl)}_shares"
                        if key in cp_data:
                            self.checkpoints[cid][lvl] = cp_data[key].get("completed_runs", [])
                            cur_prog = cp_data[key].get("current_progress_shares", 0.0)
                            self.active_checkpoint_trackers[cid][lvl].shares_chewed = cur_prog
            log.info(f"Resumed prior telemetry: {self.prior_elapsed_seconds:.1f}s ({self.prior_elapsed_seconds/60:.1f}m) already elapsed.")
        except Exception as e:
            log.warning(f"Could not load previous state: {e}")

    def start(self) -> None:
        """Register callbacks, subscribe to tokens, and begin feed."""
        self.is_running = True
        self.start_time = time.time()
        install_doh_fallback()
        
        all_tokens = list(self.token_to_cid.keys())
        log.info(f"Subscribing to {len(all_tokens)} tokens across {len(self.markets)} markets...")
        self.feed.subscribe(all_tokens)
        self.feed.add_book_listener(self._on_book_update)
        self.feed.add_trade_listener(self._on_trade_update)

    def _on_book_update(self, snapshot: OrderBookSnapshot) -> None:
        cid = self.token_to_cid.get(snapshot.asset_id)
        if not cid:
            return

        side = self.token_to_side.get(snapshot.asset_id, "TOKEN")
        self.latest_books[f"{cid}_{side}"] = snapshot.to_dict()

        # Record touch stats
        best_bid = snapshot.best_bid
        best_ask = snapshot.best_ask
        spread_cents = snapshot.spread_cents
        
        touch_bid_shares = sum(b.size for b in snapshot.bids if abs(b.price - best_bid) < 1e-4)
        touch_ask_shares = sum(a.size for a in snapshot.asks if abs(a.price - best_ask) < 1e-4)
        
        entry = {
            "timestamp": snapshot.timestamp,
            "side": side,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": snapshot.mid,
            "spread_cents": spread_cents,
            "touch_bid_shares": touch_bid_shares,
            "touch_ask_shares": touch_ask_shares,
            "touch_bid_usd": round(touch_bid_shares * best_bid, 2),
            "touch_ask_usd": round(touch_ask_shares * best_ask, 2),
        }
        
        # Keep a rolling buffer of 100 snapshots per market
        history = self.touch_snapshots[cid]
        history.append(entry)
        if len(history) > 200:
            history.pop(0)

    def _on_trade_update(self, trade: MarketTrade) -> None:
        cid = self.token_to_cid.get(trade.asset_id)
        if not cid:
            return

        now = trade.timestamp or time.time()
        last_t = self.last_trade_time[cid]
        delta_t = now - last_t if last_t > 0 else 0.0
        self.last_trade_time[cid] = now
        
        side = self.token_to_side.get(trade.asset_id, "TOKEN")
        is_sweep = trade.size >= 1000.0  # Whale sweep threshold

        trade_entry = {
            "timestamp": now,
            "side": side,
            "trade_side": trade.side,
            "price": trade.price,
            "size": trade.size,
            "usd_value": round(trade.price * trade.size, 2),
            "delta_t": round(delta_t, 3),
            "is_sweep": is_sweep,
        }
        self.trades_history[cid].append(trade_entry)

        # Update checkpoint trackers
        trackers = self.active_checkpoint_trackers[cid]
        for lvl, tracker in trackers.items():
            tracker.shares_chewed += trade.size
            if tracker.shares_chewed >= lvl:
                duration = now - tracker.start_time
                self.checkpoints[cid][lvl].append(round(duration, 2))
                log.info(
                    f"[CHECKPOINT REACHED] {self.markets[cid].question[:30]}... "
                    f"Chewed {lvl:,.0f} shares in {duration:.2f}s ({duration/60:.2f} min)"
                )
                # Reset tracker for next wave
                trackers[lvl] = TouchCheckpointTracker(target_shares=lvl, start_time=now)

    def calculate_real_world_rewards(self) -> None:
        """Fetch full order books via REST to calculate the exact quadratic competitor score denominator."""
        session = requests.Session()
        session.headers.update({"User-Agent": "polymarketbot/rewards-live-monitor"})

        for cid, m in self.markets.items():
            v = m.rewards_max_spread
            min_size = m.rewards_min_size
            rate_per_day = m.rate_per_day
            
            # Fetch full order books for YES and NO
            books = {}
            for tok_id, side in [(m.token_yes, m.label_yes), (m.token_no, m.label_no)]:
                try:
                    r = session.get(f"https://clob.polymarket.com/book?token_id={tok_id}", timeout=5.0)
                    if r.status_code == 200:
                        books[side] = r.json()
                except Exception as e:
                    log.warning(f"Failed to fetch book for {tok_id[:12]}: {e}")

            if not books or m.label_yes not in books:
                continue

            yes_bids = books[m.label_yes].get("bids", [])
            yes_asks = books[m.label_yes].get("asks", [])
            if not yes_bids or not yes_asks:
                continue

            best_bid = max(float(x["price"]) for x in yes_bids)
            best_ask = min(float(x["price"]) for x in yes_asks)
            mid = (best_bid + best_ask) / 2.0

            # Calculate D_total: sum of quadratic scores of all resting competitor orders within max spread
            q_bids_total = 0.0
            for b in yes_bids:
                p = float(b["price"])
                sz = float(b["size"])
                spread_cents = (mid - p) * 100.0
                if 0 <= spread_cents <= v and sz >= min_size:
                    q_bids_total += quadratic_order_score(v, spread_cents, sz)

            q_asks_total = 0.0
            for a in yes_asks:
                p = float(a["price"])
                sz = float(a["size"])
                spread_cents = (p - mid) * 100.0
                if 0 <= spread_cents <= v and sz >= min_size:
                    q_asks_total += quadratic_order_score(v, spread_cents, sz)

            # Polymarket two-sided minimum rule
            d_total = min(q_bids_total, q_asks_total)
            if d_total <= 0:
                d_total = max(q_bids_total, q_asks_total) / 3.0  # single-sided penalty

            self.latest_competitor_denominator[cid] = d_total

            # Now calculate our $10,000 USD position score:
            # 5,000 shares quoted at touch (s = 0.5c)
            our_quote_size = 5000.0
            s_touch = 0.5
            q_min_us = quadratic_order_score(v, s_touch, our_quote_size)

            # Daily payout formula
            share = q_min_us / (d_total + q_min_us) if (d_total + q_min_us) > 0 else 0.0
            daily_payout_usd = rate_per_day * share
            clears_floor = daily_payout_usd >= 1.00
            net_daily_payout = daily_payout_usd if clears_floor else 0.0

            self.latest_rewards_calculation[cid] = {
                "question": m.question,
                "rate_per_day": rate_per_day,
                "rewards_max_spread_cents": v,
                "rewards_min_size_shares": min_size,
                "mid_price": round(mid, 4),
                "competitor_denominator_d_total": round(d_total, 2),
                "our_quote_size_shares": our_quote_size,
                "our_spread_from_mid_cents": s_touch,
                "our_quadratic_score_q_min": round(q_min_us, 2),
                "our_payout_share_pct": round(share * 100, 4),
                "projected_daily_gross_usd": round(daily_payout_usd, 2),
                "clears_1_dollar_floor": clears_floor,
                "projected_daily_net_usd": round(net_daily_payout, 2),
                "projected_monthly_net_usd": round(net_daily_payout * 30.416, 2),
            }

    def get_empirical_metrics(self) -> Dict[str, Any]:
        """Aggregate cumulative empirical live metrics across all tracked markets."""
        elapsed = self.prior_elapsed_seconds + (time.time() - self.start_time)
        metrics = {
            "elapsed_seconds": round(elapsed, 1),
            "elapsed_minutes": round(elapsed / 60.0, 2),
            "timestamp": datetime.now(IST).isoformat(),
            "markets": {},
        }

        for cid, m in self.markets.items():
            trades = self.trades_history[cid]
            total_trades = len(trades)
            total_volume = sum(t["size"] for t in trades)
            total_usd = sum(t["usd_value"] for t in trades)
            
            # Trade size distribution
            retail_trades = [t for t in trades if not t["is_sweep"]]
            sweeps = [t for t in trades if t["is_sweep"]]
            
            retail_vol = sum(t["size"] for t in retail_trades)
            sweep_vol = sum(t["size"] for t in sweeps)
            
            # Depletion velocity
            depletion_velocity = total_volume / elapsed if elapsed > 0 else 0.0
            
            # Arrival delta t
            deltas = [t["delta_t"] for t in trades if t["delta_t"] > 0]
            avg_delta_t = sum(deltas) / len(deltas) if deltas else 0.0

            # Latest touch depth
            recent_touches = self.touch_snapshots[cid]
            latest_touch = recent_touches[-1] if recent_touches else {}

            # Checkpoint durations
            checkpoint_stats = {}
            for lvl in self.checkpoint_levels:
                completed = self.checkpoints[cid][lvl]
                current_active = self.active_checkpoint_trackers[cid][lvl]
                checkpoint_stats[f"{int(lvl)}_shares"] = {
                    "completed_runs": completed,
                    "avg_seconds": round(sum(completed) / len(completed), 2) if completed else None,
                    "current_progress_shares": round(current_active.shares_chewed, 1),
                    "current_progress_pct": round(current_active.shares_chewed / lvl * 100, 1),
                }

            metrics["markets"][cid] = {
                "question": m.question,
                "rate_per_day": m.rate_per_day,
                "total_trades": total_trades,
                "total_shares_traded": round(total_volume, 1),
                "total_usd_traded": round(total_usd, 2),
                "depletion_velocity_shares_per_sec": round(depletion_velocity, 2),
                "avg_trade_interval_sec": round(avg_delta_t, 2),
                "retail_trades_count": len(retail_trades),
                "retail_shares_volume": round(retail_vol, 1),
                "sweeps_count": len(sweeps),
                "sweep_shares_volume": round(sweep_vol, 1),
                "latest_touch": latest_touch,
                "queue_depletion_checkpoints": checkpoint_stats,
                "quadratic_rewards_calculation": self.latest_rewards_calculation.get(cid, {}),
            }

        return metrics

    def save_to_file(self) -> None:
        """Persist current metrics to JSON file."""
        data = self.get_empirical_metrics()
        temp_file = self.output_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_file.replace(self.output_file)
        log.info(f"Durable state updated -> {self.output_file}")


async def run_monitor_loop(args: argparse.Namespace) -> None:
    # 1. Selected top liquid incentivized markets:
    # Newcastle vs Everton O/U 2.5 ($8,850/day pool, heavy game action)
    # 2026 Balance of Power ($2,500/day pool, $4.25M depth)
    # Spread Newcastle (-1.5) ($8,850/day pool)
    tracked_markets = [
        MarketTracked(
            condition_id="0x87d90f7efc2f42ea04ea088725bebf71a530f2c00234a41fae0172e2cfc2323c",
            question="Newcastle United FC vs. Everton FC: O/U 2.5",
            rate_per_day=8850.0,
            rewards_max_spread=2.5,
            rewards_min_size=1000.0,
            token_yes="42991345588751281703161879118909220821006344747834751918716699755451552699030",
            token_no="3035757407688259493867268412581757640639217649143462150042214926221807116407",
            label_yes="Over",
            label_no="Under",
        ),
        MarketTracked(
            condition_id="0x16c63b7cc37f012b9f59ee164ec03877914c701d06d48291ae8d6fc08a088b0d",
            question="2026 Balance of Power: D Senate, D House",
            rate_per_day=2500.0,
            rewards_max_spread=4.5,
            rewards_min_size=200.0,
            token_yes="34722410608062854697106861099776685947172185964394483545370684749662285977831",
            token_no="24483563228660028121020430722896740519524131851526579619586176931221901842798",
            label_yes="YES",
            label_no="NO",
        ),
        MarketTracked(
            condition_id="0xedd8e82ee3e3fafe14299d63c500e572070e1765c3bb9a9e701967d30f78a2e7",
            question="Spread: Newcastle United FC (-1.5)",
            rate_per_day=8850.0,
            rewards_max_spread=2.5,
            rewards_min_size=1000.0,
            token_yes="114558685668757993385739263496915160916527071310428854959201989318034420974230",
            token_no="32236653147784987989844151335255424169306348261843888275213277073020722794589",
            label_yes="Newcastle",
            label_no="Everton",
        ),
    ]

    monitor = LiveQueueMonitor(
        markets=tracked_markets,
        duration_seconds=args.duration,
        output_file=Path(args.output),
        resume=args.resume,
    )
    monitor.start()

    # Initial rewards calculation
    log.info("Calculating initial real-world competitor quadratic denominator...")
    monitor.calculate_real_world_rewards()
    monitor.save_to_file()

    # Start feed background task
    feed_task = asyncio.create_task(monitor.feed.start())

    log.info(f"Beginning live queue monitoring for {args.duration:.0f} seconds ({args.duration/60:.1f} minutes)...")
    t0 = time.time()
    last_rewards_calc = time.time()
    last_save = time.time()

    try:
        while (time.time() - t0) < args.duration:
            await asyncio.sleep(1.0)
            now = time.time()

            # Recalculate rewards denominator every 30 seconds
            if now - last_rewards_calc >= 30.0:
                monitor.calculate_real_world_rewards()
                last_rewards_calc = now

            # Save state every 15 seconds
            if now - last_save >= 15.0:
                monitor.save_to_file()
                last_save = now
                elapsed_min = (now - t0) / 60.0
                log.info(f"[TELEMETRY HEARTBEAT] T={elapsed_min:.1f} min elapsed of {args.duration/60:.1f} min")

    except asyncio.CancelledError:
        log.info("Monitor loop cancelled.")
    finally:
        log.info("Shutting down monitor feed...")
        await monitor.feed.stop()
        monitor.calculate_real_world_rewards()
        monitor.save_to_file()
        log.info("Final empirical data persisted successfully.")


def main():
    parser = argparse.ArgumentParser(description="Live Queue Depletion & Quadratic Rewards Monitor")
    parser.add_argument("--duration", type=float, default=60.0, help="Run duration in seconds")
    parser.add_argument("--output", type=str, default="data/processed/live_liquid_queues_30min.json")
    parser.add_argument("--resume", action="store_true", help="Resume and accumulate prior telemetry from existing output file")
    args = parser.parse_args()

    asyncio.run(run_monitor_loop(args))


if __name__ == "__main__":
    main()
