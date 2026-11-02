"""Avellaneda-Stoikov dynamic inventory skewing for Polymarket Liquidity Rewards.

Optimized for binary prediction markets under the quadratic scoring rule:
- When inventory q = 0: quotes symmetrically at the 1.0c touch (mid - 0.5c, mid + 0.5c)
  to maximize quadratic score ((v - s)/v)^2 while capturing the 1.0c spread.
- When inventory q != 0: skews reservation price and quote distances to incentivize
  inventory flattening (widens the accumulating side, tightens the reducing side).
- Includes dynamic quote sizing and strict boundary clamping [0.01, 0.99].
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class SkewedQuote:
    """The computed bid and ask quotes for a market."""

    mid_price: float
    inventory: float  # Net position q (shares)
    bid_price: float
    ask_price: float
    bid_spread_cents: float
    ask_spread_cents: float
    bid_size: float
    ask_size: float
    reservation_price: float
    is_bid_active: bool = True
    is_ask_active: bool = True


@dataclass
class AvellanedaStoikovModel:
    """Avellaneda-Stoikov market making model with prediction market adaptation.

    Attributes:
        gamma: Risk aversion coefficient (> 0). Higher gamma causes sharper skew.
        volatility: Daily price volatility (e.g. 0.15 to 0.50).
        max_inventory: Maximum net position in shares before cutting off quotes.
        touch_spread_cents: Full spread at touch when q=0 (default 1.0 cent).
        tick_size: Minimum price increment (0.01 for 1c, or 0.001 for 0.1c).
        skew_coefficient: Tuning multiplier for inventory reservation shift.
    """

    gamma: float = 0.01
    volatility: float = 0.20
    max_inventory: float = 2000.0
    touch_spread_cents: float = 1.0
    tick_size: float = 0.01
    skew_coefficient: float = 1.0

    def calculate_reservation_price(
        self,
        mid_price: float,
        inventory_q: float,
        time_horizon: float = 1.0,  # 1 day horizon
    ) -> float:
        """Calculate the Avellaneda-Stoikov reservation (indifference) price r(s, q).

        Classical continuous formula: r(s, q) = s - q * gamma * sigma^2 * (T - t)
        Prediction market adaptation clamps r to [0.01, 0.99].
        """
        # Scaled inventory ratio relative to max_inventory
        variance = self.volatility**2
        drift_penalty = inventory_q * self.gamma * variance * time_horizon * self.skew_coefficient

        # Convert to price adjustment (clamped so reservation price stays valid)
        reservation = mid_price - drift_penalty
        return max(0.01, min(0.99, reservation))

    def compute_quotes(
        self,
        mid_price: float,
        inventory_q: float,
        base_quote_size: float,
        max_spread_cents: float,
        min_size: float = 0.0,
    ) -> SkewedQuote:
        """Compute optimal bid and ask quotes given current mid and inventory.

        1. When q == 0:
           Quotes sit symmetrically at touch:
           spread = 1.0c (bid_spread = 0.5c, ask_spread = 0.5c).
           This achieves maximum quadratic reward score while capturing spread.

        2. When q > 0 (long inventory):
           Maker holds excess YES shares.
           - Widen accumulating side (bid): bid drops lower, avoiding more buys.
           - Tighten reducing side (ask): ask moves closer to mid, getting filled
             first by taker volume to liquidate back towards q = 0.
           - If q >= max_inventory: bid is deactivated (size = 0).

        3. When q < 0 (short inventory / excess NO shares):
           Maker is short YES.
           - Tighten reducing side (bid): bid moves closer to mid to buy back.
           - Widen accumulating side (ask): ask moves higher to avoid selling.
           - If q <= -max_inventory: ask is deactivated (size = 0).
        """
        mid_price = max(0.01, min(0.99, mid_price))
        half_touch = self.touch_spread_cents / 2.0  # 0.5 cents

        # Reservation price
        res_price = self.calculate_reservation_price(mid_price, inventory_q)

        # Inventory fraction [-1.0, 1.0]
        inv_ratio = max(-1.0, min(1.0, inventory_q / max(1.0, self.max_inventory)))

        # Dynamic skew in cents: proportional to inventory ratio and max allowable spread
        max_skew_cents = max(0.0, max_spread_cents - half_touch - 0.1)
        # Skew magnitude using smooth tanh response
        skew_cents = math.tanh(inv_ratio * 2.5) * max_skew_cents

        if abs(inventory_q) < 1e-6:
            # Case 1: Neutral inventory (q = 0) -> Exact symmetric 1.0c touch
            bid_spread_cents = half_touch
            ask_spread_cents = half_touch
            bid_size = base_quote_size
            ask_size = base_quote_size
            bid_active = True
            ask_active = True
        elif inventory_q > 0:
            # Case 2: Long YES (q > 0) -> Incentivize flattening
            # Widen accumulating side (bid), tighten reducing side (ask)
            bid_spread_cents = half_touch + skew_cents
            ask_spread_cents = max(0.1, half_touch - (skew_cents * 0.8))

            # Sizing skew: scale down accumulating side, scale up reducing side
            bid_size = max(0.0, base_quote_size * (1.0 - inv_ratio))
            ask_size = min(base_quote_size * 2.0, base_quote_size * (1.0 + inv_ratio * 0.5))

            bid_active = (inventory_q < self.max_inventory) and (bid_size >= min_size)
            ask_active = ask_size >= min_size
        else:
            # Case 3: Short YES (q < 0) -> Incentivize flattening
            # Tighten reducing side (bid), widen accumulating side (ask)
            abs_skew = abs(skew_cents)
            bid_spread_cents = max(0.1, half_touch - (abs_skew * 0.8))
            ask_spread_cents = half_touch + abs_skew

            bid_size = min(base_quote_size * 2.0, base_quote_size * (1.0 + abs(inv_ratio) * 0.5))
            ask_size = max(0.0, base_quote_size * (1.0 - abs(inv_ratio)))

            bid_active = bid_size >= min_size
            ask_active = (abs(inventory_q) < self.max_inventory) and (ask_size >= min_size)

        # Compute price levels
        raw_bid = mid_price - (bid_spread_cents / 100.0)
        raw_ask = mid_price + (ask_spread_cents / 100.0)

        # Snap to tick boundaries: bid floors downward, ask ceils upward
        bid_price = math.floor((raw_bid / self.tick_size) + 1e-9) * self.tick_size
        ask_price = math.ceil((raw_ask / self.tick_size) - 1e-9) * self.tick_size

        # Clamp to bounds and enforce bid < ask
        bid_price = max(0.01, min(0.98, round(bid_price, 4)))
        ask_price = max(0.02, min(0.99, round(ask_price, 4)))
        if bid_price >= ask_price:
            ask_price = min(0.99, round(bid_price + self.tick_size, 4))

        # Recalculate actual effective spreads after tick snapping
        effective_bid_spread = round((mid_price - bid_price) * 100.0, 2)
        effective_ask_spread = round((ask_price - mid_price) * 100.0, 2)

        return SkewedQuote(
            mid_price=round(mid_price, 4),
            inventory=round(inventory_q, 2),
            bid_price=round(bid_price, 4),
            ask_price=round(ask_price, 4),
            bid_spread_cents=max(0.0, effective_bid_spread),
            ask_spread_cents=max(0.0, effective_ask_spread),
            bid_size=round(bid_size, 2) if bid_active else 0.0,
            ask_size=round(ask_size, 2) if ask_active else 0.0,
            reservation_price=round(res_price, 4),
            is_bid_active=bid_active,
            is_ask_active=ask_active,
        )
