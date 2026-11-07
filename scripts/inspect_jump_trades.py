"""Detailed inspection of trades around Binance jump events."""
import json
from pathlib import Path

with open("data/derived/jump_clob_response_summary.json", "r", encoding="utf-8") as f:
    results = json.load(f)

print(f"Total events analyzed: {len(results)}")
for r in results:
    ev = r["event"]
    print(f"\n=======================================================")
    print(f"EVENT: {ev['name']} | UTC: {ev['utc']} | Jump: {ev['dir']} {ev['bps']:.1f} bps | Total CLOB trades: {r['total_clob_trades']}")
    crypto_trades = r["crypto_trades"]
    print(f"Crypto trades count: {len(crypto_trades)}")
    total_dollar = sum(t["notional_usd"] for t in crypto_trades)
    print(f"Total crypto volume in window: ${total_dollar:.2f}")
    for t in crypto_trades:
        print(f"  Delta: {t['delta_venue_ms']:+5d} ms | Side: {t['side']:4s} | Price: {t['price']:.3f} | Size: {t['size']:8.1f} | Notional: ${t['notional_usd']:6.2f} | {t['question']}")
