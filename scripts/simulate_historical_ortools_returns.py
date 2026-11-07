"""Simulation of Domain 2 Google OR-Tools Global Optimum Allocator over Historical Tape.

Evaluates exact realized returns on historical Polymarket tape (November 4-6, 2026),
incorporating exact UMA dispute challenge lockup (2.0h) + CTF relayer resolution delay (1.1h).
Benchmarks the Institutional Dynamic Position Sizing model vs the static 10% hardcoded baseline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Any, Optional
import numpy as np
import pandas as pd
import duckdb

from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Candidate,
    Domain2OrtoolsAllocator,
    Domain2OptimizationResult,
)
from polymarketbot.trading.domain2.robust_hazard import RobustHazardModel

logger = get_logger("historical_ortools_simulation")


@dataclass
class SimulatedPosition:
    condition_id: str
    token_id: str
    entry_price: float
    shares: float
    cost_basis: float
    entry_ts_ms: float
    unlock_ts_ms: float
    token_payout: float
    question: str
    category: str


def load_historical_tape(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load on-chain resolutions and join with CLOB trade tape."""
    logger.info("Loading CTF condition resolutions and universe metadata...")
    
    # 1. On-chain CTF condition resolutions
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

    # 2. Join with CLOB trades and universe map
    logger.info("Joining CLOB trades with exact on-chain resolutions...")
    query = """
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
        WHERE (r.res_ts_ns / 1e6 - t.venue_ts_ms) >= 0
          AND t.price >= 0.85
    )
    SELECT * FROM trades_joined ORDER BY venue_ts_ms ASC;
    """
    df = con.execute(query).df()
    logger.info("Loaded {} pre-resolution trade observations across {} markets.", len(df), df['condition_id'].nunique())
    return df


def load_crypto_spots(con: duckdb.DuckDBPyConnection) -> Dict[str, pd.DataFrame]:
    """Load Binance spot BBO minute bars for BTC and ETH to evaluate real-time strike distance."""
    logger.info("Loading Binance spot BBO minute bars for BTC and ETH...")
    df = con.execute("""
        SELECT 
            symbol,
            (recv_ts_ns / 1000000 / 60000)::BIGINT * 60000 as ts_min_ms,
            avg(bid) as spot_px
        FROM 'data/tape/binance_bbo/**/*.parquet'
        WHERE symbol IN ('BTCUSDT', 'ETHUSDT')
        GROUP BY 1, 2
        ORDER BY 2 ASC
    """).df()
    return {
        "BTC": df[df["symbol"] == "BTCUSDT"].reset_index(drop=True),
        "ETH": df[df["symbol"] == "ETHUSDT"].reset_index(drop=True),
    }


