"""NTP-style clock-offset estimation for the venues in the tape.

Why this exists
---------------
Every cross-venue statement about timing ("the Binance BBO moved 200 ms before
the Polymarket book") compares two *server* timestamps. Those stamps come from
two different machines whose clocks are not the same, so the comparison is
only meaningful after the difference between them is measured. ``recv_ts_ns``
is stamped by one local clock and therefore hides the inter-venue skew inside
the network delay instead of removing it.

Method
------
One request to a venue's time endpoint, three timestamps:

    t1  local clock, immediately before the request is sent
    ts  venue clock, read from the response body
    t2  local clock, immediately after the response body has been read

Assuming the request and response paths take the same time (the standard NTP
assumption), the venue clock is ``ts`` at local time ``(t1 + t2) / 2``:

    offset = ts - (t1 + t2) // 2        venue clock minus local clock
    rtt    = t2 - t1                    largest possible one-way error

The offset is only known to within ``rtt / 2``: a slow request path and a fast
response path push the estimate the other way. ``rtt / 2`` is therefore the
error bar, and the sample with the smallest rtt is the best estimate. On top of
that, a venue whose endpoint truncates to whole seconds (the CLOB does) hides
the true value anywhere in the following second; see ``OFFSET_WINDOW_NS``.

One sample is therefore an interval, not a number. Several samples taken at
different sub-second phases intersect those intervals (``offset_bracket``), and
the intersection is often far tighter than any single sample: five CLOB probes
bracketed the offset to ~280 ms where each individual probe was worth a second.

Rows are written to ``<root>/clock_offset/date=/hour=/part-clock-*.parquet``
with ``CLOCK_OFFSET_SCHEMA`` and are read back with ``load_samples``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from polymarketbot.logs import get_logger

logger = get_logger("netclock")

BINANCE_TIME_URL = "https://fapi.binance.com/fapi/v1/time"
CLOB_TIME_URL = "https://clob.polymarket.com/time"

DEFAULT_VENUES: dict[str, str] = {"binance": BINANCE_TIME_URL, "clob": CLOB_TIME_URL}

# The CLOB `/time` endpoint answers with whole Unix seconds (truncated), so the
# true offset is somewhere in the second that follows the estimate. Binance
# reports milliseconds, a resolution finer than its own rtt, so its window is 0.
OFFSET_WINDOW_NS: dict[str, int] = {"clob": 1_000_000_000, "binance": 0}

DEFAULT_ROOT = Path("data/tape")
DEFAULT_TIMEOUT_SECONDS = 10.0
USER_AGENT = "polymarketbot/0.1"

CLOCK_OFFSET_SCHEMA = pa.schema(
    [
        ("stream", pa.string()),
        ("host", pa.string()),
        ("offset_ns", pa.int64()),
        ("rtt_ns", pa.int64()),
        ("recv_ts_ns", pa.int64()),
    ]
)


# --------------------------------------------------------------------- maths
def estimate_offset(t1_ns: int, ts_ns: int, t2_ns: int) -> tuple[int, int]:
    """Return ``(offset_ns, rtt_ns)`` for one request/response pair.

    ``offset_ns`` is venue clock minus local clock; add it to a local
    timestamp to express it in venue time. The midpoint uses integer floor
    division, so a pair whose bounds sum to an odd number of nanoseconds is
    truncated by at most 1 ns - far below the clock resolution of any venue.
    """
    if t2_ns < t1_ns:
        raise ValueError(f"response before request: t1={t1_ns} t2={t2_ns}")
    return ts_ns - (t1_ns + t2_ns) // 2, t2_ns - t1_ns


def one_way_offset_ns(venue_ts_ns: int, recv_ts_ns: int) -> int:
    """Offset from a message's own timestamp, with no round trip.

    This is what a feed's ``timestamp`` field allows when no time endpoint is
    reachable. It is biased by the one-way network delay, which is unknown: the
    message was stamped before it arrived, so the value is a *lower* bound on
    the offset (it understates it by exactly that delay). Use it as a sanity
    check on an rtt-based estimate, never as the estimate itself.
    """
    return venue_ts_ns - recv_ts_ns


def offset_interval(sample: "ClockSample") -> tuple[int, int]:
    """Interval the true offset of ``sample`` is known to lie in, in ns.

    Lower edge: the response could have arrived on the fast path, so the true
    offset may be ``rtt / 2`` below the estimate. Upper edge: the response may
    be ``rtt / 2`` slow, and a truncating endpoint hides up to a whole unit
    (one second for the CLOB) above the estimate.
    """
    half_rtt = max(0, sample.rtt_ns) // 2
    return (
        sample.offset_ns - half_rtt,
        sample.offset_ns + half_rtt + OFFSET_WINDOW_NS.get(sample.stream, 0),
    )


def offset_bracket(samples: Iterable["ClockSample"]) -> tuple[int, int]:
    """Intersect the intervals of the lowest-RTT samples of one venue.

    Each sample constrains the offset to an interval; the true value is in all
    of them, so it is in their intersection. Sampling at different sub-second
    phases is what makes the intersection narrow.

    Only the low-RTT cluster participates: a queued request can have a much
    larger RTT than the path's real delay, and its interval is correspondingly
    wrong. Intersecting *all* samples therefore collapses to the union as soon
    as one sample is delayed, which is not a measurement uncertainty -- it is
    the estimator being poisoned by a single outlier. The cluster is the
    samples within 1.5x (or +50 ms) of the best RTT. If even that cluster
    disagrees, the tightest single sample interval is returned and the
    disagreement is logged; the union of all samples is never returned.
    """
    ordered = sorted(samples, key=lambda sample: sample.rtt_ns)
    if not ordered:
        raise ValueError("no samples to bracket")
    best = ordered[0]
    cutoff = max(int(best.rtt_ns * 1.5), best.rtt_ns + 50_000_000)
    cluster = [sample for sample in ordered if sample.rtt_ns <= cutoff]
    intervals = [offset_interval(sample) for sample in cluster]
    lo = max(lower for lower, _ in intervals)
    hi = min(upper for _, upper in intervals)
    if lo > hi:
        logger.warning(
            f"clock samples disagree at the low-RTT cluster "
            f"(best rtt {best.rtt_ns / 1e6:.1f} ms); using the tightest sample"
        )
        return best.interval_ns
    return lo, hi


def best_sample(samples: Iterable["ClockSample"]) -> "ClockSample | None":
    """The sample with the smallest round trip, i.e. the smallest error bar."""
    best: ClockSample | None = None
    for sample in samples:
        if best is None or sample.rtt_ns < best.rtt_ns:
            best = sample
    return best


# ------------------------------------------------------------------- parsing
_UNITS = (
    (Decimal(10) ** 11, Decimal(10) ** 9),  # seconds
    (Decimal(10) ** 14, Decimal(10) ** 6),  # milliseconds
    (Decimal(10) ** 17, Decimal(10) ** 3),  # microseconds
)


def _to_ns(value: Any) -> int:
    """Convert an epoch timestamp of unknown unit to nanoseconds.

    Decimal is used rather than float because ``1793771316561 * 1e6`` in
    float64 is off by 64 ns - larger than the quantisation the estimate is
    trying to resolve. The unit is chosen by magnitude, with each boundary
    placed well away from any plausible value of the unit below it: a seconds
    timestamp is ~1.8e9, milliseconds ~1.8e12, microseconds ~1.8e15 and
    nanoseconds ~1.8e18, so 1e11, 1e14 and 1e17 separate them.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not a timestamp: {value!r}")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"not a timestamp: {value!r}") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"not a timestamp: {value!r}")
    for limit, scale in _UNITS:
        if number < limit:
            return int((number * scale).to_integral_value(rounding=ROUND_HALF_UP))
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


