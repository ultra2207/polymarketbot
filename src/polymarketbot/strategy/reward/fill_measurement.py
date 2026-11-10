"""Measured fill side of a reward quote: horizon fill probability and markout.

Both tables are the same measurements the CTF maker gate prices against
(``polymarketbot.strategy.reward.measurement``), so the two pillars cannot
disagree about how often a resting bid at the touch is filled. Every function
here is a pure function of those tables; the only state is the one-shot load
cache, which lives here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from polymarketbot.logs import get_logger

log = get_logger("polymarketbot.strategy.reward.decision")

_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True)
class MarketFillYield:
    """The fill side of a reward quote's yield, from the measured L2 tape.

    A reward bid earns its share of the daily pool only while it is **in-band
    and unfilled**: a fill converts the resting capital into inventory, stops
    the accrual on the filled part, and pays the measured adverse drift. Both
    halves are measured, never assumed.
    """

    p_fill: float
    """P(filled within ``horizon_s``), from ``ctf_quote_fill_horizon.json``."""
    horizon_s: float
    """The horizon the probability was measured at (pinned to support)."""
    markout_usd_per_share: float
    """Measured adverse drift of a filled leg, ``ctf_maker_markout.json``."""
    fill_edge_per_usd: float
    """``(leg_mid - quote - markout) / quote``: what one fill earns per dollar."""
    fill_yield_usd_per_usd_per_day: float
    """``p_fill * (86400 / horizon_s) * fill_edge_per_usd``: expected fill yield."""

    @property
    def stay_fraction(self) -> float:
        """Share of one horizon the bid is expected to rest unfilled."""
        return max(0.0, 1.0 - self.p_fill)


_QUOTE_FILL_HORIZON: Any = None
_QUOTE_FILL_HORIZON_TRIED = False
_MAKER_MARKOUT: Any = None
_MAKER_MARKOUT_TRIED = False


def _fill_table() -> Any:
    """The measured horizon fill table, or ``None`` when it is unusable.

    Loaded from the same artifacts the CTF maker gate prices against
    (``polymarketbot.strategy.reward.measurement``), so the two pillars cannot disagree
    about how often a resting bid at the touch is filled.
    """
    global _QUOTE_FILL_HORIZON, _QUOTE_FILL_HORIZON_TRIED
    if not _QUOTE_FILL_HORIZON_TRIED:
        _QUOTE_FILL_HORIZON_TRIED = True
        try:
            from polymarketbot.strategy.reward.measurement import load_quote_fill_horizon

            _QUOTE_FILL_HORIZON = load_quote_fill_horizon()
        except Exception as exc:  # noqa: BLE001 - a missing measurement is data, not a crash
            log.warning(
                f"reward fill side unpriced: horizon fill measurement unavailable ({exc})"
            )
            _QUOTE_FILL_HORIZON = None
    return _QUOTE_FILL_HORIZON


def _markout_table() -> Any:
    """The measured single-leg markout, or ``None`` when it is unusable."""
    global _MAKER_MARKOUT, _MAKER_MARKOUT_TRIED
    if not _MAKER_MARKOUT_TRIED:
        _MAKER_MARKOUT_TRIED = True
        try:
            from polymarketbot.strategy.reward.measurement import load_maker_markout

            _MAKER_MARKOUT = load_maker_markout()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"reward fill side unpriced: single-leg markout unavailable ({exc})"
            )
            _MAKER_MARKOUT = None
    return _MAKER_MARKOUT


def expected_fill_yield(
    *,
    tick: float,
    min_flow_shares: float,
    horizon_s: float,
    quote_price: float,
    leg_mid: float,
) -> Optional[MarketFillYield]:
    """Expected fill yield of one reward bid, from the two measured tape tables.

    ``min_flow_shares`` is the size our own bid must be met with - the venue's
    reward minimum in shares, which is also the smallest order that can rest
    there - so the probability is read at the flow bucket that covers *our*
    order, not at a threshold.

    Returns ``None`` - and the caller must refuse the market rather than price
    the quote on half a measurement - when either table has no measured cell
    for this ``(horizon, tick)`` or for this required flow.
    """
    if not 0.0 < quote_price < 1.0 or not 0.0 < leg_mid < 1.0:
        return None
    fills = _fill_table()
    if fills is None:
        return None
    pinned = fills.clamped_p_fill(float(horizon_s), float(tick), float(min_flow_shares))
    if pinned is None:
        return None
    p_fill, priced_horizon = pinned
    markouts = _markout_table()
    if markouts is None:
        return None
    adverse = markouts.markout(priced_horizon, float(tick))
    if adverse is None:
        return None
    edge_per_usd = (float(leg_mid) - float(quote_price) - float(adverse)) / float(quote_price)
    per_day = p_fill * (_SECONDS_PER_DAY / max(1e-9, float(priced_horizon))) * edge_per_usd
    return MarketFillYield(
        p_fill=float(p_fill),
        horizon_s=float(priced_horizon),
        markout_usd_per_share=float(adverse),
        fill_edge_per_usd=float(edge_per_usd),
        fill_yield_usd_per_usd_per_day=float(per_day),
    )




def markout_cost_usd(
    shares: float, markout_cents_per_share: Optional[float]
) -> float:
    """Adverse-move cost of ``shares`` filled at the quote, in USDC.

    The paper engine already books the *realised* adverse move: a fill enters
    ``Ledger`` at the quote price and :meth:`Engine._mark` re-values every lot
    at the live best bid on each tick, so a drop after the fill lands in equity
    immediately. A booking-side markout term on top of that would count the
    same move twice, and this window produced no fill to book either way.

    What is missing is the *ex-ante* term: the quote-selection rule prices a
    fill at the touch and ignores that the touching taker is informed. That
    term needs a measured markout, and there is no defensible constant for it
    here: cycle 2 measured -0.209 c/share (-0.585 %) over **2** fills. On a
    prediction market the per-fill dispersion is the same order as the mean, so
    the standard error at n=2 is of the same size as the estimate and no fit is
    possible.

    The 2026-11-10 12:24 tape can now supply the *sample* - 258 SELL prints at
    the price the pillar would rest at across 40 live reward candidates, 246 of
    them with a later print to mark against - but not the *horizon*: marking
    each fill with the next print of the same token realises a median gap of
    1,710 s against a nominal 60 s, and the estimate it produces is +0.311 c
    +/- 0.133 c (t = +2.33), the opposite sign to the earlier 2-fill reading.
    The missing input is therefore not prints but a persisted best-bid/mid
    series at a fixed offset from the fill: this soak writes equity snapshots
    only, so a fixed-Delta markout cannot be computed from it. The parameter
    stays ``None`` until that series exists; ``None`` -> 0.0 with no invented
    default, and the caller is expected to log that the fill is priced without
    one.

    Units: ``shares`` is shares, ``markout_cents_per_share`` is USDC cents per
    share (negative for an adverse move the pillar pays), result is USDC.
    """
    if markout_cents_per_share is None or shares <= 0.0:
        return 0.0
    return float(shares) * float(markout_cents_per_share) / 100.0


