"""Full-universe complete-set (CTF) parity census and latency persistence probe.

Measurement only.  No venue state is mutated; nothing here places an order.

The trade being measured is *passive*: buy YES at ``ask_yes`` and NO at
``ask_no`` on the CLOB, hold both legs to redemption (or merge later).  That
pays exactly 1.00 per set, so cost per set is

    ask_yes + ask_no + taker_fee(1, ask_yes, sched) + taker_fee(1, ask_no, sched)

and profit per set is ``1 - cost``.  No split, no mint transaction, no block
interval: the only latency in the critical path is the CLOB round trip.

Subcommands (each finishes inside the 300 s cap on its own):

    walk       page the whole Gamma universe, keep the tradable binary
               non-neg-risk conditions, log every filter's survivor count
    books      snapshot every retained market's two books through
               ``polymarketbot.paper.venue.BookFeed``
    census     profit/depth distributions, fee-regime and liquidity deciles,
               top-20 by total USD
    probe      high-cadence persistence poke at the markets that showed an
               opportunity plus a matched control set
    report     persist the lifetime and capturable-fraction tables

Book snapshots carry the venue's own ``timestamp`` (last book change, ms) and
``hash``.  That is what makes sub-round-trip lifetimes measurable: the episode
duration is read off the venue clock, not off our poll gap.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.paper.books import ladder
from polymarketbot.paper.venue import BookFeed, parse_market, session

log = get_logger("ctf_census")

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "derived" / "ctf_census"
UNIVERSE = OUT / "universe.jsonl"
CENSUS = OUT / "census.json"
PROBE = OUT / "probe.json"

#: Gamma clamps ``limit`` to 100 server-side; 25 req/s is inside the published
#: 300-req/10-s budget and finishes an ~800-page walk in well under a minute.
GAMMA_INTERVAL_S = 0.04


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Phase 1: universe walk
# ---------------------------------------------------------------------------
#: Fields kept per market.  Dumping raw Gamma rows for ~80k markets is hundreds
#: of MB; these are the only fields any downstream step reads.
_KEEP = (
    "conditionId", "question", "outcomes", "outcomePrices", "clobTokenIds",
    "orderPriceMinTickSize", "orderMinSize", "negRisk", "feesEnabled",
    "feeSchedule", "feeType", "closed", "active", "acceptingOrders", "archived",
    "umaResolutionStatus", "clobRewards", "rewardsMaxSpread", "rewardsMinSize",
    "tags", "gameStartTime", "endDate",
)


def _compact(row: dict) -> dict:
    out = {key: row[key] for key in _KEEP if key in row}
    tags = out.get("tags") or []
    if tags and isinstance(tags[0], dict):
        out["tags"] = [t.get("label") for t in tags if isinstance(t, dict)]
    return out


def cmd_walk(args: argparse.Namespace) -> None:
    """Page the keyset, resuming from the persisted cursor.

    A completed pass is ~800 pages at ~0.8 s each (Gamma's own latency), which
    is longer than one 300 s run, and the keyset cursor is sequential by
    construction so it cannot be parallelised.  The walk therefore persists its
    cursor and appends survivors after every page; rerunning continues the same
    pass instead of restarting it.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    state_path = OUT / "walk_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "cursor": None, "pages": 0, "rows": 0, "counts": None, "complete": False,
    }
    if state.get("complete"):
        log.info("walk already complete; delete data/derived/ctf_census/walk_state.json "
                 "to redo the pass")
        return
    counts = state.get("counts") or {
        "rows": 0, "parse_reject": 0, "non_binary": 0, "neg_risk_kept": 0,
        "fee_schedule_missing": 0, "kept": 0, "duplicate": 0,
    }
    kept_ids: set[str] = set()
    if UNIVERSE.exists():
        with UNIVERSE.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    kept_ids.add(json.loads(line)["conditionId"])
                except (ValueError, KeyError):
                    continue
    counts["kept"] = len(kept_ids)
    log.info(f"walk resume: {state['pages']} pages done, {state['rows']} rows seen, "
             f"{len(kept_ids)} markets already kept")
    gamma = GammaClient(min_interval_s=GAMMA_INTERVAL_S)
    seen: set[str] = set()
    t0 = time.time()
    budget = float(args.max_seconds)
    cursor = state["cursor"]
    pages_this_run = 0
    complete = False
    while time.time() - t0 < budget:
        payload = gamma.markets_keyset(
            closed="false", after_cursor=cursor, extra={"active": "true"}
        )
        rows = (payload or {}).get("markets") or []
        state["pages"] += 1
        pages_this_run += 1
        counts["rows"] += len(rows)
        fresh: list[dict] = []
        for row in rows:
            market = parse_market(row)
            if market is None:
                counts["parse_reject"] += 1
                continue
            if not market.binary:
                counts["non_binary"] += 1
                continue
            # neg-risk markets are kept and flagged, never dropped: YES+NO of a
            # single condition still pays exactly 1.00, and neg-risk was 98 of
            # 100 sampled live rows, so excluding it would shrink the census to
            # a corner of the universe.  The engine's CTF pillar refuses
            # neg-risk (different exchange and merge semantics), so every table
            # below reports the non-neg-risk subset separately too.
            if market.neg_risk:
                counts["neg_risk_kept"] += 1
            if market.condition_id in kept_ids:
                counts["duplicate"] += 1
                continue
            compact = _compact(row)
            compact["fee_unknown"] = market.fee_schedule is None
            if compact["fee_unknown"]:
                # feeSchedule absent while feesEnabled is not False: the fee is
                # unknown, so no headline profit number is claimed for it.
                counts["fee_schedule_missing"] += 1
            kept_ids.add(market.condition_id)
            fresh.append(compact)
        if fresh:
            with UNIVERSE.open("a", encoding="utf-8") as fh:
                for row in fresh:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        counts["kept"] = len(kept_ids)
        nxt = (payload or {}).get("next_cursor")
        if not nxt or nxt in seen:
            complete = True
            break
        seen.add(nxt)
        cursor = nxt
        state["cursor"] = cursor
        state["rows"] = counts["rows"]
        state["counts"] = counts
        state_path.write_text(json.dumps(state))
        if pages_this_run % 50 == 0:
            log.info(f"walk: page {state['pages']}, {counts['rows']} rows, "
                     f"{len(kept_ids)} kept, {time.time() - t0:.0f}s this run")

    state["cursor"] = None if complete else cursor
    state["rows"] = counts["rows"]
    state["counts"] = counts
    state["complete"] = complete
    state_path.write_text(json.dumps(state))
    rate_zero = 0
    fee_paying = 0
    for line in UNIVERSE.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("fee_unknown"):
            continue
        if float((row.get("feeSchedule") or {}).get("rate") or 0.0) == 0.0:
            rate_zero += 1
        else:
            fee_paying += 1
    summary = {
        "ts": _now_iso(),
        "pages": state["pages"],
        "complete": complete,
        "run_s": round(time.time() - t0, 1),
        "counts": counts,
        "fee_free_kept": rate_zero,
        "fee_paying_kept": fee_paying,
        "non_neg_risk_kept": counts["kept"] - counts["neg_risk_kept"],
    }
    (OUT / "walk_summary.json").write_text(json.dumps(summary, indent=2))
    log.info(f"walk run finished at page {state['pages']} "
             f"(complete={complete}) in {summary['run_s']}s: {json.dumps(counts)}")
    log.info(f"kept {counts['kept']}: {rate_zero} fee-free, {fee_paying} fee-paying, "
             f"{counts['kept'] - counts['neg_risk_kept']} non-neg-risk")


