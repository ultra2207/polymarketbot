#!/usr/bin/env python3
"""Measure how long after a Binance BBO move the linked Polymarket book moves.

What it measures
----------------
Two legs are timed against the same Binance ticks.

*Quote leg.* For every Binance ``bookTicker`` tick whose best bid or best ask
changed, find the first top-of-book change on the Polymarket asset(s) linked to
that symbol *after* the tick, within ``--max-gap-ms``. The quote series is built
from the nested ``price_changes`` of the ``price_change`` event (each change
carries the resulting ``best_bid``/``best_ask`` for its own asset id) unioned
with the few ``best_bid_ask`` frames that exist: that stream covers only a small
part of the universe in recent hours and none of it after hour 13.

*Taker leg.* Find the first ``clob_trades`` print on the same linked tokens
after the tick, with the same linkage and gap. This is the median taker snipe
Gate 3 asks about.

The statistic is the reaction gap

    reaction = t_clob(venue clock) - t_binance(venue clock)

for each leg. ``lead = taker_p50 - quote_p50`` is how much later a taker
actually fills than the book moves.

Verdict
-------
The lead is a difference of two CLOB venue timestamps against the same Binance
tick, so the cross-venue clock correction cancels in it. ``PASS`` when the
lead's 95 % bootstrap lower bound reaches the gate (or, with ``--our-path-ms``,
when ``taker - our_path`` clears the gate net of half the clock bracket);
``INCONCLUSIVE`` when the relevant interval crosses it; ``NO_DATA`` when either
leg has no matched observation. The clock bracket is printed for the absolute
reactions, not used to bound the lead.

What it cannot prove
--------------------
The reaction gap is only known to within the two venues' offset brackets:
``(rtt_binance + rtt_clob) / 2`` plus the timestamp quantisation of each
endpoint. ``/time`` on the CLOB answers in whole seconds, so a single probe is
worth 1 s; intersecting several probes narrows it, but the measured bracket on
this host is still ~360 ms wide in relative terms, several times the 100 ms
being tested. Gate 3's "100 ms faster than the median taker snipe" is therefore
**not provable from this tape** while the bracket stays that wide: the verdict
is ``INCONCLUSIVE`` and the report prints the bracket next to the point lead.
The corrected distribution carries its low/high edges, and the same statistic
is computed for book moves that are deliberately unrelated to the Binance tick
(``--placebo``) so a reader can see the noise floor. These are bounds, not a
proof.

Usage
-----
    # record fresh offsets first, then measure the latest complete hour
    .venv/Scripts/python.exe scripts/measure_lead_time.py --probe --count 40

    .venv/Scripts/python.exe scripts/measure_lead_time.py --hour 20261104T01 --minutes 10

    # explicit links when the reward-filtered universe has no crypto markets
    .venv/Scripts/python.exe scripts/measure_lead_time.py \
        --link BTCUSDT=<token_id>,<token_id> --link ETHUSDT=<token_id>
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import duckdb  # noqa: E402

from polymarketbot.logs import IST, get_logger  # noqa: E402
from polymarketbot.net import install_doh_fallback  # noqa: E402
from polymarketbot.netclock import (  # noqa: E402
    ClockSample,
    best_sample,
    load_samples,
    offset_bracket,
    probe,
    record_samples,
)

log = get_logger("measure_lead_time")

DEFAULT_ROOT = "data/tape"
DEFAULT_MINUTES = 15.0
DEFAULT_MAX_GAP_MS = 2000.0
DEFAULT_PLACEBO_SECONDS = 30.0
DEFAULT_LIMIT = 500_000
GATE_MS = 100.0
# Verdict labels. PASS is only reachable when the clock bracket is narrower
# than the gate; a point estimate below the gate is a FAIL even inside a wide
# bracket, because no correction of that size rescues a lead that small.
PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
NO_DATA = "NO_DATA"

# Which Polymarket markets a Binance symbol is allowed to lead. Crypto
# up/down markets are named after the coin, not the pair, so the question text
# is the only link the tape carries. Word boundaries keep "whether" from
# matching "eth".
SYMBOL_QUESTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "BTCUSDT": (r"\bbitcoin\b", r"\bbtc\b"),
    "ETHUSDT": (r"\bethereum\b", r"\beth\b"),
    "SOLUSDT": (r"\bsolana\b", r"\bsol\b"),
    "XRPUSDT": (r"\bxrp\b", r"\bripple\b"),
    "DOGEUSDT": (r"\bdogecoin\b", r"\bdoge\b"),
}


# ------------------------------------------------------------------ helpers
def _sql_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def _hours(root: Path, table: str) -> set[str]:
    """``YYYYMMDDTHH`` partitions present for ``table``."""
    out: set[str] = set()
    for path in root.glob(f"{table}/date=*/hour=*"):
        date_part = path.parent.name.split("=", 1)[1].replace("-", "")
        out.add(f"{date_part}T{path.name.split('=', 1)[1]}")
    return out


def _shared_hour(root: Path) -> str | None:
    """Newest hour that has parts for both tables."""
    common = _hours(root, "binance_bbo") & _hours(root, "clob_events")
    return max(common) if common else None


def _window_ns(hour_key: str, minutes: float, now_ns: int) -> tuple[int, int, str]:
    """Window inside one hour partition; returns (lo_ns, hi_ns, note)."""
    start = datetime.strptime(hour_key, "%Y%m%dT%H").replace(tzinfo=timezone.utc)
    lo_ns = int(start.timestamp() * 1e9)
    hi_ns = lo_ns + int(minutes * 60 * 1e9)
    note = f"{start.isoformat()} + {minutes:g} min"
    if hi_ns > now_ns:
        hi_ns = now_ns
        note += " (clamped to now: the hour is still being written)"
    return lo_ns, hi_ns, note


def _hour_end_ns(hour_key: str) -> int:
    start = datetime.strptime(hour_key, "%Y%m%dT%H").replace(tzinfo=timezone.utc)
    return int(start.timestamp() * 1e9) + 3_600 * 1_000_000_000


def _iso_ns(text: Any) -> int | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp() * 1e9)


def _manifest_for_window(root: Path, lo_ns: int) -> Path:
    """Newest versioned universe snapshot generated at or before the window.

    A historical hour must be linked against the universe that was actually
    subscribed then. `manifest_current.json` is today's cut; using it on a
    12-hour-old window links five-minute crypto markets that have since
    expired and produces an empty quote leg.
    """
    best: tuple[int, Path] | None = None
    for path in (root / "universe").glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        ts = _iso_ns(payload.get("generated_at")) if isinstance(payload, dict) else None
        if ts is None or ts > lo_ns:
            continue
        if best is None or ts > best[0]:
            best = (ts, path)
    return best[1] if best else root / "manifest_current.json"


def _parse_links(values: Iterable[str]) -> dict[str, set[str]]:
    links: dict[str, set[str]] = {}
    for value in values:
        symbol, _, tokens = value.partition("=")
        symbol = symbol.strip().upper()
        if not symbol or not tokens:
            raise SystemExit(f"--link expects SYMBOL=TOKEN[,TOKEN], got {value!r}")
        links.setdefault(symbol, set()).update(t.strip() for t in tokens.split(",") if t.strip())
    return links


def _manifest_links(path: Path) -> dict[str, set[str]]:
    """Link symbols to token ids by matching the market's question text."""
    if not path.exists():
        log.warning(f"no manifest at {path}; only --link mappings are used")
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning(f"manifest {path} unreadable ({exc}); only --link mappings are used")
        return {}
    markets = manifest.get("markets", manifest if isinstance(manifest, list) else [])
    links: dict[str, set[str]] = {}
    for market in markets:
        question = str(market.get("question") or "").lower()
        tokens = market.get("clob_tokens") or []
        for symbol, patterns in SYMBOL_QUESTION_PATTERNS.items():
            if any(re.search(pattern, question) for pattern in patterns):
                links.setdefault(symbol, set()).update(str(t) for t in tokens)
    return links


