"""Measured horizon-matched fill probability for the CTF maker gate.

The table in ``data/derived/ctf_quote_fill_horizon.json`` is built by
``polymarketbot.tools.ctf_quote_fill_horizon`` from the recorded L2 tape.  This
module only loads it and answers ``p_fill(horizon, tick, flow)``; nothing here
invents a probability, and a horizon outside the measured support returns
``None``.  The clamp warning helpers live here because they describe what the
table's own support can and cannot price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("paper.ctf")

# ---------------------------------------------------------------------------
# Measured horizon-matched fill probability
# (data/derived/ctf_quote_fill_horizon.json, built by
# ``polymarketbot.tools.ctf_quote_fill_horizon`` from the recorded L2 tape).
#
# The print-conditional model in ``fill_model.json`` answers "given that a print
# hit this level, did it clear the queue ahead of us".  It carries no arrival
# rate, so applying it to a resting quote is a conditional probability used as
# a joint one: the soak quoted 1,262 legs a tick for 30 minutes and filled
# none.  The gate needs P(a quote fills within the lifetime it actually rests),
# which is what this table measures.
# ---------------------------------------------------------------------------
# archive/ctf/pillars/fill_model.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QUOTE_FILL_HORIZON_PATH = (
    _REPO_ROOT / "data" / "derived" / "ctf_quote_fill_horizon.json"
)


@dataclass(frozen=True)
class QuoteFillHorizon:
    """Measured P(a resting buy at ``best_bid + tick`` fills) by horizon.

    ``cells`` holds ``(horizon_s, tick, min_flow_shares, p_fill)`` measured on
    the recorded L2 tape: a placement every engine tick, a fill when a SELL
    print crosses the quote within the horizon with enough size to meet the
    venue minimum order.

    A horizon between two measured cells is linearly interpolated in time. That
    is a measured approximation, not a guess: withholding each measured cell and
    predicting it from its neighbours gives errors of 3.39 %, 2.92 %, 5.31 %,
    0.30 % and 0.33 % on the looser at-or-below series (worst 5.31 %, on 8.77 M
    tape placements), while log interpolation is 5-30 % wrong because arrival is
    not multiplicative over these horizons. The gate prices the exact-price
    series, which the same hold-out puts at 25.71 % worst case, so the
    at-or-below figure understated the error for the cells actually used.
    ``P_FILL_INTERPOLATION_ERROR`` carries that bound.

    Nothing is extrapolated. A horizon below the smallest measured cell, or
    outside the measured tick/flow grid, returns ``None`` (missing data), so the
    maker gate refuses instead of assuming a probability. ``p_fill`` also
    returns ``None`` above the largest measured cell; ``clamped_p_fill`` pins
    that case to the measured maximum and reports the horizon it priced, which
    is a lower bound on the true probability and is what a requote gap wider
    than the table's support gets.
    """

    cells: Tuple[Tuple[float, float, float, float], ...]
    placements: int = 0

    #: Worst held-out error of the linear-in-horizon interpolation, now measured
    #: on the exact-price series the gate prices: 25.71 % over 48 withholds of
    #: the shipped table.  The cells the gate uses most (min order 5 shares,
    #: tick 0.01) stay inside 7.9 %; the worst triple is a 200-share cell at
    #: tick 0.001 whose counts are tiny.  The earlier 5.31 % was measured on the
    #: looser at-or-below series and understated the error for these cells.
    P_FILL_INTERPOLATION_ERROR = 0.2571

    def _cell_at(self, horizon: float, tick_value: float, flow: float) -> Optional[float]:
        rows = [
            c for c in self.cells
            if c[0] == horizon and abs(c[1] - tick_value) < 1e-12 and c[2] >= flow
        ]
        if not rows:
            return None
        return min(rows, key=lambda c: c[2])[3]

    def p_fill(
        self,
        horizon_s: float,
        tick: float,
        min_flow_shares: float,
    ) -> Optional[float]:
        """Measured P(fill) for this horizon, tick and required fill size.

        The horizon and tick must be measured exactly (both are discrete
        operational quantities, not continuous parameters).  The required flow
        is rounded up to the smallest measured bucket that covers it, so a
        minimum order between two measured cells is priced at the harder cell.
        """
        horizon = float(horizon_s)
        tick_value = float(tick)
        flow = max(0.0, float(min_flow_shares))
        exact = self._cell_at(horizon, tick_value, flow)
        if exact is not None:
            return exact
        if flow <= 0.0:
            return None
        measured = sorted({
            c[0] for c in self.cells
            if abs(c[1] - tick_value) < 1e-12 and c[2] >= flow
        })
        if len(measured) < 2 or horizon < measured[0] or horizon > measured[-1]:
            # Outside the measured support: the required flow is above every
            # cell, or the horizon is below the smallest / above the largest
            # measured lifetime. Extrapolating would invent a probability.
            return None
        lower = max(h for h in measured if h <= horizon)
        upper = min(h for h in measured if h >= horizon)
        low_value = self._cell_at(lower, tick_value, flow)
        high_value = self._cell_at(upper, tick_value, flow)
        if low_value is None or high_value is None or upper <= lower:
            return None
        weight = (horizon - lower) / (upper - lower)
        return low_value + (high_value - low_value) * weight

    def supported_horizons(self, tick: float, min_flow_shares: float) -> list[float]:
        """Measured horizons that cover this tick and this required fill size."""
        flow = max(0.0, float(min_flow_shares))
        return sorted({
            c[0] for c in self.cells
            if abs(c[1] - float(tick)) < 1e-12 and c[2] >= flow
        })

    def horizon_bound(self, tick: float, min_flow_shares: float) -> Optional[float]:
        """Longest measurable requote lifetime, or ``None`` without support."""
        measured = self.supported_horizons(tick, min_flow_shares)
        return measured[-1] if measured else None

    def clamped_p_fill(
        self, horizon_s: float, tick: float, min_flow_shares: float
    ) -> Optional[Tuple[float, float]]:
        """``(p_fill, priced_horizon_s)``, pinning an over-long life to support.

        A requote lifetime longer than the longest measured horizon has no
        probability, but refusing to quote is not the only honest answer: the
        probability at the longest measured horizon is a **lower bound** for it
        (arrival is non-negative over the extra time).  So a caller whose sweep
        gap exceeds the table is priced at the measured maximum and told which
        horizon that was, rather than silently dropped.

        The clamp is one-directional.  A horizon *below* the smallest measured
        cell is still refused: pricing it at the shortest cell would overstate
        the probability, and overstating a fill is how a fabricated edge gets
        into the book.
        """
        exact = self.p_fill(horizon_s, tick, min_flow_shares)
        if exact is not None:
            return exact, float(horizon_s)
        bound = self.horizon_bound(tick, min_flow_shares)
        if bound is None or float(horizon_s) <= bound:
            return None
        value = self._cell_at(bound, float(tick), max(0.0, float(min_flow_shares)))
        if value is None:
            return None
        return value, bound


_QUOTE_FILL_HORIZON: Optional[QuoteFillHorizon] = None
_QUOTE_FILL_HORIZON_TRIED = False


def _default_quote_fill_horizon() -> Optional[QuoteFillHorizon]:
    """The measured horizon fill table, or ``None`` when it is unusable."""
    global _QUOTE_FILL_HORIZON, _QUOTE_FILL_HORIZON_TRIED
    if not _QUOTE_FILL_HORIZON_TRIED:
        _QUOTE_FILL_HORIZON_TRIED = True
        try:
            _QUOTE_FILL_HORIZON = load_quote_fill_horizon()
        except Exception as exc:  # noqa: BLE001 - a missing measurement is data, not a crash
            logger.warning(
                f"CTF maker parity disabled: horizon fill measurement unavailable ({exc}). "
                "Resting both legs without a horizon-matched fill probability would "
                "claim an edge that only materialises when both legs fill."
            )
            _QUOTE_FILL_HORIZON = None
    return _QUOTE_FILL_HORIZON


def load_quote_fill_horizon(path: Optional[Path] = None) -> QuoteFillHorizon:
    """Load and validate the measured horizon fill table; raise if unusable."""
    model_path = Path(path) if path is not None else DEFAULT_QUOTE_FILL_HORIZON_PATH
    with open(model_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    cells = []
    for row in payload.get("cells") or []:
        # Price the event the paper execution model realises. It books a fill
        # only when a print lands at the quote's own price
        # (`reward.measured_fill_shares`), which the tape measures as 10.75 % of
        # the at-or-below count: a gate charged the crossing probability would
        # be pricing nine fills for every one it can get.
        p = row.get("p_fill_exact")
        if p is None:
            continue
        cells.append(
            (
                float(row["horizon_s"]),
                float(row["tick"]),
                float(row["min_flow_shares"]),
                float(p),
            )
        )
    if not cells:
        raise ValueError(f"horizon fill table at {model_path} has no measured cells")
    placements = int((payload.get("coverage") or {}).get("placements") or 0)
    logger.info(
        f"Loaded horizon fill measurement {model_path} "
        f"({len(cells)} cells, {placements:,} tape placements)"
    )
    return QuoteFillHorizon(cells=tuple(cells), placements=placements)

_UNMEASURED_HORIZON_LOGGED: set[tuple[float, float, float]] = set()
_HORIZON_CLAMP_LOGGED: set[tuple[float, float, float]] = set()


def _warn_horizon_clamped(
    model: QuoteFillHorizon,
    requested_s: float,
    priced_s: float,
    tick: float,
    min_order: float,
) -> None:
    """Log once per (gap, priced, tick, order) that a long gap was pinned.

    The sweep gap is derived from the tick cadence, so a tick that overruns its
    interval silently widens every quote's requote lifetime past the measured
    table.  Pinning the price to the measured maximum keeps the quote alive
    instead of turning the pillar off, but it must never be silent: the priced
    probability is a lower bound over the *requested* lifetime, and the bound is
    the model's own held-out interpolation error.
    """
    key = (float(requested_s), float(priced_s), float(tick), float(min_order))
    if key in _HORIZON_CLAMP_LOGGED:
        return
    _HORIZON_CLAMP_LOGGED.add(key)
    logger.warning(
        f"CTF maker quote priced at the measured maximum {float(priced_s):.0f}s "
        f"horizon, not the {float(requested_s):.0f}s requote gap: the table covers "
        f"{model.supported_horizons(tick, min_order)} s and extrapolating past it "
        "would invent a fill probability. The priced p_fill is a lower bound for the "
        "quote's true lifetime (arrival over the extra "
        f"{float(requested_s) - float(priced_s):.0f}s is unmeasured); the model's "
        f"worst held-out interpolation error measured on the exact-price series "
        f"the gate prices is "
        f"{QuoteFillHorizon.P_FILL_INTERPOLATION_ERROR:.2%} (the looser "
        "at-or-below series measured 5.31 % and understated it)."
    )


def _warn_unmeasured_horizon(
    model: QuoteFillHorizon, horizon_s: float, tick: float, min_order: float
) -> None:
    """Log once per (horizon, tick, order) that the table cannot price.

    The engine derives ``requote_horizon_s`` from the scan cadence, so a scan
    gap longer than the longest measured horizon makes the pillar refuse every
    quote.  That refusal is missing data, not a zero probability, and it must be
    visible: otherwise the sweep cadence silently turns the pillar off.
    """
    key = (float(horizon_s), float(tick), float(min_order))
    if key in _UNMEASURED_HORIZON_LOGGED:
        return
    _UNMEASURED_HORIZON_LOGGED.add(key)
    measured = sorted({cell[0] for cell in model.cells})
    logger.warning(
        f"CTF maker parity refused at a {float(horizon_s):.1f}s requote horizon with "
        f"tick {float(tick):g} and min order {float(min_order):g} shares: the measured "
        f"table covers horizons {measured} s. A scan gap wider than the measured "
        "horizon support is missing data, not a fill probability of zero."
    )
