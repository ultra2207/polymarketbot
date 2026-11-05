"""PyArrow schemas for every tape table.

One schema per table. The writer uses these to convert the JSONL WAL into
Parquet, so a row emitted by a stream must use exactly these field names and
types. Missing fields are written as null; extra fields are ignored.

Conventions
-----------
* ``recv_ts_ns``  - UTC wall clock at receive time, epoch nanoseconds.
* ``recv_mono_ns``- ``time.perf_counter_ns()`` at receive time. Immune to clock
  steps, so inter-arrival and latency deltas stay sane across NTP corrections.
* ``venue_ts_ms`` - the exchange's own timestamp, epoch milliseconds, when the
  feed carries one. Cross-venue lead-lag is only meaningful against this field.
"""

from __future__ import annotations

import pyarrow as pa

LEVEL = pa.struct([("price", pa.float64()), ("size", pa.float64())])
PRICE_CHANGE = pa.struct(
    [
        # A single `price_change` frame can carry changes for both outcome
        # tokens, so the token identity belongs on each change, not only on
        # the row.
        ("asset_id", pa.string()),
        ("price", pa.float64()),
        ("size", pa.float64()),
        ("side", pa.string()),
        ("best_bid", pa.float64()),
        ("best_ask", pa.float64()),
    ]
)

CLOB_EVENTS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("shard", pa.int16()),
        ("condition_id", pa.string()),
        ("asset_id", pa.string()),
        ("event_type", pa.string()),
        ("bids", pa.list_(LEVEL)),
        ("asks", pa.list_(LEVEL)),
        ("price_changes", pa.list_(PRICE_CHANGE)),
        ("price", pa.float64()),
        ("size", pa.float64()),
        ("side", pa.string()),
        ("best_bid", pa.float64()),
        ("best_ask", pa.float64()),
        ("spread", pa.float64()),
        ("old_tick_size", pa.float64()),
        ("new_tick_size", pa.float64()),
        ("transaction_hash", pa.string()),
        ("event_hash", pa.string()),
        ("raw_json", pa.string()),
    ]
)

CLOB_TRADES = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("shard", pa.int16()),
        ("condition_id", pa.string()),
        ("asset_id", pa.string()),
        ("price", pa.float64()),
        ("size", pa.float64()),
        ("side", pa.string()),
        ("fee_rate_bps", pa.float64()),
        ("transaction_hash", pa.string()),
        ("raw_json", pa.string()),
    ]
)

BINANCE_BBO = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("symbol", pa.string()),
        ("bid", pa.float64()),
        ("bid_qty", pa.float64()),
        ("ask", pa.float64()),
        ("ask_qty", pa.float64()),
        ("update_id", pa.int64()),
    ]
)

BINANCE_DEPTH = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("symbol", pa.string()),
        ("last_update_id", pa.int64()),
        ("bids", pa.list_(LEVEL)),
        ("asks", pa.list_(LEVEL)),
    ]
)

BINANCE_FUNDING = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("symbol", pa.string()),
        ("funding_rate", pa.float64()),
        ("funding_time_ms", pa.int64()),
        ("mark_price", pa.float64()),
        ("index_price", pa.float64()),
        ("next_funding_time_ms", pa.int64()),
    ]
)

BINANCE_OI = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("symbol", pa.string()),
        ("open_interest", pa.float64()),
    ]
)

BINANCE_LIQUIDATION = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("symbol", pa.string()),
        ("side", pa.string()),
        ("price", pa.float64()),
        ("qty", pa.float64()),
        ("order_type", pa.string()),
        ("trade_time_ms", pa.int64()),
    ]
)

DERIBIT_SURFACE = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("venue_ts_ms", pa.int64()),
        ("index_name", pa.string()),
        ("dvol", pa.float64()),
        ("atm_iv", pa.float64()),
        ("skew_25d_call", pa.float64()),
        ("skew_25d_put", pa.float64()),
        ("term_7d", pa.float64()),
        ("term_14d", pa.float64()),
        ("term_30d", pa.float64()),
        ("raw_json", pa.string()),
    ]
)

KALSHI_MARKETS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("ticker", pa.string()),
        ("event_ticker", pa.string()),
        ("series_ticker", pa.string()),
        ("title", pa.string()),
        ("yes_bid", pa.float64()),
        ("yes_ask", pa.float64()),
        ("last_price", pa.float64()),
        ("volume", pa.float64()),
        ("volume_24h", pa.float64()),
        ("open_interest", pa.float64()),
        ("liquidity", pa.float64()),
        ("close_time", pa.string()),
        ("status", pa.string()),
        ("raw_json", pa.string()),
    ]
)

