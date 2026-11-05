from __future__ import annotations

import pyarrow as pa

from polymarketbot.data.schema import BINANCE_BBO, BINANCE_DEPTH
from polymarketbot.data.streams import binance as b

RECV_TS = 1_793_750_000_000_000_000
RECV_MONO = 123_456_789

BBO_FRAME = {
    "stream": "btcusdt@bookTicker",
    "data": {
        "e": "bookTicker",
        "u": 12005260978562,
        "s": "BTCUSDT",
        "b": "77493.70",
        "B": "13.794",
        "a": "77493.80",
        "A": "0.460",
        "T": 1793750749133,
        "E": 1793750749135,
    },
}

DEPTH_FRAME = {
    "stream": "btcusdt@depth10@500ms",
    "data": {
        "e": "depthUpdate",
        "E": 1793750952399,
        "T": 1793750952397,
        "s": "BTCUSDT",
        "U": 12005333941380,
        "u": 1200533398731,
        "pu": 12005333941379,
        "b": [["77493.70", "1.5"], ["77493.60", "0.25"]],
        "a": [["77493.80", "2.0"], ["77493.90", "0.5"]],
    },
}

MARK_PRICE_FRAME = {
    "stream": "btcusdt@markPrice@1s",
    "data": {
        "e": "markPriceUpdate",
        "E": 1562305380000,
        "s": "BTCUSDT",
        "p": "11794.15000000",
        "i": "11784.62659091",
        "P": "11784.25641265",
        "r": "0.00038167",
        "T": 1562306400000,
    },
}

FORCE_ORDER_FRAME = {
    "stream": "!forceOrder@arr",
    "data": {
        "e": "forceOrder",
        "E": 1568014460893,
        "o": {
            "s": "BTCUSDT",
            "S": "SELL",
            "o": "LIMIT",
            "f": "IOC",
            "q": "0.014",
            "p": "9910",
            "ap": "9910",
            "X": "FILLED",
            "l": "0.014",
            "z": "0.014",
            "T": 1568014460893,
        },
    },
}


def test_split_frame_handles_combined_and_raw():
    stream, data = b.split_frame(BBO_FRAME)
    assert stream == "btcusdt@bookTicker"
    assert data["s"] == "BTCUSDT"

    raw = {"e": "markPriceUpdate", "s": "ETHUSDT", "p": "3000", "r": "0.0001", "T": 1}
    stream, data = b.split_frame(raw)
    assert stream is None
    assert data is raw

    stream, data = b.split_frame(b'{"stream":"x","data":{"e":"depthUpdate"}}')
    assert stream == "x"
    assert data == {"e": "depthUpdate"}


def test_handle_combined_bbo():
    table, row = b.handle_frame(BBO_FRAME, RECV_TS, RECV_MONO)
    assert table == "binance_bbo"
    assert row["symbol"] == "BTCUSDT"
    assert row["bid"] == 77493.70
    assert row["bid_qty"] == 13.794
    assert row["ask"] == 77493.80
    assert row["ask_qty"] == 0.460
    assert row["update_id"] == 12005260978562
    assert row["venue_ts_ms"] == 1793750749135
    assert row["recv_ts_ns"] == RECV_TS
    assert row["recv_mono_ns"] == RECV_MONO


def test_handle_combined_depth():
    table, row = b.handle_frame(DEPTH_FRAME, RECV_TS, RECV_MONO)
    assert table == "binance_depth"
    assert row["symbol"] == "BTCUSDT"
    assert row["last_update_id"] == 1200533398731
    assert row["venue_ts_ms"] == 1793750952399
    assert row["bids"] == [
        {"price": 77493.70, "size": 1.5},
        {"price": 77493.60, "size": 0.25},
    ]
    assert row["asks"][1] == {"price": 77493.90, "size": 0.5}


def test_handle_combined_mark_price():
    table, row = b.handle_frame(MARK_PRICE_FRAME, RECV_TS, RECV_MONO)
    assert table == "binance_funding"
    assert row["symbol"] == "BTCUSDT"
    assert row["funding_rate"] == 0.00038167
    assert row["mark_price"] == 11794.15
    assert row["index_price"] == 11784.62659091
    assert row["next_funding_time_ms"] == 1562306400000
    assert row["funding_time_ms"] == 1562306400000
    assert row["venue_ts_ms"] == 1562305380000


def test_handle_force_order():
    table, row = b.handle_frame(FORCE_ORDER_FRAME, RECV_TS, RECV_MONO)
    assert table == "binance_liquidation"
    assert row["symbol"] == "BTCUSDT"
    assert row["side"] == "SELL"
    assert row["price"] == 9910.0
    assert row["qty"] == 0.014
    assert row["order_type"] == "LIMIT"
    assert row["trade_time_ms"] == 1568014460893
    assert row["venue_ts_ms"] == 1568014460893


