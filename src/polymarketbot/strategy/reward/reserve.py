"""Endogenous cash reserve for unmerged inventory, from a measured fit.

The reserve is the sum of (a) the measured Polygon merge-gas cost for the
current position count, (b) a tail term for one instantaneous sweep of the
largest exposure, and (c) a diversification term that scales with the exposure
vector's L2 norm. Every coefficient of those three terms is a measurement, not
a constant:

* ``jump_shock_factor`` - the measured upper quantile of the mid-price jump
  over the holding horizon (``tools/fit_reserve.py``, ``1 - alpha`` with the
  same ``alpha`` the de-skew fit states).
* ``diversification_weight`` - ``1 -`` the measured average pairwise
  correlation of held-market horizon shocks.
* ``base_gas_actions`` / ``per_market_gas_actions`` - the measured expected
  number of merges over one holding horizon, at portfolio and per-market
  level.
* ``gas_floor_usd`` - the measured dollar cost of one merge at the on-chain gas
  price.

:func:`calculate_endogenous_reserve` has no numeric default: it resolves the
calibration from ``data/derived/reserve_calibration.json`` and raises when that
fit is absent, because reserving a guessed amount would either strand cash or
fail a merge call.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from polymarketbot.logs import get_logger

log = get_logger("rewards_allocator")

RESERVE_CALIBRATION_ARTIFACT = "data/derived/reserve_calibration.json"


@dataclass(frozen=True)
class ReserveCalibration:
    """Fitted coefficients of the endogenous reserve. Every field is required.

    ``tools/fit_reserve.py`` computes them from the recorded CLOB tape and the
    measured on-chain gas price; the artifact records the window, sample size,
    definition and source of each.
    """

    jump_shock_factor: float
    diversification_weight: float
    base_gas_actions: float
    per_market_gas_actions: float
    gas_floor_usd: float

    def __post_init__(self) -> None:
        for name in (
            "jump_shock_factor",
            "diversification_weight",
            "base_gas_actions",
            "per_market_gas_actions",
            "gas_floor_usd",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"reserve calibration {name} must be finite and non-negative")
        if self.diversification_weight <= 0.0:
            raise ValueError("reserve diversification weight must be positive")
        if self.base_gas_actions <= 0.0 or self.per_market_gas_actions <= 0.0:
            raise ValueError("reserve gas action counts must be positive")
        if self.gas_floor_usd <= 0.0:
            raise ValueError("reserve gas floor must be the measured positive merge cost")


_CALIBRATION_FIELDS: tuple[str, ...] = tuple(
    field.name for field in fields(ReserveCalibration)
)


def load_reserve_calibration(
    path: str | Path = RESERVE_CALIBRATION_ARTIFACT,
) -> ReserveCalibration | None:
    """The fitted reserve coefficients, or ``None`` when no artifact exists."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"reserve calibration {target} unreadable: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"reserve calibration {target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"reserve calibration {target} must contain a JSON object")
    missing = [name for name in _CALIBRATION_FIELDS if payload.get(name) is None]
    if missing:
        raise ValueError(f"reserve calibration {target} is missing required fields {missing}")
    values: dict[str, float] = {}
    for name in _CALIBRATION_FIELDS:
        try:
            values[name] = float(payload[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"reserve calibration {target} field {name}={payload[name]!r} is not numeric"
            ) from exc
    return ReserveCalibration(**values)


def calculate_endogenous_reserve(
    allocations: list[dict[str, Any]],
    bankroll_usd: float,
    base_gas_buffer_usd: float | None = None,
    jump_shock_factor: float | None = None,
    diversification_weight: float | None = None,
    ctf_client: Any | None = None,
    *,
    calibration: ReserveCalibration | None = None,
) -> float:
    """Endogenous cash reserve for unmerged inventory, from measured inputs.

    ``jump_shock_factor`` / ``diversification_weight`` are optional overrides
    for callers that measured them this cycle; ``None`` resolves the fitted
    artifact. There is no fallback number: without either an override or the
    fit, the function refuses to size the reserve.
    """
    calib = calibration if calibration is not None else load_reserve_calibration()
    if calib is None:
        raise ValueError(
            f"no reserve calibration at {RESERVE_CALIBRATION_ARTIFACT}; "
            "run polymarketbot.tools.fit_reserve before sizing a reserve"
        )
    jump = float(calib.jump_shock_factor if jump_shock_factor is None else jump_shock_factor)
    diversification = float(
        calib.diversification_weight
        if diversification_weight is None
        else diversification_weight
    )
    if jump < 0.0 or diversification <= 0.0:
        raise ValueError("reserve coefficients must be filled from a measurement")

    if bankroll_usd <= 0.0:
        return 0.0

    gas_cost_merge: float | None = None
    if base_gas_buffer_usd is not None:
        dynamic_gas_buffer = float(base_gas_buffer_usd)
    else:
        try:
            if ctf_client is not None and hasattr(ctf_client, "estimate_merge_cost_usd"):
                gas_cost_merge = ctf_client.estimate_merge_cost_usd()
            else:
                from polymarketbot.strategy.onchain.ctf import estimate_merge_cost_usd

                gas_cost_merge = estimate_merge_cost_usd()
        except (ImportError, RuntimeError, OSError, ValueError):
            # An unmeasured gas cost is not zero and not a guess: fall back to
            # the fitted floor, which *is* a measured merge cost.
            log.warning(
                "endogenous reserve: live merge gas unmeasured; using the fitted gas floor"
            )
        if gas_cost_merge is None:
            gas_cost_merge = calib.gas_floor_usd
        dynamic_gas_buffer = max(
            calib.gas_floor_usd, gas_cost_merge * calib.base_gas_actions
        )

    per_market_buffer = max(
        calib.gas_floor_usd,
        (calib.gas_floor_usd if gas_cost_merge is None else gas_cost_merge)
        * calib.per_market_gas_actions,
    )

    if not allocations:
        return round(dynamic_gas_buffer, 2)

    h_values = []
    total_gas_buffer = dynamic_gas_buffer

    for a in allocations:
        # A missing capital or mid is unknown, never a default exposure.
        cap = a.get("capital", a.get("allocated_capital"))
        mid = a.get("mid", a.get("mid_price"))
        if cap is None or mid is None:
            continue
        cap = float(cap)
        mid = float(mid)
        if not 0.0 < mid < 1.0 or cap <= 0.0:
            continue
        h_i = cap * max(mid, 1.0 - mid)
        h_values.append(h_i)
        total_gas_buffer += per_market_buffer

    if not h_values:
        return round(total_gas_buffer, 2)

    # Unified portfolio exposure:
    # 1. stochastic replenishment demand scales as sqrt(sum(h_i^2)), reduced by
    #    the measured diversification weight (1 - average shock correlation);
    # 2. jump shock: one instantaneous unhedged fill on the largest exposure,
    #    sized by the measured horizon-jump quantile;
    # 3. Polygon CTF merge gas: the measured merge cost times the measured
    #    expected action counts.
    diversified_stochastic_demand = (
        math.sqrt(sum(h**2 for h in h_values)) * diversification
    )
    tail_single_sweep = max(h_values) * jump
    optimal_reserve = diversified_stochastic_demand + tail_single_sweep + total_gas_buffer

    # Physical bounds:
    # Floor: the measured gas buffer, so merge calls can always be paid for.
    # Ceiling: every resting position filling at once needs at most sum(h_i);
    # reserving beyond that is pure cash drag.
    max_simultaneous_demand = sum(h_values)
    bounded_reserve = max(
        total_gas_buffer, min(max_simultaneous_demand, optimal_reserve)
    )
    if optimal_reserve > max_simultaneous_demand:
        log.info(
            f"endogenous reserve capped by total exposure: "
            f"optimal={optimal_reserve:.2f} > sum(h_i)={max_simultaneous_demand:.2f} "
            f"(bankroll={bankroll_usd:.2f})"
        )
    return round(bounded_reserve, 2)
