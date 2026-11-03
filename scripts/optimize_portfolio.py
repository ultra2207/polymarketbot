#!/usr/bin/env python3
"""CLI Portfolio Allocator & MINLP Optimizer for Polymarket Liquidity Rewards.

Uses Google OR-Tools SCIP solver to calculate the mathematically optimal capital allocation
across all audited reward pools, maximizing daily cash payout while enforcing:
1. $1.00 daily payout floor per market.
2. 25% cash buffer retention.
3. Order size minimums and probability divergence filters.
"""

from __future__ import annotations

import argparse
import sys

from polymarketbot.net import install_doh_fallback
from polymarketbot.rewards.allocator import LiquidityRewardsAllocator
from polymarketbot.rewards.scanner import find_optimal_lp_markets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Mathematical Portfolio Allocator & MINLP Optimizer"
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=150.0,
        help="Total portfolio bankroll in USD (default: $150.00).",
    )
    parser.add_argument(
        "--reserve-pct",
        type=float,
        default=0.25,
        help="Fraction of bankroll reserved for synthetic cross-hedging (default: 0.25).",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Maximum concurrent markets to allocate into (default: unconstrained / optimal).",
    )
    parser.add_argument(
        "--spread-offset",
        type=float,
        default=1.50,
        help="Target half-spread offset in cents (default: 1.50c).",
    )
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    install_doh_fallback()

    print(f"\nScanning live Polymarket universe for candidates (Bankroll: ${args.bankroll:,.2f})...")
    candidates = find_optimal_lp_markets(
        bankroll_usd=args.bankroll,
        min_days_to_expiry=7.0,
        min_mid_price=0.12,
        max_mid_price=0.88,
        limit_output=20,
    )

    if not candidates:
        print("No viable reward market candidates found.")
        return 1

    allocator = LiquidityRewardsAllocator(
        reserve_pct=args.reserve_pct,
        default_spread_offset_cents=args.spread_offset,
    )

    res = allocator.optimize_allocation_milp(
        candidates=candidates,
        bankroll_usd=args.bankroll,
        max_active_markets=args.max_markets,
        spread_offset_cents=args.spread_offset,
    )

    print("\n" + "=" * 115)
    print(f"  POLYMARKET MATHEMATICAL PORTFOLIO ALLOCATION STATEMENT | SOLVER: {res.solver_name}")
    print("=" * 115)
    print(f"  Total Bankroll:       ${res.bankroll_usd:>10,.2f} USDC")
    print(f"  Allocated Capital:    ${res.total_allocated:>10,.2f} USDC (Active Quoting)")
    print(f"  Cash Reserve Buffer:  ${res.cash_reserve:>10,.2f} USDC (Synthetic Hedging)")
    print(f"  Active Markets:       {res.active_markets_count:>10} markets")
    print(f"  Total Daily Payout:   ${res.total_daily_payout:>10,.2f} / day")
    print(f"  Total Monthly Payout: ${res.total_monthly_payout:>10,.2f} / month")
    print(f"  Portfolio Daily ROI:  +{res.portfolio_daily_roi_pct:>9.2f}% / day")
    print(f"  Portfolio Monthly ROI:+{res.portfolio_monthly_roi_pct:>9.2f}% / month")
    print("-" * 115)

    print(f"{'#':<3} | {'Market Question':<45} | {'Allocated $':<12} | {'Quote Size':<10} | {'Daily Pay':<11} | {'Daily ROI':<9} | {'Floor $':<8}")
    print("-" * 115)
    for i, a in enumerate(res.allocations, 1):
        print(
            f"{i:<3} | "
            f"{a.question[:45]:<45} | "
            f"${a.allocated_capital:>10,.2f} | "
            f"{a.quote_size:>8.1f}sh | "
            f"${a.daily_reward_payout:>8,.2f}/d | "
            f"+{a.daily_roi_pct:>6.1f}% | "
            f"${a.min_floor_capital:>6,.2f}"
        )
    print("=" * 115 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
