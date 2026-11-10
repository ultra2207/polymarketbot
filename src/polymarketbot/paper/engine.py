"""The paper engine: one clock, one decision loop, one ledger.

This module is the coordinator: it owns the tick order, the shared state and
the accounting, and it delegates. The landing of venue actions
(``paper.actions``) and the reward runner (``paper.pillars.reward_runner``) are
free functions over this engine, reached through the one-line delegates at the
bottom of the class, which keep every original bound-method name available on
the instance.

Cash and inventory move only through an ``ActionBook`` that charges the
measured venue round trip, and taker actions re-price against the book that
exists when they land, never against the snapshot that triggered them.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper import actions as venue_actions
import polymarketbot.strategy.venue.reads as venue
from polymarketbot.paper.actions import Resting
from polymarketbot.strategy.books import best_bid
from polymarketbot.strategy.latency import ActionBook, RttProbe
from polymarketbot.strategy.ledger import Ledger
from polymarketbot.paper.pillars import reward_runner

log = get_logger(__name__)

STRATEGIES = ("RewardHarvester",)


class PaperEngine:
    def __init__(
        self,
        capital: dict[str, float],
        *,
        http: Any,
        gamma: Any,
        feed: venue.BookFeed,
        rtt: RttProbe,
        db_path: str,
        interval: float = 10.0,
        duration: Optional[float] = None,
        resume: bool = False,
        rtt_refresh_s: float = 60.0,
    ) -> None:
        self.interval = max(1.0, float(interval))
        self.duration = duration
        self.http = http
        self.gamma = gamma
        self.feed = feed
        self.rtt = rtt
        # The landing model pays whatever p50 the probe currently holds, so a
        # probe sampled once at startup prices every action for the whole run.
        # Measured drift between windows is 1.7x (344 ms at 04:14, 570 ms at
        # 10:10), so the estimate must follow the path: one probe per
        # `rtt_refresh_s` costs 1 sample per 6 ticks at the 10 s interval,
        # about 1.9 % of a 570 ms-tick budget.
        self._rtt_refresh_s = max(1.0, float(rtt_refresh_s))
        self.ledger = Ledger(capital, db_path, resume=resume)
        self.actions = ActionBook(self.rtt.p50)
        self.markets: list[venue.Market] = []
        self.by_condition: dict[str, venue.Market] = {}
        self._stage_seconds: dict[str, list[float]] = {}
        self._stage_log_ts = 0.0
        self._last_tick_dt = 0.0
        self._reward_accrual_ts = time.time()
        self._reward_log_ts = 0.0
        #: The open accrual window: the credited sum, the time-integrated
        #: notional and the equity it started from. The implied rate and the
        #: equity delta are both derived from this one window so they cannot
        #: describe different intervals.
        self._reward_window: Optional[dict[str, float]] = None
        #: KKT price of capital from the last reward allocation, USD/USD/day.
        #: None until solved; the CTF gate refuses rather than assumes 0.
        self._shadow_yield: Optional[float] = None
        self.reward_quotes: dict[str, Resting] = {}
        self.walker = venue.UniverseWalker(gamma, pages_per_tick=20)
        self._universe_log_ts = 0.0
        self._book_cursor = 0
        self._stop = threading.Event()
        self._fills = {name: 0 for name in STRATEGIES}

    # -- lifecycle -------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        started = time.time()
        last_snapshot = 0.0
        walker_thread = threading.Thread(
            target=self.walker.run_forever, args=(self._stop,),
            name="universe-walk", daemon=True,
        )
        walker_thread.start()
        # The first sample lands in main(); this thread follows the path from
        # there, so the landing model never pays a stale round trip.
        probe_thread = threading.Thread(
            target=self._probe_loop, name="venue-probes", daemon=True,
        )
        probe_thread.start()
        log.info(
            f"Paper engine start: capital={self.ledger.equity()} "
            f"interval={self.interval}s duration={self.duration} "
            f"rtt_refresh={self._rtt_refresh_s:.0f}s"
        )
        while not self._stop.is_set():
            if self.duration is not None and time.time() - started >= self.duration:
                break
            tick_started = time.monotonic()
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the soak
                log.exception("paper tick failed")
            if time.time() - last_snapshot >= self.interval:
                last_snapshot = time.time()
                self._snapshot()
            time.sleep(max(0.2, self.interval - (time.monotonic() - tick_started)))
        self._stop.set()
        walker_thread.join(timeout=5.0)
        probe_thread.join(timeout=5.0)
        self._snapshot()
        log.info(f"Paper engine stopped: equity={self.ledger.equity()}")

    # -- one tick --------------------------------------------------------
    def _tick(self) -> None:
        now = time.time()
        tick_started = time.monotonic()
        self.markets = self.walker.snapshot()
        complete = self.walker.complete
        self.by_condition = {m.condition_id: m for m in self.markets}
        if now - self._universe_log_ts >= 60.0:
            self._universe_log_ts = now
            log.info(f"universe: {len(self.markets)} known active markets "
                     f"({sum(1 for m in self.markets if m.daily_reward > 0)} rewarded, "
                     f"pass_complete={complete})")
        tick_start0 = tick_started
        tokens = [token for m in self.markets for token in m.token_ids]
        if tokens:
            size = min(len(tokens), 12000)
            window = tokens[self._book_cursor:self._book_cursor + size]
            if len(window) < size:
                window += tokens[: size - len(window)]
            self._book_cursor = (self._book_cursor + size) % len(tokens)
            self.feed.books(window)
        books = self.feed.snapshot()
        self._stage("books", tick_start0)
        stage0 = time.monotonic()
        prints = venue.tape(self.http, window_s=max(120.0, 4 * self.interval))
        notional: dict[str, float] = {}
        for quote in list(self.reward_quotes.values()):
            if quote.cancel_at is None:
                notional[quote.condition_id] = notional.get(quote.condition_id, 0.0) + (
                    quote.shares * quote.price
                )
        if notional:
            # Spend the bounded tape budget on the conditions holding the most
            # capital first; a $0.30 long-shot leg does not outrank a $400 bid.
            ordered = sorted(notional, key=notional.get, reverse=True)
            prints += venue.condition_tape(
                self.http, ordered, window_s=max(120.0, 4 * self.interval)
            )
        self._stage("tape", stage0)
        stage0 = time.monotonic()
        self._land(books, prints)
        self._stage("land", stage0)
        stage0 = time.monotonic()
        self._reward(books, prints)
        self._stage("reward", stage0)
        stage0 = time.monotonic()
        self._mark(books)
        self._stage("mark", stage0)
        self._last_tick_dt = time.monotonic() - tick_started
        self._stage("tick", tick_started)

    def _stage(self, name: str, started: float) -> None:
        """Accumulate per-stage tick cost and log the profile once a minute.

        The soak must hold its interval; a stage that silently grows with the
        universe is the difference between 180 snapshots and 71.
        """
        elapsed = time.monotonic() - started
        totals = self._stage_seconds.setdefault(name, [0.0, 0])
        totals[0] += elapsed
        totals[1] += 1
        now = time.monotonic()
        if now - self._stage_log_ts < 60.0:
            return
        self._stage_log_ts = now
        prof = " ".join(
            f"{key}={value[0] / value[1]:.2f}s" for key, value in sorted(self._stage_seconds.items())
        )
        log.info(f"tick profile: {prof}")
        self._stage_seconds = {}

    @staticmethod
    def _reserved(quotes: dict[str, Resting]) -> float:
        """Cash the resting bids would need if every one filled."""
        return sum(
            quote.shares * quote.price
            for quote in quotes.values()
            if quote.cancel_at is None and quote.shares > 0.0
        )

    # -- accounting ------------------------------------------------------
    def _mark(self, books: dict[str, dict]) -> None:
        for strategy in STRATEGIES:
            for lot in self.ledger.positions(strategy):
                # A lot the journal prices at par needs no book: the stored
                # value is the mark.
                if lot.par_value is not None:
                    continue
                bid = best_bid(books.get(lot.token_id) or {})
                if bid is None:
                    # Never invent a price to fill the gap. Keep the last real
                    # measurement as the value, clear `mark_live`, and say so
                    # once per measured -> unmeasured transition.
                    if self.ledger.mark_unmeasured(lot.token_id, strategy):
                        log.warning(
                            f"mark unmeasured: {strategy} {lot.token_id[:10]} "
                            f"{lot.shares:.2f} shares held at ${lot.mark():.4f} "
                            f"(last live ${lot.last_mark:.4f}); no bid this tick"
                        )
                elif self.ledger.mark(lot.token_id, bid, strategy):
                    log.info(
                        f"mark live: {strategy} {lot.token_id[:10]} "
                        f"{lot.shares:.2f} shares at bid ${bid:.4f}"
                    )

    def _rtt(self) -> float:
        return self.rtt.p50() or 0.0

    def _snapshot(self) -> None:
        equity = self.ledger.snapshot()
        rtt = self.rtt.p50()
        parts = []
        for name in STRATEGIES:
            positions = self.ledger.positions(name)
            value = sum(lot.shares * lot.mark() for lot in positions)
            unmarked = self.ledger.unmarked(name)
            unmarked_value = sum(lot.shares * lot.mark() for lot in unmarked)
            parts.append(
                f"{name}: cash=${self.ledger.cash(name):,.2f} "
                f"pos=${value:,.2f} eq=${equity[name]:,.2f} "
                f"lots={len(positions)} fills={self._fills[name]} "
                # Inventory the equity number contains without a live book behind
                # it: a counter, so `pos=$...` is never mistaken for a measurement.
                f"unmarked={len(unmarked)}/${unmarked_value:,.2f}"
            )
        log.info(" | ".join(parts) +
                 f" | rtt={f'{rtt*1000:.0f}ms' if rtt else 'unmeasured'} "
                 f"resting={len(self.reward_quotes)}"
                 # The pillar owns its own cash, so the reserve is shown against
                 # the balance it is actually drawn on.
                 f" reserved=reward {self._reserved(self.reward_quotes):.2f}"
                 f"/{self.ledger.cash('RewardHarvester'):.2f})")

    # -- delegates: the moved implementations, original names ------------
    def _refresh_venue_state(self) -> None: return venue_actions.refresh_venue_state(self)
    def _probe_loop(self) -> None: return venue_actions.probe_loop(self)
    def _land(self, books, prints) -> None: return venue_actions.land(self, books, prints)
    def _reward(self, books, prints) -> None: return reward_runner.run_reward(self, books, prints)
    def _apply_resting_fills(self, resting, strategy, books, prints, now) -> None:
        return reward_runner.apply_resting_fills(
            self, resting, strategy, books, prints, now
        )
