#!/usr/bin/env python3
"""H2: does Kalshi taker flow *initiate* Polymarket trading?

Both trade tapes carry venue event timestamps at millisecond resolution, so the
lead-lag question is answered from the trades alone - no book reconstruction and
no dependence on the 2 s reference-book cadence the REST-only Kalshi access
forces on H1/H3.

For every Kalshi print on a mapped ticker the harness asks: how long until the
next Polymarket print on the paired condition, and does it agree in direction
(at the YES level)? The null is the same statistic at random times in the same
window, excluding the seconds around each Kalshi print, so a busy market cannot
manufacture a lead by itself.

Systematic prints and block prints are reported separately: block trades are
whale-sized and are the tier where information should be visible first.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.logs import get_logger

log = get_logger("report_kalshi_trade_lead")

DEFAULT_MAPPING = REPO_ROOT / "data" / "derived" / "kalshi_mapping_v2.parquet"
TAPE_ROOT = REPO_ROOT / "data" / "tape"
KALSHI_ROOT = TAPE_ROOT / "kalshi_trades"
CLOB_ROOT = TAPE_ROOT / "clob_trades"
DERIVED = REPO_ROOT / "data" / "derived" / "kalshi_trade_lead.parquet"
HORIZONS = (1.0, 5.0, 30.0)
NULL_GUARD_SECONDS = 5.0


def parse_iso_epoch(value: object) -> float | None:
    """Parse a Kalshi ``created_time`` to epoch seconds; ``None`` if unusable."""
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def kalshi_sign(taker_side: object, taker_book_side: object) -> int | None:
    """+1 when the taker bought YES, -1 when it bought NO, else ``None``.

    ``taker_side`` is the legacy field (``yes``/``no``); newer payloads carry
    ``taker_book_side`` (``YES``/``NO``). Buying NO is economically the same as
    selling YES, so both collapse onto one signed scale.
    """
    for value in (taker_side, taker_book_side):
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in {"yes", "bid", "buy", "y"}:
            return 1
        if text in {"no", "ask", "sell", "n"}:
            return -1
    return None


def pm_sign(side: object, *, yes_token: bool) -> int | None:
    """+1 for a taker buy of this token (at the YES level of the condition)."""
    if side is None:
        return None
    text = str(side).strip().lower()
    if text not in {"buy", "sell"}:
        return None
    direction = 1 if text == "buy" else -1
    return direction if yes_token else -direction


def next_latency(
    sorted_ts: np.ndarray, at: float, *, max_latency: float = 60.0
) -> float | None:
    """Seconds to the first print strictly after ``at``, censored at ``max_latency``."""
    idx = int(np.searchsorted(sorted_ts, at, side="right"))
    if idx >= len(sorted_ts):
        return None
    gap = float(sorted_ts[idx]) - float(at)
    if gap < 0 or gap > max_latency:
        return None
    return gap


def next_sign(
    sorted_ts: np.ndarray, sorted_signs: np.ndarray, at: float, *, max_latency: float = 60.0
):
    """The latency and the signed side of the first print after ``at``."""
    idx = int(np.searchsorted(sorted_ts, at, side="right"))
    if idx >= len(sorted_ts):
        return None, None
    gap = float(sorted_ts[idx]) - float(at)
    if gap < 0 or gap > max_latency:
        return None, None
    return gap, float(sorted_signs[idx])


def null_times(
    start: float,
    end: float,
    count: int,
    *,
    blocked: np.ndarray,
    guard: float = NULL_GUARD_SECONDS,
    seed: int = 7,
) -> np.ndarray:
    """Uniform random times in ``[start, end)`` kept ``guard`` seconds from ``blocked``.

    ``blocked`` is the sorted array of Kalshi print times; a null sample that
    lands inside a Kalshi burst would inherit its lead and inflate the null.
    """
    if end <= start or count <= 0:
        return np.empty(0)
    rng = np.random.default_rng(seed)
    drawn: list[float] = []
    attempts = 0
    while len(drawn) < count and attempts < count * 20 + 100:
        attempts += 1
        candidate = float(rng.uniform(start, end))
        if len(blocked):
            idx = int(np.searchsorted(blocked, candidate))
            near = [
                blocked[j]
                for j in (idx - 1, idx)
                if 0 <= j < len(blocked) and abs(float(blocked[j]) - candidate) <= guard
            ]
            if near:
                continue
        drawn.append(candidate)
    return np.asarray(drawn, dtype=float)


def reaction_rows(
    event_ts: Sequence[float],
    event_sign: Sequence[int],
    pm_ts: np.ndarray,
    pm_signs: np.ndarray,
    *,
    max_latency: float,
    block_flags: Sequence[bool] | None = None,
    sizes: Sequence[float] | None = None,
) -> list[dict[str, object]]:
    """Pair every Kalshi print with the next Polymarket print on its condition."""
    flags = list(block_flags) if block_flags is not None else [False] * len(event_ts)
    sizes = list(sizes) if sizes is not None else [float("nan")] * len(event_ts)
    rows: list[dict[str, object]] = []
    for ts, sign, is_block, size in zip(event_ts, event_sign, flags, sizes):
        latency, other = next_sign(pm_ts, pm_signs, float(ts), max_latency=max_latency)
        rows.append(
            {
                "latency_s": latency,
                "agree": (None if latency is None or other is None or sign == 0 else float(other) * float(sign) > 0),
                "event_sign": float(sign),
                "next_sign": (None if other is None else float(other)),
                "is_block": bool(is_block),
                "size": float(size),
            }
        )
    return rows


def agree_chance(events: Sequence[float], nexts: Sequence[float]) -> float | None:
    """Chance agreement of two independent sign sequences with these marginals.

    ``P(agree) = p(+)q(+) + p(-)q(-)``. Without this a market where most prints
    are YES-buys shows "agreement" above one half with no lead at all.
    """
    if not events or not nexts:
        return None
    p_up = float(np.mean([1.0 if value > 0 else 0.0 for value in events]))
    q_up = float(np.mean([1.0 if value > 0 else 0.0 for value in nexts]))
    return p_up * q_up + (1.0 - p_up) * (1.0 - q_up)


def summarize(rows: Iterable[dict[str, object]], *, horizon: float) -> dict[str, object]:
    """Counts, promptness and directional agreement inside one horizon."""
    materialised = list(rows)
    inside = [
        r
        for r in materialised
        if r["latency_s"] is not None and float(r["latency_s"]) <= horizon
    ]
    latencies = [float(r["latency_s"]) for r in inside]
    agreements = [bool(r["agree"]) for r in inside if r["agree"] is not None]
    event_signs = [
        float(r["event_sign"]) for r in inside if r.get("event_sign") is not None and r.get("next_sign") is not None
    ]
    next_signs = [
        float(r["next_sign"]) for r in inside if r.get("event_sign") is not None and r.get("next_sign") is not None
    ]
    chance = agree_chance(event_signs, next_signs)
    agree = float(np.mean(agreements)) if agreements else None
    z = None
    if agree is not None and chance is not None and len(agreements) >= 2:
        se = float(np.sqrt(max(chance * (1.0 - chance), 1e-12) / len(agreements)))
        z = (agree - chance) / se
    return {
        "n": len(materialised),
        "n_within": len(inside),
        "p_within": (len(inside) / len(materialised)) if materialised else None,
        "mean_latency_s": float(np.mean(latencies)) if latencies else None,
        "agree": agree,
        "agree_chance": chance,
        "z_vs_chance": z,
        "n_agree": len(agreements),
    }


def hour_dirs(root: Path) -> list[Path]:
    """Every ``date=.../hour=...`` directory under a tape root, oldest first."""
    if not root.exists():
        return []
    return sorted(p for p in root.glob("date=*/hour=*") if p.is_dir())


def sealed_hours(root: Path, *, now_ns: int) -> list[Path]:
    """Hour directories whose UTC hour has ended (a partial hour changes with each run)."""
    sealed: list[Path] = []
    for path in hour_dirs(root):
        try:
            date = path.parent.name.split("=", 1)[1]
            hour = path.name.split("=", 1)[1]
            start = datetime.strptime(f"{date}T{hour}", "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()
        except (IndexError, ValueError):
            continue
        if (start + 3600.0) * 1e9 <= now_ns:
            sealed.append(path)
    return sealed


def shared_hours(
    kalshi_root: Path, clob_root: Path, *, now_ns: int, limit: int | None = None
) -> list[tuple[Path, Path]]:
    """Sealed hours that both tapes have, newest last."""
    kalshi = {p.relative_to(kalshi_root).as_posix(): p for p in sealed_hours(kalshi_root, now_ns=now_ns)}
    clob = {p.relative_to(clob_root).as_posix(): p for p in sealed_hours(clob_root, now_ns=now_ns)}
    shared = sorted(set(kalshi) & set(clob))
    if limit is not None:
        shared = shared[-int(limit) :]
    return [(kalshi[key], clob[key]) for key in shared]


def load_mapping(path: Path) -> pd.DataFrame:
    """Mapping rows with the YES/NO tokens cast to strings and a direction sign."""
    frame = pd.read_parquet(path)
    for column in ("polymarket_yes_token", "polymarket_no_token", "polymarket_condition_id"):
        frame[column] = frame[column].astype(str)
    frame["dir_sign"] = np.where(frame["direction"].astype(str) == "inverted", -1, 1)
    return frame[
        ["kalshi_ticker", "polymarket_condition_id", "polymarket_yes_token", "polymarket_no_token", "dir_sign"]
    ]


def load_kalshi_prints(hours: Sequence[Path], tickers: Sequence[str]) -> pd.DataFrame:
    """Directories -> the mapped Kalshi prints with parsed timestamps and signs."""
    wanted = set(tickers)
    frames: list[pd.DataFrame] = []
    for path in hours:
        files = list(path.glob("*.parquet"))
        if not files:
            continue
        frame = pd.read_parquet(
            files,
            columns=["ticker", "created_time", "count", "taker_side", "taker_book_side", "is_block_trade"],
        )
        frame = frame[frame["ticker"].isin(wanted)]
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["ticker", "ts", "sign", "count", "is_block_trade"])
    out = pd.concat(frames, ignore_index=True)
    out["ts"] = out["created_time"].map(parse_iso_epoch)
    out["sign"] = [
        kalshi_sign(side, book) for side, book in zip(out["taker_side"], out["taker_book_side"])
    ]
    out = out.dropna(subset=["ts", "sign"])
    out["count"] = pd.to_numeric(out["count"], errors="coerce").fillna(0.0)
    out["is_block_trade"] = out["is_block_trade"].fillna(False).astype(bool)
    return out[["ticker", "ts", "sign", "count", "is_block_trade"]]


def load_pm_prints(hours: Sequence[Path], tokens: Sequence[str]) -> pd.DataFrame:
    """Directories -> the Polymarket prints on the mapped tokens, at the YES level."""
    wanted = {str(token): token for token in tokens}
    frames: list[pd.DataFrame] = []
    for path in hours:
        files = list(path.glob("*.parquet"))
        if not files:
            continue
        frame = pd.read_parquet(files, columns=["asset_id", "venue_ts_ms", "side", "size"])
        frame = frame[frame["asset_id"].astype(str).isin(wanted)]
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["asset_id", "ts", "sign", "size"])
    out = pd.concat(frames, ignore_index=True)
    out["asset_id"] = out["asset_id"].astype(str)
    out["ts"] = pd.to_numeric(out["venue_ts_ms"], errors="coerce") / 1000.0
    out = out.dropna(subset=["ts"])
    return out[["asset_id", "ts", "side", "size"]]


def build_panel(mapping: pd.DataFrame, kalshi: pd.DataFrame, pm: pd.DataFrame) -> pd.DataFrame:
    """Attach the condition and the YES-level sign to both sides, then align them."""
    if kalshi.empty or pm.empty:
        return pd.DataFrame()
    token_to_pair = {}
    for row in mapping.itertuples(index=False):
        token_to_pair[str(row.polymarket_yes_token)] = (row.polymarket_condition_id, True, row.dir_sign)
        token_to_pair[str(row.polymarket_no_token)] = (row.polymarket_condition_id, False, row.dir_sign)
    pm = pm.copy()
    pm["condition_id"] = pm["asset_id"].map(lambda token: token_to_pair[token][0])
    pm["yes_token"] = pm["asset_id"].map(lambda token: token_to_pair[token][1])
    pm["dir_sign"] = pm["asset_id"].map(lambda token: token_to_pair[token][2])
    pm["sign"] = [
        pm_sign(side, yes_token=bool(yes)) for side, yes in zip(pm["side"], pm["yes_token"])
    ]
    pm = pm.dropna(subset=["sign"])

    ticker_to_pair = dict(
        zip(mapping["kalshi_ticker"].astype(str), zip(mapping["polymarket_condition_id"], mapping["dir_sign"]))
    )
    kalshi = kalshi.copy()
    kalshi["condition_id"] = kalshi["ticker"].astype(str).map(lambda t: ticker_to_pair.get(t, (None, 1))[0])
    kalshi["dir_sign"] = kalshi["ticker"].astype(str).map(lambda t: ticker_to_pair.get(t, (None, 1))[1])
    kalshi = kalshi.dropna(subset=["condition_id"])
    return kalshi, pm


def measure(
    kalshi: pd.DataFrame,
    pm: pd.DataFrame,
    *,
    max_latency: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Run the event test and its null, condition by condition."""
    events: list[dict[str, object]] = []
    nulls: list[dict[str, object]] = []
    for condition, k_rows in kalshi.groupby("condition_id"):
        p_rows = pm[pm["condition_id"] == condition].sort_values("ts")
        if p_rows.empty:
            continue
        pm_ts = p_rows["ts"].to_numpy(dtype=float)
        # Signed at the YES level of the condition, the same frame as the Kalshi sign.
        pm_signs = p_rows["sign"].to_numpy(dtype=float) * p_rows["dir_sign"].to_numpy(dtype=float)
        k_rows = k_rows.sort_values("ts")
        k_ts = k_rows["ts"].to_numpy(dtype=float)
        k_sign = k_rows["sign"].to_numpy(dtype=float) * k_rows["dir_sign"].to_numpy(dtype=float)
        rows = reaction_rows(
            k_ts,
            k_sign,
            pm_ts,
            pm_signs,
            max_latency=max_latency,
            block_flags=k_rows["is_block_trade"].tolist(),
            sizes=k_rows["count"].tolist(),
        )
        for row in rows:
            row["condition_id"] = condition
        events.extend(rows)

        guard = NULL_GUARD_SECONDS
        span_start = float(np.min(pm_ts))
        span_end = float(np.max(pm_ts))
        draw = null_times(
            span_start,
            span_end,
            count=min(len(k_ts), 4000),
            blocked=k_ts,
            guard=guard,
        )
        for ts in draw:
            latency = next_latency(pm_ts, float(ts), max_latency=max_latency)
            nulls.append({"latency_s": latency, "agree": None, "is_block": False, "size": float("nan")})
    return events, nulls


