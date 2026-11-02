"""Polymarket Live CLOB REST & WebSocket Client for Paper Trading.

Manages:
- REST queries to https://clob.polymarket.com for dynamic head reward pool selection.
- WebSocket stream from wss://ws-subscriptions-clob.polymarket.com/ws/market.
- Event dispatching to PaperTradingEngine:
    - 'book' & 'best_bid_ask' -> orderbook state & virtual quoting
    - 'last_trade_price' -> trade execution & virtual fill simulation
- 1-minute reward sampling epochs simulation (1,440/day).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import requests
import websockets
from websockets.exceptions import ConnectionClosed

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.paper_trading.engine import PaperTradingEngine
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig

logger = get_logger("paper_trading")

CLOB_REST_URL = "https://clob.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketLiveClient:
    """Coordinates live CLOB data feeds with the PaperTradingEngine."""

    def __init__(
        self,
        engine: PaperTradingEngine,
        rest_url: str = CLOB_REST_URL,
        ws_url: str = CLOB_WS_URL,
        epoch_interval_seconds: float = 60.0,
    ) -> None:
        self.engine = engine
        self.rest_url = rest_url
        self.ws_url = ws_url
        self.epoch_interval = epoch_interval_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "polymarketbot/paper-trading-rewards"})
        self.subscribed_token_ids: list[str] = []
        self._running = False
        install_doh_fallback()

    def fetch_dynamic_reward_pools(
        self,
        limit: int = 5,
        filter_keyword: Optional[str] = None,
    ) -> list[MarketConfig]:
        """Fetch top reward head pools dynamically from Polymarket CLOB REST API.

        Args:
            limit: Maximum number of markets to return.
            filter_keyword: Optional keyword to prioritize (e.g. "Wealth Tax").

        Returns:
            List of configured MarketConfig instances.
        """
        url = f"{self.rest_url}/rewards/markets/multi"
        params = {
            "order_by": "rate_per_day",
            "position": "DESC",
            "page_size": max(limit * 3, 20),
        }
        logger.info(f"Querying CLOB REST for reward pools: {url}")
        resp = self.session.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])

        matched: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []

        for m in data:
            q = m.get("question", "")
            if filter_keyword and filter_keyword.lower() in q.lower():
                matched.append(m)
            else:
                unmatched.append(m)

        selected_raw = (matched + unmatched)[:limit]
        configs: list[MarketConfig] = []

        for m in selected_raw:
            cid = m.get("condition_id", "")
            q = m.get("question", "Unknown")
            rewards_cfg = m.get("rewards_config", [])
            rate = float(rewards_cfg[0].get("rate_per_day", 0.0)) if rewards_cfg else 0.0
            min_size = float(m.get("rewards_min_size", 50.0))
            max_spread = float(m.get("rewards_max_spread", 3.0))

            tokens: list[TokenConfig] = []
            for tok in m.get("tokens", []):
                t_id = tok.get("token_id")
                outcome = tok.get("outcome", "unknown")
                p = float(tok.get("price", 0.50))
                if t_id:
                    tokens.append(TokenConfig(token_id=t_id, outcome=outcome, price=p))

            cfg = MarketConfig(
                condition_id=cid,
                question=q,
                rate_per_day=rate,
                rewards_min_size=min_size,
                rewards_max_spread=max_spread,
                tokens=tokens,
            )
            configs.append(cfg)
            self.engine.register_market(cfg)
            for t in tokens:
                if t.token_id not in self.subscribed_token_ids:
                    self.subscribed_token_ids.append(t.token_id)

        logger.info(
            f"Dynamically selected {len(configs)} head reward pools ({len(self.subscribed_token_ids)} tokens)."
        )
        return configs

    def fetch_initial_order_books(self) -> None:
        """Fetch REST snapshot of order books for all subscribed tokens."""
        logger.info(f"Fetching initial order books for {len(self.subscribed_token_ids)} tokens...")
        for tok_id in self.subscribed_token_ids:
            try:
                resp = self.session.get(
                    f"{self.rest_url}/book", params={"token_id": tok_id}, timeout=8.0
                )
                if resp.status_code == 200:
                    book_data = resp.json()
                    bids = book_data.get("bids", [])
                    asks = book_data.get("asks", [])
                    tick = float(book_data.get("tick_size", 0.01))
                    self.engine.update_orderbook(tok_id, bids, asks, tick_size=tick)
                elif resp.status_code == 429:
                    time.sleep(2.0)
                time.sleep(0.2)  # courteous throttling
            except Exception as e:
                logger.warning(f"Failed to fetch initial book for {tok_id[:12]}: {e}")

    async def _epoch_loop(self) -> None:
        """Periodic background task simulating the 1,440 daily reward sampling epochs."""
        logger.info(f"Starting epoch sampling loop (interval: {self.epoch_interval:.1f}s)...")
        while self._running:
            try:
                await asyncio.sleep(self.epoch_interval)
                if not self._running:
                    break
                self.engine.run_epoch_sample()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in epoch sampling loop: {e}")

    def _handle_ws_message(self, raw_msg: str) -> None:
        """Parse and route WebSocket messages to the paper trading engine."""
        try:
            data = json.loads(raw_msg)
        except Exception:
            return

        # Snapshot or book list
        if isinstance(data, list):
            for item in data:
                ev = item.get("event_type")
                aid = item.get("asset_id")
                if ev == "book" and aid:
                    bids = item.get("bids", [])
                    asks = item.get("asks", [])
                    tick = float(item.get("tick_size", 0.01))
                    self.engine.update_orderbook(aid, bids, asks, tick_size=tick)
            return

        if not isinstance(data, dict):
            return

        ev_type = data.get("event_type")
        aid = data.get("asset_id")

        # 1. Order book snapshot or update
        if ev_type == "book" and aid:
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            tick = float(data.get("tick_size", 0.01))
            self.engine.update_orderbook(aid, bids, asks, tick_size=tick)

        # 2. Best bid/ask top of book update
        elif ev_type == "best_bid_ask" and aid:
            bb = float(data.get("best_bid", 0.0))
            ba = float(data.get("best_ask", 1.0))
            # Quick partial update if book exists
            existing_book = self.engine.orderbooks.get(aid)
            if existing_book:
                mid = (bb + ba) / 2.0 if (bb > 0 and ba < 1.0) else existing_book["mid"]
                existing_book["best_bid"] = bb
                existing_book["best_ask"] = ba
                existing_book["mid"] = round(mid, 4)
                existing_book["spread_cents"] = round((ba - bb) * 100.0, 2)
                self.engine.generate_quotes(aid)

        # 3. Price change / depth update
        elif ev_type == "price_change":
            changes = data.get("price_changes", [])
            for ch in changes:
                c_aid = ch.get("asset_id")
                if c_aid and c_aid in self.engine.orderbooks:
                    b_bid = ch.get("best_bid")
                    b_ask = ch.get("best_ask")
                    if b_bid is not None and b_ask is not None:
                        bb = float(b_bid)
                        ba = float(b_ask)
                        book = self.engine.orderbooks[c_aid]
                        if bb > 0 and ba > 0 and ba > bb:
                            book["best_bid"] = bb
                            book["best_ask"] = ba
                            book["mid"] = round((bb + ba) / 2.0, 4)
                            book["spread_cents"] = round((ba - bb) * 100.0, 2)
                            self.engine.generate_quotes(c_aid)

        # 4. Real-time trade event: check against virtual resting orders
        elif ev_type == "last_trade_price" and aid:
            trade_price = float(data.get("price", 0.0))
            trade_size = float(data.get("size", 0.0))
            trade_side = str(data.get("side", ""))
            ts = float(data.get("timestamp", time.time() * 1000)) / 1000.0
            if trade_price > 0 and trade_size > 0:
                self.engine.on_trade(aid, trade_price, trade_size, trade_side, timestamp=ts)

    async def run(self, duration_seconds: Optional[float] = None) -> None:
        """Run the live paper trading WebSocket listener and epoch simulator.

        Args:
            duration_seconds: If provided, runs for this duration and exits cleanly.
        """
        self._running = True
        start_ts = time.time()
        epoch_task = asyncio.create_task(self._epoch_loop())

        logger.info(
            f"Connecting to Polymarket CLOB WebSocket: {self.ws_url} "
            f"({len(self.subscribed_token_ids)} tokens)"
        )

        try:
            while self._running:
                if duration_seconds and (time.time() - start_ts) >= duration_seconds:
                    logger.info(f"Target duration of {duration_seconds}s reached.")
                    break

                try:
                    async with websockets.connect(
                        self.ws_url,
                        ping_interval=10,
                        ping_timeout=10,
                        close_timeout=5,
                    ) as ws:
                        # Subscribe to all target asset IDs
                        sub_payload = {
                            "assets_ids": self.subscribed_token_ids,
                            "type": "market",
                            "custom_feature_enabled": True,
                        }
                        await ws.send(json.dumps(sub_payload))
                        logger.info("Sent WebSocket market subscription successfully.")

                        while self._running:
                            if duration_seconds and (time.time() - start_ts) >= duration_seconds:
                                break

                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                                self._handle_ws_message(msg)
                            except asyncio.TimeoutError:
                                # Normal if market is quiet, socket ping/pong keeps connection alive
                                continue

                except ConnectionClosed as e:
                    logger.warning(f"WebSocket connection closed ({e}). Reconnecting in 3s...")
                    await asyncio.sleep(3.0)
                except Exception as e:
                    logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                    await asyncio.sleep(5.0)

        finally:
            self._running = False
            epoch_task.cancel()
            try:
                await epoch_task
            except asyncio.CancelledError:
                pass
            # Final state persistence
            ledger_file = self.engine.save_ledger()
            logger.info(f"Paper trading session ended. State persisted to {ledger_file}")
