#!/usr/bin/env python3
"""Targeted live WebSocket queue and trade monitor for J.D. Vance 2028 GOP Nom.

Tracks live book depth and monitors incoming trade messages via Polymarket CLOB WebSocket
(wss://ws-subscriptions-clob.polymarket.com/ws/market).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.simulator.feed import LivePolymarketFeed, MarketTrade, OrderBookSnapshot

log = get_logger("check_vance_websocket")
install_doh_fallback()

# Tokens for J.D. Vance 2028 GOP Nom
# Condition ID: 0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919
TOKEN_YES = "40081275558852222228080198821361202017557872256707631666334039001378518619916"
TOKEN_NO = "78633590736077251574794513664747155551297291244492840448622550955320930591622"

OUTPUT_FILE = Path("data/processed/vance_live_websocket_audit.json")
OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)


async def run_vance_audit(duration_sec: float = 60.0):
    log.info(f"Starting Live WebSocket Audit on J.D. Vance 2028 for {duration_sec:.1f}s...")
    
    feed = LivePolymarketFeed(fallback_poll_interval=2.0)
    feed.subscribe([TOKEN_YES, TOKEN_NO])

    trades_received: List[Dict[str, Any]] = []
    book_updates: List[Dict[str, Any]] = []
    initial_snapshot: Dict[str, Any] = {}

    def on_trade(trade: MarketTrade):
        log.info(f"[LIVE TRADE RECEIVED] Asset: {trade.asset_id[:8]}... | Price: {trade.price} | Size: {trade.size:,.2f} | Side: {trade.side} | Notional: ${trade.price * trade.size:,.2f}")
        trades_received.append({
            "timestamp": time.time(),
            "asset_id": trade.asset_id,
            "side": trade.side,
            "price": trade.price,
            "size": trade.size,
            "notional_usd": trade.price * trade.size,
        })

    def on_book(book: OrderBookSnapshot):
        if not initial_snapshot:
            log.info(f"[INITIAL BOOK] Best Bid: ${book.best_bid:.4f} | Best Ask: ${book.best_ask:.4f} | Mid: ${book.mid:.4f} | Spread: {book.spread_cents:.2f}c")
        book_updates.append({
            "timestamp": time.time(),
            "asset_id": book.asset_id,
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "spread_cents": book.spread_cents,
            "bids_count": len(book.bids),
            "asks_count": len(book.asks),
        })

    feed.add_trade_listener(on_trade)
    feed.add_book_listener(on_book)

    # Initial REST snapshot of the book
    rest_book_yes = feed.get_book_sync(TOKEN_YES)
    initial_touch_bid_shares = rest_book_yes.bids[0].size if rest_book_yes and rest_book_yes.bids else 0.0
    initial_touch_bid_price = rest_book_yes.bids[0].price if rest_book_yes and rest_book_yes.bids else 0.0
    initial_touch_ask_shares = rest_book_yes.asks[0].size if rest_book_yes and rest_book_yes.asks else 0.0
    initial_touch_ask_price = rest_book_yes.asks[0].price if rest_book_yes and rest_book_yes.asks else 0.0

    log.info(f"REST Snapshot at Touch: Bid {initial_touch_bid_shares:,.2f} sh @ ${initial_touch_bid_price:.4f} (${initial_touch_bid_shares * initial_touch_bid_price:,.2f} USD)")
    log.info(f"REST Snapshot at Touch: Ask {initial_touch_ask_shares:,.2f} sh @ ${initial_touch_ask_price:.4f} (${initial_touch_ask_shares * initial_touch_ask_price:,.2f} USD)")

    feed_task = asyncio.create_task(feed.start())
    start_time = time.time()

    try:
        while time.time() - start_time < duration_sec:
            await asyncio.sleep(1.0)
            elapsed = time.time() - start_time
            if int(elapsed) % 15 == 0:
                log.info(f"[PROGRESS] {elapsed:.0f}s elapsed / {duration_sec:.0f}s | Live Trades Ingested: {len(trades_received)} | Book Updates: {len(book_updates)}")
    finally:
        log.info("Stopping feed...")
        await feed.stop()

    total_shares_traded = sum(t["size"] for t in trades_received)
    total_usd_traded = sum(t["notional_usd"] for t in trades_received)
    elapsed_time = time.time() - start_time
    empirical_depletion_velocity = total_shares_traded / elapsed_time if elapsed_time > 0 else 0.0

    result = {
        "market": "J.D. Vance 2028 Republican Presidential Nomination",
        "condition_id": "0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919",
        "duration_seconds": elapsed_time,
        "initial_touch": {
            "bid_price": initial_touch_bid_price,
            "bid_shares": initial_touch_bid_shares,
            "bid_usd": initial_touch_bid_shares * initial_touch_bid_price,
            "ask_price": initial_touch_ask_price,
            "ask_shares": initial_touch_ask_shares,
            "ask_usd": initial_touch_ask_shares * initial_touch_ask_price,
        },
        "trades_count": len(trades_received),
        "total_shares_traded": total_shares_traded,
        "total_usd_traded": total_usd_traded,
        "empirical_depletion_velocity_shares_sec": empirical_depletion_velocity,
        "trades": trades_received,
        "book_updates_count": len(book_updates),
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    log.info(f"Audit completed. Output written to {OUTPUT_FILE}")
    log.info(f"SUMMARY: {len(trades_received)} trades received | {total_shares_traded:,.2f} sh traded | Velocity: {empirical_depletion_velocity:.4f} sh/sec")


def main():
    asyncio.run(run_vance_audit(duration_sec=60.0))


if __name__ == "__main__":
    main()