def _first_after(times: list[int], start_ns: int, max_gap_ns: int) -> int | None:
    """First CLOB move at or after ``start_ns`` within ``max_gap_ns``."""
    index = bisect_left(times, start_ns)
    if index < len(times) and times[index] - start_ns <= max_gap_ns:
        return times[index]
    return None


def _quantile(sorted_values: list[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def _stats(
    values_ns: list[int],
    *,
    shift_ns: int = 0,
    low_shift_ns: int | None = None,
    high_shift_ns: int | None = None,
) -> dict[str, Any]:
    """Distribution summary in milliseconds, plus the 100 ms threshold share.

    ``shift_ns`` is the point clock correction. ``low_shift_ns`` and
    ``high_shift_ns`` are the pessimistic and optimistic ends of the correction
    interval; they turn the threshold share into a range instead of pretending
    the correction is exact.
    """
    if not values_ns:
        return {"n": 0}
    shifted = sorted((value + shift_ns) / 1e6 for value in values_ns)

    def share_after(threshold_ms: float, extra_ns: int = 0) -> float:
        hits = sum(1 for value in values_ns if value + shift_ns + extra_ns > threshold_ms * 1e6)
        return round(hits / len(values_ns), 4)

    out: dict[str, Any] = {
        "n": len(shifted),
        "p10_ms": round(_quantile(shifted, 0.10), 1),
        "p50_ms": round(_quantile(shifted, 0.50), 1),
        "p90_ms": round(_quantile(shifted, 0.90), 1),
        "min_ms": round(shifted[0], 1),
        "max_ms": round(shifted[-1], 1),
        "share_after_gate": share_after(GATE_MS),
    }
    if low_shift_ns is not None and high_shift_ns is not None:
        out["share_after_gate_low"] = share_after(
            GATE_MS, low_shift_ns - shift_ns
        )
        out["share_after_gate_high"] = share_after(
            GATE_MS, high_shift_ns - shift_ns
        )
        out["lead_bounds_ms"] = [
            round(low_shift_ns / 1e6, 1),
            round(high_shift_ns / 1e6, 1),
        ]
    return out


# ------------------------------------------------------------------- queries
def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 2")
    Path(".tmp/duckdb_spill").mkdir(parents=True, exist_ok=True)
    con.execute("SET temp_directory = '.tmp/duckdb_spill'")
    return con


def _binance_moves(
    con: duckdb.DuckDBPyConnection,
    glob: str,
    lo_ns: int,
    hi_ns: int,
    symbols: list[str],
) -> dict[str, list[int]]:
    """Per symbol, the venue time of every BBO change in the window."""
    rows = con.execute(
        f"""
        WITH b AS (
            SELECT symbol, venue_ts_ms, recv_mono_ns, bid, ask,
                   lag(bid) OVER w AS prev_bid,
                   lag(ask) OVER w AS prev_ask
            FROM read_parquet('{glob}')
            WHERE recv_ts_ns BETWEEN ? AND ? AND symbol IN ({",".join("?" * len(symbols))})
            WINDOW w AS (PARTITION BY symbol ORDER BY recv_mono_ns)
        )
        SELECT symbol, venue_ts_ms
        FROM b
        WHERE prev_bid IS NOT NULL
          AND (bid IS DISTINCT FROM prev_bid OR ask IS DISTINCT FROM prev_ask)
        ORDER BY symbol, recv_mono_ns
        """,
        [lo_ns, hi_ns, *symbols],
    ).fetchall()
    out: dict[str, list[int]] = {}
    for symbol, venue_ts_ms in rows:
        out.setdefault(symbol, []).append(int(venue_ts_ms) * 1_000_000)
    return out


def _quote_moves(
    con: duckdb.DuckDBPyConnection,
    glob: str,
    lo_ns: int,
    hi_ns: int,
    token_to_symbol: dict[str, str],
) -> tuple[dict[str, list[int]], dict[str, int]]:
    """Per symbol, the venue time of every top-of-book change on its tokens.

    The ``best_bid_ask`` stream covers only a small part of the universe in
    recent hours, so the series is built from the nested ``price_changes`` of the
    ``price_change`` event — each element carries the resulting
    ``best_bid``/``best_ask`` for its own asset id — unioned with whatever
    ``best_bid_ask`` frames exist. A ``price_change`` frame that carries both
    tokens of a market has a NULL row-level ``asset_id``, which is why the token
    is read from the element. Both streams are ordered by ``recv_mono_ns`` so the
    lag comparison runs against one interleaved series per token.

    Returns ``(moves, observations)``. The observation count separates "this
    token never appears in the window" from "it appears, but its recorded top of
    book never changes" — two very different reasons for an empty series.
    """
    tokens = sorted(token_to_symbol)
    marks = ",".join("?" * len(tokens))
    rows = con.execute(
        f"""
        WITH pc AS (
            SELECT c.asset_id AS asset_id,
                   e.venue_ts_ms AS venue_ts_ms,
                   e.recv_mono_ns AS recv_mono_ns,
                   c.best_bid AS best_bid,
                   c.best_ask AS best_ask
            FROM read_parquet('{glob}') e, unnest(e.price_changes) AS t(c)
            WHERE e.event_type = 'price_change'
              AND e.recv_ts_ns BETWEEN ? AND ?
              AND c.asset_id IN ({marks})
              AND c.best_bid IS NOT NULL AND c.best_ask IS NOT NULL
              AND e.venue_ts_ms IS NOT NULL
        ), bbo AS (
            SELECT asset_id, venue_ts_ms, recv_mono_ns, best_bid, best_ask
            FROM read_parquet('{glob}')
            WHERE event_type = 'best_bid_ask'
              AND recv_ts_ns BETWEEN ? AND ?
              AND asset_id IN ({marks})
              AND best_bid IS NOT NULL AND best_ask IS NOT NULL
              AND venue_ts_ms IS NOT NULL
        ), q AS (
            SELECT * FROM pc UNION ALL SELECT * FROM bbo
        ), chg AS (
            SELECT asset_id, venue_ts_ms, best_bid, best_ask,
                   lag(best_bid) OVER w AS prev_bid,
                   lag(best_ask) OVER w AS prev_ask
            FROM q
            WINDOW w AS (PARTITION BY asset_id ORDER BY recv_mono_ns)
        )
        SELECT asset_id, venue_ts_ms,
               prev_bid IS NOT NULL
               AND (best_bid IS DISTINCT FROM prev_bid
                    OR best_ask IS DISTINCT FROM prev_ask) AS is_move
        FROM chg
        """,
        [lo_ns, hi_ns, *tokens, lo_ns, hi_ns, *tokens],
    ).fetchall()
    out: dict[str, list[int]] = {}
    observations: dict[str, int] = {}
    for asset_id, venue_ts_ms, is_move in rows:
        symbol = token_to_symbol[asset_id]
        observations[symbol] = observations.get(symbol, 0) + 1
        if not is_move:
            continue
        out.setdefault(symbol, []).append(int(venue_ts_ms) * 1_000_000)
    for times in out.values():
        times.sort()
    return out, observations


def _trade_moves(
    con: duckdb.DuckDBPyConnection,
    glob: str,
    lo_ns: int,
    hi_ns: int,
    token_to_symbol: dict[str, str],
) -> dict[str, list[int]]:
    """Per symbol, the venue time of every taker print on its tokens."""
    tokens = sorted(token_to_symbol)
    try:
        rows = con.execute(
            f"""
            SELECT asset_id, venue_ts_ms
            FROM read_parquet('{glob}')
            WHERE recv_ts_ns BETWEEN ? AND ?
              AND asset_id IN ({",".join("?" * len(tokens))})
              AND venue_ts_ms IS NOT NULL
            """,
            [lo_ns, hi_ns, *tokens],
        ).fetchall()
    except duckdb.Error as exc:
        log.warning(f"clob_trades unreadable for this window ({exc}); taker leg is empty")
        return {}
    out: dict[str, list[int]] = {}
    for asset_id, venue_ts_ms in rows:
        out.setdefault(token_to_symbol[asset_id], []).append(int(venue_ts_ms) * 1_000_000)
    for times in out.values():
        times.sort()
    return out


# ------------------------------------------------------------------ report
def _reactions(
    binance_ns: list[int], clob_ns: list[int], max_gap_ns: int, delay_ns: int = 0
) -> list[int]:
    """Wait from each measured instant to the first CLOB move after it.

    With ``delay_ns=0`` the instant is the Binance move itself, so this is the
    reaction gap. With a delay it is the wait from an arbitrary later instant,
    which is the noise floor: how long a random moment waits for any book move.
    """
    out: list[int] = []
    for move in binance_ns:
        start_ns = move + delay_ns
        hit = _first_after(clob_ns, start_ns, max_gap_ns)
        if hit is not None:
            out.append(hit - start_ns)
    return out


def _p50_ms(values_ns: list[int], shift_ns: int = 0) -> float | None:
    if not values_ns:
        return None
    shifted = sorted((value + shift_ns) / 1e6 for value in values_ns)
    return round(_quantile(shifted, 0.50), 1)


def _bootstrap_median_ci(
    values_ns: list[int], *, resamples: int = 400, seed: int = 7
) -> tuple[float, float] | None:
    """95 % bootstrap interval for the median, in ms.

    This is the lead's own sampling uncertainty. The cross-venue clock
    correction is common to both legs and therefore cancels in the lead; it
    must not be used as the lead's error bar.
    """
    if not values_ns:
        return None
    rng = random.Random(seed)
    n = len(values_ns)
    medians: list[float] = []
    for _ in range(resamples):
        draw = sorted(values_ns[rng.randrange(n)] for _ in range(n))
        medians.append(_quantile(draw, 0.5) / 1e6)
    medians.sort()
    return medians[int(0.025 * resamples)], medians[int(0.975 * resamples) - 1]


def _lead_verdict(
    quote_ns: list[int],
    taker_ns: list[int],
    bracket_ms: Iterable[float] | None,
    *,
    shift_ns: int = 0,
    our_path_ms: float | None = None,
    gate_ms: float = GATE_MS,
) -> dict[str, Any]:
    """Gate 3 verdict.

    ``lead = taker_p50 - quote_p50``. Both legs are shifted by the same clock
    correction, so the correction cancels in the lead; the bracket is the
    absolute-reaction uncertainty and is reported, never used to bound the lead.

    Without ``our_path_ms`` the verdict tests the lead itself: PASS when the
    lead's 95 % bootstrap lower bound reaches the gate, INCONCLUSIVE when the
    sampling interval crosses it. With ``our_path_ms`` (our measured
    signal-to-engine path) the verdict tests the Gate 3 comparison directly,
    ``taker_p50 - our_path_ms >= gate``, subtracting half the clock bracket
    from the lower bound because the taker's absolute reaction carries it.
    """
    quote_p50 = _p50_ms(quote_ns, shift_ns)
    taker_p50 = _p50_ms(taker_ns, shift_ns)
    bracket = [round(float(value), 1) for value in bracket_ms] if bracket_ms else None
    width = round(bracket[1] - bracket[0], 1) if bracket else None
    out: dict[str, Any] = {
        "gate_ms": gate_ms,
        "quote_p50_ms": quote_p50,
        "taker_p50_ms": taker_p50,
        "lead_ms": None,
        "lead_ci_ms": None,
        "bracket_ms": bracket,
        "bracket_width_ms": width,
        "our_path_ms": our_path_ms,
        "margin_ms": None,
        "verdict": NO_DATA,
    }
    if quote_p50 is None or taker_p50 is None:
        return out
    lead = round(taker_p50 - quote_p50, 1)
    out["lead_ms"] = lead
    quote_ci = _bootstrap_median_ci(quote_ns)
    taker_ci = _bootstrap_median_ci(taker_ns)
    lower = lead
    if quote_ci and taker_ci:
        ci = [round(taker_ci[0] - quote_ci[1], 1), round(taker_ci[1] - quote_ci[0], 1)]
        out["lead_ci_ms"] = ci
        lower = ci[0]
    if our_path_ms is not None:
        margin = round(taker_p50 - our_path_ms, 1)
        out["margin_ms"] = margin
        half_bracket = (width or 0.0) / 2.0
        taker_low = (taker_ci[0] + shift_ns / 1e6) if taker_ci else taker_p50
        lower = taker_low - our_path_ms - half_bracket
        if margin < gate_ms:
            out["verdict"] = FAIL
        elif lower >= gate_ms:
            out["verdict"] = PASS
        else:
            out["verdict"] = INCONCLUSIVE
        return out
    if lead < gate_ms:
        out["verdict"] = FAIL
    elif lower >= gate_ms:
        out["verdict"] = PASS
    else:
        out["verdict"] = INCONCLUSIVE
    return out


def _window_counts(
    con: duckdb.DuckDBPyConnection,
    binance_glob: str,
    clob_glob: str,
    lo_ns: int,
    hi_ns: int,
    symbols: list[str],
    tokens: list[str],
) -> tuple[int, int]:
    """(binance frames, clob quote frames) in the window; the emptiness guard.

    The clob side counts ``price_change`` frames for every market — a
    ``price_change`` row carries a NULL ``asset_id`` when it moves both tokens of
    a market, so it cannot be filtered by token before the unnest. Whether the
    *linked* tokens actually moved is decided by ``_quote_moves``.
    """
    binance = con.execute(
        f"SELECT count(*) FROM read_parquet('{binance_glob}') "
        f"WHERE recv_ts_ns BETWEEN ? AND ? AND symbol IN ({','.join('?' * len(symbols))})",
        [lo_ns, hi_ns, *symbols],
    ).fetchone()[0]
    quote = con.execute(
        f"SELECT count(*) FROM read_parquet('{clob_glob}') "
        f"WHERE recv_ts_ns BETWEEN ? AND ? "
        f"AND (event_type = 'price_change' "
        f"OR (event_type = 'best_bid_ask' AND asset_id IN ({','.join('?' * len(tokens))})))",
        [lo_ns, hi_ns, *tokens],
    ).fetchone()[0]
    return int(binance), int(quote)


def _anchor_window(
    con: duckdb.DuckDBPyConnection,
    binance_glob: str,
    clob_glob: str,
    lo_ns: int,
    hi_ns: int,
    symbols: list[str],
    tokens: list[str],
    hour_end_ns: int,
) -> tuple[int, int] | None:
    """Slide a window that starts before either stream wrote this hour.

    The recorder joins mid-hour, so the hour partition routinely starts after
    ``:00``. Anchoring at the later of the two first rows keeps ``--minutes``
    meaning "minutes of recorded data" instead of "minutes of wall clock that
    are mostly empty".
    """
    row = con.execute(
        f"""
        SELECT max(start_ns) FROM (
            SELECT min(recv_ts_ns) AS start_ns FROM read_parquet('{binance_glob}')
            WHERE recv_ts_ns >= ? AND recv_ts_ns < ?
              AND symbol IN ({",".join("?" * len(symbols))})
            UNION ALL
            SELECT min(recv_ts_ns) FROM read_parquet('{clob_glob}')
            WHERE recv_ts_ns >= ? AND recv_ts_ns < ?
              AND (event_type = 'price_change'
                   OR (event_type = 'best_bid_ask'
                       AND asset_id IN ({",".join("?" * len(tokens))})))
        )
        """,
        [lo_ns, hour_end_ns, *symbols, lo_ns, hour_end_ns, *tokens],
    ).fetchone()
    start_ns = row[0] if row else None
    if start_ns is None or start_ns <= lo_ns:
        return None
    span_ns = hi_ns - lo_ns
    new_hi = min(start_ns + span_ns, hour_end_ns, time.time_ns())
    if new_hi <= start_ns:
        return None
    return int(start_ns), new_hi


def _measure(
    root: Path,
    hour_key: str,
    lo_ns: int,
    hi_ns: int,
    token_to_symbol: dict[str, str],
    max_gap_ns: int,
    limit: int,
    offsets: dict[str, ClockSample],
    placebo_ns: int,
    our_path_ms: float | None = None,
) -> dict[str, Any]:
    symbols = sorted({token_to_symbol[token] for token in token_to_symbol})
    if not symbols:
        raise SystemExit("no linked assets: pass --link SYMBOL=TOKEN or use a manifest with crypto markets")
    date_part = f"{hour_key[:4]}-{hour_key[4:6]}-{hour_key[6:8]}"
    hour_part = hour_key[9:]
    tokens = sorted(token_to_symbol)
    con = _connect()
    try:
        binance_glob = _sql_path(root / "binance_bbo" / f"date={date_part}" / f"hour={hour_part}" / "*.parquet")
        clob_glob = _sql_path(root / "clob_events" / f"date={date_part}" / f"hour={hour_part}" / "*.parquet")
        trade_glob = _sql_path(root / "clob_trades" / f"date={date_part}" / f"hour={hour_part}" / "*.parquet")

        binance_count, quote_count = _window_counts(
            con, binance_glob, clob_glob, lo_ns, hi_ns, symbols, tokens
        )
        if binance_count == 0 or quote_count == 0:
            anchored = _anchor_window(
                con,
                binance_glob,
                clob_glob,
                lo_ns,
                hi_ns,
                symbols,
                tokens,
                _hour_end_ns(hour_key),
            )
            if anchored is not None:
                log.warning(
                    f"the requested window starts before anything was recorded in {hour_key}; "
                    f"slid it to {datetime.fromtimestamp(anchored[0] / 1e9, timezone.utc).isoformat()}"
                )
                lo_ns, hi_ns = anchored
                binance_count, quote_count = _window_counts(
                    con, binance_glob, clob_glob, lo_ns, hi_ns, symbols, tokens
                )
        log.info(f"window rows: binance_bbo={binance_count:,} clob quotes={quote_count:,}")
        if binance_count > limit:
            raise SystemExit(
                f"window has more than --limit {limit:,} binance rows; shrink --minutes "
                f"(binance {binance_count:,})"
            )
        if binance_count == 0 or quote_count == 0:
            raise SystemExit("window is empty for at least one source; nothing to measure")

        binance_moves = _binance_moves(con, binance_glob, lo_ns, hi_ns, symbols)
        quote_moves, quote_obs = _quote_moves(con, clob_glob, lo_ns, hi_ns, token_to_symbol)
        trade_moves = _trade_moves(con, trade_glob, lo_ns, hi_ns, token_to_symbol)
    finally:
        con.close()

    if not any(binance_moves.values()):
        raise SystemExit(
            f"no BBO move for {', '.join(symbols)} in the window; pass another --hour/--minutes"
        )
    if not any(quote_moves.values()):
        log.warning(
            "no top-of-book change on the linked tokens in this window "
            f"({sum(quote_obs.values()):,} quote observations, all unchanged): the linked "
            "markets are dormant or the recorded top of book is pinned; the quote leg is empty"
        )

    binance_samples = offsets.get("binance") or []
    clob_samples = offsets.get("clob") or []
    synchronised = bool(binance_samples) and bool(clob_samples)
    brackets = {venue: offset_bracket(samples) for venue, samples in offsets.items() if samples}
    shift_ns = shift_low_ns = shift_high_ns = 0
    bracket_ms: list[float] | None = None
    if synchronised:
        # lead = (c - off_c) - (b - off_b) = (c - b) + (off_b - off_c)
        # The correction is the midpoint of the difference of the two brackets,
        # not the difference of the two lowest-rtt samples: a single sample's
        # point estimate can sit outside its own venue's intersection when the
        # endpoint truncates to whole seconds (which is exactly the CLOB case).
        shift_low_ns = brackets["binance"][0] - brackets["clob"][1]
        shift_high_ns = brackets["binance"][1] - brackets["clob"][0]
        shift_ns = (shift_low_ns + shift_high_ns) // 2
        bracket_ms = [shift_low_ns / 1e6, shift_high_ns / 1e6]

    per_symbol: dict[str, Any] = {}
    pooled: list[int] = []
    pooled_taker: list[int] = []
    pooled_placebo: list[int] = []
    total_moves = 0

    def corrected(values: list[int]) -> dict[str, Any] | None:
        if not synchronised:
            return None
        return _stats(
            values,
            shift_ns=shift_ns,
            low_shift_ns=shift_low_ns,
            high_shift_ns=shift_high_ns,
        )

    for symbol in symbols:
        moves = binance_moves.get(symbol, [])
        quotes = quote_moves.get(symbol, [])
        trades = trade_moves.get(symbol, [])
        reactions = _reactions(moves, quotes, max_gap_ns)
        taker = _reactions(moves, trades, max_gap_ns)
        placebo = _reactions(moves, quotes, max_gap_ns, placebo_ns) if placebo_ns else []
        total_moves += len(moves)
        pooled.extend(reactions)
        pooled_taker.extend(taker)
        pooled_placebo.extend(placebo)
        per_symbol[symbol] = {
            "binance_moves": len(moves),
            "quote_observations": quote_obs.get(symbol, 0),
            "quote_moves": len(quotes),
            "trades": len(trades),
            "matched": len(reactions),
            "taker_matched": len(taker),
            "raw": _stats(reactions),
            "corrected": corrected(reactions),
            "taker": _stats(taker, shift_ns=shift_ns),
            "lead": _lead_verdict(
                reactions, taker, bracket_ms, shift_ns=shift_ns, our_path_ms=our_path_ms
            ),
            "placebo": corrected(placebo) if placebo else None,
        }
        stats = per_symbol[symbol]["corrected"] or per_symbol[symbol]["raw"]
        log.info(
            f"{symbol}: binance_moves={len(moves):,} quote_obs={quote_obs.get(symbol, 0):,} "
            f"quote_moves={len(quotes):,} matched={len(reactions):,} taker={len(taker):,} "
            f"p50={stats.get('p50_ms')} ms p90={stats.get('p90_ms')} ms"
        )

    report: dict[str, Any] = {
        "generated_at": datetime.now(IST).isoformat(),
        "window": {"hour": hour_key, "start_ns": lo_ns, "end_ns": hi_ns},
        "max_gap_ms": max_gap_ns / 1e6,
        "placebo_delay_ms": placebo_ns / 1e6 if placebo_ns else None,
        "clock": {
            "samples": {
                venue: {
                    "n": len(samples),
                    "best_offset_ms": round(best_sample(samples).offset_ns / 1e6, 1),
                    "best_rtt_ms": round(best_sample(samples).rtt_ns / 1e6, 1),
                    "bracket_ms": [
                        round(brackets[venue][0] / 1e6, 1),
                        round(brackets[venue][1] / 1e6, 1),
                    ],
                    "newest_age_s": round(
                        (time.time_ns() - max(s.recv_ts_ns for s in samples)) / 1e9, 1
                    ),
                }
                for venue, samples in offsets.items()
                if samples
            },
            "correction_ms": round(shift_ns / 1e6, 1),
            "correction_bracket_ms": [round(value, 1) for value in bracket_ms]
            if bracket_ms
            else None,
            "synchronised": synchronised,
        },
        "symbols": per_symbol,
        "pooled": {
            "binance_moves": total_moves,
            "quote_observations": sum(quote_obs.values()),
            "quote_moves": sum(len(values) for values in quote_moves.values()),
            "matched": len(pooled),
            "unmatched": total_moves - len(pooled),
            "taker_matched": len(pooled_taker),
            "raw": _stats(pooled),
            "corrected": corrected(pooled),
            "taker": _stats(pooled_taker, shift_ns=shift_ns),
            "lead": _lead_verdict(
                pooled, pooled_taker, bracket_ms, shift_ns=shift_ns, our_path_ms=our_path_ms
            ),
            "placebo": corrected(pooled_placebo) if pooled_placebo else None,
        },
    }
    return report


def _lead_line(lead: dict[str, Any]) -> str:
    """One line: point lead, then the bracket the verdict was resolved against."""
    bracket = lead["bracket_ms"]
    bracket_text = (
        f"bracket=[{bracket[0]}, {bracket[1]}] ms (width {lead['bracket_width_ms']} ms)"
        if bracket
        else "bracket=none"
    )
    if lead.get("margin_ms") is not None:
        bracket_text += (
            f" margin={lead['margin_ms']} ms (our path {lead['our_path_ms']} ms)"
        )
    quote = "n/a" if lead["quote_p50_ms"] is None else f"{lead['quote_p50_ms']} ms"
    taker = "n/a" if lead["taker_p50_ms"] is None else f"{lead['taker_p50_ms']} ms"
    delta = "n/a" if lead["lead_ms"] is None else f"{lead['lead_ms']} ms"
    return (
        f"quote_p50={quote} taker_p50={taker} lead={delta} "
        f"{bracket_text} verdict={lead['verdict']}"
    )


def _print_report(report: dict[str, Any]) -> None:
    window = report["window"]
    log.info(
        "measured window: "
        f"{datetime.fromtimestamp(window['start_ns'] / 1e9, timezone.utc).isoformat()} .. "
        f"{datetime.fromtimestamp(window['end_ns'] / 1e9, timezone.utc).isoformat()} "
        f"({window['hour']})"
    )
    clock = report["clock"]
    bracket = clock["correction_bracket_ms"]
    bracket_text = f" bracket={bracket} ms" if bracket else ""
    log.info(
        f"clock: correction={clock['correction_ms']} ms{bracket_text} "
        f"synchronised={clock['synchronised']}"
    )
    for venue, row in clock["samples"].items():
        log.info(
            f"  {venue}: n={row['n']} offset={row['best_offset_ms']} ms "
            f"rtt={row['best_rtt_ms']} ms bracket={row['bracket_ms']} ms "
            f"age={row['newest_age_s']} s"
        )
        if row["newest_age_s"] > 3_600:
            log.warning(
                f"  {venue} offsets are {row['newest_age_s'] / 3600:.1f} h old; re-probe with "
                "--probe before trusting the bracket"
            )
    for symbol, row in report["symbols"].items():
        log.info(
            f"{symbol}: binance={row['binance_moves']:,} quote_obs={row['quote_observations']:,} "
            f"quote_moves={row['quote_moves']:,} matched={row['matched']:,} "
            f"taker_matched={row['taker_matched']:,}"
        )
        stats = row["corrected"] or row["raw"]
        quote_line = (
            f"  quote reaction: n={stats.get('n', 0):,} p10={stats.get('p10_ms')} ms "
            f"p50={stats.get('p50_ms')} ms p90={stats.get('p90_ms')} ms"
        )
        log.info(quote_line)
        taker = row["taker"]
        log.info(
            f"  taker reaction: n={taker.get('n', 0):,} p50={taker.get('p50_ms')} ms "
            f"p90={taker.get('p90_ms')} ms"
        )
        log.info(f"  {_lead_line(row['lead'])}")

    pooled = report["pooled"]
    log.info(
        f"pooled moves: binance={pooled['binance_moves']:,} matched={pooled['matched']:,} "
        f"unmatched={pooled['unmatched']:,} (no CLOB top-of-book move within {report['max_gap_ms']:g} ms)"
    )
    for label in ("raw", "corrected"):
        stats = pooled.get(label)
        if not stats or not stats.get("n"):
            continue
        line = (
            f"pooled {label}: n={stats['n']:,} p10={stats['p10_ms']} ms p50={stats['p50_ms']} ms "
            f"p90={stats['p90_ms']} ms share_after_{GATE_MS:g}ms={stats['share_after_gate']}"
        )
        if "share_after_gate_low" in stats:
            line += (
                f" (over the correction bracket: {stats['share_after_gate_low']} .. "
                f"{stats['share_after_gate_high']})"
            )
        log.info(line)
    log.info(
        f"pooled taker: n={pooled['taker_matched']:,} "
        f"p50={pooled['taker'].get('p50_ms')} ms"
    )

    log.info(f"pooled {_lead_line(pooled['lead'])}")
    placebo = pooled.get("placebo")
    if placebo and placebo.get("n"):
        log.info(
            f"pooled placebo: n={placebo['n']:,} p50={placebo['p50_ms']} ms "
            f"share_after_{GATE_MS:g}ms={placebo['share_after_gate']}"
        )
    lead = pooled["lead"]
    if lead["verdict"] == INCONCLUSIVE:
        log.info(
            f"Gate 3 verdict {INCONCLUSIVE}: the lead {lead['lead_ms']} ms has a sampling "
            f"interval {lead['lead_ci_ms']} ms that crosses the {GATE_MS:g} ms gate"
        )
    elif lead["verdict"] == PASS:
        if lead.get("margin_ms") is not None:
            log.info(
                f"Gate 3 verdict {PASS}: lead {lead['lead_ms']} ms (CI {lead['lead_ci_ms']} ms), "
                f"margin {lead['margin_ms']} ms over our {lead['our_path_ms']} ms path; "
                f"clock bracket {lead['bracket_width_ms']} ms applies to the absolute reactions"
            )
        else:
            log.info(
                f"Gate 3 verdict {PASS}: lead {lead['lead_ms']} ms, sampling interval "
                f"{lead['lead_ci_ms']} ms"
            )
    elif lead["verdict"] == FAIL:
        log.info(
            f"Gate 3 verdict {FAIL}: the point lead {lead['lead_ms']} ms is below the "
            f"{GATE_MS:g} ms gate"
        )
    else:
        log.warning(
            f"Gate 3 verdict {NO_DATA}: {pooled['matched']:,} quote reactions and "
            f"{pooled['taker_matched']:,} taker reactions on the linked tokens — nothing to "
            "compare. The taker leg needs a linked market that actually trades"
        )


# --------------------------------------------------------------------- main
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--manifest", default="", help="default: <root>/manifest_current.json")
    ap.add_argument("--hour", default="", help="YYYYMMDDTHH; default: newest hour with both tables")
    ap.add_argument("--minutes", type=float, default=DEFAULT_MINUTES)
    ap.add_argument("--max-gap-ms", type=float, default=DEFAULT_MAX_GAP_MS)
    ap.add_argument("--placebo-seconds", type=float, default=DEFAULT_PLACEBO_SECONDS,
                    help="0 disables the unrelated-move baseline")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="refuse a window with more Binance frames than this")
    ap.add_argument("--link", action="append", default=[], metavar="SYMBOL=TOKEN[,TOKEN]")
    ap.add_argument("--probe", action="store_true", help="record fresh clock offsets first")
    ap.add_argument("--probe-only", action="store_true", help="record offsets and exit (implies --probe)")
    ap.add_argument("--count", type=int, default=20, help="probe rounds per venue (40+ tightens the bracket)")
    ap.add_argument(
        "--our-path-ms",
        type=float,
        default=None,
        help="measured signal-to-engine path (bench_cancel_path.py); tests the Gate 3 margin",
    )
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between probe rounds")
    ap.add_argument("--out", default="", help="JSON summary path; default under data/derived")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.root)
    started = time.monotonic()
    # The local resolver fails clob.polymarket.com intermittently; every
    # Polymarket client installs the DoH fallback explicitly (see net.py).
    install_doh_fallback()

    if args.probe or args.probe_only:
        samples = probe(count=args.count, interval_seconds=args.interval)
        path = record_samples(samples, root)
        for sample in samples:
            log.info(
                f"probe {sample.stream}: offset={sample.offset_ns / 1e6:+.1f} ms "
                f"rtt={sample.rtt_ns / 1e6:.1f} ms host={sample.host}"
            )
        counts: dict[str, int] = {}
        for sample in samples:
            counts[sample.stream] = counts.get(sample.stream, 0) + 1
        log.info(f"probe rounds recorded: {counts} -> {path}")
        if any(count < 3 for count in counts.values()):
            log.warning(f"fewer than 3 offsets for a venue: {counts}")
        if args.probe_only:
            return 0 if path and all(c >= 3 for c in counts.values()) else 1

    # A fresh probe is the only useful one: older parts carry a different
    # local-clock offset and poison the intersection.
    offsets = load_samples(root, max_parts=1)
    if not offsets:
        log.warning("no recorded clock offsets: reporting uncorrected gaps only")
    hour_key = args.hour or _shared_hour(root)
    if not hour_key:
        log.error(f"no hour partitions with both tables under {root}")
        return 2
    lo_ns, hi_ns, note = _window_ns(hour_key, args.minutes, time.time_ns())
    log.info(f"window {note} ({hour_key}); max_gap={args.max_gap_ms:g} ms")

    if args.manifest:
        manifest = Path(args.manifest)
    else:
        manifest = _manifest_for_window(root, lo_ns)
        log.info(f"universe manifest: {manifest.name}")
    links = _manifest_links(manifest)
    for symbol, tokens in _parse_links(args.link).items():
        links.setdefault(symbol, set()).update(tokens)
    token_to_symbol = {token: symbol for symbol, tokens in links.items() for token in tokens}
    log.info(
        "linked assets: "
        + ", ".join(f"{symbol}={len(tokens)}" for symbol, tokens in sorted(links.items()) if tokens)
    )
    if not token_to_symbol:
        log.error(
            "no linked assets: the reward-filtered universe has no market whose question names a "
            "Binance underlying. Pass --link SYMBOL=TOKEN[,TOKEN] for the assets to measure."
        )
        return 2

    report = _measure(
        root=root,
        hour_key=hour_key,
        lo_ns=lo_ns,
        hi_ns=hi_ns,
        token_to_symbol=token_to_symbol,
        max_gap_ns=int(args.max_gap_ms * 1e6),
        limit=args.limit,
        offsets=offsets,
        placebo_ns=int(args.placebo_seconds * 1e9),
        our_path_ms=args.our_path_ms,
    )
    _print_report(report)

    stamp = datetime.now(IST).strftime("%Y%m%dT%H%M%S")
    out = Path(args.out) if args.out else Path("data/derived/lead_time") / f"lead_time_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    log.info(f"wrote {out} in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
