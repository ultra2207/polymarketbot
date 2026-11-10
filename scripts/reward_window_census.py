"""Rewarded-universe marginal-yield census and the price of a book window.

Measurement only.

The live engine hands every pillar one rotating 12,000-token book window per
tick out of ~374,634 tokens, so the reward allocator solves over a rotating
fraction of its own universe and can allocate to the best market it can *see*
rather than the best that exists.  This script quantifies that loss from a full
book snapshot of every rewarded market:

1. Walk ``clob.polymarket.com/rewards/markets/multi`` (``order_by=rate_per_day``)
   to the end and keep every market with a live campaign and two token ids.
2. Snapshot both books of every kept market through
   ``polymarketbot.paper.venue.BookFeed``.
3. Build the allocator's own candidate dicts with
   ``polymarketbot.paper.pillars.reward.reward_candidates`` and price each one
   with the allocator's own marginal-yield function, at the capital its
   activation lot requires.  No formula is re-implemented here.
4. Resample the 19 % window the engine actually sees (~2,043-2,296 of 11,403
   rewarded markets, measured from the ``allocator prune:`` lines) and compare
   the best visible yield against the true best.

Missing books, missing campaign parameters and missing Gamma metadata are
refused and counted, never priced at a default.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from polymarketbot.logs import get_logger
from polymarketbot.paper.pillars.reward import reward_candidates
from polymarketbot.paper.venue import BookFeed, parse_market, session
from polymarketbot.rewards.allocator import (
    PROTOCOL_MIN_DAILY_PAYOUT_USD,
    LiquidityRewardsAllocator,
)

log = get_logger("reward_window")

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "derived" / "reward_window"
UNIVERSE = ROOT / "data" / "derived" / "ctf_census" / "universe.jsonl"
CAMPAIGNS = "https://clob.polymarket.com/rewards/markets/multi"

#: Measured cap of one ``POST /books`` request: a 400-token body returns 200
#: books, so 200 is the largest chunk that does not silently drop half the
#: requested books.
BOOKS_CHUNK = 200
#: The engine's own book budget per tick, and the measured rewarded-market
#: visibility it buys (``allocator prune:`` lines, window 9, 14:17-14:47 IST).
WINDOW_TOKENS = 12_000
WINDOW_FRACTION = 2_170 / 11_403  # midpoint of the measured 2,043-2,296
#: Capital the allocator is allowed to deploy. ``configs/windows.yaml`` sets
#: ``bankroll_usd: 2000``; this is that number, an input, not a measurement.
BANKROLL_USD = 2_000.0


def _now() -> float:
    return time.time()


def _parse_ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()


def _offset_cursor(offset: int) -> str:
    """The endpoint's ``next_cursor`` is the base64 of a plain row offset."""
    return base64.b64encode(str(offset).encode()).decode()


#: A market whose whole daily pool is below the protocol's minimum payout can
#: never clear the allocator's floor: its share is smaller than the pool.  The
#: endpoint is ordered by ``rate_per_day`` descending, so the first page whose
#: best rate is under this bound ends the part of the universe that can pay.
POOL_CUTOFF_USD_PER_DAY = PROTOCOL_MIN_DAILY_PAYOUT_USD
PAGE_SIZE = 500
#: Offsets are independent, so pages are fetched in parallel windows rather
#: than by following ``next_cursor`` serially: the deep pages take 3-8 s each
#: serially and would not fit a single 300 s run.
WALK_WORKERS = 8


def _campaign_page(http, offset: int) -> tuple[int, list[dict]]:
    params = {"order_by": "rate_per_day", "position": "DESC",
              "page_size": PAGE_SIZE, "next_cursor": _offset_cursor(offset)}
    try:
        response = http.get(CAMPAIGNS, params=params, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 - a failed page is a missing page
        log.warning(f"campaigns page {offset} failed: {exc}")
        return offset, []
    if response.status_code != 200:
        log.warning(f"campaigns page {offset} HTTP {response.status_code}")
        return offset, []
    try:
        return offset, (response.json().get("data") or [])
    except ValueError:
        return offset, []


def _best_rate(page: list[dict]) -> float:
    best = 0.0
    for row in page:
        for campaign in (row.get("rewards_config") or []):
            try:
                best = max(best, float(campaign.get("rate_per_day") or 0.0))
            except (TypeError, ValueError):
                continue
    return best


def walk_campaigns(max_pages: int = 120) -> tuple[list[dict], dict]:
    """Campaign rows down to the pool cutoff, fetched in parallel windows."""
    http = session()
    rows: list[dict] = []
    pages = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WALK_WORKERS) as pool:
        while pages < max_pages:
            offsets = [pages * PAGE_SIZE + i * PAGE_SIZE for i in range(WALK_WORKERS)]
            results = list(pool.map(lambda off: _campaign_page(http, off), offsets))
            stop = False
            for offset, page in results:
                pages = offset // PAGE_SIZE + 1
                if not page:
                    stop = True
                    break
                rows.extend(page)
                if _best_rate(page) < POOL_CUTOFF_USD_PER_DAY:
                    stop = True
                    break
            if pages % 20 < WALK_WORKERS:
                log.info(f"campaigns: {pages} pages, {len(rows)} rows, "
                         f"{time.time() - t0:.0f}s")
            if stop:
                break
    meta = {
        "pages": pages,
        "rows": len(rows),
        "pool_cutoff_usd_per_day": POOL_CUTOFF_USD_PER_DAY,
        "wall_s": round(time.time() - t0, 1),
        "min_rate_on_last_page": _best_rate(rows[-PAGE_SIZE:] if rows else []),
    }
    log.info(f"campaigns: {len(rows)} rows over {pages} pages, "
             f"cutoff ${POOL_CUTOFF_USD_PER_DAY:.2f}/day, {meta['wall_s']}s")
    return rows, meta