def measure_reverse(
    kalshi: pd.DataFrame,
    pm: pd.DataFrame,
    *,
    max_latency: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """The symmetric control: Polymarket print -> next Kalshi print.

    Agreement in both directions at the same latency means the two venues react
    to one common event (co-movement). A lead shows up as agreement in one
    direction only.
    """
    events: list[dict[str, object]] = []
    nulls: list[dict[str, object]] = []
    for condition, p_rows in pm.groupby("condition_id"):
        k_rows = kalshi[kalshi["condition_id"] == condition].sort_values("ts")
        if k_rows.empty:
            continue
        k_ts = k_rows["ts"].to_numpy(dtype=float)
        k_signs = k_rows["sign"].to_numpy(dtype=float) * k_rows["dir_sign"].to_numpy(dtype=float)
        p_rows = p_rows.sort_values("ts")
        p_ts = p_rows["ts"].to_numpy(dtype=float)
        p_signs = p_rows["sign"].to_numpy(dtype=float) * p_rows["dir_sign"].to_numpy(dtype=float)
        rows = reaction_rows(
            p_ts,
            p_signs,
            k_ts,
            k_signs,
            max_latency=max_latency,
            sizes=np.zeros_like(p_ts),
        )
        for row in rows:
            row["condition_id"] = condition
        events.extend(rows)
        draw = null_times(
            float(np.min(k_ts)),
            float(np.max(k_ts)),
            count=min(len(p_ts), 4000),
            blocked=p_ts,
        )
        for ts in draw:
            latency = next_latency(k_ts, float(ts), max_latency=max_latency)
            nulls.append({"latency_s": latency, "agree": None, "is_block": False, "size": float("nan")})
    return events, nulls


def per_condition_table(rows: Sequence[dict[str, object]], *, horizon: float) -> pd.DataFrame:
    """Agreement versus chance for each condition - the clustering-robust view.

    Prints cluster inside a condition, so an event-level z overstates precision.
    The condition is the independent unit: count how many conditions beat their
    own chance agreement.
    """
    inside = [
        r
        for r in rows
        if r.get("latency_s") is not None
        and float(r["latency_s"]) <= horizon
        and r.get("agree") is not None
        and r.get("event_sign") is not None
        and r.get("next_sign") is not None
    ]
    table: dict[str, list[dict[str, object]]] = {}
    for row in inside:
        table.setdefault(str(row["condition_id"]), []).append(row)
    records: list[dict[str, object]] = []
    for condition, group in table.items():
        signs = [float(r["event_sign"]) for r in group]
        nexts = [float(r["next_sign"]) for r in group]
        chance = agree_chance(signs, nexts)
        records.append(
            {
                "condition_id": condition,
                "n": len(group),
                "agree": float(np.mean([bool(r["agree"]) for r in group])),
                "chance": chance,
            }
        )
    return pd.DataFrame(records)


def _fmt(value: object, width: int = 9, dp: int = 3) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return " " * (width - 3) + "null"
    if isinstance(value, str):
        return value[:width].ljust(width)
    return f"{value:>{width}.{dp}f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--hours", type=int, default=None, help="most recent N shared sealed hours")
    parser.add_argument("--max-latency", type=float, default=60.0)
    parser.add_argument("--out", type=Path, default=DERIVED)
    args = parser.parse_args(argv)

    now_ns = int(datetime.now(tz=UTC).timestamp() * 1e9)
    shared = shared_hours(KALSHI_ROOT, CLOB_ROOT, now_ns=now_ns, limit=args.hours)
    if not shared:
        log.error("no sealed hour has both kalshi_trades and clob_trades; nothing to measure")
        return 1
    log.info(f"shared sealed hours: {len(shared)} ({shared[0][0].parent.name}/{shared[0][0].name} .. {shared[-1][0].parent.name}/{shared[-1][0].name})")

    mapping = load_mapping(args.mapping)
    tickers = mapping["kalshi_ticker"].astype(str).tolist()
    tokens = mapping["polymarket_yes_token"].tolist() + mapping["polymarket_no_token"].tolist()

    kalshi = load_kalshi_prints([pair[0] for pair in shared], tickers)
    pm = load_pm_prints([pair[1] for pair in shared], tokens)
    log.info(f"mapped Kalshi prints: {len(kalshi)}; Polymarket prints on mapped tokens: {len(pm)}")
    if kalshi.empty or pm.empty:
        log.error("one side of the tape is empty for the mapped universe")
        return 1

    kalshi, pm = build_panel(mapping, kalshi, pm)
    events, nulls = measure(kalshi, pm, max_latency=args.max_latency)
    reverse, reverse_null = measure_reverse(kalshi, pm, max_latency=args.max_latency)
    if not events:
        log.error("no Kalshi print had a Polymarket condition with prints")
        return 1

    blocks = [row for row in events if row["is_block"]]
    thresholds = np.nanpercentile([float(r["size"]) for r in events], [50, 90])
    big = [row for row in events if float(row["size"]) >= thresholds[1]]
    summary = {
        "hours": len(shared),
        "reverse": {h: summarize(reverse, horizon=h) for h in HORIZONS},
        "reverse_null": {h: summarize(reverse_null, horizon=h) for h in HORIZONS},
        "kalshi_prints": len(events),
        "block_prints": len(blocks),
        "size_p50": float(thresholds[0]),
        "size_p90": float(thresholds[1]),
        "conditions": len({r["condition_id"] for r in events}),
        "series": {
            "events": {h: summarize(events, horizon=h) for h in HORIZONS},
            "null": {h: summarize(nulls, horizon=h) for h in HORIZONS},
            "blocks": {h: summarize(blocks, horizon=h) for h in HORIZONS},
            "top_decile_size": {h: summarize(big, horizon=h) for h in HORIZONS},
        },
    }

    print(f"shared sealed hours: {len(shared)}  conditions: {summary['conditions']}  "
          f"Kalshi prints: {len(events)}  blocks: {len(blocks)}  size p50/p90: "
          f"{thresholds[0]:.0f}/{thresholds[1]:.0f}")
    header = f"{'tier':<18}" + "".join(
        f"{'H=' + str(int(h)) + 's':>10}" for h in HORIZONS
    )
    print(header + "   (P(next PM print within H) / agreement)")
    for tier, rows in (
        ("kalshi all", events),
        ("kalshi block", blocks),
        ("kalshi top decile", big),
        ("null (kalshi)", nulls),
        ("reverse: PM all", reverse),
        ("null (reverse)", reverse_null),
    ):
        cells = []
        for horizon in HORIZONS:
            stats = summarize(rows, horizon=horizon)
            p_within = stats["p_within"]
            agree = stats["agree"]
            if p_within is None:
                cells.append("-")
            elif agree is None:
                cells.append(f"{p_within:.2f}/  - ")
            else:
                cells.append(f"{p_within:.2f}/{agree:.2f}")
        print(f"{tier:<18}" + "".join(f"{cell:>10}" for cell in cells))
    print("\nper-condition (independent unit = condition; n >= 20 matched prints)")
    for tier, rows in (("kalshi all", events), ("kalshi top decile", big), ("reverse: PM all", reverse)):
        table = per_condition_table(rows, horizon=5.0)
        usable = table[table["n"] >= 20].sort_values("n", ascending=False)
        if usable.empty:
            print(f"{tier}: no condition reached 20 matched prints at H=5s")
            continue
        above = int((usable["agree"] > usable["chance"]).sum())
        print(f"{tier}: {above}/{len(usable)} conditions above their own chance "
              f"(median agree {usable['agree'].median():.2f}, median n {usable['n'].median():.0f})")
        for row in usable.head(8).itertuples(index=False):
            print(f"   {row.condition_id[:14]}  n={int(row.n):>4}  agree={row.agree:.2f}  chance={row.chance:.2f}")

    print("\nagreement vs chance (same marginals, independent draws)")
    for tier, rows in (("kalshi all", events), ("kalshi top decile", big), ("reverse: PM all", reverse)):
        for horizon in HORIZONS:
            stats = summarize(rows, horizon=horizon)
            if stats["agree"] is None:
                continue
            chance = stats["agree_chance"]
            z = stats["z_vs_chance"]
            print(
                f"{tier:<18}H={horizon:>3.0f}s  agree={stats['agree']:.3f}  chance={chance:.3f}"
                f"  z={z:+.1f}  n={stats['n_agree']}"
            )

    print("\nlatency of the first print after a Kalshi print (mean s, censored at "
          f"{args.max_latency:.0f}s)")
    for tier, rows in (
        ("kalshi all", events),
        ("kalshi block", blocks),
        ("null (kalshi)", nulls),
        ("reverse: PM all", reverse),
        ("null (reverse)", reverse_null),
    ):
        stats = summarize(rows, horizon=args.max_latency)
        print(f"{tier:<18}{_fmt(stats['mean_latency_s'], 10)}  n={stats['n_within']}")

    frame = pd.DataFrame(events)
    frame["null"] = False
    null_frame = pd.DataFrame(nulls)
    null_frame["null"] = True
    out = pd.concat([frame, null_frame], ignore_index=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(args.out, index=False)
    log.info(f"wrote {args.out} rows={len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
