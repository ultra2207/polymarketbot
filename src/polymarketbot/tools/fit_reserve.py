"""Fit the endogenous-reserve coefficients from the recorded CLOB tape.

The reserve in :mod:`polymarketbot.strategy.reward.reserve` has three measured
inputs; this tool computes them and writes
``data/derived/reserve_calibration.json`` with the window, sample size,
definition and source of each.

What is measured
----------------
*Horizon.* The holding horizon and the merge batch come from the de-skew fit
(``data/derived/deskew_calibration.json``): the reserve is the cash needed to
survive one inventory cycle, so it must use the same measured horizon.

*Grid returns.* For every asset the last mid inside each ``horizon`` bucket is
taken from the tape's ``price_change`` top-of-book series; the return between
consecutive buckets is the market's horizon shock
``mid(t+H)/mid(t) - 1``.

* ``jump_shock_factor`` - the ``1 - alpha`` quantile of ``|shock|`` pooled over
  the measured asset-hours. ``alpha`` is the same stated preference the de-skew
  fit carries (10 % per default there; here the CLI takes it explicitly and the
  artifact records it).
* ``diversification_weight`` - ``1 -`` the average pairwise Pearson correlation
  of the horizon shocks across the measured markets.
* ``base_gas_actions`` - expected merges over one holding horizon across the
  measured book: ``sum_m fills_m * (H / 3600) / fills_per_batch`` with
  ``fills_per_batch = max(1, merge_min_shares / quote_size)``.
* ``per_market_gas_actions`` - the same quantity's per-market median.
* ``gas_floor_usd`` - the measured dollar cost of one merge at the on-chain gas
  price (``data/derived/onchain_gas.json`` and the POL/USD feed).

Usage
-----
    python -m polymarketbot.tools.fit_reserve \
        --dates 2026-11-04,2026-11-05,2026-11-06 --hours 07 \
        --out data/derived/reserve_calibration.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.deskew import (
    DESKEW_CALIBRATION_ARTIFACT,
    load_deskew_calibration,
)
from polymarketbot.strategy.reward.reserve import load_reserve_calibration
from polymarketbot.tools.pooling_stats import SCHEMA, linear_histogram

log = get_logger("fit_reserve")

DEFAULT_OUT = Path("data/derived/reserve_calibration.json")
DEFAULT_DESKEW = Path(DESKEW_CALIBRATION_ARTIFACT)

#: Fixed grid of |horizon shock| the pooled jump quantile is read from: 0.002
#: of a shock per bin over [0, 2].  A binary market's mid cannot double in one
#: horizon without a resolution, and any count above 2 is reported rather than
#: named.
JUMP_HISTOGRAM = {"lo": 0.0, "hi": 2.0, "width": 0.002}


def _hour_globs(
    dates: Sequence[str], hours: Sequence[str]
) -> list[tuple[str, str, list[str], list[str]]]:
    out = []
    for date in dates:
        for hour in hours:
            trades = sorted(
                Path(f"data/tape/clob_trades/date={date}/hour={hour}").glob("*.parquet")
            )
            events = sorted(
                Path(f"data/tape/clob_events/date={date}/hour={hour}").glob("*.parquet")
            )
            if trades and events:
                out.append((date, hour, [str(p) for p in events], [str(p) for p in trades]))
    return out


def measure_hour(
    con: duckdb.DuckDBPyConnection,
    events: list[str],
    trades: list[str],
    *,
    bucket_ns: int,
) -> tuple[dict[str, dict[int, float]], dict[str, int], int]:
    """Per-asset last mid per ``bucket_ns`` bucket, crossing prints, row counts."""
    book_rows = con.execute(
        f"""
        with traded as (select distinct asset_id from read_parquet({trades!r}))
        select asset_id, ts, bid, ask from (
            select (unnest(price_changes)).asset_id asset_id,
                   recv_ts_ns ts,
                   (unnest(price_changes)).best_bid bid,
                   (unnest(price_changes)).best_ask ask
            from read_parquet({events!r}) where event_type = 'price_change'
        )
        where bid > 0 and ask > 0 and ask > bid
          and asset_id in (select asset_id from traded)
        order by asset_id, ts
        """
    ).fetchall()
    trade_rows = con.execute(
        f"""
        select asset_id, recv_ts_ns, price, size, side
        from read_parquet({trades!r})
        where price > 0 and size > 0
        order by asset_id, recv_ts_ns
        """
    ).fetchall()
    mids: dict[str, dict[int, float]] = defaultdict(dict)
    # Crossings are measured against the last book at or before each print; the
    # per-hour book list is already ordered, so a merge pointer is enough.
    book_list: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for asset_id, ts, bid, ask in book_rows:
        key = str(asset_id)
        book_list[key].append((int(ts), float(bid), float(ask)))
        mids[key][int(ts) // bucket_ns] = (float(bid) + float(ask)) / 2.0
    crossing: dict[str, int] = defaultdict(int)
    by_asset: dict[str, list[tuple[int, float, float, str]]] = defaultdict(list)
    for asset_id, ts, price, size, side in trade_rows:
        by_asset[str(asset_id)].append((int(ts), float(price), float(size), str(side)))
    n_prints = len(trade_rows)
    for asset_id, prints in by_asset.items():
        rows = book_list.get(asset_id, [])
        if not rows:
            continue
        ptr = -1
        for ts, price, _size, side in prints:
            while ptr + 1 < len(rows) and rows[ptr + 1][0] <= ts:
                ptr += 1
            if ptr < 0:
                continue
            bid, ask = rows[ptr][1], rows[ptr][2]
            if (side == "SELL" and price <= bid + 1e-9) or (
                side == "BUY" and price >= ask - 1e-9
            ):
                crossing[asset_id] += 1
    return dict(mids), dict(crossing), len(book_rows), n_prints


def horizon_returns(
    mids: dict[str, dict[int, float]], *, min_obs: int
) -> tuple[list[np.ndarray], dict[str, dict[int, float]]]:
    """Consecutive-bucket returns keyed by the bucket they start in."""
    per_asset: dict[str, dict[int, float]] = {}
    pooled: list[np.ndarray] = []
    for asset_id, buckets in mids.items():
        keys = sorted(buckets)
        returns = {
            keys[k]: buckets[keys[k + 1]] / buckets[keys[k]] - 1.0
            for k in range(len(keys) - 1)
            if keys[k + 1] == keys[k] + 1
        }
        if len(returns) < min_obs:
            continue
        values = np.array(list(returns.values()), dtype=float)
        if not np.all(np.isfinite(values)):
            continue
        per_asset[asset_id] = returns
        pooled.append(values)
    return pooled, per_asset


def jump_shock_quantile(shocks: np.ndarray, probability: float) -> float:
    """``1 - probability`` quantile of the measured |horizon shock|."""
    if shocks.size == 0:
        raise ValueError("no measured shocks")
    return float(np.quantile(np.abs(shocks), 1.0 - probability))


def mean_pairwise_correlation(
    returns: dict[str, dict[int, float]],
    *,
    max_assets: int,
) -> dict[str, Any]:
    """Average pairwise Pearson correlation of exactly aligned horizon shocks."""
    ranked = sorted(returns, key=lambda a: len(returns[a]), reverse=True)[:max_assets]
    if len(ranked) < 2:
        return {"measurable": False, "reason": "fewer than two markets with returns"}
    buckets = sorted({bucket for asset_id in ranked for bucket in returns[asset_id]})
    index = {bucket: i for i, bucket in enumerate(buckets)}
    matrix = np.full((len(ranked), len(buckets)), np.nan)
    for row, asset_id in enumerate(ranked):
        for bucket, value in returns[asset_id].items():
            matrix[row, index[bucket]] = value
    correlations: list[float] = []
    for i in range(len(ranked)):
        for j in range(i + 1, len(ranked)):
            mask = np.isfinite(matrix[i]) & np.isfinite(matrix[j])
            n = int(mask.sum())
            if n < 3:
                continue
            x, y = matrix[i, mask], matrix[j, mask]
            sx, sy = x.std(), y.std()
            if sx <= 0.0 or sy <= 0.0:
                continue
            corr = float(np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy))
            if math.isfinite(corr):
                correlations.append(corr)
    if not correlations:
        return {"measurable": False, "reason": "no pair has enough overlapping shocks"}
    return {
        "measurable": True,
        "mean_correlation": float(np.mean(correlations)),
        "median_correlation": float(np.median(correlations)),
        "pairs": len(correlations),
        "assets": len(ranked),
    }


def _merge_gas_cost() -> tuple[float, dict[str, Any]]:
    from polymarketbot.strategy.onchain.ctf.gas import estimate_merge_cost_usd

    path = Path("data/derived/onchain_gas.json")
    try:
        gas = json.loads(path.read_text(encoding="utf-8"))
        gwei = float(gas["gwei"])
    except (OSError, ValueError, KeyError, TypeError):
        gwei = None
    pol_path = Path("data/derived/onchain_pol.json")
    try:
        pol_usd = float(json.loads(pol_path.read_text(encoding="utf-8"))["usd"])
    except (OSError, ValueError, KeyError, TypeError):
        pol_usd = None
    cost = estimate_merge_cost_usd(gas_price_gwei=gwei, pol_usd_price=pol_usd)
    if cost is None:
        raise SystemExit("merge gas is unmeasured; refusing to write the reserve fit")
    return float(cost), {"gas_file": str(path), "pol_file": str(pol_path),
                         "gwei": gwei, "pol_usd": pol_usd}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", required=True)
    parser.add_argument("--hours", required=True)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--deskew-calibration", default=str(DEFAULT_DESKEW))
    parser.add_argument(
        "--jump-probability",
        type=float,
        default=0.01,
        help="stated preference: tail probability of the horizon jump quantile",
    )
    parser.add_argument("--min-obs", type=int, default=3)
    parser.add_argument("--max-assets", type=int, default=400)
    parser.add_argument("--max-seconds", type=float, default=600.0)
    parser.add_argument("--max-hours", type=int, default=0)
    args = parser.parse_args(argv)
    if not 0.0 < args.jump_probability < 0.5:
        parser.error("--jump-probability must be in (0, 0.5)")

    started = datetime.now(UTC)
    deskew = load_deskew_calibration(args.deskew_calibration)
    if deskew is None:
        raise SystemExit(
            f"no de-skew calibration at {args.deskew_calibration}: the reserve "
            "horizon and merge batch are only defined by that fit"
        )
    deskew_payload = json.loads(Path(args.deskew_calibration).read_text(encoding="utf-8"))
    diagnostics = deskew_payload.get("diagnostics", {})
    horizon_seconds = float(diagnostics.get("holding_horizon_seconds") or 0.0)
    quote_size = float(diagnostics.get("quote_size_shares") or 0.0)
    if horizon_seconds <= 0.0 or quote_size <= 0.0:
        raise SystemExit("the de-skew fit carries no measured horizon or quote size")
    bucket_ns = max(int(horizon_seconds * 1e9), 1_000_000_000)

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    hours = [h.strip().zfill(2) for h in args.hours.split(",") if h.strip()]
    partitions = _hour_globs(dates, hours)
    if not partitions:
        raise SystemExit("no tape partition matches the requested dates/hours")
    if args.max_hours > 0:
        partitions = partitions[: args.max_hours]

    con = duckdb.connect()
    con.execute("SET threads = 4")
    mids: dict[str, dict[int, float]] = defaultdict(dict)
    fills_per_asset: dict[str, int] = defaultdict(int)
    hours_per_asset: dict[str, int] = defaultdict(int)
    book_events = 0
    prints = 0
    partitions_measured = 0
    measured_labels: list[str] = []
    for date, hour, events, trades in partitions:
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if elapsed > args.max_seconds:
            log.warning(f"stopped after {elapsed:.0f}s at {date} {hour}")
            break
        hour_mids, hour_crossings, n_books, hour_prints = measure_hour(
            con, events, trades, bucket_ns=bucket_ns
        )
        if not hour_mids:
            continue
        partitions_measured += 1
        measured_labels.append(f"{date} {hour}")
        book_events += n_books
        for asset_id, buckets in hour_mids.items():
            mids[asset_id].update(buckets)
        for asset_id in hour_mids:
            hours_per_asset[asset_id] += 1
        for asset_id, count in hour_crossings.items():
            fills_per_asset[asset_id] += count
        prints += hour_prints
        log.info(
            f"{date} {hour}: {len(hour_mids)} assets, {sum(hour_crossings.values())} crossing prints"
        )
    if not mids:
        raise SystemExit("no book rows measured; refusing to write a reserve fit")

    pooled, returns = horizon_returns(mids, min_obs=args.min_obs)
    if not pooled:
        raise SystemExit("no consecutive-horizon returns; the jump factor is not measurable")
    shocks = np.concatenate(pooled)
    jump = jump_shock_quantile(shocks, args.jump_probability)

    buckets_seen = sorted({b for buckets in mids.values() for b in buckets})
    correlation = mean_pairwise_correlation(returns, max_assets=args.max_assets)
    if not correlation.get("measurable"):
        raise SystemExit(
            f"diversification weight is not measurable: {correlation.get('reason')}"
        )
    diversification = 1.0 - float(correlation["mean_correlation"])

    merge_min_shares = float(deskew.merge_min_shares)
    fills_per_batch = max(1.0, math.ceil(merge_min_shares / quote_size))
    horizon_hours = horizon_seconds / 3600.0
    actions = {
        asset_id: (
            count / max(1, hours_per_asset[asset_id]) * horizon_hours / fills_per_batch
        )
        for asset_id, count in fills_per_asset.items()
        if count > 0
    }
    if not actions:
        raise SystemExit("no crossing prints; expected merge actions are not measurable")
    base_actions = float(sum(actions.values()))
    per_market_actions = float(np.median(list(actions.values())))
    if per_market_actions <= 0.0:
        raise SystemExit("measured per-market merge actions are zero")

    gas_cost, gas_meta = _merge_gas_cost()
    window_start = datetime.fromtimestamp(buckets_seen[0] * bucket_ns / 1e9, tz=UTC)
    window_end = datetime.fromtimestamp(
        (buckets_seen[-1] + 1) * bucket_ns / 1e9, tz=UTC
    )

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {
            "start_utc": window_start.isoformat(),
            "end_utc": window_end.isoformat(),
            "partitions": measured_labels,
        },
        "sample_size": {
            "hour_partitions": partitions_measured,
            "assets": len(mids),
            "assets_with_returns": len(returns),
            "horizon_shocks": int(shocks.size),
            "correlation_pairs": correlation["pairs"],
            "markets_with_fills": len(actions),
            "book_events": book_events,
            "quote_placements": prints,
        },
        "preference": {
            "jump_probability": args.jump_probability,
            "statement": "tail probability of the measured horizon-jump quantile",
        },
        "definition": {
            "window": "bucket span of the measured top-of-book series (UTC)",
            "horizon_seconds": horizon_seconds,
            "bucket": "last mid per floor(recv_ts_ns / horizon) bucket",
            "shock": "mid(t+H)/mid(t) - 1 between consecutive buckets",
            "jump_shock_factor": "(1-alpha) quantile of |shock|",
            "diversification_weight": "1 - mean pairwise Pearson correlation of shocks",
            "base_gas_actions": "sum_m expected merges over one horizon",
            "per_market_gas_actions": "median_m expected merges over one horizon",
            "gas_floor_usd": "measured dollar cost of one merge",
        },
        "source": {
            "jump_shock_factor": "pooled measured horizon shocks",
            "diversification_weight": "measured pairwise shock correlation",
            "base_gas_actions": "measured crossing prints / measured fills per merge batch",
            "per_market_gas_actions": "measured crossing prints / measured fills per merge batch",
            "gas_floor_usd": gas_meta,
        },
        "diagnostics": {
            "horizon_seconds": horizon_seconds,
            "quote_size_shares": quote_size,
            "merge_min_shares": merge_min_shares,
            "fills_per_batch": fills_per_batch,
            "jump_abs_median": float(np.median(np.abs(shocks))),
            "jump_abs_quantile": jump,
            "correlation": correlation,
            "base_actions": base_actions,
            "per_market_actions": per_market_actions,
            "gas_cost_usd_per_merge": gas_cost,
        },
        "jump_shock_factor": jump,
        "diversification_weight": float(diversification),
        "base_gas_actions": base_actions,
        "per_market_gas_actions": per_market_actions,
        "gas_floor_usd": gas_cost,
        # Raw sufficient statistics for the pooled quantile and the two
        # market-set sums: the |shock| histogram adds over partitions, and the
        # per-market crossing/hours counts are the additive form of the
        # expected-merge actions (the same market in two hours merges).
        "sufficient_statistics": {
            "schema": SCHEMA,
            "jump_abs": linear_histogram(np.abs(shocks), **JUMP_HISTOGRAM),
            "jump_probability": float(args.jump_probability),
            "merge_actions": {
                "horizon_seconds": float(horizon_seconds),
                "fills_per_batch": float(fills_per_batch),
                "hours_total": float(sum(hours_per_asset[asset] for asset in actions)),
                "markets": {
                    str(asset_id): {
                        "fills": int(fills_per_asset[asset_id]),
                        "hours": int(hours_per_asset[asset_id]),
                    }
                    for asset_id in actions
                },
            },
        },
    }
    for name in (
        "jump_shock_factor",
        "diversification_weight",
        "base_gas_actions",
        "per_market_gas_actions",
        "gas_floor_usd",
    ):
        value = float(payload[name])
        if not math.isfinite(value) or value <= 0.0:
            raise SystemExit(f"reserve field {name}={value!r} is not a positive measurement")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    written = load_reserve_calibration(out)
    if written is None:
        raise SystemExit(f"wrote {out} but the reserve loader cannot read it")
    # Smoke-check the module's construction path against the written fit.
    from polymarketbot.strategy.reward.reserve import calculate_endogenous_reserve

    calculate_endogenous_reserve(
        [{"capital": 100.0, "mid": 0.5}],
        1000.0,
        base_gas_buffer_usd=0.0,
        calibration=written,
    )
    log.info(f"wrote {out} (jump={jump:.5f}, diversification={diversification:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
