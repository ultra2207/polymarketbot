"""Domain 2 Settlement Harvester Engine.

Production orchestrator integrating event listening, passive CLOB order placement,
emergency circuit breaker protection, and automated CTF settlement redemption.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from polymarketbot.logs import get_logger
from polymarketbot.trading.client import LiveClobClient
from polymarketbot.trading.ctf import PolygonCtfClient
from polymarketbot.trading.domain2.types import (
    Domain2MarketState,
    UmaProposalEvent,
    UmaDisputeEvent,
    ConditionResolutionEvent,
    DiscountOpportunity,
    Domain2Position,
)
from polymarketbot.trading.domain2.event_listener import Domain2EventListener
from polymarketbot.trading.domain2.circuit_breaker import Domain2CircuitBreaker
from polymarketbot.trading.domain2.clob_order_placer import Domain2ClobOrderPlacer
from polymarketbot.trading.domain2.settlement_worker import Domain2SettlementWorker
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Candidate,
    Domain2OrtoolsAllocator,
    Domain2OptimizationResult,
    Domain2Allocation,
)

logger = get_logger("domain2_harvester_engine")


class Domain2SettlementHarvester:
    """Production coordinator for Domain 2 Settlement Discount Harvesting."""

    def __init__(
        self,
        clob_client: LiveClobClient,
        ctf_client: PolygonCtfClient,
        initial_capital_usdc: float = 10000.0,
        dry_run: bool = True,
        circuit_breaker: Optional[Domain2CircuitBreaker] = None,
        allocator: Optional[Domain2OrtoolsAllocator] = None,
    ):
        self.clob_client = clob_client
        self.ctf_client = ctf_client
        self.dry_run = dry_run
        self.available_cash = initial_capital_usdc
        self.initial_capital = initial_capital_usdc

        # Mathematical Global Optima Allocator (Google OR-Tools)
        self.allocator = allocator or Domain2OrtoolsAllocator()

        # Emergency Circuit Breaker (Broad bounds so math controls entry, halted on disputes)
        self.circuit_breaker = circuit_breaker or Domain2CircuitBreaker(
            max_market_allocation_pct=0.10,
            max_portfolio_exposure_usdc=initial_capital_usdc,
            max_concurrent_markets=15,
            min_price=0.850,
            max_price=0.998,
            min_hours_to_expiry=0.01,
            max_hours_to_expiry=72.0,
        )

        self.order_placer = Domain2ClobOrderPlacer(
            clob_client=self.clob_client,
            target_discount_bps=150.0,  # 1.50% discount ($0.985 limit bid)
        )

        self.settlement_worker = Domain2SettlementWorker(
            ctf_client=self.ctf_client,
            dry_run=self.dry_run,
        )

        self.event_listener = Domain2EventListener(
            on_proposal=self.handle_proposal,
            on_dispute=self.handle_dispute,
            on_resolution=self.handle_resolution,
        )

        self._positions: Dict[str, Domain2Position] = {}
        self._market_states: Dict[str, Domain2MarketState] = {}

    def get_portfolio_equity(self) -> float:
        """Calculate total mark-to-market portfolio equity."""
        pos_val = sum(p.shares * p.entry_price for p in self._positions.values() if not p.is_redeemed)
        return self.available_cash + pos_val

    def handle_proposal(self, event: UmaProposalEvent) -> None:
        """Process incoming UMA ProposePrice event."""
        cid = event.condition_id.lower()
        logger.info("Received UMA ProposePrice for {}: proposed_price={}", cid[:14], event.proposed_price)
        self._market_states[cid] = Domain2MarketState.PROPOSED

        # Evaluate discount opportunity
        hours_left = max(0.0, (event.expiration_timestamp - time.time()) / 3600.0) if event.expiration_timestamp > 0 else 2.0
        
        # In production, query book via LiveClobClient or book feed
        # Here we construct an opportunity assuming standard winning outcome
        opp = DiscountOpportunity(
            condition_id=cid,
            winning_asset_id=event.identifier,
            losing_asset_id="",
            proposed_price=event.proposed_price or 1.0,
            best_bid=0.980,
            best_ask=0.988,
            discount_bps=150.0,
            hours_to_liveness_expiry=hours_left,
            recommended_bid_price=0.985,
            recommended_size_usdc=1000.0,
        )

        # Check circuit breaker
        equity = self.get_portfolio_equity()
        curr_cost = self._positions[cid].cost_basis_usdc if cid in self._positions else 0.0
        total_exposure = sum(p.cost_basis_usdc for p in self._positions.values() if not p.is_redeemed)
        active_count = sum(1 for p in self._positions.values() if not p.is_redeemed)

        allowed, alloc_cap, reason = self.circuit_breaker.check_entry_allowed(
            opportunity=opp,
            current_equity_usdc=equity,
            current_market_cost_usdc=curr_cost,
            total_open_exposure_usdc=total_exposure,
            active_markets_count=active_count,
        )

        if not allowed:
            logger.info("Entry rejected by circuit breaker for condition {}: {}", cid[:14], reason)
            return

        # Place passive limit bid
        alloc_to_use = min(alloc_cap, self.available_cash)
        order_res = self.order_placer.place_discount_bid(opp, alloc_to_use)
        if order_res and order_res.success:
            self._market_states[cid] = Domain2MarketState.QUOTING

    def handle_dispute(self, event: UmaDisputeEvent) -> None:
        """Process emergency UMA DisputePrice event."""
        cid = event.condition_id.lower()
        self._market_states[cid] = Domain2MarketState.DISPUTED_HALTED
        
        # 1. Trigger circuit breaker
        should_cancel, msg = self.circuit_breaker.handle_dispute_event(event)

        # 2. Emergency cancel all active CLOB orders
        if should_cancel:
            cancelled = self.order_placer.cancel_market_orders(cid)
            logger.warning("Emergency circuit breaker cancelled orders: {}", cancelled)

        # 3. Inform settlement worker to halt redemption
        self.settlement_worker.mark_disputed(cid)

    def handle_resolution(self, event: ConditionResolutionEvent) -> None:
        """Process on-chain ConditionResolution event."""
        cid = event.condition_id.lower()
        self._market_states[cid] = Domain2MarketState.RESOLVED
        self.settlement_worker.on_condition_resolution(event)

        # Trigger redemption check
        redeem_results = self.settlement_worker.check_and_settle_ready_positions()
        for res in redeem_results:
            if res.success and res.condition_id in self._positions:
                pos = self._positions[res.condition_id]
                # Payout received (assume 1.0 per share on win)
                payout_proceeds = pos.shares * 1.0
                profit = payout_proceeds - pos.cost_basis_usdc
                pos.realized_pnl = profit
                self.available_cash += payout_proceeds
                self._market_states[res.condition_id] = Domain2MarketState.SETTLED
                logger.info(
                    "Settlement complete for condition {}: proceeds ${:,.2f}, profit ${:,.2f}",
                    res.condition_id[:14], payout_proceeds, profit,
                )

    def record_fill(
        self,
        condition_id: str,
        asset_id: str,
        shares: float,
        price: float,
        liveness_expiry_ts: int,
    ) -> Domain2Position:
        """Record trade execution fill into internal inventory and settlement tracker."""
        cid = condition_id.lower()
        cost = shares * price
        self.available_cash -= cost

        pos = Domain2Position(
            condition_id=cid,
            asset_id=asset_id,
            shares=shares,
            cost_basis_usdc=cost,
            entry_price=price,
            liveness_expiry_ts=liveness_expiry_ts,
            proposed_price=1.0,
        )
        self._positions[cid] = pos
        self._market_states[cid] = Domain2MarketState.LIVENESS_ACTIVE
        self.settlement_worker.register_position(pos)
        logger.info("Recorded fill for {}: {} shares @ ${:.4f} (${:.2f} USDC)", cid[:14], shares, price, cost)
        return pos

    def poll_settlements(self) -> None:
        """Periodic heartbeat to process dispute window expirations and redemptions."""
        results = self.settlement_worker.check_and_settle_ready_positions()
        for res in results:
            if res.success and res.condition_id in self._positions:
                pos = self._positions[res.condition_id]
                payout_proceeds = pos.shares * 1.0
                profit = payout_proceeds - pos.cost_basis_usdc
                pos.realized_pnl = profit
                self.available_cash += payout_proceeds
                self._market_states[res.condition_id] = Domain2MarketState.SETTLED

    def scan_universe_candidates(
        self,
        manifest_path: str = "data/tape/manifest_current.json",
        max_hours_ahead: float = 48.0,
        min_price: float = 0.90,
    ) -> List[Domain2Candidate]:
        """Scan active Polymarket universe to discover settlement and basis harvesting candidates.

        Covers all markets with decent depth, extracting token IDs, categories, and time to resolution.
        """
        path = Path(manifest_path)
        if not path.exists():
            logger.warning("Manifest path {} does not exist; returning empty candidate list", manifest_path)
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error("Failed to read universe manifest: {}", e)
            return []

        markets = data.get("markets", [])
        candidates: List[Domain2Candidate] = []
        now_ts = time.time()

        for m in markets:
            cid = m.get("condition_id", "").lower()
            if self.circuit_breaker.is_market_halted(cid):
                continue

            end_date_str = m.get("end_date")
            if not end_date_str:
                continue

            try:
                # Parse ISO date string (e.g. '2026-11-06 18:00:00+00')
                clean_date = end_date_str.replace("+00", "+00:00")
                if "+" not in clean_date and not clean_date.endswith("Z"):
                    clean_date += "+00:00"
                dt = datetime.fromisoformat(clean_date)
                end_ts = dt.timestamp()
            except Exception:
                continue

            hours_left = (end_ts - now_ts) / 3600.0
            # Include markets resolving soon or recently closed awaiting oracle proposal
            if hours_left > max_hours_ahead or hours_left < -24.0:
                continue

            tokens = m.get("clob_tokens", [])
            if not tokens or len(tokens) < 2:
                continue

            # In live operation, tokens[0] is YES, tokens[1] is NO
            # Book level querying: in production, fetch ladder via LiveClobClient
            question = m.get("question", "")
            category = "general"
            for tag in m.get("tags", []):
                if tag in ("crypto", "politics", "sports", "weather", "economics", "pop-culture"):
                    category = tag
                    break

            # Default price estimate: top of book from volume/liquidity heuristics or direct query
            # Assumes the dominant outcome token has price >= min_price
            price_est = 0.970 if hours_left <= 2.0 else 0.950
            if price_est < min_price:
                continue

            depth_usd = float(m.get("liquidity", 0.0))
            if depth_usd <= 0.0:
                depth_usd = 2500.0  # Default conservative resting depth

            from polymarketbot.fees import schedule_from_market
            fee_sched = schedule_from_market(m)

            cand = Domain2Candidate(
                condition_id=cid,
                token_id=tokens[0],
                question=question,
                category=category,
                price=price_est,
                hours_to_resolution=max(0.25, hours_left),
                depth_usd=depth_usd,
                fee_rate=fee_sched.rate,
                fee_exponent=fee_sched.exponent,
                fee_schedule=fee_sched,
                is_taker=False,  # Maker limit bids pay 0.0% fee
                redemption_gas_usd=0.005,  # Polygon PoS CTF.redeemPositions gas
                is_uma_proposed=(hours_left <= 2.0),
            )
            candidates.append(cand)

        logger.info(
            "Scanned universe ({} total markets): found {} candidates within {:.1f}h horizon",
            len(markets), len(candidates), max_hours_ahead,
        )
        return candidates

    def execute_optimal_portfolio(
        self,
        candidates: List[Domain2Candidate],
    ) -> Domain2OptimizationResult:
        """Run the Google OR-Tools Global Optimum Allocator and dispatch optimal orders."""
        result = self.allocator.optimize_allocation(
            candidates=candidates,
            bankroll_usd=self.available_cash,
        )

        logger.info(
            "Portfolio Optimization complete [{}]: total allocated ${:,.2f} / ${:,.2f} across {} markets",
            result.solver_status,
            result.total_allocated_usd,
            result.usable_capital_usd,
            result.active_markets_count,
        )

        # Dispatch orders for each allocated candidate
        for alloc in result.allocations:
            cid = alloc.condition_id.lower()
            if self.circuit_breaker.is_market_halted(cid):
                continue

            opp = DiscountOpportunity(
                condition_id=cid,
                winning_asset_id=alloc.token_id,
                losing_asset_id="",
                proposed_price=1.0,
                best_bid=alloc.price - 0.005,
                best_ask=alloc.price,
                discount_bps=(1.0 - alloc.price) * 10000.0,
                hours_to_liveness_expiry=alloc.hours_to_resolution,
                recommended_bid_price=alloc.effective_avg_price or alloc.price,
                recommended_size_usdc=alloc.allocated_usd,
            )

            order_res = self.order_placer.place_discount_bid(opp, alloc.allocated_usd)
            if order_res and order_res.success:
                self._market_states[cid] = Domain2MarketState.QUOTING
                logger.info(
                    "Dispatched optimal limit bid for {}: ${:,.2f} @ {:.4f} ({} shares)",
                    cid[:14], alloc.allocated_usd, alloc.effective_avg_price or alloc.price, alloc.shares,
                )

        return result
