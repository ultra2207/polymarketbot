"""One interpretation of a market's expiry field, shared by the tape recorders.

Both recorders tag each row with the market's window end, and a row tagged with
a different expiry depending on which recorder wrote it cannot be joined. The
field arrives in the shapes the venue actually publishes:

* ``"2026-12-01T00:00:00Z"`` - ISO 8601 with a time and zone (``endDate`` /
  ``endDateIso`` on current payloads);
* ``"2026-12-01T00:00:00"`` - ISO 8601 without a zone, read as UTC;
* ``"2026-12-01"`` and ``"Dec 1, 2026"`` - a calendar date, which is what
  ``clobRewards[].endDate`` carries and what some older Gamma rows put in
  ``endDate``. It is read as UTC midnight.

``None`` means "no usable value was supplied" - never epoch zero and never an
invented date. A caller that knows the window from another source (the crypto
slugs encode start + interval) passes ``require_time=True`` so a date-only
field is refused instead of putting a five-minute window at midnight.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Month-name month + day + year, with or without the comma. `%b` covers "Dec",
# `%B` covers "December".
_MONTH_NAME_FORMATS = ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y")


def _parse(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _MONTH_NAME_FORMATS:
        try:
            # The format has no zone by definition; UTC is attached by the
            # caller-visible path below, so the naive value never escapes.
            return datetime.strptime(text, fmt)  # noqa: DTZ007
        except ValueError:
            continue
    return None


def parse_expiry_ms(*values: Any, require_time: bool = False) -> int | None:
    """UTC epoch milliseconds from the first usable expiry field in ``values``.

    ``values`` are tried in order, so a caller passes the documented ISO field
    first and the looser legacy field second. With ``require_time`` a value
    that carries no time of day (a bare calendar date) is skipped rather than
    read as midnight.
    """
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        parsed = _parse(text)
        if parsed is None:
            continue
        if require_time and "T" not in text and ":" not in text:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    return None
