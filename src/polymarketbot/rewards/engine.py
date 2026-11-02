"""Production-grade Polymarket Liquidity Rewards Bot & Backtest Engine.

Integrates:
1. Dynamic Avellaneda-Stoikov inventory skewing.
2. Complete-set merge logic (YES + NO -> USDC).
3. Realistic order book depth and queue dynamics.
4. Exact 1,440-minute epoch reward score accumulation.
5. Comprehensive PnL reporting:
   Gross Rewards + Spread Captured - Adverse Selection - Inventory Decay.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from polymarketbot.rewards.inventory import AvellanedaStoikovModel, SkewedQuote
from polymarketbot.rewards.merger import CompleteSetPosition, MergeEvent
from polymarketbot.rewards.queue import FillExecution, OrderBookQueueSimulator
from polymarketbot.rewards.scoring import (
    DEFAULT_COMPETITOR_LADDER,
    OrderQuote,
    calculate_epoch_payout,
    compute_book_side_scores,
    compute_competitor_ladder_score,
    compute_minimum_score,
    normalize_sample_score,
    quadratic_order_score,
)

CATEGORY_REGIMES = {
    "politics": {
        "vol_1m_bps": 12.0,
        "jump_prob_1h": 0.015,
        "jump_mean_cents": 12.0,
        "trade_rate_per_min": 0.25,
        "avg_trade_size": 250.0,
        "decay_300s_ratio": 0.15,
        "decay_3600s_ratio": 0.40,
    },
    "sports": {
        "vol_1m_bps": 35.0,
        "jump_prob_1h": 0.080,
        "jump_mean_cents": 8.0,
        "trade_rate_per_min": 0.60,
        "avg_trade_size": 180.0,
        "decay_300s_ratio": 0.91,
        "decay_3600s_ratio": 0.98,
    },
    "crypto": {
        "vol_1m_bps": 45.0,
        "jump_prob_1h": 0.050,
        "jump_mean_cents": 5.0,
        "trade_rate_per_min": 0.85,
        "avg_trade_size": 300.0,
        "decay_300s_ratio": 0.91,
        "decay_3600s_ratio": 0.98,
    },
    "geopolitics": {
        "vol_1m_bps": 15.0,
        "jump_prob_1h": 0.025,
        "jump_mean_cents": 15.0,
        "trade_rate_per_min": 0.30,
        "avg_trade_size": 200.0,
        "decay_300s_ratio": 0.70,
        "decay_3600s_ratio": 0.85,
    },
    "default": {
        "vol_1m_bps": 20.0,
        "jump_prob_1h": 0.020,
        "jump_mean_cents": 10.0,
        "trade_rate_per_min": 0.35,
        "avg_trade_size": 220.0,
        "decay_300s_ratio": 0.50,
        "decay_3600s_ratio": 0.75,
    },
}


@dataclass
class MarketRewardParameters:
    """Market parameters configured by Polymarket."""

    condition_id: str
    question: str
    rate_per_day: float  # Total pool payout per day ($)
    rewards_min_size: float  # Min qualifying size (shares)
    rewards_max_spread: float  # Max qualifying spread (cents)
    initial_mid: float = 0.50
    category: str = "default"
    competing_qualifying_depth: float = 5000.0  # Competing shares in qualifying band
    competing_touch_depth: float = 1000.0  # Competing shares at touch


@dataclass
class EpochReport:
    """Comprehensive performance report for a 1,440-minute epoch."""

    epoch_index: int
    condition_id: str
    question: str
    daily_rate: float
    allocated_capital: float
    maker_epoch_score: float
    total_epoch_score: float
    reward_share_pct: float
    gross_rewards: float
    net_rewards: float
    spread_captured: float
    adverse_selection: float
    inventory_decay: float
    complete_sets_merged_usd: float
    net_pnl: float
    daily_roi_pct: float
    annual_roi_pct: float
    total_fills: int
    total_shares_traded: float
    final_yes_inventory: float
    final_no_inventory: float
    final_net_inventory: float
    passed_payout_floor: bool
    gas_fees: float = 0.0
    adverse_selection_60s: float = 0.0
    adverse_selection_300s: float = 0.0
    adverse_selection_3600s: float = 0.0


class LiquidityRewardsBotEngine:
    """Production market maker and simulation engine for Polymarket Liquidity Rewards."""

    def __init__(
        self,
        market: MarketRewardParameters,
        quote_size: float = 200.0,
        risk_gamma: float = 0.01,
        max_inventory: float = 2000.0,
        tick_size: float = 0.01,
        auto_merge: bool = True,
        seed: Optional[int] = 42,
    ) -> None:
        self.market = market
        self.quote_size = max(market.rewards_min_size, quote_size)
        self.max_inventory = max_inventory
        self.auto_merge = auto_merge

        self.regime = CATEGORY_REGIMES.get(
            market.category.lower(), CATEGORY_REGIMES["default"]
        )
        vol_daily = (self.regime["vol_1m_bps"] * math.sqrt(1440)) / 10000.0

        self.as_model = AvellanedaStoikovModel(
            gamma=risk_gamma,
            volatility=vol_daily,
            max_inventory=max_inventory,
            touch_spread_cents=1.0,
            tick_size=tick_size,
        )
        self.queue_sim = OrderBookQueueSimulator()
        self.position = CompleteSetPosition(auto_merge_threshold=100.0)
        self.rng = random.Random(seed)

    def simulate_epoch(
        self,
        epoch_idx: int = 1,
        n_minutes: int = 1440,
    ) -> EpochReport:
        """Simulate an exact 1,440-minute day with dynamic skewing, queueing, and rewards."""
        mid_price = self.market.initial_mid
        initial_mid = mid_price

        # Track minute-by-minute scores
        sample_maker_q_min: List[float] = []
        sample_competing_q_min: List[float] = []

        total_spread_captured = 0.0
        total_adverse_selection = 0.0
        total_adverse_60s = 0.0
        total_adverse_300s = 0.0
        total_adverse_3600s = 0.0
        total_fills_count = 0
        total_shares_traded = 0.0
        gas_fees_start = self.position.total_gas_fees

        # 1. Competing score estimation across 4-tier competitor depth ladder (Fix Gap B2)
        comp_side_depth = self.market.competing_qualifying_depth * 0.5
        comp_q1 = compute_competitor_ladder_score(
            max_spread_cents=self.market.rewards_max_spread,
            qualifying_depth=comp_side_depth,
            ladder=DEFAULT_COMPETITOR_LADDER,
        )
        comp_q2 = comp_q1
        comp_min = compute_minimum_score(comp_q1, comp_q2, mid_price)

        # 2. Pre-simulate price trajectory for multi-horizon adverse selection (Fix Gap B4)
        mid_prices = [mid_price]
        cur = mid_price
        p_jump = self.regime["jump_prob_1h"] / 60.0
        for _ in range(n_minutes + 60):
            dt_vol = (self.regime["vol_1m_bps"] / 10000.0) * cur
            drift = self.rng.gauss(0.0, dt_vol)
            jump = 0.0
            if self.rng.random() < p_jump:
                direction = 1.0 if self.rng.random() > 0.5 else -1.0
                jump_cents = self.rng.expovariate(1.0 / self.regime["jump_mean_cents"])
                jump = direction * (jump_cents / 100.0)
            cur = max(0.01, min(0.99, cur + drift + jump))
            mid_prices.append(cur)

        for minute in range(n_minutes):
            mid_price = mid_prices[minute]
            next_mid_60s = mid_prices[minute + 1]
            next_mid_300s = mid_prices[minute + 5]
            next_mid_3600s = mid_prices[minute + 60]

            # 3. Avellaneda-Stoikov inventory skewing
            # When q = 0: quote symmetrically at 1.0c touch (mid - 0.5c, mid + 0.5c)
            # When q != 0: skew quote prices (widen accumulating side, tighten reducing side)
            net_q = self.position.net_inventory
            quotes = self.as_model.compute_quotes(
                mid_price=mid_price,
                inventory_q=net_q,
                base_quote_size=self.quote_size,
                max_spread_cents=self.market.rewards_max_spread,
                min_size=self.market.rewards_min_size,
            )

            # 4. Synchronize resting quotes with order book queue
            if quotes.is_bid_active:
                comp_bid_depth = self.market.competing_touch_depth if quotes.bid_spread_cents <= 0.6 else self.market.competing_qualifying_depth * 0.25
                self.queue_sim.sync_quote(
                    order_id="maker_bid_yes",
                    side="BUY",
                    outcome="YES",
                    price=quotes.bid_price,
                    size=quotes.bid_size,
                    competing_depth_at_price=comp_bid_depth,
                    current_epoch=minute,
                )
            else:
                self.queue_sim.cancel_order("maker_bid_yes")

            if quotes.is_ask_active:
                comp_ask_depth = self.market.competing_touch_depth if quotes.ask_spread_cents <= 0.6 else self.market.competing_qualifying_depth * 0.25
                self.queue_sim.sync_quote(
                    order_id="maker_ask_yes",
                    side="SELL",
                    outcome="YES",
                    price=quotes.ask_price,
                    size=quotes.ask_size,
                    competing_depth_at_price=comp_ask_depth,
                    current_epoch=minute,
                )
            else:
                self.queue_sim.cancel_order("maker_ask_yes")

            # 5. Market trade arrival & fill execution (including crosses)
            if self.rng.random() < self.regime["trade_rate_per_min"]:
                trade_side = "BUY" if self.rng.random() > 0.5 else "SELL"
                trade_sz = self.rng.expovariate(1.0 / self.regime["avg_trade_size"])
                # Trade price near mid with occasional crossing slippage
                trade_offset = self.rng.gauss(0.0, 0.005)
                trade_px = max(0.01, min(0.99, mid_price + trade_offset))

                fills = self.queue_sim.process_trade(
                    trade_side=trade_side,
                    trade_price=trade_px,
                    trade_volume=trade_sz,
                    current_mid=mid_price,
                    next_mid_60s=next_mid_60s,
                    current_epoch=minute,
                    next_mid_300s=next_mid_300s,
                    next_mid_3600s=next_mid_3600s,
                    decay_ratio=self.regime.get("decay_300s_ratio", 0.0),
                )

                for f in fills:
                    total_fills_count += 1
                    total_shares_traded += f.fill_size
                    total_spread_captured += f.spread_captured
                    total_adverse_selection += f.adverse_selection_loss
                    total_adverse_60s += f.adverse_selection_60s
                    total_adverse_300s += f.adverse_selection_300s
                    total_adverse_3600s += f.adverse_selection_3600s

                    # Update position and auto-merge complete sets
                    self.position.record_fill(
                        outcome=f.outcome,
                        side=f.side,
                        price=f.fill_price,
                        size=f.fill_size,
                    )

            # 6. Evaluate official Polymarket quadratic scoring for this sample minute
            active_maker_quotes = []
            if quotes.is_bid_active and quotes.bid_size >= self.market.rewards_min_size:
                active_maker_quotes.append(
                    OrderQuote(
                        side="BUY",
                        outcome="YES",
                        price=quotes.bid_price,
                        size=quotes.bid_size,
                        duration_seconds=60.0,
                    )
                )
            if quotes.is_ask_active and quotes.ask_size >= self.market.rewards_min_size:
                active_maker_quotes.append(
                    OrderQuote(
                        side="SELL",
                        outcome="YES",
                        price=quotes.ask_price,
                        size=quotes.ask_size,
                        duration_seconds=60.0,
                    )
                )

            q_one, q_two = compute_book_side_scores(
                quotes=active_maker_quotes,
                mid_price_yes=mid_price,
                max_spread_cents=self.market.rewards_max_spread,
                min_size=self.market.rewards_min_size,
            )
            maker_q_min = compute_minimum_score(
                q_one=q_one,
                q_two=q_two,
                midpoint=mid_price,
                c_penalty=3.0,
            )

            sample_maker_q_min.append(maker_q_min)
            sample_competing_q_min.append(comp_min)

        # 7. Accumulate normalized epoch score and calculate payouts
        sample_q_normals = []
        for m_q, c_q in zip(sample_maker_q_min, sample_competing_q_min):
            sample_sum = m_q + c_q
            sample_q_normals.append(normalize_sample_score(m_q, sample_sum))

        maker_epoch_score = sum(sample_q_normals)
        # Total pool score across 1,440 samples
        total_epoch_score = float(n_minutes)

        reward_share, gross_rewards, net_rewards = calculate_epoch_payout(
            maker_epoch_score=maker_epoch_score,
            total_epoch_score=total_epoch_score,
            rate_per_day=self.market.rate_per_day,
            payout_floor_usd=1.00,
        )

        # 8. Inventory Decay / Mark-to-market markout
        # Evaluate open position against final mid price
        final_mid = mid_prices[n_minutes]
        mark_to_market_val = (self.position.shares_yes * final_mid) + (
            self.position.shares_no * (1.0 - final_mid)
        )
        open_cost_basis = self.position.cost_basis_yes + self.position.cost_basis_no
        inventory_decay = max(0.0, open_cost_basis - mark_to_market_val)

        # 9. Comprehensive PnL Accounting:
        # Net PnL = Gross Rewards + Spread Captured - Adverse Selection - Inventory Decay - Gas Fees
        epoch_gas_fees = self.position.total_gas_fees - gas_fees_start
        net_pnl = (
            net_rewards
            + total_spread_captured
            - total_adverse_selection
            - inventory_decay
            - epoch_gas_fees
        )

        # Capital allocated requirement: quoting both sides at touch
        allocated_capital = max(100.0, self.quote_size * max(0.20, initial_mid) * 2.0)
        daily_roi = (net_pnl / allocated_capital) * 100.0 if allocated_capital > 0 else 0.0
        annual_roi = daily_roi * 365.0

        return EpochReport(
            epoch_index=epoch_idx,
            condition_id=self.market.condition_id,
            question=self.market.question,
            daily_rate=self.market.rate_per_day,
            allocated_capital=round(allocated_capital, 2),
            maker_epoch_score=round(maker_epoch_score, 4),
            total_epoch_score=round(total_epoch_score, 4),
            reward_share_pct=round(reward_share * 100.0, 4),
            gross_rewards=round(gross_rewards, 2),
            net_rewards=round(net_rewards, 2),
            spread_captured=round(total_spread_captured, 2),
            adverse_selection=round(total_adverse_selection, 2),
            inventory_decay=round(inventory_decay, 2),
            complete_sets_merged_usd=round(self.position.total_unlocked_usd, 2),
            net_pnl=round(net_pnl, 2),
            daily_roi_pct=round(daily_roi, 2),
            annual_roi_pct=round(annual_roi, 1),
            total_fills=total_fills_count,
            total_shares_traded=round(total_shares_traded, 1),
            final_yes_inventory=round(self.position.shares_yes, 2),
            final_no_inventory=round(self.position.shares_no, 2),
            final_net_inventory=round(self.position.net_inventory, 2),
            passed_payout_floor=net_rewards >= 1.00,
            gas_fees=round(epoch_gas_fees, 3),
            adverse_selection_60s=round(total_adverse_60s, 2),
            adverse_selection_300s=round(total_adverse_300s, 2),
            adverse_selection_3600s=round(total_adverse_3600s, 2),
        )
