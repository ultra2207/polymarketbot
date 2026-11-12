"""Measured market economics for the market-making engines.

Absent ``data/derived/market_economics.json`` yields the field defaults. A
field whose default is ``None`` is unmeasured: its consumer must refuse (skip
sizing / quoting) instead of trading on a provisional number.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path

from polymarketbot.logs import get_logger

logger = get_logger("trading.base")


# Kept for import compatibility. There is deliberately no fabricated price:
# a missing book is represented by ``None`` and every quoting path must skip
# rather than trade against an invented 0.49/0.51 mark.

MARKET_ECONOMICS_PATH: Path = Path("data/derived/market_economics.json")
"""Measured market economics. Absent file means the provisional defaults apply."""


@dataclass(frozen=True)
class MarketEconomics:
    """Economic parameters for a market-making engine.

    Most fields are **provisional defaults pending measurement**: they
    reproduce the constants that used to be hardcoded in
    :meth:`BaseMarketMakingEngine.__init__`, so they are not calibrated
    estimates. Fields typed ``float | None`` default to ``None`` — unmeasured —
    and their consumers refuse rather than trade on a substituted number. When
    the JSON file exists, its keys (the field names below) override the
    defaults.
    """

    quote_size: float = 20.0  # provisional default pending measurement
    # ``None`` means unmeasured: no measured quote distance exists, so the
    # engine refuses to quote rather than fade the book by 0.50c it never
    # measured. The live supervisor supplies the measured value.
    spread_offset_cents: float | None = None
    daily_reward_rate: float = 20.0  # provisional default pending measurement
    rewards_max_spread_cents: float = 1.0  # provisional default pending measurement
    rewards_min_size: float = 20.0  # provisional default pending measurement
    gamma: float = 0.02  # A-S risk aversion; provisional default pending measurement
    max_holding_seconds: float = 300.0  # provisional default pending measurement
    # ``None`` means unmeasured: the microstructure gate derives both thresholds
    # from the live book and realised volatility instead of a provisional number.
    ofi_threshold: float | None = None
    drift_threshold_cents: float | None = None
    daily_loss_limit_usd: float = 15.0  # provisional default pending measurement
    max_drawdown_usd: float = 30.0  # provisional default pending measurement
    days_to_resolution: float = 7.0  # provisional default pending measurement

    @classmethod
    def load(cls, path: str | Path = MARKET_ECONOMICS_PATH) -> MarketEconomics:
        """Load measured economics from ``path``, or fall back to the defaults.

        Unknown keys and non-finite values raise ``ValueError`` so a malformed
        measurement file fails closed instead of silently trading on a typo.
        """
        economics_path = Path(path)
        if not economics_path.exists():
            logger.info(
                f"No market economics at {economics_path}; using provisional defaults"
            )
            return cls()

        payload = json.loads(economics_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"{economics_path} must contain a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(f"{economics_path} has unknown keys: {unknown}")

        values: dict[str, float | None] = {}
        for key, raw in payload.items():
            if raw is None:
                # An explicit ``null`` means "unmeasured": the gate must derive
                # its threshold at runtime rather than read a provisional one.
                values[key] = None
                continue
            value = float(raw)
            if math.isnan(value) or value in (float("inf"), float("-inf")):
                raise ValueError(f"{economics_path} has non-finite {key}={raw!r}")
            values[key] = value
        logger.info(f"Loaded market economics from {economics_path}")
        return cls(**values)
