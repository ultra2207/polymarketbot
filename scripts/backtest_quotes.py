#!/usr/bin/env python3
"""Counterfactual maker quote on one L3 asset-hour: fills, markout, net.

What this is
------------
A deliberately small counterfactual, not a strategy backtest. One
``data/derived/l3_queue`` asset-hour is replayed; a hypothetical maker quote is
placed ``distance`` away from the mid on one or both sides, and the L3 telemetry
says whether the level traded through while the quote was resting. Each fill is
marked out at 5 minutes, charged the maker fee (zero) and, when a merge is part
of the strategy, the CTF merge gas.

Quote lifecycle
---------------
The quote is placed when the top of book changes and cancelled when it changes
again, so one episode per distinct ``(best_bid, best_ask)`` state:

    episode = [first recv_ts_ns of that state, first recv_ts_ns of the next)

A fill is the first L3 row for that ``(side, price)`` level strictly inside the
episode whose ``fill_size > 0`` (the shadow order resting at that level was
filled). Filled quantity is the sum of ``fill_size`` inside the episode, capped
at ``--size`` so one episode never fills more than the order. A fill at the
instant the episode opens or closes is not counted: the trade that moved the top
of book was in flight before the quote was placed.

``--distance-ticks`` is measured in the market's tick. The tick comes from
``--tick`` when given, else the hour's ``tick_size_change`` event, else the
smallest step observed in the L3 level grid (``0.01`` markets and ``0.001``
markets differ, and quoting one tick on the wrong grid lands on a level that
does not exist).

Two caveats that bound the result
---------------------------------
1. The L3 shadow order joined its level earlier than our hypothetical quote, so
   it can sit nearer the front of the queue. Treating its fills as ours is
   *optimistic*: a fresh entrant joins behind whatever rests at the level when it
   arrives. The fill count here is an upper bound, not an estimate.
2. The tape is one asset, one hour. Nothing here is a sample of the strategy's
   behaviour across markets, and a 1-hour window contains few fills, so the PnL
   is printed with its sample size and is ``n/a`` below ``--min-fills``.

Net per share, per side
-----------------------
    bid: gross = mid_fill - price, markout = mid(t+5m) - price
    ask: gross = price - mid_fill, markout = price - mid(t+5m)
    net      = markout - maker_fee - gas_per_share

The maker fee is zero (``docs/fees.md``); the taker fee the same fill would have
paid is printed for comparison. Gas is zero unless ``--merge`` is passed: a
one-sided or a round-trip two-sided quote needs no CTF merge, and the flag
then charges one 78,000-gas ``mergePositions`` per filled buy episode at the
hour's median Polygon base fee, amortised over its shares. That assumes the
YES inventory is paired against a NO leg bought elsewhere, which this tape does
not contain.

Output
------
``data/derived/quote_backtest_<date>T<HH>_<asset>.parquet`` -- one row per fill
(quote ts, side, price, fill ts, shares, mid at fill, gross, markout, fee, gas,
net), and the same summary as ``.txt`` next to it. ``--no-write`` prints only.

Usage
-----
    # default: the busiest asset-hour of the latest l3_queue hour
    .venv/Scripts/python.exe scripts/backtest_quotes.py

    # explicit slice, one tick either side of the mid, both sides
    .venv/Scripts/python.exe scripts/backtest_quotes.py \
        --date 2026-11-04 --hour 01 \
        --asset 4639340586725578716005500791739559303082924048072534329825226706547465941088

    # join the best quote (half a tick from the mid), bid only
    .venv/Scripts/python.exe scripts/backtest_quotes.py --distance-ticks 0.5 --sides bid

    # list the available asset-hours
    .venv/Scripts/python.exe scripts/backtest_quotes.py --list
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import duckdb  # noqa: E402

from polymarketbot.fees import FeeSchedule, maker_rebate, schedule_for_fee_type, taker_fee  # noqa: E402
from polymarketbot.logs import get_logger  # noqa: E402
from polymarketbot.trading.ctf import estimate_merge_cost_usd  # noqa: E402

log = get_logger("backtest_quotes")

DEFAULT_L3_ROOT = Path("data/derived/l3_queue")
DEFAULT_TAPE = Path("data/tape")
DEFAULT_SNAPSHOT = Path("data/raw/gamma/snapshot_2026-11-01.parquet")
DEFAULT_MANIFEST = Path("data/tape/manifest_current.json")
DEFAULT_OUT = Path("data/derived")

MERGE_GAS_UNITS = 78_000  # CTF mergePositions, trading/ctf.py
HORIZON_NS = 300 * 1_000_000_000  # 5 minutes
MARKOUT_TOLERANCE_NS = 60 * 1_000_000_000  # accept a quote within 60 s of t+5m
DEFAULT_TICK = 0.01
MIN_FILLS_DEFAULT = 10  # below this the PnL block prints n/a
PRICE_EPSILON = 1e-9


# ------------------------------------------------------------------- geometry
def tick_grid(value: float, tick: float, side: str) -> float:
    """Snap ``value`` to the tick grid, away from the mid.

    A bid is floored and an ask ceilinged onto the grid so the quote is never
    closer to the mid than ``value``. ``PRICE_EPSILON`` absorbs float dust
    (``0.16000000000000003 / 0.01``), so an on-grid price stays on its own step.
    """
    if tick <= 0.0:
        raise ValueError(f"tick must be positive, got {tick}")
    steps = value / tick
    if side == "bid":
        snapped = math.floor(steps + PRICE_EPSILON)
    else:
        snapped = math.ceil(steps - PRICE_EPSILON)
    return round(snapped * tick, 10)


def quote_prices(mid: float, distance: float, tick: float) -> Dict[str, Optional[float]]:
    """``{side: price}`` for a quote ``distance`` either side of ``mid``.

    A side whose price falls outside ``(0, 1)`` is ``None``: that quote cannot be
    placed and is not counted.
    """
    out: Dict[str, Optional[float]] = {}
    for side, target in (("bid", mid - distance), ("ask", mid + distance)):
        price = tick_grid(target, tick, side)
        out[side] = round(price, 10) if 0.0 < price < 1.0 else None
    return out


KNOWN_TICKS = (0.1, 0.05, 0.01, 0.005, 0.001, 0.0001)


def infer_tick(prices: Sequence[float], known: Sequence[float] = KNOWN_TICKS) -> Optional[float]:
    """Tick size implied by the level grid: the smallest observed price step.

    The Gamma snapshot carries a tick only for markets that changed it, so the
    rest is inferred from the L3 levels. A missing level makes the observed step
    an over-estimate of the tick, so it is snapped *down* to the nearest known
    grid (a 0.02 gap in a 0.01 market reads as 0.01, never 0.02); a finer grid
    than the market's own is harmless because only the quoted price has to be on
    it.
    """
    distinct = sorted({round(float(p), 10) for p in prices})
    if len(distinct) < 2:
        return None
    step = min(b - a for a, b in zip(distinct, distinct[1:]))
    if step <= 0:
        return None
    fitting = [t for t in known if t <= step + 1e-12]
    return max(fitting) if fitting else round(step, 10)


@dataclass(frozen=True)
class Episode:
    """One resting quote window: the book top did not change for its length."""

    start_ns: int
    end_ns: int
    mid: float


def collapse_mid_states(
    rows: Iterable[Tuple[int, Optional[float], Optional[float]]], end_ns: int
) -> List[Episode]:
    """``[(ts, best_bid, best_ask)]`` -> one episode per distinct top of book.

    Consecutive frames that restate the same ``(best_bid, best_ask)`` are one
    state, not a reprice. Rows with a missing side are skipped (no mid). The last
    state runs to ``end_ns``, the hour's close.
    """
    episodes: List[Episode] = []
    pending: Optional[Tuple[int, float, float]] = None
    for ts, bid, ask in rows:
        if bid is None or ask is None:
            continue
        ts = int(ts)
        if pending is None:
            pending = (ts, float(bid), float(ask))
            continue
        if float(bid) == pending[1] and float(ask) == pending[2]:
            continue
        if ts > pending[0]:
            episodes.append(Episode(pending[0], ts, (pending[1] + pending[2]) / 2.0))
        pending = (ts, float(bid), float(ask))
    if pending is not None and end_ns > pending[0]:
        episodes.append(Episode(pending[0], int(end_ns), (pending[1] + pending[2]) / 2.0))
    return episodes


@dataclass
class Level:
    """Cumulative traded size at one ``(side, price)`` L3 level."""

    side: str
    price: float
    ts: List[int]
    cum: List[float]  # cum[i] = sum of fill_size over ts[:i+1]
    last_ts: int

    def filled_between(self, start_ns: int, end_ns: int) -> Tuple[Optional[int], float]:
        """``(first_fill_ts, shares)`` traded through this level inside the window.

        Strictly after ``start_ns``: the trade that changed the top of book and
        started this episode was in flight before the quote was placed, so it
        cannot have filled it. Strictly before ``end_ns``: a fill at the instant
        the quote is cancelled belongs to the next quote, if any.
        """
        lo = bisect.bisect_right(self.ts, start_ns)
        hi = bisect.bisect_left(self.ts, end_ns)
        if hi <= lo:
            return None, 0.0
        base = self.cum[lo - 1] if lo else 0.0
        return self.ts[lo], self.cum[hi - 1] - base

    def observed_after(self, ts: int) -> bool:
        """The level was updated at or after ``ts`` (it may have no fills at all)."""
        return self.last_ts > 0 and self.last_ts >= ts


def make_level(
    side: str, price: float, fills: Sequence[Tuple[int, float]], last_ts: Optional[int] = None
) -> Level:
    """Build a :class:`Level` from ``[(ts, fill_size)]`` in any order."""
    ordered = sorted((int(ts), float(size)) for ts, size in fills if float(size) > 0.0)
    ts: List[int] = []
    cum: List[float] = []
    running = 0.0
    for stamp, size in ordered:
        running += size
        ts.append(stamp)
        cum.append(running)
    if last_ts is None:
        last_ts = ts[-1] if ts else 0
    return Level(side=side, price=round(float(price), 10), ts=ts, cum=cum, last_ts=int(last_ts))


# --------------------------------------------------------------------- markout
def mid_before(ts_ns: Sequence[int], mids: Sequence[float], query_ns: int) -> Optional[float]:
    """Mid in force at ``query_ns``: the last state at or before it."""
    idx = bisect.bisect_right(ts_ns, query_ns) - 1
    return float(mids[idx]) if idx >= 0 else None


def mid_at_or_after(
    ts_ns: Sequence[int], mids: Sequence[float], query_ns: int, tolerance_ns: int
) -> Optional[float]:
    """First mid at or after ``query_ns``, and no later than ``tolerance_ns``.

    A market that goes quiet must not borrow a quote from minutes later, so the
    tolerance is a hard cap: past it the markout is missing, not stale.
    """
    idx = bisect.bisect_left(ts_ns, query_ns)
    if idx >= len(ts_ns):
        return None
    if ts_ns[idx] - query_ns > tolerance_ns:
        return None
    return float(mids[idx])


# ------------------------------------------------------------------- simulator
@dataclass(frozen=True)
class QuoteConfig:
    sides: str = "both"  # both | bid | ask
    distance: float = DEFAULT_TICK
    tick: float = DEFAULT_TICK
    order_size: float = 100.0
    horizon_ns: int = HORIZON_NS
    markout_tolerance_ns: int = MARKOUT_TOLERANCE_NS
    merge_usd: float = 0.0  # 0 = no merge leg

    def active_sides(self) -> Tuple[str, ...]:
        if self.sides == "both":
            return ("bid", "ask")
        return (self.sides,)


def simulate(
    episodes: Sequence[Episode],
    levels: Dict[Tuple[str, float], Level],
    mid_ts: Sequence[int],
    mid_val: Sequence[float],
    cfg: QuoteConfig,
) -> Dict[str, Any]:
    """Run the quote across ``episodes``; return per-fill rows and counters.

    Pure: every input is a sequence, so a test can build a synthetic fill
    sequence and check the PnL arithmetic exactly.
    """
    rows: List[Dict[str, Any]] = []
    quotes_placed = 0
    quotes_no_level = 0
    for episode in episodes:
        prices = quote_prices(episode.mid, cfg.distance, cfg.tick)
        for side in cfg.active_sides():
            price = prices.get(side)
            if price is None:
                continue
            quotes_placed += 1
            level = levels.get((side, price))
            if level is None or not level.observed_after(episode.start_ns):
                quotes_no_level += 1
                continue
            fill_ts, traded = level.filled_between(episode.start_ns, episode.end_ns)
            if fill_ts is None:
                continue
            shares = float(min(cfg.order_size, traded))
            mid_fill = mid_before(mid_ts, mid_val, fill_ts)
            mid_h = mid_at_or_after(
                mid_ts, mid_val, fill_ts + cfg.horizon_ns, cfg.markout_tolerance_ns
            )
            if mid_fill is None:
                gross: Optional[float] = None
            elif side == "bid":
                gross = mid_fill - price
            else:
                gross = price - mid_fill
            if mid_h is None:
                markout: Optional[float] = None
            elif side == "bid":
                markout = mid_h - price
            else:
                markout = price - mid_h
            gas_per_share = (
                cfg.merge_usd / shares if cfg.merge_usd > 0.0 and side == "bid" else 0.0
            )
            fee_per_share = 0.0  # maker
            net = None if markout is None else markout - fee_per_share - gas_per_share
            rows.append(
                {
                    "quote_ts_ns": episode.start_ns,
                    "side": side,
                    "quote_price": price,
                    "mid_at_quote": episode.mid,
                    "fill_ts_ns": fill_ts,
                    "shares": shares,
                    "mid_at_fill": mid_fill,
                    "gross_spread": gross,
                    "mid_after_horizon": mid_h,
                    "markout": markout,
                    "fee_per_share": fee_per_share,
                    "gas_per_share": gas_per_share,
                    "net_per_share": net,
                    "net_usd": None if net is None else net * shares,
                }
            )
    return {
        "rows": rows,
        "quotes_placed": quotes_placed,
        "quotes_no_level": quotes_no_level,
    }


def summarise(result: Dict[str, Any], min_fills: int = MIN_FILLS_DEFAULT) -> Dict[str, Any]:
    """Fill rate, mean gross/markout/net per share and the sample size."""
    rows: List[Dict[str, Any]] = result["rows"]
    fills = len(rows)
    quoted = result["quotes_placed"]
    observed = quoted - result["quotes_no_level"]
    marked = [r for r in rows if r["net_per_share"] is not None]

    def mean(key: str) -> Optional[float]:
        vals = [r[key] for r in marked if r[key] is not None]
        return statistics.fmean(vals) if vals else None

    total_shares = sum(r["shares"] for r in marked)
    net_usd = sum(r["net_usd"] for r in marked if r["net_usd"] is not None)
    # Share-weighted net per share: the money number. The unweighted mean answers
    # "what does one fill earn", the weighted one "what does one share earn".
    weighted = (net_usd / total_shares) if total_shares else None
    return {
        "quotes": quoted,
        "quotes_no_level": result["quotes_no_level"],
        "quotes_observed": observed,
        "fills": fills,
        "fill_rate": (fills / observed) if observed else None,
        "fills_missing_markout": fills - len(marked),
        "marked_fills": len(marked),
        "shares": total_shares,
        "gross_spread": mean("gross_spread"),
        "markout": mean("markout"),
        "fee": mean("fee_per_share"),
        "gas": mean("gas_per_share"),
        "net_per_share": mean("net_per_share") if len(marked) >= min_fills else None,
        "net_per_share_weighted": weighted if len(marked) >= min_fills else None,
        "net_usd": net_usd if (marked and len(marked) >= min_fills) else None,
        "net_per_share_indication": mean("net_per_share"),
        "net_per_share_weighted_indication": weighted,
    }


# ------------------------------------------------------------------------- IO
def available_asset_hours(root: Path) -> List[Dict[str, Any]]:
    """Every ``l3_queue`` asset-hour, busiest first within an hour, latest first."""
    out: List[Dict[str, Any]] = []
    for path in root.glob("date=*/hour=*/asset=*.parquet"):
        try:
            date = path.parent.parent.name.split("=", 1)[1]
            hour = path.parent.name.split("=", 1)[1]
        except IndexError:  # pragma: no cover - layout guard
            continue
        out.append(
            {
                "date": date,
                "hour": hour,
                "asset": path.stem.split("=", 1)[1],
                "path": path,
                "bytes": path.stat().st_size,
            }
        )
    out.sort(key=lambda r: (r["date"], r["hour"], r["bytes"]), reverse=True)
    return out


def resolve_asset_hour(
    root: Path, date: Optional[str], hour: Optional[str], asset: Optional[str]
) -> Dict[str, Any]:
    """Pick one asset-hour: exact date/hour, asset as full id or unique prefix."""
    hours = available_asset_hours(root)
    if not hours:
        raise SystemExit(f"no l3_queue asset-hours under {root}")
    if date:
        hours = [r for r in hours if r["date"] == date]
    if hour:
        hours = [r for r in hours if r["hour"] == f"{int(hour):02d}"]
    if not hours:
        raise SystemExit(f"no l3_queue asset-hour for date={date} hour={hour}")
    if asset:
        exact = [r for r in hours if r["asset"] == asset]
        if not exact:
            exact = [r for r in hours if r["asset"].startswith(asset)]
        if not exact:
            raise SystemExit(f"no l3_queue asset-hour for asset {asset}")
        if len(exact) > 1:
            raise SystemExit(f"asset prefix {asset} matches {len(exact)} asset-hours")
        return exact[0]
    return hours[0]


def _sql(path: str | Path) -> str:
    return Path(path).as_posix()


def load_episodes(tape: Path, date: str, hour: str, asset: str) -> Tuple[List[Episode], str]:
    """Mid episodes for ``asset`` from the hour's ``clob_events``.

    The nested ``price_changes`` carry each token's own ``best_bid``/``best_ask``;
    ``best_bid_ask`` frames are unioned in for assets that only emit the BBO.
    ``condition_id`` is returned so the market can be looked up.
    """
    glob = _sql(tape / "clob_events" / f"date={date}" / f"hour={hour}" / "*.parquet")
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            WITH pc AS (
                SELECT recv_ts_ns, condition_id, UNNEST(price_changes) AS c
                FROM read_parquet('{glob}')
                WHERE event_type = 'price_change'
            )
            SELECT recv_ts_ns, c.best_bid, c.best_ask, condition_id
            FROM pc
            WHERE c.asset_id = ? AND c.best_bid IS NOT NULL AND c.best_ask IS NOT NULL
            UNION ALL
            SELECT recv_ts_ns, best_bid, best_ask, condition_id
            FROM read_parquet('{glob}')
            WHERE event_type = 'best_bid_ask' AND asset_id = ?
              AND best_bid IS NOT NULL AND best_ask IS NOT NULL
            ORDER BY 1
            """,
            [asset, asset],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        raise SystemExit(f"no mid observations for asset {asset} in {date} hour {hour}")
    end_ns = max(int(r[0]) for r in rows)
    episodes = collapse_mid_states([(r[0], r[1], r[2]) for r in rows], end_ns)
    condition_ids = {str(r[3]).lower() for r in rows if r[3]}
    condition_id = sorted(condition_ids)[0] if condition_ids else ""
    return episodes, condition_id


