"""Synthetic L3 queue telemetry reconstructed from the raw CLOB tape.

The tape is L2 + trades: ``book`` snapshots carry the aggregate size at every
price level, ``price_change`` frames carry the *new aggregate size* at a level,
and ``clob_trades`` carries executions. The queue is therefore observable: the
first ``book`` for an asset gives the resting size at each level, and every
later frame tells us how that size moved. This module replays that stream into
per-level queue telemetry for a *hypothetical* maker order resting at a price.

Model
-----
One shadow order per price level (per asset). It joins at the back of the
level, so at join time everybody resting there is ahead of it:

    q_ahead  = observed level size when the order joined
    q_behind = size that arrived at the level after the join

FIFO consumption: a decrease at the level removes quantity from the front, so
it eats ``q_ahead`` first; an increase appends behind, so it grows ``q_behind``.
``queue_ratio = q_ahead / (q_ahead + q_behind)`` is the share of the level still
in front of the order (1.0 = order is last in line, 0.0 = it is next to trade).
When a decrease consumes more than ``q_ahead`` the shadow order is filled
(``survival = 0``) and it rejoins at the back of whatever remains, because a
maker that quotes continuously is back in the queue immediately.

Row semantics (one row per level update)
----------------------------------------
``size_at_level``    observed aggregate size after the update.
``q_ahead/q_behind`` queue state of the shadow order after the update.
``queue_age_ns``     age of the shadow order at this observation.
``cancel_size``      part of the decrease attributed to a cancel.
``fill_size``        part of the decrease attributed to a trade.
``replenishment_latency``  ns from the depleting update to the first increase
                     at the same level (null when this update is not that
                     increase).
``survival``         1 while the shadow order still rests, 0 at the update that
                     filled it or removed the level.
``discontinuity``    this anchor replaced state the replay could not trust: a
                     ``gap`` fell inside the interval, or the snapshot disagreed
                     with the replayed book.

A ``book`` re-anchor emits a row only for a level that appeared, changed or
vanished. Restating an unchanged level would add nothing but bytes: the tape
already holds the snapshot, and the shadow order is reset from it either way.

Attribution rule
----------------
A decrease at a level is explained by trades in the same asset at the same
price whose taker side consumes that book side: taker ``BUY`` lifts asks,
taker ``SELL`` hits bids (verified on the tape: a BUY print at 0.59 is matched
by ``SELL``-side level decreases at 0.59). Matches are taken from a pool of
unconsumed trade volume within ``match_window_ns`` of the decrease.

* no matching trade volume       -> ``cancel``
* trade volume explains it all    -> ``fill``
* trade volume explains only part -> ``ambiguous`` + ``attribution_ambiguous``

The partial/over-explained case is genuinely ambiguous: a cancel and a fill can
land in the same interval and the tape cannot separate them, so both sizes are
reported as best estimates and the row is flagged. The matcher is greedy
(oldest unconsumed trade volume first); it is a reconstruction, not an
exchange statement.

The tape is also not guaranteed complete across a reconnect: a dropped
``price_change`` turns a fill into a false cancel. That is why a ``gap``
unanchors the asset instead of guessing.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

# Price keys are rounded the same way as ``polymarketbot.simulator.book`` so a
# float that arrives as 0.59000000001 keys the level that arrived as 0.59.
PRICE_PRECISION = 6
SIZE_EPSILON = 1e-9
SIZE_TOLERANCE = 1e-6

# A fill's ``last_trade_price`` row can trail the ``price_change`` that reflects
# it by a frame or two; a print is allowed to explain a decrease this far away
# in either direction.
DEFAULT_MATCH_WINDOW_NS = 500_000_000

BOOK_SIDE_BID = "bid"
BOOK_SIDE_ASK = "ask"

# Taker side of a print -> the book side it consumes.
TAKER_BOOK_SIDE = {"BUY": BOOK_SIDE_ASK, "SELL": BOOK_SIDE_BID}

L3_QUEUE_SCHEMA = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("asset_id", pa.string()),
        ("side", pa.string()),
        ("price", pa.float64()),
        ("size_at_level", pa.float64()),
        ("q_ahead", pa.float64()),
        ("q_behind", pa.float64()),
        ("queue_ratio", pa.float64()),
        ("cancel_size", pa.float64()),
        ("fill_size", pa.float64()),
        ("replenishment_latency", pa.int64()),
        ("survival", pa.int8()),
        ("queue_age_ns", pa.int64()),
        ("attribution", pa.string()),
        ("attribution_ambiguous", pa.bool_()),
        ("discontinuity", pa.bool_()),
    ]
)


# --------------------------------------------------------------------- parsing
def norm_price(price: Any) -> float | None:
    """Round a venue price to the tick grid used as the level key."""
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    return round(value, PRICE_PRECISION)


def book_side(side: Any) -> str | None:
    """Map a tape side to the book side it belongs to.

    ``price_change.side`` is the side of the *resting* order, so ``BUY`` is a
    bid level and ``SELL`` is an ask level.
    """
    text = str(side or "").strip().upper()
    if text in ("BUY", "BID"):
        return BOOK_SIDE_BID
    if text in ("SELL", "ASK"):
        return BOOK_SIDE_ASK
    return None


def parse_levels(raw: Any) -> list[tuple[float, float]]:
    """``[{price, size}]`` (or objects with those attributes) -> ``[(price, size)]``."""
    out: list[tuple[float, float]] = []
    for item in raw or ():
        if isinstance(item, Mapping):
            price, size = item.get("price"), item.get("size")
        else:
            price, size = getattr(item, "price", None), getattr(item, "size", None)
        p = norm_price(price)
        if p is None or size is None:
            continue
        try:
            s = float(size)
        except (TypeError, ValueError):
            continue
        if s <= SIZE_EPSILON:
            continue
        out.append((p, s))
    return out


# ----------------------------------------------------------------------- state
@dataclass(slots=True)
class LevelState:
    """One price level plus the shadow order resting in it."""

    size: float
    ahead: float
    behind: float = 0.0
    join_ts_ns: int = 0
    last_decrease_ts_ns: int | None = None


@dataclass(slots=True)
class AssetState:
    asset_id: str
    shard: int | None = None
    anchored: bool = False
    reanchor_pending: bool = False
    last_ts_ns: int = 0
    levels: dict[tuple[str, float], LevelState] = field(default_factory=dict)


@dataclass
class L3Counters:
    """Everything a replay needs to prove what it did (and did not) see."""

    events: int = 0
    books: int = 0
    book_levels: int = 0
    price_changes: int = 0
    noop_changes: int = 0
    increases: int = 0
    fills: int = 0
    cancels: int = 0
    ambiguous: int = 0
    trades: int = 0
    rows: int = 0
    gaps: int = 0
    gap_resets: int = 0
    dropped_changes: int = 0
    anchor_checks: int = 0
    anchor_mismatches: int = 0
    book_unchanged: int = 0
    fill_volume: float = 0.0
    cancel_volume: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            name: getattr(self, name)
            for name in (
                "events",
                "books",
                "book_levels",
                "book_unchanged",
                "price_changes",
                "noop_changes",
                "increases",
                "fills",
                "cancels",
                "ambiguous",
                "trades",
                "rows",
                "gaps",
                "gap_resets",
                "dropped_changes",
                "anchor_checks",
                "anchor_mismatches",
                "fill_volume",
                "cancel_volume",
            )
        }


def _row(
    *,
    recv_ts_ns: int,
    asset_id: str,
    side: str,
    price: float,
    size_at_level: float,
    q_ahead: float,
    q_behind: float,
    cancel_size: float = 0.0,
    fill_size: float = 0.0,
    replenishment_latency: int | None = None,
    survival: int = 1,
    queue_age_ns: int = 0,
    attribution: str,
    ambiguity: bool = False,
    discontinuity: bool = False,
) -> dict[str, Any]:
    total = q_ahead + q_behind
    return {
        "recv_ts_ns": int(recv_ts_ns),
        "asset_id": asset_id,
        "side": side,
        "price": price,
        "size_at_level": float(size_at_level),
        "q_ahead": float(q_ahead),
        "q_behind": float(q_behind),
        "queue_ratio": float(q_ahead / total) if total > SIZE_EPSILON else 0.0,
        "cancel_size": float(cancel_size),
        "fill_size": float(fill_size),
        "replenishment_latency": None if replenishment_latency is None else int(replenishment_latency),
        "survival": int(survival),
        "queue_age_ns": int(queue_age_ns),
        "attribution": attribution,
        "attribution_ambiguous": bool(ambiguity),
        "discontinuity": bool(discontinuity),
    }


# -------------------------------------------------------------------- replayer
class L3Replayer:
    """Order-book replay -> per-level queue telemetry.

    Pure and clock-free: every method takes the event time. Feed it in
    ``recv_ts_ns`` order per asset. Trades may arrive before or after the level
    update they caused (the venue pushes ``last_trade_price`` around the same
    time as the change), so they land in a pool and are claimed by the first
    decrease at the same asset/side/price inside the match window.
    """

    def __init__(self, *, match_window_ns: int = DEFAULT_MATCH_WINDOW_NS) -> None:
        self.match_window_ns = int(match_window_ns)
        self.counters = L3Counters()
        self._assets: dict[str, AssetState] = {}
        # shard -> sorted reconnect timestamps
        self._gaps: dict[int, list[int]] = {}
        # (asset, book side, price) -> list of [recv_ts_ns, remaining size]
        self._trades: dict[tuple[str, str, float], list[list[float]]] = {}

    # -- plumbing ---------------------------------------------------------
    def state(self, asset_id: str) -> AssetState | None:
        return self._assets.get(asset_id)

    def reset(self) -> None:
        """Forget all book state, pools and counters."""
        self.reset_counters()
        self._assets.clear()
        self._gaps.clear()
        self._trades.clear()

    def reset_counters(self) -> L3Counters:
        """Zero the counters, keeping book state (used after seeding anchors)."""
        self.counters = L3Counters()
        return self.counters

    def _asset(self, asset_id: str, shard: int | None) -> AssetState:
        state = self._assets.get(asset_id)
        if state is None:
            state = AssetState(asset_id=asset_id)
            self._assets[asset_id] = state
        if shard is not None:
            state.shard = int(shard)
        return state

    def add_gap(self, *, shard: int | None, recv_ts_ns: int) -> None:
        """Register a disconnect window for a shard.

        The shard's assets are unanchored the next time they are touched: a
        ``price_change`` after a gap cannot be trusted to follow the last
        snapshot we saw, so it is dropped until the venue sends a book again.
        """
        self.counters.gaps += 1
        if shard is None:
            return
        stamps = self._gaps.setdefault(int(shard), [])
        bisect.insort(stamps, int(recv_ts_ns))

    def _apply_gaps(self, state: AssetState, prev_ts_ns: int, ts_ns: int) -> None:
        if state.shard is None:
            return
        stamps = self._gaps.get(state.shard)
        if not stamps:
            return
        for gap_ts in stamps:
            if gap_ts <= prev_ts_ns:
                continue
            if gap_ts > ts_ns:
                break
            if state.anchored:
                state.levels.clear()
                state.anchored = False
                state.reanchor_pending = True
                self.counters.gap_resets += 1

    # -- books ------------------------------------------------------------
    def on_book(
        self,
        *,
        asset_id: str,
        recv_ts_ns: int,
        bids: Sequence[Any] | None,
        asks: Sequence[Any] | None,
        shard: int | None = None,
    ) -> list[dict[str, Any]]:
        """Anchor (or re-anchor) every level from a full-depth snapshot.

        Every level restarts with the observed size as its queue ahead. Rows are
        emitted only where the snapshot says something new (a level appeared,
        changed or vanished) — an unchanged restatement of the replayed book is
        a benign resync, not telemetry, and is not flagged as a discontinuity.
        """
        state = self._asset(asset_id, shard)
        prev = state.last_ts_ns
        self._apply_gaps(state, prev, recv_ts_ns)
        self.counters.events += 1

        previous = state.levels
        mismatches = self._check_anchor(state, bids, asks) if previous else 0
        discontinuity = state.reanchor_pending or mismatches > 0

        state.levels = {}
        rows: list[dict[str, Any]] = []
        for side, raw in ((BOOK_SIDE_BID, bids), (BOOK_SIDE_ASK, asks)):
            for price, size in parse_levels(raw):
                key = (side, price)
                if key in state.levels:  # duplicate price in one snapshot
                    continue
                state.levels[key] = LevelState(size=size, ahead=size, join_ts_ns=recv_ts_ns)
                old = previous.get(key)
                if old is not None and abs(old.size - size) <= SIZE_TOLERANCE:
                    self.counters.book_unchanged += 1
                    continue
                rows.append(
                    _row(
                        recv_ts_ns=recv_ts_ns,
                        asset_id=asset_id,
                        side=side,
                        price=price,
                        size_at_level=size,
                        q_ahead=size,
                        q_behind=0.0,
                        survival=1,
                        queue_age_ns=0,
                        attribution="anchor",
                        discontinuity=discontinuity,
                    )
                )

        for key in previous:
            if key in state.levels:
                continue
            # The snapshot dropped the level. Whether it was filled or cancelled
            # between the two anchors is not knowable from a snapshot.
            rows.append(
                _row(
                    recv_ts_ns=recv_ts_ns,
                    asset_id=asset_id,
                    side=key[0],
                    price=key[1],
                    size_at_level=0.0,
                    q_ahead=0.0,
                    q_behind=0.0,
                    survival=0,
                    queue_age_ns=0,
                    attribution="anchor",
                    discontinuity=discontinuity,
                )
            )

        state.anchored = True
        state.reanchor_pending = False
        state.last_ts_ns = max(prev, int(recv_ts_ns))
        self.counters.books += 1
        self.counters.book_levels += len(rows)
        self.counters.rows += len(rows)
        return rows

    def _check_anchor(
        self,
        state: AssetState,
        bids: Sequence[Any] | None,
        asks: Sequence[Any] | None,
    ) -> int:
        """Compare replayed level sizes with the incoming snapshot.

        A mismatch means an update between the two anchors was missed or not
        sent; the counter is the tape's own consistency check, and its count
        here is what makes the following anchor a discontinuity.
        """
        seen: dict[tuple[str, float], float] = {}
        for side, raw in ((BOOK_SIDE_BID, bids), (BOOK_SIDE_ASK, asks)):
            for price, size in parse_levels(raw):
                seen[(side, price)] = size
        mismatches = 0
        for key, level in state.levels.items():
            self.counters.anchor_checks += 1
            other = seen.get(key)
            if other is None or abs(other - level.size) > SIZE_TOLERANCE:
                mismatches += 1
        for key in seen:
            if key not in state.levels:
                self.counters.anchor_checks += 1
                mismatches += 1
        self.counters.anchor_mismatches += mismatches
        return mismatches

    # -- deltas -----------------------------------------------------------
    def on_changes(
        self,
        *,
        asset_id: str,
        recv_ts_ns: int,
        changes: Iterable[Mapping[str, Any]] | None,
        shard: int | None = None,
    ) -> list[dict[str, Any]]:
        """Apply a frame's change list, keeping only this asset's changes."""
        rows: list[dict[str, Any]] = []
        for change in changes or ():
            if not isinstance(change, Mapping):
                continue
            change_asset = change.get("asset_id")
            if change_asset is not None and str(change_asset) != asset_id:
                continue
            rows.extend(
                self.on_price_change(
                    asset_id=asset_id,
                    recv_ts_ns=recv_ts_ns,
                    side=change.get("side"),
                    price=change.get("price"),
                    size=change.get("size"),
                    shard=shard,
                )
            )
        return rows

    def on_price_change(
        self,
        *,
        asset_id: str,
        recv_ts_ns: int,
        side: Any,
        price: Any,
        size: Any,
        shard: int | None = None,
    ) -> list[dict[str, Any]]:
        """Apply one ``price_change`` entry (new aggregate size at a level)."""
        state = self._asset(asset_id, shard)
        prev = state.last_ts_ns
        self._apply_gaps(state, prev, recv_ts_ns)
        self.counters.events += 1
        state.last_ts_ns = max(prev, int(recv_ts_ns))

        book = book_side(side)
        level_price = norm_price(price)
        if not state.anchored or book is None or level_price is None:
            self.counters.dropped_changes += 1
            return []

        try:
            new_size = float(size) if size is not None else 0.0
        except (TypeError, ValueError):
            self.counters.dropped_changes += 1
            return []
        if new_size < 0.0:
            new_size = 0.0

        key = (book, level_price)
        level = state.levels.get(key)
        old_size = level.size if level is not None else 0.0
        delta = new_size - old_size
        self.counters.price_changes += 1

        if abs(delta) <= SIZE_EPSILON:
            # Duplicate frame: the venue re-sends an unchanged aggregate size.
            self.counters.noop_changes += 1
            return []

        if delta > 0.0:
            return self._increase(
                state=state,
                key=key,
                level=level,
                new_size=new_size,
                added=delta,
                recv_ts_ns=int(recv_ts_ns),
            )
        return self._decrease(
            state=state,
            key=key,
            level=level,
            new_size=new_size,
            removed=-delta,
            recv_ts_ns=int(recv_ts_ns),
        )

    def _increase(
        self,
        *,
        state: AssetState,
        key: tuple[str, float],
        level: LevelState | None,
        new_size: float,
        added: float,
        recv_ts_ns: int,
    ) -> list[dict[str, Any]]:
        replenishment: int | None = None
        if level is None:
            level = LevelState(size=new_size, ahead=new_size, join_ts_ns=recv_ts_ns)
            state.levels[key] = level
        else:
            level.size = new_size
            level.behind += added
            if level.last_decrease_ts_ns is not None:
                replenishment = recv_ts_ns - level.last_decrease_ts_ns
                level.last_decrease_ts_ns = None
        self.counters.increases += 1
        row = _row(
            recv_ts_ns=recv_ts_ns,
            asset_id=state.asset_id,
            side=key[0],
            price=key[1],
            size_at_level=new_size,
            q_ahead=level.ahead,
            q_behind=level.behind,
            replenishment_latency=replenishment,
            survival=1,
            queue_age_ns=recv_ts_ns - level.join_ts_ns,
            attribution="replenish" if replenishment is not None else "increase",
        )
        self.counters.rows += 1
        return [row]

    def _decrease(
        self,
        *,
        state: AssetState,
        key: tuple[str, float],
        level: LevelState,
        new_size: float,
        removed: float,
        recv_ts_ns: int,
    ) -> list[dict[str, Any]]:
        fill = self._claim_trades(
            asset_id=state.asset_id,
            book_side=key[0],
            price=key[1],
            want=removed,
            ts_ns=recv_ts_ns,
        )
        cancel = max(0.0, removed - fill)
        ambiguous = fill > SIZE_EPSILON and fill < removed - SIZE_EPSILON
        if ambiguous:
            attribution = "ambiguous"
            self.counters.ambiguous += 1
        elif fill > SIZE_EPSILON:
            attribution = "fill"
            self.counters.fills += 1
        else:
            attribution = "cancel"
            self.counters.cancels += 1
        self.counters.fill_volume += fill
        self.counters.cancel_volume += cancel

        ahead_before = level.ahead
        # FIFO: the front of the level trades/cancels first, then the queue
        # behind the shadow order.
        level.ahead = max(0.0, ahead_before - removed)
        level.behind = max(0.0, level.behind - max(0.0, removed - ahead_before))
        level.size = new_size
        level.last_decrease_ts_ns = recv_ts_ns

        filled = removed > ahead_before + SIZE_EPSILON
        emptied = new_size <= SIZE_EPSILON
        row = _row(
            recv_ts_ns=recv_ts_ns,
            asset_id=state.asset_id,
            side=key[0],
            price=key[1],
            size_at_level=new_size,
            q_ahead=level.ahead,
            q_behind=level.behind,
            cancel_size=cancel,
            fill_size=fill,
            survival=0 if (filled or emptied) else 1,
            queue_age_ns=recv_ts_ns - level.join_ts_ns,
            attribution=attribution,
            ambiguity=ambiguous,
        )

        if emptied:
            state.levels.pop(key, None)
        elif filled:
            # The shadow order left the book; a continuously quoting maker is
            # back at the tail of what remains the instant it is filled.
            level.ahead = new_size
            level.behind = 0.0
            level.join_ts_ns = recv_ts_ns

        self.counters.rows += 1
        return [row]

    # -- trades -----------------------------------------------------------
    def on_trade(
        self,
        *,
        asset_id: str,
        recv_ts_ns: int,
        side: Any,
        price: Any,
        size: Any,
    ) -> None:
        """Add a print to the pool that later decreases can claim."""
        book = TAKER_BOOK_SIDE.get(str(side or "").strip().upper())
        level_price = norm_price(price)
        if book is None or level_price is None:
            return
        try:
            amount = float(size) if size is not None else 0.0
        except (TypeError, ValueError):
            return
        if amount <= SIZE_EPSILON:
            return
        ts_ns = int(recv_ts_ns)
        key = (asset_id, book, level_price)
        pool = self._trades.setdefault(key, [])
        pool.append([ts_ns, amount])
        self.counters.trades += 1
        # Anything older than the far end of the match window can never explain
        # a future decrease; dropping it keeps the pool bounded.
        horizon = ts_ns - 2 * self.match_window_ns
        while pool and pool[0][0] < horizon:
            pool.pop(0)

    def _claim_trades(
        self, *, asset_id: str, book_side: str, price: float, want: float, ts_ns: int
    ) -> float:
        pool = self._trades.get((asset_id, book_side, price))
        if not pool:
            return 0.0
        matched = 0.0
        keep: list[list[float]] = []
        for entry in pool:
            trade_ts, remaining = entry
            if abs(trade_ts - ts_ns) > self.match_window_ns:
                if trade_ts >= ts_ns - self.match_window_ns:
                    keep.append(entry)
                continue
            if matched >= want - SIZE_EPSILON:
                keep.append(entry)
                continue
            take = min(remaining, want - matched)
            remaining -= take
            matched += take
            if remaining > SIZE_EPSILON:
                keep.append([trade_ts, remaining])
        if keep:
            self._trades[(asset_id, book_side, price)] = keep
        else:
            self._trades.pop((asset_id, book_side, price), None)
        return matched


# --------------------------------------------------------------------- session
class L3Session:
    """Drives one :class:`L3Replayer` from a time-ordered stream of tape rows.

    A row is a dict with ``kind`` (``book`` | ``price_change`` | ``trade`` |
    ``gap``) and ``recv_ts_ns``. Rows must be ordered inside one asset's stream;
    assets may be grouped in any order (the driver streams the tape sorted by
    ``asset_id, recv_ts_ns``).

    Trades are ingested the moment they are pulled; level events are held until
    the watermark passes their timestamp plus ``lookahead_ns``. That delay is
    what lets a print pushed *after* the ``price_change`` it caused still be
    claimed by that decrease instead of defaulting to a cancel.
    """

    def __init__(self, replayer: L3Replayer | None = None, *, lookahead_ns: int | None = None) -> None:
        self.replayer = replayer or L3Replayer()
        self.lookahead_ns = (
            int(lookahead_ns) if lookahead_ns is not None else self.replayer.match_window_ns
        )

    def feed(self, rows: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
        buffer: list[Mapping[str, Any]] = []
        last_asset: str | None = None
        for row in rows:
            if row.get("kind") == "trade":
                self.replayer.on_trade(
                    asset_id=str(row["asset_id"]),
                    recv_ts_ns=int(row["recv_ts_ns"]),
                    side=row.get("side"),
                    price=row.get("price"),
                    size=row.get("size"),
                )
                continue
            asset_id = row.get("asset_id")
            if last_asset is not None and asset_id != last_asset:
                # Asset streams are contiguous; flush the previous asset's tail
                # so its watermark work does not wait on a later asset's clock.
                for held in buffer:
                    yield from self._emit(held)
                buffer.clear()
            last_asset = asset_id
            buffer.append(row)
            horizon = int(row["recv_ts_ns"]) - self.lookahead_ns
            while buffer and int(buffer[0]["recv_ts_ns"]) <= horizon:
                yield from self._emit(buffer.pop(0))
        for held in buffer:
            yield from self._emit(held)

    def _emit(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = row.get("kind")
        ts_ns = int(row["recv_ts_ns"])
        shard = row.get("shard")
        if kind == "book":
            return self.replayer.on_book(
                asset_id=str(row["asset_id"]),
                recv_ts_ns=ts_ns,
                bids=row.get("bids"),
                asks=row.get("asks"),
                shard=shard,
            )
        if kind == "price_change":
            changes = row.get("changes")
            if changes is None:
                changes = [{"side": row.get("side"), "price": row.get("price"), "size": row.get("size")}]
            return self.replayer.on_changes(
                asset_id=str(row["asset_id"]),
                recv_ts_ns=ts_ns,
                changes=changes,
                shard=shard,
            )
        if kind == "gap":
            self.replayer.add_gap(shard=shard, recv_ts_ns=ts_ns)
            return []
        return []


# ---------------------------------------------------------------------- output
def write_rows(path: Any, rows: Sequence[Mapping[str, Any]]) -> int:
    """Write queue rows as one Parquet file (ZSTD, like the tape parts)."""
    if not rows:
        return 0
    table = pa.Table.from_pylist(list(rows), schema=L3_QUEUE_SCHEMA)
    pq.write_table(table, str(path), compression="zstd")
    return table.num_rows
