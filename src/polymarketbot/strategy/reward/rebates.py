"""Maker rebates: credit the venue's own number, fall back to the model.

The Maker Rebates program pays a **per-wallet** daily entitlement: a share of
the taker fees the maker's filled liquidity generated, computed per market
``rebate = (your_fee_equivalent / total_fee_equivalent) * rebate_pool`` and paid
daily in pUSD above a $1 floor
(``docs.polymarket.com/programs/maker-rebates``). It is not aggregate
maker-side flow, and a wallet that fills can read its own accrual from
``GET /rebates/current?date=&maker_address=``, which returns
``{date, condition_id, asset_address, maker_address, rebated_fees_usdc}`` per
condition (an array, or ``null`` when there are no rows).

That endpoint is authoritative because it already applies the per-market
rebate pool, the fee curve and the venue's rounding, none of which the
modelled :func:`polymarketbot.fees.maker_fee_rebate` can observe: the model
prices one fill from the market's published rebate share and knows nothing
about competing maker flow. This module therefore credits the *increment* of
the venue's per-condition total since the last credit, and falls back to the
model only when the endpoint cannot be read - recording which source produced
each credit so the journal (and :mod:`polymarketbot.strategy.reward.reconciler`) can
say so.

The poll cadence is the venue's own sampling cadence (1,440 samples per UTC
day, i.e. once a minute), not the engine tick: rebates accrue continuously but
the published resolution of the daily figure is that sampling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from polymarketbot.fees import FeeSchedule, maker_fee_rebate
from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.scoring import PROTOCOL_EPOCH_SAMPLES

log = get_logger(__name__)

_SECONDS_PER_DAY = 86_400.0
# One refresh per venue sample interval: 86,400 / 1,440 = 60 s.
REBATE_REFRESH_INTERVAL_S = _SECONDS_PER_DAY / float(PROTOCOL_EPOCH_SAMPLES)

# Journal details: distinct so a reader can tell a venue number from a modelled
# one without a schema change, and so reconcile can split them. The modelled
# detail keeps the historical name so existing journals stay readable.
DETAIL_VENUE = "maker_rebate_venue"
DETAIL_MODELLED = "maker_rebate"


@dataclass(frozen=True)
class RebateCredit:
    """One credit to book: ``usd`` from ``source`` ("venue" or "modelled")."""

    condition_id: str
    usd: float
    source: str

    @property
    def detail(self) -> str:
        return DETAIL_VENUE if self.source == "venue" else DETAIL_MODELLED


class RebateAccountant:
    """Tracks venue rebate totals and hands the runner what is new to credit.

    State is per UTC day: the venue's number is the running daily accrual, so a
    new day starts from zero rather than carrying yesterday's total. Every
    credit - venue or modelled - is accumulated in ``_credited`` so the next
    venue reading credits only the increment and a fallback that later turns
    out to have been too generous cannot be double-counted (the gap is logged
    instead, and reconcile shows it).
    """

    def __init__(
        self,
        client: Any = None,
        *,
        refresh_s: float = REBATE_REFRESH_INTERVAL_S,
    ) -> None:
        self._client = client
        self._refresh_s = float(refresh_s)
        self._day: Optional[str] = None
        self._venue_total: dict[str, float] = {}
        self._credited: dict[str, float] = {}
        self._last_poll = -math.inf
        self._source = "unpolled"
        self._error = ""
        self._rows = 0

    # -- read-only state, for logs and reconcile --------------------------
    @property
    def source(self) -> str:
        """Which source the last attempt used: "venue", "modelled" or "unpolled"."""
        return self._source

    @property
    def error(self) -> str:
        """Why the venue source was not used (empty when it was)."""
        return self._error

    @property
    def rows(self) -> int:
        """Venue rows in the last successful read."""
        return self._rows

    def _roll(self, day: str) -> None:
        if self._day == day:
            return
        if self._day is not None:
            log.info(
                f"rebate day rolled {self._day} -> {day}: resetting the venue "
                f"total (credited ${sum(self._credited.values()):,.4f} yesterday)"
            )
        self._day = day
        self._venue_total.clear()
        self._credited.clear()
        self._last_poll = -math.inf
        self._rows = 0

    def _fall_back(self, reason: str) -> None:
        """Record that the venue number is unavailable, logging the transition."""
        if self._source != "modelled":
            log.warning(
                f"rebates: venue endpoint unreadable ({reason}); crediting the "
                f"modelled fees.maker_fee_rebate until it returns"
            )
        self._source = "modelled"
        self._error = reason

    def sync(self, now: float, day: str) -> list[RebateCredit]:
        """Poll the venue (at most once per refresh interval) for new credits.

        Returns only the *increment* per condition, so the caller can credit
        each one as cash. A failed read switches the source to ``modelled`` and
        keeps the error text; it never invents a number.
        """
        self._roll(day)
        if self._client is None:
            self._fall_back(
                "no CLOB client in this process: /rebates/current needs a "
                "maker address"
            )
            return []
        if now - self._last_poll < self._refresh_s:
            return []
        self._last_poll = float(now)
        payload = self._client.get_current_rebates(day)
        source = str(payload.get("source") or "unavailable")
        if source != "clob_api":
            self._fall_back(str(payload.get("error") or f"rebate source {source}"))
            return []
        if self._source == "modelled":
            log.info(
                "rebates: venue endpoint readable again; switching from the "
                "modelled fallback to /rebates/current"
            )
        self._source = "venue"
        self._error = ""
        self._venue_total = {
            str(condition_id): float(usd)
            for condition_id, usd in (payload.get("by_condition") or {}).items()
        }
        self._rows = int(payload.get("row_count") or 0)
        credits: list[RebateCredit] = []
        for condition_id, total in self._venue_total.items():
            delta = total - self._credited.get(condition_id, 0.0)
            if delta > 1e-9:
                self._credited[condition_id] = self._credited.get(condition_id, 0.0) + delta
                credits.append(RebateCredit(condition_id, delta, "venue"))
            elif delta < -1e-9:
                # The venue's running total fell (a correction, or a modelled
                # fallback that overshot). Cash already booked is not clawed
                # back here; the discrepancy is logged and reconcile reports it.
                log.warning(
                    f"rebate venue total for {condition_id[:12]} fell by "
                    f"${-delta:,.6f} below what the ledger already credited; "
                    f"not reversing booked cash, reconcile will show the gap"
                )
        return credits

    def modelled_credit(
        self,
        *,
        condition_id: str,
        price: float,
        shares: float,
        schedule: Optional[FeeSchedule],
    ) -> RebateCredit:
        """Modelled fallback for one fill; zero whenever the venue source is live.

        Only called after a fill, so it cannot price a market that did not
        trade. ``source`` on the returned credit is "modelled" and the journal
        detail says so.
        """
        if self._source == "venue" or schedule is None:
            return RebateCredit(condition_id, 0.0, "modelled")
        rebate = maker_fee_rebate(price, shares, schedule)
        if rebate > 0.0:
            self._credited[condition_id] = self._credited.get(condition_id, 0.0) + rebate
        return RebateCredit(condition_id, rebate, "modelled")

    def describe(self) -> str:
        """One-line provenance for the accrual log."""
        if self._source == "venue":
            return (
                f"venue (/rebates/current, {self._rows} row(s), "
                f"${sum(self._venue_total.values()):,.4f} accrued today)"
            )
        if self._source == "unpolled":
            return "unpolled (no fill credited yet)"
        return f"modelled fees.maker_fee_rebate - venue unreadable: {self._error}"