def load_levels(path: Path) -> Dict[Tuple[str, float], Level]:
    """Every ``(side, price)`` level in the asset-hour, with its fills."""
    con = duckdb.connect()
    try:
        obs = con.execute(
            f"""
            SELECT side, price, max(recv_ts_ns) AS last_ts
            FROM read_parquet('{_sql(path)}')
            GROUP BY 1, 2
            """
        ).fetchall()
        fills = con.execute(
            f"""
            SELECT side, price, recv_ts_ns, fill_size
            FROM read_parquet('{_sql(path)}')
            WHERE fill_size > 0
            ORDER BY 1, 2, 3
            """
        ).fetchall()
    finally:
        con.close()
    by_level: Dict[Tuple[str, float], List[Tuple[int, float]]] = {}
    for side, price, ts, size in fills:
        by_level.setdefault((str(side), round(float(price), 10)), []).append((int(ts), float(size)))
    last_by_level = {(str(s), round(float(p), 10)): int(t) for s, p, t in obs}
    levels: Dict[Tuple[str, float], Level] = {}
    for key, last_ts in last_by_level.items():
        levels[key] = make_level(key[0], key[1], by_level.get(key, ()), last_ts=last_ts)
    return levels


def load_tick_size(tape: Path, date: str, hour: str, asset: str) -> Optional[float]:
    """Latest ``tick_size_change`` for the asset inside the hour, if any."""
    glob = _sql(tape / "clob_events" / f"date={date}" / f"hour={hour}" / "*.parquet")
    con = duckdb.connect()
    try:
        row = con.execute(
            f"""
            SELECT new_tick_size
            FROM read_parquet('{glob}')
            WHERE event_type = 'tick_size_change' AND asset_id = ?
              AND new_tick_size IS NOT NULL
            ORDER BY recv_ts_ns DESC
            LIMIT 1
            """,
            [asset],
        ).fetchone()
    finally:
        con.close()
    return float(row[0]) if row else None