# ---------------------------------------------------------------------------
# Phase 2: book snapshot
# ---------------------------------------------------------------------------
def _load_universe() -> list[dict]:
    """Survivors of the walk, deduplicated by condition id.

    Resumed runs append, and an earlier (now retired) non-resuming walker also
    appended to the same file, so the same condition can appear more than once;
    the last copy wins.
    """
    by_id: dict[str, dict] = {}
    with UNIVERSE.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a torn line from an interleaved append
            condition_id = row.get("conditionId")
            if condition_id:
                by_id[condition_id] = row
    return list(by_id.values())


def _tokens(row: dict) -> tuple[str, str]:
    raw = row.get("clobTokenIds")
    ids = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    return str(ids[0]), str(ids[1])


def _snapshot_group(http, rows: list[dict], chunk: int, workers: int,
                    ) -> dict[str, dict]:
    """One ``BookFeed`` over this group's 2N tokens, then drop the cache.

    A single snapshot of the whole universe is ~144k books (~500 MB of JSON),
    so it is taken group by group: a fresh feed per group is also what keeps
    ``BookFeed``'s cache from growing without bound.  Each group is a whole
    number of markets, so both legs of every market come from the same batch.
    """
    tokens: list[str] = []
    for row in rows:
        tokens.extend(_tokens(row))
    feed = BookFeed(http, ttl=0.0, chunk=chunk, workers=workers, max_age=5.0)
    return feed.books(tokens, force=True)


