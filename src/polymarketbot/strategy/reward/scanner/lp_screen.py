"""The bankroll-sized LP universe screen: ``find_optimal_lp_markets``."""

from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import requests

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.allocator import PROTOCOL_MIN_DAILY_PAYOUT_USD
from polymarketbot.strategy.reward.fill_measurement import expected_fill_yield
from polymarketbot.strategy.reward.scoring import (
    quadratic_order_score,
    single_sided_score_fraction,
)
from polymarketbot.strategy.sizing import load_fill_policy_table

from . import state
from .candidates import _screen_lp_candidate
from .venue import fetch_tick_size, get_real_l2_score

logger = get_logger("rewards_scanner")


@lru_cache(maxsize=32)
def _fill_policy(tick_size_cents: float, max_spread_cents: float):
    """The measured fill/markout table for one (tick, band), cached per scan."""
    return load_fill_policy_table(
        tick_size_cents=tick_size_cents, max_spread_cents=max_spread_cents
    )


def find_optimal_lp_markets(
    bankroll_usd: float,
    min_days_to_expiry: float = 7.0,
    min_mid_price: float = 0.12,
    max_mid_price: float = 0.88,
    max_candidates: int = 250,
    limit_output: int = 15,
) -> list[dict[str, Any]]:
    """Scan all reward pools and rank by Net Daily ROI (%) on deployed capital.

    ``bankroll_usd`` is required: the scan sizes each candidate's quote from the
    capital the caller actually has (``bankroll / pair_cost`` shares per leg),
    so a default would silently quote a bankroll nobody chose. A non-positive
    bankroll refuses the scan instead of returning a ranked universe priced on
    nothing.
    """
    if bankroll_usd is None or float(bankroll_usd) <= 0.0:
        logger.warning(
            f"no bankroll supplied to the LP scan (bankroll_usd={bankroll_usd!r}); "
            "unmeasured: caller supplied no capital"
        )
        return []
    with state._TELEMETRY_LOCK:
        state.GLOBAL_SCANNER_TELEMETRY.total_scans += 1
    now = time.time()
    cache_key = f"{round(bankroll_usd, 2)}_{min_days_to_expiry}_{min_mid_price}_{max_mid_price}_{max_candidates}_{limit_output}"

    # 1. 5-Second TTL In-Memory Opportunity Cache Check
    cached_markets = state._cache_get_fresh(state._SCANNER_CACHE, cache_key, state.SCANNER_CACHE_TTL_SECONDS)
    if cached_markets is not None:
        with state._TELEMETRY_LOCK:
            state.GLOBAL_SCANNER_TELEMETRY.cache_hits += 1
            hits = state.GLOBAL_SCANNER_TELEMETRY.cache_hits
            scans = state.GLOBAL_SCANNER_TELEMETRY.total_scans
        logger.info(
            f"[SCANNER CACHE HIT] Served {len(cached_markets)} candidate markets from "
            f"{state.SCANNER_CACHE_TTL_SECONDS:.1f}s TTL cache "
            f"(Hits: {hits}/{scans})"
        )
        return [dict(m) for m in cached_markets]

    with state._TELEMETRY_LOCK:
        state.GLOBAL_SCANNER_TELEMETRY.cache_misses += 1
    session = state._get_scanner_session()

    url = f"{state.CLOB_REST_URL}/rewards/markets/multi"
    params = {"order_by": "rate_per_day", "position": "DESC", "page_size": min(500, max_candidates)}

    logger.info(f"Querying top {max_candidates} reward pools from Polymarket CLOB...")
    try:
        resp = session.get(url, params=params, timeout=15.0)
        if resp.status_code == 429:
            with state._TELEMETRY_LOCK:
                state.GLOBAL_SCANNER_TELEMETRY.http_429_errors += 1
                state.GLOBAL_SCANNER_TELEMETRY.last_error_time = now
                state.GLOBAL_SCANNER_TELEMETRY.last_error_message = (
                    "HTTP 429 Too Many Requests on /rewards/markets/multi"
                )
            logger.error("[SCANNER HTTP 429] Rate limit reached on CLOB multi-rewards endpoint.")
            stale = state._cache_get_stale(state._SCANNER_CACHE, cache_key)
            if stale is not None:
                logger.warning("[SCANNER STALE CACHE FALLBACK] Returning stale cached candidates due to HTTP 429.")
                return [dict(m) for m in stale]
            return []
        resp.raise_for_status()
        raw_markets = resp.json().get("data", [])
    except requests.exceptions.RequestException as e:
        with state._TELEMETRY_LOCK:
            state.GLOBAL_SCANNER_TELEMETRY.network_errors += 1
            state.GLOBAL_SCANNER_TELEMETRY.last_error_time = now
            state.GLOBAL_SCANNER_TELEMETRY.last_error_message = (
                f"Network error on /rewards/markets/multi: {e}"
            )
        logger.error(f"[SCANNER NETWORK ERROR] Failed to fetch reward pools from CLOB: {e}")
        stale = state._cache_get_stale(state._SCANNER_CACHE, cache_key)
        if stale is not None:
            logger.warning("[SCANNER STALE CACHE FALLBACK] Returning stale cached candidates due to network error.")
            return [dict(m) for m in stale]
        return []
    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"Failed to fetch reward pools from CLOB: {e}")
        stale = state._cache_get_stale(state._SCANNER_CACHE, cache_key)
        if stale is not None:
            return [dict(m) for m in stale]
        return []

    logger.info(f"Received {len(raw_markets)} markets. Applying risk & viability filters...")

    candidates: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    now_utc = datetime.now(UTC)

    for raw_market in raw_markets:
        if not isinstance(raw_market, dict):
            rejection_counts["payload"] += 1
            logger.debug(f"[SCANNER REJECT] non-mapping market payload: {raw_market!r}")
            continue
        candidate, reason = _screen_lp_candidate(
            raw_market,
            now_utc=now_utc,
            min_days_to_expiry=min_days_to_expiry,
            min_mid_price=min_mid_price,
            max_mid_price=max_mid_price,
            bankroll_usd=bankroll_usd,
        )
        if candidate is None:
            rejection_counts[reason.split(":", 1)[0]] += 1
            logger.debug(
                f"[SCANNER REJECT] {raw_market.get('condition_id')!r} "
                f"({str(raw_market.get('question') or 'Unknown')[:60]!r}): {reason}"
            )
            continue
        candidates.append(candidate)

    rejected_total = sum(rejection_counts.values())
    rejection_summary = ", ".join(
        f"{code}={count}" for code, count in rejection_counts.most_common()
    ) or "none"
    logger.info(
        f"Screened {len(candidates)}/{len(raw_markets)} reward markets "
        f"(rejected {rejected_total}: {rejection_summary}). Auditing full L2 depth in parallel..."
    )

    # The reward-campaign endpoint publishes no price tick, and the tick is the
    # grid every quote distance (and so every alpha) is measured on. Read the
    # venue's published value per surviving market instead of assuming one; a
    # market whose tick cannot be read has no measurable quote distance and is
    # dropped, counted in the log.
    def hydrate_tick(cand: dict[str, Any]) -> dict[str, Any] | None:
        if cand.get("tick_size") is not None and cand.get("our_score") is not None:
            return cand
        tick = fetch_tick_size(state._get_scanner_session(), str(cand["yes_token"]))
        if tick is None:
            return None
        cand["tick_size"] = tick
        # The allocator needs the distance the score was computed at; publish
        # it alongside the score once the venue tick is known.
        cand["spread_offset_cents"] = tick * 100.0 / 2.0
        cand["our_score"] = quadratic_order_score(
            float(cand["max_spread"]), tick * 100.0 / 2.0, float(cand["quote_size"])
        )
        # The fill side, from the same measured table and horizon the execution
        # policy prices spreads with. A bid that fills stops accruing; pricing
        # the pool share without that cost is the accrual-only overstatement
        # the allocator refuses to mix silently. A market whose (tick, band)
        # has no measured rows stays unpriced and the allocator says so.
        mid = float(cand.get("mid_yes") or 0.0)
        policy = _fill_policy(tick * 100.0, float(cand["max_spread"]))
        if policy is not None and 0.0 < mid < 1.0:
            fy = expected_fill_yield(
                tick=tick,
                min_flow_shares=float(cand["min_size"]),
                horizon_s=float(policy.horizon_s),
                quote_price=mid - tick / 2.0,
                leg_mid=mid,
            )
            if fy is not None:
                cand["p_fill"] = fy.p_fill
                cand["stay_fraction"] = fy.stay_fraction
                cand["fill_yield_usd_per_usd_per_day"] = fy.fill_yield_usd_per_usd_per_day
                cand["fill_horizon_s"] = fy.horizon_s
        return cand

    if candidates:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="tick-hydrate") as pool:
            hydrated = list(pool.map(hydrate_tick, candidates))
        missing_tick = sum(1 for item in hydrated if item is None)
        candidates = [item for item in hydrated if item is not None]
        if missing_tick:
            logger.info(
                f"tick hydrate: dropped {missing_tick}/{len(hydrated)} candidates whose "
                "venue tick could not be read (quote distance unmeasurable)"
            )

    def audit_market(cand: dict[str, Any]) -> dict[str, Any]:
        s = state._get_scanner_session()
        score_yes, qual_yes, real_mid_yes = get_real_l2_score(
            session=s,
            token_id=cand["yes_token"],
            max_spread_cents=cand["max_spread"],
        )
        score_no: float | None = None
        qual_no: float | None = None
        if cand.get("no_token"):
            score_no, qual_no, _ = get_real_l2_score(
                session=s,
                token_id=cand["no_token"],
                max_spread_cents=cand["max_spread"],
            )

        # The market's competing score is the qual in-band depth of BOTH token
        # books - the pool is divided against the pair, not against one leg.
        # Both readings are measurements: when either book could not be read,
        # or the pair scores nothing at all, the denominator is unmeasured and
        # the market is excluded instead of being priced off a floor.
        cand["competing_score_measured"] = (
            score_yes is not None
            and score_no is not None
            and real_mid_yes is not None
            and (score_yes + score_no) > 0.0
        )
        if not cand["competing_score_measured"]:
            cand["competing_score"] = None
            cand["real_mid"] = real_mid_yes
            cand["qualifying_shares"] = None
            cand["raw_daily_payout"] = None
            cand["real_daily_payout"] = None
            cand["daily_roi_pct"] = 0.0
            cand["monthly_roi_pct"] = 0.0
            cand["clears_floor"] = False
            return cand
        audited_comp_score = score_yes + score_no
        qual_shares = (qual_yes or 0.0) + (qual_no or 0.0)
        comp_score = audited_comp_score

        cand["competing_score"] = comp_score
        cand["qualifying_shares"] = qual_shares
        cand["real_mid"] = real_mid_yes

        # The venue pays Q_min = min(Q_one, Q_two) to a two-sided maker, so one
        # side's raw score is the right payout numerator only when both legs
        # rest. A one-sided plan is paid 1/c of its raw score inside the
        # [0.10, 0.90] midpoint band and nothing outside it: scoring it with
        # the two-sided rule claimed 3x. The audited YES mid decides the band.
        our_score = float(cand["our_score"])
        if int(cand.get("quote_sides", 2)) < 2:
            our_score *= single_sided_score_fraction(real_mid_yes)
        cand["our_score"] = our_score

        total_score = comp_score + our_score
        if total_score <= 0 or cand["daily_pool"] <= 0:
            cand["competing_score"] = None
            cand["raw_daily_payout"] = None
            cand["real_daily_payout"] = None
            cand["daily_roi_pct"] = 0.0
            cand["monthly_roi_pct"] = 0.0
            cand["clears_floor"] = False
            return cand
        raw_payout = cand["daily_pool"] * (our_score / total_score)

        cand["raw_daily_payout"] = raw_payout
        # The only payout hurdle is the documented venue floor: a market that
        # would pay less than $1.00 in a day pays nothing at all. The old $1.20
        # was a hand-set 20 % buffer on top of it.
        cand["clears_floor"] = raw_payout >= PROTOCOL_MIN_DAILY_PAYOUT_USD
        cand["real_daily_payout"] = raw_payout if cand["clears_floor"] else 0.0
        cand["daily_roi_pct"] = (cand["real_daily_payout"] / cand["capital_required"]) * 100.0
        cand["monthly_roi_pct"] = cand["daily_roi_pct"] * 30.0
        return cand

    scored: list[dict[str, Any]] = []
    futures = [state._SCAN_EXECUTOR.submit(audit_market, c) for c in candidates]
    for f in as_completed(futures):
        res = f.result()
        # Only markets whose denominator and mid were actually measured can be
        # ranked: an unmeasured one has no payout to rank and no price to quote.
        if (
            res.get("competing_score") is not None
            and res.get("real_mid") is not None
            and min_mid_price <= res["real_mid"] <= max_mid_price
        ):
            scored.append(res)

    # Sort primarily by floor clearance, then Daily ROI descending
    scored.sort(
        key=lambda x: (x["clears_floor"], x["daily_roi_pct"], x["raw_daily_payout"]),
        reverse=True,
    )

    final_candidates = scored[:limit_output]
    # Store in 5-second TTL in-memory cache
    state._cache_put(state._SCANNER_CACHE, cache_key, final_candidates, state._SCANNER_CACHE_MAX_ENTRIES)
    with state._TELEMETRY_LOCK:
        state.GLOBAL_SCANNER_TELEMETRY.cached_candidates_count = len(final_candidates)
        state.GLOBAL_SCANNER_TELEMETRY.last_scan_timestamp = now

    return final_candidates
