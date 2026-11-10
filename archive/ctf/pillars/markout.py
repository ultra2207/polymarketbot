"""Measured adverse markout of one filled CTF quote leg.

The single-leg cost the maker gate prices on top of the one-tick unwind: the
best bid at the fill minus the best bid one horizon later, measured on the L2
tape by ``polymarketbot.tools.ctf_quote_fill_horizon --markout``.  A horizon
without a measured cell has no markout, and the gate refuses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from polymarketbot.logs import get_logger

from .fill_model import _REPO_ROOT

logger = get_logger("paper.ctf")

# Measured adverse markout of one filled leg, built by
# ``polymarketbot.tools.ctf_quote_fill_horizon --markout`` from the same L2 tape
# as the fill table above.
DEFAULT_MAKER_MARKOUT_PATH = (
    _REPO_ROOT / "data" / "derived" / "ctf_maker_markout.json"
)


@dataclass(frozen=True)
class MakerMarkout:
    """Measured adverse markout of a single filled leg, by ``(horizon, tick)``.

    ``cells`` holds ``(horizon_s, tick, usd_per_share, fills)`` measured on the
    recorded L2 tape by ``polymarketbot.tools.ctf_quote_fill_horizon --markout``:
    for every placement that filled, the best bid at the fill minus the best bid
    one horizon later.  A positive value is the per-share loss the drift
    inflicts on a leg that fills alone -- the outcome the maker gate prices with
    weight ``p * (1 - p)``, i.e. ~3000x the ``p^2`` weight of the lock at the
    measured exact fill probabilities.

    Nothing is interpolated: the drift is not a linear function of time, so a
    horizon without a measured cell has no markout and the gate refuses.  The
    measurement is conditioned on fills that reach the venue minimum order,
    which is the only size every emitted quote must clear.
    """

    cells: Tuple[Tuple[float, float, float, int], ...] = ()

    def markout(self, horizon_s: float, tick: float) -> Optional[float]:
        """Measured adverse drift per share, or ``None`` when unmeasured.

        A horizon between two measured cells is charged the **larger** of the
        two measured drifts.  That is a measured number, not an interpolation:
        the drift is not monotone in time (measured 0.08c at 5 s, 0.25c at 30 s,
        0.12c at 300 s for tick 0.001), so a straight line through it would
        invent values, while taking the worse neighbour can only overstate the
        cost the gate must clear.  Below the smallest measured cell there is no
        cell to lean on and the answer is ``None`` (missing data, refuse); above
        the largest, the caller has already pinned the horizon to support
        (``QuoteFillHorizon.clamped_p_fill``), so a lookup there is out of
        contract rather than a value.
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


_MAKER_MARKOUT: Optional[MakerMarkout] = None
_MAKER_MARKOUT_TRIED = False


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


def _default_maker_markout() -> Optional[MakerMarkout]:
    """The measured single-leg markout, or ``None`` when it is unusable."""
    global _MAKER_MARKOUT, _MAKER_MARKOUT_TRIED
    if not _MAKER_MARKOUT_TRIED:
        _MAKER_MARKOUT_TRIED = True
        try:
            _MAKER_MARKOUT = load_maker_markout()
        except Exception as exc:  # noqa: BLE001 - a missing measurement is data, not a crash
            logger.warning(
                f"CTF maker parity disabled: single-leg markout measurement "
                f"unavailable ({exc}). Without it the one-leg branch is priced "
                "against a one-tick unwind alone, which is the error the 12:53 "
                "window ran for 30 minutes at 0 fills."
            )
            _MAKER_MARKOUT = None
    return _MAKER_MARKOUT

_UNMEASURED_MARKOUT_LOGGED: set[tuple[float, float]] = set()


def _warn_unmeasured_markout(
    model: Optional[MakerMarkout], horizon_s: float, tick: float
) -> None:
    """Log once per (horizon, tick) that no markout was measured for this quote."""
    key = (float(horizon_s), float(tick))
    if key in _UNMEASURED_MARKOUT_LOGGED:
        return
    _UNMEASURED_MARKOUT_LOGGED.add(key)
    measured = sorted(model.supported_horizons(tick)) if model is not None else []
    logger.warning(
        f"CTF maker parity refused a {float(horizon_s):.0f}s quote at tick "
        f"{float(tick):g}: no single-leg markout is measured there (the markout "
        f"table covers horizons {measured} s at this tick). A re-quote that "
        "outlives its measured lifetime lands unwound, and pricing that as free "
        "is how a rebate-funded quote books a loss. Build the table with "
        "`python -m polymarketbot.tools.ctf_quote_fill_horizon --markout`."
    )
