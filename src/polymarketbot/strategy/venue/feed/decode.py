"""WebSocket frame decoding into books and trades."""

from __future__ import annotations

import json
import time
import uuid

from .models import (
    MarketTrade,
    _parse_float,
    _parse_opt_float,
    _pick,
    build_book_snapshot,
)


class _DecodeMixin:
    """Turn one raw frame into book snapshots and trades."""

    async def _process_ws_payload(self, raw_data: str) -> None:
        """Parse raw WebSocket text and dispatch domain events."""
        self._last_ws_message_time = time.time()
        # Handle ping/pong text frames
        if raw_data.strip().upper() in ("PONG", "PING"):
            return

        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            return
        # A parsed frame is data traffic; PONG-only sockets never reach here.
        self._last_ws_data_time = time.time()

        messages = data if isinstance(data, list) else [data]

        for item in messages:
            if not isinstance(item, dict):
                continue

            # Support both flat and wrapped payload schemas
            p = item.get("payload", {}) if isinstance(item.get("payload"), dict) else item
            ev_type = item.get("event_type") or item.get("type") or p.get("event_type") or p.get("type")
            asset_id = item.get("asset_id") or item.get("assetId") or p.get("asset_id") or p.get("assetId")

            # Infer event type if not explicitly set
            if not ev_type:
                if "bids" in item or "bids" in p:
                    ev_type = "book"
                elif "price_changes" in item or "price_changes" in p:
                    ev_type = "price_change"
                elif ("best_bid" in item or "best_bid" in p) and ("best_ask" in item or "best_ask" in p):
                    ev_type = "best_bid_ask"
                elif ("price" in item or "price" in p) and ("side" in item or "fee_rate_bps" in p or "transaction_hash" in p):
                    ev_type = "last_trade_price"

            # 1. Full order book snapshot
            if ev_type == "book" and asset_id:
                bids = p.get("bids") if "bids" in p else item.get("bids", [])
                asks = p.get("asks") if "asks" in p else item.get("asks", [])
                tick = _parse_float(_pick(p, item, "tick_size", "tickSize"), 0.01)
                book_hash = _pick(p, item, "hash")
                ts_raw = _pick(p, item, "timestamp")
                ts = (_parse_float(ts_raw) / 1000.0) if ts_raw else time.time()

                snapshot = build_book_snapshot(
                    asset_id=asset_id,
                    raw_bids=bids,
                    raw_asks=asks,
                    tick_size=tick,
                    book_hash=book_hash,
                    timestamp=ts,
                    source="ws",
                )
                self._reset_book(asset_id, snapshot)
                await self._dispatch_book(snapshot)

            # 2. Best bid/ask top of book update (deep levels are preserved)
            elif ev_type == "best_bid_ask" and asset_id:
                bb = _parse_opt_float(_pick(p, item, "best_bid", "bestBid"))
                ba = _parse_opt_float(_pick(p, item, "best_ask", "bestAsk"))
                if bb is None and ba is None:
                    continue
                ts_raw = _pick(p, item, "timestamp")
                ts = (_parse_float(ts_raw) / 1000.0) if ts_raw else time.time()
                snapshot = self._update_book_top(asset_id, bb, ba, ts)
                await self._dispatch_book(snapshot)

            # 3. Price change / batch delta: `size` is the new aggregate size at
            # `price` on the `side` ladder (BUY = bids, SELL = asks); 0 removes it.
            # The `best_bid`/`best_ask` fields on these rows are stale (see
            # scripts/reduce_pmxt.py) and are deliberately not used.
            elif ev_type == "price_change":
                changes = _pick(p, item, "price_changes", "priceChanges", default=[])
                if not isinstance(changes, list):
                    changes = []
                updated = self._apply_price_changes(asset_id, changes)
                for changed_asset, ts in updated.items():
                    snapshot = self._snapshot_from_book(changed_asset, timestamp=ts, source="ws")
                    await self._dispatch_book(snapshot)

            # 4. Public trade stream
            elif ev_type in ("last_trade_price", "trade") and asset_id:
                price = _parse_float(p.get("price") or item.get("price"))
                size = _parse_float(p.get("size") or item.get("size"))
                side = str(p.get("side") or item.get("side", "BUY")).upper()
                ts_raw = p.get("timestamp") or item.get("timestamp")
                ts = (_parse_float(ts_raw) / 1000.0) if ts_raw else time.time()
                trade_id = str(p.get("transaction_hash") or p.get("transactionHash") or item.get("transaction_hash") or item.get("id") or uuid.uuid4().hex[:12])

                if price > 0.0 and size > 0.0:
                    trade = MarketTrade(
                        asset_id=asset_id,
                        price=price,
                        size=size,
                        side=side,
                        timestamp=ts,
                        trade_id=trade_id,
                        source="ws",
                    )
                    await self._dispatch_trade(trade)
