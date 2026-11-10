"""Dynamic Quote Sizing and Optimal Spread Optimizer for Polymarket CLOB.

Optimizes:
1. Dynamic Quote Sizing:
   - Derives safe maker quote size directly from initial cash bankroll.
   - Preserves 25% liquid reserve buffer for synthetic de-skew crossing and CTF gas fees.
   - Respects Polymarket rewards minimum size threshold (min_size) so quotes qualify for reward pools.
2. Market-Specific Optimal Spread Offset:
   - Jointly optimizes daily reward score yield S(v, s) = ((v - s)/v)^2 * size,
     expected spread capture 2s * size * fill_prob, and Kyle's lambda adverse selection risk.
   - Enforces $1.00 daily payout floor and post-only boundary constraints.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.rewards.allocator import calculate_endogenous_reserve

logger = get_logger("strategy.sizing")


def estimate_market_trade_frequency(
    volume_24h: float,
    mid_price: float,
    default_trade_size: float = 50.0,
) -> float:
    """Estimate daily trade count dynamically from market volume.

    Args:
        volume_24h: 24-hour traded volume in USD.
        mid_price: Current mid-price of the token (bounded in [0.01, 0.99]).
        default_trade_size: Expected average trade size in shares (default 50.0).

    Returns:
        Estimated daily trade count (float, >= 0.0).
    """
    if volume_24h <= 0.0:
        return 0.0

    p = max(0.01, min(0.99, float(mid_price)))
    sz = max(1.0, float(default_trade_size))
    avg_trade_notional = sz * p
    if avg_trade_notional <= 0.0:
        return 0.0

    daily_trades = float(volume_24h) / avg_trade_notional
    return round(daily_trades, 2)


def calculate_dynamic_cycles_budget(
    alloc_capital: float,
    quote_size: float,
    price: float,
) -> float:
    """Scale cycles budget dynamically based on allocated capital, quote size, and price.

    Formula:
        cycles_budget = max(1.5, min(5.0, alloc_capital / (2.0 * quote_size * price)))

    Args:
        alloc_capital: Available or allocated capital in USD.
        quote_size: Maker quote size in shares.
        price: Price of the asset (bounded in [0.01, 0.99]).

    Returns:
        Calibrated cycles budget bounded in [1.5, 5.0].
    """
    if alloc_capital <= 0.0:
        return 1.5

    p = max(0.01, min(0.99, float(price)))
    qs = max(1.0, float(quote_size))
    denom = 2.0 * qs * p
    if denom <= 0.0:
        return 1.5

    raw_budget = float(alloc_capital) / denom
    return float(max(1.5, min(5.0, raw_budget)))


calibrate_cycles_budget = calculate_dynamic_cycles_budget


def derive_quote_size(
    initial_cash: float,
    mid_price: float,
    min_size: float = 0.0,
    reserve_pct: Optional[float] = None,
    cycles_budget: Optional[float] = None,
    allocations: Optional[list[dict[str, Any]]] = None,
    base_gas_buffer_usd: Optional[float] = None,
    quote_size: Optional[float] = None,
    default_min_size: float = 5.0,
) -> float:
    """Derive maker quote size dynamically from bankroll and price.

    Args:
        initial_cash: Total initial bankroll in USD (e.g. $150.00).
        mid_price: Current mid-price of the primary token (e.g. 0.74).
        min_size: Polymarket rewards minimum size requirement (e.g. 20.0 or 50.0).
        reserve_pct: Fraction of capital held liquid for de-skewing and gas.
            If None, dynamically computed using calculate_endogenous_reserve.
        cycles_budget: Number of unhedged fill cycles to withstand without cashout.
            If None, calibrated dynamically via calculate_dynamic_cycles_budget.
        allocations: Optional list of market allocation dicts for single or multi-market
            endogenous reserve calculation.
        base_gas_buffer_usd: Minimum gas buffer in USD. If None, dynamically computed
            by calculate_endogenous_reserve from live gas price and POL exchange rate.
        quote_size: Optional target quote size in shares used for dynamic cycles budget calibration.
        default_min_size: Minimum quote size floor in shares when min_size is not specified (default 5.0).

    Returns:
        Safe quote size in shares (float, 1 decimal place).
    """
    if initial_cash <= 0.0:
        return min_size if min_size > 0.0 else default_min_size

    p_yes = max(0.01, min(0.99, mid_price))
    p_no = 1.0 - p_yes
    worst_side_price = max(p_yes, p_no)

    # Available capital after liquid buffer reserve
    if reserve_pct is None:
        alloc_list = allocations if allocations is not None else [{"capital": initial_cash, "mid": mid_price}]
        reserve_usd = calculate_endogenous_reserve(
            allocations=alloc_list,
            bankroll_usd=initial_cash,
            base_gas_buffer_usd=base_gas_buffer_usd,
        )
        if len(alloc_list) > 1:
            reserve_ratio = min(0.90, reserve_usd / max(1.0, initial_cash))
            avail_cash = max(0.0, initial_cash * (1.0 - reserve_ratio))
        else:
            avail_cash = max(0.0, initial_cash - reserve_usd)
    else:
        avail_cash = initial_cash * max(0.10, 1.0 - reserve_pct)

    # Collateral for 2-sided quoting ~ $1.00 per share set
    # Dividing by cycles_budget prevents depleting bankroll on first asymmetric fill
    if cycles_budget is None:
        target_size = quote_size if quote_size is not None else (min_size if min_size > 0.0 else 50.0)
        cycles_budget = calculate_dynamic_cycles_budget(
            alloc_capital=avail_cash,
            quote_size=target_size,
            price=worst_side_price,
        )
    else:
        cycles_budget = max(1.0, float(cycles_budget))

    budget_size = avail_cash / max(1.0, cycles_budget)

    floor_size = min_size if min_size > 0.0 else default_min_size

    if min_size > 0.0:
        worst_side_cost = min_size * worst_side_price
        # If available cash comfortably covers min_size
        if avail_cash >= worst_side_cost:
            return round(max(min_size, budget_size), 1)
        else:
            # Bankroll is constrained: scale down to maximum safe affordable size
            affordable = avail_cash / worst_side_price
            return round(max(1.0, affordable), 1)

    return round(max(floor_size, budget_size), 1)


def optimize_spread_offset(
    max_spread_cents: float,
    market_spread_cents: float,
    tick_size_cents: float = 1.0,
    daily_reward_pool: float = 25.0,
    quote_size: float = 20.0,
    competing_depth: float = 1500.0,
    category: str = "default",
    daily_trades_estimate: Optional[float] = None,
    volume_24h: Optional[float] = None,
    mid_price: float = 0.50,
    maker_participation_rate: float = 0.15,
) -> Tuple[float, Dict[str, Any]]:
    """Calculate the mathematically optimal half-spread offset for a target market.

    Maximizes Expected Net Utility:
        U(s) = Expected_Reward_Payout(s) + Expected_Spread_Capture(s) - Expected_Adverse_Loss(s)

    Args:
        max_spread_cents: Qualifying spread threshold (v) in cents.
        market_spread_cents: Current top-of-book market spread in cents.
        tick_size_cents: Minimum price tick in cents (e.g. 1.0c or 0.1c).
        daily_reward_pool: Daily reward budget in USD for this market.
        quote_size: Order size in shares.
        competing_depth: Estimated competing qualifying score/depth.
        category: Market classification ('politics', 'crypto', 'sports', 'default').
        daily_trades_estimate: Expected total market trade count per day.
            If None, dynamically estimated from volume_24h / average trade notional.
        volume_24h: Optional 24-hour market volume in USD.
        mid_price: Current mid-price of the token (default 0.50).
        maker_participation_rate: Estimated fraction of market fills captured by maker quote (default 0.15).

    Returns:
        (optimal_spread_offset_cents, optimization_telemetry_dict)
    """
    v = max(0.5, float(max_spread_cents))
    mkt_s = max(0.1, float(market_spread_cents))
    half_mkt = mkt_s / 2.0
    tick = max(0.01, float(tick_size_cents))

    # Dynamic trade frequency estimation from market metrics
    if daily_trades_estimate is not None:
        trades_est = max(1.0, float(daily_trades_estimate))
    else:
        # Dynamic volume estimation: if volume_24h not explicitly supplied,
        # estimate turnover from qualifying depth and reward pool budget
        eff_vol = (
            float(volume_24h)
            if volume_24h is not None
            else max(100.0, competing_depth * mid_price * 4.0 if competing_depth > 0 else daily_reward_pool * 120.0)
        )
        trades_est = estimate_market_trade_frequency(
            volume_24h=eff_vol,
            mid_price=mid_price,
            default_trade_size=quote_size if quote_size > 0 else 50.0,
        )
        trades_est = max(1.0, trades_est)

    # Candidate spread offsets on half-tick granularity
    step = max(0.1, tick / 2.0)
    candidates: list[float] = []
    curr = step
    upper_bound = min(v * 0.95, max(mkt_s * 1.5, 3.5))

    while curr <= upper_bound:
        candidates.append(round(curr, 2))
        curr += step

    if not candidates:
        candidates = [round(max(0.2, min(half_mkt, v * 0.8)), 2)]

    # Category volatility parameters
    cat_sigma = {
        "politics": 0.08,
        "geopolitics": 0.10,
        "crypto": 0.18,
        "sports": 0.05,
        "default": 0.09,
    }.get(category.lower(), 0.09)

    best_s = candidates[0]
    best_score = -float("inf")
    metrics_map: Dict[float, Dict[str, Any]] = {}

    for s in candidates:
        # 1. Quadratic Maker Reward Score
        ratio = max(0.0, (v - s) / v)
        maker_score = (ratio**2) * quote_size

        # 2. Daily Reward Share & $1.00 Floor Enforcement
        total_score = maker_score + max(50.0, competing_depth)
        gross_payout = daily_reward_pool * (maker_score / total_score)
        eff_payout = gross_payout if gross_payout >= 1.00 else 0.0

        # 3. Fill Intensity & Effective Spread Capture
        if s < half_mkt - 1e-4:
            # Inside the market spread (tightest quote, high arrival)
            fill_prob = 1.0
            eff_spread = 2.0 * s
        elif abs(s - half_mkt) <= 1e-4:
            # At top of book (competes in FIFO queue)
            fill_prob = 0.70
            eff_spread = mkt_s
        else:
            # Deep in the book (fills only on sweeps)
            dist_beyond = s - half_mkt
            fill_prob = 0.70 * math.exp(-1.5 * dist_beyond)
            eff_spread = 2.0 * s

        # Daily Spread Capture USD
        daily_spread_usd = (eff_spread / 100.0) * quote_size * fill_prob * (trades_est * maker_participation_rate)

        # 4. Kyle's Adverse Selection Markout Risk
        # Tighter quotes face higher adverse selection from informed takers
        adverse_cents = (0.25 * cat_sigma / max(0.2, s)) * 100.0
        daily_adverse_usd = (adverse_cents / 100.0) * quote_size * fill_prob * (trades_est * maker_participation_rate)

        # Net Utility
        net_utility = eff_payout + daily_spread_usd - daily_adverse_usd

        metrics_map[s] = {
            "s_cents": s,
            "maker_score": round(maker_score, 4),
            "payout_daily": round(eff_payout, 3),
            "spread_capture": round(daily_spread_usd, 3),
            "adverse_loss": round(daily_adverse_usd, 3),
            "net_utility": round(net_utility, 3),
            "fill_prob": round(fill_prob, 3),
            "daily_trades_estimate": round(trades_est, 1),
        }

        if net_utility > best_score:
            best_score = net_utility
            best_s = s

    optimal_metrics = metrics_map[best_s]
    logger.info(
        f"Optimized spread offset for market: s*={best_s:.2f}c (Max Spread={v:.1f}c, Mkt Spread={mkt_s:.1f}c, "
        f"Est Trades={trades_est:.1f}/d) -> Net Utility=${optimal_metrics['net_utility']:.2f}/d, "
        f"Proj Rewards=${optimal_metrics['payout_daily']:.2f}/d"
    )
    return best_s, optimal_metrics
