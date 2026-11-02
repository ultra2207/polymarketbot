import json
import time
from pathlib import Path
import requests
from loguru import logger

OUTPUT_DIR = Path("data/processed")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "live_reward_head_pools.json"

CLOB_API = "https://clob.polymarket.com"

def fetch_top_reward_pools(session: requests.Session, limit: int = 12):
    url = f"{CLOB_API}/rewards/markets/multi"
    params = {
        "order_by": "rate_per_day",
        "position": "DESC",
        "page_size": limit,
    }
    logger.info("Fetching top reward markets from CLOB API...")
    resp = session.get(url, params=params, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    markets = data.get("data", [])
    logger.info(f"Retrieved {len(markets)} reward markets.")
    return markets

def fetch_order_book(session: requests.Session, token_id: str):
    url = f"{CLOB_API}/book"
    params = {"token_id": token_id}
    try:
        resp = session.get(url, params=params, timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 429:
            logger.warning("Rate limit (429) encountered. Backing off 5s...")
            time.sleep(5.0)
            return None
        else:
            logger.warning(f"Error fetching book for {token_id[:12]}: {resp.status_code}")
            return None
    except Exception as e:
        logger.warning(f"Request failed for {token_id[:12]}: {e}")
        return None

def analyze_qualifying_depth(book: dict, max_spread_cents: float):
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None

    best_bid = max(float(x["price"]) for x in bids)
    best_ask = min(float(x["price"]) for x in asks)
    mid = (best_bid + best_ask) / 2.0
    spread = best_ask - best_bid
    max_spread_val = max_spread_cents / 100.0

    # Orders within max_spread from midpoint
    qualifying_bid_depth = sum(float(x["size"]) for x in bids if (mid - float(x["price"])) <= max_spread_val)
    qualifying_ask_depth = sum(float(x["size"]) for x in asks if (float(x["price"]) - mid) <= max_spread_val)

    # Touch size
    touch_bid_sz = sum(float(x["size"]) for x in bids if abs(float(x["price"]) - best_bid) < 1e-6)
    touch_ask_sz = sum(float(x["size"]) for x in asks if abs(float(x["price"]) - best_ask) < 1e-6)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": round(mid, 4),
        "spread_cents": round(spread * 100, 2),
        "touch_bid_shares": round(touch_bid_sz, 1),
        "touch_ask_shares": round(touch_ask_sz, 1),
        "touch_bid_usd": round(touch_bid_sz * best_bid, 2),
        "touch_ask_usd": round(touch_ask_sz * best_ask, 2),
        "qualifying_bid_shares": round(qualifying_bid_depth, 1),
        "qualifying_ask_shares": round(qualifying_ask_depth, 1),
        "total_qualifying_shares": round(qualifying_bid_depth + qualifying_ask_depth, 1),
        "total_qualifying_usd": round(qualifying_bid_depth * best_bid + qualifying_ask_depth * best_ask, 2),
    }

def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "polymarketbot/research (rewards-audit)"})
    markets = fetch_top_reward_pools(session, limit=12)
    results = []

    for idx, m in enumerate(markets):
        q = m.get("question", "Unknown")
        cid = m.get("condition_id", "")
        rewards_cfg = m.get("rewards_config", [])
        rate = rewards_cfg[0].get("rate_per_day", 0) if rewards_cfg else 0
        min_size = m.get("rewards_min_size", 0)
        max_spread = m.get("rewards_max_spread", 0)
        tokens = m.get("tokens", [])

        books_analyzed = {}
        for tok in tokens[:2]:  # YES and NO
            tok_id = tok.get("token_id")
            outcome = tok.get("outcome", "unknown")
            if tok_id:
                time.sleep(0.5)  # polite throttle to strictly prevent 429
                book = fetch_order_book(session, tok_id)
                if book:
                    books_analyzed[outcome] = analyze_qualifying_depth(book, max_spread)

        logger.info(f"[{idx+1}/{len(markets)}] {q[:40]} | ${rate}/d | minSz={min_size} maxSpr={max_spread}c")
        results.append({
            "question": q,
            "condition_id": cid,
            "rate_per_day": rate,
            "rewards_min_size": min_size,
            "rewards_max_spread": max_spread,
            "books": books_analyzed,
            "timestamp": time.time()
        })

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Analysis saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
