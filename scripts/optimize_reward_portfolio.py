"""Dynamic Portfolio Allocation & Yield Surface Optimizer for Polymarket Liquidity Rewards.

Solves the optimal capital distribution across reward pools under:
1. Exact binary collateral invariant: 1 YES + 1 NO = $1.00 USDC collateral (q shares = $q capital).
2. Official quadratic scoring: S(v, s) = ((v - s) / v)^2 * q.
3. Market-specific competing score denominator: D_k.
4. Polymarket's strict $1.00 daily payout floor per market.
5. Jump risk & adverse selection penalties as a function of quote spread s.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from loguru import logger
from polymarketbot.logs import get_logger
log = get_logger(__name__)


@dataclass
class PoolCandidate:
    question: str
    condition_id: str
    daily_rate: float
    min_size: float
    max_spread_cents: float
    competing_depth_usd: float
    touch_depth_usd: float
    category: str
    jump_risk_factor: float  # Expected daily adverse selection per share at 1c


CATEGORY_RISK = {
    "politics": 0.025,      # $0.025 daily jump risk / share
    "geopolitics": 0.045,   # $0.045 daily jump risk / share
    "sports": 0.060,        # $0.060 daily jump risk / share
    "crypto": 0.070,        # $0.070 daily jump risk / share
    "default": 0.030,
}


def load_candidate_pools(snapshot_path: Path) -> List[PoolCandidate]:
    with open(snapshot_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    candidates = []
    for item in data:
        q = item["question"]
        cid = item["condition_id"]
        rate = float(item["rate_per_day"])
        min_sz = float(item["rewards_min_size"])
        max_spr = float(item["rewards_max_spread"])

        yes_book = item["books"].get("Yes")
        if not yes_book:
            continue

        qual_usd = float(yes_book["total_qualifying_usd"])
        touch_usd = float(yes_book["touch_bid_usd"] + yes_book["touch_ask_usd"])

        q_lower = q.lower()
        if any(w in q_lower for w in ["senate", "house", "democrat", "republican", "tax", "election", "president"]):
            cat = "politics"
        elif any(w in q_lower for w in ["netanyahu", "iran", "ceasefire", "israel", "war"]):
            cat = "geopolitics"
        elif any(w in q_lower for w in ["btc", "eth", "crypto", "bitcoin"]):
            cat = "crypto"
        else:
            cat = "default"

        risk = CATEGORY_RISK.get(cat, CATEGORY_RISK["default"])

        candidates.append(
            PoolCandidate(
                question=q,
                condition_id=cid,
                daily_rate=rate,
                min_size=min_sz,
                max_spread_cents=max_spr,
                competing_depth_usd=qual_usd,
                touch_depth_usd=touch_usd,
                category=cat,
                jump_risk_factor=risk,
            )
        )
    return candidates


def evaluate_market_quote(
    cand: PoolCandidate,
    q_shares: float,
    s_cents: float,
) -> Tuple[float, float, float]:
    """Computes (Gross Reward, Risk Penalty, Net Reward) for q shares at spread s."""
    if q_shares < cand.min_size or s_cents > cand.max_spread_cents:
        return 0.0, 0.0, 0.0

    # Quadratic weight
    weight = ((cand.max_spread_cents - s_cents) / cand.max_spread_cents) ** 2
    my_score = weight * q_shares

    # Competitor score estimate (assume competitors average ~1.0c spread)
    comp_s = min(1.0, cand.max_spread_cents * 0.5)
    comp_weight = ((cand.max_spread_cents - comp_s) / cand.max_spread_cents) ** 2
    comp_score = comp_weight * cand.competing_depth_usd

    total_score = my_score + comp_score
    share = (my_score / total_score) if total_score > 0 else 0.0
    gross_reward = share * cand.daily_rate

    # Enforce $1.00 payout floor
    if gross_reward < 1.0:
        gross_reward = 0.0

    # Risk penalty decays exponentially as we step back from touch
    # At s = 1c, full risk; at s = 2c, risk drops by ~50%; at s = 3c, risk drops by ~75%
    spread_risk_multiplier = max(0.1, 1.0 / (s_cents ** 1.2))
    risk_cost = q_shares * cand.jump_risk_factor * spread_risk_multiplier

    net_reward = gross_reward - risk_cost
    return gross_reward, risk_cost, net_reward


def optimize_portfolio(
    candidates: List[PoolCandidate],
    total_bankroll_usd: float = 2000.0,
    max_markets: int = 5,
    min_allocation_usd: float = 200.0,
) -> Dict:
    """Finds the optimal set of markets and (size, spread) allocations."""
    # Discretized search grid for each candidate
    spread_options = [0.8, 1.0, 1.5, 2.0, 2.5]
    size_options = [200.0, 300.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0]

    best_score_per_market = []
    for cand in candidates:
        best_net = -9999.0
        best_cfg = None

        for s in spread_options:
            if s >= cand.max_spread_cents:
                continue
            for q in size_options:
                if q < cand.min_size or q > total_bankroll_usd:
                    continue

                gross, risk, net = evaluate_market_quote(cand, q, s)
                if gross >= 1.0 and net > best_net:
                    best_net = net
                    best_cfg = {
                        "shares": q,
                        "capital_usd": q,  # 1 share = $1 USDC collateral
                        "spread_cents": s,
                        "gross_reward_usd": round(gross, 2),
                        "risk_penalty_usd": round(risk, 2),
                        "net_pnl_usd": round(net, 2),
                        "roi_daily_pct": round((net / q) * 100, 2),
                        "annual_roi_pct": round((net / q) * 365 * 100, 1),
                    }

        if best_cfg and best_cfg["net_pnl_usd"] > 0:
            best_score_per_market.append((cand, best_cfg))

    # Rank candidate markets by risk-adjusted capital efficiency (ROI)
    best_score_per_market.sort(key=lambda x: x[1]["roi_daily_pct"], reverse=True)

    # Greedy knapsack allocation under total_bankroll_usd
    allocated = []
    remaining_capital = total_bankroll_usd

    for cand, cfg in best_score_per_market:
        if len(allocated) >= max_markets:
            break
        cost = cfg["capital_usd"]
        if cost <= remaining_capital:
            allocated.append({
                "market": cand.question,
                "daily_rate": cand.daily_rate,
                "max_spread_cents": cand.max_spread_cents,
                "competing_depth_usd": cand.competing_depth_usd,
                "category": cand.category,
                **cfg
            })
            remaining_capital -= cost

    total_gross = sum(x["gross_reward_usd"] for x in allocated)
    total_risk = sum(x["risk_penalty_usd"] for x in allocated)
    total_net = sum(x["net_pnl_usd"] for x in allocated)
    total_used = total_bankroll_usd - remaining_capital
    portfolio_daily_roi = (total_net / total_used) * 100 if total_used > 0 else 0.0
    portfolio_annual_roi = portfolio_daily_roi * 365.0

    return {
        "bankroll_usd": total_bankroll_usd,
        "capital_allocated_usd": round(total_used, 2),
        "unallocated_capital_usd": round(remaining_capital, 2),
        "num_markets_selected": len(allocated),
        "total_gross_reward_daily": round(total_gross, 2),
        "total_risk_penalty_daily": round(total_risk, 2),
        "total_net_daily_profit": round(total_net, 2),
        "portfolio_daily_roi_pct": round(portfolio_daily_roi, 2),
        "portfolio_annual_roi_pct": round(portfolio_annual_roi, 1),
        "allocations": allocated,
    }


def main():
    parser = argparse.ArgumentParser(description="Optimize Liquidity Rewards Portfolio")
    parser.add_argument("--snapshot", default="data/processed/live_reward_head_pools.json")
    parser.add_argument("--bankroll", type=float, default=2000.0)
    parser.add_argument("--max-markets", type=int, default=4)
    args = parser.parse_args()

    snap_path = Path(args.snapshot)
    if not snap_path.exists():
        logger.error(f"Snapshot not found: {snap_path}")
        return 1

    candidates = load_candidate_pools(snap_path)
    logger.info(f"Loaded {len(candidates)} candidate pools for optimization...")

    opt = optimize_portfolio(
        candidates,
        total_bankroll_usd=args.bankroll,
        max_markets=args.max_markets,
    )

    log.info("\n" + "=" * 105)
    log.info(f"OPTIMAL REWARD HARVESTING PORTFOLIO (Bankroll: ${args.bankroll:,.0f} | Target Markets: {args.max_markets})")
    log.info("=" * 105)
    log.info(f"{'Market':<35} | {'Size':<6} | {'Spread':<7} | {'Gross/d':<8} | {'Risk/d':<7} | {'Net/d':<7} | {'Ann ROI'}")
    log.info("-" * 105)
    for a in opt["allocations"]:
        q = a["market"][:34]
        sz = f"{a['shares']:.0f}"
        spr = f"{a['spread_cents']:.1f}c"
        gr = f"${a['gross_reward_usd']:.2f}"
        rk = f"-${a['risk_penalty_usd']:.2f}"
        nt = f"${a['net_pnl_usd']:.2f}"
        roi = f"{a['annual_roi_pct']:,.1f}%"
        log.info(f"{q:<35} | {sz:<6} | {spr:<7} | {gr:<8} | {rk:<7} | {nt:<7} | {roi}")
    log.info("-" * 105)
    log.info(f"TOTALS: Capital Used: ${opt['capital_allocated_usd']:,.0f} / ${opt['bankroll_usd']:,.0f} | "
          f"Gross: ${opt['total_gross_reward_daily']:.2f}/d | Net Profit: ${opt['total_net_daily_profit']:.2f}/d "
          f"({opt['portfolio_daily_roi_pct']:.2f}%/d -> {opt['portfolio_annual_roi_pct']:,.1f}% APR)")
    log.info("=" * 105 + "\n")

    out_file = Path("data/processed/optimal_reward_portfolio.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(opt, f, indent=2)
    logger.info(f"Portfolio saved to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
