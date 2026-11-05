"""Counterfactual market tape recorder.

Subscribes to the live public Polymarket feed for one market's YES/NO tokens
and snapshots the book at 1 Hz into hourly Parquet files. The tape is purely
observational: it never places orders and never reads private account state, so
it can be replayed to evaluate a strategy that was *not* running.

Layout: ``<out>/<condition_id>_<YYYYMMDDTHH>.parquet`` (UTC hours). Each flush
rewrites the in-progress hour atomically (tmp file + ``os.replace``), and hours
already on disk are skipped on start, so a re-run never overwrites or appends
to a completed hour.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import pandas as pd

from polymarketbot.logs import get_logger
from polymarketbot.simulator.feed import (
    LivePolymarketFeed,
    MarketTrade,
    OrderBookSnapshot,
)

logger = get_logger("simulator.tape")

DEPTH_BUCKETS_CENTS = (0.5, 1.0, 2.0, 3.5)


def _utc_hour_key(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y%m%dT%H")


def _safe_name(text: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(text))


def _bucket_label(cents: float) -> str:
    return f"{cents:g}c"


def _depth_side(
    levels: Optional[list[Any]],
    reference: Optional[float],
    bucket_cents: float,
    side: str,
) -> float:
    """Depth within ``bucket_cents`` of ``reference`` on the bid or ask ladder."""
    if not levels or reference is None or not (0.0 < float(reference) < 1.0):
        return 0.0
    width = float(bucket_cents) / 100.0
    total = 0.0
    for level in levels:
        price = float(getattr(level, "price", 0.0) or 0.0)
        size = float(getattr(level, "size", 0.0) or 0.0)
        if size <= 0.0 or not (0.0 < price < 1.0):
            continue
        if side == "bid" and price >= float(reference) - width:
            total += size
        elif side == "ask" and price <= float(reference) + width:
            total += size
    return total


def _top_of_book(book: Optional[OrderBookSnapshot]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Best bid, best ask and mid from a snapshot, or Nones when absent."""
    if book is None:
        return None, None, None
    bid = float(book.best_bid) if 0.0 < float(book.best_bid) < 1.0 else None
    ask = float(book.best_ask) if 0.0 < float(book.best_ask) < 1.0 else None
    if bid is not None and ask is not None and ask >= bid:
        mid = (bid + ask) / 2.0
    elif bid is not None:
        mid = bid
    elif ask is not None:
        mid = ask
    else:
        mid = None
    return bid, ask, mid


