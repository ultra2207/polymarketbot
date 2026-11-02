"""Production-grade Liquidity Rewards Backtesting CLI Suite.

Simulates multi-day (1,440-minute epochs per day) execution across real Polymarket reward pools.
Evaluates:
- Avellaneda-Stoikov inventory skewing (symmetric 1.0c touch at q=0; flattening skew at q!=0).
- Complete-set merging (YES + NO -> USDC).
- Competing order book depth and FIFO queue priority.
- Trade fills (including aggressive crossing orders).
- Exact quadratic scoring and daily payout with $1.00 minimum floor.
- Full PnL breakdown: Gross Rewards + Spread Captured - Adverse Selection - Inventory Decay.

Usage:
    .venv/Scripts/python.exe scripts/backtest_rewards_production.py --days 7 --size 200
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from polymarketbot.logs import get_logger
from polymarketbot.rewards import (
    CATEGORY_REGIMES,
    LiquidityRewardsBotEngine,
    MarketRewardParameters,
)

log = get_logger("backtest_rewards_production")

DEFAULT_INPUT = Path("data/processed/live_reward_head_pools.json")
OUTPUT_DIR = Path("data/processed")


def classify_category(question: str) -> str:
    """Infer market category for regime parameter selection."""
    q_lower = question.lower()
    if any(w in q_lower for w in ["senate", "house", "democrat", "republican", "election", "biden", "trump"]):
        return "politics"
    elif any(w in q_lower for w in ["israel", "iran", "ceasefire", "war", "netanyahu", "gaza", "lebanon"]):
        return "geopolitics"
    elif any(w in q_lower for w in ["btc", "eth", "crypto", "bitcoin", "solana", "token"]):
        return "crypto"
    elif any(w in q_lower for w in ["nba", "nfl", "premier league", "champions league", "cup", "game", "match"]):
        return "sports"
    return "default"


def load_reward_markets(input_file: Path) -> List[MarketRewardParameters]:
    """Load and parse real reward markets from JSON snapshot."""
    if not input_file.exists():
        log.warning(f"File {input_file} not found. Using representative production pools.")
        return [
            MarketRewardParameters(
                condition_id="0x7586a96520578acaaaa4ea84a2582f197f84255da1f3392a7aa300386c187b37",
                question="Will Benjamin Netanyahu be the next Prime Minister of Israel?",
                rate_per_day=3000.0,
                rewards_min_size=200.0,
                rewards_max_spread=3.5,
                initial_mid=0.355,
                category="geopolitics",
                competing_qualifying_depth=337962.0,
                competing_touch_depth=29310.0,
            ),
            MarketRewardParameters(
                condition_id="0x16c63b7cc37f012b9f59ee164ec03877914c701d06d48291ae8d6fc08a088b0d",
                question="2026 Balance of Power: D Senate, D House",
                rate_per_day=2500.0,
                rewards_min_size=200.0,
                rewards_max_spread=4.5,
                initial_mid=0.715,
                category="politics",
                competing_qualifying_depth=5961325.0,
                competing_touch_depth=157348.0,
            ),
        ]

    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    markets = []
    for item in data:
        q = item.get("question", "Unknown")
        cid = item.get("condition_id", "")
        rate = float(item.get("rate_per_day", 0.0))
        min_sz = float(item.get("rewards_min_size", 100.0))
        max_spr = float(item.get("rewards_max_spread", 3.0))

        yes_book = item.get("books", {}).get("Yes")
        if yes_book:
            mid = float(yes_book.get("mid", 0.50))
            qual_depth = float(yes_book.get("total_qualifying_shares", 10000.0))
            touch_depth = float(yes_book.get("touch_bid_shares", 2000.0))
        else:
            mid = 0.50
            qual_depth = 10000.0
            touch_depth = 2000.0

        cat = classify_category(q)
        markets.append(
            MarketRewardParameters(
                condition_id=cid,
                question=q,
                rate_per_day=rate,
                rewards_min_size=min_sz,
                rewards_max_spread=max_spr,
                initial_mid=mid,
                category=cat,
                competing_qualifying_depth=qual_depth,
                competing_touch_depth=touch_depth,
            )
        )

    log.info(f"Loaded {len(markets)} reward market configurations.")
    return markets


def run_production_backtest(
    markets: List[MarketRewardParameters],
    quote_size: float = 200.0,
    days: int = 7,
    risk_gamma: float = 0.01,
    max_inventory: float = 2000.0,
    output_file: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Execute multi-day simulation with incremental persistence."""
    results = []
    output_path = output_file or (OUTPUT_DIR / "production_rewards_backtest_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    log.info(
        f"Starting {days}-day production backtest across {len(markets)} markets "
        f"(size={quote_size}, gamma={risk_gamma}, max_inv={max_inventory})..."
    )

    for mkt_idx, mkt in enumerate(markets):
        log.info(f"[{mkt_idx + 1}/{len(markets)}] Simulating market: {mkt.question[:45]}...")
        engine = LiquidityRewardsBotEngine(
            market=mkt,
            quote_size=quote_size,
            risk_gamma=risk_gamma,
            max_inventory=max_inventory,
            seed=42 + mkt_idx,
        )

        daily_reports = []
        for day in range(1, days + 1):
            report = engine.simulate_epoch(epoch_idx=day, n_minutes=1440)
            daily_reports.append(report)

        # Aggregate multi-day statistics
        avg_gross_rew = sum(r.gross_rewards for r in daily_reports) / days
        avg_net_rew = sum(r.net_rewards for r in daily_reports) / days
        avg_spread_pnl = sum(r.spread_captured for r in daily_reports) / days
        avg_adverse_pnl = sum(r.adverse_selection for r in daily_reports) / days
        avg_decay = sum(r.inventory_decay for r in daily_reports) / days
        avg_net_pnl = sum(r.net_pnl for r in daily_reports) / days
        total_merged_usd = daily_reports[-1].complete_sets_merged_usd
        alloc_cap = daily_reports[0].allocated_capital

        daily_roi = (avg_net_pnl / alloc_cap) * 100.0 if alloc_cap > 0 else 0.0
        annual_roi = daily_roi * 365.0
        floor_pass_rate = sum(1 for r in daily_reports if r.passed_payout_floor) / days

        mkt_summary = {
            "question": mkt.question,
            "condition_id": mkt.condition_id,
            "category": mkt.category,
            "rate_per_day": mkt.rate_per_day,
            "allocated_capital": alloc_cap,
            "min_size": mkt.rewards_min_size,
            "max_spread_cents": mkt.rewards_max_spread,
            "avg_daily_gross_rewards": round(avg_gross_rew, 2),
            "avg_daily_net_rewards": round(avg_net_rew, 2),
            "avg_daily_spread_captured": round(avg_spread_pnl, 2),
            "avg_daily_adverse_selection": round(avg_adverse_pnl, 2),
            "avg_daily_inventory_decay": round(avg_decay, 2),
            "avg_daily_net_pnl": round(avg_net_pnl, 2),
            "daily_roi_pct": round(daily_roi, 2),
            "annual_roi_pct": round(annual_roi, 1),
            "total_complete_sets_merged_usd": round(total_merged_usd, 2),
            "floor_pass_rate": round(floor_pass_rate, 3),
            "days_simulated": days,
        }
        results.append(mkt_summary)

        # AGENTS.md rule: Incremental persistence after each market
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

    return results


