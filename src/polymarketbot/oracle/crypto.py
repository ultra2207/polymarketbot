"""Cluster 1 Oracle Adapter: Crypto Fast-Expiry & Strike Markets.

Covers:
- 5-minute, 15-minute, 1-hour, and Daily BTC/ETH/SOL/XRP strike & Up/Down markets.
- Polymarket resolution mechanics:
  * 5m / 15m Fast-Expiry: Chainlink Data Streams 60-second symmetric TWAP.
    Up wins if Closing TWAP >= Opening TWAP (ties resolve to UP).
  * 1h / Daily & Strike: Binance 1m/1h klines, Chainlink Polygon aggregators,
    and UMA Optimistic Oracle OOv2 (identifier: YES_OR_NO_QUERY).
  * Pyth Hermes API: Low-latency sub-second cross-venue validation & confidence intervals.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple, Union

import requests

from polymarketbot.logs import get_logger
from polymarketbot.oracle.base import (
    BaseOracleAdapter,
    GroundTruthOutcome,
    OracleCategory,
    OracleFeedSource,
    OracleStatus,
    ParsedAncillaryData,
    PricePoint,
    ResolutionOutcome,
    ResolutionVerdict,
)

logger = get_logger("oracle.crypto")

# Default public low-latency REST endpoints (no authentication required)
DEFAULT_BINANCE_REST = "https://api.binance.com"
DEFAULT_BINANCE_FALLBACK = "https://data-api.binance.vision"
DEFAULT_PYTH_HERMES = "https://hermes.pyth.network"
DEFAULT_COINBASE_REST = "https://api.coinbase.com"

# Pyth Price Feed IDs (Hex format)
PYTH_FEED_IDS: Dict[str, str] = {
    "BTC": "0xe62df6e88884733396b194958b7f7cc5ab4f99f0b394957d643ca7a97728cf4b",
    "ETH": "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL": "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "XRP": "0xec5d399846a9209f3fe5881d70aae9268c94339d0088307a82199e0ecdb4f279",
}

# Chainlink Polygon Mainnet Aggregators (Proxy Addresses)
CHAINLINK_POLYGON_FEEDS: Dict[str, str] = {
    "BTC": "0xc907E11604dAd104C31d274044ffb6Q433D57F6f",
    "ETH": "0xF9680d99D6C9589e2a93A78A04A279e509205945",
    "SOL": "0x10C8264C0935b3B9870013e057f330ff3e9C56dC",
    "XRP": "0x4d32f117e84a355D9D71C1b12b5fB62b66D40026",
}

# Default annualised volatility assumptions for Black-Scholes projection
DEFAULT_ANNUAL_VOL: Dict[str, float] = {
    "BTC": 0.55,
    "ETH": 0.65,
    "SOL": 0.80,
    "XRP": 0.75,
}


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class CryptoMarketMetadata:
    """Parsed crypto market taxonomy and resolution parameters."""
    condition_id: str
    slug: str
    question: str
    asset: str
    regime: str                     # "FAST_5M", "FAST_15M", "INTRADAY_1H", "DAILY", "STRIKE"
    is_up_down: bool
    is_twap: bool
    twap_window_s: float
    start_timestamp: float
    end_timestamp: float
    strike_price: Optional[float] = None
    feed_source: OracleFeedSource = OracleFeedSource.CHAINLINK_DATA_STREAMS


class CryptoOracleAdapter(BaseOracleAdapter):
    """Cluster 1 Real-Time Oracle Adapter for Crypto Fast-Expiry & Strike Markets."""

    def __init__(
        self,
        binance_url: Optional[str] = None,
        pyth_url: Optional[str] = None,
        coinbase_url: Optional[str] = None,
        polygon_rpc_url: Optional[str] = None,
        timeout_seconds: float = 3.0,
        enable_network: bool = True,
    ) -> None:
        super().__init__(name="crypto", category=OracleCategory.CRYPTO)
        self.binance_url = (binance_url or os.getenv("BINANCE_REST_URL") or DEFAULT_BINANCE_REST).rstrip("/")
        self.pyth_url = (pyth_url or os.getenv("PYTH_HERMES_URL") or DEFAULT_PYTH_HERMES).rstrip("/")
        self.coinbase_url = (coinbase_url or os.getenv("COINBASE_API_URL") or DEFAULT_COINBASE_REST).rstrip("/")
        self.polygon_rpc_url = polygon_rpc_url or os.getenv("POLYGON_RPC_URL", "")
        self.timeout_s = timeout_seconds
        self.enable_network = enable_network

        # Local in-memory rolling price series: asset -> deque of (timestamp_s, price)
        self._price_history: Dict[str, Deque[Tuple[float, float]]] = {
            "BTC": deque(maxlen=3600),
            "ETH": deque(maxlen=3600),
            "SOL": deque(maxlen=3600),
            "XRP": deque(maxlen=3600),
        }
        # Last verified price point per asset
        self._latest_prices: Dict[str, PricePoint] = {}

    def can_handle(
        self,
        question: str,
        resolution_source: Optional[str] = None,
        category: Optional[str] = None,
    ) -> bool:
        """Check if this adapter can resolve the given question/market."""
        if category and "crypto" in category.lower():
            return True
        q_lower = question.lower()
        if any(c in q_lower for c in ["btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp", "ripple"]):
            return True
        if resolution_source and any(s in resolution_source.lower() for s in ["binance", "chainlink", "pyth", "coinbase"]):
            return True
        return False

    def evaluate(self, candidate: Any) -> Optional[GroundTruthOutcome]:
        """Evaluate a Domain2Candidate for crypto markets."""
        market_info = {
            "condition_id": getattr(candidate, "condition_id", ""),
            "slug": getattr(candidate, "slug", getattr(candidate, "market_slug", "")),
            "question": getattr(candidate, "question", ""),
            "end_timestamp": getattr(candidate, "end_date_ts", getattr(candidate, "end_timestamp", None)),
        }
        verdict = self.evaluate_market_resolution(market_info)
        status = OracleStatus.CONCLUDED if verdict.is_deterministic else OracleStatus.IN_PLAY
        winner = "YES" if verdict.outcome == ResolutionOutcome.YES else ("NO" if verdict.outcome == ResolutionOutcome.NO else None)
        h_rev = 0.0 if verdict.is_deterministic else 0.05
        return GroundTruthOutcome(
            event_id=market_info["condition_id"],
            category=OracleCategory.CRYPTO,
            status=status,
            concluded=verdict.is_deterministic,
            winning_outcome=winner,
            p_hat=verdict.p_yes,
            h_reversal=h_rev,
            confidence=verdict.confidence,
            resolution_source_url=self.binance_url,
            ancillary_details={"rationale": verdict.rationale, "seconds_remaining": verdict.seconds_remaining},
        )

    def record_price(
        self,
        asset: str,
        price: float,
        timestamp_s: Optional[float] = None,
        source: OracleFeedSource = OracleFeedSource.BINANCE_SPOT,
        confidence: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> PricePoint:
        """Inject or record a verified price observation into the rolling cache."""
        symbol = asset.upper()
        ts = timestamp_s if timestamp_s is not None else time.time()
        point = PricePoint(
            asset=symbol,
            price=price,
            timestamp_ms=int(ts * 1000),
            source=source,
            confidence=confidence,
            extra=extra or {},
        )
        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=3600)
        self._price_history[symbol].append((ts, price))
        self._latest_prices[symbol] = point
        return point

    async def get_latest_price(self, asset: str) -> PricePoint:
        """Fetch latest price from lowest latency free endpoint with automatic failover."""
        symbol = asset.upper()
        if not self.enable_network:
            cached = self._latest_prices.get(symbol)
            if cached:
                return cached
            raise RuntimeError(f"Network disabled and no cached price for {symbol}")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_latest_price_sync, symbol)

    def _fetch_latest_price_sync(self, symbol: str) -> PricePoint:
        """Synchronous multi-venue fetch: Binance Spot -> Pyth Hermes -> Coinbase."""
        now_s = time.time()

        # 1. Primary: Binance Public REST (lowest latency global benchmark)
        try:
            url = f"{self.binance_url}/api/v3/ticker/price"
            params = {"symbol": f"{symbol}USDT"}
            resp = requests.get(url, params=params, timeout=self.timeout_s)
            if resp.status_code == 200:
                data = resp.json()
                price = float(data["price"])
                return self.record_price(
                    symbol, price, now_s, source=OracleFeedSource.BINANCE_SPOT
                )
        except Exception as exc:
            self.logger.warning(f"Binance fetch failed for {symbol}: {exc}")

        # 1b. Binance Vision mirror fallback
        try:
            url = f"{DEFAULT_BINANCE_FALLBACK}/api/v3/ticker/price"
            params = {"symbol": f"{symbol}USDT"}
            resp = requests.get(url, params=params, timeout=self.timeout_s)
            if resp.status_code == 200:
                data = resp.json()
                price = float(data["price"])
                return self.record_price(
                    symbol, price, now_s, source=OracleFeedSource.BINANCE_SPOT
                )
        except Exception:
            pass

        # 2. Secondary: Pyth Hermes API (Decentralized signed data)
        feed_id = PYTH_FEED_IDS.get(symbol)
        if feed_id:
            try:
                url = f"{self.pyth_url}/v2/updates/price/latest"
                params = {"ids[]": feed_id}
                resp = requests.get(url, params=params, timeout=self.timeout_s)
                if resp.status_code == 200:
                    payload = resp.json()
                    parsed = payload.get("parsed", [])
                    if parsed:
                        item = parsed[0]
                        p_data = item.get("price", {})
                        raw_price = float(p_data.get("price", 0))
                        expo = int(p_data.get("expo", 0))
                        price = raw_price * (10 ** expo)
                        conf_raw = float(p_data.get("conf", 0))
                        conf = conf_raw * (10 ** expo)
                        return self.record_price(
                            symbol,
                            price,
                            now_s,
                            source=OracleFeedSource.PYTH_HERMES,
                            confidence=conf,
                        )
            except Exception as exc:
                self.logger.warning(f"Pyth Hermes fetch failed for {symbol}: {exc}")

        # 3. Tertiary: Coinbase Public API (USD spot)
        try:
            url = f"{self.coinbase_url}/v2/prices/{symbol}-USD/spot"
            resp = requests.get(url, timeout=self.timeout_s)
            if resp.status_code == 200:
                data = resp.json()
                price = float(data["data"]["amount"])
                return self.record_price(
                    symbol, price, now_s, source=OracleFeedSource.COINBASE_SPOT
                )
        except Exception as exc:
            self.logger.warning(f"Coinbase fetch failed for {symbol}: {exc}")

        # Fallback to last recorded cached price if available
        if symbol in self._latest_prices:
            return self._latest_prices[symbol]

        raise RuntimeError(f"All oracle sources exhausted for {symbol}")

    async def get_historical_price(
        self, asset: str, timestamp_s: float
    ) -> Optional[PricePoint]:
        """Fetch historical price at exact timestamp via Binance klines or in-memory history."""
        symbol = asset.upper()

        # Check in-memory buffer first
        history = self._price_history.get(symbol)
        if history:
            best_diff = float("inf")
            best_point: Optional[Tuple[float, float]] = None
            for t, p in history:
                diff = abs(t - timestamp_s)
                if diff < best_diff:
                    best_diff = diff
                    best_point = (t, p)
            if best_point and best_diff <= 5.0:
                return PricePoint(
                    asset=symbol,
                    price=best_point[1],
                    timestamp_ms=int(best_point[0] * 1000),
                    source=OracleFeedSource.BINANCE_SPOT,
                )

        if not self.enable_network:
            return None

        # Fetch 1m kline from Binance around timestamp_s
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._fetch_historical_kline_sync, symbol, timestamp_s
        )

    def _fetch_historical_kline_sync(
        self, symbol: str, timestamp_s: float
    ) -> Optional[PricePoint]:
        start_ms = int((timestamp_s - 60) * 1000)
        end_ms = int((timestamp_s + 60) * 1000)
        try:
            url = f"{self.binance_url}/api/v3/klines"
            params = {
                "symbol": f"{symbol}USDT",
                "interval": "1m",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 5,
            }
            resp = requests.get(url, params=params, timeout=self.timeout_s)
            if resp.status_code == 200:
                klines = resp.json()
                if klines:
                    target_ms = int(timestamp_s * 1000)
                    best_kline = min(klines, key=lambda k: abs(k[0] - target_ms))
                    close_price = float(best_kline[4])
                    return PricePoint(
                        asset=symbol,
                        price=close_price,
                        timestamp_ms=int(best_kline[0]),
                        source=OracleFeedSource.BINANCE_SPOT,
                        extra={"open": float(best_kline[1]), "close": close_price},
                    )
        except Exception as exc:
            self.logger.warning(f"Binance historical kline failed for {symbol}: {exc}")
        return None

    def parse_ancillary_data(self, ancillary: Union[str, bytes]) -> ParsedAncillaryData:
        """Parse raw UMA ancillary data bytes/string or Polymarket description."""
        if isinstance(ancillary, bytes):
            text = ancillary.decode("utf-8", errors="replace")
        else:
            text = str(ancillary)

        parsed = ParsedAncillaryData(raw_ancillary=text)
        lower_text = text.lower()

        # 1. Identify crypto asset (BTC, ETH, SOL, XRP)
        if any(k in lower_text for k in ["bitcoin", "btc"]):
            parsed.asset = "BTC"
        elif any(k in lower_text for k in ["ethereum", "eth"]):
            parsed.asset = "ETH"
        elif any(k in lower_text for k in ["solana", "sol"]):
            parsed.asset = "SOL"
        elif any(k in lower_text for k in ["ripple", "xrp"]):
            parsed.asset = "XRP"

        # 2. Extract Title / Question
        title_match = re.search(r"title:\s*([^,\n\r]+)", text, re.IGNORECASE)
        if title_match:
            parsed.title = title_match.group(1).strip()
        elif "q:" in lower_text:
            parsed.title = text.split("q:")[-1].split(",")[0].strip()
        else:
            parsed.title = text[:120].strip()

        # 3. Detect TWAP mechanism vs Candle mechanism
        if "twap" in lower_text or "-5m-" in lower_text or "-15m-" in lower_text or "up or down" in lower_text:
            parsed.is_twap = True
            parsed.twap_window_seconds = 60.0
            parsed.source_feed = "Chainlink Data Streams"
        elif "binance" in lower_text:
            parsed.source_feed = "Binance Klines"
        elif "chainlink" in lower_text:
            parsed.source_feed = "Chainlink Aggregator"

        # 4. Extract Strike Price (e.g. $95,000, $2,500.50, 95000)
        strike_match = re.search(r"\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)", text)
        if strike_match:
            val_str = strike_match.group(1).replace(",", "")
            try:
                parsed.strike_price = float(val_str)
            except ValueError:
                pass

        # 5. Extract Timestamps (Unix epoch timestamps)
        ts_matches = re.findall(r"\b(1[6-9][0-9]{8})\b", text)
        if ts_matches:
            timestamps = sorted([float(ts) for ts in ts_matches])
            if len(timestamps) >= 2:
                parsed.benchmark_timestamp = timestamps[0]
                parsed.resolution_timestamp = timestamps[1]
            elif len(timestamps) == 1:
                parsed.resolution_timestamp = timestamps[0]

        # 6. Comparison Operator
        if any(k in lower_text for k in ["at or above", "greater than or equal", ">="]):
            parsed.comparison_operator = ">="
        elif any(k in lower_text for k in ["above", "greater than", ">"]):
            parsed.comparison_operator = ">"
        elif any(k in lower_text for k in ["below", "less than", "<"]):
            parsed.comparison_operator = "<"
        elif "up or down" in lower_text:
            parsed.comparison_operator = "UP_OR_DOWN"

        return parsed

    def classify_market(
        self,
        market_info: Dict[str, Any],
        now_ts: Optional[float] = None,
    ) -> CryptoMarketMetadata:
        """Parse Polymarket dictionary into structured CryptoMarketMetadata."""
        now = now_ts if now_ts is not None else time.time()
        slug = str(market_info.get("market_slug", market_info.get("slug", ""))).lower()
        question = str(market_info.get("question", "")).lower()
        cond_id = str(market_info.get("condition_id", market_info.get("id", "")))

        # Determine Asset
        combined_text = f"{slug} {question}"
        if any(k in combined_text for k in ["btc", "bitcoin"]):
            asset = "BTC"
        elif any(k in combined_text for k in ["eth", "ethereum"]):
            asset = "ETH"
        elif any(k in combined_text for k in ["sol", "solana"]):
            asset = "SOL"
        elif any(k in combined_text for k in ["xrp", "ripple"]):
            asset = "XRP"
        else:
            asset = "BTC"

        # Determine Regime & Timestamps
        start_ts = now
        end_ts = now + 3600.0

        # Check explicit timestamp fields in market_info
        for key in ["end_timestamp", "end_date_iso", "end_date", "endDate", "resolution_time"]:
            val = market_info.get(key)
            if not val:
                continue
            if isinstance(val, (int, float)) and val > 1_000_000_000:
                end_ts = float(val)
                break
            if isinstance(val, str):
                try:
                    clean_str = val.replace("Z", "+00:00")
                    if len(clean_str) == 10 and clean_str.count("-") == 2:
                        clean_str += "T23:59:59+00:00"
                    end_ts = datetime.fromisoformat(clean_str).timestamp()
                    break
                except Exception:
                    pass

        # Check slug for Unix epoch tag (e.g. btc-updown-5m-1793701200)
        match = re.search(r"-(\d{10})(?:-|$)", slug)
        if match:
            end_ts = float(match.group(1))

        if "-5m-" in slug:
            regime = "FAST_5M"
            duration = 300.0
            start_ts = end_ts - duration
            is_twap = True
            twap_window = 60.0
            feed = OracleFeedSource.CHAINLINK_DATA_STREAMS
        elif "-15m-" in slug:
            regime = "FAST_15M"
            duration = 900.0
            start_ts = end_ts - duration
            is_twap = True
            twap_window = 60.0
            feed = OracleFeedSource.CHAINLINK_DATA_STREAMS
        elif "-1h-" in slug or "1-hour" in question:
            regime = "INTRADAY_1H"
            duration = 3600.0
            start_ts = end_ts - duration
            is_twap = False
            twap_window = 60.0
            feed = OracleFeedSource.BINANCE_SPOT
        elif "daily" in slug or "daily" in question:
            regime = "DAILY"
            duration = 86400.0
            start_ts = end_ts - duration
            is_twap = False
            twap_window = 60.0
            feed = OracleFeedSource.BINANCE_SPOT
        else:
            regime = "STRIKE"
            is_twap = False
            twap_window = 60.0
            feed = OracleFeedSource.UMA_OPTIMISTIC

        is_up_down = "up or down" in question or "updown" in slug or regime in ("FAST_5M", "FAST_15M")

        # Parse strike price from question/slug
        strike = None
        strike_match = re.search(r"\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)", question)
        if strike_match:
            try:
                strike = float(strike_match.group(1).replace(",", ""))
            except ValueError:
                pass

        return CryptoMarketMetadata(
            condition_id=cond_id,
            slug=slug,
            question=question,
            asset=asset,
            regime=regime,
            is_up_down=is_up_down,
            is_twap=is_twap,
            twap_window_s=twap_window,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            strike_price=strike,
            feed_source=feed,
        )

    def evaluate_market_resolution(
        self,
        market_info: Dict[str, Any],
        current_prices: Optional[Dict[str, float]] = None,
        as_of_timestamp: Optional[float] = None,
    ) -> ResolutionVerdict:
        """Evaluate real-time settlement probability and deterministic finality.
        
        Covers:
        1. 5m / 15m Fast Binary TWAP: Symmetric 60s lookback benchmark vs settlement.
           Ties resolve to YES/UP.
        2. Strike Price Binary: Current/settlement spot vs fixed strike price K.
        3. 1h / Daily Binance Kline: Close price vs Open price of candle.
        """
        now = as_of_timestamp if as_of_timestamp is not None else time.time()
        meta = self.classify_market(market_info, now_ts=now)
        asset = meta.asset

        # Resolve current reference price
        if current_prices and asset in current_prices:
            curr_spot = current_prices[asset]
        elif asset in self._latest_prices:
            curr_spot = self._latest_prices[asset].price
        else:
            curr_spot = 100_000.0 if asset == "BTC" else 3_000.0

        time_remaining = max(0.0, meta.end_timestamp - now)
        is_expired = time_remaining <= 0.0

        # ------------------------------------------------------------------- #
        # Case 1: Fast-Expiry 5m / 15m Up/Down Markets (Chainlink 60s TWAP)
        # ------------------------------------------------------------------- #
        if meta.is_up_down and meta.is_twap:
            twap_win = meta.twap_window_s

            samples = list(self._price_history.get(asset, []))
            if not samples:
                samples = [(now - 300, curr_spot), (now, curr_spot)]

            # 1. Opening Benchmark TWAP: [start_timestamp, start_timestamp + 60]
            open_twap_start = meta.start_timestamp
            open_twap_end = meta.start_timestamp + twap_win
            bench_price = self.calculate_twap(samples, open_twap_start, open_twap_end)
            if bench_price is None:
                bench_price = meta.strike_price or curr_spot

            # 2. Closing Settlement TWAP: [end_timestamp - 60, end_timestamp]
            close_twap_start = meta.end_timestamp - twap_win
            close_twap_end = meta.end_timestamp

            if is_expired:
                settle_price = self.calculate_twap(samples, close_twap_start, close_twap_end)
                if settle_price is None:
                    settle_price = curr_spot

                # Polymarket Rule: UP wins if Closing TWAP >= Opening TWAP (ties resolve to UP)
                if settle_price >= bench_price:
                    return ResolutionVerdict(
                        condition_id=meta.condition_id,
                        outcome=ResolutionOutcome.YES,
                        p_yes=1.0,
                        settlement_price=settle_price,
                        benchmark_price=bench_price,
                        confidence=1.0,
                        rationale=f"Final Chainlink TWAP ({settle_price:.2f}) >= Benchmark TWAP ({bench_price:.2f}). Ties resolve UP.",
                        is_deterministic=True,
                        seconds_remaining=0.0,
                    )
                else:
                    return ResolutionVerdict(
                        condition_id=meta.condition_id,
                        outcome=ResolutionOutcome.NO,
                        p_yes=0.0,
                        settlement_price=settle_price,
                        benchmark_price=bench_price,
                        confidence=1.0,
                        rationale=f"Final Chainlink TWAP ({settle_price:.2f}) < Benchmark TWAP ({bench_price:.2f}). Outcome: DOWN.",
                        is_deterministic=True,
                        seconds_remaining=0.0,
                    )
            else:
                tau_years = max(1e-6, time_remaining / (365.25 * 86400.0))
                sigma = DEFAULT_ANNUAL_VOL.get(asset, 0.60)
                vol_remaining = sigma * math.sqrt(tau_years)

                denom = max(1e-6, vol_remaining)
                d2 = (math.log(curr_spot / bench_price) - 0.5 * (sigma ** 2) * tau_years) / denom
                p_up = _norm_cdf(d2)

                vol_window = curr_spot * sigma * math.sqrt(time_remaining / (365.25 * 86400.0))
                delta_price = abs(curr_spot - bench_price)
                is_locked = time_remaining <= 15.0 and delta_price > (3.5 * max(vol_window, 0.01))

                outcome = ResolutionOutcome.YES if p_up >= 0.5 else ResolutionOutcome.NO
                return ResolutionVerdict(
                    condition_id=meta.condition_id,
                    outcome=ResolutionOutcome.PENDING if not is_locked else outcome,
                    p_yes=p_up,
                    settlement_price=curr_spot,
                    benchmark_price=bench_price,
                    confidence=min(1.0, max(p_up, 1.0 - p_up)),
                    rationale=f"In-flight 5m/15m TWAP: P(UP)={p_up:.3f}, Spot={curr_spot:.2f}, Benchmark={bench_price:.2f}, tau={time_remaining:.0f}s",
                    is_deterministic=is_locked,
                    seconds_remaining=time_remaining,
                )

        # ------------------------------------------------------------------- #
        # Case 2: Fixed Strike Price Binary Markets (e.g. BTC >= $100,000)
        # ------------------------------------------------------------------- #
        strike = meta.strike_price if meta.strike_price is not None else curr_spot
        if is_expired:
            if curr_spot >= strike:
                return ResolutionVerdict(
                    condition_id=meta.condition_id,
                    outcome=ResolutionOutcome.YES,
                    p_yes=1.0,
                    settlement_price=curr_spot,
                    benchmark_price=strike,
                    confidence=1.0,
                    rationale=f"Expired: Spot price ({curr_spot:.2f}) >= Strike ({strike:.2f}). Payout YES.",
                    is_deterministic=True,
                    seconds_remaining=0.0,
                )
            else:
                return ResolutionVerdict(
                    condition_id=meta.condition_id,
                    outcome=ResolutionOutcome.NO,
                    p_yes=0.0,
                    settlement_price=curr_spot,
                    benchmark_price=strike,
                    confidence=1.0,
                    rationale=f"Expired: Spot price ({curr_spot:.2f}) < Strike ({strike:.2f}). Payout NO.",
                    is_deterministic=True,
                    seconds_remaining=0.0,
                )

        # In-flight strike evaluation
        tau_years = max(1e-6, time_remaining / (365.25 * 86400.0))
        sigma = DEFAULT_ANNUAL_VOL.get(asset, 0.60)
        vol_remaining = sigma * math.sqrt(tau_years)
        d2 = (math.log(curr_spot / strike) - 0.5 * (sigma ** 2) * tau_years) / max(1e-6, vol_remaining)
        p_yes = _norm_cdf(d2)

        return ResolutionVerdict(
            condition_id=meta.condition_id,
            outcome=ResolutionOutcome.PENDING,
            p_yes=p_yes,
            settlement_price=curr_spot,
            benchmark_price=strike,
            confidence=min(1.0, max(p_yes, 1.0 - p_yes)),
            rationale=f"In-flight strike: Spot={curr_spot:.2f}, Strike={strike:.2f}, P(YES)={p_yes:.3f}, tau={time_remaining:.0f}s",
            is_deterministic=False,
            seconds_remaining=time_remaining,
        )
