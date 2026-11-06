#!/usr/bin/env python3
"""Build a replay universe from the markets that actually trade.

The reward universe and the liquid universe are disjoint: reward markets have a
median of two book events per six hours in this tape, while the busiest markets
pay nothing. A strategy that only works on volume is still useful, so this
script ranks every condition by book events over a bounded window and emits a
snapshot in the schema `replay_all_markets.py` already loads.

Markets absent from the reward snapshot keep ``daily_pool = 0``, which is the
markout-only mode: ``net`` is then spread capture minus adverse selection, with
no reward income to hide behind.

Example:
    .venv/Scripts/python.exe scripts/build_highvol_universe.py --hours 2 --top 40
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections.abc import Iterable
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb

from polymarketbot.logs import get_logger

log = get_logger("build_highvol_universe")

TAPE = REPO_ROOT / "data" / "tape"
GAMMA = REPO_ROOT / "data" / "raw" / "gamma" / "snapshot_2026-11-01.parquet"
DEFAULT_OUT = TAPE / "universe" / "highvol_latest.json"

#: Feeds every market scored by the venue must declare these; a market without
#: a reward schedule takes the venue's documented default width and minimum size
#: so the engine has a qualifying band to quote inside.
DEFAULT_MIN_SIZE = 20.0
DEFAULT_MAX_SPREAD = 3.5


def _hour_partitions(hours: int) -> list[str]:
    """The ``hours`` busiest partitions on the tape, not the newest ones.

    Partitions are named by the hour they were written, and a quiet hour at the
    end of the tape would rank a handful of markets as "liquid" while the real
    flow sits earlier. Rank on volume first, then bound the window.
    """
    root = TAPE / "clob_events"
    parts = sorted(root.glob("date=*/hour=*"))
    if not parts:
        raise SystemExit(f"no clob_events partitions under {root}")
    con = duckdb.connect()
    con.execute("PRAGMA threads=4")
    scored: list[tuple[str, int]] = []
    for part in parts[-24:]:
        files = f"{str(part).replace(chr(92), '/')}/*.parquet"
        n = con.execute(f"select count(*) from read_parquet('{files}')").fetchone()[0]
        scored.append((str(part), int(n)))
    scored.sort(key=lambda kv: -kv[1])
    chosen = sorted(p for p, _ in scored[:hours])
    return chosen


def rank_conditions(parts: Iterable[str], *, min_events: int) -> list[tuple[str, int]]:
    """Conditions ranked by book-event count, busiest first.

    All event types count: ``book`` snapshots are infrequent, while
    ``price_change`` rows are the flow. Ranking on ``book`` alone measures how
    often a market was re-snapshotted, not how often it traded.
    """
    files = [str(p) for p in parts]
    con = duckdb.connect()
    con.execute("PRAGMA threads=4")
    query = """
        select condition_id, count(*) as n
        from read_parquet(?)
        where condition_id is not null
        group by 1
        having count(*) >= ?
        order by n desc
    """
    rows = con.execute(query, [files, int(min_events)]).fetchall()
    return [(str(c), int(n)) for c, n in rows]


def market_metadata(conditions: list[str]) -> dict[str, dict[str, Any]]:
    """Token pairs and reward schedules from the Gamma snapshot."""
    if not conditions:
        return {}
    con = duckdb.connect()
    con.execute("PRAGMA threads=4")
    placeholders = ",".join("?" for _ in conditions)
    query = f"""
        select conditionId, slug, question, clobTokenIds, rewardsMinSize,
               rewardsMaxSpread, rewards_daily_rate, volume, liquidity, active, closed,
               feesEnabled, feeType
        from '{GAMMA.as_posix()}'
        where conditionId in ({placeholders})
    """
    out: dict[str, dict[str, Any]] = {}
    # The Gamma snapshot names the fee schedule but not its numbers; the numbers
    # live in the reward-universe snapshot. Learn rate/rebate per fee type from
    # the markets where both are known rather than guessing a rate.
    schedule: dict[str, tuple[float, float]] = {}
    universe_dir = TAPE / "universe"
    snaps = sorted(
        (p for p in universe_dir.glob("*.json") if p.stem[:8].isdigit()),
        key=lambda p: p.stat().st_mtime,
    )
    if snaps:
        for m in json.loads(snaps[-1].read_text(encoding="utf-8")).get("markets") or []:
            ftype = str(m.get("fee_type") or "")
            if ftype and ftype != "None" and m.get("fee_rate"):
                schedule.setdefault(ftype, (float(m["fee_rate"]), float(m.get("fee_rebate") or 0.0)))

    for (
        cid, slug, question, tokens, min_size, max_spread, rate, vol, liq,
        active, closed, fees_on, fee_type,
    ) in con.execute(query, conditions).fetchall():
        fee_rate, fee_rebate = schedule.get(str(fee_type or ""), (0.0, 0.0))
        if not tokens:
            continue
        try:
            token_list = json.loads(tokens) if isinstance(tokens, str) else list(tokens)
        except (TypeError, ValueError):
            continue
        token_list = [str(t) for t in token_list if str(t).isdigit()]
        if len(token_list) < 2:
            continue
        out[str(cid)] = {
            "condition_id": str(cid),
            "slug": slug,
            "question": question,
            "clob_tokens": token_list[:2],
            "min_size": float(min_size) if min_size else DEFAULT_MIN_SIZE,
            "max_spread": float(max_spread) if max_spread else DEFAULT_MAX_SPREAD,
            "daily_pool": float(rate or 0.0),
            "volume": float(vol or 0.0),
            "liquidity": float(liq or 0.0),
            "active": bool(active),
            "closed": bool(closed),
            # The maker earns a share of the taker fee on fee-enabled markets.
            # A market missing from the snapshot keeps fees off: an unknown fee
            # must never be invented as revenue.
            "fees_enabled": bool(fees_on) and fee_rate > 0.0,
            "fee_rate": float(fee_rate),
            "fee_rebate": float(fee_rebate),
        }
    return out


def tokens_from_tape(
    conditions: list[str], *, parts: list[str] | None = None, tape: Path = TAPE
) -> dict[str, list[str]]:
    """Fallback token pairs for markets newer than the Gamma snapshot.

    ``clob_events`` names both tokens of a condition; the pair order is not the
    Gamma order, which matters only for which side is called YES. The engine
    prices both from the same ladder, so the replay is unaffected.
    """
    missing = [c for c in conditions]
    if not missing:
        return {}
    parts = parts or [str(p) for p in sorted((tape / "clob_events").glob("date=*/hour=*"))[-1:]]
    if not parts:
        return {}
    con = duckdb.connect()
    con.execute("PRAGMA threads=4")
    placeholders = ",".join("?" for _ in missing)
    files = "[" + ",".join(f"'{p.replace(chr(92), '/')}/*.parquet'" for p in parts) + "]"
    query = f"""
        select condition_id, list(distinct asset_id) as toks
        from read_parquet({files})
        where condition_id in ({placeholders})
        group by 1
        having count(distinct asset_id) >= 2
    """
    out: dict[str, list[str]] = {}
    for cid, toks in con.execute(query, missing).fetchall():
        pair = [str(t) for t in toks if str(t).isdigit()][:2]
        if len(pair) == 2:
            out[str(cid)] = pair
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=2, help="trailing hour partitions to rank over")
    parser.add_argument("--top", type=int, default=40, help="how many conditions to keep")
    parser.add_argument("--min-events", type=int, default=50, help="minimum book rows in the window")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    parts = _hour_partitions(args.hours)
    log.info(f"ranking over {len(parts)} partition(s): {parts[-1].split('clob_events')[-1]}")
    ranked = rank_conditions(parts, min_events=args.min_events)
    log.info(f"{len(ranked)} conditions clear {args.min_events} book rows")
    ranked = ranked[: args.top]
    meta = market_metadata([c for c, _ in ranked])
    fallback = tokens_from_tape([c for c, _ in ranked if c not in meta], parts=parts)
    if fallback:
        log.info(f"{len(fallback)} market(s) are newer than the Gamma snapshot; tokens from the tape")

    markets: list[dict[str, Any]] = []
    dropped = 0
    for cid, events in ranked:
        m = meta.get(cid)
        if m is None and cid in fallback:
            m = {
                "condition_id": cid, "slug": None, "question": None,
                "clob_tokens": fallback[cid], "min_size": DEFAULT_MIN_SIZE,
                "max_spread": DEFAULT_MAX_SPREAD, "daily_pool": 0.0,
                "volume": 0.0, "liquidity": 0.0, "active": True, "closed": False,
                "fees_enabled": False, "fee_rate": 0.0, "fee_rebate": 0.0,
            }
        if m is None:
            dropped += 1
            continue
        if m["closed"] or not m["active"]:
            dropped += 1
            continue
        markets.append({**m, "book_events": events})

    payload = {
        "generated_from": [Path(p).name for p in parts],
        "ranked_conditions": len(ranked),
        "markets": markets,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    logs = [m for m in markets if m["daily_pool"] > 0]
    print(f"wrote {args.out}")
    print(f"markets={len(markets)} dropped={dropped} with_rewards={len(logs)}")
    for m in markets[:10]:
        print(
            f"  {m['condition_id'][:12]} events={m['book_events']:6d} "
            f"pool=${m['daily_pool']:.2f} min={m['min_size']:.0f} "
            f"{(m['question'] or '')[:60]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