def print_summary_table(results: List[Dict[str, Any]]) -> None:
    """Print readable PnL and ROI table."""
    header = (
        f"{'Market':<32} | {'Rate':<7} | {'Cap ($)':<8} | {'Rew ($)':<8} | "
        f"{'Spr ($)':<8} | {'Adv ($)':<8} | {'Dec ($)':<8} | {'Net ($/d)':<9} | {'Ann ROI':<9} | {'Merged'}"
    )
    divider = "-" * len(header)

    print("\n" + "=" * len(header))
    print("POLYMARKET LIQUIDITY REWARDS PRODUCTION BACKTEST SUMMARY")
    print(header)
    print(divider)

    for r in sorted(results, key=lambda x: x["avg_daily_net_pnl"], reverse=True):
        q = r["question"][:31]
        rate = f"${r['rate_per_day']:,.0f}"
        cap = f"${r['allocated_capital']:,.0f}"
        rew = f"${r['avg_daily_net_rewards']:,.2f}"
        spr = f"${r['avg_daily_spread_captured']:,.2f}"
        adv = f"-${r['avg_daily_adverse_selection']:,.2f}"
        dec = f"-${r['avg_daily_inventory_decay']:,.2f}"
        net = f"${r['avg_daily_net_pnl']:,.2f}"
        roi = f"{r['annual_roi_pct']:,.1f}%"
        mrg = f"${r['total_complete_sets_merged_usd']:,.0f}"
        print(
            f"{q:<32} | {rate:<7} | {cap:<8} | {rew:<8} | {spr:<8} | {adv:<8} | {dec:<8} | {net:<9} | {roi:<9} | {mrg}"
        )

    print("=" * len(header) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Production Liquidity Rewards Backtest Suite")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input pools JSON")
    parser.add_argument("--days", type=int, default=7, help="Simulation duration in days (1,440 mins/day)")
    parser.add_argument("--size", type=float, default=200.0, help="Base quote size in shares")
    parser.add_argument("--gamma", type=float, default=0.01, help="Avellaneda-Stoikov risk aversion gamma")
    parser.add_argument("--max-inv", type=float, default=2000.0, help="Max inventory limit in shares")
    parser.add_argument("--limit", type=int, default=10, help="Limit number of markets to test")
    parser.add_argument("--output", default=str(OUTPUT_DIR / "production_rewards_backtest_results.json"))

    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    markets = load_reward_markets(input_path)
    if args.limit and len(markets) > args.limit:
        markets = markets[: args.limit]

    results = run_production_backtest(
        markets=markets,
        quote_size=args.size,
        days=args.days,
        risk_gamma=args.gamma,
        max_inventory=args.max_inv,
        output_file=output_path,
    )

    print_summary_table(results)
    log.info(f"Backtest successfully completed. Results saved to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
