"""Deribit options surface: spot, DVOL, ATM IV and 25-delta skew.

Every ``poll_seconds`` the stream asks Deribit three questions per index:

* ``public/get_index_price`` for the spot,
* ``public/get_book_summary_by_currency`` for the whole option chain,
* ``public/get_volatility_index_data`` for DVOL (last close of the last hour).

The chain is then cut into expiry buckets near 7d / 14d / 30d. Per bucket the
ATM IV is the IV of the strike nearest spot, and the 25-delta skew is
``IV(25d call) - IV(ATM)`` / ``IV(25d put) - IV(ATM)`` with delta from
Black-Scholes at r=0 using the venue's ``mark_iv``.

The emitted ``atm_iv`` and skews come from the 30-day bucket (falling back to
the bucket nearest 30d when it has no valid option), so they line up with
``dvol``. A bucket with no usable option leaves its fields null; nothing is
coerced to zero.

Units: every volatility in the row is a decimal fraction (0.3958 = 39.58 vol
points). Deribit reports percent, so both ``mark_iv`` and DVOL are divided by
100 on the way in.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import orjson
import requests
from scipy.stats import norm

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import DERIBIT_SURFACE

NAME = "deribit"

DEFAULT_REST = "https://www.deribit.com/api/v2"
DEFAULT_INDEXES = ("btc_usd", "eth_usd")
DEFAULT_BUCKETS_DAYS = (7, 14, 30)

DAYS_PER_YEAR = 365.0
SECONDS_PER_DAY = 86400.0
MS_PER_DAY = SECONDS_PER_DAY * 1000.0
_REQUEST_TIMEOUT = 20.0
_USER_AGENT = "polymarketbot/0.1"

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_EXPIRY_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_expiry_ms(token: str) -> int | None:
    """``13NOV26`` -> the Deribit expiry instant, 08:00 UTC that day."""
    match = _EXPIRY_RE.match((token or "").upper())
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    try:
        moment = datetime(2000 + int(year), month, int(day), 8, 0, tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


def parse_instrument(name: str) -> dict[str, Any] | None:
    """``BTC-13NOV26-83000-P`` -> expiry, strike and option type."""
    parts = (name or "").split("-")
    if len(parts) < 4:
        return None
    expiry_ms = parse_expiry_ms(parts[1])
    strike = _as_float(parts[2])
    kind = parts[3].upper()
    if expiry_ms is None or strike is None or strike <= 0 or kind not in ("C", "P"):
        return None
    return {"expiry_ms": expiry_ms, "strike": strike, "is_call": kind == "C"}


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    is_call: bool,
    r: float = 0.0,
) -> float | None:
    """Black-Scholes delta with zero rates; ``None`` for degenerate inputs."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or vol <= 0:
        return None
    sigma_t = vol * math.sqrt(t_years)
    if sigma_t <= 0:
        return None
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t_years) / sigma_t
    nd1 = float(norm.cdf(d1))
    return nd1 if is_call else nd1 - 1.0


def parse_index_price(payload: dict[str, Any]) -> float | None:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    price = _as_float(result.get("index_price"))
    return price if price and price > 0 else None


def parse_dvol(payload: dict[str, Any]) -> float | None:
    """Last close of the DVOL series, as a decimal fraction."""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    data = result.get("data") or []
    if not data:
        return None
    last = data[-1]
    if not isinstance(last, (list, tuple)) or len(last) < 5:
        return None
    close = _as_float(last[4])
    if close is None or close <= 0:
        return None
    return close / 100.0


