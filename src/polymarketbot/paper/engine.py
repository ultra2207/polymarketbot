"""The paper engine: one clock, three decision loops, one ledger.

I/O lives here. Every pillar is a pure decision module under ``pillars/``.
Cash and inventory move only through an ``ActionBook`` that charges the
measured venue round trip, and taker actions re-price against the book that
exists when they land, never against the snapshot that triggered them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper import venue
from polymarketbot.paper.books import best_ask, best_bid, ladder, shares_at
from polymarketbot.paper.latency import ActionBook, RttProbe
from polymarketbot.paper.ledger import Ledger
from polymarketbot.paper.pillars import ctf, reward, settlement
from polymarketbot.trading.domain2.settlement_fair_value import normalize_question

log = get_logger(__name__)

STRATEGIES = ("RewardHarvester", "SettlementHarvester", "CtfMinter")


@dataclass
class Resting:
    strategy: str
    condition_id: str
    token_id: str
    price: float
    shares: float
    min_order: float
    placed: float
    cancel_at: Optional[float] = None
    tick: float = 0.01
    daily_payout: float = 0.0
    """Optimizer payout estimate in USDC/day for the capital behind this quote."""
    seen_ts: float = 0.0
    """Newest print already consumed by the fill model; stops re-counting."""
    accumulated: float = 0.0
    """Sub-min-order fill flow carried until it can clear the venue minimum."""
    initial_shares: float = 0.0
    """Size when placed; accrual scales with what is still resting."""
    max_age_s: float = 0.0
    """Lifetime the quote's EV was measured over; 0.0 = unbounded.

    A maker quote justified by ``p_fill`` at a requote horizon earns a
    probability nobody measured once it outlives that horizon, so it is pulled
    at ``placed + max_age_s`` rather than resting on an unmeasured probability.
    """


# Reasons a verdict market produced no settlement candidate. Named so a
# zero-fill window states which one blocked it instead of logging nothing.
NO_MARKET = "no_market_in_universe"
NO_VERDICT = "no_live_verdict"
NO_ASK = "no_ask"
NO_EDGE = "edge_unpriced"
NEGATIVE_EDGE = "edge_negative"
# Gamma returned the row, but it is closed and no longer accepting orders, so
# there is no book to buy. Distinct from ``NO_MARKET`` on purpose: one is a
# coverage gap (Gamma has no such market) and the other is protocol reality.
MARKET_CLOSED = "market_closed"

# Once the settlement tally stops changing, reprint it this often so a silent
# radar is still legible as alive. Five minutes is long enough that a steady
# state costs six lines an hour instead of sixty.
_SETTLEMENT_HEARTBEAT_SEC = 300.0


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
        self._verdict_fetch_ts = 0.0
        # Gamma rows fetched by condition id for verdict markets that the
        # walker does not list. Kept so a refusal states whether Gamma has no
        # such market or the market is closed - the difference between a
        # coverage gap and protocol reality.
        self._verdict_rows: dict[str, dict] = {}
        self._ctf_budget = 1500
        self._ctf_cursor = 0
        self._ctf_coverage_ts = 0.0
        self._last_tick_dt = 0.0
        self._reward_accrual_ts = time.time()
        self._reward_log_ts = 0.0
        #: KKT price of capital from the last reward allocation, USD/USD/day.
        #: None until solved; the CTF gate refuses rather than assumes 0.
        self._shadow_yield: Optional[float] = None
        self.reward_quotes: dict[str, Resting] = {}
        self.maker_quotes: dict[str, Resting] = {}
        self.settlement_model = settlement.model()
        self.poller = self._make_uma_poller() if enable_uma_polling else None
        self.walker = venue.UniverseWalker(gamma, pages_per_tick=20)
        self._universe_log_ts = 0.0
        self._book_cursor = 0
        self._held_ts = 0.0
        self._settlement_log_ts = 0.0
        self._stop = threading.Event()
        self._fills = {name: 0 for name in STRATEGIES}


    def _refresh_venue_state(self) -> None:
        """One measured refresh of venue state: CLOB round trip, then gas.

        Run on its own thread, not in the tick: both are network round trips
        that cost 0.2-0.9 s, and paying them inside the tick pushed it past the
        snapshot interval (measured `rtt=0.22s gas=0.90s` on a 10.06 s tick).
        """
        refreshed = self.rtt.measure()
        if refreshed is None:
            log.error("RTT refresh failed and no persisted probe exists: "
                      "venue actions will be refused, not landed at 0 ms")
        if self._gas_probe is None:
            return
        try:
            repriced = self._gas_probe()
        except Exception:  # noqa: BLE001 - a failed probe is not a failed tick
            log.exception("gas refresh failed; keeping the last measured cost")
            return
        if repriced is None:
            log.warning("gas refresh unmeasured; keeping last measured "
                        f"${self.gas_usd if self.gas_usd is not None else float('nan'):.5f}")
            return
        self.gas_usd = repriced

    def _probe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_venue_state()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad probe
                log.exception("venue probe failed")
            self._stop.wait(self._rtt_refresh_s)

    def _make_uma_poller(self) -> Any:
        """Wire live UMA proposal polling when an RPC provider is configured.

        The block interval is measured from two consecutive heads; until it is
        measured the poller is not constructed, because an invented interval
        makes every proposal's age (and therefore its liveness) wrong.
        """
        try:
            from polymarketbot.chain import ChainClient
            from polymarketbot.paper import uma_poll
        except Exception:  # noqa: BLE001 - optional chain dependency
            return None
        # The tape's header sequence is the primary measurement: 407
        # consecutive headers beat two live heads, and it is the same number
        # the on-chain flight model uses, so the poller and the CTF legs cannot
        # disagree about the protocol's block time.
        interval_s: Optional[float] = None
        try:
            from polymarketbot.trading.ctf import load_measured_block_interval_s

            interval_s = load_measured_block_interval_s()
        except Exception as exc:  # noqa: BLE001 - no tape is a valid paper mode
            log.warning(f"Block interval not on tape ({exc}); measuring live heads")
        try:
            chain = ChainClient()
            first_block = int(chain.block_number())
        except Exception as exc:  # noqa: BLE001 - no RPC is a valid paper mode
            log.warning(f"UMA polling disabled: no RPC head ({exc})")
            return None
        if interval_s is None:
            first_ts = time.monotonic()
            time.sleep(min(5.0, max(1.0, self.interval / 2.0)))
            try:
                second_block = int(chain.block_number())
            except Exception:  # noqa: BLE001
                return None
            elapsed = max(1e-6, time.monotonic() - first_ts)
            advanced = second_block - first_block
            if advanced <= 0:
                log.warning("UMA polling disabled: block interval did not advance")
                return None
            # Seconds per block. Dividing blocks by elapsed measures blocks
            # per second, which reported 0.79 s while the header tape measured
            # 1.50 s - a 1.9x error in every proposal age and liveness window.
            interval_s = elapsed / advanced
        if interval_s <= 0.0:
            log.warning("UMA polling disabled: block interval did not advance")
            return None
        poller = uma_poll.UmaProposalPoller(
            tape=self.settlement_model.tape,
            providers=[chain],
            state_path="data/derived/uma/uma_poll_state.json",
            block_interval_s=interval_s,
            liveness_fallback_sec=uma_poll.UMA_POLL_LIVENESS_FALLBACK_SEC,
            max_blocks=uma_poll.UMA_POLL_MAX_BLOCKS,
            max_windows=uma_poll.UMA_POLL_MAX_WINDOWS,
            confirmations=uma_poll.UMA_POLL_CONFIRMATIONS,
        )
        log.info(f"UMA live polling enabled: block interval {interval_s:.2f}s")
        return poller

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
        for quote in list(self.reward_quotes.values()) + list(self.maker_quotes.values()):
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
        self._ctf(books, prints)
        self._stage("ctf", stage0)
        stage0 = time.monotonic()
        self._mark(books)
        self._stage("mark", stage0)
        self._last_tick_dt = time.monotonic() - tick_started
        self._stage("tick", tick_started)

    # -- landing ---------------------------------------------------------
    def _land(self, books: dict[str, dict], prints: list[venue.Print]) -> None:
        for action in self.actions.due():
            payload = action.payload
            if action.kind == "reward_cancel":
                self.reward_quotes.pop(payload["token_id"], None)
            elif action.kind == "reward_quote":
                self.reward_quotes[payload["token_id"]] = payload["resting"]
            elif action.kind == "ctf_cancel":
                self.maker_quotes.pop(payload["token_id"], None)
            elif action.kind == "ctf_quote":
                self.maker_quotes[payload["token_id"]] = payload["resting"]
            elif action.kind in ("settle_buy", "ctf_taker"):
                market = self.by_condition.get(payload["condition_id"])
                fresh = (self.feed.books(market.token_ids, force=True)
                         if market is not None else books)
                if action.kind == "settle_buy":
                    self._land_settlement(action.strategy, payload, fresh)
                else:
                    self._land_ctf_taker(action.strategy, payload, fresh)
            elif action.kind == "ctf_merge":
                self.ledger.merge(
                    action.strategy,
                    condition_id=payload["condition_id"],
                    token_id=payload["token_id"],
                    pair_shares=payload["pair_shares"],
                    gas=payload["gas"],
                )

    def _land_settlement(self, strategy: str, payload: dict, books: dict[str, dict]) -> None:
        condition_id = payload["condition_id"]
        market = self.by_condition.get(condition_id)
        if market is None:
            return
        verdict = settlement.verdict_for(self.settlement_model, market)
        if verdict is None:
            return
        token_id = market.token_ids[verdict.winning_index]
        book = books.get(token_id) or {}
        legs = settlement.fundable_legs(
            market, verdict, ladder(book, asks=True), self.ledger.cash(strategy)
        )
        for leg in legs:
            if self.ledger.buy(
                strategy,
                condition_id=condition_id,
                token_id=leg.token_id,
                shares=leg.shares,
                price=leg.price,
                fee=leg.fee_usd,
                par_value=settlement.mark_value(market, verdict),
                tag="settlement",
            ):
                self._fills[strategy] += 1
                log.info(f"settled buy {leg.shares:.1f} @ {leg.price:.4f} "
                         f"p_win={leg.p_win:.3f} edge=${leg.edge_usd:.4f}")

    def _land_ctf_taker(self, strategy: str, payload: dict, books: dict[str, dict]) -> None:
        market = self.by_condition.get(payload["condition_id"])
        if market is None or not market.binary:
            return
        yes, no = market.token_ids
        tranches = ctf.optimize_taker_parity(
            market, books.get(yes) or {}, books.get(no) or {},
            self.ledger.cash(strategy), self.gas_usd or 0.0,
        )
        for tranche in tranches:
            if self.ledger.buy(strategy, condition_id=market.condition_id, token_id=yes,
                               shares=tranche.shares, price=tranche.price_yes,
                               fee=tranche.fee_yes, tag="ctf_leg"):
                if self.ledger.buy(strategy, condition_id=market.condition_id, token_id=no,
                                   shares=tranche.shares, price=tranche.price_no,
                                   fee=tranche.fee_no, tag="ctf_leg"):
                    self._fills[strategy] += 1
                    log.info(f"ctf parity {tranche.shares:.1f} sh "
                             f"@{tranche.price_yes:.4f}+{tranche.price_no:.4f} "
                             f"edge=${tranche.edge_usd:.4f}")
                    if self.gas_usd is not None:
                        try:
                            onchain = ctf.resolve_on_chain_timing("merge")
                        except ctf.OnChainRefused as exc:
                            log.warning(f"{exc}; parity legs stay as inventory")
                            continue
                        self.actions.submit("ctf_merge", strategy, {
                            "condition_id": market.condition_id,
                            "token_id": yes,
                            "pair_shares": tranche.shares,
                            "gas": tranche.gas,
                        }, rtt=onchain.flight_s)

    # -- reward ----------------------------------------------------------
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

    def _markets_with_books(self, books: dict[str, dict]) -> list[venue.Market]:
        """Markets that have at least one fresh book this tick.

        The universe walk discovers every active market, but a decision needs a
        price. Iterating the book set keeps the full universe eligible — any
        market the feed has priced in the last ``max_age`` seconds competes —
        without re-optimising hundreds of thousands of bookless markets every
        tick.
        """
        out: list[venue.Market] = []
        seen: set[str] = set()
        for token in books:
            market = self._token_index.get(token)
            if market is None or market.condition_id in seen:
                continue
            seen.add(market.condition_id)
            out.append(market)
        return out

    def _reward(self, books: dict[str, dict], prints: list[venue.Print]) -> None:
        now = time.time()
        dt = max(0.0, now - self._reward_accrual_ts)
        self._reward_accrual_ts = now
        if dt > 0.0:
            # Resting liquidity earns the pool's daily payout pro rata. The
            # payout is NOT read from the optimizer's plan: it is recomputed
            # every tick from what is actually resting and from the measured
            # books - our in-band score on the leg against the pair-wide
            # measured competing score of both outcome tokens. A quote whose
            # denominator cannot be measured earns nothing and is counted, so a
            # window can never book a share of a pool it never measured.
            accrued = 0.0
            implied = 0.0
            priced = refused = unqualified = 0
            pools = 0.0
            notional = 0.0
            competing: list[float] = []
            top: list[tuple[float, str, float, float]] = []
            # One quote per market: the pillar quotes the cheaper leg only, so
            # this set must have the same size as ``priced``. Logged so a market
            # paying its pool twice would be visible in a single line.
            priced_markets: set[str] = set()
            for quote in self.reward_quotes.values():
                if quote.cancel_at is not None or quote.shares <= 0.0:
                    continue
                market = self.by_condition.get(quote.condition_id)
                if market is None or not market.binary:
                    refused += 1
                    continue
                others = [t for t in market.token_ids if t != quote.token_id]
                if len(others) != 1:
                    refused += 1
                    continue
                share = reward.measured_accrual(
                    pool_usd_per_day=market.daily_reward,
                    price=quote.price,
                    shares=quote.shares,
                    min_size=quote.min_order,
                    max_spread_cents=market.reward_max_spread_cents,
                    leg_book=books.get(quote.token_id),
                    other_book=books.get(others[0]),
                )
                if share is None:
                    refused += 1
                    continue
                priced += 1
                priced_markets.add(quote.condition_id)
                pools += share.pool_usd_per_day
                notional += quote.shares * quote.price
                competing.append(share.competing_score)
                if not share.qualifies:
                    unqualified += 1
                    continue
                implied += share.usd_per_day
                accrued += share.usd_per_day * dt / 86_400.0
                top.append((share.usd_per_day, quote.condition_id,
                            share.competing_score, share.share))
            if accrued > 0.0:
                self.ledger.credit("RewardHarvester", amount=accrued,
                                   detail="reward_accrual")
            if now - self._reward_log_ts >= 60.0:
                self._reward_log_ts = now
                venue_pool = sum(m.daily_reward for m in self.markets)
                rewarded_count = sum(1 for m in self.markets if m.daily_reward > 0)
                depth = sorted(competing)
                median_d = depth[len(depth) // 2] if depth else 0.0
                # The claim must be auditable against the venue's own budget:
                # share of the measured program, share of the pools we cover,
                # and the largest single claim with the denominator behind it.
                biggest = sorted(top, reverse=True)[:3]
                top_txt = " ".join(
                    f"{cid[:8]}=${usd:,.2f}/day(D={d:,.1f} shares,share={share * 100.0:.1f}%)"
                    for usd, cid, d, share in biggest
                ) or "none"
                log.info(
                    f"reward accrual: {priced} quotes over {len(priced_markets)} "
                    f"markets priced, {unqualified} "
                    f"not qualifying, {refused} refused (unmeasured books or "
                    f"denominator) | implied ${implied:,.2f}/day on "
                    f"${notional:,.2f} resting "
                    f"({implied / notional * 100.0 if notional > 0 else 0.0:.2f}%/day) = "
                    f"{implied / venue_pool * 100.0 if venue_pool > 0 else 0.0:.3f}% of the "
                    f"${venue_pool:,.0f}/day venue pool measured over {rewarded_count} "
                    f"rewarded markets and "
                    f"{implied / pools * 100.0 if pools > 0 else 0.0:.1f}% of the "
                    f"${pools:,.0f}/day of pools it covers | measured pair-wide "
                    f"competing score: median {median_d:,.0f} | top claims: {top_txt}"
                )
        self._apply_resting_fills(self.reward_quotes, "RewardHarvester", books, prints, now)
        rewarded = [m for m in self.markets if m.daily_reward > 0]
        candidates = reward.reward_candidates(rewarded, books, prints)
        # The allocator answers "where would cash go on an empty book"; the
        # engine must also respect the notional already resting. Handing it the
        # bare balance each tick let resting reward quotes reach 1445.65 on a
        # 1000.51 cash balance (measured 10:33:48 this window), so a
        # simultaneous fill set would be refused by the ledger after the
        # decision to take it had already been made.
        shadow_state: dict = {}
        wanted = {
            q.token_id: q
            for q in reward.plan_quotes(
                candidates,
                max(0.0, self.ledger.cash("RewardHarvester")),
                {
                    token_id: (quote.price, quote.shares)
                    for token_id, quote in self.reward_quotes.items()
                    if quote.cancel_at is None and quote.shares > 0.0
                },
                state=shadow_state,
            )
        }
        # The optimum's KKT price of capital: the hurdle every other pillar
        # must clear before it spends the same dollar. None = not solved, which
        # the CTF gate treats as missing data and refuses on.
        self._shadow_yield = shadow_state.get("shadow_yield")
        for token_id, quote in wanted.items():
            current = self.reward_quotes.get(token_id)
            if current and abs(current.price - quote.price) < 1e-9:
                current.daily_payout = quote.daily_payout
                continue
            if current is not None and current.cancel_at is None:
                current.cancel_at = now
                self.actions.submit("reward_cancel", "RewardHarvester",
                                    {"token_id": token_id})
            landing = now + self._rtt()
            resting = Resting("RewardHarvester", quote.condition_id, token_id,
                              quote.price, quote.shares, quote.min_size, landing,
                              tick=self.by_condition[quote.condition_id].tick,
                              daily_payout=quote.daily_payout, seen_ts=landing,
                              initial_shares=quote.shares)
            self.actions.submit("reward_quote", "RewardHarvester",
                                {"token_id": token_id, "resting": resting})
        if not wanted and self._reserved(self.reward_quotes) > 0.0:
            # Cash is already committed to resting bids: hold them rather than
            # cancelling and re-quoting the same liquidity every tick.
            return
        for token_id, current in list(self.reward_quotes.items()):
            if token_id in wanted:
                continue
            if current.cancel_at is None:
                current.cancel_at = now
                self.actions.submit("reward_cancel", "RewardHarvester",
                                    {"token_id": token_id})
            if current.cancel_at is not None and now >= current.cancel_at + self._rtt():
                self.reward_quotes.pop(token_id, None)

    def _apply_resting_fills(
        self,
        resting: dict[str, Resting],
        strategy: str,
        books: dict[str, dict],
        prints: list[venue.Print],
        now: float,
    ) -> None:
        for token_id, quote in list(resting.items()):
            if now < quote.placed:
                continue
            if quote.max_age_s and now > quote.placed + quote.max_age_s:
                # The EV that justified this quote was measured over exactly
                # this window; past it the quote rests on an unmeasured
                # probability.
                resting.pop(token_id, None)
                continue
            if quote.cancel_at is not None and now >= quote.cancel_at + self._rtt():
                resting.pop(token_id, None)
                continue
            market_prints = [p for p in prints
                             if p.token_id == token_id and p.ts > quote.seen_ts]
            if market_prints:
                quote.seen_ts = max(p.ts for p in market_prints)
            book = books.get(token_id) or {}
            level_shares = shares_at(book, bid=True, price=quote.price)
            fill = reward.measured_fill_shares(quote.price, quote.shares, level_shares,
                                               market_prints, quote.tick)
            # Measured prints are 1-5 shares; discarding sub-lot flow guarantees
            # no fill against a 20-200 share minimum. Carry it instead.
            quote.accumulated += max(0.0, fill)
            fill = min(quote.accumulated, quote.shares)
            if fill < quote.min_order:
                continue
            market = self.by_condition.get(quote.condition_id)
            schedule = market.fee_schedule if market else None
            if not self.ledger.buy(
                strategy, condition_id=quote.condition_id, token_id=token_id,
                shares=fill, price=quote.price, tag="reward_fill",
            ):
                continue
            quote.shares -= fill
            quote.accumulated = max(0.0, quote.accumulated - fill)
            self._fills[strategy] += 1
            if schedule is not None:
                rebate = reward.maker_rebate(quote.price, fill, schedule)
                if rebate > 0.0:
                    self.ledger.credit(strategy, amount=rebate, detail="maker_rebate")
            log.info(f"reward fill {fill:.1f} @ {quote.price:.4f} on {token_id[:12]}")

    # -- settlement ------------------------------------------------------
    def _settlement(self, books: dict[str, dict]) -> None:
        """Fund verdict markets in descending edge per dollar, within cash."""
        now = time.time()
        if self.poller is not None:
            try:
                self.poller.poll(now)
            except Exception:  # noqa: BLE001 - the tape from disk still prices markets
                log.exception("UMA poll failed; using the durable verdict tape")
        self.settlement_model.refresh(now)
        if now - self._held_ts >= 60.0:
            self._held_ts = now
            self._reconcile_held()
        remaining = self.ledger.cash("SettlementHarvester")
        refused: dict[str, int] = {}
        refused_ask: list[float] = []
        # The walker only lists open, order-accepting markets, and UMA proposes
        # after trading stops - so a verdict market is usually not in it. The
        # verdict index is the radar: fetch exactly those condition ids.
        verdict_ids = self.settlement_model.tape.live_verdict_condition_ids(now)
        held_ids = [lot.condition_id for lot in self.ledger.positions("SettlementHarvester")]
        wanted = list(dict.fromkeys([cid for cid in verdict_ids + held_ids if cid]))
        missing = [cid for cid in wanted if cid not in self.by_condition]
        if missing and now - self._verdict_fetch_ts >= 60.0:
            self._verdict_fetch_ts = now
            for row in (self.gamma.markets(missing) or []):
                fetched = str(row.get("conditionId") or "").lower()
                if fetched:
                    self._verdict_rows[fetched] = row
                market = venue.parse_market(row)
                if market is not None:
                    self.by_condition[market.condition_id] = market
                    self._token_index.update({token: market for token in market.token_ids})
        candidates: list[tuple[float, venue.Market, float]] = []
        for cid in wanted:
            market = self.by_condition.get(cid)
            if market is None or not market.binary:
                reason = self._market_refusal(cid)
                refused[reason] = refused.get(reason, 0) + 1
                continue
            verdict = settlement.verdict_for(self.settlement_model, market)
            if verdict is None:
                refused[NO_VERDICT] = refused.get(NO_VERDICT, 0) + 1
                continue
            ask = best_ask(books.get(market.token_ids[verdict.winning_index]) or {})
            if ask is None or ask <= 0.0:
                # A market with no ask cannot be bought at any price: that is a
                # different fact from a market whose edge is negative, and the
                # difference is the whole question a zero-fill window asks.
                refused[NO_ASK] = refused.get(NO_ASK, 0) + 1
                continue
            edge = settlement.marginal_edge(market, verdict, ask)
            if edge is None:
                refused[NO_EDGE] = refused.get(NO_EDGE, 0) + 1
                continue
            if edge <= 0.0:
                refused[NEGATIVE_EDGE] = refused.get(NEGATIVE_EDGE, 0) + 1
                refused_ask.append(ask)
                continue
            edge_per_usd = edge / ask
            candidates.append((edge_per_usd, market, ask))
        for _edge, market, ask in sorted(candidates, key=lambda item: -item[0]):
            if self._pending_for(market.condition_id):
                continue
            # Reserve the minimum notional this order can take; the landing
            # re-prices against the fresh book, so the reservation is a floor,
            # not the size.
            reserve = ask * max(market.min_order, 1.0)
            if reserve > remaining:
                continue
            if self.actions.submit("settle_buy", "SettlementHarvester",
                                   {"condition_id": market.condition_id}) is not None:
                remaining -= reserve
        if now - self._settlement_log_ts >= 60.0:
            self._settlement_log_ts = now
            tally = " ".join(f"{reason}={count}" for reason, count in sorted(refused.items()))
            best = (f" best_ask={min(refused_ask):.4f} over {len(refused_ask)} refused"
                    if refused_ask else "")
            line = (
                f"settlement candidates: {len(wanted)} verdict cid(s), "
                f"{len(candidates)} +EV, cash ${remaining:.2f}"
                + (f" | refused: {tally}" if tally else "")
                + best
            )
            # A steady state is a *state*, not an event: reprinting an identical
            # tally every minute buries the change that matters. Measured window
            # 2026-11-10 12:53-13:23: 30 identical lines, not one of them a
            # finding. Log on change, and reprint once past the heartbeat so a
            # silent radar is still visible as alive.
            signature = (len(wanted), len(candidates), tuple(sorted(refused.items())))
            last = getattr(self, "_settlement_signature", None)
            quiet = now - getattr(self, "_settlement_log_beat", 0.0)
            if signature != last or quiet >= _SETTLEMENT_HEARTBEAT_SEC:
                self._settlement_signature = signature
                self._settlement_log_beat = now
                log.info(line)

    def _market_refusal(self, condition_id: str) -> str:
        """Why a verdict's market produced no candidate, from Gamma's own row.

        ``no_market_in_universe`` used to carry two facts that need opposite
        responses. A row that Gamma does not have is a coverage gap: the verdict
        exists and the radar cannot reach its market. A row that Gamma *does*
        have but that is ``closed`` and no longer accepting orders is protocol
        reality - the verdict market stopped trading before the oracle spoke,
        so there is no book to buy and no trade was missed. Measured 2026-11-10
        12:53-13:23: all three refusals were the second kind (Ferrari F1
        Constructors' Champion, and both WA-05 House seat legs), which is
        invisible when they are counted as "not in universe".
        """
        row = self._verdict_rows.get(condition_id)
        if row is None:
            return NO_MARKET
        if row.get("closed") is True or row.get("acceptingOrders") is False:
            return MARKET_CLOSED
        return NO_MARKET

    def _reconcile_held(self) -> None:
        """Mark held lots at the oracle's par and redeem the resolved ones.

        Two facts cannot both hold through ``venue.parse_market``: it returns
        ``None`` for any row with ``acceptingOrders == False`` (it is the
        *trading* gate), and redemption is only legal once the market is
        ``closed``. Reading the verdict off a parsed ``Market`` therefore made
        the redeem branch unreachable - a closed market never parsed, so a
        winning lot stayed marked at par and never became cash. The payout
        decision reads Gamma's own fields instead; only the *tradable* side of
        the pillar needs a ``Market``.

        Accounting: par is written only when the oracle has a verdict, and cash
        is credited only when the settlement is final. A pending redemption is
        inventory at par - never cash, never a drawdown.
        """
        held = {}
        for lot in self.ledger.positions("SettlementHarvester"):
            held.setdefault(lot.condition_id, []).append(lot)
        if not held:
            return
        rows = {
            str(row.get("conditionId") or "").lower(): row
            for row in (self.gamma.markets(held) or [])
        }
        for condition_id, lots in held.items():
            row = rows.get(condition_id)
            if row is None:
                continue
            token_ids = tuple(self.gamma.parse_ids(row.get("clobTokenIds")) or ())
            if len(token_ids) < 2:
                continue
            # The market's own Gamma question is the exact join key the tape
            # already uses; the walker's copy (if still open) is the same text.
            known = self.by_condition.get(condition_id)
            question = known.question if known is not None else str(row.get("question") or "")
            uma_status = str(row.get("umaResolutionStatus") or "").lower()
            closed = bool(row.get("closed"))
            verdict = settlement.verdict_for_fields(
                self.settlement_model,
                condition_id=condition_id,
                question=question,
                outcomes=self.gamma.parse_ids(row.get("outcomes")) or (),
                token_count=len(token_ids),
                prices=self.gamma.parse_ids(row.get("outcomePrices")) or (),
                closed=closed,
                uma_status=uma_status,
                category=known.category if known is not None else "settlement",
            )
            if verdict is None:
                continue
            winner = token_ids[verdict.winning_index]
            self.ledger.set_par(
                condition_id, winner, par=settlement.par_from_verdict(verdict)
            )
            if not closed:
                continue
            winners, losers = settlement.split_redemption(
                lots, condition_id=condition_id, token_ids=token_ids, verdict=verdict
            )
            for token_id, shares in winners:
                self.ledger.redeem("SettlementHarvester", condition_id=condition_id,
                                   token_id=token_id, shares=shares,
                                   gas=self.gas_usd or 0.0)
            for token_id, shares in losers:
                self.ledger.write_off("SettlementHarvester", condition_id=condition_id,
                                      token_id=token_id, shares=shares)

    # -- ctf -------------------------------------------------------------
    def _ctf(self, books: dict[str, dict], prints: list[venue.Print]) -> None:
        """Allocate the CTF budget by edge per dollar, takers first.

        Every binary market competes for the same cash: the pillar's optimizer
        returns per-market edges, and this loop funds them in descending edge
        per dollar until the budget is exhausted. Quoting every market with the
        whole bankroll would over-allocate the same dollars many times over.
        """
        if self.gas_usd is None:
            return
        now = time.time()
        self._apply_resting_fills(self.maker_quotes, "CtfMinter", books, prints, now)
        remaining = max(0.0, self.ledger.cash("CtfMinter") - self._reserved(self.maker_quotes))
        takers: list[tuple[float, float, venue.Market]] = []
        makers: list[tuple[float, venue.Market, Any, float]] = []
        priced_all = self._markets_with_books(books)
        # The EV gate costs ~2 ms per market. Evaluate a rotating, logged slice
        # so the stage stays inside the tick interval; every priced market is
        # still evaluated, just not all in the same tick.
        sweep_ticks = max(1, -(-len(priced_all) // self._ctf_budget))
        log.debug(
            f"ctf sweep: {len(priced_all)} priced markets every {sweep_ticks} ticks, "
            f"{sweep_ticks * max(self._last_tick_dt, 1.0):.0f}s requote horizon"
        )
        if len(priced_all) > self._ctf_budget:
            start = self._ctf_cursor % len(priced_all)
            priced = priced_all[start:start + self._ctf_budget]
            if len(priced) < self._ctf_budget:
                priced += priced_all[: self._ctf_budget - len(priced)]
            self._ctf_cursor = (start + self._ctf_budget) % len(priced_all)
            if now - self._ctf_coverage_ts >= 60.0:
                self._ctf_coverage_ts = now
                log.info(
                    f"ctf scan budget: {self._ctf_budget} of {len(priced_all)} priced "
                    f"markets per tick (full sweep every "
                    f"{-(-len(priced_all) // self._ctf_budget)} ticks)"
                )
        else:
            priced = priced_all
        sweep_ticks = max(1, -(-len(priced_all) // self._ctf_budget))
        horizon_s = sweep_ticks * max(self._last_tick_dt, 1.0)
        refused: set[str] = set()
        for market in priced:
            if not market.binary or market.closed:
                continue
            yes, no = market.token_ids
            if yes not in books or no not in books:
                continue
            book_yes, book_no = books.get(yes) or {}, books.get(no) or {}
            tranches = ctf.optimize_taker_parity(market, book_yes, book_no, remaining,
                                                 self.gas_usd)
            if tranches:
                cost = sum(t.shares * (t.price_yes + t.price_no) + t.fee_yes + t.fee_no
                           + t.gas for t in tranches)
                edge = sum(t.edge_usd for t in tranches)
                if cost > 0.0:
                    takers.append((edge / cost, cost, market))
                continue
            bid_yes = best_bid(book_yes)
            bid_no = best_bid(book_no)
            if bid_yes is None or bid_no is None:
                continue
            # Size to what the displayed book can actually fill, not to the
            # whole bankroll: the pillar's fill probability is only measured
            # inside that support.
            depth_yes = shares_at(book_yes, bid=True, price=bid_yes)
            depth_no = shares_at(book_no, bid=True, price=bid_no)
            size_budget = min(depth_yes, depth_no, remaining / max(market.tick, bid_yes + bid_no))
            quote = ctf.optimize_maker_parity(
                market, book_yes, book_no, size_budget, requote_horizon_s=horizon_s,
                min_daily_yield=self._shadow_yield,
            )
            if quote is None:
                refused.add(market.condition_id)
                continue
            pair_cost = quote.bid_yes + quote.bid_no
            if pair_cost <= 0.0:
                continue
            makers.append((quote.edge_usd / pair_cost, market, quote, pair_cost))
        for _ratio, cost, market in sorted(takers, key=lambda item: -item[0]):
            if cost > remaining or self._pending_for(market.condition_id):
                continue
            if self.actions.submit("ctf_taker", "CtfMinter",
                                   {"condition_id": market.condition_id}) is not None:
                remaining -= cost
        for _ratio, market, quote, pair_cost in sorted(makers, key=lambda item: -item[0]):
            if self._pending_for(market.condition_id):
                continue
            yes, no = market.token_ids
            size = min(quote.shares, remaining / pair_cost)
            if size < market.min_order:
                continue
            remaining -= size * pair_cost
            for token_id, price in ((yes, quote.bid_yes), (no, quote.bid_no)):
                current = self.maker_quotes.get(token_id)
                if current and abs(current.price - price) < 1e-9:
                    continue
                if current is not None:
                    current.cancel_at = now
                    self.actions.submit("ctf_cancel", "CtfMinter", {"token_id": token_id})
                resting = Resting("CtfMinter", market.condition_id, token_id, price,
                                  size, market.min_order, now + self._rtt(),
                                  tick=market.tick,
                                  max_age_s=quote.priced_horizon_s)
                self.actions.submit("ctf_quote", "CtfMinter",
                                    {"token_id": token_id, "resting": resting})
        # Cancel only orders the optimizer no longer wants. Cancelling merely
        # because this tick's cash was already reserved makes the book oscillate
        # between "quote everything" and "cancel everything".
        wanted_tokens: set[str] = set()
        for _ratio, market, _quote, _cost in makers:
            wanted_tokens.update(market.token_ids)
        for token_id, current in list(self.maker_quotes.items()):
            # A quote outside this tick's rotating slice is held, not cancelled:
            # its requote lifetime is the measured full-sweep horizon. Only a
            # market that was priced and refused this tick loses its order.
            if current.condition_id not in refused or current.cancel_at is not None:
                continue
            if token_id in wanted_tokens:
                continue
            current.cancel_at = now
            self.actions.submit("ctf_cancel", "CtfMinter", {"token_id": token_id})
        held: dict[str, list[Any]] = {}
        for lot in self.ledger.positions("CtfMinter"):
            held.setdefault(lot.condition_id, []).append(lot)
        merge_timing: Any = None
        if held:
            # One live head read per tick serves every merge candidate.
            try:
                merge_timing = ctf.resolve_on_chain_timing("merge")
            except ctf.OnChainRefused as exc:
                log.warning(f"{exc}; {len(held)} merge candidate(s) held")
        for condition_id, lots in held.items():
            market = self.by_condition.get(condition_id)
            if market is None or not market.binary:
                continue
            if merge_timing is None:
                continue
            pairs = ctf.merge_pairs(lots, condition_id)
            # Each merged pair redeems at par ($1). The break-even is the
            # measured gas of one action, not a guessed share count.
            if pairs * 1.0 > float(self.gas_usd or 0.0):
                self.actions.submit("ctf_merge", "CtfMinter", {
                    "condition_id": condition_id, "token_id": market.token_ids[0],
                    "pair_shares": pairs, "gas": self.gas_usd,
                }, rtt=merge_timing.flight_s)

    def _pending_for(self, condition_id: str) -> bool:
        return any(action.payload.get("condition_id") == condition_id
                   for action in self.actions.pending())

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
                 f"resting={len(self.reward_quotes) + len(self.maker_quotes)}"
                 f" (reward={len(self.reward_quotes)} ctf={len(self.maker_quotes)}"
                 # Each pillar owns its own cash, so a summed reserve reads as a
                 # breach when both are individually inside their balance. Show
                 # the pair against the balance each is actually drawn on.
                 f" reserved=reward {self._reserved(self.reward_quotes):.2f}"
                 f"/{self.ledger.cash('RewardHarvester'):.2f}"
                 f" ctf {self._reserved(self.maker_quotes):.2f}"
                 f"/{self.ledger.cash('CtfMinter'):.2f})")
