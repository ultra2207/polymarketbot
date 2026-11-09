#!/usr/bin/env python3
"""Benchmark Hot Execution Paths in Python for OCI Amsterdam VM deployment.

Measures latency of:
1. FastEIP712Signer order hashing & ECDSA signing (pure Python vs C-lib/coincurve).
2. LatencySniper decision pipeline (velocity calculation, book evaluation, fee curve, sizing).
3. CLOB L2 Auth HMAC-SHA256 signature generation.
4. JSON serialization / deserialization (standard json vs orjson if available).
5. Queue valuation & OFI computation.
"""

import hashlib
import hmac
import json
import statistics
import time
from typing import Dict, List

import numpy as np
from polymarketbot.logs import get_logger
log = get_logger(__name__)

# 1. Measure L2 Auth HMAC
def bench_l2_auth_hmac(iterations: int = 1000) -> Dict[str, float]:
    secret = b"my_secret_api_key_32_bytes_long!!"
    timestamp = "1793850000"
    method = "POST"
    path = "/order"
    body = '{"order":{"salt":12345,"maker":"0x1234","signer":"0x1234"},"owner":"0x1234"}'
    
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        msg = f"{timestamp}{method}{path}{body}".encode("utf-8")
        h = hmac.new(secret, msg, hashlib.sha256).digest()
        t1 = time.perf_counter_ns()
        samples.append((t1 - t0) / 1000.0)  # microseconds
    
    return {
        "p50_us": round(statistics.median(samples), 2),
        "p99_us": round(np.percentile(samples, 99), 2),
        "mean_us": round(statistics.mean(samples), 2),
    }

# 2. Measure FastEIP712Signer
def bench_eip712_signing(iterations: int = 200) -> Dict[str, float]:
    from polymarketbot.risk.fast_signer import FastEIP712Signer, FastOrder
    
    priv_key = "0x" + "01" * 32
    signer = FastEIP712Signer(priv_key)
    order = FastOrder(
        salt=123456789,
        maker="0x1111111111111111111111111111111111111111",
        signer="0x1111111111111111111111111111111111111111",
        taker="0x0000000000000000000000000000000000000000",
        tokenId=99999999999999999999,
        makerAmount=1000000,
        takerAmount=500000,
        expiration=1800000000,
        nonce=1,
        feeRateBps=0,
        side=0,
        signatureType=0,
    )
    
    # Warmup
    signer.sign_order(order)
    
    samples_hash = []
    samples_sign = []
    samples_total = []
    
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        digest = signer.hash_order(order)
        t1 = time.perf_counter_ns()
        sig = signer.sign_order(order)
        t2 = time.perf_counter_ns()
        
        samples_hash.append((t1 - t0) / 1000.0)
        samples_sign.append((t2 - t1) / 1000.0)
        samples_total.append((t2 - t0) / 1000.0)
        
    return {
        "hash_p50_us": round(statistics.median(samples_hash), 2),
        "hash_p99_us": round(np.percentile(samples_hash, 99), 2),
        "sign_p50_us": round(statistics.median(samples_sign), 2),
        "sign_p99_us": round(np.percentile(samples_sign, 99), 2),
        "total_p50_ms": round(statistics.median(samples_total) / 1000.0, 3),
        "total_p99_ms": round(np.percentile(samples_total, 99) / 1000.0, 3),
    }

# 3. Measure LatencySniper Decision Engine
def bench_latency_sniper(iterations: int = 1000) -> Dict[str, float]:
    from polymarketbot.trading.latency_sniper import (
        BinanceLatencySniperEngine,
        LatencySniperConfig,
        PolymarketTopBook,
    )
    
    engine = BinanceLatencySniperEngine(LatencySniperConfig(min_jump_bps=25.0, vip_tier="none"))
    now_ms = time.time() * 1000.0
    
    # Populate trailing ticks
    for i in range(10):
        engine.record_binance_price(68000.0 + i * 10.0, timestamp_ms=now_ms - (10 - i) * 50)
    
    book = PolymarketTopBook(
        token_id="0x12345678",
        best_bid=0.48,
        best_bid_size=1000.0,
        best_ask=0.50,
        best_ask_size=800.0,
        timestamp_ms=now_ms - 200,
    )
    
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        # record tick, calc velocity, evaluate snipe
        engine.record_binance_price(68250.0, timestamp_ms=now_ms)
        dec = engine.evaluate_snipe(book=book, current_time_ms=now_ms)
        t1 = time.perf_counter_ns()
        samples.append((t1 - t0) / 1000.0)
        
    return {
        "p50_us": round(statistics.median(samples), 2),
        "p99_us": round(np.percentile(samples, 99), 2),
        "mean_us": round(statistics.mean(samples), 2),
    }

# 4. Measure JSON parsing & serialization
def bench_json(iterations: int = 1000) -> Dict[str, float]:
    sample_ws_payload = json.dumps({
        "event_type": "price_change",
        "market": "0x1234567890abcdef",
        "asset_id": "9876543210",
        "price": "0.52",
        "size": "250.0",
        "side": "BUY",
        "timestamp": 1793850000123,
        "hash": "0xabcdef1234567890",
    })
    
    samples_parse = []
    samples_dump = []
    
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        d = json.loads(sample_ws_payload)
        t1 = time.perf_counter_ns()
        s = json.dumps(d)
        t2 = time.perf_counter_ns()
        
        samples_parse.append((t1 - t0) / 1000.0)
        samples_dump.append((t2 - t1) / 1000.0)
        
    # Check if orjson is available
    has_orjson = False
    orjson_p50 = None
    try:
        import orjson
        has_orjson = True
        samples_orjson = []
        b_payload = sample_ws_payload.encode("utf-8")
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            d = orjson.loads(b_payload)
            s = orjson.dumps(d)
            t1 = time.perf_counter_ns()
            samples_orjson.append((t1 - t0) / 1000.0)
        orjson_p50 = round(statistics.median(samples_orjson), 2)
    except ImportError:
        pass
        
    res = {
        "json_parse_p50_us": round(statistics.median(samples_parse), 2),
        "json_parse_p99_us": round(np.percentile(samples_parse, 99), 2),
        "json_dump_p50_us": round(statistics.median(samples_dump), 2),
        "orjson_available": has_orjson,
    }
    if orjson_p50:
        res["orjson_total_p50_us"] = orjson_p50
    return res

if __name__ == "__main__":
    log.info("--- BENCHMARKING HOT EXECUTION PATHS IN PYTHON ---")
    t_start = time.time()
    
    hmac_res = bench_l2_auth_hmac()
    log.info('{!s} {!s}', "1. L2 Auth HMAC-SHA256:", hmac_res)
    
    sniper_res = bench_latency_sniper()
    log.info('{!s} {!s}', "2. LatencySniper Decision Path:", sniper_res)
    
    json_res = bench_json()
    log.info('{!s} {!s}', "3. JSON Serialization/Parsing:", json_res)
    
    eip712_res = bench_eip712_signing()
    log.info('{!s} {!s}', "4. EIP-712 Order Hashing & ECDSA Signing:", eip712_res)
    
    log.info(f"\nBenchmark completed in {time.time() - t_start:.2f}s")
