"""Opportunity scanning: score measured candidates and rank them by net yield."""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

import requests

from polymarketbot.logs import get_logger
from polymarketbot.market import TokenConfig
from polymarketbot.net import install_doh_fallback
from polymarketbot.strategy.reward.regime import MarketRegime, measure_regimes_cached

from . import state
from .candidates import MarketOpportunity, infer_market_category
from .payload import _coerce_float, _extract_daily_pool, _extract_market_metadata
from .pricing import calculate_expected_yield
from .venue import fetch_book_spread_cents, fetch_tick_size, get_real_l2_score

logger = get_logger("rewards_scanner")


class OpportunityScanner:
    """Scans Polymarket CLOB head reward pools dynamically and scores net yield."""

    def __init__(
        self,
        rest_url: str = state.CLOB_REST_URL,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.rest_url = rest_url
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "polymarketbot/rewards-scanner"})
        install_doh_fallback()

    def fetch_raw_reward_pools(self, limit: int = 50) -> list[dict[str, Any]]:
        """Query all available head reward pools dynamically without hardcoded filters."""
        url = f"{self.rest_url}/rewards/markets/multi"
        params = {
            "order_by": "rate_per_day",
            "position": "DESC",
            "page_size": max(limit, 20),
        }
        logger.info(f"Scanning CLOB REST for reward pools: {url} (limit={limit})")
        resp = self.session.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        logger.info(f"Retrieved {len(data)} raw reward market candidates.")
        return data

    def score_market_candidate(
        self,
        raw_mkt: dict[str, Any],
        *,
        allocated_capital_usd: float,
        quote_horizon_s: float,
        spread_cents: Optional[float] = None,
        competing_score: Optional[float] = None,
        regime: Optional[MarketRegime] = None,
    ) -> Optional[MarketOpportunity]:
        """Parse a raw payload and score net yield from measured inputs.

        Every number this method prices comes from the payload, the venue's L2
        book, or a measured tape artifact: the daily pool (summed over the
        market's campaigns), the published maximum spread, minimum size and
        tick, the **measured** competing score of both outcome-token books
        inside the band, the measured horizon fill probability for
        ``(tick, min size, quote_horizon_s)``, the measured markout for that
        cell, and the market's own prints over the trailing tape window.

        ``allocated_capital_usd`` is the capital the **caller** allocates to
        this market and is required: the old ``bankroll_usd=300 *
        concentration_pct=0.75`` pair had the scanner deploy three quarters of a
        hard-coded bankroll into every market it looked at. Sizing belongs to
        ``allocator.py``; a missing allocation refuses the market.

        ``quote_horizon_s`` is the lifetime a quote rests before it is replaced
        - the horizon the measured fill table is keyed on. It has no default: a
        fill probability without the lifetime it was measured over is not a
        probability.

        ``regime`` is the market's tape regime
        (``polymarketbot.strategy.reward.regime``); ``scan_and_rank_markets``
        measures it once per scan and passes it in. When it is ``None`` the
        market has no measured tape statistics and is refused with
        ``unmeasured: <reason>``.

        The old ``max(1000.0, rate * 40.0)`` competing-depth estimate made the
        denominator a function of the market's own reward rate, so every pool
        looked equally contested no matter what depth actually rested in it.
        """
        cid = str(raw_mkt.get("condition_id") or "")
        if not cid:
            logger.warning("reward market without condition_id; not scoreable")
            return None
        if allocated_capital_usd is None or float(allocated_capital_usd) <= 0.0:
            logger.warning(
                f"no allocated capital for {cid!r} "
                f"(allocated_capital_usd={allocated_capital_usd!r}); "
                "unmeasured: caller allocated no capital. Sizing belongs to allocator.py"
            )
            return None
        if quote_horizon_s is None or float(quote_horizon_s) <= 0.0:
            logger.warning(
                f"no quote horizon for {cid!r} (quote_horizon_s={quote_horizon_s!r}); "
                "unmeasured: fill probability needs the quote lifetime"
            )
            return None
        q = raw_mkt.get("question") or "Unknown"
        daily_pool = _extract_daily_pool(raw_mkt)
        if daily_pool is None:
            logger.warning(f"no published daily pool for {cid!r}; not scoreable")
            return None
        min_size = _coerce_float(raw_mkt.get("rewards_min_size"))
        max_spread = _coerce_float(raw_mkt.get("rewards_max_spread"))
        if min_size is None or min_size <= 0.0 or max_spread is None or max_spread <= 0.0:
            logger.warning(
                f"reward campaign parameters unmeasured for {cid!r} "
                f"(min_size={raw_mkt.get('rewards_min_size')!r}, "
                f"max_spread={raw_mkt.get('rewards_max_spread')!r}); not scoreable"
            )
            return None
        tick_size = _extract_market_metadata(raw_mkt)["tick_size"]
        if tick_size is None:
            # The campaign payload publishes no tick; read the venue's own.
            token_for_tick = next(
                (
                    str(t.get("token_id"))
                    for t in (raw_mkt.get("tokens") or [])
                    if str(t.get("outcome", "")).upper() == "YES" and t.get("token_id")
                ),
                None,
            )
            if token_for_tick is None:
                logger.warning(f"no YES token for {cid!r}; tick unmeasurable")
                return None
            tick_size = fetch_tick_size(self.session, token_for_tick)
        if tick_size is None:
            logger.warning(f"no published tick for {cid!r}; not scoreable")
            return None
        tokens: list[TokenConfig] = []
        mid: Optional[float] = None
        yes_token: Optional[str] = None
        no_token: Optional[str] = None
        for tok in raw_mkt.get("tokens", []):
            t_id = tok.get("token_id")
            outcome = tok.get("outcome", "unknown")
            price = _coerce_float(tok.get("price"))
            if not t_id or price is None or not 0.0 < price < 1.0:
                logger.warning(
                    f"token {outcome!r} of {cid!r} publishes no price; not scoreable"
                )
                return None
            tokens.append(TokenConfig(token_id=t_id, outcome=outcome, price=price))
            if str(outcome).upper() == "YES":
                yes_token = str(t_id)
                mid = price
            elif str(outcome).upper() == "NO":
                no_token = str(t_id)
        if mid is None or not 0.0 < mid < 1.0:
            logger.warning(f"no measured YES mid for {cid!r}; not scoreable")
            return None

        if spread_cents is None:
            # A maker captures the half-spread it actually quotes into, so the
            # spread is read from the live book. This used to fall back to
            # ``tick_size * 100 / 2`` - an invented half-tick that priced every
            # candidate in the universe with a quote distance nobody measured.
            spread_cents = (
                fetch_book_spread_cents(self.session, yes_token)
                if yes_token is not None
                else None
            )
        if spread_cents is None:
            logger.warning(f"live book spread unmeasured for {cid!r}; not scoreable")
            return None

        measured_competing = competing_score
        if measured_competing is None:
            score_total = 0.0
            for token_id in (yes_token, no_token):
                if token_id is None:
                    logger.warning(
                        f"reward market {cid!r} does not publish both tokens; "
                        "competing depth unmeasurable"
                    )
                    return None
                score, _shares, _mid = get_real_l2_score(
                    session=self.session,
                    token_id=token_id,
                    max_spread_cents=max_spread,
                )
                if score is None:
                    logger.warning(
                        f"competing depth unmeasurable for {cid!r} "
                        f"(token {token_id[:14]}); not scoreable"
                    )
                    return None
                score_total += score
            measured_competing = score_total
        if measured_competing <= 0.0:
            logger.debug(
                f"measured competing score for {cid!r} is {measured_competing}; "
                "a zero denominator prices the whole pool"
            )
            return None

        yield_metrics = calculate_expected_yield(
            daily_rate=daily_pool,
            max_spread_cents=max_spread,
            min_size=min_size,
            allocated_capital_usd=float(allocated_capital_usd),
            tick_size=float(tick_size),
            quote_horizon_s=float(quote_horizon_s),
            spread_cents=spread_cents,
            mid_price=mid,
            competing_score=measured_competing,
            regime=regime,
        )
        if yield_metrics["daily_reward_payout"] is None:
            logger.warning(
                f"market {cid!r} refused - "
                f"{yield_metrics.get('unmeasured_reason') or 'no measurable payout'}"
            )
            return None

        return MarketOpportunity(
            condition_id=cid,
            question=q,
            rate_per_day=daily_pool,
            rewards_min_size=min_size,
            rewards_max_spread=max_spread,
            mid_price=mid,
            category=infer_market_category(q),
            competing_depth=measured_competing,
            competing_score=measured_competing,
            tokens=tokens,
            event_id=str(raw_mkt.get("event_id")) if raw_mkt.get("event_id") is not None else None,
            maker_score=yield_metrics["maker_score"],
            gross_reward_payout=yield_metrics["gross_reward_payout"],
            daily_reward_payout=yield_metrics["daily_reward_payout"],
            expected_spread_capture=yield_metrics["expected_spread_capture"],
            expected_adverse_selection=yield_metrics["expected_adverse_selection"],
            expected_gas_fees=yield_metrics["expected_gas_fees"],
            net_expected_yield=yield_metrics["net_expected_yield"],
            net_yield_per_dollar=yield_metrics["net_yield_per_dollar"],
            passed_payout_floor=yield_metrics["passed_payout_floor"],
            gas_measured=yield_metrics["gas_measured"],
            allocated_capital_usd=float(allocated_capital_usd),
            quote_horizon_s=float(quote_horizon_s),
            tick_size=float(tick_size),
            measured_inputs={
                "p_fill": yield_metrics["p_fill"],
                "priced_horizon_s": yield_metrics["priced_horizon_s"],
                "required_flow_shares": float(min_size),
                "markout_usd_per_share": yield_metrics["markout_usd_per_share"],
                "quote_size_shares": yield_metrics["quote_size_shares"],
                "expected_fills_per_day": yield_metrics["expected_fills_per_day"],
                "regime": regime.to_dict() if regime is not None else None,
            },
        )

    def scan_and_rank_markets(
        self,
        *,
        allocated_capital_usd: float,
        quote_horizon_s: float,
        limit: int = 50,
        raw_markets: Optional[list[dict[str, Any]]] = None,
    ) -> list[MarketOpportunity]:
        """Rank the rate universe by net yield per dollar on allocated capital.

        ``allocated_capital_usd`` is the capital the caller is prepared to put
        into **one** of these markets, and it is required: every candidate is
        priced at that deployment, which makes the ranking a per-dollar marginal
        comparison (the same capital in each market, so the best per-dollar
        yield ranks first). Sizing the winners - the actual split of a bankroll
        across markets - is ``allocator.py``'s job, not the scanner's.
        ``quote_horizon_s`` is the quote lifetime the fill table is read at.

        Markets are measured, not assumed: one tape pass over the trailing
        ``tape_regime.TAPE_WINDOW_HOURS`` supplies every candidate's regime, and
        each candidate that cannot be priced is refused with
        ``unmeasured: <reason>``. The refusal reasons are counted and logged, so
        a thin scan is distinguishable from a bad one.
        """
        if raw_markets is None:
            raw_markets = self.fetch_raw_reward_pools(limit=limit)

        # One tape pass for the whole scan: bands are the market's own published
        # reward spreads, the jump threshold the regime measures against.
        bands: dict[str, float] = {}
        for m in raw_markets:
            cid = str(m.get("condition_id") or "")
            band = _coerce_float(m.get("rewards_max_spread"))
            if cid and band is not None and band > 0.0:
                bands[cid] = band
        measured = measure_regimes_cached(bands) if bands else None
        regimes = measured.regimes if measured is not None else {}

        scored: list[MarketOpportunity] = []
        refusals: Counter[str] = Counter()
        for m in raw_markets:
            cid = str(m.get("condition_id") or "")
            regime = regimes.get(cid)
            if regime is None:
                reason = (
                    measured.reason_for(cid)
                    if measured is not None
                    else "unmeasured: no tape measurement"
                )
                refusals[reason] += 1
                logger.info(f"market {cid!r} refused - {reason}")
                continue
            opp = self.score_market_candidate(
                m,
                allocated_capital_usd=allocated_capital_usd,
                quote_horizon_s=quote_horizon_s,
                regime=regime,
            )
            # Only consider active pools with a positive daily rate and a
            # measurable net yield: a market whose gas could not be priced has
            # no rank, and ranking it last would still let a switch adopt it.
            if opp is not None and opp.rate_per_day > 0.0 and opp.gas_measured:
                scored.append(opp)
            else:
                refusals["unmeasured: pool rate or gas unmeasured"] += 1

        refusal_summary = (
            "; ".join(f"{count}x {reason}" for reason, count in refusals.most_common())
            or "none"
        )
        logger.info(
            f"scan refusals: {sum(refusals.values())}/{len(raw_markets)} markets "
            f"not priced ({refusal_summary})"
        )

        # Rank descending by net yield per dollar (and net yield)
        scored.sort(key=lambda o: (o.passed_payout_floor, o.net_yield_per_dollar, o.net_expected_yield), reverse=True)
        logger.info(
            f"Ranked {len(scored)} active reward markets. Top market: "
            f"'{scored[0].question[:40]}' with net yield ${scored[0].net_expected_yield:.2f}/day "
            f"({scored[0].net_yield_per_dollar * 100:.2f}%/day per dollar)"
            if scored else "No active markets found."
        )
        return scored
