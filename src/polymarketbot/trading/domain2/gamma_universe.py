"""Full-universe Gamma pagination with explicit fault accounting.

The settlement radar must never present a partial discovery sweep as the whole
universe. Each stratum advances only across contiguous pages that actually
returned data; a transient failure keeps its offset, is retried with the
repo's capped jittered backoff (``chain.jittered_delay``), and otherwise
surfaces as a fault on the returned sweep. A stratum is ``complete`` only when
Gamma proves the end of the universe with a short page or a past-end 4xx.

No timeout, retry count or backoff constant lives here: the caller passes the
Gamma policy it already uses (``GammaClient.timeout_s`` / ``max_retries``) and
the retry delay comes from ``polymarketbot.chain``.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from polymarketbot.chain import jittered_delay
from polymarketbot.logs import get_logger

log = get_logger("domain2_gamma_universe")

# Gamma /markets offset pagination answers 400/422 past the end of the
# universe (observed at offset 2300; see run_live_paper_soak's CTF paginator).
PAGE_END_HTTP: Tuple[int, ...] = (400, 422)

PAGE_OK = "ok"
PAGE_END = "end"
PAGE_ERROR = "error"


@dataclass(frozen=True)
class GammaPage:
    """One page fetch outcome. ``error`` is set only for ``status == error``."""

    offset: int
    status: str
    rows: Tuple[Dict[str, Any], ...] = ()
    error: str = ""


@dataclass
class StratumSweep:
    """Result of paginating one discovery stratum."""

    query: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    pages_ok: int = 0
    complete: bool = False
    faults: List[str] = field(default_factory=list)
    next_offset: int = 0

    @property
    def healthy(self) -> bool:
        """True only when the stratum was proven exhausted with no faults."""
        return self.complete and not self.faults


def fetch_page(
    session: Any,
    host: str,
    query: str,
    offset: int,
    page_size: int,
    timeout_s: float,
) -> GammaPage:
    """Fetch one page without swallowing the reason for a failure.

    ``end`` is only a past-end 4xx on a non-zero offset; a timeout, 429, 5xx or
    unparseable payload is ``error`` so the caller can retry without mistaking
    it for the end of the universe.
    """
    url = f"{host.rstrip('/')}/markets?limit={page_size}&offset={offset}&{query}"
    try:
        resp = session.get(url, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - network failure is a retryable page fault
        return GammaPage(offset, PAGE_ERROR, error=f"{type(exc).__name__}: {exc}")
    if resp.status_code == 200:
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - malformed payload is a page fault
            return GammaPage(offset, PAGE_ERROR, error=f"unparseable JSON: {exc}")
        if not isinstance(payload, list):
            return GammaPage(
                offset, PAGE_ERROR, error=f"payload {type(payload).__name__}, not a list"
            )
        return GammaPage(offset, PAGE_OK, rows=tuple(payload))
    if resp.status_code in PAGE_END_HTTP and offset > 0:
        return GammaPage(offset, PAGE_END)
    return GammaPage(offset, PAGE_ERROR, error=f"HTTP {resp.status_code}")


def sweep_stratum(
    session: Any,
    host: str,
    query: str,
    *,
    page_size: int,
    wave: int,
    timeout_s: float,
    max_attempts: int,
    is_running: Optional[Callable[[], bool]] = None,
    on_fault: Optional[Callable[[str], None]] = None,
) -> StratumSweep:
    """Paginate one stratum, retrying failed pages without losing the frontier.

    Pages are fetched ``wave`` at a time. A wave advances the frontier only if
    every offset before the frontier returned data; a permanent failure stops
    the sweep with ``complete=False`` and ``next_offset`` at the first missing
    page, so the caller can resume instead of treating a hole as universe end.
    """
    should_continue = is_running or (lambda: True)
    sweep = StratumSweep(query=query)
    attempts = max(1, int(max_attempts))
    base = 0

    while should_continue():
        offsets = tuple(base + i * page_size for i in range(wave))
        pages: Dict[int, GammaPage] = {}
        last_errors: Dict[int, str] = {}
        pending: Tuple[int, ...] = offsets

        for attempt in range(attempts):
            if not pending or not should_continue():
                break
            with ThreadPoolExecutor(max_workers=max(1, min(len(pending), wave))) as pool:
                fetched = list(
                    pool.map(
                        lambda off: fetch_page(session, host, query, off, page_size, timeout_s),
                        pending,
                    )
                )
            for page in fetched:
                pages[page.offset] = page
                if page.status == PAGE_ERROR:
                    last_errors[page.offset] = page.error
                else:
                    last_errors.pop(page.offset, None)
            pending = tuple(sorted(last_errors))
            if pending and attempt + 1 < attempts:
                time.sleep(jittered_delay(attempt))

        advance = base
        end_reached = False
        wave_faulted = False
        for off in offsets:
            page = pages.get(off)
            if page is None or page.status == PAGE_ERROR:
                wave_faulted = True
                break
            if page.status == PAGE_END:
                end_reached = True
                break
            sweep.pages_ok += 1
            sweep.rows.extend(page.rows)
            advance = off + page_size
            if len(page.rows) < page_size:
                end_reached = True
                break

        if end_reached:
            sweep.complete = True
            sweep.next_offset = advance
            break
        if wave_faulted:
            sweep.next_offset = advance
            break
        base = advance

    for off in sorted(last_errors):
        fault = f"offset={off} {last_errors[off]}"
        sweep.faults.append(fault)
        if on_fault is not None:
            on_fault(f"Gamma page {fault}")
    if sweep.faults:
        log.warning(
            "Gamma stratum partial sweep ({}): {} page fault(s), {} page(s) ok, "
            "next_offset={} - a transient failure is not end-of-universe",
            query,
            len(sweep.faults),
            sweep.pages_ok,
            sweep.next_offset,
        )
    return sweep
