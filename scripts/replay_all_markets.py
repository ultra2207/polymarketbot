#!/usr/bin/env python3
"""Replay the full reward universe through the paper engine, one strategy at a time.

Every market above the pool cutoff gets its own paper engine with a capital
allocation scaled by its daily pool. The tape supplies book changes and prints;
the engine supplies FIFO queue position, the quadratic reward score, 1-minute
reward epochs, and the measured fill model. Two strategies therefore see
identical data, and the difference between them is the strategy.

The harness is deliberately serial: this machine has one CPU, and two replays
running together are slower than the same two run in order.

    .venv/Scripts/python.exe scripts/replay_all_markets.py \
        --minutes 10 --strategies reactionary,queue_aware_ofi

Outputs under ``data/derived/replay/<tag>/``: ``metrics.parquet`` (one row per
strategy x condition), ``fills.parquet``, and ``summary.json``.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Optional

import duckdb
import pandas as pd
from dataclasses import asdict

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.engine import PaperTradingEngine
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig
from polymarketbot.strategy.base import MarketView, load_strategy

log = get_logger("replay_all_markets")

TAPE = REPO_ROOT / "data" / "tape"  # default; --tape-root overrides for tests
DEFAULT_UNIVERSE = TAPE / "universe"
DERIVED = REPO_ROOT / "data" / "derived" / "replay"
MARKOUT_SECONDS = 300.0


# --------------------------------------------------------------------------- #
# Universe and slice
# --------------------------------------------------------------------------- #
def latest_universe(root: Path) -> Path:
    files = sorted(root.glob("*.json"))
    if not files:
        raise SystemExit(f"no universe snapshot under {root}")
    return files[-1]


def load_universe(path: Path, *, min_pool: float, order_by: str = "pool") -> list[dict[str, Any]]:
    state = json.loads(path.read_text(encoding="utf-8"))
    markets = state.get("markets") or []
    picked = [
        m
        for m in markets
        if float(m.get("daily_pool") or 0.0) >= min_pool
        and len(m.get("clob_tokens") or []) >= 2
    ]
    return sorted(picked, key=lambda m: -float(m.get("daily_pool") or 0.0))


def resolve_slice(minutes: int, start_utc: Optional[str], *, tape: Path = TAPE) -> tuple[float, float]:
    """Pick a sealed slice: explicit start, else the last full N minutes available."""
    if start_utc:
        start = datetime.strptime(start_utc, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp()
        return start, start + minutes * 60.0
    newest = duckdb.connect().execute(
        f"select max(venue_ts_ms) from read_parquet('{tape.as_posix()}/clob_events/**/*.parquet')"
    ).fetchone()[0]
    if newest is None:
        raise SystemExit("no clob_events on the tape")
    end = math.floor(float(newest) / 1000.0 / 300.0) * 300.0  # 5-minute grid
    return end - minutes * 60.0, end


# --------------------------------------------------------------------------- #
# Event stream
# --------------------------------------------------------------------------- #
def iter_events(
    conditions: set[str], start: float, end: float, *, tape: Path = TAPE, batch: int = 200_000
) -> Iterable[dict[str, Any]]:
    """Book changes for the universe, oldest first, in bounded batches."""
    con = duckdb.connect()
    con.execute("create temp table wanted(condition_id varchar)")
    con.executemany("insert into wanted values (?)", [(c,) for c in conditions])
    query = f"""
        select e.condition_id, e.asset_id, e.event_type, e.venue_ts_ms,
               e.bids, e.asks, e.price, e.size, e.side,
               e.best_bid, e.best_ask, e.spread
        from read_parquet('{tape.as_posix()}/clob_events/**/*.parquet') e
        join wanted w using (condition_id)
        where e.venue_ts_ms >= ? and e.venue_ts_ms < ?
        order by e.venue_ts_ms
    """
    cursor = con.execute(query, [int(start * 1000), int(end * 1000)])
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            return
        yield from rows


def recent_hour_dirs(count: int = 3, *, tape: Path = TAPE) -> list[str]:
    """The newest ``count`` clob_events hour partitions, for cheap seed lookups."""
    dirs = sorted(p for p in (tape / "clob_events").glob("date=*/hour=*") if p.is_dir())
    return [d.as_posix() for d in dirs[-count:]]


def seed_books(
    assets: set[str], start: float, *, hours: int = 3, tape: Path = TAPE
) -> dict[str, Any]:
    """Last top-of-book per asset before the slice, so a quiet market still quotes.

    Without a seed a market with no event during the slice never quotes at all,
    which would silently delete the reward tail - exactly the markets where a
    resting quote earns without adverse selection.
    """
    dirs = recent_hour_dirs(hours, tape=tape)
    if not dirs:
        return {}
    con = duckdb.connect()
    con.execute("create temp table tok(asset_id varchar)")
    con.executemany("insert into tok values (?)", [(a,) for a in assets])
    files = ", ".join(f"'{d}/**/*.parquet'" for d in dirs)
    query = f"""
        select asset_id, best_bid, best_ask, spread from (
          select e.asset_id, e.best_bid, e.best_ask, e.spread,
                 row_number() over (partition by e.asset_id order by e.venue_ts_ms desc) rn
          from read_parquet([{files}]) e
          join tok k using (asset_id)
          where e.venue_ts_ms < ? and e.best_bid is not null and e.best_ask is not null
            and e.best_bid > 0 and e.best_ask < 1 and e.best_ask > e.best_bid
        ) where rn = 1
    """
    rows = con.execute(query, [int(start * 1000)]).fetchall()
    tops = {str(a): (float(b), float(a2), float(sp)) for a, b, a2, sp in rows}

    book_query = f"""
        select asset_id, bids, asks from (
          select e.asset_id, e.bids, e.asks,
                 row_number() over (partition by e.asset_id order by e.venue_ts_ms desc) rn
          from read_parquet([{files}]) e
          join tok k using (asset_id)
          where e.venue_ts_ms < ? and e.event_type = 'book'
            and e.bids is not null and e.asks is not null
        ) where rn = 1
    """
    ladders = {
        str(a): (bids, asks) for a, bids, asks in con.execute(book_query, [int(start * 1000)]).fetchall()
    }
    return {"tops": tops, "ladders": ladders}


def iter_prints(assets: set[str], start: float, end: float, *, tape: Path = TAPE) -> Iterable[tuple]:
    """Prints on the universe tokens, oldest first."""
    if not list((tape / "clob_trades").glob("date=*/hour=*/*.parquet")):
        return
    con = duckdb.connect()
    con.execute("create temp table tok(asset_id varchar)")
    con.executemany("insert into tok values (?)", [(a,) for a in assets])
    query = f"""
        select t.asset_id, t.venue_ts_ms, t.price, t.size, t.side
        from read_parquet('{tape.as_posix()}/clob_trades/**/*.parquet') t
        join tok k using (asset_id)
        where t.venue_ts_ms >= ? and t.venue_ts_ms < ?
        order by t.venue_ts_ms
    """
    cursor = con.execute(query, [int(start * 1000), int(end * 1000)])
    while True:
        rows = cursor.fetchmany(100_000)
        if not rows:
            return
        yield from rows


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def capital_for(pool: float, mid: float, min_size: float, args: argparse.Namespace) -> tuple[float, float]:
    """Per-market notional and share size, scaled by the daily pool."""
    if args.flat > 0:
        notional = float(args.flat)
    else:
        notional = min(args.notional_max, max(args.notional_min, args.pool_multiplier * pool))
    shares = max(float(min_size), notional / max(0.01, min(0.99, mid)))
    return notional, shares


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
class Ladder:
    """Per-asset price ladders applied from the tape, bounded around the mid."""

    __slots__ = ("asks", "bids")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_book(self, bids: Any, asks: Any) -> None:
        self.bids = {float(b["price"]): float(b["size"]) for b in (bids or []) if float(b["size"]) > 0}
        self.asks = {float(a["price"]): float(a["size"]) for a in (asks or []) if float(a["size"]) > 0}

    def apply_level(self, side: str, price: float, size: float) -> None:
        book = self.bids if str(side).upper() in ("BUY", "BID") else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def to_lists(self, mid: float, *, window: float = 0.15) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        lo, hi = mid - window, mid + window
        bids = [{"price": p, "size": s} for p, s in self.bids.items() if p >= lo]
        asks = [{"price": p, "size": s} for p, s in self.asks.items() if p <= hi]
        return bids, asks


def run_strategy(
    strategy_name: str,
    markets: list[dict[str, Any]],
    *,
    start: float,
    end: float,
    args: argparse.Namespace,
    tag_dir: Path,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    tape = Path(args.tape_root)
    """One full pass of one strategy over the slice."""
    engines: dict[str, PaperTradingEngine] = {}
    strategies: dict[str, Any] = {}
    token_owner: dict[str, str] = {}
    initial_mid: dict[str, float] = {}
    ladders: dict[str, Ladder] = {}
    mids: dict[str, list[tuple[float, float]]] = defaultdict(list)
    dirty: set[str] = set()
    quote_size: dict[str, float] = {}
    skipped_no_book: set[str] = set()

    for m in markets:
        tokens = [str(t) for t in m["clob_tokens"][:2]]
        min_size = float(m.get("min_size") or 0.0)
        mid0 = 0.5
        _, shares = capital_for(float(m.get("daily_pool") or 0.0), mid0, min_size, args)
        engine = PaperTradingEngine(
            initial_cash_usd=float(args.flat if args.flat > 0 else max(10.0, args.pool_multiplier * float(m.get("daily_pool") or 0.0))),
            default_quote_size=shares,
            max_inventory_shares=max(min_size, shares * 3.0),
            ledger_path=tag_dir / "ledgers" / f"{m['condition_id'][:18]}.json",
            strategy=load_strategy(strategy_name),
        )
        engine.logger = log
        cfg = MarketConfig(
            condition_id=str(m["condition_id"]),
            question=str(m.get("question") or ""),
            rate_per_day=float(m.get("daily_pool") or 0.0),
            rewards_min_size=min_size,
            rewards_max_spread=float(m.get("max_spread") or 0.0),
            tokens=[
                TokenConfig(token_id=tokens[0], outcome="YES", price=mid0),
                TokenConfig(token_id=tokens[1], outcome="NO", price=mid0),
            ],
            event_id=str(m.get("slug") or m["condition_id"]),
        )
        engine.register_market(cfg)
        engines[cfg.condition_id] = engine
        strategies[cfg.condition_id] = engine.strategy
        strategies[cfg.condition_id].setup(
            MarketView(
                condition_id=cfg.condition_id,
                question=cfg.question,
                rate_per_day=cfg.rate_per_day,
                rewards_min_size=cfg.rewards_min_size,
                rewards_max_spread=cfg.rewards_max_spread,
                tokens=tuple(tokens),
            )
        )
        quote_size[cfg.condition_id] = shares
        for t in tokens:
            token_owner[t] = cfg.condition_id
            ladders[t] = Ladder()

    conditions = {m["condition_id"] for m in markets}
    assets = set(token_owner)

    seeds = seed_books(assets, start, tape=tape)
    observed: set[str] = set()
    for asset_id, (bids, asks) in seeds["ladders"].items():
        ladder = ladders.get(asset_id)
        if ladder is not None:
            ladder.apply_book(bids, asks)
            observed.add(asset_id)
    for asset_id, (best_bid, best_ask, _spread) in seeds["tops"].items():
        owner = token_owner.get(asset_id)
        if owner is None or engines[owner].orderbooks[asset_id]["mid"] != 0.5:
            continue
        engine = engines[owner]
        ladder = ladders[asset_id]
        bids, asks = ladder.to_lists(float(best_bid))
        if not bids and not asks:
            bids = [{"price": best_bid, "size": float(quote_size[owner])}]
            asks = [{"price": best_ask, "size": float(quote_size[owner])}]
        engine.update_orderbook(asset_id, bids, asks, 0.01)
        mids[asset_id].append((start, float(engine.orderbooks[asset_id]["mid"])))
        initial_mid.setdefault(asset_id, float(engine.orderbooks[asset_id]["mid"]))
    log.info(
        f"[{strategy_name}] seeded {len(seeds['tops'])} books "
        f"({len(observed)} with a competitor ladder, {len(seeds['tops']) - len(observed)} top-only)"
    )

    fills: list[dict[str, Any]] = []
    last_step = math.floor(start)
    last_epoch = math.floor(start)
    started = time.perf_counter()
    processed = 0

    def flush(until: float) -> None:
        """Apply pending top changes and step the epoch clock up to ``until``."""
        nonlocal last_epoch
        for asset_id in dirty:
            owner = token_owner.get(asset_id)
            if owner is None:
                continue
            engine = engines[owner]
            ladder = ladders[asset_id]
            book = engine.orderbooks.get(asset_id)
            mid = float(book["mid"]) if book else 0.5
            bids, asks = ladder.to_lists(mid)
            # Polymarket scores nothing below 0.02 or above 0.98, and a one-sided
            # or degenerate book cannot price a quote. Skip rather than invent a
            # 0c/100c quote, which also caused fills at impossible prices.
            bids = [b for b in bids if 0.01 <= b["price"] <= 0.98]
            asks = [a for a in asks if 0.02 <= a["price"] <= 0.99]
            if not bids and not asks:
                skipped_no_book.add(asset_id)
                continue
            best_b = max((b["price"] for b in bids), default=0.0)
            best_a = min((a["price"] for a in asks), default=1.0)
            if best_b < 0.02 or best_a > 0.98:
                skipped_no_book.add(asset_id)
                continue
            engine.update_orderbook(asset_id, bids, asks, float(book.get("tick_size", 0.01)) if book else 0.01)
            mid_now = float(engine.orderbooks[asset_id]["mid"])
            mids[asset_id].append((until, mid_now))
            initial_mid.setdefault(asset_id, mid_now)
        dirty.clear()

        while last_epoch + 60.0 <= until <= end:
            last_epoch += 60.0
            for engine in engines.values():
                engine.run_epoch_sample()

    end_track = end + MARKOUT_SECONDS
    for cond, asset, etype, ts_ms, bids, asks, price, size, side, *_ in iter_events(
        conditions, start, end_track, tape=tape
    ):
        t = float(ts_ms) / 1000.0
        second = math.floor(t)
        while last_step < second:
            flush(float(last_step + 1))
            last_step += 1
        ladder = ladders[asset]
        if etype == "book":
            ladder.apply_book(bids, asks)
        elif etype == "price_change" and price is not None and side is not None:
            ladder.apply_level(str(side), float(price), float(size or 0.0))
        dirty.add(asset)
        processed += 1
    while last_step < end_track:
        flush(float(last_step + 1))
        last_step += 1

    _quiet("paper_trading")
    for asset_id, ts_ms, price, size, side in iter_prints(assets, start, end, tape=tape):
        owner = token_owner.get(asset_id)
        if owner is None:
            continue
        # Apply the print to the ladder first, then reprice, then let the engine
        # decide the fill. Without this step a quote left over from an older book
        # "fills" at a price the market left long ago, which produced markouts of
        # thousands of bps - an artefact of the replay, not a strategy result.
        engine = engines[owner]
        ladder = ladders[asset_id]
        taker = str(side).upper()
        level_book = ladder.asks if taker == "BUY" else ladder.bids
        px = float(price)
        level_book[px] = max(0.0, level_book.get(px, 0.0) - float(size))
        bids_now, asks_now = ladder.to_lists(float(engine.orderbooks[asset_id]["mid"]))
        if bids_now or asks_now:
            engine.update_orderbook(asset_id, bids_now, asks_now, 0.01)
            mids[asset_id].append((float(ts_ms) / 1000.0, float(engine.orderbooks[asset_id]["mid"])))
        fill = engine.on_trade(
            asset_id, float(price), float(size), str(side), timestamp=float(ts_ms) / 1000.0
        )
        if fill is not None:
            fill_t = float(ts_ms) / 1000.0
            row = asdict(fill) if hasattr(fill, "__dataclass_fields__") else dict(vars(fill))
            row["asset_id"] = asset_id
            row["condition_id"] = owner
            row["strategy"] = strategy_name
            row["fill_ts"] = fill_t
            row["markout_5m"] = _markout(mids[asset_id], fill_t, float(row.get("price") or price))
            fills.append(row)

    rows: list[dict[str, Any]] = []
    observed_conditions = {
        engine.asset_to_condition.get(a) for a in observed if a in engine.asset_to_condition
    } if engines else set()
    for cond, engine in engines.items():
        summary = engine.get_portfolio_summary()
        inventory = sum(p.market_value for p in engine.positions.values())
        rows.append(
            {
                "strategy": strategy_name,
                "condition_id": cond,
                "rate_per_day": engine.markets[cond].rate_per_day,
                "quote_shares": round(quote_size[cond], 4),
                "fills": sum(p.fills_count for p in engine.positions.values()),
                "cash": float(engine.cash_usd),
                "inventory_mtm": float(inventory),
                "rewards_usd": float(engine.total_rewards_earned_usd),
                "net": float(engine.cash_usd - engine.initial_cash_usd + inventory + engine.total_rewards_earned_usd),
                "epochs": int(engine.epoch_count),
                "competition_observed": bool(
                    any(engine.asset_to_condition.get(a) == cond for a in observed)
                ),
                "equity": float(summary.get("total_equity", 0.0)),
            }
        )
    elapsed = time.perf_counter() - started
    log.info(
        f"[{strategy_name}] {processed:,} book events, {len(fills)} fills, "
        f"{len(rows)} markets in {elapsed:.1f}s"
    )
    frame = pd.DataFrame(rows)
    frame["events"] = processed
    frame["elapsed_s"] = round(elapsed, 2)
    frame["skipped_assets"] = len(skipped_no_book)
    return frame, fills


def _quiet(name: str) -> None:
    """Silence a module logger for the replay: per-fill INFO lines swamp the run."""
    try:
        from loguru import logger as _loguru

        _loguru.disable(name)
    except Exception:  # noqa: BLE001 - logging must never break a replay
        pass


def _markout(series: list[tuple[float, float]], fill_t: float, fill_px: float) -> Optional[float]:
    """Signed mid move ``MARKOUT_SECONDS`` after the fill, in bps of fill price."""
    if not series or fill_px <= 0:
        return None
    target = fill_t + MARKOUT_SECONDS
    times = [t for t, _ in series]
    idx = bisect.bisect_left(times, target)
    if idx >= len(series):
        # Censor rather than invent: use the last mark only if it is at least a
        # minute past the fill, otherwise the number is not a 5-minute markout.
        if series and series[-1][0] - fill_t >= 60.0:
            later = series[-1][1]
        else:
            return None
    else:
        later = series[idx][1]
    return (later - fill_px) / fill_px * 1e4


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=None)
    parser.add_argument("--min-pool", type=float, default=15.0)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--start-utc", default=None, help="YYYY-MM-DDTHH:MM; default = last sealed slice")
    parser.add_argument("--strategies", default="queue_aware_ofi")
    parser.add_argument("--pool-multiplier", type=float, default=0.25)
    parser.add_argument("--notional-min", type=float, default=10.0)
    parser.add_argument("--notional-max", type=float, default=500.0)
    parser.add_argument("--flat", type=float, default=0.0, help="flat notional per market; 0 = pool-scaled")
    parser.add_argument("--max-markets", type=int, default=0)
    parser.add_argument("--order-by", default="liquidity", choices=("pool", "liquidity", "volume"))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--tape-root", default=str(TAPE))
    args = parser.parse_args(argv)

    universe_path = args.universe or latest_universe(DEFAULT_UNIVERSE)
    markets = load_universe(universe_path, min_pool=args.min_pool, order_by=args.order_by)
    if args.max_markets:
        markets = markets[: args.max_markets]
    start, end = resolve_slice(args.minutes, args.start_utc, tape=Path(args.tape_root))
    tag = args.tag or f"{datetime.fromtimestamp(start, UTC):%Y%m%dT%H%M}_{args.minutes}m"
    tag_dir = args.out or (DERIVED / tag)
    tag_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        f"universe={universe_path.name} markets={len(markets)} "
        f"slice={datetime.fromtimestamp(start, UTC):%Y-%m-%dT%H:%M}Z.."
        f"{datetime.fromtimestamp(end, UTC):%H:%M}Z tag={tag}"
    )

    frames = []
    all_fills = []
    for name in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        frame, fills = run_strategy(name, markets, start=start, end=end, args=args, tag_dir=tag_dir)
        frames.append(frame)
        all_fills.extend(fills)

    metrics = pd.concat(frames, ignore_index=True)
    metrics.to_parquet(tag_dir / "metrics.parquet", index=False)
    if all_fills:
        pd.DataFrame(all_fills).to_parquet(tag_dir / "fills.parquet", index=False)

    summary = {"tag": tag, "slice": [start, end], "markets": len(markets), "strategies": {}}
    for name, frame in metrics.groupby("strategy"):
        marks = [
            f["markout_5m"]
            for f in all_fills
            if f.get("strategy") == str(name) and f.get("markout_5m") is not None
        ]
        summary["strategies"][str(name)] = {
            "fills": int(frame["fills"].sum()),
            "fills_recorded": len(
                [f for f in all_fills if f.get("strategy") == str(name)]
            ),
            "net": round(float(frame["net"].sum()), 4),
            "rewards_usd": round(float(frame["rewards_usd"].sum()), 4),
            "inventory_mtm": round(float(frame["inventory_mtm"].sum()), 4),
            "markets_with_fills": int((frame["fills"] > 0).sum()),
            "markout_5m_bps": round(float(sum(marks) / len(marks)), 2) if marks else None,
            "rewards_observed": round(
                float(frame.loc[frame["competition_observed"], "rewards_usd"].sum()), 4
            ),
            "rewards_unobserved_competition": round(
                float(frame.loc[~frame["competition_observed"], "rewards_usd"].sum()), 4
            ),
            "markets_competition_observed": int(frame["competition_observed"].sum()),
            "events": int(frame["events"].iloc[0]),
            "elapsed_s": float(frame["elapsed_s"].iloc[0]),
        }
    (tag_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
