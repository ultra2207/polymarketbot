"""Parquet reader for the UMA ``ProposePrice`` tape.

Only a decisive binary proposal is a verdict - ``1e18`` answers YES, ``0`` NO -
so a fractional (scalar) row says nothing about which outcome pays and is
dropped here, as is a dispute or a non-``YES_OR_NO_QUERY`` event. The measured
liveness window is the median span of the rows that publish one.
"""

from __future__ import annotations

from typing import List, Tuple

from polymarketbot.strategy.settlement.verdict_index import TapeProposal

# 1.0 == 1e18 fixed-point int256 price encoding used by the Optimistic Oracle.
_ORACLE_PRICE_ONE = 1e18


def read_tape(glob: str, lookback_sec: float, now_ts: float) -> Tuple[List[TapeProposal], float]:
    import duckdb

    cutoff_ms = int((now_ts - lookback_sec) * 1000.0)
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT question, condition_id, proposed_price, ts, expiry "
            "FROM read_parquet(?) "
            "WHERE event = 'ProposePrice' "
            "  AND identifier_text = 'YES_OR_NO_QUERY' "
            "  AND proposed_price IS NOT NULL "
            "  AND coalesce(disputed, false) = false "
            "  AND ts >= ? "
            "ORDER BY ts ASC",
            [glob, cutoff_ms],
        ).fetchall()
    finally:
        con.close()
    proposals: List[TapeProposal] = []
    liveness: List[float] = []
    for question, condition_id, price_raw, ts_raw, expiry_raw in rows:
        try:
            price = float(price_raw) / _ORACLE_PRICE_ONE
        except (TypeError, ValueError):
            continue
        # Only a decisive binary proposal is a verdict; a fractional
        # (scalar) proposal says nothing about which outcome pays.
        if price >= 0.999:
            price = 1.0
        elif price <= 0.001:
            price = 0.0
        else:
            continue
        try:
            ts = float(ts_raw) / 1000.0
        except (TypeError, ValueError):
            continue
        if not (0.0 < ts <= now_ts + 3600.0):
            continue
        span = 0.0
        expiry_ts = 0.0
        if expiry_raw is not None:
            try:
                # ``expiry`` is an epoch in seconds while ``ts`` is in ms.
                expiry_ts = float(expiry_raw)
                candidate_span = expiry_ts - ts
            except (TypeError, ValueError):
                candidate_span = 0.0
                expiry_ts = 0.0
            if 0.0 < candidate_span <= 6 * 3600.0:
                span = candidate_span
                liveness.append(candidate_span)
            elif not (0.0 < expiry_ts <= now_ts + 6 * 3600.0):
                expiry_ts = 0.0
        proposals.append(
            TapeProposal(
                question=str(question or ""),
                condition_id=str(condition_id or "").lower(),
                proposed_price=price,
                ts=ts,
                liveness_sec=span,
                expiry_ts=expiry_ts,
            )
        )
    liveness_sorted = sorted(liveness)
    measured = (
        liveness_sorted[len(liveness_sorted) // 2] if liveness_sorted else 0.0
    )
    return proposals, measured

