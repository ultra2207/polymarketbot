"""Measured market economics for the market-making engines.

Every field is a provisional default pending measurement; an absent
``data/derived/market_economics.json`` reproduces the historical constants.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Union

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

    Every field is a **provisional default pending measurement**. The values
    reproduce the constants that used to be hardcoded in
    :meth:`BaseMarketMakingEngine.__init__` so that a deployment without a
    measured ``data/derived/market_economics.json`` behaves exactly as before;
    they are not calibrated estimates. When the JSON file exists, its keys (the
    field names below) override the defaults.
    """

    quote_size: float = 20.0  # provisional default pending measurement
    spread_offset_cents: float = 0.50  # provisional default pending measurement
    daily_reward_rate: float = 20.0  # provisional default pending measurement
    rewards_max_spread_cents: float = 1.0  # provisional default pending measurement
    rewards_min_size: float = 20.0  # provisional default pending measurement
    gamma: float = 0.02  # A-S risk aversion; provisional default pending measurement
    q_soft_threshold: float = 15.0  # provisional default pending measurement
    q_hard_threshold: float = 100.0  # provisional default pending measurement
    max_holding_seconds: float = 300.0  # provisional default pending measurement
    ofi_threshold: float = 850.0  # provisional default pending measurement
    drift_threshold_cents: float = 1.5  # provisional default pending measurement
    daily_loss_limit_usd: float = 15.0  # provisional default pending measurement
    max_drawdown_usd: float = 30.0  # provisional default pending measurement
    days_to_resolution: float = 7.0  # provisional default pending measurement

    @classmethod
    def load(cls, path: Union[str, Path] = MARKET_ECONOMICS_PATH) -> "MarketEconomics":
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
            raise ValueError(f"{economics_path} must contain a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(f"{economics_path} has unknown keys: {unknown}")

        values: Dict[str, float] = {}
        for key, raw in payload.items():
            value = float(raw)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{economics_path} has non-finite {key}={raw!r}")
            values[key] = value
        logger.info(f"Loaded market economics from {economics_path}")
        return cls(**values)