KALSHI_TRADES = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("trade_id", pa.string()),
        ("ticker", pa.string()),
        ("created_time", pa.string()),
        ("yes_price", pa.float64()),
        ("no_price", pa.float64()),
        ("count", pa.float64()),
        ("taker_side", pa.string()),
        ("taker_book_side", pa.string()),
        ("is_block_trade", pa.bool_()),
    ]
)

KALSHI_BOOKS = pa.schema(
    [
        # One row per (ticker, snapshot). Kalshi sends bids only; the asks are
        # derived as 1 - the opposite side's best bid, and mid/spread follow.
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("ticker", pa.string()),
        ("yes_best_bid", pa.float64()),
        ("yes_best_ask", pa.float64()),
        ("no_best_bid", pa.float64()),
        ("no_best_ask", pa.float64()),
        ("mid", pa.float64()),
        ("spread", pa.float64()),
        ("yes_bid_size", pa.float64()),
        ("no_bid_size", pa.float64()),
        # Cumulative resting size within 1/2/5/10 cents of that side's best bid.
        ("yes_bid_size_1c", pa.float64()),
        ("yes_bid_size_2c", pa.float64()),
        ("yes_bid_size_5c", pa.float64()),
        ("yes_bid_size_10c", pa.float64()),
        ("no_bid_size_1c", pa.float64()),
        ("no_bid_size_2c", pa.float64()),
        ("no_bid_size_5c", pa.float64()),
        ("no_bid_size_10c", pa.float64()),
        # JSON [[price, size], ...], best bid first.
        ("yes_bids", pa.string()),
        ("no_bids", pa.string()),
        ("raw_json", pa.string()),
    ]
)

POLYGON_LOGS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("block_number", pa.int64()),
        ("block_hash", pa.string()),
        ("tx_hash", pa.string()),
        ("tx_index", pa.int32()),
        ("log_index", pa.int32()),
        ("address", pa.string()),
        ("topic0", pa.string()),
        ("topic1", pa.string()),
        ("topic2", pa.string()),
        ("topic3", pa.string()),
        ("data", pa.string()),
        ("removed", pa.bool_()),
    ]
)

POLYGON_GAS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("block_number", pa.int64()),
        ("block_hash", pa.string()),
        ("base_fee_gwei", pa.float64()),
        ("gas_used", pa.int64()),
        ("gas_limit", pa.int64()),
        ("timestamp_ms", pa.int64()),
        ("tx_count", pa.int32()),
    ]
)

NEWS_ITEMS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("source", pa.string()),
        ("title", pa.string()),
        ("url", pa.string()),
        ("published_ts_ms", pa.int64()),
        ("summary", pa.string()),
        ("language", pa.string()),
        ("raw_json", pa.string()),
    ]
)

GAMMA_COMMENTS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("comment_id", pa.string()),
        ("parent_entity_type", pa.string()),
        ("parent_entity_id", pa.string()),
        ("body", pa.string()),
        ("created_at_ms", pa.int64()),
        ("author", pa.string()),
        ("raw_json", pa.string()),
    ]
)

WHALE_ACTIVITY = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        ("wallet", pa.string()),
        ("condition_id", pa.string()),
        ("asset_id", pa.string()),
        ("side", pa.string()),
        ("size", pa.float64()),
        ("price", pa.float64()),
        ("usdc_size", pa.float64()),
        ("activity_ts_ms", pa.int64()),
        ("tx_hash", pa.string()),
        ("activity_type", pa.string()),
        ("raw_json", pa.string()),
    ]
)

HEALTH_METRICS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("stream", pa.string()),
        ("metric", pa.string()),
        ("value", pa.float64()),
        ("labels_json", pa.string()),
    ]
)

SCHEMAS: dict[str, pa.Schema] = {
    "clob_events": CLOB_EVENTS,
    "clob_trades": CLOB_TRADES,
    "binance_bbo": BINANCE_BBO,
    "binance_depth": BINANCE_DEPTH,
    "binance_funding": BINANCE_FUNDING,
    "binance_oi": BINANCE_OI,
    "binance_liquidation": BINANCE_LIQUIDATION,
    "deribit_surface": DERIBIT_SURFACE,
    "kalshi_markets": KALSHI_MARKETS,
    "kalshi_trades": KALSHI_TRADES,
    "kalshi_books": KALSHI_BOOKS,
    "polygon_logs": POLYGON_LOGS,
    "polygon_gas": POLYGON_GAS,
    "news_items": NEWS_ITEMS,
    "gamma_comments": GAMMA_COMMENTS,
    "whale_activity": WHALE_ACTIVITY,
    "health_metrics": HEALTH_METRICS,
}
