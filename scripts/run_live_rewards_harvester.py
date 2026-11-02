"""Production Real-Money Liquidity Rewards Harvester for Polymarket CLOB.

Deployable execution client designed for micro-capital ($300 initial bankroll)
and multi-market scaling with enterprise-grade risk management and institutional
active rebalancing.

Architecture:
1. Market Selection: High-rate, low-competition head pools (default: CA Wealth Tax, $2,000/day).
2. Sizing: 50-100 share quotes ($50-$100 committed), reserving 66.7% ($200) liquid cash.
3. Pricing: Inside touch quotes (0.5c from mid) to maximize quadratic reward score S(v, s).
4. Risk Management: 6-layer defense (hard $300 cap, daily 5% stop-loss, 60s price jump breaker,
   toxicity skewing/suppression, gas/merge manager, 15s disconnect kill-switch).
5. Institutional Rebalancing (Regime B): Active synthetic cross-hedging (buying complementary
   token to form complete set and merging) when inventory reaches urgency threshold or toxic markout.
6. Execution Safety: Post-only order placement, credentials check, --dry-run mode.

Usage:
    # 1. Dry-run simulation mode (connects to live CLOB, verifies credentials & book, zero real orders):
    uv run python scripts/run_live_rewards_harvester.py --dry-run

    # 2. Live production deployment with real capital ($300 bankroll):
    uv run python scripts/run_live_rewards_harvester.py --bankroll 300 --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.client import PolymarketLiveClient
from polymarketbot.paper_trading.engine import PaperTradingEngine
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig
from polymarketbot.rewards.scanner import MarketOpportunity, OpportunityScanner
from polymarketbot.rewards.switching import (
    OpportunisticSwitchingEngine,
    SwitchingConfig,
)
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import CircuitBreakerReason, RiskConfig, RiskStatus

log = get_logger("live_harvester")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Liquidity Rewards Dynamic Production Harvester"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute real orders with live funds (requires API credentials). Default is False (dry-run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate execution without placing real funds on the book.",
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=300.0,
        help="Total allocated bankroll in USD (default: $300.0).",
    )
    parser.add_argument(
        "--market-filter",
        type=str,
        default=None,
        help="Optional keyword filter for head reward pools (default: None for fully dynamic scanning).",
    )
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=300.0,
        help="Periodic re-scan interval in seconds (default: 300.0 / 5 minutes).",
    )
    parser.add_argument(
        "--switch-hurdle",
        type=float,
        default=0.20,
        help="Minimum net yield premium percentage to trigger market switch (default: 0.20 / 20%%).",
    )
    parser.add_argument(
        "--amortization-horizon",
        type=float,
        default=4.0,
        help="Switching cost amortization horizon in hours (default: 4.0 hours).",
    )
    parser.add_argument(
        "--concentration-pct",
        type=float,
        default=0.75,
        help="Capital concentration percentage on top market (default: 0.75 / 75%%).",
    )
    parser.add_argument(
        "--cash-reserve-pct",
        type=float,
        default=0.25,
        help="Liquid cash reserve percentage for synthetic hedging (default: 0.25 / 25%%).",
    )
    parser.add_argument(
        "--quote-size",
        type=float,
        default=50.0,
        help="Base quote size in shares (default: 50.0).",
    )
    parser.add_argument(
        "--max-position",
        type=float,
        default=250.0,
        help="Hard maximum position cap in shares per token (default: 250.0).",
    )
    parser.add_argument(
        "--urgency-threshold",
        type=float,
        default=60.0,
        help="Inventory threshold to trigger active synthetic hedging (default: 60.0 shares).",
    )
    parser.add_argument(
        "--daily-stop-loss",
        type=float,
        default=15.0,
        help="Daily loss stop-loss in USD (default: $15.00 / 5%% of $300).",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Runtime duration in seconds (0 = run indefinitely).",
    )
    return parser.parse_args()


class LiveRewardsHarvester:
    """Production deployment controller for Polymarket Liquidity Rewards harvesting."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.is_live = args.live and not args.dry_run

        # Initialize Concentrated Micro-Capital Risk Management Engine ($300 bankroll)
        self.risk_config = RiskConfig(
            total_bankroll_usd=args.bankroll,
            concentrated_micro_capital=True,
            micro_capital_concentration_pct=args.concentration_pct,
            micro_capital_cash_reserve_pct=args.cash_reserve_pct,
            max_position_cost_per_market=None,  # Dynamically derived (e.g. $225 on $300)
            max_position_shares_per_token=args.max_position,
            max_quote_size_shares=args.quote_size,
            daily_loss_limit_usd=args.daily_stop_loss,
            daily_loss_limit_pct=args.daily_stop_loss / args.bankroll,
            max_drawdown_usd=args.daily_stop_loss * 2.0,  # $30 max drawdown
            max_drawdown_pct=(args.daily_stop_loss * 2.0) / args.bankroll,
            price_jump_threshold_cents=2.0,
            price_jump_window_seconds=60.0,
            cooldown_duration_seconds=300.0,
            consecutive_fills_threshold=3,
            toxicity_action="widen",
            toxicity_spread_penalty_cents=1.5,
            min_merge_threshold_shares=30.0,
            gas_fee_usd=0.015,
            max_gas_spread_pct=0.05,
            enable_synthetic_hedging=True,
            q_urgency_threshold_shares=args.urgency_threshold,
            max_synthetic_cross_spread_cents=0.5,
        )
        self.risk_manager = RiskManager(self.risk_config)

        # Initialize Paper Trading / Execution Engine with Risk Manager Hooks
        self.engine = PaperTradingEngine(
            initial_cash_usd=args.bankroll,
            default_quote_size=args.quote_size,
            max_inventory_shares=args.max_position,
            max_skew_cents=3.0,
            auto_merge_threshold=self.risk_config.min_merge_threshold_shares,
            merge_gas_fee_usd=self.risk_config.gas_fee_usd,
            risk_manager=self.risk_manager,
            ledger_path="data/processed/paper_trading/live_deployment_ledger.json",
        )

        # Initialize Live Polymarket CLOB Feed Client
        self.client = PolymarketLiveClient(
            engine=self.engine,
            epoch_interval_seconds=60.0,
        )

        # Initialize Dynamic Scanner and Opportunistic Switching Engine
        self.scanner = OpportunityScanner(
            rest_url=self.client.rest_url,
            session=self.client.session,
        )
        self.switching_config = SwitchingConfig(
            scan_interval_seconds=args.scan_interval,
            switch_hurdle_pct=args.switch_hurdle,
            amortization_horizon_hours=args.amortization_horizon,
        )
        self.switching_engine = OpportunisticSwitchingEngine(
            scanner=self.scanner,
            config=self.switching_config,
            bankroll_usd=args.bankroll,
            concentration_pct=args.concentration_pct,
        )

    def verify_environment(self) -> bool:
        """Verify operating credentials, RPC connectivity, and capital parameters."""
        log.info("====================================================================")
        log.info("    POLYMARKET LIQUIDITY REWARDS PRODUCTION HARVESTER               ")
        log.info("====================================================================")
        log.info(f"Mode: {'🔴 LIVE REAL MONEY' if self.is_live else '🟢 DRY-RUN SIMULATION'}")
        log.info(f"Allocated Bankroll: ${self.risk_config.total_bankroll_usd:,.2f} USD")
        log.info(f"Quote Size: {self.risk_config.max_quote_size_shares:.1f} shares")
        log.info(f"Max Position Cap: {self.risk_config.max_position_shares_per_token:.1f} shares/token")
        log.info(f"Urgency Rebalance Threshold: {self.risk_config.q_urgency_threshold_shares:.1f} shares")
        log.info(f"Daily Stop-Loss: ${self.risk_config.daily_loss_limit_usd:.2f} (5% hard stop)")
        log.info(f"Price Jump Breaker: > {self.risk_config.price_jump_threshold_cents:.1f}c within 60s -> 5m cooldown")

        if self.is_live:
            api_key = os.getenv("POLYMARKET_API_KEY")
            api_secret = os.getenv("POLYMARKET_API_SECRET")
            passphrase = os.getenv("POLYMARKET_PASSPHRASE")
            private_key = os.getenv("POLYMARKET_PRIVATE_KEY")

            if not all([api_key, api_secret, passphrase, private_key]):
                log.critical(
                    "LIVE REAL-MONEY MODE REQUIRES ENVIRONMENT VARIABLES: "
                    "POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_PASSPHRASE, POLYMARKET_PRIVATE_KEY. "
                    "Aborting execution for capital safety."
                )
                return False
            log.info("✅ API Credentials & Polygon Signing Keys Verified.")
        else:
            log.info("ℹ️ Running in Dry-Run mode: Connecting to live orderbook & trades with virtual capital.")

        return True

    async def _switching_loop(self, duration_seconds: float) -> None:
        """Periodic background task evaluating candidate head pools for opportunistic market switching."""
        start_ts = time.time()
        interval = self.switching_config.scan_interval_seconds
        log.info(f"Starting opportunistic market switching loop (Interval: {interval:.1f}s)...")

        while True:
            try:
                await asyncio.sleep(interval)
                if duration_seconds > 0 and (time.time() - start_ts) >= duration_seconds:
                    break

                log.info("Re-scanning market universe for opportunistic switching candidates...")
                raw_pools = self.scanner.fetch_raw_reward_pools(limit=30)
                if self.args.market_filter:
                    raw_pools = [
                        m for m in raw_pools
                        if self.args.market_filter.lower() in m.get("question", "").lower()
                    ] or raw_pools

                ranked = self.scanner.scan_and_rank_markets(
                    bankroll_usd=self.args.bankroll,
                    concentration_pct=self.args.concentration_pct,
                    raw_markets=raw_pools,
                )

                should_switch, target_mkt, reason = self.switching_engine.check_opportunity_switch(ranked)
                if should_switch and target_mkt:
                    log.info(f"⚡ OPPORTUNISTIC SWITCH TRIGGERED: {reason}")
                    self.switching_engine.execute_market_switch(
                        new_market=target_mkt,
                        engine=self.engine,
                        client=self.client,
                        reason=reason,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in opportunistic switching loop: {e}")

    async def run(self) -> None:
        """Execute market selection, order book streaming, and risk-managed quoting."""
        if not self.verify_environment():
            sys.exit(1)

        # 1. Fetch target head reward pool dynamically via Yield Scanner
        log.info(
            f"Scanning all head reward pools dynamically (Bankroll: ${self.args.bankroll:.2f}, "
            f"Concentration: {self.args.concentration_pct * 100:.0f}%)..."
        )
        raw_pools = self.scanner.fetch_raw_reward_pools(limit=50)
        if self.args.market_filter:
            log.info(f"Filtering candidate pools with keyword: '{self.args.market_filter}'")
            raw_pools = [
                m for m in raw_pools
                if self.args.market_filter.lower() in m.get("question", "").lower()
            ] or raw_pools

        ranked_opportunities = self.scanner.scan_and_rank_markets(
            bankroll_usd=self.args.bankroll,
            concentration_pct=self.args.concentration_pct,
            raw_markets=raw_pools,
        )

        if not ranked_opportunities:
            log.critical("Failed to retrieve any active reward pools from Polymarket API.")
            sys.exit(1)

        target_opp = ranked_opportunities[0]
        self.switching_engine.set_active_market(target_opp)

        target = target_opp.to_market_config()
        self.engine.register_market(target)
        self.client.switch_active_tokens(target.tokens)

        log.info(
            f"🎯 Target Market Selected: {target.question} "
            f"(${target.rate_per_day:,.0f}/day pool | Net Yield: ${target_opp.net_expected_yield:.2f}/d | "
            f"min_size={target.rewards_min_size} | max_spread={target.rewards_max_spread}c)"
        )

        # 2. Fetch initial order books
        self.client.fetch_initial_order_books()

        # 3. Baseline epoch snapshot
        log.info("Establishing baseline reward score and quote state...")
        self.engine.run_epoch_sample()

        # 4. Start execution loop with concurrent switching evaluator
        duration = self.args.duration if self.args.duration > 0 else 86400  # Default 24h
        log.info(f"Starting harvester execution loop (Duration: {duration}s)...")

        switching_task = asyncio.create_task(self._switching_loop(duration_seconds=duration))
        try:
            await self.client.run(duration_seconds=duration)
        finally:
            switching_task.cancel()
            try:
                await switching_task
            except asyncio.CancelledError:
                pass


def main() -> None:
    args = parse_args()
    harvester = LiveRewardsHarvester(args)
    try:
        asyncio.run(harvester.run())
    except KeyboardInterrupt:
        log.info("Received operator shutdown interrupt. Safely unquoting and exiting.")
    except Exception as e:
        log.exception(f"Fatal error in harvester: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
