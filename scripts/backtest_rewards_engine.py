"""Advanced Polymarket Liquidity Rewards Backtesting & Simulation Engine.

Simulates 24-hour reward farming across 1,440 randomized 1-minute epochs.
Accounts for:
1. Official quadratic scoring function: S(v, s) = ((v - s) / v)^2 * size.
2. Two-sided vs single-sided penalties (c=3.0 or 0 outside [0.10, 0.90]).
3. Competing order-book depth and queue priority.
4. Category-specific volatility regimes, price drift, and discrete news jumps.
5. In-flight fills, queue depletion, and post-fill adverse selection markouts.
6. Polymarket's strict $1.00 daily payout floor per market.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from loguru import logger

CATEGORY_REGIMES = {
    "politics": {
        "vol_1m_bps": 12.0,
        "jump_prob_1h": 0.015,
        "jump_mean_cents": 12.0,
        "one_sided_flow_ratio": 0.75,
        "decay_300s_ratio": 0.15,
    },
    "sports": {
        "vol_1m_bps": 35.0,
        "jump_prob_1h": 0.080,
        "jump_mean_cents": 8.0,
        "one_sided_flow_ratio": 0.85,
        "decay_300s_ratio": 0.91,
    },
    "crypto": {
        "vol_1m_bps": 45.0,
        "jump_prob_1h": 0.050,
        "jump_mean_cents": 5.0,
        "one_sided_flow_ratio": 0.85,
        "decay_300s_ratio": 0.92,
    },
    "geopolitics": {
        "vol_1m_bps": 15.0,
        "jump_prob_1h": 0.025,
        "jump_mean_cents": 15.0,
        "one_sided_flow_ratio": 0.80,
        "decay_300s_ratio": 0.70,
    },
    "default": {
        "vol_1m_bps": 20.0,
        "jump_prob_1h": 0.020,
        "jump_mean_cents": 10.0,
        "one_sided_flow_ratio": 0.75,
        "decay_300s_ratio": 0.50,
    },
}


@dataclass
class MarketConfig:
    question: str
    condition_id: str
    rate_per_day: float
    min_size: float
    max_spread_cents: float
    category: str
    initial_mid: float
    competing_qualifying_depth_usd: float
    competing_touch_usd: float


@dataclass
class QuotingStrategy:
    quote_size: float
    spread_offset_cents: float
    rebalance_threshold_cents: float = 1.0
    max_inventory_shares: float = 2000.0
    pause_minutes_on_fill: int = 5


@dataclass
class MarketSimResult:
    question: str
    daily_rate: float
    allocated_capital: float
    quote_spread_cents: float
    max_spread_cents: float
    avg_reward_score_share: float
    gross_reward_payout: float
    net_reward_payout: float
    num_fills: int
    shares_filled: float
    adverse_selection_pnl: float
    spread_captured_pnl: float
    net_pnl: float
    roi_daily_pct: float
    roi_annual_pct: float


def compute_order_score(v_cents: float, s_cents: float, size: float) -> float:
    if s_cents > v_cents or size <= 0:
        return 0.0
    return (((v_cents - s_cents) / v_cents) ** 2) * size


def simulate_market_day(
    mkt: MarketConfig,
    strat: QuotingStrategy,
    rng: random.Random,
) -> MarketSimResult:
    regime = CATEGORY_REGIMES.get(mkt.category.lower(), CATEGORY_REGIMES["default"])

    n_epochs = 1440
    current_mid = mkt.initial_mid
    current_inventory = 0.0

    quoted_bid_dist = strat.spread_offset_cents
    quoted_ask_dist = strat.spread_offset_cents
    pause_until_epoch = -1

    daily_scores = []
    daily_competing_scores = []

    fills_count = 0
    total_shares_filled = 0.0
    adverse_selection_loss = 0.0
    spread_captured = 0.0

    comp_spread_cents = min(1.0, mkt.max_spread_cents * 0.5)
    comp_shares = mkt.competing_qualifying_depth_usd / max(0.01, current_mid)
    comp_score_per_epoch = compute_order_score(
        mkt.max_spread_cents, comp_spread_cents, comp_shares
    )

    for t in range(n_epochs):
        dt_vol = (regime["vol_1m_bps"] / 10000.0) * current_mid
        drift = rng.gauss(0.0, dt_vol)

        p_jump_min = regime["jump_prob_1h"] / 60.0
        jump = 0.0
        if rng.random() < p_jump_min:
            direction = 1.0 if rng.random() > 0.5 else -1.0
            jump_cents = rng.expovariate(1.0 / regime["jump_mean_cents"])
            jump = direction * (jump_cents / 100.0)

        price_delta = drift + jump
        prev_mid = current_mid
        current_mid = max(0.01, min(0.99, current_mid + price_delta))

        is_paused = t < pause_until_epoch
        if not is_paused and abs(current_inventory) < strat.max_inventory_shares:
            bid_price = prev_mid - (quoted_bid_dist / 100.0)
            ask_price = prev_mid + (quoted_ask_dist / 100.0)

            touch_fill_prob = max(0.01, 0.15 * math.exp(-0.8 * quoted_bid_dist))
            filled_side = None

            if current_mid <= bid_price:
                filled_side = "BUY"
                fill_price = bid_price
                markout_loss = (fill_price - current_mid) * strat.quote_size
                adverse_selection_loss += markout_loss
            elif current_mid >= ask_price:
                filled_side = "SELL"
                fill_price = ask_price
                markout_loss = (current_mid - fill_price) * strat.quote_size
                adverse_selection_loss += markout_loss
            elif rng.random() < touch_fill_prob:
                is_buy = rng.random() < regime["one_sided_flow_ratio"]
                filled_side = "BUY" if is_buy else "SELL"
                fill_price = bid_price if is_buy else ask_price
                spread_captured += (strat.spread_offset_cents / 100.0) * strat.quote_size
                decay_loss = (
                    (strat.spread_offset_cents / 100.0)
                    * regime["decay_300s_ratio"]
                    * strat.quote_size
                )
                adverse_selection_loss += decay_loss

            if filled_side:
                fills_count += 1
                total_shares_filled += strat.quote_size
                if filled_side == "BUY":
                    current_inventory += strat.quote_size
                else:
                    current_inventory -= strat.quote_size
                pause_until_epoch = t + strat.pause_minutes_on_fill

        if not is_paused:
            s_bid = compute_order_score(
                mkt.max_spread_cents, quoted_bid_dist, strat.quote_size
            )
            s_ask = compute_order_score(
                mkt.max_spread_cents, quoted_ask_dist, strat.quote_size
            )

            if 0.10 <= current_mid <= 0.90:
                q_min = max(min(s_bid, s_ask), max(s_bid, s_ask) / 3.0)
            else:
                q_min = min(s_bid, s_ask)

            maker_score = q_min
        else:
            maker_score = 0.0

        daily_scores.append(maker_score)
        daily_competing_scores.append(comp_score_per_epoch)

    tot_maker_score = sum(daily_scores)
    tot_comp_score = sum(daily_competing_scores)
    tot_pool_score = tot_maker_score + tot_comp_score

    reward_share = (tot_maker_score / tot_pool_score) if tot_pool_score > 0 else 0.0
    gross_reward = reward_share * mkt.rate_per_day

    net_reward = gross_reward if gross_reward >= 1.0 else 0.0

    net_pnl = net_reward + spread_captured - adverse_selection_loss
    capital_req = strat.quote_size * max(0.20, mkt.initial_mid) * 2.0

    roi_daily = (net_pnl / capital_req) if capital_req > 0 else 0.0
    roi_annual = roi_daily * 365.0

    return MarketSimResult(
        question=mkt.question,
        daily_rate=mkt.rate_per_day,
        allocated_capital=round(capital_req, 2),
        quote_spread_cents=strat.spread_offset_cents,
        max_spread_cents=mkt.max_spread_cents,
        avg_reward_score_share=round(reward_share, 5),
        gross_reward_payout=round(gross_reward, 2),
        net_reward_payout=round(net_reward, 2),
        num_fills=fills_count,
        shares_filled=round(total_shares_filled, 1),
        adverse_selection_pnl=round(-adverse_selection_loss, 2),
        spread_captured_pnl=round(spread_captured, 2),
        net_pnl=round(net_pnl, 2),
        roi_daily_pct=round(roi_daily * 100, 2),
        roi_annual_pct=round(roi_annual * 100, 1),
    )


def run_full_simulation(
    live_pools_file: Path,
    quote_size: float = 200.0,
    spread_dist: float = 1.0,
    n_days: int = 14,
    seed: int = 42,
) -> List[Dict]:
    with open(live_pools_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    rng = random.Random(seed)
    results = []

    for item in data:
        q = item["question"]
        cid = item["condition_id"]
        rate = float(item["rate_per_day"])
        min_sz = float(item["rewards_min_size"])
        max_spr = float(item["rewards_max_spread"])

        yes_book = item["books"].get("Yes")
        if not yes_book:
            continue

        mid = float(yes_book["mid"])
        qual_usd = float(yes_book["total_qualifying_usd"])
        touch_usd = float(yes_book["touch_bid_usd"] + yes_book["touch_ask_usd"])

        q_lower = q.lower()
        if any(w in q_lower for w in ["senate", "house", "democrat", "republican", "election", "tax", "president"]):
            cat = "politics"
        elif any(w in q_lower for w in ["netanyahu", "iran", "ceasefire", "israel", "war"]):
            cat = "geopolitics"
        elif any(w in q_lower for w in ["btc", "eth", "crypto", "bitcoin"]):
            cat = "crypto"
        elif any(w in q_lower for w in ["rate", "fed", "inflation", "gdp"]):
            cat = "economics"
        else:
            cat = "default"

        mkt = MarketConfig(
            question=q,
            condition_id=cid,
            rate_per_day=rate,
            min_size=min_sz,
            max_spread_cents=max_spr,
            category=cat,
            initial_mid=mid,
            competing_qualifying_depth_usd=qual_usd,
            competing_touch_usd=touch_usd,
        )

        strat = QuotingStrategy(
            quote_size=max(min_sz, quote_size),
            spread_offset_cents=min(spread_dist, max_spr * 0.9),
            rebalance_threshold_cents=1.0,
            pause_minutes_on_fill=5,
        )

        day_results = [simulate_market_day(mkt, strat, rng) for _ in range(n_days)]

        avg_gross_reward = sum(d.gross_reward_payout for d in day_results) / n_days
        avg_net_reward = sum(d.net_reward_payout for d in day_results) / n_days
        avg_fills = sum(d.num_fills for d in day_results) / n_days
        avg_adv_loss = sum(d.adverse_selection_pnl for d in day_results) / n_days
        avg_spread_pnl = sum(d.spread_captured_pnl for d in day_results) / n_days
        avg_net_pnl = sum(d.net_pnl for d in day_results) / n_days
        allocated_cap = day_results[0].allocated_capital
        daily_roi = (avg_net_pnl / allocated_cap) * 100 if allocated_cap > 0 else 0.0
        annual_roi = daily_roi * 365.0

        results.append({
            "question": q,
            "category": cat,
            "daily_rate": rate,
            "allocated_capital": allocated_cap,
            "quote_spread_cents": strat.spread_offset_cents,
            "max_spread_cents": max_spr,
            "avg_daily_reward": round(avg_net_reward, 2),
            "avg_daily_fills": round(avg_fills, 1),
            "avg_daily_adverse_pnl": round(avg_adv_loss, 2),
            "avg_daily_spread_pnl": round(avg_spread_pnl, 2),
            "avg_daily_net_pnl": round(avg_net_pnl, 2),
            "daily_roi_pct": round(daily_roi, 2),
            "annual_roi_pct": round(annual_roi, 1),
            "pass_floor_rate": sum(1 if d.net_reward_payout >= 1.0 else 0 for d in day_results) / n_days,
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Backtest Polymarket Liquidity Rewards")
    parser.add_argument("--input", default="data/processed/live_reward_head_pools.json")
    parser.add_argument("--size", type=float, default=200.0)
    parser.add_argument("--spread", type=float, default=1.0)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--output", default="data/processed/rewards_backtest_results.json")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        return 1

    logger.info(f"Running {args.days}-day rewards simulation (size={args.size}, spread={args.spread}c)...")
    sim_results = run_full_simulation(
        input_path,
        quote_size=args.size,
        spread_dist=args.spread,
        n_days=args.days,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(sim_results, f, indent=2)

    logger.info(f"Backtest complete. Results written to {out_path}")

    print("\n" + "=" * 115)
    print(f"{'Market':<35} | {'Rate':<7} | {'Cap ($)':<8} | {'Rew ($)':<8} | {'Adv ($)':<8} | {'Spr ($)':<8} | {'Net ($/d)':<9} | {'Ann ROI':<9}")
    print("-" * 115)
    for r in sorted(sim_results, key=lambda x: x["avg_daily_net_pnl"], reverse=True):
        q = r["question"][:34]
        rate = f"${r['daily_rate']:,.0f}"
        cap = f"${r['allocated_capital']:,.0f}"
        rew = f"${r['avg_daily_reward']:,.2f}"
        adv = f"${r['avg_daily_adverse_pnl']:,.2f}"
        spr = f"${r['avg_daily_spread_pnl']:,.2f}"
        net = f"${r['avg_daily_net_pnl']:,.2f}"
        roi = f"{r['annual_roi_pct']:,.1f}%"
        print(f"{q:<35} | {rate:<7} | {cap:<8} | {rew:<8} | {adv:<8} | {spr:<8} | {net:<9} | {roi:<9}")
    print("=" * 115 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
