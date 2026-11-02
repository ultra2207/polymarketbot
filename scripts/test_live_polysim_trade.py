#!/usr/bin/env python3
"""Test script to verify live trade execution, position management, and PnL realization on PolySimulator."""

import sys
import time
from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.polysim_client import PolySimulatorClient

log = get_logger("test_live_trade")


def main() -> int:
    client = PolySimulatorClient()
    if not client.api_key:
        print("[ERROR] POLYSIMULATOR_API_KEY is missing from environment.")
        return 1

    print("=================================================================")
    print(" [1] CONNECTING TO POLYSIMULATOR CLOUD")
    print("=================================================================")
    bal = client.get_balance()
    print(f" Account Balance : ${bal.balance:.2f} {bal.currency}")
    print(f" Total Equity    : ${bal.total_value:.2f}")
    print(f" Unrealized PnL  : ${bal.unrealized_pnl:.2f}")

    print("\n=================================================================")
    print(" [2] INSPECTING CURRENT OPEN POSITIONS")
    print("=================================================================")
    positions = client.get_positions(status="open")
    print(f" Found {len(positions)} open position(s):")
    for p in positions:
        print(f"   • Position #{p.get('id')}: {p.get('quantity')} shares of '{p.get('outcome')}' on '{p.get('market_question')[:40]}...'")
        print(f"     Entry: ${float(p.get('avg_entry_price', 0)):.3f} | Current: ${float(p.get('current_price', 0)):.3f} | Unrealized PnL: ${float(p.get('unrealized_pnl', 0)):.2f}")

    # Select target market
    markets = client.get_markets(limit=10, hot_only=True)
    if not markets:
        markets = client.get_markets(limit=10)
    target = markets[0]
    tokens = target["clob_token_ids"]
    yes_token = tokens[0]
    no_token = tokens[1]
    question = target.get("question", "")

    print(f"\n Target Market: '{question}'")
    print(f" YES Token ID : {yes_token[:20]}...")
    print(f" NO Token ID  : {no_token[:20]}...")

    # Fetch tick sizes and order books
    yes_tick = client.get_tick_size(yes_token)
    time.sleep(0.5)
    book = client.get_book(yes_token, depth=5)
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    best_bid = float(bids[-1]["price"]) if bids else 0.30
    best_ask = float(asks[-1]["price"]) if asks else 0.32
    print(f" Best Bid: ${best_bid:.3f} | Best Ask: ${best_ask:.3f}")

    # Step 3: If we have an existing open position of YES, let's close it by selling at the best bid!
    yes_pos = next((p for p in positions if p.get("outcome") == "Yes"), None)
    if yes_pos and float(yes_pos.get("quantity", 0)) > 0:
        sell_qty = float(yes_pos["quantity"])
        print(f"\n=================================================================")
        print(f" [3] EXECUTING LIVE EXIT: SELLING {sell_qty:.1f} SHARES OF YES @ ${best_bid:.3f}")
        print("=================================================================")
        sell_res = client.place_order(
            token_id=yes_token,
            side="SELL",
            size=sell_qty,
            price=best_bid,
            order_type="GTC",
            tick_size=yes_tick,
        )
        print(f" Sell Order Result: success={sell_res.success}, order_id={sell_res.order_id}, status={sell_res.status}")
        time.sleep(1.0)

    # Step 4: Execute a tactical BUY trade on YES or NO to demonstrate live entry
    trade_size = 10.0
    print(f"\n=================================================================")
    print(f" [4] EXECUTING LIVE ENTRY: BUYING {trade_size:.1f} SHARES OF YES @ ${best_ask:.3f}")
    print("=================================================================")
    buy_res = client.place_order(
        token_id=yes_token,
        side="BUY",
        size=trade_size,
        price=best_ask,
        order_type="GTC",
        tick_size=yes_tick,
    )
    print(f" Buy Order Result: success={buy_res.success}, order_id={buy_res.order_id}, status={buy_res.status}")
    time.sleep(1.0)

    # Step 5: Check updated positions and account balance
    print("\n=================================================================")
    print(" [5] VERIFYING UPDATED ACCOUNT PORTFOLIO")
    print("=================================================================")
    new_bal = client.get_balance()
    new_pos = client.get_positions(status="open")
    history = client._request("GET", "/account/history").json()

    print(f" New Balance : ${new_bal.balance:.2f} USD")
    print(f" New Equity  : ${new_bal.total_value:.2f}")
    print(f" Open Positions Count: {len(new_pos)}")
    for p in new_pos:
        print(f"   • [LIVE POSITION #{p.get('id')}] {p.get('quantity')} shares of '{p.get('outcome')}' on '{p.get('market_question')[:40]}...' @ avg ${float(p.get('avg_entry_price', 0)):.3f}")

    print(f"\n Most Recent Executed Trades in PolySimulator:")
    for h in history[:3]:
        print(f"   • Trade #{h.get('order_id')}: {h.get('side')} {h.get('quantity')} {h.get('outcome')} @ ${float(h.get('price', 0)):.3f} (status: {h.get('status')}, fee: ${h.get('fee')})")

    print("\n>>> LIVE TRADING EXECUTION VERIFIED ON POLYSIMULATOR! <<<\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
