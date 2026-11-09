#!/usr/bin/env python3
"""CLI utility to find best Polymarket LP reward markets by ROI.

Scans all active Polymarket reward pools via CLOB REST API, applies institutional
safety filters (divergence bounds, days to expiry, min size feasibility), fetches
real-time L2 order book depth in parallel to compute the true competing quadratic
score, and ranks markets by net Daily ROI (%) on deployed capital.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.rewards.scanner import find_optimal_lp_markets

logger = get_logger("lp_optimizer")
install_doh_fallback()


def print_leaderboard(markets: list[dict[str, Any]], bankroll: float) -> None:
    """Print high-density terminal leaderboard."""
    logger.info("\n" + "=" * 135)
    logger.info(f"  POLYMARKET OPTIMAL LIQUIDITY REWARDS LEADERBOARD | BANKROLL: ${bankroll:.2f} USDC")
    logger.info("  (Audited against live L2 order book depth with strict $1.00 daily payout floor enforcement)")
    logger.info("=" * 135)
    header = (
        f"{'Rank':<4} | {'Market Question':<45} | {'Pool/d':<8} | {'Mid':<5} | "
        f"{'Comp Depth':<11} | {'Quote':<8} | {'Real Pay/d':<11} | {'Daily ROI':<10} | {'Floor Status'}"
    )
    logger.info(header)
    logger.info("-" * 135)

    for idx, m in enumerate(markets, 1):
        q = m["question"][:43]
        pool = f"${m['daily_pool']:,.0f}"
        mid = f"{m['real_mid']:.2f}"
        depth = f"{m['qualifying_shares']:,.0f} sh"
        q_sz = f"{m['quote_size']:.0f} sh"
        pay = f"${m['real_daily_payout']:.2f}/d"
        roi = f"+{m['daily_roi_pct']:.1f}%"
        status = "PASSED (Floor Cleared)" if m["clears_floor"] else f"FAILED (${m['raw_daily_payout']:.2f}/d)"
        logger.info(f"{idx:<4} | {q:<45} | {pool:<8} | {mid:<5} | {depth:<11} | {q_sz:<8} | {pay:<11} | {roi:<10} | {status}")

    logger.info("=" * 135 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Find best Polymarket LP reward markets by ROI.")
    parser.add_argument("--bankroll", type=float, default=150.0, help="Initial bankroll in USDC (default: 150.0)")
    parser.add_argument("--min-days", type=float, default=7.0, help="Minimum days to resolution (default: 7.0)")
    parser.add_argument("--limit", type=int, default=15, help="Number of markets to display (default: 15)")
    args = parser.parse_args()

    results = find_optimal_lp_markets(
        bankroll_usd=args.bankroll,
        min_days_to_expiry=args.min_days,
        limit_output=args.limit,
    )

    if not results:
        logger.info("No qualifying markets found matching safety criteria.")
        sys.exit(1)

    print_leaderboard(results, args.bankroll)

    best = results[0]
    logger.info(f"[*] Optimal Market Selected: {best['question']}")
    logger.info(f"    Condition ID:    {best['condition_id']}")
    logger.info(f"    Daily Reward:    ${best['daily_pool']:,.2f}/day")
    logger.info(f"    Expected Payout: ${best['real_daily_payout']:.2f}/day (+{best['daily_roi_pct']:.1f}%/day ROI)")
    logger.info(f"    Quote Sizing:    {best['quote_size']:.1f} shares @ mid {best['real_mid']:.3f}\n")


if __name__ == "__main__":
    main()
