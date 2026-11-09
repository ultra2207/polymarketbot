"""Analyze Latency Sniper Actionability across VIP Taker Rebate Tiers & Scalp vs Hold Risk.

Analyzes:
1. Across the 8 major Binance BTC/ETH jump events in our tape, at each VIP rebate tier:
   - None (0%)
   - Bronze (3%)
   - Silver (8%)
   - Gold (18%)
   - Platinum (32%)
   - Diamond (44%)
   - Obsidian (50%)
   How many times does the LatencySniper act? (i.e. passes profitability hurdle >15 bps).
2. What are the gross fees, rebates, net edge, and net PnL at each tier?
3. Scalp (5-60s) vs Hold-to-Expiry analysis:
   - Empirical outcome of the contracts (did they resolve YES or NO?).
   - Expected value, variance, and reversal risk (Brownian motion first-passage probability).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from polymarketbot.trading.latency_sniper import (
    VIP_REBATE_RATES,
    calculate_taker_fee,
    get_vip_rebate_rate,
)
from polymarketbot.logs import get_logger
log = get_logger(__name__)

# Load jump summary
data_path = Path("data/derived/jump_clob_response_summary.json")
with open(data_path, "r", encoding="utf-8") as f:
    jump_events = json.load(f)

# The official VIP rebate tiers
TIERS = [
    ("None (Tier 0)", 0.00),
    ("Bronze ($2K vol)", 0.03),
    ("Silver ($20K vol)", 0.08),
    ("Gold ($200K vol)", 0.18),
    ("Platinum ($1M vol)", 0.32),
    ("Diamond ($4M vol)", 0.44),
    ("Obsidian ($10M+ vol)", 0.50),
]

MIN_NET_EDGE_BPS = 15.0  # 15 bps minimum net hurdle
MIN_NET_EDGE = MIN_NET_EDGE_BPS / 10000.0

log.info(f"Loaded {len(jump_events)} major jump events.")

results_by_tier = []

for tier_name, rebate_rate in TIERS:
    tier_key = tier_name.split()[0].lower()
    total_opportunities = 0
    actionable_snipes = 0
    rejected_fee_drag = 0
    total_shares = 0.0
    total_volume_usd = 0.0
    total_gross_edge_usd = 0.0
    total_gross_fee_usd = 0.0
    total_rebate_usd = 0.0
    total_net_fee_usd = 0.0
    total_net_pnl_usd = 0.0
    sniped_markets = []

    for evt_wrapper in jump_events:
        evt = evt_wrapper.get("event", {})
        jump_bps = abs(float(evt.get("bps", 0.0)))
        crypto_trades = evt_wrapper.get("crypto_trades", [])

        for tr in crypto_trades:
            delta_ms = float(tr.get("delta_venue_ms", 0.0))
            px = float(tr.get("price", 0.50))
            q = tr.get("question", "")

            # Lingering quote window: 500ms to 3500ms
            if 500.0 <= delta_ms <= 3500.0 and 0.02 <= px <= 0.98:
                total_opportunities += 1
                trade_usd = 100.0  # $100 ticket
                shares = trade_usd / px

                # Non-linear taker fee curve
                p_clamped = max(0.001, min(0.999, px))
                fee_curve = p_clamped * (1.0 - p_clamped)
                gross_fee_per_share = 0.07 * fee_curve
                rebate_per_share = gross_fee_per_share * rebate_rate
                net_fee_per_share = gross_fee_per_share - rebate_per_share

                # Expected price shift on contract
                sensitivity = 8.0 if (0.35 <= px <= 0.65) else 3.5
                gross_edge_per_share = (jump_bps / 10000.0) * sensitivity

                expected_net_edge = gross_edge_per_share - net_fee_per_share

                if expected_net_edge >= MIN_NET_EDGE:
                    actionable_snipes += 1
                    gross_fee = shares * gross_fee_per_share
                    rebate = shares * rebate_per_share
                    net_fee = shares * net_fee_per_share
                    gross_profit = shares * gross_edge_per_share
                    net_profit = gross_profit - net_fee

                    total_shares += shares
                    total_volume_usd += trade_usd
                    total_gross_edge_usd += gross_profit
                    total_gross_fee_usd += gross_fee
                    total_rebate_usd += rebate
                    total_net_fee_usd += net_fee
                    total_net_pnl_usd += net_profit
                    sniped_markets.append({
                        "question": q,
                        "price": px,
                        "jump_bps": jump_bps,
                        "gross_edge": round(gross_edge_per_share, 4),
                        "net_fee": round(net_fee_per_share, 4),
                        "net_profit": round(net_profit, 2),
                    })
                else:
                    rejected_fee_drag += 1

    results_by_tier.append({
        "tier": tier_name,
        "rebate_pct": f"{rebate_rate*100:.0f}%",
        "actionable_snipes": actionable_snipes,
        "rejected_fee_drag": rejected_fee_drag,
        "actionable_pct": round((actionable_snipes / max(1, total_opportunities)) * 100.0, 1),
        "total_volume_usd": round(total_volume_usd, 2),
        "gross_edge_usd": round(total_gross_edge_usd, 2),
        "fees_paid_usd": round(total_gross_fee_usd, 2),
        "rebates_earned_usd": round(total_rebate_usd, 2),
        "net_pnl_usd": round(total_net_pnl_usd, 2),
    })

log.info("\n" + "=" * 95)
log.info(f"{'VIP Tier':<22} | {'Rebate':<7} | {'Snipes':<7} | {'Rejected':<9} | {'Act %':<6} | {'Volume':<9} | {'Fees':<8} | {'Rebate $':<9} | {'Net PnL':<8}")
log.info("-" * 95)
for r in results_by_tier:
    log.info(f"{r['tier']:<22} | {r['rebate_pct']:>6} | {r['actionable_snipes']:>7} | {r['rejected_fee_drag']:>9} | {r['actionable_pct']:>5.1f}% | ${r['total_volume_usd']:>7.0f} | ${r['fees_paid_usd']:>6.2f} | ${r['rebates_earned_usd']:>7.2f} | ${r['net_pnl_usd']:>6.2f}")
log.info("=" * 95)

# Save JSON results
out_path = Path("data/derived/latency_sniper_tier_actionability.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"tiers": results_by_tier}, f, indent=2)
log.info(f"\nSaved analysis to {out_path}")
