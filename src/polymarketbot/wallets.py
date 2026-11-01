"""Wallet archetype classification from PnL decomposition.

The Data API's `/v2/user-stats` reports all-time PnL split by source:

    realized_market_pnl   directional trading
    realized_lp_pnl       liquidity rewards
    realized_combo_pnl    combos
    maker_rebate          resting-order rebates
    taker_rebate          taker rebates
    fees_refunded         refunded taker fees

No public source reports queue position or individual orders, so this is the
cheapest honest signal for *what kind* of trader a wallet is. It is a
classification of outcomes, not of behaviour: a wallet can be a maker and still
lose money, and the label says nothing about replicability.
"""

from __future__ import annotations

# A rebate is considered material when it is at least this share of the
# absolute directional PnL. Chosen conservatively; revisit with data.
REBATE_MATERIALITY = 0.10


def classify_archetype(row: dict) -> str:
    """Return one of: liquidity_provider, maker, combo, directional, unknown."""
    market = abs(row.get("realized_market_pnl") or 0.0)
    rebate = (row.get("maker_rebate") or 0.0) + (row.get("taker_rebate") or 0.0)
    lp = row.get("realized_lp_pnl") or 0.0
    combo = abs(row.get("realized_combo_pnl") or 0.0)

    if lp > 0 and lp >= market:
        return "liquidity_provider"
    if rebate > 0 and rebate >= REBATE_MATERIALITY * max(market, 1.0):
        return "maker"
    if combo > max(market, rebate, 1.0):
        return "combo"
    if market > 0:
        return "directional"
    return "unknown"
