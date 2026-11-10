"""Measured quote-fill and adverse-markout tables, shared by the live reward paths.

Both artifacts are built from the recorded L2 tape by
``python -m polymarketbot.tools.ctf_quote_fill_horizon`` (add ``--markout`` for
the markout table):

* ``data/derived/ctf_quote_fill_horizon.json`` - P(a resting buy at
  ``best_bid + tick`` fills within a horizon), keyed by ``(horizon, tick,
  min_flow_shares)``.
* ``data/derived/ctf_maker_markout.json`` - measured adverse drift of one filled
  leg, keyed by ``(horizon, tick)``.

The names carry the ``ctf_`` prefix because the complete-set gate measured them
first; the reward harvester and the opportunity scanner price against the same
tables so the three cannot disagree about how often a resting quote is filled or
what a fill drifts.  Nothing here invents a probability: a horizon outside the
measured support returns ``None`` and the caller refuses the market.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("rewards.fill_measurement")

# src/polymarketbot/strategy/reward/measurement.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_QUOTE_FILL_HORIZON_PATH = (
    _REPO_ROOT / "data" / "derived" / "ctf_quote_fill_horizon.json"
)
DEFAULT_MAKER_MARKOUT_PATH = (
    _REPO_ROOT / "data" / "derived" / "ctf_maker_markout.json"
)


@dataclass(frozen=True)
class QuoteFillHorizon:
    """Measured P(a resting buy at ``best_bid + tick`` fills) by horizon.

    ``cells`` holds ``(horizon_s, tick, min_flow_shares, p_fill)`` measured on
    the recorded L2 tape: a placement every engine tick, a fill when a SELL
    print crosses the quote within the horizon with enough size to meet the
    venue minimum order.

    A horizon between two measured cells is linearly interpolated in time: the
    held-out error of that interpolation on the exact-price series the gate
    prices is 25.71 % worst case (``P_FILL_INTERPOLATION_ERROR``).  Nothing is
    extrapolated - a horizon below the smallest measured cell, or outside the
    measured tick/flow grid, returns ``None`` (missing data).
    """

    cells: Tuple[Tuple[float, float, float, float], ...]
    placements: int = 0

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
        """Measured P(fill) for this horizon, tick and required fill size."""
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

        A requote lifetime longer than the longest measured horizon is priced at
        the measured maximum, which is a **lower bound** for the requested
        lifetime; the caller is told which horizon it was priced at.  A horizon
        below the smallest measured cell is still refused.
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


@dataclass(frozen=True)
class MakerMarkout:
    """Measured adverse markout of a single filled leg, by ``(horizon, tick)``.

    ``cells`` holds ``(horizon_s, tick, usd_per_share, fills)``: for every
    placement that filled, the best bid at the fill minus the best bid one
    horizon later.  A positive value is the per-share loss the drift inflicts on
    a leg that fills alone.
    """

    cells: Tuple[Tuple[float, float, float, int], ...] = ()

    def markout(self, horizon_s: float, tick: float) -> Optional[float]:
        """Measured adverse drift per share, or ``None`` when unmeasured.

        A horizon between two measured cells is charged the **larger** of the
        two measured drifts: the drift is not monotone in time, so a straight
        line would invent values, while the worse neighbour can only overstate
        the cost the gate must clear.
        """
        horizon = float(horizon_s)
        tick_value = float(tick)
        measured = sorted(
            (cell_horizon, value)
            for cell_horizon, cell_tick, value, fills in self.cells
            if abs(cell_tick - tick_value) < 1e-12 and fills > 0
        )
        if not measured:
            return None
        for cell_horizon, value in measured:
            if abs(cell_horizon - horizon) < 1e-9:
                return value
        if horizon < measured[0][0] or horizon > measured[-1][0]:
            return None
        lower = max(h for h, _ in measured if h <= horizon)
        upper = min(h for h, _ in measured if h >= horizon)
        return max(
            value for cell_horizon, value in measured if cell_horizon in (lower, upper)
        )

    def supported_horizons(self, tick: float) -> list[float]:
        """Measured horizons that carry a markout for this tick."""
        return sorted(
            cell_horizon
            for cell_horizon, cell_tick, _value, fills in self.cells
            if abs(cell_tick - float(tick)) < 1e-12 and fills > 0
        )


def load_quote_fill_horizon(path: Optional[Path] = None) -> QuoteFillHorizon:
    """Load and validate the measured horizon fill table; raise if unusable."""
    model_path = Path(path) if path is not None else DEFAULT_QUOTE_FILL_HORIZON_PATH
    with open(model_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    cells = []
    for row in payload.get("cells") or []:
        # Price the event the paper execution model realises: a fill is booked
        # only when a print lands at the quote's own price.
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


def load_maker_markout(path: Optional[Path] = None) -> MakerMarkout:
    """Load the measured single-leg markout; raise when it carries no cells."""
    model_path = Path(path) if path is not None else DEFAULT_MAKER_MARKOUT_PATH
    with open(model_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    cells = []
    for row in payload.get("cells") or []:
        value = row.get("mean_markout_usd_per_share")
        fills = row.get("fills")
        if value is None or fills is None:
            continue
        if int(fills) <= 0:
            continue
        cells.append(
            (
                float(row["horizon_s"]),
                float(row["tick"]),
                float(value),
                int(fills),
            )
        )
    if not cells:
        raise ValueError(f"markout table at {model_path} has no measured cells")
    logger.info(
        f"Loaded single-leg markout measurement {model_path} "
        f"({len(cells)} cells, {sum(c[3] for c in cells):,} measured fills)"
    )
    return MakerMarkout(cells=tuple(cells))