def load_prices(path: Path) -> List[float]:
    """Distinct prices present in the asset-hour's L3 telemetry."""
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT DISTINCT price FROM read_parquet('{_sql(path)}')").fetchall()
    finally:
        con.close()
    return [float(r[0]) for r in rows]


def load_fee_schedule(snapshot: Path, condition_id: str, when: datetime) -> Tuple[Optional[FeeSchedule], Optional[str]]:
    """The market's ``feeType`` from the Gamma snapshot -> its fee schedule."""
    if not condition_id:
        return None, None
    if not snapshot.exists():
        log.warning(f"gamma snapshot missing ({snapshot}); fee schedule unknown")
        return None, None
    con = duckdb.connect()
    try:
        row = con.execute(
            f"SELECT feeType FROM read_parquet('{_sql(snapshot)}') WHERE lower(conditionId) = ?",
            [condition_id],
        ).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        return None, None
    return schedule_for_fee_type(str(row[0]), when), str(row[0])


def load_median_gas_gwei(tape: Path, date: str, hour: str) -> Optional[float]:
    """Median Polygon base fee in the hour; the tape's own observation."""
    path = tape / "polygon_gas" / f"date={date}" / f"hour={hour}"
    glob = _sql(path / "*.parquet")
    if not path.exists():
        return None
    con = duckdb.connect()
    try:
        row = con.execute(f"SELECT median(base_fee_gwei) FROM read_parquet('{glob}')").fetchone()
    finally:
        con.close()
    return float(row[0]) if row and row[0] is not None else None