_TIME_KEYS = ("serverTime", "time", "timestamp", "server_time", "time_ms", "ts")


def _mapping_time(payload: Mapping[str, Any]) -> int:
    for key in _TIME_KEYS:
        if key in payload:
            return _to_ns(payload[key])
    raise ValueError(f"no server time field in {sorted(payload)}")


def parse_binance_time(payload: Any) -> int:
    """``GET /fapi/v1/time`` -> ``{"serverTime": <epoch ms>}``, in nanoseconds."""
    if isinstance(payload, Mapping):
        return _mapping_time(payload)
    return _to_ns(payload)


def parse_clob_time(payload: Any) -> int:
    """``GET https://clob.polymarket.com/time`` -> bare epoch seconds.

    Whole seconds only, hence ``OFFSET_WINDOW_NS["clob"]``. A mapping is
    accepted too, because the endpoint has also been documented with a
    ``time`` field.
    """
    if isinstance(payload, Mapping):
        return _mapping_time(payload)
    return _to_ns(payload)


def parse_server_time(venue: str, payload: Any) -> int:
    if venue == "binance":
        return parse_binance_time(payload)
    if venue == "clob":
        return parse_clob_time(payload)
    raise KeyError(f"unknown venue {venue!r}")


# ------------------------------------------------------------------- samples
@dataclass(frozen=True)
class ClockSample:
    """One offset measurement, shaped exactly like a ``clock_offset`` row."""

    stream: str
    host: str
    offset_ns: int
    rtt_ns: int
    recv_ts_ns: int

    def to_row(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "host": self.host,
            "offset_ns": self.offset_ns,
            "rtt_ns": self.rtt_ns,
            "recv_ts_ns": self.recv_ts_ns,
        }

    @property
    def interval_ns(self) -> tuple[int, int]:
        return offset_interval(self)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def sample_venue(
    venue: str,
    *,
    url: str | None = None,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ClockSample:
    """One round trip to ``venue``'s time endpoint; raises on any failure.

    ``t1`` is taken immediately before ``get`` and ``t2`` immediately after it
    returns, which is after the body has been read - so ``rtt`` includes the
    response transfer, as it must.
    """
    endpoint = url or DEFAULT_VENUES.get(venue)
    if endpoint is None:
        raise KeyError(f"no time endpoint for venue {venue!r}")
    sess = session or _session()
    t1_ns = time.time_ns()
    response = sess.get(endpoint, timeout=timeout)
    t2_ns = time.time_ns()
    if response.status_code != 200:
        raise RuntimeError(f"{venue} time endpoint returned HTTP {response.status_code}")
    venue_ts_ns = parse_server_time(venue, orjson.loads(response.content))
    offset_ns, rtt_ns = estimate_offset(t1_ns, venue_ts_ns, t2_ns)
    return ClockSample(
        stream=venue,
        host=urlsplit(endpoint).hostname or "",
        offset_ns=offset_ns,
        rtt_ns=rtt_ns,
        recv_ts_ns=t2_ns,
    )


def probe(
    venues: Iterable[str] | None = None,
    *,
    count: int = 3,
    interval_seconds: float = 1.0,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[ClockSample]:
    """Sample every venue ``count`` times, round-robin, and return what worked.

    The loop is round-robin rather than venue-at-a-time so all venues are
    sampled inside the same few seconds of local clock drift. A failed request
    is logged and skipped; the caller compares the per-venue counts and decides
    whether it has enough samples.
    """
    names = list(venues or DEFAULT_VENUES)
    sess = session or _session()
    samples: list[ClockSample] = []
    for round_index in range(max(1, count)):
        for venue in names:
            try:
                samples.append(sample_venue(venue, session=sess, timeout=timeout))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"clock probe {venue} failed: {exc}")
        if round_index + 1 < count:
            time.sleep(max(0.0, interval_seconds))
    return samples


# ------------------------------------------------------------------- storage
def record_samples(samples: Iterable[ClockSample], root: str | Path = DEFAULT_ROOT) -> Path | None:
    """Write samples as one Parquet part; return the path (``None`` if empty).

    One part per probe run, written to a temporary file and renamed, so a
    reader never sees a half-written part. The layout matches the rest of the
    tape: ``<root>/clock_offset/date=YYYY-MM-DD/hour=HH/part-clock-*.parquet``.
    """
    rows = [sample.to_row() for sample in samples]
    if not rows:
        return None
    table = pa.Table.from_pylist(rows, schema=CLOCK_OFFSET_SCHEMA)
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    dest_dir = (
        Path(root) / "clock_offset"
        / f"date={time.strftime('%Y-%m-%d', time.gmtime(now))}"
        / f"hour={time.strftime('%H', time.gmtime(now))}"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    seq = 0
    dest = dest_dir / f"part-clock-{stamp}-{seq:02d}.parquet"
    while dest.exists():
        seq += 1
        dest = dest_dir / f"part-clock-{stamp}-{seq:02d}.parquet"
    tmp = dest.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd", compression_level=7)
    os.replace(tmp, dest)
    logger.info(f"recorded {len(rows)} clock offsets -> {dest}")
    return dest


def load_samples(
    root: str | Path = DEFAULT_ROOT, *, max_parts: int = 8
) -> dict[str, list[ClockSample]]:
    """Recorded samples per venue from the newest ``max_parts`` probe parts.

    Several samples are returned, not one, because their intervals intersect to
    something tighter than any single sample (``offset_bracket``). A fresh
    offset is the only useful one, so older parts are ignored. An empty result
    means no probe has run yet; callers must report the measurement as
    uncorrected rather than assume zero.
    """
    parts = sorted(Path(root).glob("clock_offset/date=*/hour=*/*.parquet"))
    out: dict[str, list[ClockSample]] = {}
    for part in parts[-max_parts:]:
        table = pq.read_table(
            part, columns=["stream", "host", "offset_ns", "rtt_ns", "recv_ts_ns"]
        )
        for row in table.to_pylist():
            sample = ClockSample(**row)
            out.setdefault(sample.stream, []).append(sample)
    return out