# ---------------------------------------------------------------------------
# Phase 3: census
# ---------------------------------------------------------------------------
def _complete_set(book_yes: dict, book_no: dict, schedule: FeeSchedule,
                  ) -> dict[str, Any]:
    """Walk both ask ladders pairing cheapest YES with cheapest NO.

    Returns the best-pair numbers (depth = shares available at both best asks)
    and the full-ladder sweep (marginal edge > 0 on every pair taken).  Fees are
    charged per share at the price actually paid, which is what a taker pays.
    """
    levels_yes = ladder(book_yes, asks=True)
    levels_no = ladder(book_no, asks=True)
    if not levels_yes or not levels_no:
        return {}
    a1, d1 = levels_yes[0]
    a2, d2 = levels_no[0]
    fee_best = taker_fee(1.0, a1, schedule) + taker_fee(1.0, a2, schedule)
    net_best = 1.0 - a1 - a2 - fee_best
    depth_best = min(d1, d2)

    i = j = 0
    rem_yes, rem_no = d1, d2
    sweep_depth = 0.0
    sweep_usd = 0.0
    while i < len(levels_yes) and j < len(levels_no):
        p1, p2 = levels_yes[i][0], levels_no[j][0]
        take = min(rem_yes, rem_no)
        if take <= 0.0:
            break
        fee = taker_fee(1.0, p1, schedule) + taker_fee(1.0, p2, schedule)
        marginal = 1.0 - p1 - p2 - fee
        if marginal <= 0.0:
            break
        # Charge the fee on the lot actually taken, not per-share extrapolation.
        lot_fee = taker_fee(take, p1, schedule) + taker_fee(take, p2, schedule)
        sweep_usd += take * (1.0 - p1 - p2) - lot_fee
        sweep_depth += take
        rem_yes -= take
        rem_no -= take
        if rem_yes <= 1e-9:
            i += 1
            if i < len(levels_yes):
                rem_yes = levels_yes[i][1]
        if rem_no <= 1e-9:
            j += 1
            if j < len(levels_no):
                rem_no = levels_no[j][1]

    ask_notional = (
        sum(p * s for p, s in ladder(book_yes, asks=True)),
        sum(p * s for p, s in ladder(book_no, asks=True)),
    )
    return {
        "ask_yes": a1, "ask_no": a2, "depth_best": depth_best,
        "gross_per_set": 1.0 - a1 - a2,
        "fee_per_set": fee_best, "net_per_set": net_best,
        "usd_best": depth_best * net_best,
        "sweep_depth": sweep_depth, "usd_sweep": sweep_usd,
        "ask_notional_min": min(ask_notional),
        "levels_yes": len(levels_yes), "levels_no": len(levels_no),
    }


def _quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def q(frac: float) -> float:
        pos = frac * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)

    return {
        "min": ordered[0], "p25": q(0.25), "p50": q(0.5),
        "p75": q(0.75), "p90": q(0.9), "max": ordered[-1],
    }


