#!/usr/bin/env python
"""Domain 2 Execution Regimes Backtester: Settlement Discount Harvester.

Simulates and rigorously compares three execution regimes across historical tape data:
- Regime A ("Ultra-Safe / Post-Proposal Only"): Enters only when t <= 2.0h before resolution
  (or post UMA proposal), price P in [0.975, 0.990], 10% max allocation per market.
- Regime B ("User Thesis: Risky & Diversified Hours Away"): Enters early at t in [2.0h, 12.0h]
  before resolution, price P in [0.92, 0.975] (capturing wider 3%-8% discounts), taking
  reversals, diversified across up to 20 concurrent markets with equal weighting (5% each).
- Regime C ("Dynamic Fractional Kelly"): Enters dynamically whenever Kelly fraction f* > 0,
  sizing each bet as f_i = min(0.10, 0.25 * f*_i), scaling size down as time-to-resolution
  increases and hazard rate rises.

Outputs comprehensive metrics:
- Total Net PnL (USDC)
- Realized APY / ARR
- Max Peak-to-Trough Drawdown ($ and %)
- Reversal count & total capital lost to reversals
- Capital turnover rate (turns per day)
- Realized Sharpe & Calmar Ratios

Adheres strictly to AGENTS.md:
- Uses DuckDB with SET threads=2.
- Logs via loguru through polymarketbot.logs.get_logger.
- Fully bounded, optimized query projections and execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Any

import duckdb
import numpy as np
import pandas as pd

from polymarketbot.logs import get_logger

logger = get_logger("backtest_domain2_regimes")


@dataclass
class Position:
    condition_id: str
    asset_id: str
    shares: float
    cost_basis: float
    entry_price: float
    res_ts_ms: float
    token_payout: float
    question: str


def load_tape_data(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load and join resolved conditions, universe map, and CLOB trades."""
    logger.info("Decoding CTF on-chain condition resolutions...")
    res_df = con.execute("""
        SELECT DISTINCT topic1 as condition_id, min(recv_ts_ns) as res_ts_ns, arbitrary(data) as raw_data
        FROM read_parquet(['data/derived/uma/res_nov5.parquet', 'data/derived/uma/res_nov6.parquet'])
        GROUP BY 1
    """).df()

    p0_list, p1_list, is_void_list = [], [], []
    for raw in res_df['raw_data']:
        b = bytes.fromhex(raw[2:])
        chunks = [int.from_bytes(b[i:i+32], 'big') for i in range(0, len(b), 32)]
        if len(chunks) >= 5:
            p0, p1 = chunks[3], chunks[4]
            p0_list.append(p0)
            p1_list.append(p1)
            is_void_list.append(p0 > 0 and p1 > 0)
        else:
            p0_list.append(None)
            p1_list.append(None)
            is_void_list.append(None)

    res_df['payout0'] = p0_list
    res_df['payout1'] = p1_list
    res_df['is_void'] = is_void_list
    con.register("decoded_resolutions", res_df)
    logger.info("Decoded {} on-chain resolved conditions.", len(res_df))

    logger.info("Joining CLOB trades with resolutions and market universe...")
    q = """
    WITH u AS (
        SELECT condition_id, u_question, tokens[1] as tok0, tokens[2] as tok1
        FROM read_parquet('data/derived/uma/universe_map.parquet')
    ),
    trades_joined AS (
        SELECT 
            t.condition_id,
            t.asset_id,
            t.price,
            t.size,
            t.side,
            t.venue_ts_ms,
            r.res_ts_ns,
            (r.res_ts_ns / 1e6 - t.venue_ts_ms) / 1000.0 / 3600.0 as hours_before_res,
            t.size * t.price as vol_usdc,
            u.u_question,
            CASE 
                WHEN t.asset_id = u.tok0 THEN 
                    CASE WHEN r.is_void THEN 0.5 WHEN r.payout0 > 0 THEN 1.0 ELSE 0.0 END
                WHEN t.asset_id = u.tok1 THEN 
                    CASE WHEN r.is_void THEN 0.5 WHEN r.payout1 > 0 THEN 1.0 ELSE 0.0 END
                ELSE NULL
            END as token_payout
        FROM read_parquet('data/tape/clob_trades/**/*.parquet') t
        JOIN decoded_resolutions r ON t.condition_id = r.condition_id
        JOIN u ON t.condition_id = u.condition_id
        WHERE (r.res_ts_ns / 1e6 - t.venue_ts_ms) >= 0 -- strictly pre-resolution
          AND t.price >= 0.85
    )
    SELECT * FROM trades_joined ORDER BY venue_ts_ms ASC;
    """
    df_trades = con.execute(q).df()
    logger.info("Loaded {} qualifying pre-resolution trades across {} conditions.", len(df_trades), df_trades['condition_id'].nunique())
    return df_trades