def test_raw_frames_route_by_event_type():
    table, row = b.handle_frame(DEPTH_FRAME["data"], RECV_TS, RECV_MONO)
    assert table == "binance_depth"
    assert row["last_update_id"] == 1200533398731

    table, row = b.handle_frame(MARK_PRICE_FRAME["data"], RECV_TS, RECV_MONO)
    assert table == "binance_funding"

    table, row = b.handle_frame(FORCE_ORDER_FRAME["data"], RECV_TS, RECV_MONO)
    assert table == "binance_liquidation"


def test_unknown_and_malformed_frames_are_ignored():
    assert b.handle_frame({"e": "24hrTicker", "s": "BTCUSDT"}, RECV_TS, RECV_MONO) is None
    assert b.handle_frame({}, RECV_TS, RECV_MONO) is None
    assert b.handle_frame(b"not json", RECV_TS, RECV_MONO) is None
    assert b.handle_frame(b"[]", RECV_TS, RECV_MONO) is None
    # A bookTicker without an ask cannot be a BBO.
    assert b.handle_frame({"e": "bookTicker", "s": "BTCUSDT", "b": "1"}, RECV_TS, RECV_MONO) is None


def test_premium_index_row():
    payload = {
        "symbol": "BTCUSDT",
        "markPrice": "77509.88519565",
        "indexPrice": "77514.83630435",
        "estimatedSettlePrice": "77771.60158599",
        "lastFundingRate": "0.00010000",
        "interestRate": "0.00010000",
        "nextFundingTime": 1793779200000,
        "time": 1793750428941,
    }
    row = b.parse_premium_index(payload, RECV_TS, RECV_MONO)
    assert row["symbol"] == "BTCUSDT"
    assert row["funding_rate"] == 0.0001
    assert row["mark_price"] == 77509.88519565
    assert row["index_price"] == 77514.83630435
    assert row["next_funding_time_ms"] == 1793779200000
    assert row["venue_ts_ms"] == 1793750428941
    assert b.parse_premium_index({}, RECV_TS, RECV_MONO) is None


def test_open_interest_row():
    payload = {"openInterest": "98714.849", "symbol": "BTCUSDT", "time": 1793750428941}
    row = b.parse_open_interest(payload, RECV_TS, RECV_MONO)
    assert row["symbol"] == "BTCUSDT"
    assert row["open_interest"] == 98714.849
    assert row["venue_ts_ms"] == 1793750428941
    assert b.parse_open_interest({"symbol": "BTCUSDT"}, RECV_TS, RECV_MONO) is None


def test_rest_depth_row():
    payload = {
        "lastUpdateId": 12005324790134,
        "E": 1793751310472,
        "T": 1793751310469,
        "bids": [["77319.80", "3.782"]],
        "asks": [["77319.90", "1.0"]],
    }
    row = b.parse_rest_depth(payload, "BTCUSDT", RECV_TS, RECV_MONO)
    assert row["symbol"] == "BTCUSDT"
    assert row["last_update_id"] == 12005324790134
    assert row["venue_ts_ms"] == 1793751310472
    assert row["bids"] == [{"price": 77319.80, "size": 3.782}]


def test_depth_interval_is_clamped_to_futures_speeds():
    assert b.depth_interval_ms(500) == 500
    assert b.depth_interval_ms(250) == 250
    assert b.depth_interval_ms(1000) == 500
    assert b.depth_interval_ms(100) == 250


def test_stream_names_and_combined_url():
    names = b.stream_names(["BTCUSDT"], 10, 500)
    assert names == [
        "btcusdt@bookTicker",
        "btcusdt@depth10@500ms",
        "btcusdt@markPrice@1s",
    ]
    url = b.combined_url("wss://fstream.binance.com/stream", names)
    assert url.startswith("wss://fstream.binance.com/stream?streams=")
    assert url.count("/") == 5


def test_futures_urls_are_derived_from_config():
    assert (
        b.futures_ws_url("wss://stream.binance.com:9443/stream")
        == "wss://fstream.binance.com:443/stream"
    )
    assert b.futures_ws_url("wss://fstream.binance.com/stream") == "wss://fstream.binance.com/stream"
    assert b.futures_rest_url("https://api.binance.com") == "https://fapi.binance.com"
    assert b.futures_rest_url("https://fapi.binance.com") == "https://fapi.binance.com"


def test_rows_match_arrow_schemas():
    bbo = b.handle_frame(BBO_FRAME, RECV_TS, RECV_MONO)[1]
    depth = b.handle_frame(DEPTH_FRAME, RECV_TS, RECV_MONO)[1]
    table = pa.Table.from_pylist([bbo], schema=BINANCE_BBO)
    assert table.num_rows == 1
    assert table.column("symbol").to_pylist() == ["BTCUSDT"]
    table = pa.Table.from_pylist([depth], schema=BINANCE_DEPTH)
    assert table.column("bids").to_pylist()[0][0] == {"price": 77493.70, "size": 1.5}