def load_market(manifest: Path, condition_id: str, asset: str) -> Optional[Dict[str, Any]]:
    """Manifest entry for the market; ``min_size`` caps the quote size."""
    if not manifest.exists() or not condition_id:
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for market in data.get("markets", []):
        if str(market.get("condition_id", "")).lower() == condition_id.lower():
            return market
    for market in data.get("markets", []):
        if asset in (market.get("clob_tokens") or []):
            return market
    return None


# ---------------------------------------------------------------------- report
def _fmt(value: Optional[float], dp: int = 5, width: int = 10) -> str:
    if value is None:
        return "n/a".rjust(width)
    return f"{value:+.{dp}f}".rjust(width)


def _utc(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def report(
    *,
    slice_info: Dict[str, Any],
    market: Optional[Dict[str, Any]],
    condition_id: str,
    episodes: Sequence[Episode],
    stats: Dict[str, Any],
    cfg: QuoteConfig,
    fee_type: Optional[str],
    schedule: Optional[FeeSchedule],
    gas_gwei: Optional[float],
    gas_usd: Optional[float],
    taker_ref: Optional[float],
    min_fills: int,
    tick_source: str = "",
) -> str:
    window = (
        f"{_utc(episodes[0].start_ns)} .. {_utc(episodes[-1].end_ns)}"
        if episodes
        else "n/a"
    )
    hours = (episodes[-1].end_ns - episodes[0].start_ns) / 3.6e12 if episodes else 0.0
    lines: List[str] = []
    lines.append("quote backtest -- one asset-hour (counterfactual, L3-shadow fills)")
    lines.append(f"asset      : {slice_info['asset']}")
    lines.append(f"market     : {condition_id or 'n/a'}")
    if market:
        lines.append(f"question   : {market.get('question', 'n/a')}")
        lines.append(
            f"min_size   : {market.get('min_size')}  daily_pool: {market.get('daily_pool')}"
            f"  neg_risk: {market.get('neg_risk')}"
        )
    lines.append(f"window     : {window}  ({hours:.2f} h of tape, partition date={slice_info['date']} hour={slice_info['hour']})")
    lines.append(f"source     : {slice_info['path']}")
    lines.append(
        f"quotes     : sides={cfg.sides} distance={cfg.distance:.6f}"
        f" ({cfg.distance / cfg.tick:.2f} x tick {cfg.tick:g}), size cap {cfg.order_size:g} shares"
    )
    if tick_source:
        lines.append(f"tick       : {cfg.tick:g} -- {tick_source}")
    if schedule is None:
        lines.append("fee        : n/a (feeType unknown; maker fee is 0 by rule anyway)")
    else:
        fee_line = (
            f"fee        : {fee_type} rate={schedule.rate:g} exponent={schedule.exponent:g}"
            f" maker={0.0:.6f}/share"
        )
        if taker_ref is not None:
            fee_line += (
                f"  taker-equivalent={taker_ref:.6f}/share"
                f"  maker-rebate={maker_rebate(taker_ref, schedule):.6f}/share"
                f" (rebate not counted)"
            )
        lines.append(fee_line)
    if gas_usd is None:
        lines.append("gas        : n/a (no polygon_gas rows for the hour)")
    else:
        lines.append(
            f"gas        : median base fee {gas_gwei:.2f} gwei -> "
            f"{MERGE_GAS_UNITS:,} gas = ${gas_usd:.6f} per merge "
            + ("(merge leg ON, per filled bid episode)" if cfg.merge_usd > 0 else "(merge leg OFF)")
        )
    lines.append("")
    lines.append(f"episodes   : {len(episodes)} distinct top-of-book states in the hour")
    lines.append(f"quotes     : {stats['quotes']} sides placed")
    lines.append(f"levels     : {stats['quotes_observed']} of them had an L3 level after the quote")
    lines.append(f"           : {stats['quotes_no_level']} had no L3 observation (excluded)")
    rate = stats["fill_rate"]
    lines.append(
        f"fills      : {stats['fills']}"
        + (f"  (fill rate {rate:.2%} of observed sides)" if rate is not None else "  (fill rate n/a: no observed side)")
    )
    lines.append(f"shares     : {stats['shares']:.2f} filled")
    lines.append(
        f"missing    : {stats['fills_missing_markout']} fill(s) without a mid inside "
        f"{cfg.markout_tolerance_ns / 1e9:.0f}s of t+{cfg.horizon_ns / 1e9:.0f}s "
        f"(excluded from PnL)"
    )
    lines.append("")
    lines.append(f"per share (mean over the {stats['marked_fills']} marked fills):")
    lines.append(f"  gross spread {_fmt(stats['gross_spread'])}")
    lines.append(f"  markout(5m)  {_fmt(stats['markout'])}")
    lines.append(f"  maker fee    {_fmt(stats['fee'])}")
    lines.append(f"  gas          {_fmt(stats['gas'])}")
    if stats["net_per_share"] is None:
        lines.append(
            f"  net          n/a -- {stats['marked_fills']} marked fill(s) < --min-fills {min_fills}"
            f" (indication: per fill {_fmt(stats['net_per_share_indication'])},"
            f" per share {_fmt(stats['net_per_share_weighted_indication'])})"
        )
    else:
        lines.append(f"  net / fill   {_fmt(stats['net_per_share'])}")
        lines.append(f"  net / share  {_fmt(stats['net_per_share_weighted'])}  (share-weighted)")
        lines.append(
            f"  net total    {_fmt(stats['net_usd'], dp=4)} USD over {stats['shares']:.2f} shares"
            f" (n={stats['marked_fills']} fills)"
        )
    lines.append("")
    lines.append("caveats:")
    lines.append("  * one asset-hour, one market, one hour of tape -- not a strategy sample.")
    lines.append("  * fills come from the L3 shadow order, which joined its level before the")
    lines.append("    quote did, so a fresh quote sits further back in the queue: the fill")
    lines.append("    count is an optimistic upper bound. A fill is not a size-priority proof.")
    lines.append("  * ask-side PnL is the markout of a sale against the mid; inventory carry")
    lines.append("    cost is not modelled (no shorting on Polymarket).")
    if cfg.merge_usd > 0:
        lines.append("  * gas assumes the filled YES inventory is paired with a NO leg bought")
        lines.append("    elsewhere (this tape holds no complementary asset) and merged once.")
    return "\n".join(lines)


def write_outputs(rows: Sequence[Dict[str, Any]], out_dir: Path, stem: str, text: str) -> Tuple[Path, Path]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    out_dir.mkdir(parents=True, exist_ok=True)
    schema = pa.schema(
        [
            ("quote_ts_ns", pa.int64()),
            ("side", pa.string()),
            ("quote_price", pa.float64()),
            ("mid_at_quote", pa.float64()),
            ("fill_ts_ns", pa.int64()),
            ("shares", pa.float64()),
            ("mid_at_fill", pa.float64()),
            ("gross_spread", pa.float64()),
            ("mid_after_horizon", pa.float64()),
            ("markout", pa.float64()),
            ("fee_per_share", pa.float64()),
            ("gas_per_share", pa.float64()),
            ("net_per_share", pa.float64()),
            ("net_usd", pa.float64()),
        ]
    )
    table = pa.Table.from_pylist([dict(r) for r in rows], schema=schema)
    parquet_path = out_dir / f"{stem}.parquet"
    pq.write_table(table, parquet_path)
    text_path = out_dir / f"{stem}.txt"
    text_path.write_text(text + "\n", encoding="utf-8")
    return parquet_path, text_path


# ------------------------------------------------------------------------ main
def build_parser(epilog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Counterfactual maker quote on one L3 asset-hour.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", help="tape date, e.g. 2026-11-04 (default: latest)")
    parser.add_argument("--hour", help="tape hour 00-23 (default: latest)")
    parser.add_argument("--asset", help="asset id or unique prefix (default: busiest in the hour)")
    parser.add_argument("--sides", choices=("both", "bid", "ask"), default="both")
    parser.add_argument(
        "--distance-ticks",
        type=float,
        default=1.0,
        help="quote distance from the mid in ticks (0.5 joins the best quote)",
    )
    parser.add_argument(
        "--tick",
        type=float,
        default=None,
        help="tick size (default: tick_size_change in the hour, else inferred from the L3 grid)",
    )
    parser.add_argument("--size", type=float, default=None, help="order size in shares (default: market min_size)")
    parser.add_argument("--horizon-s", type=float, default=HORIZON_NS / 1e9, help="markout horizon (default 300)")
    parser.add_argument("--markout-tolerance-s", type=float, default=MARKOUT_TOLERANCE_NS / 1e9)
    parser.add_argument("--merge", action="store_true", help="charge one CTF merge per filled bid episode")
    parser.add_argument("--pol-usd", type=float, default=0.092, help="POL/USD for gas (default 0.092, ctf.py fallback)")
    parser.add_argument("--min-fills", type=int, default=MIN_FILLS_DEFAULT, help="marks below this print n/a")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT), help="Gamma snapshot for feeType")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--tape", default=str(DEFAULT_TAPE))
    parser.add_argument("--l3-root", default=str(DEFAULT_L3_ROOT))
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--list", action="store_true", help="list available asset-hours and exit")
    parser.add_argument("--no-write", action="store_true", help="print only, write no parquet")
    parser.add_argument("--limit-rows", type=int, default=20, help="rows of the fill table to print")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    l3_root = Path(DEFAULT_L3_ROOT)
    hours = available_asset_hours(l3_root)
    epilog = "available asset-hours (date hour asset-prefix bytes):\n"
    epilog += "\n".join(
        f"  {r['date']}  {r['hour']}  {r['asset'][:16]}..  {r['bytes']:,}" for r in hours
    ) or "  (none)"
    parser = build_parser(epilog)
    args = parser.parse_args(argv)

    if args.list:
        log.info(epilog)
        return 0

    started = time.time()
    slice_info = resolve_asset_hour(Path(args.l3_root), args.date, args.hour, args.asset)
    date, hour, asset = slice_info["date"], slice_info["hour"], slice_info["asset"]
    log.info(f"asset-hour {date} h{hour} asset {asset[:16]}.. ({slice_info['bytes']:,} bytes)")

    episodes, condition_id = load_episodes(Path(args.tape), date, hour, asset)
    levels = load_levels(slice_info["path"])
    log.info(f"{len(episodes)} episodes, {len(levels)} L3 levels, market {condition_id}")

    tape_tick = load_tick_size(Path(args.tape), date, hour, asset)
    if args.tick is not None:
        tick, tick_source = float(args.tick), "--tick"
    elif tape_tick is not None:
        tick, tick_source = tape_tick, "tick_size_change in the hour"
    else:
        inferred = infer_tick(load_prices(slice_info["path"]))
        if inferred is not None:
            tick, tick_source = inferred, "inferred from the L3 price grid"
        else:
            tick, tick_source = DEFAULT_TICK, "default"
    log.info(f"tick {tick:g} ({tick_source})")

    market = load_market(Path(args.manifest), condition_id, asset)
    order_size = args.size or (float(market["min_size"]) if market and market.get("min_size") else 100.0)

    when = datetime.fromtimestamp(episodes[0].start_ns / 1e9, tz=timezone.utc)
    schedule, fee_type = load_fee_schedule(Path(args.snapshot), condition_id, when)
    gas_gwei = load_median_gas_gwei(Path(args.tape), date, hour)
    gas_usd: Optional[float] = None
    if gas_gwei is not None:
        gas_usd = estimate_merge_cost_usd(
            pol_usd_price=float(args.pol_usd),
            gas_units=MERGE_GAS_UNITS,
            gas_price_gwei=gas_gwei,
        )

    cfg = QuoteConfig(
        sides=args.sides,
        distance=round(args.distance_ticks * tick, 10),
        tick=tick,
        order_size=order_size,
        horizon_ns=int(args.horizon_s * 1e9),
        markout_tolerance_ns=int(args.markout_tolerance_s * 1e9),
        merge_usd=float(gas_usd) if (args.merge and gas_usd is not None) else 0.0,
    )
    mid_ts = [e.start_ns for e in episodes]
    mid_val = [e.mid for e in episodes]
    result = simulate(episodes, levels, mid_ts, mid_val, cfg)
    stats = summarise(result, min_fills=args.min_fills)

    taker_ref: Optional[float] = None
    if schedule is not None and result["rows"]:
        first = result["rows"][0]
        if first["shares"] > 0:
            taker_ref = taker_fee(first["shares"], first["quote_price"], schedule) / first["shares"]

    text = report(
        slice_info=slice_info,
        market=market,
        condition_id=condition_id,
        episodes=episodes,
        stats=stats,
        cfg=cfg,
        fee_type=fee_type,
        schedule=schedule,
        gas_gwei=gas_gwei,
        gas_usd=gas_usd,
        taker_ref=taker_ref,
        min_fills=args.min_fills,
        tick_source=tick_source,
    )
    log.info(text)

    if result["rows"]:
        log.info("")
        log.info(f"per-fill rows ({min(args.limit_rows, len(result['rows']))} of {len(result['rows'])}):")
        log.info("      side     price    shares      fill_ts        gross     markout         net")
        for row in result["rows"][: args.limit_rows]:
            log.info(
                f"  {row['side']:>5}  {row['quote_price']:.4f}  {row['shares']:8.2f}"
                f"  {_utc(row['fill_ts_ns'])}"
                f"  {_fmt(row['gross_spread'], dp=4, width=9)}"
                f"  {_fmt(row['markout'], dp=4, width=10)}"
                f"  {_fmt(row['net_per_share'], dp=4, width=10)}"
            )

    if not args.no_write:
        stem = f"quote_backtest_{date}T{hour}_{asset}"
        parquet_path, text_path = write_outputs(result["rows"], Path(args.out), stem, text)
        log.info("")
        log.info(f"wrote {parquet_path} ({len(result['rows'])} rows)")
        log.info(f"wrote {text_path}")
    log.info(f"done in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