def run_regime_simulation(
    regime_name: str,
    trades_df: pd.DataFrame,
    initial_cash: float = 10000.0,
) -> tuple[dict[str, Any], pd.DataFrame, list[dict[str, Any]]]:
    """Execute event-driven portfolio simulation for a specific regime."""
    logger.info("Running simulation for '{}' with ${:,.2f} initial capital...", regime_name, initial_cash)
    cash = initial_cash
    open_positions: Dict[str, Position] = {}
    
    equity_curve: list[dict[str, Any]] = []
    trade_log: list[dict[str, Any]] = []
    reversal_log: list[dict[str, Any]] = []
    total_volume_invested = 0.0
    
    trades = trades_df.to_dict('records')
    t_start = trades[0]['venue_ts_ms']
    t_end = trades[-1]['venue_ts_ms']
    
    for t in trades:
        now_ms = t['venue_ts_ms']
        
        # 1. Settle any positions that reached on-chain resolution
        resolved_cids = [cid for cid, pos in open_positions.items() if pos.res_ts_ms <= now_ms]
        for cid in resolved_cids:
            pos = open_positions.pop(cid)
            payout = pos.token_payout
            proceeds = pos.shares * payout
            cash += proceeds
            if payout == 0.0:
                reversal_log.append({
                    'condition_id': cid,
                    'cost_basis': pos.cost_basis,
                    'shares': pos.shares,
                    'entry_price': pos.entry_price,
                    'question': pos.question,
                    'time_ms': pos.res_ts_ms
                })
        
        curr_equity = cash + sum(p.shares * p.entry_price for p in open_positions.values())
        
        cid = t['condition_id']
        price = t['price']
        h_before = t['hours_before_res']
        vol_usdc = t['vol_usdc']
        res_ts_ms = t['res_ts_ns'] / 1e6
        
        qualifies = False
        target_allocation_pct = 0.0
        
        if regime_name == "Regime A (Ultra-Safe / Post-Proposal Only)":
            # Enters only when t <= 2.0h before resolution (or post UMA proposal), price P in [0.975, 0.990], 10% max allocation per market
            if h_before <= 2.0 and 0.975 <= price <= 0.990:
                qualifies = True
                target_allocation_pct = 0.10

        elif regime_name == "Regime B (User Thesis: Risky & Diversified Hours Away)":
            # Enters early at t in [2.0h, 12.0h] before resolution, price P in [0.92, 0.975], diversified across up to 20 concurrent markets (5% each)
            if 2.0 < h_before <= 12.0 and 0.92 <= price <= 0.975:
                # Concurrent markets capacity constraint
                if cid in open_positions or len(open_positions) < 20:
                    qualifies = True
                    target_allocation_pct = 0.05

        elif regime_name == "Regime C (Dynamic Fractional Kelly)":
            # Dynamic Fractional Kelly sizing:
            # Calibrated hazard rate: near-zero dispute risk in final 2h, rising macro/jump hazard rate earlier
            if h_before <= 2.0:
                p_win = 0.996
            else:
                # Survival hazard decay: base rate 0.996 decaying with hazard function
                hazard_decay = np.exp(-0.015 * (h_before ** 1.15))
                p_win = 0.996 * hazard_decay

            if p_win > price:
                f_star = (p_win - price) / (1.0 - price)
                # Scale size down as time-to-resolution increases and hazard rate rises
                time_discount = 1.0 / (1.0 + 0.25 * h_before)
                f_quarter = 0.25 * f_star * time_discount
                target_allocation_pct = float(min(0.10, max(0.0, f_quarter)))
                if target_allocation_pct >= 0.005:  # Minimum 0.5% allocation
                    qualifies = True
        
        if qualifies and target_allocation_pct > 0:
            max_market_capital = curr_equity * target_allocation_pct
            current_allocated = open_positions[cid].cost_basis if cid in open_positions else 0.0
            available_market_capacity = max(0.0, max_market_capital - current_allocated)
            
            # Size fill against trade liquidity and available unallocated cash
            invest_amount = min(available_market_capacity, vol_usdc, cash)
            
            if invest_amount >= 1.0:
                cash -= invest_amount
                shares_bought = invest_amount / price
                total_volume_invested += invest_amount
                
                if cid in open_positions:
                    pos = open_positions[cid]
                    new_shares = pos.shares + shares_bought
                    new_cost = pos.cost_basis + invest_amount
                    pos.shares = new_shares
                    pos.cost_basis = new_cost
                    pos.entry_price = new_cost / new_shares
                else:
                    open_positions[cid] = Position(
                        condition_id=cid,
                        asset_id=t['asset_id'],
                        shares=shares_bought,
                        cost_basis=invest_amount,
                        entry_price=price,
                        res_ts_ms=res_ts_ms,
                        token_payout=t['token_payout'],
                        question=t['u_question']
                    )
                
                trade_log.append({
                    'time_ms': now_ms,
                    'condition_id': cid,
                    'price': price,
                    'invest_amount': invest_amount,
                    'hours_before_res': h_before,
                    'token_payout': t['token_payout']
                })
        
        now_equity = cash + sum(p.shares * p.entry_price for p in open_positions.values())
        equity_curve.append({
            'time_ms': now_ms,
            'equity': now_equity,
            'cash': cash,
            'open_markets': len(open_positions)
        })
        
    # Final settlement for any positions open at end of tape
    for cid, pos in list(open_positions.items()):
        payout = pos.token_payout
        proceeds = pos.shares * payout
        cash += proceeds
        if payout == 0.0:
            reversal_log.append({
                'condition_id': cid,
                'cost_basis': pos.cost_basis,
                'shares': pos.shares,
                'entry_price': pos.entry_price,
                'question': pos.question,
                'time_ms': pos.res_ts_ms
            })
    open_positions.clear()
    
    final_equity = cash
    net_pnl = final_equity - initial_cash
    duration_days = (t_end - t_start) / 1000.0 / 86400.0
    
    # Calculate performance metrics
    eq_df = pd.DataFrame(equity_curve)
    eq_df['hwm'] = eq_df['equity'].cummax()
    eq_df['drawdown'] = eq_df['equity'] - eq_df['hwm']
    eq_df['drawdown_pct'] = eq_df['drawdown'] / eq_df['hwm']
    
    max_dd_dollars = abs(eq_df['drawdown'].min())
    max_dd_pct = abs(eq_df['drawdown_pct'].min()) * 100.0
    
    total_return = net_pnl / initial_cash
    realized_arr = total_return * (365.0 / duration_days) * 100.0
    realized_apy = (((1.0 + total_return) ** (365.0 / duration_days)) - 1.0) * 100.0 if (1.0 + total_return) > 0 else -100.0
    capital_turnover = (total_volume_invested / initial_cash) / duration_days
    
    # Hourly Sharpe
    eq_df['dt'] = pd.to_datetime(eq_df['time_ms'], unit='ms')
    eq_df_indexed = eq_df.set_index('dt')
    hourly_eq = eq_df_indexed['equity'].resample('1h').last().ffill()
    hourly_ret = hourly_eq.pct_change().dropna()
    
    sharpe = float((hourly_ret.mean() / hourly_ret.std()) * np.sqrt(8760)) if len(hourly_ret) > 1 and hourly_ret.std() > 0 else 0.0
    calmar = float(realized_arr / max_dd_pct) if max_dd_pct > 0 else 0.0
    
    metrics = {
        'regime': regime_name,
        'initial_cash': initial_cash,
        'final_equity': round(final_equity, 2),
        'net_pnl': round(net_pnl, 2),
        'total_return_pct': round(total_return * 100.0, 2),
        'realized_arr_pct': round(realized_arr, 2),
        'realized_apy_pct': round(realized_apy, 2),
        'max_dd_dollars': round(max_dd_dollars, 2),
        'max_dd_pct': round(max_dd_pct, 2),
        'reversal_count': len(reversal_log),
        'capital_lost_reversals': round(sum(r['cost_basis'] for r in reversal_log), 2),
        'capital_turnover_turns_per_day': round(capital_turnover, 2),
        'realized_sharpe': round(sharpe, 2),
        'calmar_ratio': round(calmar, 2),
        'trades_executed': len(trade_log),
        'total_volume_invested': round(total_volume_invested, 2),
        'duration_days': round(duration_days, 2)
    }
    
    return metrics, eq_df, reversal_log


