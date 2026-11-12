"""Market and token configuration models shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TokenConfig:
    token_id: str
    outcome: str
    price: float = 0.5


@dataclass
class MarketConfig:
    condition_id: str
    question: str
    rate_per_day: float
    rewards_min_size: float
    rewards_max_spread: float  # in cents
    tokens: list[TokenConfig] = field(default_factory=list)
    event_id: str | None = None  # Gamma event grouping for correlated-event caps
