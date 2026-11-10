"""The paper engine: one clock, two decision loops, one ledger.

This module is the coordinator: it owns the tick order, the shared state and
the accounting, and it delegates. The settlement radar (``paper.radar``), the
landing of venue actions (``paper.actions``) and one runner per pillar
(``paper.pillars.*_runner``) are free functions over this engine, reached
through the one-line delegates at the bottom of the class, which keep every
original bound-method name available on the instance.

Cash and inventory move only through an ``ActionBook`` that charges the
measured venue round trip, and taker actions re-price against the book that
exists when they land, never against the snapshot that triggered them.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper import actions as venue_actions, radar, venue
from polymarketbot.paper.actions import Resting
from polymarketbot.paper.books import best_bid
from polymarketbot.paper.latency import ActionBook, RttProbe
from polymarketbot.paper.ledger import Ledger
# ``settlement`` is re-exported: callers patch the shared module object
# (``engine_mod.settlement.verdict_for``) and every runner must see the patch,
# so all of them import the same module object.
from polymarketbot.paper.pillars import reward_runner, settlement, settlement_runner, tail_budget  # noqa: F401
from polymarketbot.trading.domain2.settlement_allocator import MarginalSettlementAllocator
from polymarketbot.trading.domain2.settlement_fair_value import normalize_question

log = get_logger(__name__)

STRATEGIES = ("RewardHarvester", "SettlementHarvester")


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
        gas_usd: Optional[float] = None,
        resume: bool = False,
        enable_uma_polling: bool = True,
        rtt_refresh_s: float = 60.0,
        gas_probe: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self.interval = max(1.0, float(interval))
        self.duration = duration
        self.gas_usd = gas_usd
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
        # Gas is venue state like the round trip: measured 42.70 Gwei at 05:14
        # and 46.33 Gwei at 10:10, so one startup sample prices every on-chain
        # leg for the whole run. A failed refresh keeps the last measured
        # value - None here would disable the pillars and lose the sample.
        self._gas_probe = gas_probe
        self.ledger = Ledger(capital, db_path, resume=resume)
        self.actions = ActionBook(self.rtt.p50)
        self.markets: list[venue.Market] = []
        self.by_condition: dict[str, venue.Market] = {}
        self._token_index: dict[str, venue.Market] = {}
        self._indexed_markets = -1
        self._stage_seconds: dict[str, list[float]] = {}
        self._stage_log_ts = 0.0
        # Gamma rows fetched by condition id for verdict markets that the
        # walker does not list. Kept so a refusal states whether Gamma has no
        # such market or the market is closed - the difference between a
        # coverage gap and protocol reality.
        self._verdict_rows: dict[str, dict] = {}
        # Every settlement-stage network call runs on the radar thread below,
        # never inside the tick. Measured 2026-11-10 14:17 window:
        # ``settlement=15.11s ... tick=18.76s`` and ``settlement=22.06s ...
        # tick=13.56s`` inside a 5 s tick, settling to 0.31-0.67 s once the
        # one-per-minute refresh had run. Three blocking calls shared that
        # 60 s gate: the UMA ``eth_getLogs`` sweep, the Gamma row fetch for
        # verdict conditions the walker does not list, and the Gamma row fetch
        # for held conditions. Their latency is the venue's, so they cannot be
        # made fast - only moved off the critical path, exactly as the RTT and
        # gas probes were.
        self._radar_markets: dict[str, venue.Market] = {}
        self._radar_lock = threading.Lock()
        self._radar_want: tuple[str, ...] = ()
        self._radar_fetched: dict[str, float] = {}
        self._radar_wake = threading.Event()
        self._radar_cycles = 0
        self._radar_cost = 0.0
        #: The tail-budget settlement allocator.  It maximises expected profit
        #: inside the loss the book may carry at the stated confidence, with the
        #: tail probability *measured* from the proposal ledger.  All three
        #: inputs (appetite, band, evidence) are required and are resolved here,
        #: once, from one named place: a paper book that cannot state its
        #: appetite does not spend.
        self.settlement_allocator = MarginalSettlementAllocator("tail_budget")
        self._tail_policy, self._tail_evidence = tail_budget.resolve_tail_inputs()
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
        self.settlement_model = settlement.model()
        self.poller = self._make_uma_poller() if enable_uma_polling else None
        self.walker = venue.UniverseWalker(gamma, pages_per_tick=20)
        self._universe_log_ts = 0.0
        self._book_cursor = 0
        self._held_ts = 0.0
        self._settlement_log_ts = 0.0
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
        radar_thread = threading.Thread(
            target=self._settlement_radar_loop, name="settlement-radar", daemon=True,
        )
        radar_thread.start()
        log.info(
            f"Paper engine start: capital={self.ledger.equity()} gas_usd={self.gas_usd} "
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
        radar_thread.join(timeout=5.0)
        self._snapshot()
        log.info(f"Paper engine stopped: equity={self.ledger.equity()}")

    # -- one tick --------------------------------------------------------
    def _tick(self) -> None:
        now = time.time()
        tick_started = time.monotonic()
        self.markets = self.walker.snapshot()
        complete = self.walker.complete
        self.by_condition = {m.condition_id: m for m in self.markets}
        if len(self.markets) != self._indexed_markets:
            self._token_index = {t: m for m in self.markets for t in m.token_ids}
            self._indexed_markets = len(self.markets)
            bound = self.settlement_model.tape.bind_condition_ids(
                {normalize_question(m.question): m.condition_id for m in self.markets}
            )
            if bound:
                log.info(f"verdict tape: bound {bound} proposal(s) to walked condition ids")
        if now - self._universe_log_ts >= 60.0:
            self._universe_log_ts = now
            log.info(f"universe: {len(self.markets)} known active markets "
                     f"({sum(1 for m in self.markets if m.daily_reward > 0)} rewarded, "
                     f"pass_complete={complete})")
        tick_start0 = tick_started
        tokens = [token for m in self.markets for token in m.token_ids]
        if tokens:
            size = min(len(tokens), 12000)
            # A verdict market is the only place a live par edge exists, and it
            # can sit anywhere in the rotation. Price it every tick, then spend
            # the remaining budget on the round-robin slice.
            priority: list[str] = []
            for cid in self.settlement_model.tape.live_verdict_condition_ids(now):
                market = self.by_condition.get(cid)
                if market is not None:
                    priority.extend(market.token_ids)
            priority = list(dict.fromkeys(priority))
            window = priority[:size]
            need = size - len(window)
            if need > 0:
                rotated = tokens[self._book_cursor:self._book_cursor + need]
                if len(rotated) < need:
                    rotated += tokens[: need - len(rotated)]
                seen = set(window)
                window += [token for token in rotated if token not in seen]
                self._book_cursor = (self._book_cursor + need) % len(tokens)
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
        self._settlement(books)
        self._stage("settlement", stage0)
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
                bid = best_bid(books.get(lot.token_id) or {})
                if bid is not None:
                    self.ledger.mark(lot.token_id, bid)

    def _rtt(self) -> float:
        return self.rtt.p50() or 0.0

    def _snapshot(self) -> None:
        equity = self.ledger.snapshot()
        rtt = self.rtt.p50()
        parts = []
        for name in STRATEGIES:
            positions = self.ledger.positions(name)
            value = sum(lot.shares * lot.mark() for lot in positions)
            parts.append(
                f"{name}: cash=${self.ledger.cash(name):,.2f} "
                f"pos=${value:,.2f} eq=${equity[name]:,.2f} "
                f"lots={len(positions)} fills={self._fills[name]}"
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
    def _settlement_radar_loop(self) -> None: return radar.settlement_radar_loop(self)
    def _fetch_verdict_rows(self, wanted, now) -> None: return radar.fetch_verdict_rows(self, wanted, now)
    def _radar_snapshot(self): return radar.radar_snapshot(self)
    def _market_refusal(self, condition_id, verdict_rows): return radar.market_refusal(condition_id, verdict_rows)
    def _make_uma_poller(self) -> Any: return radar.make_uma_poller(self)
    def _land(self, books, prints) -> None: return venue_actions.land(self, books, prints)
    def _land_settlement(self, strategy, payload, books) -> None:
        return venue_actions.land_settlement(self, strategy, payload, books)
    def _pending_for(self, condition_id) -> bool: return venue_actions.pending_for(self, condition_id)
    def _reward(self, books, prints) -> None: return reward_runner.run_reward(self, books, prints)
    def _apply_resting_fills(self, resting, strategy, books, prints, now) -> None:
        return reward_runner.apply_resting_fills(
            self, resting, strategy, books, prints, now
        )
    def _settlement(self, books) -> None: return settlement_runner.run_settlement(self, books)
    def _tail_budget(self, priced, books, verdict_rows, settle_sec, cash) -> dict[str, float]:
        return tail_budget.tail_budget(
            self, priced, books, verdict_rows, settle_sec, cash
        )
    def _reconcile_held(self, verdict_rows) -> None:
        return settlement_runner.reconcile_held(self, verdict_rows)