def main():
    logger.info("Initializing DuckDB with threads=2...")
    con = duckdb.connect()
    con.execute("SET threads=2;")

    # Load data
    trades_df = load_tape_data(con)

    regimes = [
        "Regime A (Ultra-Safe / Post-Proposal Only)",
        "Regime B (User Thesis: Risky & Diversified Hours Away)",
        "Regime C (Dynamic Fractional Kelly)",
    ]

    results = []
    reversals_all = {}
    curves = {}

    for reg in regimes:
        metrics, eq_df, revs = run_regime_simulation(reg, trades_df, initial_cash=10000.0)
        results.append(metrics)
        reversals_all[reg] = revs
        curves[reg] = eq_df[['time_ms', 'equity', 'cash', 'open_markets']].iloc[::50].to_dict('records')

    # Print summary table
    summary_df = pd.DataFrame(results)
    logger.info("\n" + "=" * 100 + "\nDOMAIN 2 EXECUTION REGIMES BACKTEST RESULTS (INITIAL CAPITAL: $10,000 USDC)\n" + "=" * 100)
    print("\n" + summary_df[['regime', 'net_pnl', 'realized_arr_pct', 'realized_apy_pct', 'max_dd_pct', 'reversal_count', 'capital_lost_reversals', 'capital_turnover_turns_per_day', 'realized_sharpe', 'calmar_ratio']].to_string(index=False))

    # Save to JSON
    out_dir = Path("data/derived/uma")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "domain2_regimes_backtest.json"

    export_payload = {
        "timestamp": pd.Timestamp.now().isoformat(),
        "summary": results,
        "reversals": reversals_all,
        "sample_curves": curves
    }

    with open(out_path, "w") as f:
        json.dump(export_payload, f, indent=2)
    logger.info("Saved backtest results artifact to {}", out_path)


if __name__ == "__main__":
    main()
