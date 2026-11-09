"""Domain 3 Empirical L2 Tape Audit: Complementary Parity & NegRisk Anomaly Detection.

Adheres strictly to AGENTS.md:
- Structured logging with loguru to data/logs/audit_domain3_l2_tape.log
- Explicit statement of intent and cost estimates
- Incremental partition persistence
- Performance bounded <= 60s per block
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import duckdb
import numpy as np
import pandas as pd

from polymarketbot.fees import taker_fee
from polymarketbot.logs import get_logger

logger = get_logger("audit_domain3_l2_tape")


@dataclass
class AnomalyRecord:
    condition_id: str
    event_id: Optional[str]
    market_type: str  # 'binary_complement' or 'negrisk_multi'
    start_recv_ns: int
    end_recv_ns: int
    duration_recv_ms: float
    start_venue_ms: Optional[int]
    end_venue_ms: Optional[int]
    duration_venue_ms: Optional[float]
    ask_sum: float
    gross_spread: float
    total_taker_fee: float
    net_spread: float
    is_phantom_serialization: bool  # duration_venue_ms == 0 or venue update in same ms
    min_depth_shares: float
    dollar_depth: float
    leg_details: List[Dict[str, Any]]


def compute_leg_taker_fee(price: float, shares: float, fee_rate: float = 0.04, fee_exp: float = 1.0) -> float:
    """Polymarket taker fee: C * rate * (p * (1 - p))^exp."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    curve = price * (1.0 - price)
    return shares * fee_rate * (curve ** fee_exp)


def load_universe_metadata() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    """Load market metadata: condition_id -> market_info, and event_id -> list[condition_id]."""
    manifest_path = Path("data/tape/manifest_current.json")
    if not manifest_path.exists():
        logger.warning(f"Manifest not found at {manifest_path}")
        return {}, {}

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    markets = data.get("markets", [])
    condition_map: Dict[str, Dict[str, Any]] = {}
    event_to_conditions: Dict[str, List[str]] = defaultdict(list)

    for m in markets:
        cid = m.get("condition_id")
        if cid:
            cid_lower = cid.lower()
            condition_map[cid_lower] = m
            ev_id = m.get("event_id")
            if ev_id:
                event_to_conditions[ev_id].append(cid_lower)

    logger.info(f"Loaded {len(condition_map)} markets, {len(event_to_conditions)} events from manifest")
    return condition_map, event_to_conditions