def cmd_census(args: argparse.Namespace) -> None:
    rows = _load_universe()
    records: list[dict] = []
    missing = {"none": 0, "one_side": 0, "no_asks": 0, "fee_unknown": 0}
    http = session()
    group = max(1, int(args.group))
    groups = [rows[i:i + group] for i in range(0, len(rows), group)]
    log.info(f"snapshotting {len(rows)} markets ({2 * len(rows)} books) in "
             f"{len(groups)} groups of {group} at ttl=0, chunk={args.chunk}")
    t0 = time.time()
    for index, chunk_rows in enumerate(groups):
        books = _snapshot_group(http, chunk_rows, args.chunk, args.workers)
        for row in chunk_rows:
            tok_yes, tok_no = _tokens(row)
            b1, b2 = books.get(tok_yes), books.get(tok_no)
            if b1 is None and b2 is None:
                missing["none"] += 1
                continue
            if b1 is None or b2 is None:
                missing["one_side"] += 1
                continue
            schedule = parse_market(row).fee_schedule
            if schedule is None:
                missing["fee_unknown"] += 1
                continue
            m = _complete_set(b1, b2, schedule)
            if not m:
                missing["no_asks"] += 1
                continue
            tick = float(row.get("orderPriceMinTickSize") or 0.0)
            records.append({
                "condition_id": row["conditionId"],
                "tok_yes": tok_yes,
                "tok_no": tok_no,
                "question": (row.get("question") or "")[:90],
                "tick": tick,
                "min_order": float(row.get("orderMinSize") or 0.0),
                "neg_risk": bool(row.get("negRisk")),
                "rate": schedule.rate,
                "exponent": schedule.exponent,
                "fee_free": schedule.rate == 0.0,
                **m,
                "ticks_per_set": m["net_per_set"] / tick if tick > 0 else 0.0,
            })
        if index % 10 == 0:
            log.info(f"census: group {index + 1}/{len(groups)}, "
                     f"{len(records)} priced, {time.time() - t0:.0f}s")
    (OUT / "census_records.jsonl").write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    )
    log.info(f"priced {len(records)} markets in {time.time() - t0:.0f}s; "
             f"books missing none={missing['none']} one_side={missing['one_side']} "
             f"no_asks={missing['no_asks']} fee_unknown={missing['fee_unknown']}")

    pos = [r for r in records if r["net_per_set"] > 0.0]
    sweep_pos = [r for r in records if r["usd_sweep"] > 0.0]

    def band(rows_in: list[dict], mult: float) -> int:
        return sum(1 for r in rows_in if r["net_per_set"] > mult * r["tick"])

    summary = {
        "ts": _now_iso(),
        "priced_markets": len(records),
        "fee_free_markets": sum(1 for r in records if r["fee_free"]),
        "fee_paying_markets": sum(1 for r in records if not r["fee_free"]),
        "gross_positive": sum(1 for r in records if r["gross_per_set"] > 0.0),
        "gross_positive_1tick": sum(
            1 for r in records if r["gross_per_set"] > r["tick"]),
        "profit_gt_zero": len(pos),
        "profit_gt_1tick": band(pos, 1.0),
        "profit_gt_2tick": band(pos, 2.0),
        "sweep_positive": len(sweep_pos),
        "best_usd_sweep": sum(r["usd_sweep"] for r in sweep_pos),
        "best_usd_best_pair": sum(r["usd_best"] for r in pos),
        "markets_missing_books": missing,
        "net_per_set_quantiles_pos": _quantiles([r["net_per_set"] for r in pos]),
        "fee_per_set_quantiles_paying": _quantiles(
            [r["fee_per_set"] for r in records if not r["fee_free"]]),
        "tick_counts": {
            str(t): sum(1 for r in records if abs(r["tick"] - t) < 1e-12)
            for t in sorted({r["tick"] for r in records})
        },
        # Distance to parity in ticks.  There is no economic threshold here:
        # it is the raw (ask_yes + ask_no - 1) / tick, the quantity a taker
        # complete set has to push below zero, and the venue's own matching
        # keeps it above zero by construction.
        "ask_sum_minus_one_ticks": _quantiles(
            [(r["ask_yes"] + r["ask_no"] - 1.0) / r["tick"]
             for r in records if r["tick"] > 0]),
        "markets_at_or_below_par": sum(
            1 for r in records if r["ask_yes"] + r["ask_no"] <= 1.0 + 1e-12),
        "markets_within_1_tick": sum(
            1 for r in records
            if r["tick"] > 0
            and (r["ask_yes"] + r["ask_no"] - 1.0) / r["tick"] <= 1.0 + 1e-9),
        "markets_within_2_ticks": sum(
            1 for r in records
            if r["tick"] > 0
            and (r["ask_yes"] + r["ask_no"] - 1.0) / r["tick"] <= 2.0 + 1e-9),
    }

    def segment(name: str, predicate) -> dict[str, Any]:
        subset = [r for r in records if predicate(r)]
        live = [r for r in subset if r["net_per_set"] > 0.0]
        gross = [r for r in subset if r["gross_per_set"] > 0.0]
        return {
            "segment": name,
            "markets": len(subset),
            "gross_positive": len(gross),
            "profit_gt_zero": len(live),
            "profit_gt_1tick": band(live, 1.0),
            "usd_sweep": sum(r["usd_sweep"] for r in live),
            "usd_best_pair": sum(r["usd_best"] for r in live),
            "max_net_per_set": max((r["net_per_set"] for r in live), default=0.0),
            "median_fee_per_set": (
                statistics.median([r["fee_per_set"] for r in subset]) if subset else 0.0
            ),
        }

    segments = [
        segment("fee-free", lambda r: r["fee_free"]),
        segment("fee-paying", lambda r: not r["fee_free"]),
        segment("fee-free non-neg-risk", lambda r: r["fee_free"] and not r["neg_risk"]),
        segment("fee-free neg-risk", lambda r: r["fee_free"] and r["neg_risk"]),
        segment("non-neg-risk", lambda r: not r["neg_risk"]),
        segment("neg-risk", lambda r: r["neg_risk"]),
        segment("tick=0.01", lambda r: abs(r["tick"] - 0.01) < 1e-12),
        segment("tick=0.001", lambda r: abs(r["tick"] - 0.001) < 1e-12),
        segment("tick<0.001", lambda r: r["tick"] < 0.001 - 1e-12),
    ]
    # Liquidity deciles on the thinner leg's ask-side notional: how much dollar
    # size a taker could actually buy, so a decile is a tradable depth, not a
    # reward score.
    ordered = sorted(records, key=lambda r: r["ask_notional_min"])
    cut = max(1, len(ordered) // 10)
    deciles = []
    for k in range(10):
        chunk = ordered[k * cut:(k + 1) * cut] if k < 9 else ordered[9 * cut:]
        if not chunk:
            continue
        live = [r for r in chunk if r["net_per_set"] > 0.0]
        deciles.append({
            "decile": k + 1,
            "markets": len(chunk),
            "ask_notional_min_lo": chunk[0]["ask_notional_min"],
            "ask_notional_min_hi": chunk[-1]["ask_notional_min"],
            "profit_gt_zero": len(live),
            "usd_sweep": sum(r["usd_sweep"] for r in live),
        })

    top = sorted(records, key=lambda r: r["usd_sweep"], reverse=True)[:20]
    top_best = sorted(records, key=lambda r: r["usd_best"], reverse=True)[:20]
    closest = sorted(
        (r for r in records if r["tick"] > 0),
        key=lambda r: (r["ask_yes"] + r["ask_no"] - 1.0) / r["tick"],
    )[:20]
    payload = {
        "summary": summary,
        "segments": segments,
        "deciles": deciles,
        "top20_sweep": top,
        "top20_best_pair": top_best,
        "top20_closest_to_par": closest,
        "records": records,
    }
    CENSUS.write_text(json.dumps(payload, indent=1))
    log.info(json.dumps(summary, indent=2))
    for row in top:
        log.info(
            f"TOP {row['question'][:44]:44s} tick={row['tick']:<6} "
            f"aY={row['ask_yes']:.4f} aN={row['ask_no']:.4f} "
            f"net={row['net_per_set']:+.4f} ({row['ticks_per_set']:+.1f}t) "
            f"depth={row['sweep_depth']:.1f} usd={row['usd_sweep']:.2f} "
            f"rate={row['rate']}"
        )


# ---------------------------------------------------------------------------
# Phase 4: persistence probe
# ---------------------------------------------------------------------------
#: One request carries this many token books.  The venue answers 30-40 tokens
#: in one round trip at the same ~0.2 s the single-token call costs, so batching
#: buys cadence per request, not lower latency.
PROBE_BATCH_TOKENS = 40

_CLOB_BOOKS = "https://clob.polymarket.com/books"


def _select_targets(records: list[dict], max_opp: int, max_control: int,
                    ) -> tuple[list[dict], list[dict]]:
    """Opportunity markets plus a matched control set from the same census.

    Controls are drawn from markets that showed no complete-set profit, walking
    the census records in a fixed stride so the control is spread over the whole
    liquidity range rather than being the cheapest ten markets.
    """
    opps = sorted(
        (r for r in records if r["usd_sweep"] > 0.0),
        key=lambda r: r["usd_sweep"], reverse=True,
    )[:max_opp]
    if len(opps) < max_opp:
        # No-probability fallback: if the census found no positive market, probe
        # the closest near-misses by net per set so the persistence measurement
        # still says whether the *gross* parity is reachable at all.  The
        # manifest records each target's net_per_set, so a near-miss cannot be
        # mistaken for an opportunity.
        chosen = {r["condition_id"] for r in opps}
        near = sorted(
            (r for r in records if r["condition_id"] not in chosen),
            key=lambda r: r["net_per_set"], reverse=True,
        )[:max_opp - len(opps)]
        log.info(f"only {len(opps)} opportunity markets; probing {len(near)} "
                 f"near-misses (best net={near[0]['net_per_set']:+.4f})" if near else "")
        opps.extend(near)
    flat = [r for r in records if r["usd_sweep"] <= 0.0]
    stride = max(1, len(flat) // max(1, max_control))
    controls = flat[::stride][:max_control]
    return opps, controls


def _poll_batch(http, tokens: list[str], path: Path, stop, stats: dict) -> None:
    """One tight request/response loop over a fixed token batch."""
    polls = 0
    errors = 0
    with path.open("a", encoding="utf-8") as fh:
        while not stop.is_set():
            send = time.time()
            try:
                response = http.post(
                    _CLOB_BOOKS, json=[{"token_id": t} for t in tokens], timeout=10.0
                )
            except Exception:  # noqa: BLE001 - a lost poll is a missing sample
                errors += 1
                time.sleep(0.1)
                continue
            recv = time.time()
            if response.status_code != 200:
                errors += 1
                time.sleep(0.1)
                continue
            try:
                books = response.json()
            except ValueError:
                errors += 1
                continue
            for book in books:
                token = str(book.get("asset_id") or "")
                if not token:
                    continue
                levels = []
                for level in book.get("asks") or []:
                    try:
                        levels.append((float(level["price"]), float(level["size"])))
                    except (KeyError, TypeError, ValueError):
                        continue
                levels.sort()
                fh.write(json.dumps({
                    "s": round(send, 4), "r": round(recv, 4), "tok": token,
                    "vts": float(book.get("timestamp") or 0) / 1000.0,
                    "h": book.get("hash"),
                    "a": [[p, s] for p, s in levels[:12]],
                }, separators=(",", ":")) + "\n")
            fh.flush()
            polls += 1
            stats["polls"] = polls
            stats["errors"] = errors
            stats["rtt_ms"].append((recv - send) * 1000.0)


def cmd_probe(args: argparse.Namespace) -> None:
    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    records = census["records"]
    opps, controls = _select_targets(records, args.max_opp, args.max_control)
    log.info(f"probe targets: {len(opps)} opportunity markets, "
             f"{len(controls)} control markets, duration={args.duration}s")
    probe_dir = OUT / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    for old in probe_dir.glob("batch_*.jsonl"):
        old.unlink()
    targets = [("opp", r) for r in opps] + [("ctl", r) for r in controls]
    batches: list[list[dict]] = []
    per_batch_markets = max(1, args.batch_markets)
    for i in range(0, len(targets), per_batch_markets):
        batches.append(targets[i:i + per_batch_markets])
    log.info(f"{len(batches)} poll batches of {per_batch_markets} markets "
             f"({per_batch_markets * 2} tokens each)")

    import threading
    from concurrent.futures import ThreadPoolExecutor

    stop = threading.Event()
    http = session()
    stats_pool: list[dict] = [{"polls": 0, "errors": 0, "rtt_ms": []}
                              for _ in batches]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        futures = []
        for index, batch in enumerate(batches):
            tokens: list[str] = []
            for _, row in batch:
                tokens.extend([row["tok_yes"], row["tok_no"]])
            futures.append(pool.submit(
                _poll_batch, http, tokens, probe_dir / f"batch_{index:02d}.jsonl",
                stop, stats_pool[index],
            ))
        while time.time() - t0 < args.duration:
            time.sleep(10.0)
            polls = sum(s["polls"] for s in stats_pool)
            errors = sum(s["errors"] for s in stats_pool)
            rtts = [v for s in stats_pool for v in s["rtt_ms"]]
            log.info(f"probe {time.time() - t0:.0f}s: {polls} requests, "
                     f"{errors} errors, rtt p50={statistics.median(rtts):.0f}ms"
                     if rtts else f"probe {time.time() - t0:.0f}s: {polls} requests")
            for s in stats_pool:
                s["rtt_ms"] = s["rtt_ms"][-2000:]
        stop.set()
        for future in futures:
            future.result()

    manifest = {
        "ts": _now_iso(),
        "duration_s": round(time.time() - t0, 1),
        "batch_markets": per_batch_markets,
        "opportunity_markets": [
            {k: r[k] for k in ("condition_id", "question", "tick", "ask_yes",
                               "ask_no", "net_per_set", "sweep_depth",
                               "usd_sweep", "rate")} for r in opps
        ],
        "control_markets": [
            {k: r[k] for k in ("condition_id", "question", "tick", "ask_yes",
                               "ask_no", "net_per_set", "sweep_depth",
                               "usd_sweep", "rate")} for r in controls
        ],
        "batches": len(batches),
        "requests": sum(s["polls"] for s in stats_pool),
        "errors": sum(s["errors"] for s in stats_pool),
        "rtt_ms": _quantiles([v for s in stats_pool for v in s["rtt_ms"]]),
    }
    PROBE.write_text(json.dumps(manifest, indent=1))
    log.info(f"probe done: {manifest['requests']} requests, "
             f"{manifest['errors']} errors, rtt={manifest['rtt_ms']}")


# ---------------------------------------------------------------------------
# Phase 5: episode reconstruction on the venue clock
# ---------------------------------------------------------------------------
def _load_probe_observations() -> dict[str, list[tuple[float, float, str, list]]]:
    """token -> observations sorted by the venue's own book-change timestamp.

    Each entry is ``(venue_ts, recv_local, hash, asks)``.  ``venue_ts`` is when
    the venue last changed that book, so the gaps between consecutive distinct
    ``venue_ts`` values are the true state durations, independent of our poll
    gap.  The poll gap only decides whether a state was caught at all.
    """
    per_token: dict[str, dict[float, tuple[float, float, str, list]]] = {}
    for path in sorted((OUT / "probe").glob("batch_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            token = row["tok"]
            vts = float(row["vts"])
            if vts <= 0:
                continue
            entry = (vts, float(row["r"]), str(row["h"]), row["a"])
            slot = per_token.setdefault(token, {})
            # Keep the earliest receipt per venue state; a later identical
            # venue_ts is the same book re-read, not a new state.
            if vts not in slot or row["r"] < slot[vts][1]:
                slot[vts] = entry
    return {
        token: [slot[k] for k in sorted(slot)] for token, slot in per_token.items()
    }


def _state_at(states: list[tuple], when: float) -> tuple | None:
    lo, hi = 0, len(states)
    while lo < hi:
        mid = (lo + hi) // 2
        if states[mid][0] <= when:
            lo = mid + 1
        else:
            hi = mid
    return states[lo - 1] if lo else None


def _net_at(state_yes: tuple, state_no: tuple, schedule: FeeSchedule) -> dict[str, float]:
    asks_yes, asks_no = state_yes[3], state_no[3]
    if not asks_yes or not asks_no:
        return {}
    p1, p2 = asks_yes[0][0], asks_no[0][0]
    fee = taker_fee(1.0, p1, schedule) + taker_fee(1.0, p2, schedule)
    net = 1.0 - p1 - p2 - fee
    depth = min(asks_yes[0][1], asks_no[0][1])
    return {"net": net, "depth": depth, "usd": depth * net, "ay": p1, "an": p2}


def cmd_evaluate(args: argparse.Namespace) -> None:
    manifest = json.loads(PROBE.read_text(encoding="utf-8"))
    observations = _load_probe_observations()
    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    by_condition = {r["condition_id"]: r for r in census["records"]}
    tags = {r["condition_id"]: "opp" for r in manifest["opportunity_markets"]}
    tags.update({r["condition_id"]: "ctl" for r in manifest["control_markets"]})

    all_recv = [entry[1] for states in observations.values() for entry in states]
    probe_start = min(all_recv) if all_recv else 0.0
    probe_end = max(all_recv) if all_recv else 0.0
    # Local clock vs venue clock: a book state's receipt is always *after* its
    # venue change timestamp, so the minimum (recv - venue_ts) over distinct
    # states bounds our skew plus the fastest observed round trip.  Only
    # censored-episode boundaries mix the two clocks; this says by how much.
    ages = sorted(entry[1] - entry[0] for states in observations.values()
                  for entry in states)

    episodes: list[dict] = []
    change_stats: dict[str, dict[str, float]] = {}
    # Distance to parity, measured at every observed book state: this is what
    # says *how far* the universe sits from an opportunity even when the count
    # of opportunities is zero.  ``net`` already carries the taker fees.
    all_nets: list[float] = []
    all_net_ticks: list[float] = []
    all_aspect: list[float] = []
    for condition_id, tag in tags.items():
        row = by_condition.get(condition_id)
        if row is None:
            continue
        yes = observations.get(row["tok_yes"]) or []
        no = observations.get(row["tok_no"]) or []
        if len(yes) < 2 or len(no) < 2:
            change_stats[condition_id] = {"states_yes": len(yes), "states_no": len(no)}
            continue
        schedule = FeeSchedule(rate=row["rate"], exponent=row["exponent"],
                               taker_only=True, rebate_rate=0.0, source="census")
        # Event times are the venue's book-change timestamps on either leg.
        events = sorted({s[0] for s in yes} | {s[0] for s in no})
        intervals = []
        for index, when in enumerate(events):
            sy, sn = _state_at(yes, when), _state_at(no, when)
            if sy is None or sn is None:
                continue
            net = _net_at(sy, sn, schedule)
            if not net:
                continue
            end = events[index + 1] if index + 1 < len(events) else probe_end
            intervals.append({"start": when, "end": end,
                              "live": net["net"] > 0.0, **net})
            all_nets.append(net["net"])
            if row["tick"] > 0:
                all_net_ticks.append(net["net"] / row["tick"])
                all_aspect.append((net["ay"] + net["an"] - 1.0) / row["tick"])
        change_stats[condition_id] = {
            "states_yes": len(yes), "states_no": len(no),
            "events": len(events),
            "live_intervals": sum(1 for i in intervals if i["live"]),
            "probe_s": round(probe_end - probe_start, 2),
        }
        # Episodes: maximal contiguous runs of live intervals.
        run: list[dict] = []
        for index, interval in enumerate(intervals):
            last = index + 1 == len(intervals)
            if interval["live"]:
                run.append(interval)
                if not last:
                    continue
            if run:
                start = run[0]["start"]
                end = run[-1]["end"]
                episodes.append({
                    "condition_id": condition_id,
                    "tag": tag,
                    "tick": row["tick"],
                    "rate": row["rate"],
                    "start": start,
                    "end": end,
                    "duration_s": max(0.0, end - start),
                    "intervals": len(run),
                    # A run touching the probe edges is censored: its true
                    # lifetime is at least what we saw, possibly more.
                    "left_censored": start <= probe_start,
                    "right_censored": run[-1]["end"] >= probe_end - 1e-9,
                    "net": max(i["net"] for i in run),
                    "depth": max(i["depth"] for i in run),
                    "usd": max(i["usd"] for i in run),
                    "ticks": max(i["net"] for i in run) / row["tick"] if row["tick"] else 0.0,
                })
                run = []

    durations = [e["duration_s"] for e in episodes]
    report: dict[str, Any] = {
        "ts": _now_iso(),
        "probe": {
            "duration_s": manifest["duration_s"],
            "markets": len(tags),
            "opp_markets": sum(1 for t in tags.values() if t == "opp"),
            "ctl_markets": sum(1 for t in tags.values() if t == "ctl"),
            "request_rtt_ms": manifest["rtt_ms"],
            "requests": manifest["requests"],
        },
        "detection": {
            "markets_with_timeline": sum(1 for c in change_stats.values() if "events" in c),
            "markets_with_episode": len({e["condition_id"] for e in episodes}),
            "episodes": len(episodes),
            "episodes_opp": sum(1 for e in episodes if e["tag"] == "opp"),
            "episodes_control": sum(1 for e in episodes if e["tag"] == "ctl"),
            "state_changes": change_stats,
        },
        "parity_distance": {
            "states": len(all_nets),
            "net_per_set": _quantiles(all_nets),
            "net_ticks": _quantiles(all_net_ticks),
            "ask_sum_minus_one_ticks": _quantiles(all_aspect),
            "states_positive": sum(1 for v in all_nets if v > 0.0),
        },
        "episode_duration_s": _quantiles(durations),
        "state_age_at_receipt_s": {
            "min": ages[0] if ages else 0.0,
            "p50": ages[len(ages) // 2] if ages else 0.0,
            "max": ages[-1] if ages else 0.0,
        },
        "episodes": episodes,
    }
    # Two different gaps, kept apart on purpose:
    #   poll_cadence_s         - receipts of the same token, i.e. how often we
    #                            looked.  This is the floor for *detecting* a
    #                            state, never for timing one that was caught.
    #   state_change_interval_s - distinct venue-timestamped book states, i.e.
    #                            how often the book actually changed.
    receipts: dict[str, list[float]] = {}
    for path in sorted((OUT / "probe").glob("batch_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            receipts.setdefault(row["tok"], []).append(float(row["r"]))
    cadence = []
    for times in receipts.values():
        times.sort()
        cadence.extend(nxt - prev for prev, nxt in zip(times, times[1:]))
    changes = []
    for states in observations.values():
        changes.extend(nxt[1] - prev[1] for prev, nxt in zip(states, states[1:]))
    report["poll_cadence_s"] = _quantiles(cadence)
    report["state_change_interval_s"] = _quantiles(changes)
    report["polls"] = {token: len(times) for token, times in receipts.items()}
    report["states_per_token"] = {token: len(states)
                                  for token, states in observations.items()}

    def at_latency(r_s: float, extra: float = 0.0) -> dict[str, Any]:
        hurdle = r_s + extra
        live = [e for e in episodes if e["duration_s"] > hurdle]
        return {
            "hurdle_s": hurdle,
            "episodes": len(live),
            "fraction": (len(live) / len(episodes)) if episodes else 0.0,
            "usd_one_pass": sum(e["usd"] for e in live),
            "usd_fee_free": sum(e["usd"] for e in live if e["rate"] == 0.0),
            "max_net": max((e["net"] for e in live), default=0.0),
        }

    gap_p50 = report["poll_cadence_s"].get("p50", 0.0) if report["poll_cadence_s"] else 0.0
    report["capturable"] = {
        "venue_clock_only": {str(r): at_latency(r) for r in (0.025, 0.030, 0.100, 0.285)},
        "plus_detection_gap": {str(r): at_latency(r, gap_p50)
                              for r in (0.025, 0.030, 0.100, 0.285)},
    }
    (OUT / "episodes.json").write_text(json.dumps(report, indent=1))
    log.info(f"episodes: {len(episodes)} across "
             f"{len({e['condition_id'] for e in episodes})} markets")
    log.info(f"duration quantiles: {report['episode_duration_s']}")
    log.info(f"poll cadence: {report['poll_cadence_s']}")
    log.info(f"state change interval: {report['state_change_interval_s']}")
    for name, block in report["capturable"].items():
        for r, cell in block.items():
            log.info(f"{name} R={r}: {cell['episodes']} episodes "
                     f"({cell['fraction']:.1%}), ${cell['usd_one_pass']:.2f}/pass")
    for e in sorted(episodes, key=lambda e: e["duration_s"], reverse=True)[:20]:
        log.info(f"EP {e['tag']} {e['condition_id'][:10]} dur={e['duration_s']:.3f}s "
                 f"net={e['net']:+.4f} depth={e['depth']:.1f} usd={e['usd']:.2f} "
                 f"tick={e['tick']} rate={e['rate']} cens={e['left_censored']}/{e['right_censored']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("walk").add_argument("--max-seconds", type=float, default=240.0)
    p_census = sub.add_parser("census")
    p_census.add_argument("--group", type=int, default=1600)
    p_census.add_argument("--chunk", type=int, default=200)
    p_census.add_argument("--workers", type=int, default=16)
    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--max-opp", type=int, default=30)
    p_probe.add_argument("--max-control", type=int, default=30)
    p_probe.add_argument("--batch-markets", type=int, default=10)
    p_probe.add_argument("--duration", type=float, default=100.0)
    sub.add_parser("evaluate")
    args = parser.parse_args()
    {"walk": cmd_walk, "census": cmd_census,
     "probe": cmd_probe, "evaluate": cmd_evaluate}[args.cmd](args)
