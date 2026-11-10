"""Venue-measured inputs: the published price tick and the live L2 book score."""

from __future__ import annotations

import time
from typing import Optional

import requests

from polymarketbot.logs import get_logger
from polymarketbot.rewards.scoring import quadratic_order_score

from . import state
from .payload import _coerce_float

logger = get_logger("rewards_scanner")


def fetch_tick_size(
    session: requests.Session, token_id: str, timeout: float = 6.0
) -> Optional[float]:
    """The venue's published price tick for one token, or ``None``.

    Source: ``GET /tick-size?token_id=...`` -> ``minimum_tick_size``. The CLOB
    reward-campaign rows carry no tick, and the tick is the grid the quote
    distance (and therefore ``alpha``) is measured on, so it must be read from
    the venue. ``None`` means unmeasured; callers refuse the market instead of
    assuming a tick.
    """
    cached = state._cache_get_fresh(state._TICK_CACHE, token_id, 300.0)
    if cached is not None:
        return cached
    try:
        response = session.get(
            f"{state.CLOB_REST_URL}/tick-size",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.debug(f"tick-size HTTP {response.status_code} for {str(token_id)[:14]}")
            return None
        tick = _coerce_float(response.json().get("minimum_tick_size"))
    except Exception as exc:  # noqa: BLE001 - an unread tick is missing data
        logger.debug(f"tick-size fetch failed for {str(token_id)[:14]}: {exc}")
        return None
    if tick is None or tick <= 0.0:
        return None
    state._cache_put(state._TICK_CACHE, token_id, tick, state._BOOK_CACHE_MAX_ENTRIES)
    return tick


def fetch_book_spread_cents(
    session: requests.Session, token_id: str, timeout: float = 4.0
) -> Optional[float]:
    """The venue's live bid-ask spread for one token, in cents, or ``None``.

    A maker captures the half-spread it actually quotes into, so the spread has
    to be read from the live book rather than assumed from the tick grid. A
    one-sided, crossed or unreadable book returns ``None`` (unmeasured); callers
    refuse the market instead of substituting a half-tick.
    """
    cached = state._cache_get_fresh(
        state._SPREAD_CACHE, token_id, state.SCANNER_CACHE_TTL_SECONDS
    )
    if cached is not None:
        return cached
    try:
        response = session.get(
            f"{state.CLOB_REST_URL}/book",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.debug(f"book HTTP {response.status_code} for {str(token_id)[:14]}")
            return None
        data = response.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            return None
        best_bid = max(float(b.get("price", 0.0)) for b in bids)
        best_ask = min(float(a.get("price", 1.0)) for a in asks)
        if best_bid >= best_ask:
            return None
        spread_cents = (best_ask - best_bid) * 100.0
    except Exception as exc:  # noqa: BLE001 - an unread book is missing data
        logger.debug(f"book spread fetch failed for {str(token_id)[:14]}: {exc}")
        return None
    if spread_cents <= 0.0:
        return None
    state._cache_put(
        state._SPREAD_CACHE, token_id, spread_cents, state._BOOK_CACHE_MAX_ENTRIES
    )
    return spread_cents


def get_real_l2_score(
    session: requests.Session,
    token_id: str,
    max_spread_cents: float,
    timeout: float = 4.0,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Measure the venue's in-band competing score from the live L2 book.

    Returns ``(competing_score, total_qualifying_shares, mid)``, each
    ``Optional``. Every failure path - invalid spread, rate limit, non-200,
    empty book, crossed or one-sided book, network error - returns
    ``(None, None, None)``: it used to return ``(0.0, 0.0, 0.50)``, telling every
    caller that a market it could not read had a 0.50 midpoint and no
    competition, and a zero denominator then paid the whole pool. An unmeasured
    book is missing data and the caller must refuse the market.
    """
    unknown: tuple[None, None, None] = (None, None, None)
    now = time.time()
    # Check 5-second in-memory book cache
    cached_res = state._cache_get_fresh(state._BOOK_CACHE, token_id, state.SCANNER_CACHE_TTL_SECONDS)
    if cached_res is not None:
        return cached_res

    effective_spread = _coerce_float(max_spread_cents)
    if effective_spread is None or effective_spread <= 0.0:
        logger.debug(
            f"[L2 BOOK INVALID SPREAD] Refusing to score {str(token_id)[:14]} "
            f"with max_spread_cents={max_spread_cents!r}"
        )
        return unknown
    max_spread_cents = effective_spread

    with state._TELEMETRY_LOCK:
        state.GLOBAL_SCANNER_TELEMETRY.total_book_queries += 1
    url = f"{state.CLOB_REST_URL}/book"
    try:
        r = session.get(url, params={"token_id": token_id}, timeout=timeout)
        if r.status_code == 429:
            with state._TELEMETRY_LOCK:
                state.GLOBAL_SCANNER_TELEMETRY.http_429_errors += 1
                state.GLOBAL_SCANNER_TELEMETRY.last_error_time = now
                state.GLOBAL_SCANNER_TELEMETRY.last_error_message = (
                    f"HTTP 429 Too Many Requests on /book for token {token_id[:14]}"
                )
            logger.warning(f"[L2 BOOK 429 RATE LIMIT] Rate limited on /book for token {token_id[:14]}")
            stale = state._cache_get_stale(state._BOOK_CACHE, token_id)
            if stale is not None:
                return stale
            return unknown
        elif r.status_code != 200:
            return unknown

        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids and not asks:
            # An empty book publishes no mid: it has neither a measurable score
            # nor a measurable midpoint, so the caller must refuse it rather
            # than receive a 0.50 stand-in.
            return unknown

        best_bid = max(float(b.get("price", 0.0)) for b in bids) if bids else None
        best_ask = min(float(a.get("price", 1.0)) for a in asks) if asks else None

        # A crossed book (best_bid >= best_ask) cannot be scored without inventing a midpoint.
        if (
            bids
            and asks
            and best_bid is not None
            and best_ask is not None
            and best_bid >= best_ask
        ):
            logger.debug(
                f"[L2 BOOK CROSSED] bid {best_bid} >= ask {best_ask} for "
                f"{str(token_id)[:14]}; refusing to score"
            )
            stale = state._cache_get_stale(state._BOOK_CACHE, token_id)
            if stale is not None:
                return stale
            return unknown

        # The band is measured from the midpoint, so a book with one side (or a
        # locked one) has no midpoint and no score.
        if best_bid is None or best_ask is None or not 0.0 < best_bid < best_ask < 1.0:
            return unknown
        mid = (best_bid + best_ask) / 2.0

        comp_score = 0.0
        total_qual_shares = 0.0

        for b in bids:
            p = float(b.get("price", 0.0))
            sz = float(b.get("size", 0.0))
            spread_c = (mid - p) * 100.0
            if 0.0 <= spread_c <= max_spread_cents:
                comp_score += quadratic_order_score(max_spread_cents, spread_c, sz)
                total_qual_shares += sz

        for a in asks:
            p = float(a.get("price", 0.0))
            sz = float(a.get("size", 0.0))
            spread_c = (p - mid) * 100.0
            if 0.0 <= spread_c <= max_spread_cents:
                comp_score += quadratic_order_score(max_spread_cents, spread_c, sz)
                total_qual_shares += sz

        res = (comp_score, total_qual_shares, mid)
        state._cache_put(state._BOOK_CACHE, token_id, res, state._BOOK_CACHE_MAX_ENTRIES)
        return res
    except requests.exceptions.RequestException as e:
        with state._TELEMETRY_LOCK:
            state.GLOBAL_SCANNER_TELEMETRY.network_errors += 1
            state.GLOBAL_SCANNER_TELEMETRY.last_error_time = now
            state.GLOBAL_SCANNER_TELEMETRY.last_error_message = (
                f"Network error on /book for {token_id[:14]}: {e}"
            )
        logger.debug(f"Network error fetching order book for {token_id[:14]}: {e}")
        stale = state._cache_get_stale(state._BOOK_CACHE, token_id)
        if stale is not None:
            return stale
        return unknown
    except Exception as e:
        logger.debug(f"Error fetching order book for {token_id[:14]}: {e}")
        return unknown