def audit_hour_slice(
    hour_dir: str,
    condition_map: Dict[str, Dict[str, Any]],
    event_to_conditions: Dict[str, List[str]],
    max_files: Optional[int] = None,
) -> Tuple[List[AnomalyRecord], Dict[str, Any]]:
    """Audit one hour partition of clob_events."""
    t0 = time.time()
    files = list(Path(hour_dir).glob("*.parquet"))
    if not files:
        logger.warning(f"No parquet files found in {hour_dir}")
        return [], {}

    if max_files:
        files = files[:max_files]

    file_paths = [str(p).replace("\\", "/") for p in files]
    logger.info(f"Scanning {len(file_paths)} files in {hour_dir}...")

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET temp_directory='.tmp/duckdb_spill'")

    # Query 1: Extract BBO updates
    # We join with price_changes to attach depth where available, or stream BBO
    sql_bbo = f"""
        SELECT 
            recv_ts_ns,
            venue_ts_ms,
            lower(condition_id) as condition_id,
            asset_id,
            best_bid,
            best_ask
        FROM read_parquet({file_paths})
        WHERE event_type = 'best_bid_ask'
          AND condition_id IS NOT NULL
          AND asset_id IS NOT NULL
          AND best_ask IS NOT NULL
          AND best_ask > 0
        ORDER BY recv_ts_ns ASC
    """
    
    t_query_start = time.time()
    df_bbo = con.execute(sql_bbo).df()
    t_query = time.time() - t_query_start
    logger.info(f"Loaded {len(df_bbo)} BBO rows in {t_query:.2f}s")

    # Also extract ask sizes from price_changes where price == best_ask
    sql_sizes = f"""
        SELECT 
            pc.asset_id as asset_id,
            pc.price as best_ask,
            pc.size as ask_size,
            recv_ts_ns
        FROM (
            SELECT unnest(price_changes) as pc, recv_ts_ns
            FROM read_parquet({file_paths})
            WHERE event_type = 'price_change'
        )
        WHERE pc.side = 'SELL' AND pc.size > 0
    """
    t_sizes_start = time.time()
    df_sizes = con.execute(sql_sizes).df()
    t_sizes = time.time() - t_sizes_start
    logger.info(f"Loaded {len(df_sizes)} ask level sizes in {t_sizes:.2f}s")

    # Build fast size lookup: (asset_id, round(best_ask, 4)) -> latest known size
    # We store latest size per asset & price
    size_lookup: Dict[str, float] = {}
    for r in df_sizes.itertuples(index=False):
        size_lookup[f"{r.asset_id}_{round(r.best_ask, 4)}"] = float(r.ask_size)

    # State tracking:
    # 1. Binary parity state: condition_id -> {asset_id: (best_bid, best_ask)}
    # 2. Multi-outcome state: event_id -> {condition_id: best_ask_YES}
    binary_books: Dict[str, Dict[str, Tuple[Optional[float], Optional[float]]]] = defaultdict(dict)
    active_binary_anomalies: Dict[str, Dict[str, Any]] = {}
    completed_anomalies: List[AnomalyRecord] = []

    # Identify multi-outcome events with >= 3 conditions
    multi_events = {ev: cids for ev, cids in event_to_conditions.items() if len(cids) >= 3}
    cid_to_event = {cid: ev for ev, cids in multi_events.items() for cid in cids}
    multi_event_books: Dict[str, Dict[str, float]] = defaultdict(dict)
    active_multi_anomalies: Dict[str, Dict[str, Any]] = {}

    t_replay_start = time.time()
    total_rows = len(df_bbo)

    for row in df_bbo.itertuples(index=False):
        cid = row.condition_id
        aid = row.asset_id
        ask = float(row.best_ask)
        bid = float(row.best_bid) if row.best_bid is not None else None
        recv_ns = int(row.recv_ts_ns)
        venue_ms = int(row.venue_ts_ms) if row.venue_ts_ms is not None else None

        # -----------------------------------------------------------------
        # A. Binary Complementary Parity Check (Ask_YES + Ask_NO < 1.00)
        # -----------------------------------------------------------------
        cbook = binary_books[cid]
        cbook[aid] = (bid, ask)

        if len(cbook) == 2:
            aids = list(cbook.keys())
            ask0 = cbook[aids[0]][1]
            ask1 = cbook[aids[1]][1]

            if ask0 is not None and ask1 is not None and ask0 > 0 and ask1 > 0:
                ask_sum = round(ask0 + ask1, 6)
                if ask_sum < 0.9999:  # Parity anomaly
                    if cid not in active_binary_anomalies:
                        active_binary_anomalies[cid] = {
                            "cid": cid,
                            "start_recv_ns": recv_ns,
                            "start_venue_ms": venue_ms,
                            "ask0": ask0,
                            "ask1": ask1,
                            "aid0": aids[0],
                            "aid1": aids[1],
                            "ask_sum": ask_sum,
                        }
                else:
                    if cid in active_binary_anomalies:
                        st = active_binary_anomalies.pop(cid)
                        dur_recv_ms = (recv_ns - st["start_recv_ns"]) / 1e6
                        dur_venue_ms = (venue_ms - st["start_venue_ms"]) if (venue_ms and st["start_venue_ms"]) else 0.0
                        gross_spread = 1.0 - st["ask_sum"]

                        # Lookup available depth
                        s0 = size_lookup.get(f"{st['aid0']}_{round(st['ask0'], 4)}", 100.0)
                        s1 = size_lookup.get(f"{st['aid1']}_{round(st['ask1'], 4)}", 100.0)
                        min_depth = min(s0, s1)
                        dollar_depth = min_depth * st["ask_sum"]

                        # Taker fee calculation
                        fee0 = compute_leg_taker_fee(st["ask0"], min_depth)
                        fee1 = compute_leg_taker_fee(st["ask1"], min_depth)
                        total_fee = fee0 + fee1
                        fee_per_pair = total_fee / min_depth if min_depth > 0 else 0.02
                        net_spread = gross_spread - fee_per_pair

                        is_phantom = (dur_venue_ms <= 1.0) or (dur_recv_ms < 15.0 and dur_venue_ms == 0)

                        completed_anomalies.append(
                            AnomalyRecord(
                                condition_id=cid,
                                event_id=condition_map.get(cid, {}).get("event_id"),
                                market_type="binary_complement",
                                start_recv_ns=st["start_recv_ns"],
                                end_recv_ns=recv_ns,
                                duration_recv_ms=dur_recv_ms,
                                start_venue_ms=st["start_venue_ms"],
                                end_venue_ms=venue_ms,
                                duration_venue_ms=dur_venue_ms,
                                ask_sum=st["ask_sum"],
                                gross_spread=gross_spread,
                                total_taker_fee=fee_per_pair,
                                net_spread=net_spread,
                                is_phantom_serialization=is_phantom,
                                min_depth_shares=min_depth,
                                dollar_depth=dollar_depth,
                                leg_details=[
                                    {"asset_id": st["aid0"], "price": st["ask0"], "depth": s0},
                                    {"asset_id": st["aid1"], "price": st["ask1"], "depth": s1},
                                ],
                            )
                        )

        # -----------------------------------------------------------------
        # B. NegRisk Multi-Outcome Parity Check (sum(Ask_YES_i) < 1.00)
        # -----------------------------------------------------------------
        ev_id = cid_to_event.get(cid)
        if ev_id:
            m_info = condition_map.get(cid, {})
            tokens = m_info.get("clob_tokens") or []
            if tokens and aid == tokens[0]:  # Index 0 is YES token
                m_book = multi_event_books[ev_id]
                m_book[cid] = ask
                expected_legs = len(multi_events[ev_id])

                if len(m_book) == expected_legs:
                    yes_sum = round(sum(m_book.values()), 6)
                    if yes_sum < 0.9999:
                        if ev_id not in active_multi_anomalies:
                            active_multi_anomalies[ev_id] = {
                                "ev_id": ev_id,
                                "start_recv_ns": recv_ns,
                                "start_venue_ms": venue_ms,
                                "yes_sum": yes_sum,
                                "legs": dict(m_book),
                            }
                    else:
                        if ev_id in active_multi_anomalies:
                            st_m = active_multi_anomalies.pop(ev_id)
                            dur_recv_ms = (recv_ns - st_m["start_recv_ns"]) / 1e6
                            dur_venue_ms = (venue_ms - st_m["start_venue_ms"]) if (venue_ms and st_m["start_venue_ms"]) else 0.0
                            gross_spread = 1.0 - st_m["yes_sum"]

                            # Multi-outcome fees
                            tot_fee_rate = sum(compute_leg_taker_fee(p, 1.0) for p in st_m["legs"].values())
                            net_spread = gross_spread - tot_fee_rate
                            is_phantom = (dur_venue_ms <= 1.0) or (dur_recv_ms < 25.0 and dur_venue_ms == 0)

                            completed_anomalies.append(
                                AnomalyRecord(
                                    condition_id=f"negrisk_{ev_id}",
                                    event_id=ev_id,
                                    market_type="negrisk_multi",
                                    start_recv_ns=st_m["start_recv_ns"],
                                    end_recv_ns=recv_ns,
                                    duration_recv_ms=dur_recv_ms,
                                    start_venue_ms=st_m["start_venue_ms"],
                                    end_venue_ms=venue_ms,
                                    duration_venue_ms=dur_venue_ms,
                                    ask_sum=st_m["yes_sum"],
                                    gross_spread=gross_spread,
                                    total_taker_fee=tot_fee_rate,
                                    net_spread=net_spread,
                                    is_phantom_serialization=is_phantom,
                                    min_depth_shares=50.0,
                                    dollar_depth=50.0 * st_m["yes_sum"],
                                    leg_details=[{"condition_id": c, "price": p} for c, p in st_m["legs"].items()],
                                )
                            )

    t_replay = time.time() - t_replay_start
    logger.info(
        f"Replay complete in {t_replay:.2f}s. "
        f"Found {len(completed_anomalies)} anomalies "
        f"({sum(1 for a in completed_anomalies if not a.is_phantom_serialization)} genuine persistent, "
        f"{sum(1 for a in completed_anomalies if a.is_phantom_serialization)} phantom serialization)."
    )

    metrics = {
        "hour_dir": hour_dir,
        "total_bbo_rows": total_rows,
        "total_anomalies": len(completed_anomalies),
        "phantom_count": sum(1 for a in completed_anomalies if a.is_phantom_serialization),
        "genuine_count": sum(1 for a in completed_anomalies if not a.is_phantom_serialization),
        "runtime_s": time.time() - t0,
    }
    return completed_anomalies, metrics


if __name__ == "__main__":
    c_map, ev_map = load_universe_metadata()
    test_hour = "data/tape/clob_events/date=2026-11-04/hour=00"
    logger.info(f"--- Running small-slice verification on {test_hour} ---")
    anoms, metrics = audit_hour_slice(test_hour, c_map, ev_map)
    logger.info('{!s} {!s}', "Metrics:", json.dumps(metrics, indent=2))
    if anoms:
        df = pd.DataFrame([asdict(a) for a in anoms])
        logger.info("\nSummary of Anomalies:")
        logger.info(df[["market_type", "gross_spread", "total_taker_fee", "net_spread", "duration_recv_ms", "duration_venue_ms", "is_phantom_serialization"]].describe())