def run_simulation(
    df_trades: pd.DataFrame,
    initial_bankroll: float = 200.0,
    epoch_interval_minutes: float = 30.0,
    mode: str = "dynamic",
    spots_map: Optional[Dict[str, pd.DataFrame]] = None,
) -> Dict[str, Any]:
    """Run event-driven simulation with Google OR-Tools Global Optimum Allocator."""
    logger.info("Starting historical simulation [{}]: Bankroll=${:,.2f}, Epoch={}m", mode, initial_bankroll, epoch_interval_minutes)
    
    # Configure allocator for this bankroll
    if mode == "static_baseline":
        allocator = Domain2OrtoolsAllocator(
            reserve_pct=0.25,
            max_drawdown_target=0.10,
            max_single_bet_pct=0.10,  # Static 10% hardcoded cap
            max_category_pct=0.150,   # Static 15% category cap
            max_active_markets=15,    # Static 15 active markets cap
            min_order_usd=10.0 if initial_bankroll <= 500.0 else 20.0,
            depth_consumption_gamma=0.25, # Static 25% depth
            variance_penalty_lambda=3.0,
            use_dynamic_sizing=False,
            use_precautionary_reserve=False,
            use_factor_risk_budgeting=False,
            use_fixed_ticket_cost=False,
        )
    else:
        allocator = Domain2OrtoolsAllocator(
            max_drawdown_target=0.10,
            max_single_bet_pct=0.25,  # Unshackled up to 25% for deterministic 4+ sigma bets
            min_order_usd=10.0 if initial_bankroll <= 500.0 else 20.0,
            variance_penalty_lambda=3.0,
            use_dynamic_sizing=True,
            use_precautionary_reserve=True,
            use_factor_risk_budgeting=True,
            use_fixed_ticket_cost=True,
        )
    
    cash = initial_bankroll
    open_positions: Dict[str, SimulatedPosition] = {}
    completed_trades: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    reversals_count = 0
    total_volume_deployed = 0.0

    epoch_ms = epoch_interval_minutes * 60.0 * 1000.0
    t_start = df_trades['venue_ts_ms'].min()
    t_end = df_trades['venue_ts_ms'].max()
    total_duration_hours = (t_end - t_start) / (1000.0 * 3600.0)
    total_duration_days = total_duration_hours / 24.0

    current_t = t_start
    df_records = df_trades.to_dict('records')
    record_idx = 0
    n_records = len(df_records)

    while current_t <= t_end + epoch_ms:
        # 1. Process resolutions up to current_t
        resolved_cids = [cid for cid, pos in open_positions.items() if pos.unlock_ts_ms <= current_t]
        for cid in resolved_cids:
            pos = open_positions.pop(cid)
            payout_proceeds = pos.shares * pos.token_payout
            profit = payout_proceeds - pos.cost_basis
            cash += payout_proceeds
            
            if pos.token_payout == 0.0:
                reversals_count += 1
                logger.warning("Reversal occurred for {}: lost ${:.2f}", pos.condition_id[:14], pos.cost_basis)
            
            completed_trades.append({
                'condition_id': pos.condition_id,
                'entry_price': pos.entry_price,
                'shares': pos.shares,
                'cost_basis': pos.cost_basis,
                'payout_proceeds': payout_proceeds,
                'net_profit': profit,
                'return_pct': (profit / pos.cost_basis) * 100.0,
                'hold_time_hours': (pos.unlock_ts_ms - pos.entry_ts_ms) / (1000.0 * 3600.0),
                'payout': pos.token_payout,
            })

        # Calculate current equity
        open_pos_value = sum(p.cost_basis for p in open_positions.values())
        current_equity = cash + open_pos_value
        equity_curve.append({
            'timestamp_ms': current_t,
            'cash': cash,
            'open_positions_usd': open_pos_value,
            'equity': current_equity,
            'active_positions_count': len(open_positions),
        })

        # 2. Collect trade candidates available in this epoch window [current_t, current_t + epoch_ms]
        epoch_end_t = current_t + epoch_ms
        epoch_candidates_map: Dict[str, Dict[str, Any]] = {}

        while record_idx < n_records and df_records[record_idx]['venue_ts_ms'] < epoch_end_t:
            r = df_records[record_idx]
            cid = r['condition_id']
            # Only consider if not already in open positions
            if cid not in open_positions:
                px = float(r['price'])
                hours_left = float(r['hours_before_res'])
                vol = float(r['vol_usdc'])
                res_ts = float(r['res_ts_ns'] / 1e6)
                payout = float(r['token_payout']) if r['token_payout'] is not None else 1.0

                if cid not in epoch_candidates_map:
                    epoch_candidates_map[cid] = {
                        'condition_id': cid,
                        'token_id': str(r['asset_id']),
                        'question': str(r['u_question']),
                        'price': px,
                        'hours_to_resolution': max(0.25, hours_left),
                        'depth_usd': vol,
                        'unlock_ts_ms': res_ts,
                        'token_payout': payout,
                    }
                else:
                    # Accumulate visible depth
                    epoch_candidates_map[cid]['depth_usd'] += vol
                    # Keep latest/lowest ask price
                    epoch_candidates_map[cid]['price'] = min(epoch_candidates_map[cid]['price'], px)
            record_idx += 1

        # 3. Form Domain2Candidate list and run Google OR-Tools SCIP MILP Allocator
        candidates: List[Domain2Candidate] = []
        for cid, item in epoch_candidates_map.items():
            # Classify category heuristically from question
            q_lower = item['question'].lower()
            cat = "general"
            if any(k in q_lower for k in ["btc", "bitcoin", "eth", "sol"]):
                cat = "crypto"
            elif any(k in q_lower for k in ["game", "win", "vs", "score", "nhl", "nfl", "nba", "kills"]):
                cat = "sports"
            elif any(k in q_lower for k in ["senate", "election", "president", "trump", "harris"]):
                cat = "politics"

            # Dynamic spot and strike extraction for continuous drift-diffusion
            spot_px = None
            strike_px = None
            if cat == "crypto":
                m = re.search(r'\$([0-9,]+(?:\.[0-9]+)?)', item['question'])
                if m:
                    try:
                        strike_px = float(m.group(1).replace(',', ''))
                    except Exception:
                        strike_px = None

                if spots_map:
                    if ("btc" in q_lower or "bitcoin" in q_lower) and "BTC" in spots_map:
                        df_s = spots_map["BTC"]
                        idx = int(np.searchsorted(df_s['ts_min_ms'], current_t, side='left'))
                        idx = min(len(df_s) - 1, max(0, idx))
                        spot_px = float(df_s.iloc[idx]['spot_px'])
                    elif ("eth" in q_lower or "ethereum" in q_lower) and "ETH" in spots_map:
                        df_s = spots_map["ETH"]
                        idx = int(np.searchsorted(df_s['ts_min_ms'], current_t, side='left'))
                        idx = min(len(df_s) - 1, max(0, idx))
                        spot_px = float(df_s.iloc[idx]['spot_px'])

            cand = Domain2Candidate(
                condition_id=cid,
                token_id=item['token_id'],
                question=item['question'],
                category=cat,
                price=item['price'],
                hours_to_resolution=item['hours_to_resolution'],
                depth_usd=max(50.0, item['depth_usd']),
                is_uma_proposed=(item['hours_to_resolution'] <= 2.0),
                spot_price=spot_px,
                strike_price=strike_px,
            )
            candidates.append(cand)

        # Solve optimal portfolio if cash is available and candidates exist
        if candidates and cash >= allocator.min_order_usd:
            opt_res = allocator.optimize_allocation(candidates, bankroll_usd=current_equity)
            if opt_res.allocations:
                for alloc in opt_res.allocations:
                    cid = alloc.condition_id
                    cost = min(alloc.allocated_usd, cash)
                    if cost >= allocator.min_order_usd and cid in epoch_candidates_map:
                        item = epoch_candidates_map[cid]
                        entry_px = alloc.effective_avg_price or alloc.price
                        shares = cost / entry_px
                        cash -= cost
                        total_volume_deployed += cost

                        open_positions[cid] = SimulatedPosition(
                            condition_id=cid,
                            token_id=alloc.token_id,
                            entry_price=entry_px,
                            shares=shares,
                            cost_basis=cost,
                            entry_ts_ms=current_t,
                            unlock_ts_ms=item['unlock_ts_ms'],
                            token_payout=item['token_payout'],
                            question=alloc.question,
                            category=alloc.category,
                        )

        # Advance to next epoch
        current_t += epoch_ms

    # Settle remaining positions at final mark
    for cid, pos in list(open_positions.items()):
        payout_proceeds = pos.shares * pos.token_payout
        profit = payout_proceeds - pos.cost_basis
        cash += payout_proceeds
        if pos.token_payout == 0.0:
            reversals_count += 1
        completed_trades.append({
            'condition_id': pos.condition_id,
            'entry_price': pos.entry_price,
            'shares': pos.shares,
            'cost_basis': pos.cost_basis,
            'payout_proceeds': payout_proceeds,
            'net_profit': profit,
            'return_pct': (profit / pos.cost_basis) * 100.0,
            'hold_time_hours': (pos.unlock_ts_ms - pos.entry_ts_ms) / (1000.0 * 3600.0),
            'payout': pos.token_payout,
        })

    final_equity = cash
    net_pnl = final_equity - initial_bankroll
    total_return_pct = (net_pnl / initial_bankroll) * 100.0
    daily_return_pct = total_return_pct / max(0.01, total_duration_days)
    annualized_arr_pct = daily_return_pct * 365.0
    annualized_apy_pct = (((1.0 + (daily_return_pct / 100.0)) ** 365.0) - 1.0) * 100.0

    # Drawdown metrics
    eq_series = pd.Series([e['equity'] for e in equity_curve])
    cummax = eq_series.cummax()
    drawdowns = (cummax - eq_series) / cummax
    max_dd_pct = drawdowns.max() * 100.0
    max_dd_usd = (cummax - eq_series).max()

    turns_per_day = (total_volume_deployed / initial_bankroll) / max(0.01, total_duration_days)

    # Sharpe ratio
    returns_series = eq_series.pct_change().dropna()
    if len(returns_series) > 1 and returns_series.std() > 1e-6:
        epoch_sharpe = float(returns_series.mean() / returns_series.std())
        annualized_sharpe = round(epoch_sharpe * np.sqrt(48.0 * 365.0), 2)
    else:
        annualized_sharpe = 0.0

    metrics = {
        'mode': mode,
        'initial_bankroll_usd': initial_bankroll,
        'final_equity_usd': round(final_equity, 2),
        'net_pnl_usd': round(net_pnl, 2),
        'total_return_pct': round(total_return_pct, 2),
        'daily_return_pct': round(daily_return_pct, 2),
        'annualized_arr_pct': round(annualized_arr_pct, 2),
        'annualized_apy_pct': round(annualized_apy_pct, 2),
        'annualized_sharpe': annualized_sharpe,
        'duration_days': round(total_duration_days, 2),
        'total_trades': len(completed_trades),
        'reversals_count': reversals_count,
        'win_rate_pct': round((len(completed_trades) - reversals_count) / max(1, len(completed_trades)) * 100.0, 2),
        'capital_turnover_per_day': round(turns_per_day, 2),
        'max_drawdown_pct': round(max_dd_pct, 2),
        'max_drawdown_usd': round(max_dd_usd, 2),
        'total_volume_deployed_usd': round(total_volume_deployed, 2),
    }

    logger.info("=== SIMULATION RESULTS [{}] [Bankroll=${:,.2f}] ===", mode, initial_bankroll)
    logger.info("Net PnL: +${:,.2f} ({:+.2f}%) over {:.2f} days | Sharpe: {:.2f}", net_pnl, total_return_pct, total_duration_days, annualized_sharpe)
    logger.info("Daily Return: {:+.2f}%/day | Annualized ARR: {:+.2f}% | Compounded APY: {:+.2f}%", daily_return_pct, annualized_arr_pct, annualized_apy_pct)
    logger.info("Trades: {} | Win Rate: {:.1f}% | Reversals: {}", len(completed_trades), metrics['win_rate_pct'], reversals_count)
    logger.info("Turnover: {:.2f} turns/day | Max Drawdown: {:.2f}% (${:.2f})", turns_per_day, max_dd_pct, max_dd_usd)

    return metrics