def live_markets(rows: list[dict], universe: dict[str, dict]) -> tuple[list, dict]:
    """Rewarded markets with a live campaign, joined to their Gamma row."""
    now = _now()
    markets = []
    refused = {"no_tokens": 0, "no_live_campaign": 0, "not_in_universe": 0,
               "unparseable_gamma": 0, "unpillarable": 0}
    for row in rows:
        tokens = [str(t.get("token_id")) for t in (row.get("tokens") or [])
                  if t.get("token_id")]
        if len(tokens) != 2:
            refused["no_tokens"] += 1
            continue
        campaigns = row.get("rewards_config") or []
        if isinstance(campaigns, dict):
            campaigns = [campaigns]
        daily = 0.0
        for campaign in campaigns:
            start = _parse_ts(campaign.get("start_date"))
            end = _parse_ts(campaign.get("end_date"))
            if start is not None and start > now:
                continue
            if end is not None and end < now:
                continue
            daily += float(campaign.get("rate_per_day") or 0.0)
        if daily <= 0.0:
            refused["no_live_campaign"] += 1
            continue
        condition_id = str(row.get("condition_id") or "")
        gamma = universe.get(condition_id)
        if gamma is None:
            refused["not_in_universe"] += 1
            continue
        market = parse_market(gamma)
        if market is None or len(market.token_ids) != 2:
            refused["unparseable_gamma"] += 1
            continue
        market = replace(
            market,
            token_ids=(tokens[0], tokens[1]),
            daily_reward=daily,
            reward_max_spread_cents=float(row.get("rewards_max_spread") or 0.0),
            reward_min_size=float(row.get("rewards_min_size") or 0.0),
        )
        if market.tick <= 0.0 or market.reward_max_spread_cents <= 0.0 \
                or market.reward_min_size <= 0.0:
            refused["unpillarable"] += 1
            continue
        markets.append(market)
    return markets, refused


def _snapshot(http, markets, chunk: int, workers: int) -> tuple[dict, float]:
    """Books for every market's two tokens; one fresh feed per 800 markets."""
    books: dict[str, dict] = {}
    t0 = time.time()
    group = 800
    for start in range(0, len(markets), group):
        part = markets[start:start + group]
        tokens = [token for market in part for token in market.token_ids]
        feed = BookFeed(http, ttl=0.0, chunk=chunk, workers=workers, max_age=5.0)
        books.update(feed.books(tokens, force=True))
    return books, time.time() - t0


def price_candidates(candidates: list[dict], allocator: LiquidityRewardsAllocator,
                     bankroll: float) -> list[dict]:
    """Each candidate's marginal yield at the capital its activation lot needs."""
    priced = []
    for cand in candidates:
        p = float(cand["daily_pool"])
        d = float(cand["competing_score"])
        alpha = allocator.calculate_alpha(
            float(cand["max_spread"]), float(cand["spread_offset_cents"]))
        alpha *= float(cand.get("score_per_usd", 1.0))
        if alpha <= 0.0 or p <= 0.0:
            continue
        c_floor = allocator.calculate_min_floor_capital(
            p, d, alpha, floor_target=PROTOCOL_MIN_DAILY_PAYOUT_USD)
        lot = float(cand["min_size"]) * float(cand.get("lot_price", cand["real_mid"]))
        c_min = max(lot, c_floor)
        if not math.isfinite(c_min) or c_min <= 0.0 or c_min > bankroll:
            continue
        priced.append({
            "condition_id": cand["condition_id"],
            "question": cand["question"][:80],
            "pool_per_day": p,
            "competing_score": d,
            "alpha": alpha,
            "c_min": c_min,
            "marginal_per_usd_day": allocator.marginal_payout(c_min, p, d, alpha),
            "mid": float(cand["real_mid"]),
            "lot_price": float(cand.get("lot_price", cand["real_mid"])),
            "min_size": float(cand["min_size"]),
        })
    return priced