class MarketTapeRecorder:
    """1 Hz book/trade tape writer with hourly Parquet flushing.

    Feed listeners only cache the newest snapshot per asset; the sampler is the
    only writer, which keeps the Parquet flushes single-threaded.
    """

    def __init__(
        self,
        condition_id: str,
        token_yes: str,
        token_no: str,
        out_dir: Union[str, Path],
        *,
        sample_interval: float = 1.0,
        flush_interval: float = 60.0,
        clock: Optional[Callable[[], float]] = None,
        feed: Optional[LivePolymarketFeed] = None,
    ) -> None:
        self.condition_id = str(condition_id)
        self.token_yes = str(token_yes)
        self.token_no = str(token_no)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.sample_interval = max(0.05, float(sample_interval))
        self.flush_interval = max(1.0, float(flush_interval))
        self._clock = clock or time.time
        self._feed = feed

        self._books: Dict[str, OrderBookSnapshot] = {}
        self._trades: List[MarketTrade] = []
        self._rows_by_hour: Dict[str, List[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._last_flush = self._clock()
        self._last_row: Optional[dict[str, Any]] = None
        self._completed_hours: set[str] = self._existing_hours()

    # ------------------------------------------------------------------
    # Feed listeners
    # ------------------------------------------------------------------
    def on_book(self, snapshot: OrderBookSnapshot) -> None:
        asset_id = str(getattr(snapshot, "asset_id", "") or "")
        if asset_id not in (self.token_yes, self.token_no):
            return
        with self._lock:
            self._books[asset_id] = snapshot

    def on_trade(self, trade: MarketTrade) -> None:
        asset_id = str(getattr(trade, "asset_id", "") or "")
        if asset_id not in (self.token_yes, self.token_no):
            return
        with self._lock:
            self._trades.append(trade)

    # ------------------------------------------------------------------
    # Sampling & persistence
    # ------------------------------------------------------------------
    @property
    def columns(self) -> List[str]:
        columns = [
            "ts",
            "condition_id",
            "token_yes",
            "token_no",
            "bid_yes",
            "ask_yes",
            "mid_yes",
            "bid_no",
            "ask_no",
            "mid_no",
        ]
        for token in ("yes", "no"):
            for side in ("bid", "ask"):
                for bucket in DEPTH_BUCKETS_CENTS:
                    columns.append(f"depth_{token}_{side}_{_bucket_label(bucket)}")
        columns.extend(
            [
                "trades_since_last_count",
                "trades_since_last_signed_volume",
                "trades_since_last_vwap",
            ]
        )
        return columns

    def existing_hours(self) -> set[str]:
        return set(self._completed_hours)

    def sample(self, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Append one 1 Hz row and flush when the interval has elapsed."""
        ts = float(self._clock() if now is None else now)
        hour = _utc_hour_key(ts)
        with self._lock:
            if hour in self._completed_hours:
                self._trades.clear()
                return None
            row = self._build_row(ts)
            self._rows_by_hour.setdefault(hour, []).append(row)
            self._last_row = row
            self._trades.clear()
            due = (ts - self._last_flush) >= self.flush_interval
        if due:
            self.flush(now=ts)
        return row

    def flush(self, now: Optional[float] = None) -> Dict[str, int]:
        """Atomically rewrite every buffered hour file; returns rows per file."""
        ts = float(self._clock() if now is None else now)
        current_hour = _utc_hour_key(ts)
        with self._lock:
            buffers = {
                hour: list(rows)
                for hour, rows in self._rows_by_hour.items()
                if rows
            }
            self._last_flush = ts
        written: Dict[str, int] = {}
        for hour, rows in buffers.items():
            path = self._hour_path(hour)
            tmp = path.with_name(path.name + ".tmp")
            frame = pd.DataFrame(rows, columns=self.columns)
            frame.to_parquet(tmp, index=False)
            os.replace(tmp, path)
            written[str(path)] = int(len(frame))
            logger.info(f"tape flush {path.name}: {len(frame)} rows")
            if hour != current_hour:
                with self._lock:
                    self._rows_by_hour.pop(hour, None)
                    self._completed_hours.add(hour)
        return written

    def output_files(self) -> List[Path]:
        with self._lock:
            hours = set(self._rows_by_hour) | set(self._completed_hours)
        return sorted(self._hour_path(hour) for hour in hours)

    # ------------------------------------------------------------------
    # Live recording loop
    # ------------------------------------------------------------------
    async def run(self, duration: Optional[float] = None) -> List[Path]:
        """Record for ``duration`` seconds (or until cancelled), then flush."""
        feed = self._feed or LivePolymarketFeed()
        self._feed = feed
        feed.add_book_listener(self.on_book)
        feed.add_trade_listener(self.on_trade)
        feed.subscribe([self.token_yes, self.token_no])

        feed_task = asyncio.create_task(feed.start())
        started = self._clock()
        try:
            while duration is None or (self._clock() - started) < float(duration):
                await asyncio.sleep(self.sample_interval)
                self.sample()
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await feed.stop()
            except Exception as e:
                logger.warning(f"tape feed stop failed: {e}")
            if not feed_task.done():
                feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"tape feed task ended: {e}")
            self.flush()
        return self.output_files()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _existing_hours(self) -> set[str]:
        prefix = f"{_safe_name(self.condition_id)}_"
        hours: set[str] = set()
        for path in self.out_dir.glob(f"{prefix}*.parquet"):
            stem = path.stem[len(prefix):]
            if len(stem) == len("YYYYMMDDTHH"):
                hours.add(stem)
        return hours

    def _hour_path(self, hour: str) -> Path:
        return self.out_dir / f"{_safe_name(self.condition_id)}_{hour}.parquet"

    def _build_row(self, ts: float) -> dict[str, Any]:
        yes = self._books.get(self.token_yes)
        no = self._books.get(self.token_no)
        bid_yes, ask_yes, mid_yes = _top_of_book(yes)
        bid_no, ask_no, mid_no = _top_of_book(no)

        row: dict[str, Any] = {
            "ts": ts,
            "condition_id": self.condition_id,
            "token_yes": self.token_yes,
            "token_no": self.token_no,
            "bid_yes": bid_yes,
            "ask_yes": ask_yes,
            "mid_yes": mid_yes,
            "bid_no": bid_no,
            "ask_no": ask_no,
            "mid_no": mid_no,
        }
        for token, book, bid, ask in (
            ("yes", yes, bid_yes, ask_yes),
            ("no", no, bid_no, ask_no),
        ):
            bids = getattr(book, "bids", None) if book is not None else None
            asks = getattr(book, "asks", None) if book is not None else None
            for bucket in DEPTH_BUCKETS_CENTS:
                label = _bucket_label(bucket)
                row[f"depth_{token}_bid_{label}"] = _depth_side(bids, bid, bucket, "bid")
                row[f"depth_{token}_ask_{label}"] = _depth_side(asks, ask, bucket, "ask")

        trades = list(self._trades)
        count = len(trades)
        signed_volume = 0.0
        notional = 0.0
        volume = 0.0
        for trade in trades:
            size = float(getattr(trade, "size", 0.0) or 0.0)
            price = float(getattr(trade, "price", 0.0) or 0.0)
            signed_volume += size if str(getattr(trade, "side", "")).upper() == "BUY" else -size
            volume += size
            notional += size * price
        row["trades_since_last_count"] = count
        row["trades_since_last_signed_volume"] = signed_volume
        row["trades_since_last_vwap"] = (notional / volume) if volume > 0.0 else None
        return row