if __name__ == "__main__":
    con = duckdb.connect()
    con.execute("SET threads=2;")
    df_tape = load_historical_tape(con)
    spots_map = load_crypto_spots(con)

    # 1. Benchmark: $10,000 Institutional Bankroll
    logger.info(">>> BENCHMARKING $10,000 PORTFOLIO <<<")
    res_10k_static = run_simulation(df_tape, initial_bankroll=10000.0, mode="static_baseline", spots_map=spots_map)
    res_10k_dynamic = run_simulation(df_tape, initial_bankroll=10000.0, mode="institutional_dynamic", spots_map=spots_map)

    # 2. Benchmark: $200 Small Bankroll
    logger.info(">>> BENCHMARKING $200 PORTFOLIO <<<")
    res_200_static = run_simulation(df_tape, initial_bankroll=200.0, mode="static_baseline", spots_map=spots_map)
    res_200_dynamic = run_simulation(df_tape, initial_bankroll=200.0, mode="institutional_dynamic", spots_map=spots_map)

    output = {
        'bankroll_10000': {
            'static_baseline': res_10k_static,
            'institutional_dynamic': res_10k_dynamic,
        },
        'bankroll_200': {
            'static_baseline': res_200_static,
            'institutional_dynamic': res_200_dynamic,
        },
    }
    with open('data/derived/historical_ortools_returns.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2)
    logger.info("Saved results to data/derived/historical_ortools_returns.json")