def parse_chain(book_rows: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """Keep the usable options: parsed instrument, live expiry, positive IV."""
    options: list[dict[str, Any]] = []
    for row in book_rows or []:
        if not isinstance(row, dict):
            continue
        info = parse_instrument(str(row.get("instrument_name") or ""))
        if info is None or info["expiry_ms"] <= now_ms:
            continue
        iv_pct = _as_float(row.get("mark_iv"))
        if iv_pct is None or iv_pct <= 0:
            continue
        options.append(
            {
                **info,
                "iv": iv_pct / 100.0,
                "underlying": _as_float(row.get("underlying_price")),
                "mid": _as_float(row.get("mid_price")),
            }
        )
    return options


def _bucket_metrics(options: list[dict[str, Any]], spot: float, now_ms: int) -> dict[str, Any] | None:
    if not options:
        return None
    expiry_ms = options[0]["expiry_ms"]
    t_years = (expiry_ms - now_ms) / 1000.0 / SECONDS_PER_DAY / DAYS_PER_YEAR
    if t_years <= 0:
        return None

    by_strike: dict[float, list[float]] = defaultdict(list)
    for option in options:
        by_strike[option["strike"]].append(option["iv"])
    atm_strike = min(by_strike, key=lambda strike: (abs(strike - spot), strike))
    ivs = by_strike[atm_strike]
    atm_iv = sum(ivs) / len(ivs)

    calls: list[tuple[float, float]] = []
    puts: list[tuple[float, float]] = []
    for option in options:
        delta = bs_delta(spot, option["strike"], t_years, option["iv"], option["is_call"])
        if delta is None:
            continue
        (calls if option["is_call"] else puts).append((delta, option["iv"]))

    call_25 = min(calls, key=lambda item: abs(item[0] - 0.25)) if calls else None
    put_25 = min(puts, key=lambda item: abs(item[0] + 0.25)) if puts else None

    return {
        "expiry_ms": expiry_ms,
        "days": (expiry_ms - now_ms) / MS_PER_DAY,
        "n": len(options),
        "atm_iv": atm_iv,
        "atm_strike": atm_strike,
        "skew_25d_call": None if call_25 is None else call_25[1] - atm_iv,
        "skew_25d_put": None if put_25 is None else put_25[1] - atm_iv,
    }


def _expiry_groups(options: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for option in options:
        groups[option["expiry_ms"]].append(option)
    return groups


def _bucket_expiry(groups: dict[int, list[dict[str, Any]]], now_ms: int, target: int) -> int | None:
    """Nearest expiry to ``target`` days, or ``None`` when the chain is too short.

    Without the tolerance a lone 7-day expiry would answer the 30-day bucket
    and the term structure would silently lie. The window is generous enough
    for weekly/monthly ladders (a 27-day expiry serves the 30-day bucket) but
    rejects reusing a much shorter or longer tenor.
    """
    if not groups:
        return None
    expiry = min(groups, key=lambda e: (abs((e - now_ms) / MS_PER_DAY - target), e))
    days = (expiry - now_ms) / MS_PER_DAY
    if abs(days - target) > max(2.0, target * 0.4):
        return None
    return expiry


def build_surface(
    index_name: str,
    spot: float,
    book_rows: list[dict[str, Any]],
    dvol: float | None,
    now_ms: int,
    recv_ts_ns: int,
    recv_mono_ns: int,
    buckets: tuple[int, ...] = DEFAULT_BUCKETS_DAYS,
) -> dict[str, Any]:
    """One ``deribit_surface`` row from one chain snapshot."""
    options = parse_chain(book_rows, now_ms)
    groups = _expiry_groups(options)

    metrics: dict[int, dict[str, Any] | None] = {}
    for target in buckets:
        expiry = _bucket_expiry(groups, now_ms, target)
        metrics[target] = None if expiry is None else _bucket_metrics(groups[expiry], spot, now_ms)

    valid = {target: m for target, m in metrics.items() if m is not None}
    anchor_target = None
    if valid:
        anchor_target = min(valid, key=lambda target: (abs(target - 30), -target))
    anchor = valid.get(anchor_target) if anchor_target is not None else None

    summary = {
        "spot": spot,
        "dvol": dvol,
        "anchor_days": anchor_target,
        "buckets": [
            {
                "target": target,
                "days": None if m is None else round(m["days"], 3),
                "expiry_ms": None if m is None else m["expiry_ms"],
                "n": 0 if m is None else m["n"],
                "atm_iv": None if m is None else round(m["atm_iv"], 6),
                "skew_25d_call": None if m is None or m["skew_25d_call"] is None else round(m["skew_25d_call"], 6),
                "skew_25d_put": None if m is None or m["skew_25d_put"] is None else round(m["skew_25d_put"], 6),
            }
            for target, m in metrics.items()
        ],
    }

    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": now_ms,
        "index_name": index_name,
        "dvol": dvol,
        "atm_iv": None if anchor is None else anchor["atm_iv"],
        "skew_25d_call": None if anchor is None else anchor["skew_25d_call"],
        "skew_25d_put": None if anchor is None else anchor["skew_25d_put"],
        "term_7d": (metrics.get(7) or {}).get("atm_iv") if 7 in buckets else None,
        "term_14d": (metrics.get(14) or {}).get("atm_iv") if 14 in buckets else None,
        "term_30d": (metrics.get(30) or {}).get("atm_iv") if 30 in buckets else None,
        "raw_json": orjson.dumps(summary).decode(),
    }


def parse_buckets(expiries: Any) -> tuple[int, ...]:
    """``["7d", "14d", "30d"]`` (or ``[7, 14, 30]``) -> ``(7, 14, 30)``."""
    out: list[int] = []
    for item in expiries or DEFAULT_BUCKETS_DAYS:
        text = str(item).strip().lower().rstrip("d")
        try:
            days = int(float(text))
        except ValueError:
            continue
        if days > 0:
            out.append(days)
    return tuple(sorted(set(out))) or DEFAULT_BUCKETS_DAYS


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _get_json(url: str, params: dict[str, Any], timeout: float = _REQUEST_TIMEOUT) -> Any:
    response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": _USER_AGENT})
    response.raise_for_status()
    return response.json()


async def _fetch_json(url: str, params: dict[str, Any], timeout: float = _REQUEST_TIMEOUT) -> Any:
    return await asyncio.to_thread(_get_json, url, params, timeout)


async def _sleep_or_stop(ctx: CollectorContext, seconds: float) -> bool:
    try:
        await asyncio.wait_for(ctx.stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #


async def _poll_index(
    ctx: CollectorContext,
    *,
    rest_url: str,
    index_name: str,
    currency: str,
    buckets: tuple[int, ...],
) -> None:
    log = ctx.log(NAME)
    now_ms = int(time.time() * 1000)
    recv_ts_ns = time.time_ns()
    recv_mono_ns = time.perf_counter_ns()

    spot: float | None = None
    try:
        payload = await _fetch_json(
            rest_url + "/public/get_index_price", {"index_name": index_name}
        )
        spot = parse_index_price(payload)
        if spot is None:
            ctx.health.incr(NAME, "index_price_empty")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ctx.health.incr(NAME, "index_price_errors")
        log.warning(f"{index_name} index price failed: {type(exc).__name__}: {exc}")

    try:
        payload = await _fetch_json(
            rest_url + "/public/get_book_summary_by_currency",
            {"currency": currency, "kind": "option"},
        )
        book_rows = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(book_rows, list):
            book_rows = []
            ctx.health.incr(NAME, "book_summary_empty")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ctx.health.incr(NAME, "book_summary_errors")
        log.warning(f"{index_name} book summary failed: {type(exc).__name__}: {exc}")
        return

    if spot is None:
        underlyings = [
            row["underlying"]
            for row in parse_chain(book_rows, now_ms)
            if row.get("underlying") is not None
        ]
        if underlyings:
            underlyings.sort()
            spot = underlyings[len(underlyings) // 2]
    if spot is None or spot <= 0:
        ctx.health.incr(NAME, "surface_skipped_no_spot")
        log.warning(f"{index_name} no spot price; skipping surface row")
        return

    dvol: float | None = None
    try:
        payload = await _fetch_json(
            rest_url + "/public/get_volatility_index_data",
            {
                "currency": currency,
                "start_timestamp": now_ms - 3600_000,
                "end_timestamp": now_ms,
                "resolution": 60,
            },
        )
        dvol = parse_dvol(payload)
        if dvol is None:
            ctx.health.incr(NAME, "dvol_empty")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ctx.health.incr(NAME, "dvol_errors")
        log.warning(f"{index_name} dvol failed: {type(exc).__name__}: {exc}")

    row = build_surface(
        index_name,
        spot,
        book_rows,
        dvol,
        now_ms,
        recv_ts_ns,
        recv_mono_ns,
        buckets,
    )
    ctx.emit("deribit_surface", row)
    ctx.health.incr(NAME, "rows_deribit_surface")
    ctx.health.gauge(NAME, f"spot_{index_name}", spot)
    if dvol is not None:
        ctx.health.gauge(NAME, f"dvol_{index_name}", dvol)


async def _index_loop(
    ctx: CollectorContext,
    *,
    rest_url: str,
    index_name: str,
    currency: str,
    buckets: tuple[int, ...],
    poll_seconds: float,
) -> None:
    log = ctx.log(NAME)
    while not ctx.stop.is_set():
        try:
            await _poll_index(
                ctx,
                rest_url=rest_url,
                index_name=index_name,
                currency=currency,
                buckets=buckets,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep polling whatever happens
            ctx.health.incr(NAME, f"poll_errors_{index_name}")
            log.warning(f"{index_name} poll failed: {type(exc).__name__}: {exc}")
        if await _sleep_or_stop(ctx, poll_seconds):
            return


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def run(ctx: CollectorContext) -> None:
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)
    ctx.register_table("deribit_surface", DERIBIT_SURFACE)

    rest_url = str(cfg.get("rest_url") or DEFAULT_REST).rstrip("/")
    poll_seconds = max(1.0, float(cfg.get("poll_seconds", 10)))
    indexes = [str(i) for i in (cfg.get("indexes") or DEFAULT_INDEXES)]
    buckets = parse_buckets(cfg.get("expiries"))

    log.info(f"deribit: indexes={indexes}, buckets={buckets}, poll={poll_seconds:.0f}s")

    tasks = [
        asyncio.create_task(
            _index_loop(
                ctx,
                rest_url=rest_url,
                index_name=index_name,
                currency=index_name.split("_")[0].upper(),
                buckets=buckets,
                poll_seconds=poll_seconds,
            )
        )
        for index_name in indexes
    ]

    stop_task = asyncio.create_task(ctx.stop.wait())
    try:
        await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, stop_task, return_exceptions=True)
    log.info("deribit stream stopped")
