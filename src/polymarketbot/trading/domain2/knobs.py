"""Configurable Risk & Execution Knobs with Meta-Optimizer for Domain 2.

Exposes knobs:
1. Entry Horizon: [min_hours_to_expiry, max_hours_to_expiry]
2. Pricing Band: [min_price, max_price, min_net_edge_bps]
3. Diversification: [max_single_bet_pct, max_category_pct, max_active_markets, cash_reserve_pct]
4. Risk Bounds: [max_drawdown_target_pct, variance_penalty_lambda, objective_metric]
5. Liquidity: [depth_consumption_gamma, min_order_usd]

Optimizes for:
- Realized Annualized Risk-Adjusted Return (ARR)
- Calmar Ratio: ARR / MaxDrawdown
- Sharpe Ratio: Annualized Return / Annualized Volatility
- Sortino Ratio: Annualized Return / Downside Semi-Deviation
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import numpy as np
import pandas as pd

from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.robust_hazard import RobustHazardModel

log = get_logger("domain2_knobs")

ObjectiveMetric = Literal["calmar", "sharpe", "sortino", "risk_adjusted_arr", "arr"]


@dataclass
class Domain2Knobs:
    """Configurable hyperparameter knobs for Domain 2 Settlement Basis Harvester."""

    # 1. Entry Timing Knobs
    min_hours_to_expiry: float = 0.05       # 3 minutes buffer before cutoff
    max_hours_to_expiry: float = 2.0        # Max lead time (e.g. 0.5h, 2h, 6h, 12h)

    # 2. Pricing & Edge Knobs
    min_price: float = 0.970                # Minimum contract price (e.g. 0.92, 0.95, 0.975)
    max_price: float = 0.992                # Maximum contract price (e.g. 0.990, 0.995)
    min_net_edge_bps: float = 30.0          # Minimum net edge hurdle in bps (e.g. 0.30%)

    # 3. Diversification Knobs
    max_single_bet_pct: float = 0.025       # Max single market allocation (2.5% of bankroll)
    max_category_pct: float = 0.150         # Max correlated category allocation (15% of bankroll)
    max_active_markets: int = 15            # Maximum concurrent active positions
    cash_reserve_pct: float = 0.25          # Cash buffer retained (25% unallocated)

    # 4. Risk & Objective Knobs
    max_drawdown_target_pct: float = 0.10   # Maximum tolerated drawdown ceiling (10%)
    variance_penalty_lambda: float = 3.0    # Downside jump penalty multiplier
    objective_metric: ObjectiveMetric = "calmar"

    # 5. Liquidity & Execution Knobs
    depth_consumption_gamma: float = 0.25   # Consume at most 25% of top-of-book depth
    min_order_usd: float = 20.0             # Minimum order size to avoid dust

    def validate(self) -> None:
        """Validate knob parameter consistency."""
        if not (0.0 < self.min_hours_to_expiry < self.max_hours_to_expiry):
            raise ValueError(f"Invalid hours range: {self.min_hours_to_expiry} >= {self.max_hours_to_expiry}")
        if not (0.50 <= self.min_price < self.max_price <= 1.0):
            raise ValueError(f"Invalid price band: {self.min_price} >= {self.max_price}")
        if not (0.0 < self.max_single_bet_pct <= self.max_category_pct <= 1.0):
            raise ValueError(f"Invalid sizing hierarchy: bet {self.max_single_bet_pct} > cat {self.max_category_pct}")
        if not (0.0 <= self.cash_reserve_pct < 1.0):
            raise ValueError(f"Invalid reserve pct: {self.cash_reserve_pct}")
        if self.max_active_markets < 1:
            raise ValueError(f"Invalid max active markets: {self.max_active_markets}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Domain2Knobs:
        valid_keys = set(cls.__dataclass_fields__.keys())  # type: ignore
        clean = {k: v for k, v in d.items() if k in valid_keys}
        obj = cls(**clean)
        obj.validate()
        return obj


@dataclass
class KnobEvaluationResult:
    """Performance evaluation of a specific knob setting."""

    knobs: Domain2Knobs
    net_pnl_usd: float
    total_return_pct: float
    realized_arr_pct: float
    realized_apy_pct: float
    max_drawdown_pct: float
    max_drawdown_usd: float
    reversals_count: int
    capital_lost_reversals: float
    capital_turnover_per_day: float
    trades_executed: int
    sharpe_ratio: float
    calmar_ratio: float
    sortino_ratio: float
    objective_score: float
    is_feasible: bool = True
    constraint_violation: str = ""


class Domain2KnobOptimizer:
    """Meta-Optimizer tuning Domain 2 knobs to maximize annual risk-adjusted returns."""

    def __init__(
        self,
        bankroll_usd: float = 10000.0,
        objective_metric: ObjectiveMetric = "calmar",
        max_drawdown_ceiling: float = 0.10,
    ) -> None:
        self.bankroll_usd = float(bankroll_usd)
        self.objective_metric = objective_metric
        self.max_drawdown_ceiling = float(max_drawdown_ceiling)

    def evaluate_knobs(
        self,
        knobs: Domain2Knobs,
        trades_df: pd.DataFrame,
    ) -> KnobEvaluationResult:
        """Simulate execution on historical tape trades under the specified knob configuration."""
        knobs.validate()
        initial_cash = self.bankroll_usd
        cash = initial_cash
        open_positions: Dict[str, Dict[str, Any]] = {}
        category_exposure: Dict[str, float] = {}

        trades = trades_df.to_dict("records")
        if not trades:
            return KnobEvaluationResult(
                knobs=knobs,
                net_pnl_usd=0.0,
                total_return_pct=0.0,
                realized_arr_pct=0.0,
                realized_apy_pct=0.0,
                max_drawdown_pct=0.0,
                max_drawdown_usd=0.0,
                reversals_count=0,
                capital_lost_reversals=0.0,
                capital_turnover_per_day=0.0,
                trades_executed=0,
                sharpe_ratio=0.0,
                calmar_ratio=0.0,
                sortino_ratio=0.0,
                objective_score=-999.0,
                is_feasible=False,
                constraint_violation="Empty trades dataset",
            )

        t_start = trades[0]["venue_ts_ms"]
        t_end = trades[-1]["venue_ts_ms"]
        duration_days = max(0.1, (t_end - t_start) / 1000.0 / 86400.0)

        peak_equity = initial_cash
        max_dd_usd = 0.0
        max_dd_pct = 0.0
        reversals_count = 0
        capital_lost_reversals = 0.0
        total_vol_invested = 0.0
        trades_executed = 0

        returns_series: List[float] = []
        downside_returns: List[float] = []

        usable_cash_budget = initial_cash * (1.0 - knobs.cash_reserve_pct)
        max_single_bet = initial_cash * knobs.max_single_bet_pct
        max_cat_exposure = initial_cash * knobs.max_category_pct

        hazard_model = RobustHazardModel()

        for t in trades:
            now_ms = t["venue_ts_ms"]

            # 1. Settle resolved positions
            resolved_cids = [cid for cid, pos in open_positions.items() if pos["res_ts_ms"] <= now_ms]
            for cid in resolved_cids:
                pos = open_positions.pop(cid)
                cat = pos["category"]
                category_exposure[cat] = max(0.0, category_exposure.get(cat, 0.0) - pos["cost_basis"])

                payout = pos["token_payout"]
                proceeds = pos["shares"] * payout
                cash += proceeds

                trade_return = (proceeds - pos["cost_basis"]) / pos["cost_basis"]
                returns_series.append(trade_return)
                if trade_return < 0:
                    downside_returns.append(trade_return)

                if payout == 0.0:
                    reversals_count += 1
                    capital_lost_reversals += pos["cost_basis"]

            # 2. Track equity & drawdown
            open_cost = sum(p["cost_basis"] for p in open_positions.values())
            curr_equity = cash + sum(p["shares"] * p["entry_price"] for p in open_positions.values())

            if curr_equity > peak_equity:
                peak_equity = curr_equity
            dd_usd = peak_equity - curr_equity
            dd_pct = (dd_usd / peak_equity) if peak_equity > 0 else 0.0
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

            # 3. Filter candidate by knobs
            h_before = t["hours_before_res"]
            price = t["price"]
            cid = t["condition_id"]
            cat = t.get("category", "other")
            vol_usdc = t.get("vol_usdc", 1000.0)
            res_ts_ms = t["res_ts_ns"] / 1e6

            # Entry Timing Knob Check
            if not (knobs.min_hours_to_expiry <= h_before <= knobs.max_hours_to_expiry):
                continue
            # Price Band Knob Check
            if not (knobs.min_price <= price <= knobs.max_price):
                continue

            # Net Edge Hurdle evaluated dynamically via institutional RobustHazardModel
            est_p_win, hazard, _ = hazard_model.evaluate_candidate(
                category=cat,
                price=price,
                hours_to_resolution=max(0.01, h_before),
                is_uma_proposed=(h_before <= 2.0),
            )
            net_edge_bps = ((est_p_win - price) / price) * 10000.0
            if net_edge_bps < knobs.min_net_edge_bps:
                continue

            # Risk & Diversification Knobs Check
            if len(open_positions) >= knobs.max_active_markets and cid not in open_positions:
                continue
            if open_cost >= usable_cash_budget:
                continue

            current_cat_exp = category_exposure.get(cat, 0.0)
            if current_cat_exp >= max_cat_exposure:
                continue

            # Sizing: respecting liquidity depth gamma, single bet cap, and available cash
            avail_market_cap = max(0.0, max_single_bet - open_positions.get(cid, {}).get("cost_basis", 0.0))
            avail_cat_cap = max(0.0, max_cat_exposure - current_cat_exp)
            liquidity_limit = vol_usdc * knobs.depth_consumption_gamma
            allocated_usd = min(avail_market_cap, avail_cat_cap, liquidity_limit, cash, usable_cash_budget - open_cost)

            if allocated_usd < knobs.min_order_usd:
                continue

            # Execute trade
            shares = allocated_usd / price
            cash -= allocated_usd
            total_vol_invested += allocated_usd
            trades_executed += 1
            category_exposure[cat] = current_cat_exp + allocated_usd

            if cid in open_positions:
                open_positions[cid]["shares"] += shares
                open_positions[cid]["cost_basis"] += allocated_usd
            else:
                open_positions[cid] = {
                    "shares": shares,
                    "cost_basis": allocated_usd,
                    "entry_price": price,
                    "res_ts_ms": res_ts_ms,
                    "category": cat,
                    "token_payout": t.get("token_payout", 1.0),
                }

        # Final settle
        for cid, pos in open_positions.items():
            payout = pos["token_payout"]
            proceeds = pos["shares"] * payout
            cash += proceeds
            if payout == 0.0:
                reversals_count += 1
                capital_lost_reversals += pos["cost_basis"]

        final_equity = cash
        net_pnl = final_equity - initial_cash
        total_return_pct = (net_pnl / initial_cash) * 100.0

        # Annualization: (1 + r)^(365 / days) - 1
        realized_arr_pct = (total_return_pct / duration_days) * 365.0
        try:
            realized_apy_pct = ((final_equity / initial_cash) ** (365.0 / duration_days) - 1.0) * 100.0
        except OverflowError:
            realized_apy_pct = 999999.0

        turnover = (total_vol_invested / initial_cash) / duration_days

        # Risk-adjusted metrics
        if returns_series and len(returns_series) > 1:
            mean_ret = np.mean(returns_series)
            std_ret = np.std(returns_series) + 1e-8
            # Annualized trades per year
            trades_per_year = (len(returns_series) / duration_days) * 365.0
            sharpe = float((mean_ret / std_ret) * np.sqrt(trades_per_year))
            downside_std = np.std(downside_returns) + 1e-8 if downside_returns else 0.001
            sortino = float((mean_ret / downside_std) * np.sqrt(trades_per_year))
        else:
            sharpe = 0.0
            sortino = 0.0

        calmar = (realized_arr_pct / (max_dd_pct * 100.0)) if max_dd_pct > 0.001 else realized_arr_pct

        # Check constraint feasibility (Hard Drawdown Ceiling)
        is_feasible = True
        violation = ""
        if max_dd_pct > knobs.max_drawdown_target_pct:
            is_feasible = False
            violation = f"Max drawdown ({max_dd_pct*100:.2f}%) exceeded target ({knobs.max_drawdown_target_pct*100:.2f}%)"

        # Calculate Objective Score
        if not is_feasible:
            # Harsh penalty for violating drawdown constraint
            objective_score = -1000.0 * (max_dd_pct / knobs.max_drawdown_target_pct)
        else:
            if self.objective_metric == "calmar":
                objective_score = calmar
            elif self.objective_metric == "sharpe":
                objective_score = sharpe
            elif self.objective_metric == "sortino":
                objective_score = sortino
            elif self.objective_metric == "risk_adjusted_arr":
                # ARR penalized quadratically by drawdown
                objective_score = realized_arr_pct - knobs.variance_penalty_lambda * ((max_dd_pct * 100.0) ** 2)
            else:
                objective_score = realized_arr_pct

        return KnobEvaluationResult(
            knobs=knobs,
            net_pnl_usd=round(net_pnl, 2),
            total_return_pct=round(total_return_pct, 2),
            realized_arr_pct=round(realized_arr_pct, 2),
            realized_apy_pct=round(realized_apy_pct, 2),
            max_drawdown_pct=round(max_dd_pct * 100.0, 2),
            max_drawdown_usd=round(max_dd_usd, 2),
            reversals_count=reversals_count,
            capital_lost_reversals=round(capital_lost_reversals, 2),
            capital_turnover_per_day=round(turnover, 2),
            trades_executed=trades_executed,
            sharpe_ratio=round(sharpe, 2),
            calmar_ratio=round(calmar, 2),
            sortino_ratio=round(sortino, 2),
            objective_score=round(objective_score, 2),
            is_feasible=is_feasible,
            constraint_violation=violation,
        )

    def grid_search_tune(
        self,
        trades_df: pd.DataFrame,
        hours_grid: List[float] = [0.5, 1.0, 1.5, 2.0, 3.0, 6.0, 10.0],
        single_bet_grid: List[float] = [0.01, 0.02, 0.025, 0.04, 0.05],
        category_cap_grid: List[float] = [0.08, 0.12, 0.15, 0.25],
        min_price_grid: List[float] = [0.93, 0.96, 0.975],
    ) -> Tuple[KnobEvaluationResult, List[KnobEvaluationResult]]:
        """Run parameter sweep to locate the global optimal knob settings."""
        log.info(
            "Starting Domain 2 Knob Meta-Optimization sweep across {} parameter combinations...",
            len(hours_grid) * len(single_bet_grid) * len(category_cap_grid) * len(min_price_grid),
        )

        all_results: List[KnobEvaluationResult] = []
        best_result: Optional[KnobEvaluationResult] = None
        best_score = -float("inf")

        for h_max in hours_grid:
            for f_bet in single_bet_grid:
                for f_cat in category_cap_grid:
                    if f_bet > f_cat:
                        continue
                    for p_min in min_price_grid:
                        knobs = Domain2Knobs(
                            max_hours_to_expiry=h_max,
                            max_single_bet_pct=f_bet,
                            max_category_pct=f_cat,
                            min_price=p_min,
                            max_drawdown_target_pct=self.max_drawdown_ceiling,
                            objective_metric=self.objective_metric,
                        )

                        res = self.evaluate_knobs(knobs, trades_df)
                        all_results.append(res)

                        if res.is_feasible and res.objective_score > best_score:
                            best_score = res.objective_score
                            best_result = res

        if best_result is None and all_results:
            # Fallback to least unfeasible
            best_result = max(all_results, key=lambda r: r.objective_score)

        log.info(
            "Optimization sweep completed! Best knobs: MaxHours={:.1f}h, SingleBet={:.1f}%, CatCap={:.1f}%, PriceMin={:.3f} | ARR={:+.2f}%, DD={:.2f}%, Calmar={:.2f}, Sharpe={:.2f}",
            best_result.knobs.max_hours_to_expiry,
            best_result.knobs.max_single_bet_pct * 100.0,
            best_result.knobs.max_category_pct * 100.0,
            best_result.knobs.min_price,
            best_result.realized_arr_pct,
            best_result.max_drawdown_pct,
            best_result.calmar_ratio,
            best_result.sharpe_ratio,
        )

        return best_result, all_results
