"""Trades-only tape for the liquidity-rewards universe.

Why this exists: the old collector was deleted in commit 999ec0a (43 files /
16,913 lines across eight streams) and the tape it wrote stopped at 2026-11-10
19:50 IST. The reward harvester reads ``data/tape/clob_trades`` for trailing
flow, and with the tape dead it quotes off stale inputs - the smoke run logs
``condition tape index behind ... (missing data, not zero flow)``.

What the harvester actually consumes, from ``strategy/reward/regime.py``:

    SELECT condition_id, recv_ts_ns, price, size
    FROM read_parquet('data/tape/clob_trades/**/*.parquet')

That is the whole contract. It does not read books, events, Binance, Kalshi,
funding or liquidations, so this recorder writes trades and nothing else. The
reader globs the table **without** ``union_by_name``, so the parts must carry the
existing 14-column schema or the read breaks; rows are emitted in that schema,
with the fields we cannot observe (``transaction_hash``, ``fee_rate_bps``,
``shard``) left null rather than invented.

    python -m polymarketbot.tools.reward_tape --duration 172800
    python -m polymarketbot.tools.reward_tape --probe      # discovery only

Discovery is a Gamma keyset walk on the venue's own ``rewardsDailyRate`` - the
field the reward economics are denominated in, and the filter the old collector
used (``> 15``). Markets are re-walked every ``--rediscover`` seconds.

Expected cost, measured rather than assumed: a few hundred subscribed tokens,
~4 prints per second, so roughly 15k rows and 2-3 MB per hour, flushed once a
minute. A kill loses one minute.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from contextlib import AsyncExitStack
from typing import Any

from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.feed import LivePolymarketFeed
from polymarketbot.tools.tape_buffer import MinuteParquetBuffer

log = get_logger("reward_tape")

ROOT = Path(__file__).resolve().parents[3]
TAPE = ROOT / "data" / "tape" / "clob_trades"

# Exactly the columns the existing parts carry; see the module docstring.
COLUMNS = [
    "recv_ts_ns", "recv_mono_ns", "venue_ts_ms", "shard", "condition_id",
    "asset_id", "price", "size", "side", "fee_rate_bps", "transaction_hash",
    "raw_json", "date", "hour",
]


# match the existing parts exactly, including the columns we cannot observe
DTYPES = {
    "recv_ts_ns": "int64", "recv_mono_ns": "int64", "venue_ts_ms": "Int64",
    "shard": "int16", "price": "float64", "size": "float64",
    "fee_rate_bps": "float64", "transaction_hash": "string", "hour": "int64",
}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _end_ms(market: dict[str, Any]) -> int | None:
    raw = market.get("endDateIso") or market.get("endDate")
    if not raw or "T" not in str(raw):
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


class RewardUniverse:
    """Active markets with a live reward campaign, ranked by daily rate."""

    def __init__(self, client: GammaClient, min_rate: float, max_markets: int,
                 max_pages: int) -> None:
        self.client = client
        self.min_rate = min_rate
        self.max_markets = max_markets
        self.max_pages = max_pages

    def discover(self, now: float) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for m in self.client.iter_markets(closed="false", max_pages=self.max_pages):
            # the rate is not a top-level field: it lives in the campaign list
            rate = 0.0
            for camp in m.get("clobRewards") or []:
                try:
                    rate += float(camp.get("rewardsDailyRate") or 0.0)
                except (TypeError, ValueError):
                    continue
            if rate < self.min_rate:
                continue
            if m.get("closed") or m.get("active") is False:
                continue
            tokens = _json_list(m.get("clobTokenIds"))
            if len(tokens) != 2:
                continue
            labels = _json_list(m.get("outcomes"))
            found.append({
                "condition_id": m.get("conditionId"),
                "slug": m.get("slug") or m.get("conditionId"),
                "min_size": m.get("rewardsMinSize"),
                "max_spread": m.get("rewardsMaxSpread"),
                "tokens": tokens,
                "sides": {t: (labels[i] if i < len(labels) else f"leg{i}")
                          for i, t in enumerate(tokens)},
                "window_end_ms": _end_ms(m),
                "reward_rate": rate,
            })
        found.sort(key=lambda r: r["reward_rate"], reverse=True)
        return found[: self.max_markets]


def _meta(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        tok: {"condition_id": m["condition_id"], "asset_id": tok,
              "window_end_ms": m["window_end_ms"]}
        for m in markets for tok in m["tokens"]
    }


async def run(args: argparse.Namespace) -> int:
    """Record trades across the reward universe, sharded over WS connections.

    One connection cannot hold the whole universe: at ~336 tokens the CLOB
    subscription does not take effect and the feed silently falls back to REST
    polling, which returns books and no trades at all (measured: 0 prints in
    77 s across 336 tokens, versus 541 prints in 60 s on 8 tokens). Sharding
    fixes it the same way the deleted collector did, with a stagger so the
    venue's connection-rate limit is not tripped.
    """
    TAPE.mkdir(parents=True, exist_ok=True)
    buf = MinuteParquetBuffer(TAPE, COLUMNS, DTYPES)
    universe = RewardUniverse(GammaClient(), args.min_rate, args.max_markets,
                              args.max_pages)

    shards: list[dict[str, Any]] = []

    def on_trade(trade: Any) -> None:
        recv_ns = time.time_ns()
        stamp = datetime.fromtimestamp(recv_ns / 1e9, UTC)
        buf.add(trade.asset_id, {
            "recv_ts_ns": recv_ns,
            "recv_mono_ns": time.perf_counter_ns(),
            "venue_ts_ms": int(trade.timestamp * 1000) if trade.timestamp else None,
            "shard": 0,
            "price": float(trade.price),
            "size": float(trade.size),
            "side": str(trade.side).upper(),
            "fee_rate_bps": None,
            "transaction_hash": None,
            "raw_json": json.dumps({
                "price": round(float(trade.price), 4),
                "size": round(float(trade.size), 4),
                "side": str(trade.side).upper(),
                "timestamp": int(trade.timestamp * 1000) if trade.timestamp else None,
                "trade_id": trade.trade_id,
                "source": getattr(trade, "source", ""),
            }),
            "date": stamp.date(),
            "hour": stamp.hour,
        })

    async def consumer(feed: LivePolymarketFeed) -> None:
        while True:
            event = await feed.get_next_event(timeout=0.5)
            if event is None:
                continue
            kind, payload = event
            if kind == "trade":
                on_trade(payload)

    async def add_shard(stack: AsyncExitStack) -> dict[str, Any]:
        if len(shards) >= args.max_shards:
            return shards[-1]
        if shards:
            await asyncio.sleep(args.shard_stagger)
        feed = LivePolymarketFeed(fallback_poll_interval=args.poll_interval)
        await stack.enter_async_context(feed)
        shard = {"feed": feed, "tokens": set(),
                 "task": asyncio.create_task(consumer(feed), name=f"tape-consumer-{len(shards)}")}
        shards.append(shard)
        log.info(f"shard {len(shards)} connected (tokens per shard <= {args.tokens_per_shard})")
        return shard

    state: dict[str, Any] = {"last_discover": 0.0, "last_flush": time.time(),
                             "last_report": time.time()}
    started = time.time()
    deadline = started + args.duration

    async with AsyncExitStack() as stack:
        await add_shard(stack)
        while time.time() < deadline:
            now = time.time()

            if now - state["last_discover"] >= args.rediscover:
                markets = await asyncio.to_thread(universe.discover, now)
                meta = _meta(markets)
                buf.set_meta(meta)
                want = sorted(meta)
                per = max(1, args.tokens_per_shard)
                chunks = [want[i:i + per] for i in range(0, len(want), per)]
                while len(shards) < len(chunks):
                    await add_shard(stack)
                assigned = set()
                for i, chunk in enumerate(chunks):
                    shard = shards[i]
                    target = set(chunk)
                    if len(target - shard["tokens"]) + len(shard["tokens"] - target) > args.tokens_per_shard:
                        log.warning(f"shard {i + 1} churn {len(target)} tokens exceeds "
                                    f"{args.tokens_per_shard}; re-subscribing in place")
                    new = target - shard["tokens"]
                    drop = shard["tokens"] - target
                    if new:
                        shard["feed"].subscribe(sorted(new))
                    if drop:
                        shard["feed"].unsubscribe(sorted(drop))
                    shard["tokens"] = target
                    assigned |= target
                for shard in shards[len(chunks):]:
                    if shard["tokens"]:
                        shard["feed"].unsubscribe(sorted(shard["tokens"]))
                        shard["tokens"] = set()
                state["last_discover"] = now
                top = markets[0]["reward_rate"] if markets else 0.0
                log.info(f"discover: {len(markets)} markets (top ${top:.0f}/day), "
                         f"{len(assigned)} tokens over {len(chunks)} shard(s)")

            if now - state["last_flush"] >= args.flush:
                n = await asyncio.to_thread(buf.flush)
                state["last_flush"] = now
                if n:
                    log.info(f"flush: {n} trades (total {buf.written})")

            if now - state["last_report"] >= 60:
                state["last_report"] = now
                conn = sum(1 for s in shards if s["feed"].is_ws_connected)
                held = sum(len(s["tokens"]) for s in shards)
                dropped = sum(s["feed"].event_dropped_count for s in shards)
                log.info(f"running {now - started:.0f}s/{args.duration}s "
                         f"shards={conn}/{len(shards)} tokens={held} "
                         f"buffered={len(buf.rows)} written={buf.written} dropped={dropped}")

            await asyncio.sleep(0.25)

        for shard in shards:
            shard["task"].cancel()
    await asyncio.to_thread(buf.flush)
    log.info(f"done: {buf.written} trades after {time.time() - started:.0f}s")
    return 0


def probe(args: argparse.Namespace) -> int:
    now = time.time()
    markets = RewardUniverse(GammaClient(), args.min_rate, args.max_markets,
                             args.max_pages).discover(now)
    log.info(f"probe: {len(markets)} markets with rewardsDailyRate >= "
             f"${args.min_rate}/day")
    for m in markets[:10]:
        log.info(f"  ${m['reward_rate']:>8.2f}/day  {m['slug']}")
    total = sum(m["reward_rate"] for m in markets)
    log.info(f"total recorded reward rate: ${total:.0f}/day across {len(markets)} markets")
    if markets:
        log.info(f"subscribing {2 * len(markets)} tokens")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Trades-only tape for reward pools")
    ap.add_argument("--duration", type=int, default=3600, help="seconds to run")
    ap.add_argument("--flush", type=float, default=60.0)
    ap.add_argument("--rediscover", type=float, default=300.0)
    ap.add_argument("--min-rate", type=float, default=15.0,
                    help="rewardsDailyRate floor; the old collector cut on >15")
    ap.add_argument("--max-markets", type=int, default=250)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--tokens-per-shard", type=int, default=60,
                    help="one WS connection cannot hold the universe; 336 tokens "
                         "silently fall back to REST and deliver no trades")
    ap.add_argument("--max-shards", type=int, default=10)
    ap.add_argument("--shard-stagger", type=float, default=2.0,
                    help="seconds between connections; the venue resets bursts")
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--probe", action="store_true", help="discovery only, then exit")
    a = ap.parse_args()
    if a.probe:
        return probe(a)
    log.info(f"reward tape: duration={a.duration}s flush={a.flush}s "
             f"rediscover={a.rediscover}s min_rate=${a.min_rate}/day "
             f"max_markets={a.max_markets} out={TAPE}")
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