def _q(values: list[float], frac: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    pos = frac * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _payout(cand: dict, capital: float) -> float:
    """The allocator's own payout function ``P * alpha*c / (D + alpha*c)``."""
    denom = cand["competing_score"] + cand["alpha"] * capital
    if denom <= 0.0:
        return 0.0
    return cand["pool_per_day"] * cand["alpha"] * capital / denom


def greedy_payout(priced: list[dict], budget: float, steps: int = 120) -> dict:
    """Greedy marginal water-filling, the allocator's objective without the MILP.

    Activation first, cheapest lot first while the budget allows (that is the
    allocator's own ``k_max`` rule), then capital in equal steps to whichever
    active market has the highest marginal payout.  Labelled an approximation:
    the engine solves this with SCIP, so the levels here are a lower bound on
    what a correct solve earns, but the *difference* between two candidate sets
    priced by the same rule is what is being measured.
    """
    active: list[dict] = []
    spent = 0.0
    for cand in sorted(priced, key=lambda c: c["c_min"]):
        if spent + cand["c_min"] > budget:
            break
        spent += cand["c_min"]
        active.append({**cand, "capital": cand["c_min"]})
    if not active:
        return {"active": 0, "capital": 0.0, "payout_per_day": 0.0}
    step = max(0.01, (budget - spent) / steps)
    for _ in range(steps):
        if spent + step > budget:
            break
        best = None
        best_marginal = -1.0
        for cand in active:
            alpha = cand["alpha"]
            denom = cand["competing_score"] + alpha * cand["capital"]
            marginal = cand["pool_per_day"] * alpha * cand["competing_score"] / (denom * denom)
            if marginal > best_marginal:
                best_marginal = marginal
                best = cand
        if best is None or best_marginal <= 0.0:
            break
        best["capital"] += step
        spent += step
    total = sum(_payout(c, c["capital"]) for c in active)
    return {"active": len(active), "capital": spent, "payout_per_day": total,
            "best_marginal_at_optimum": best_marginal}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draws", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--chunk", type=int, default=BOOKS_CHUNK)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    t_start = time.time()
    universe: dict[str, dict] = {}
    for line in UNIVERSE.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        universe[row["conditionId"]] = row
    log.info(f"gamma universe: {len(universe)} markets")

    rows, campaign_meta = walk_campaigns()
    markets, refused = live_markets(rows, universe)
    log.info(f"rewarded markets with a live campaign and two tokens: {len(markets)} "
             f"(refused: {refused})")

    http = session()
    books, snapshot_s = _snapshot(http, markets, args.chunk, args.workers)
    log.info(f"books: {len(books)} of {2 * len(markets)} tokens in {snapshot_s:.1f}s")

    allocator = LiquidityRewardsAllocator()
    candidates = reward_candidates(markets, books, [])
    log.info(f"allocator candidates (quoted inside the band): {len(candidates)}")
    priced = price_candidates(candidates, allocator, BANKROLL_USD)
    log.info(f"priced with an activation lot inside ${BANKROLL_USD:,.0f}: {len(priced)}")

    marginal = [c["marginal_per_usd_day"] for c in priced]
    full = {
        "candidates": len(candidates),
        "priced": len(priced),
        "refused": refused,
        "marginal_per_usd_day": {
            "min": min(marginal) if marginal else 0.0,
            "median": statistics.median(marginal) if marginal else 0.0,
            "p90": _q(marginal, 0.90),
            "p99": _q(marginal, 0.99),
            "max": max(marginal) if marginal else 0.0,
        },
        "positive_marginal": sum(1 for v in marginal if v > 0.0),
        "total_pool_per_day_all_candidates": sum(c["pool_per_day"] for c in priced),
    }

    # Shadow yield floor: the allocator drops everything under the marginal
    # yield of the k_max-th ranked market, k_max being how many activation lots
    # the capital can fund.  Usable capital is the configured bankroll, stated
    # as an input above.
    ranked = sorted(priced, key=lambda c: c["marginal_per_usd_day"], reverse=True)
    spent = 0.0
    k_max = 0
    for cand in sorted(priced, key=lambda c: c["c_min"]):
        if spent + cand["c_min"] > BANKROLL_USD:
            break
        spent += cand["c_min"]
        k_max += 1
    shadow_floor = ranked[k_max - 1]["marginal_per_usd_day"] if 0 < k_max <= len(ranked) else None
    fundable = [c for c in ranked if shadow_floor is not None
                and c["marginal_per_usd_day"] >= shadow_floor]
    full["shadow_floor"] = {
        "k_max": k_max,
        "capital_used": spent,
        "shadow_yield_floor_per_usd_day": shadow_floor,
        "fundable_markets": len(fundable),
        "fundable_pool_per_day": sum(c["pool_per_day"] for c in fundable),
        "best_marginal": ranked[0]["marginal_per_usd_day"] if ranked else 0.0,
    }

    rng = random.Random(args.seed)
    n_markets = len(markets)
    window_n = max(1, int(round(n_markets * WINDOW_FRACTION)))
    draws = []
    for _ in range(args.draws):
        visible = set(rng.sample(range(n_markets), window_n))
        visible_ids = {markets[i].condition_id for i in visible}
        seen = [c for c in priced if c["condition_id"] in visible_ids]
        best = max((c["marginal_per_usd_day"] for c in seen), default=0.0)
        fundable_seen = sum(1 for c in fundable if c["condition_id"] in visible_ids)
        draws.append({
            "window_markets": window_n,
            "visible_candidates": len(seen),
            "best_marginal": best,
            "fundable_visible": fundable_seen,
            "_visible_ids": visible_ids,
        })
    full_greedy = greedy_payout(priced, BANKROLL_USD)
    for draw in draws:
        visible_set = draw.pop("_visible_ids", None) or set()
        seen = [c for c in priced if c["condition_id"] in visible_set]
        draw["greedy"] = greedy_payout(seen, BANKROLL_USD)
    payout_gaps = [full_greedy["payout_per_day"] - d["greedy"]["payout_per_day"]
                   for d in draws]
    best_full = full["shadow_floor"]["best_marginal"]
    gaps = [best_full - d["best_marginal"] for d in draws]
    ratios = [(d["best_marginal"] / best_full) if best_full > 0 else 0.0 for d in draws]
    window = {
        "window_markets": window_n,
        "window_fraction": WINDOW_FRACTION,
        "draws": draws,
        "best_full": best_full,
        "best_window_median": statistics.median(d["best_marginal"] for d in draws),
        "gap_median": statistics.median(gaps),
        "gap_p90": _q(gaps, 0.90),
        "ratio_median": statistics.median(ratios),
        "fundable_visible_median": statistics.median(d["fundable_visible"] for d in draws),
        "fundable_total": len(fundable),
        "payout_per_day_full": full_greedy["payout_per_day"],
        "payout_per_day_window_median": statistics.median(
            d["greedy"]["payout_per_day"] for d in draws),
        "payout_gap_per_day_median": statistics.median(payout_gaps),
        "payout_ratio_median": statistics.median(
            (d["greedy"]["payout_per_day"] / full_greedy["payout_per_day"])
            if full_greedy["payout_per_day"] > 0 else 0.0 for d in draws),
    }

    # What full coverage would cost: every rewarded market's two books, at the
    # measured 200-token request cap.
    tokens = 2 * n_markets
    chunks = math.ceil(tokens / BOOKS_CHUNK)
    rounds = math.ceil(chunks / args.workers)
    per_round = snapshot_s / max(1, rounds)
    window_chunks = math.ceil(WINDOW_TOKENS / BOOKS_CHUNK)
    window_rounds = math.ceil(window_chunks / args.workers)
    full_cost = {
        "tokens": tokens,
        "chunks_at_200": chunks,
        "workers": args.workers,
        "measured_seconds": round(snapshot_s, 1),
        "measured_seconds_per_round": round(per_round, 3),
        "window_tokens": WINDOW_TOKENS,
        "window_chunks_at_200": window_chunks,
        "window_seconds_est": round(window_rounds * per_round, 1),
        "coverage_multiple": round(tokens / WINDOW_TOKENS, 2),
        "chunk_note": "POST /books returns at most 200 books per request; a "
                      "400-token body comes back with 200 books (measured)",
    }

    report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rewarded_markets": n_markets,
        "campaign_rows": len(rows),
        "campaign_walk": campaign_meta,
        "books": len(books),
        "tokens_measured_in_books": 2 * n_markets,
        "bankroll_usd_assumption": BANKROLL_USD,
        "full": full,
        "window": window,
        "coverage_cost": full_cost,
        "wall_s": round(time.time() - t_start, 1),
    }
    (OUT / "reward_window.json").write_text(json.dumps(report, indent=1))
    log.info(json.dumps({k: report[k] for k in
                         ("rewarded_markets", "books", "full", "window",
                          "coverage_cost", "wall_s")}, indent=1)[:4000])


if __name__ == "__main__":
    main()
