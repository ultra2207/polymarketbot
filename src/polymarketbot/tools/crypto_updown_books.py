"""Continuous L2 book recorder for the crypto up/down markets.

The existing study tape (`crypto_updown_study/books`) snapshots the book at six
fixed horizons before expiry (T-300s ... T-9s). That is enough to price a taker,
who crosses the spread at a single instant, but it cannot answer a maker
question: a resting bid that lives for 300 seconds can be filled at any moment,
and the six snapshots never see the fills that would matter - the ones in the
300 seconds before a favourite collapses.

This records what that needs: every L2 update, for every token of every active
market, with full depth on each side. Run:

    python -m polymarketbot.tools.crypto_updown_books --duration 3600

Output is one parquet per minute under ``books_live/date=/hour=/``, so a kill
loses at most the minute in flight and the next run resumes by writing new
minutes. Market discovery uses the deterministic slug
``<coin>-updown-<interval>-<window_start>``, looked up by exact slug - the
alternative is a 4,000-market keyset walk every minute, which costs 18 s of
latency for 59 useful rows.

Expected cost per hour, measured on a 150 s validation run rather than assumed:
4 coins x (12 five-minute windows + 4 fifteen-minute windows) = 64 markets,
~20-48 tokens subscribed concurrently, and ~5 book updates per second across the
whole set (the feed emits only on change, and these books are quiet). That is
roughly 18k rows and ~10 MB per hour at 5 levels per side, flushed once a
minute.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.feed import LivePolymarketFeed

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "derived" / "crypto_updown_study" / "books_live"
SLUG_CACHE = ROOT / "data" / "derived" / "crypto_updown_study" / "slug_cache"

COINS = ("btc", "eth", "sol", "xrp")
INTERVALS = {"5m": 300, "15m": 900}
DEPTH_LEVELS = 5


def _get_json(url: str, timeout: float = 15.0) -> Any:
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": "polymarketbot-book-recorder/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _parse_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _mark_side(outcomes: Any, token_ids: list[str]) -> dict[str, str]:
    """token id -> 'up' | 'down'.

    The up/down markets publish outcomes as ["Up","Down"] (older `up-or-down`
    series use ["Yes","No"]). Anything unrecognised falls back to index order,
    which is what the study's market table assumed.
    """
    outs = _parse_ids(outcomes)
    labels = [str(o).strip().lower() for o in outs]
    if labels == ["up", "down"] or labels == ["down", "up"]:
        return {tok: lab for tok, lab in zip(token_ids, labels)}
    if len(token_ids) == 2:
        return {token_ids[0]: "up", token_ids[1]: "down"}
    return {}


def _window_end_ms(market: dict[str, Any], window_start_s: int, seconds: int) -> int:
    """Expiry in ms, from the slug rather than the market payload.

    `endDateIso` on these markets is date-only ("2026-11-10"), so parsing it
    puts every window at midnight and every market looks long expired. The slug
    timestamp is the window start, and the interval is in the slug, so
    start + interval is exact. The payload is only trusted when it carries a
    time component.
    """
    for key in ("endDate", "endDateIso"):
        raw = market.get(key)
        if not raw or "T" not in str(raw):
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return (window_start_s + seconds) * 1000


class Discovery:
    """Resolve active up/down markets by generated slug."""

    def __init__(self) -> None:
        SLUG_CACHE.mkdir(parents=True, exist_ok=True)
        self._seen: dict[str, dict[str, Any]] = {}

    def _lookup(self, slug: str) -> dict[str, Any] | None:
        path = SLUG_CACHE / f"{slug}.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text())
            except Exception:
                payload = None
            if payload is not None:
                return payload or None
        try:
            rows = _get_json(f"https://gamma-api.polymarket.com/markets?slug={slug}")
        except Exception as exc:  # transient DNS/HTTP; retried next cycle
            log.debug(f"slug lookup failed {slug}: {exc}")
            return None
        market = rows[0] if rows else None
        path.write_text(json.dumps(market))
        return market

    def discover(self, now: float) -> list[dict[str, Any]]:
        """Every market whose window overlaps [now - max_interval, now + max_interval]."""
        out: dict[str, dict[str, Any]] = {}
        for coin in COINS:
            for name, seconds in INTERVALS.items():
                base = int(now // seconds) * seconds
                for k in (-2, -1, 0, 1, 2):
                    start = base + k * seconds
                    slug = f"{coin}-updown-{name}-{start}"
                    market = self._lookup(slug)
                    if not market:
                        continue
                    tokens = _parse_ids(market.get("clobTokenIds"))
                    if len(tokens) != 2:
                        continue
                    end_ms = _window_end_ms(market, start, seconds)
                    if end_ms < (now - 60) * 1000:
                        continue  # already expired; nothing left to record
                    sides = _mark_side(market.get("outcomes"), tokens)
                    out[slug] = {
                        "slug": slug,
                        "condition_id": market.get("conditionId"),
                        "tokens": tokens,
                        "sides": sides,
                        "interval": name,
                        "coin": coin,
                        "window_start_ms": start * 1000,
                        "window_end_ms": end_ms,
                    }
        self._seen.update(out)
        return list(out.values())


class BookBuffer:
    """Buffers rows and writes one parquet per minute."""

    def __init__(self, out: Path, levels: int = DEPTH_LEVELS) -> None:
        self.out = out
        self.levels = levels
        self.rows: deque = deque()
        self.written = 0
        self.files = 0
        self.meta: dict[str, dict[str, Any]] = {}

    def set_markets(self, markets: list[dict[str, Any]]) -> None:
        for m in markets:
            for tok in m["tokens"]:
                self.meta[tok] = {
                    "condition_id": m["condition_id"],
                    "slug": m["slug"],
                    "side": m["sides"].get(tok),
                    "interval": m["interval"],
                    "coin": m["coin"],
                    "window_end_ms": m["window_end_ms"],
                }

    def add(self, snap: Any, recv_ns: int) -> None:
        info = self.meta.get(snap.asset_id)
        if info is None:
            return
        bids = snap.bids[: self.levels]
        asks = snap.asks[: self.levels]
        self.rows.append((
            recv_ns,
            int(snap.timestamp * 1000) if snap.timestamp else None,
            snap.asset_id,
            info["condition_id"],
            info["slug"],
            info["side"],
            info["coin"],
            info["interval"],
            info["window_end_ms"],
            snap.best_bid,
            snap.best_ask,
            snap.mid,
            snap.spread,
            bids[0].size if bids else 0.0,
            asks[0].size if asks else 0.0,
            sum(b.size for b in bids),
            sum(a.size for a in asks),
            json.dumps([[b.price, b.size] for b in bids]),
            json.dumps([[a.price, a.size] for a in asks]),
            snap.source,
        ))

    def flush(self, now: float | None = None) -> int:
        if not self.rows:
            return 0
        now = now or time.time()
        date = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        hour = datetime.fromtimestamp(now, UTC).strftime("%H")
        minute = datetime.fromtimestamp(now, UTC).strftime("%M")
        dest = self.out / f"date={date}" / f"hour={hour}"
        dest.mkdir(parents=True, exist_ok=True)
        rows = list(self.rows)
        self.rows.clear()
        df = pd.DataFrame(rows, columns=[
            "recv_ns", "book_ts_ms", "asset_id", "condition_id", "slug", "side", "coin",
            "interval", "window_end_ms", "best_bid", "best_ask", "mid", "spread",
            "bid_size", "ask_size", "depth_bid", "depth_ask", "bids_json", "asks_json",
            "source",
        ])
        path = dest / f"minute={minute}.parquet"
        # a rerun inside the same wall-clock minute appends rather than overwrites
        if path.exists():
            try:
                prev = pd.read_parquet(path)
                df = pd.concat([prev, df], ignore_index=True)
            except Exception:
                pass
        df.to_parquet(path, index=False)
        self.written += len(rows)
        self.files += 1
        return len(rows)


async def run(args: argparse.Namespace) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    buf = BookBuffer(OUT)
    disc = Discovery()
    feed = LivePolymarketFeed(fallback_poll_interval=args.poll_interval)

    state = {"subscribed": set(), "ticks": 0, "last_discover": 0.0,
             "last_flush": time.time(), "last_report": time.time()}

    def on_book(snap: Any) -> None:
        buf.add(snap, time.time_ns())

    feed.add_book_listener(on_book)

    started = time.time()
    deadline = started + args.duration

    async with feed:
        while time.time() < deadline:
            now = time.time()

            if now - state["last_discover"] >= args.rediscover:
                markets = await asyncio.to_thread(disc.discover, now)
                buf.set_markets(markets)
                want = {t for m in markets for t in m["tokens"]}
                expired = {t for t, meta in buf.meta.items()
                           if (meta["window_end_ms"] or 0) < (now - 120) * 1000}
                new = want - state["subscribed"]
                drop = (state["subscribed"] - want) | expired
                if new:
                    feed.subscribe(sorted(new))
                if drop:
                    feed.unsubscribe(sorted(drop))
                state["subscribed"] = (state["subscribed"] | new) - drop
                state["last_discover"] = now
                log.info(f"discover: {len(markets)} markets, {len(state['subscribed'])} tokens "
                         f"subscribed (+{len(new)}/-{len(drop)})")

            if now - state["last_flush"] >= args.flush:
                n = await asyncio.to_thread(buf.flush)
                state["last_flush"] = now
                if n:
                    log.info(f"flush: {n} rows -> {buf.files} files, {buf.written} total")

            if now - state["last_report"] >= 60:
                state["last_report"] = now
                log.info(f"running {now - started:.0f}s/{args.duration}s ws={feed.is_ws_connected} "
                         f"tokens={len(state['subscribed'])} buffered={len(buf.rows)} "
                         f"written={buf.written} dropped={feed.dispatch_dropped_count}")

            await asyncio.sleep(0.25)

    await asyncio.to_thread(buf.flush)
    log.info(f"done: {buf.written} rows in {buf.files} files "
             f"after {time.time() - started:.0f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Record continuous L2 books for crypto up/down markets")
    ap.add_argument("--duration", type=int, default=3600, help="seconds to run")
    ap.add_argument("--flush", type=float, default=60.0, help="seconds between parquet flushes")
    ap.add_argument("--rediscover", type=float, default=60.0, help="seconds between slug rediscovery")
    ap.add_argument("--poll-interval", type=float, default=2.0, help="REST fallback poll interval")
    a = ap.parse_args()
    log.info(f"book recorder: duration={a.duration}s flush={a.flush}s "
             f"rediscover={a.rediscover}s out={OUT}")
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
