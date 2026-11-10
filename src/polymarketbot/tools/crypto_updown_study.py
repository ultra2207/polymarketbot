"""Settlement-harvesting feasibility study on Polymarket crypto up/down markets.

Reproducible pipeline (kept, not scratch).  Sub-commands, each writing one file
per unit of work and skipping units already written:

    scan      per (date, hour) CLOB tape partition -> the up/down markets seen there
    markets   union of scan + universe snapshots + CLOB metadata -> markets.parquet
    outcomes  realised outcome per market (CLOB `winner`; Binance-kline cross-check
              for the hourly markets, which settle on the Binance 1H candle)
    books     per partition: top-of-book / depth at T-300/-60/-30/-10 s (+ latency)
    report    calibration, threshold sweep net of fee, latency -> summary.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

# This pipeline lives under src/polymarketbot/tools/, so the repo root is three
# parents up (it started life in scripts/ and used parents[1]).
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from polymarketbot.logs import get_logger  # noqa: E402

TAPE = ROOT / "data" / "tape"
OUT = ROOT / "data" / "derived" / "crypto_updown_study"
CLOB_META = OUT / "clob_meta"
KLINES = OUT / "klines"
BOOKS = OUT / "books"
SEEN = OUT / "tape_markets"

# decision offsets before expiry (ms); 9500/9000 are "decided at T-10s, filled 0.5 s / 1.0 s later"
OFFSETS = ((300_000, "T-300s"), (60_000, "T-60s"), (30_000, "T-30s"),
           (10_000, "T-10s"), (9_500, "T-9.5s"), (9_000, "T-9s"))

CRYPTO = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT"}
ALIAS = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana"}

HOURLY = re.compile(
    r"^(?P<asset>[a-z]+)-up-or-down-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})-"
    r"(?P<hour>\d{1,2})(?P<ampm>am|pm)-et$")
BUCKET = re.compile(r"^(?P<asset>btc|[a-z]+)-updown-(?P<mins>\d+)m-(?P<unix>\d+)$")
RANGE = re.compile(r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<a1>am|pm)\s*[-–]\s*"
                   r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<a2>am|pm)", re.I)
SINGLE = re.compile(r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<a>am|pm)", re.I)

ET = ZoneInfo("America/New_York")
log = get_logger("crypto_updown_study")


# --------------------------------------------------------------------------
# window parsing
# --------------------------------------------------------------------------

def _et_ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ET).timestamp() * 1000)


def classify(slug: str, question: str) -> dict | None:
    """slug/question -> {asset, interval_minutes, window_start_ms}; None if not up/down."""
    slug = (slug or "").strip().lower()
    m = BUCKET.match(slug)
    if m:
        return {"asset": ALIAS.get(m["asset"], m["asset"]),
                "interval_minutes": int(m["mins"]), "window_start_ms": int(m["unix"]) * 1000}
    if "up-or-down" in slug or "updown" in slug or "up or down" in (question or "").lower():
        return {"asset": ALIAS.get(slug.split("-")[0], slug.split("-")[0]),
                "interval_minutes": None, "window_start_ms": None}
    return None


def hourly_window(question: str) -> tuple[int, int] | None:
    """'Bitcoin Up or Down - November 6, 12AM ET' (or '6:55AM-7:00AM') -> (start_ms, minutes)."""
    q = question or ""
    m = re.search(r"-\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(.*)$", q)
    if not m:
        return None
    month_name, day, rest = m.group(1), int(m.group(2)), m.group(3)
    months = "january february march april may june july august september october november december".split()
    if month_name.lower() not in months:
        return None
    month = months.index(month_name.lower()) + 1
    year = int(re.search(r"(20\d\d)", q).group(1)) if re.search(r"(20\d\d)", q) else \
        datetime.now(timezone.utc).year
    def hr(h: str, a: str) -> int:
        h = int(h) % 12
        return h + (12 if a.lower() == "pm" else 0)
    r = RANGE.search(rest)
    if r:
        start = _et_ms(year, month, day, hr(r["h1"], r["a1"]), int(r["m1"] or 0))
        end = _et_ms(year, month, day, hr(r["h2"], r["a2"]), int(r["m2"] or 0))
        while end <= start:  # a range that crosses midnight
            end += 86_400_000
        return start, int((end - start) / 60000)
    s = SINGLE.search(rest)
    if s:
        start = _et_ms(year, month, day, hr(s["h"], s["a"]), int(s["m"] or 0))
        return start, 60
    return None


# --------------------------------------------------------------------------
# 1. scan the tape for up/down markets
# --------------------------------------------------------------------------

FIELDS = ("slug", "question", "outcomes", "clob_token_ids", "order_price_min_tick_size",
          "fee_schedule", "fees_enabled", "description")


def _partitions() -> list[tuple[str, str, Path]]:
    out = []
    for p in sorted((TAPE / "clob_events").glob("date=*/hour=*")):
        out.append((p.parent.name.split("=")[1], p.name.split("=")[1], p))
    return out


def cmd_scan(args) -> None:
    SEEN.mkdir(parents=True, exist_ok=True)
    parts = _partitions()
    todo = [p for p in parts if not (SEEN / f"date={p[0]}" / f"hour={p[1]}.parquet").exists()]
    log.info(f"scan: {len(parts)} partitions, {len(todo)} to do")
    con = duckdb.connect()
    con.execute("set threads=4")
    for i, (date, hour, path) in enumerate(todo, 1):
        t = time.time()
        dest = SEEN / f"date={date}" / f"hour={hour}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        cols = ", ".join(f"json_extract_string(raw_json,'$.{f}') AS {f}" for f in FIELDS)
        sql = f"""
        copy (
          select distinct condition_id, {cols},
                 '{date}' AS date, '{hour}' AS hour
          from read_parquet('{path.as_posix()}/*.parquet')
          where event_type = 'new_market'
            and condition_id is not null
            and (json_extract_string(raw_json,'$.slug') like '%up-or-down%'
                 or json_extract_string(raw_json,'$.slug') like '%updown%')
            and coalesce(json_extract_string(raw_json,'$.slug'),'') <> ''
        ) to '{dest.as_posix()}' (format parquet)
        """
        con.execute(sql)
        n = con.execute(f"select count(*) from '{dest.as_posix()}'").fetchone()[0]
        log.info(f"scan {date}/{hour}: {n} updown markets in {time.time() - t:.1f}s "
                 f"[{i}/{len(todo)}]")


def load_seen() -> "duckdb.DuckDBPyRelation":
    con = duckdb.connect()
    return con.sql(f"select * from '{SEEN.as_posix()}/date=*/hour=*.parquet'")


# --------------------------------------------------------------------------
# 2. market table
# --------------------------------------------------------------------------

def fetch_clob_meta(cid: str, tries: int = 4) -> dict | None:
    path = CLOB_META / f"{cid}.json"
    if path.exists():
        try:
            d = json.loads(path.read_text())
            return None if d.get("__missing") else d
        except Exception:
            path.unlink(missing_ok=True)
    url = f"https://clob.polymarket.com/markets/{cid}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "polymarketbot-research/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
            path.write_text(json.dumps(d))
            return d
        except urllib.error.HTTPError as e:
            if e.code == 404:
                path.write_text(json.dumps({"__missing": True}))
                return None
            time.sleep(1.2 * (attempt + 1))
        except Exception:
            time.sleep(1.2 * (attempt + 1))
    log.warning(f"clob metadata failed cid={cid}")
    return None


def _fs(raw) -> dict:
    """Tape fee_schedule JSON -> {rate, exponent, rebate_rate, taker_only} as floats."""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    out = {}
    for k in ("rate", "exponent", "rebate_rate"):
        if d.get(k) is not None:
            try:
                out[k] = float(d[k])
            except (TypeError, ValueError):
                pass
    if d.get("taker_only") is not None:
        out["taker_only"] = bool(d["taker_only"])
    return out


def cmd_markets(args) -> None:
    CLOB_META.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    rows = con.sql(f"""
        select condition_id, any_value(slug) slug, any_value(question) question,
               any_value(outcomes) outcomes, any_value(clob_token_ids) clob_token_ids,
               any_value(order_price_min_tick_size) tick, any_value(fee_schedule) fee_schedule,
               any_value(fees_enabled) fees_enabled, any_value(description) rules,
               min(date) first_date, max(date) last_date,
               count(distinct date || hour) n_partitions,
               list(distinct date || '/' || hour order by date || '/' || hour) partitions
        from '{SEEN.as_posix()}/date=*/hour=*.parquet'
        group by 1 order by 2
    """).df()
    log.info(f"markets: {len(rows)} distinct up/down condition_ids in the tape")
    t = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        metas = list(ex.map(lambda c: fetch_clob_meta(c), rows.condition_id))
    log.info(f"clob metadata {sum(1 for m in metas if m)}/{len(rows)} in {time.time() - t:.0f}s")

    recs = []
    for (_, r), meta in zip(rows.iterrows(), metas):
        cls = classify(r.slug, r.question) or {}
        if cls.get("interval_minutes") is None:
            hw = hourly_window(r.question)
            if hw is None:
                log.warning(f"cannot parse window: {r.slug} | {r.question}")
                continue
            start_ms, minutes = hw
        else:
            start_ms, minutes = cls["window_start_ms"], cls["interval_minutes"]
        asset = cls.get("asset")
        if asset not in CRYPTO:
            continue
        up = down = winner = None
        if meta:
            outs = [str(x.get("outcome", "")).lower() for x in meta.get("tokens") or []]
            for tok in meta.get("tokens") or []:
                label = str(tok.get("outcome", "")).lower()
                if label == "up" or label == "yes":
                    up = tok["token_id"]
                    winner = bool(tok.get("winner"))
                elif label == "down" or label == "no":
                    down = tok["token_id"]
        else:
            outs = []
        if up is None and r.outcomes:
            try:
                outs = json.loads(r.outcomes)
                ids = json.loads(r.clob_token_ids)
                if len(outs) == 2 and len(ids) == 2:
                    for label, tid in zip(outs, ids):
                        if str(label).lower() in ("up", "yes"):
                            up = tid
                        else:
                            down = tid
            except Exception:
                pass
        if up is None:
            log.warning(f"no UP token for {r.slug} (outcomes={outs})")
            continue
        tick = float(r.tick) if r.tick else 0.001
        recs.append({
            "condition_id": r.condition_id, "slug": r.slug, "question": r.question,
            "asset": asset, "interval_minutes": int(minutes),
            "window_start_ms": int(start_ms),
            "window_end_ms": int(start_ms) + int(minutes) * 60_000,
            "up_token": up, "down_token": down, "winner_up": winner,
            "tick_size": tick,
            "fee_rate": (_fs(r.fee_schedule).get("rate")),
            "fee_exponent": (_fs(r.fee_schedule).get("exponent")),
            "fee_rebate": (_fs(r.fee_schedule).get("rebate_rate")),
            "fee_taker_only": (_fs(r.fee_schedule).get("taker_only")),
            "fees_enabled": r.fees_enabled == "true",
            "rules": r.rules,
            "settles_on": ("binance_1h_candle" if "binance" in (r.rules or "").lower()
                           else "chainlink_twap" if "chainlink" in (r.rules or "").lower()
                           else "unknown"),
            "first_date": r.first_date, "last_date": r.last_date,
            "n_partitions": int(r.n_partitions),
            "partitions": list(r.partitions),
        })
    # ---- union with the universe snapshots (the set the recorder actually subscribed) ----
    have = {r["condition_id"] for r in recs}
    uni: dict[str, dict] = {}
    for f in sorted((TAPE / "universe").glob("20*.json")):
        for m in json.loads(f.read_text())["markets"]:
            slug = (m.get("slug") or "").strip().lower()
            if not ("up-or-down" in slug or "updown" in slug):
                continue
            cls = classify(slug, m.get("question", ""))
            if cls is None or cls.get("asset") not in CRYPTO:
                continue
            uni.setdefault(m["condition_id"], {
                "slug": m.get("slug"), "question": m.get("question"),
                "asset": cls["asset"], "interval_minutes": cls["interval_minutes"],
                "window_start_ms": cls["window_start_ms"], "end_date": m.get("end_date"),
                "tokens": m.get("clob_tokens") or [], "tick_size": m.get("tick_size"),
                "fee_rate": m.get("fee_rate"), "fee_exponent": m.get("fee_exponent"),
                "fee_rebate": m.get("fee_rebate"), "fee_type": m.get("fee_type"),
            })
    new = {k: v for k, v in uni.items() if k not in have}
    log.info(f"universe updown crypto cids={len(uni)}, not in tape list={len(new)}")
    t = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        metas = list(ex.map(lambda c: fetch_clob_meta(c), list(new)))
    log.info(f"universe metadata {sum(1 for x in metas if x)}/{len(new)} in {time.time() - t:.0f}s")
    for (cid, u), meta in zip(new.items(), metas):
        up = down = winner = None
        rules = None
        if meta:
            rules = meta.get("description")
            for tok in meta.get("tokens") or []:
                lab = str(tok.get("outcome", "")).lower()
                if lab in ("up", "yes"):
                    up, winner = tok["token_id"], bool(tok.get("winner"))
                elif lab in ("down", "no"):
                    down = tok["token_id"]
        if up is None and len(u["tokens"]) == 2:
            up, down = None, None
            continue
        if u["interval_minutes"] is not None and u["window_start_ms"] is not None:
            start = u["window_start_ms"]
            minutes = u["interval_minutes"]
        else:
            hw = hourly_window(u["question"] or "")
            if hw is None:
                continue
            start, minutes = hw
        recs.append({
            "condition_id": cid, "slug": u["slug"], "question": u["question"],
            "asset": u["asset"], "interval_minutes": int(minutes),
            "window_start_ms": int(start),
            "window_end_ms": int(start) + int(minutes) * 60_000,
            "up_token": up, "down_token": down, "winner_up": winner,
            "tick_size": float(u["tick_size"] or 0.01),
            "fee_rate": u["fee_rate"], "fee_exponent": u["fee_exponent"],
            "fee_rebate": u["fee_rebate"], "fee_taker_only": True,
            "fees_enabled": True, "rules": rules,
            "settles_on": ("binance_1h_candle" if "binance" in (rules or "").lower()
                           else "chainlink_twap" if "chainlink" in (rules or "").lower()
                           else "unknown"),
            "first_date": None, "last_date": None, "n_partitions": 0, "partitions": [],
        })
    have = {r["condition_id"] for r in recs}
    recs = [r for r in recs if r["up_token"]]
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.Table.from_pylist(recs), OUT / "markets.parquet")
    import collections
    c = collections.Counter((x["asset"], x["interval_minutes"]) for x in recs)
    log.info(f"markets.parquet rows={len(recs)} with_winner={sum(1 for x in recs if x['winner_up'] is not None)}")
    log.info(f"by asset/interval: {dict(sorted(c.items()))}")
    log.info("settles_on: " + str(dict(collections.Counter(x["settles_on"] for x in recs))))
    log.info("fee(taker_only): " + str(dict(collections.Counter(
        (x["fee_rate"], x["fee_exponent"], x["fee_rebate"], x["fee_taker_only"]) for x in recs))))
    log.info("fee_rate/exponent: " + str(dict(collections.Counter((x["fee_rate"], x["fee_exponent"]) for x in recs))))


# --------------------------------------------------------------------------
# 3. outcomes (Binance kline cross-check, hourly markets only)
# --------------------------------------------------------------------------

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    path = KLINES / f"{symbol}_{interval}_{day}.json"
    if path.exists():
        return json.loads(path.read_text())
    out, cursor = [], start_ms
    while cursor <= end_ms:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cursor}&limit=500")
        req = urllib.request.Request(url, headers={"User-Agent": "polymarketbot-research/0.1"})
        with urllib.request.urlopen(req, timeout=25) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1][0] + 1
        time.sleep(0.12)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out))
    return out


def cmd_outcomes(args) -> None:
    con = duckdb.connect()
    rows = con.sql(f"select * from '{OUT.as_posix()}/markets.parquet' "
                   f"where interval_minutes = 60 and settles_on = 'binance_1h_candle' "
                   f"order by window_start_ms").df()
    log.info(f"outcomes: {len(rows)} hourly Binance-settled markets")
    cache: dict[tuple[str, str], dict[int, list]] = {}
    out = []
    for _, r in rows.iterrows():
        key = (CRYPTO[r.asset], "1h")
        cache.setdefault(key, {})
        start = int(r.window_start_ms)
        if start not in cache[key]:
            ks = fetch_klines(key[0], key[1], start - 3_600_000, int(r.window_end_ms) + 3_600_000)
            for k in ks:
                cache[key][int(k[0])] = k
        k = cache[key].get(start)
        ok = bool(k) and int(k[6]) == start + 3_600_000 - 1
        o, c = (float(k[1]), float(k[4])) if ok else (None, None)
        out.append({
            "condition_id": r.condition_id, "asset": r.asset,
            "window_start_ms": start, "window_end_ms": int(r.window_end_ms),
            "kline_open": o, "kline_close": c,
            "binance_up": None if not ok else (c >= o),
            "abs_move": None if not ok else abs(c - o),
            "rel_move_bp": None if not ok else (c - o) / o * 1e4,
            "clob_winner_up": None if r.winner_up is None else bool(r.winner_up),
        })
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.Table.from_pylist(out), OUT / "outcomes.parquet")
    df = con.sql(f"select * from '{OUT.as_posix()}/outcomes.parquet'").df()
    both = df.dropna(subset=["binance_up", "clob_winner_up"])
    if len(both):
        log.info(f"outcomes rows={len(df)} kline={df.kline_open.notna().sum()} "
                 f"agree={(both.binance_up == both.clob_winner_up).mean():.4f} "
                 f"n_agree_sample={len(both)} median_abs_move_usd={both.abs_move.median():.2f}")




# --------------------------------------------------------------------------
# 4. book path at the decision horizons
# --------------------------------------------------------------------------

def _num(v):
    """pandas/pyarrow missing value -> None; otherwise float."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _num_list(v):
    if v is None:
        return []
    try:
        return [x for x in v]
    except TypeError:
        return []


def _hour_of(ms: int) -> tuple[str, str]:
    t = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return t.strftime("%Y-%m-%d"), t.strftime("%H")


def cmd_books(args) -> None:
    BOOKS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("set threads=4")
    mk = con.sql(f"select * from '{OUT.as_posix()}/markets.parquet'").df()
    mk["part"] = [(0, 0)] * len(mk)
    mk["part"] = [ _hour_of(int(x) - 10_000) for x in mk.window_end_ms ]
    parts = _partitions()
    only = set(args.only.split(",")) if args.only else None
    done = 0
    for date, hour, path in parts:
        if only and f"{date}/{hour}" not in only:
            continue
        dest = BOOKS / f"date={date}" / f"hour={hour}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            continue
        sub = mk[mk.part == (date, hour)]
        if sub.empty:
            continue
        # every horizon must fall inside this partition
        part_start = int(datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H").replace(
            tzinfo=timezone.utc).timestamp() * 1000)
        part_end = part_start + 3_600_000
        sub = sub[(sub.window_end_ms - 300_000 >= part_start) & (sub.window_end_ms - 9_000 < part_end)]
        if sub.empty:
            continue
        cids = list(sub.condition_id)
        ph = ", ".join(f"'{c}'" for c in cids)
        t = time.time()
        con.execute(f"""
            create or replace temp table ev as
            select condition_id, asset_id, recv_ts_ns, best_bid, best_ask
            from read_parquet('{path.as_posix()}/*.parquet')
            where event_type = 'best_bid_ask' and condition_id in ({ph})
              and best_bid is not null and best_ask is not null
        """)
        con.execute(f"""
            create or replace temp table bk as
            select condition_id, asset_id, recv_ts_ns, bids, asks
            from read_parquet('{path.as_posix()}/*.parquet')
            where event_type = 'book' and condition_id in ({ph})
        """)
        rows = []
        for _, m in sub.iterrows():
            for side, token in (("up", m.up_token), ("down", m.down_token)):
                if not token:
                    continue
                rows.append({"condition_id": m.condition_id, "asset": m.asset,
                             "interval_minutes": int(m.interval_minutes),
                             "window_end_ms": int(m.window_end_ms), "side": side,
                             "token": str(token)})
        hz = pd.DataFrame(rows)
        con.register("hz0", hz)
        con.execute("create or replace temp table h as select * from hz0")
        offs = pd.DataFrame([{"offset_ms": int(o), "label": l} for o, l in OFFSETS])
        con.register("offs0", offs)
        con.execute("create or replace temp table offs as select * from offs0")
        con.execute("""
            create or replace temp table hob as
            select h.condition_id, h.asset, h.interval_minutes, h.window_end_ms, h.side,
                   h.token, o.offset_ms, o.label,
                   (h.window_end_ms - o.offset_ms) * 1000000 as ts_ns
            from h, offs o
        """)
        con.execute("""
            create or replace temp table tob as
            select h.condition_id, h.asset, h.interval_minutes, h.window_end_ms, h.side,
                   h.offset_ms, h.label, h.ts_ns,
                   e.recv_ts_ns as tob_ts_ns, e.best_bid, e.best_ask
            from hob h asof left join ev e
              on h.condition_id = e.condition_id and h.token = e.asset_id
                 and e.recv_ts_ns <= h.ts_ns
        """)
        con.execute("""
            create or replace temp table dep as
            select h.condition_id, h.side, h.offset_ms,
                   b.recv_ts_ns as book_ts_ns, b.bids, b.asks
            from hob h asof left join bk b
              on h.condition_id = b.condition_id and h.token = b.asset_id
                 and b.recv_ts_ns <= h.ts_ns
        """)
        tob = con.sql("select * from tob").df()
        dep = con.sql("select * from dep").df()
        dkey = {(r["condition_id"], r["side"], int(r["offset_ms"])): r
                for r in dep.to_dict("records")}
        tick_map = dict(zip(sub.condition_id, sub.tick_size.astype(float)))
        recs = []
        for r in tob.to_dict("records"):
            tick = tick_map.get(r["condition_id"], 0.001)
            d = dkey.get((r["condition_id"], r["side"], int(r["offset_ms"])))
            asks = _num_list(d.get("asks") if d else None)
            bids = _num_list(d.get("bids") if d else None)
            levels = sorted({float(a["price"]) for a in asks})[:5]
            size_at: dict[float, float] = {}
            for a in asks:
                size_at[float(a["price"])] = size_at.get(float(a["price"]), 0.0) + float(a["size"])
            ask_best = levels[0] if levels else None
            def depth(k):
                if ask_best is None:
                    return None
                cap = ask_best + tick * k + 1e-9
                return sum(float(a["size"]) for a in asks if float(a["price"]) <= cap)
            tob_ts = _num(r.get("tob_ts_ns"))
            book_ts = _num(d.get("book_ts_ns")) if d else None
            ts = int(r["ts_ns"])
            recs.append({
                "condition_id": r["condition_id"], "asset": r["asset"],
                "interval_minutes": int(r["interval_minutes"]),
                "window_end_ms": int(r["window_end_ms"]),
                "side": r["side"], "offset_ms": int(r["offset_ms"]), "label": r["label"],
                "ts_ns": ts, "tob_ts_ns": tob_ts, "best_bid": _num(r.get("best_bid")),
                "best_ask": _num(r.get("best_ask")),
                "tob_staleness_ms": None if tob_ts is None else (ts - tob_ts) / 1e6,
                "book_ts_ns": book_ts,
                "book_staleness_ms": None if book_ts is None else (ts - book_ts) / 1e6,
                "ladder_ask_best": ask_best,
                "size_at_ask": None if ask_best is None else size_at.get(ask_best),
                "depth_1tick": depth(1), "depth_2ticks": depth(2), "depth_5ticks": depth(5),
                "ask_ladder": [(p, size_at[p]) for p in levels],
                "tick_size": tick,
            })
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist(recs), dest)
        done += 1
        log.info(f"books {date}/{hour}: markets={len(sub)} token-horizons={len(recs)} "
                 f"in {time.time() - t:.1f}s ({done} written)")
    log.info(f"books: done, {done} partitions written")


# --------------------------------------------------------------------------
# 5. retrospective price series (CLOB prices-history) + 1-minute Binance paths
# --------------------------------------------------------------------------

HIST = OUT / "price_history"


def fetch_history(token: str, tries: int = 4) -> list[dict] | None:
    path = HIST / f"{token}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            path.unlink(missing_ok=True)
    url = f"https://clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=1"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "polymarketbot-research/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read()).get("history") or []
            path.write_text(json.dumps(d))
            return d
        except Exception:
            time.sleep(1.2 * (attempt + 1))
    log.warning(f"price history failed token={token}")
    return None


def _hist_rows(con, mk) -> list[dict]:
    """Per market: observations, distinct prices, and the last observation before expiry."""
    out = []
    for _, r in mk.iterrows():
        for side, token in (("up", r.up_token), ("down", r.down_token)):
            if not token:
                continue
            h = fetch_history(str(token))
            if h is None:
                out.append({"condition_id": r.condition_id, "side": side, "n_obs": 0})
                continue
            ts = [int(x["t"]) * 1000 for x in h]
            px = [float(x["p"]) for x in h]
            wend = int(r.window_end_ms)
            wstart = int(r.window_start_ms)
            before = [(t, p) for t, p in zip(ts, px) if t <= wend]
            inside = [(t, p) for t, p in before if t >= wstart]
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            last_before = before[-1] if before else (None, None)
            out.append({
                "condition_id": r.condition_id, "side": side, "n_obs": len(h),
                "n_distinct_px": len(set(px)),
                "median_gap_s": (sorted(gaps)[len(gaps) // 2] / 1000) if gaps else None,
                "n_obs_inside_window": len(inside),
                "first_obs_ms": ts[0] if ts else None,
                "last_obs_ms": ts[-1] if ts else None,
                "last_before_end_ms": last_before[0],
                "last_before_end_px": last_before[1],
                "secs_before_end": (None if last_before[0] is None
                                    else (wend - last_before[0]) / 1000),
                "max_px_before_end": max((p for _, p in before), default=None),
                "max_px_before_end_minus_60s": max(
                    (p for t, p in before if t <= wend - 60_000), default=None),
            })
    return out


def cmd_prices(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    HIST.mkdir(exist_ok=True)
    con = duckdb.connect()
    mk = con.sql(f"select * from '{OUT.as_posix()}/markets.parquet' order by window_end_ms").df()
    t = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda x: fetch_history(str(x)), list(mk.up_token) + list(mk.down_token)))
    log.info(f"price history fetched in {time.time() - t:.0f}s")
    rows = _hist_rows(con, mk)
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.Table.from_pylist(rows), OUT / "price_history_features.parquet")
    df = con.sql(f"select * from '{OUT.as_posix()}/price_history_features.parquet'").df()
    log.info(f"price features rows={len(df)} obs>0={(df.n_obs > 0).sum()} "
             f"median_obs={df.n_obs.median()} obs_inside_window={(df.n_obs_inside_window > 0).sum()}")


def cmd_minutes(args) -> None:
    """1-minute Binance klines over the study windows -> distance-to-strike at T-x."""
    con = duckdb.connect()
    mk = con.sql(f"""select condition_id, asset, interval_minutes, window_start_ms, window_end_ms,
                            winner_up, settles_on
                     from '{OUT.as_posix()}/markets.parquet'
                     where asset = 'bitcoin' order by window_start_ms""").df()
    if mk.empty:
        log.info("minutes: no bitcoin markets")
        return
    lo, hi = int(mk.window_start_ms.min()), int(mk.window_end_ms.max())
    days = sorted({datetime.fromtimestamp(x / 1000, timezone.utc).strftime("%Y-%m-%d")
                   for x in range(int(lo) - 86_400_000, int(hi) + 86_400_000, 86_400_000)})
    mins: dict[int, list] = {}
    for d in days:
        start = int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        for k in fetch_klines("BTCUSDT", "1m", start, start + 86_400_000 - 60_000):
            mins[int(k[0])] = k
    log.info(f"minutes: {len(mins)} 1-minute bars over {len(days)} days; markets={len(mk)}")
    import statistics
    # per-minute log-return scale, used for the residual-volatility normalisation
    closes = [(t, float(k[4])) for t, k in sorted(mins.items())]
    rets = [abs(closes[i][1] / closes[i - 1][1] - 1) for i in range(1, len(closes))]
    sigma_1m = statistics.median(rets) * 1.4826  # MAD -> sigma for a centred normal
    log.info(f"minutes: sigma_1m={sigma_1m:.6f} from {len(rets)} one-minute returns")
    rows = []
    for _, r in mk.iterrows():
        ws, we = int(r.window_start_ms), int(r.window_end_ms)
        k0 = mins.get(ws)
        if k0 is None:
            continue
        strike = float(k0[1])  # candle open, the settlement reference
        for secs in (300, 120, 60, 30, 10):
            t = we - secs * 1000
            bar = mins.get(t // 60_000 * 60_000)
            if bar is None:
                continue
            px = float(bar[4])  # close of the minute bar that contains T-secs
            resid = sigma_1m * ((secs / 60.0) ** 0.5)
            d = (px / strike - 1.0)
            rows.append({
                "condition_id": r.condition_id, "interval_minutes": int(r.interval_minutes),
                "window_end_ms": we, "secs_before_end": secs,
                "strike": strike, "px": px, "dist": px - strike,
                "dist_rel": d, "resid_sigma_rel": resid,
                "z": (d / resid) if resid else None,
                "winner_up": None if r.winner_up is None else bool(r.winner_up),
                "settles_on": r.settles_on,
            })
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.Table.from_pylist(rows), OUT / "minute_paths.parquet")
    log.info(f"minute_paths rows={len(rows)} markets={len(mk)}")


MINUTE_PX = OUT / "minute_px"


def fetch_window_px(token: str, start_s: int, end_s: int, tries: int = 4) -> list[dict] | None:
    """1-minute midpoints over [start_s, end_s] for one token (venue's own history)."""
    path = MINUTE_PX / f"{token}_{start_s}_{end_s}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            path.unlink(missing_ok=True)
    url = (f"https://clob.polymarket.com/prices-history?market={token}"
           f"&startTs={start_s}&endTs={end_s}&fidelity=1")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "polymarketbot-research/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read()).get("history") or []
            path.write_text(json.dumps(d))
            return d
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    log.warning(f"window px failed token={token}")
    return None


HORIZON_S = (300, 240, 180, 120, 60, 10)


def cmd_windows(args) -> None:
    """Per market: the 1-minute price path through its window, at both tokens."""
    MINUTE_PX.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    mk = con.sql(f"""select condition_id, slug, asset, interval_minutes, up_token, down_token,
                            window_start_ms, window_end_ms, winner_up, fee_rate, tick_size
                     from '{OUT.as_posix()}/markets.parquet' order by window_end_ms""").df()
    batch = 200
    for b0 in range(0, len(mk), batch):
        dest = OUT / "paths" / f"batch={b0:05d}.parquet"
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        sub = mk.iloc[b0:b0 + batch]
        jobs = []
        for _, r in sub.iterrows():
            s0 = int(r.window_start_ms // 1000) - 60
            s1 = int(r.window_end_ms // 1000) + 60
            for side, tok in (("up", r.up_token), ("down", r.down_token)):
                if tok:
                    jobs.append((str(tok), s0, s1, r.condition_id, side))
        with ThreadPoolExecutor(max_workers=8) as ex:
            series = list(ex.map(lambda j: fetch_window_px(j[0], j[1], j[2]), jobs))
        rows = []
        for (tok, s0, s1, cid, side), h in zip(jobs, series):
            r = sub[sub.condition_id == cid].iloc[0]
            wend = int(r.window_end_ms)
            pts = [(int(x["t"]) * 1000, float(x["p"])) for x in (h or [])]
            pts.sort()
            before = [(t, p) for t, p in pts if t <= wend]
            for off in HORIZON_S:
                cut = wend - off * 1000
                cand = [(t, p) for t, p in before if t <= cut]
                t, p = cand[-1] if cand else (None, None)
                rows.append({
                    "condition_id": cid, "side": side, "offset_s": off,
                    "px": p, "px_ts_ms": t,
                    "staleness_s": None if t is None else (cut - t) / 1000,
                    "n_obs_in_window": sum(1 for t2, _ in pts if int(r.window_start_ms) <= t2 <= wend),
                })
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist(rows), dest)
        log.info(f"windows batch {b0}: {len(sub)} markets, {len(rows)} horizon rows -> {dest.name}")
    log.info("windows: done")


# --------------------------------------------------------------------------
# 6. report
# --------------------------------------------------------------------------

BUCKETS = ((0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 0.99), (0.99, 1.01))
THRESHOLDS = (0.80, 0.85, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.985, 0.99, 0.995)


def _fee(px: float, rate: float = 0.07) -> float:
    """polymarketbot.fees.taker_fee with the market's own schedule (rate 0.07, exp 1)."""
    return round(rate * px * (1.0 - px), 5)


def _boot_ci(vals, blocks, reps: int = 2000, seed: int = 7):
    """Cluster bootstrap by expiry block: trades inside an hour share a volatility regime."""
    import random
    rnd = random.Random(seed)
    uniq = sorted(set(blocks))
    idx = {b: [] for b in uniq}
    for i, b in enumerate(blocks):
        idx[b].append(vals[i])
    means = []
    for _ in range(reps):
        pick = [rnd.choice(uniq) for _ in uniq]
        sample = [v for b in pick for v in idx[b]]
        if sample:
            means.append(sum(sample) / len(sample))
    means.sort()
    return (means[int(0.025 * len(means))], means[int(0.975 * len(means))]) if means else (None, None)


def cmd_report(args) -> None:
    import collections
    import statistics
    con = duckdb.connect()
    con.execute("set threads=4")
    mk = con.sql(f"select * from '{OUT.as_posix()}/markets.parquet'").df()
    paths = con.sql(f"select * from '{OUT.as_posix()}/paths/batch=*.parquet'").df()
    cov = con.sql(f"select * from '{OUT.as_posix()}/partition_coverage.parquet'").df()
    summary: dict = {}
    summary["inventory"] = {
        "markets": int(len(mk)),
        "by_asset_interval": {f"{a}/{i}m": int(n) for (a, i), n in
                              mk.groupby(["asset", "interval_minutes"]).size().items()},
        "settlement_source": {k: int(v) for k, v in mk.settles_on.value_counts().items()},
        "fee_schedule": {f"rate={r},exp={e},rebate={rb},taker_only={to}": int(n)
                         for (r, e, rb, to), n in mk.groupby(
                             ["fee_rate", "fee_exponent", "fee_rebate", "fee_taker_only"]).size().items()},
        "tick_size": {str(k): int(v) for k, v in mk.tick_size.value_counts().items()},
        "window_start_utc": datetime.fromtimestamp(mk.window_start_ms.min() / 1000, timezone.utc).isoformat(),
        "window_end_utc": datetime.fromtimestamp(mk.window_end_ms.max() / 1000, timezone.utc).isoformat(),
        "window_dates": {str(k): int(v) for k, v in
                         mk.assign(d=lambda x: [datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")
                                                for t in x.window_end_ms]).d.value_counts().sort_index().items()},
        "up_share": float(mk.winner_up.mean()),
        "tape_partitions_with_updown_rows": int((cov.ncids > 0).sum()),
        "tape_partitions": int(len(cov)),
    }

    # ---- the decisive data check: is there any tick-level book for the windows? ----
    con.execute("create temp table m as select * from markets_df") if False else None
    mk_path = (OUT / "markets.parquet").as_posix()
    audit = con.sql(f"""
        with t as (
          select 'book' ev, count(*) n from read_parquet(
            ['{(TAPE / "clob_events").as_posix()}/date=*/hour=*/*.parquet']) e
            where e.event_type in ('book','best_bid_ask')
              and e.asset_id in (select up_token from '{mk_path}'
                                 union all select down_token from '{mk_path}')
        ) select * from t
    """).df() if False else None
    audit = con.sql(f"""
        select event_type, count(*) rows, count(distinct asset_id) tokens,
               min(to_timestamp(recv_ts_ns/1e9)) t0, max(to_timestamp(recv_ts_ns/1e9)) t1
        from read_parquet('{(TAPE / "clob_events").as_posix()}/date=*/hour=*/*.parquet')
        where event_type in ('book','best_bid_ask')
          and asset_id in (select up_token from '{mk_path}' union all
                           select down_token from '{mk_path}' where down_token is not null)
        group by 1
    """).df()
    trades = con.sql(f"""
        select count(*) rows, count(distinct asset_id) tokens
        from read_parquet('{(TAPE / "clob_trades").as_posix()}/date=*/*/*.parquet')
        where asset_id in (select up_token from '{mk_path}' union all
                           select down_token from '{mk_path}' where down_token is not null)
    """).df()
    summary["tick_level_availability"] = {
        "clob_events_rows_for_updown_tokens": {r.event_type: int(r.rows) for r in audit.itertuples()},
        "clob_events_tokens_seen": {r.event_type: int(r.tokens) for r in audit.itertuples()},
        "clob_trades_rows_for_updown_tokens": int(trades.rows.iloc[0]),
        "clob_trades_tokens": int(trades.tokens.iloc[0]),
    }

    # ---- 1-minute price path: calibration and the trade ----
    # One row per (market, horizon). The favourite is chosen by price only - never by
    # the outcome - so the entry side is observable at decision time.
    pts = paths[paths.px.notna()]
    wide = pts.pivot_table(index=["condition_id", "offset_s"], columns="side",
                           values="px").reset_index()
    wide = wide[wide.get("up").notna()] if "up" in wide else wide
    wide = wide.dropna(subset=[c for c in ("up",) if c in wide.columns])
    meta = mk.set_index("condition_id")
    wide["winner_up"] = wide.condition_id.map(meta.winner_up).astype(bool)
    wide["interval_minutes"] = wide.condition_id.map(meta.interval_minutes)
    wide["day"] = [datetime.fromtimestamp(meta.window_end_ms[c] / 1000, timezone.utc).strftime("%Y-%m-%d")
                   for c in wide.condition_id]
    wide["expiry_hour"] = (wide.condition_id.map(meta.window_end_ms) // 3_600_000).astype("int64")
    wide["hour"] = [datetime.fromtimestamp(int(x) * 3600, timezone.utc).strftime("%H")
                    for x in wide.expiry_hour]
    wide["mid_sum"] = wide["up"] + wide.get("down")
    wide["fav_side"] = ["up" if u >= d else "down" for u, d in
                        zip(wide["up"], wide.get("down", 1.0 - wide["up"]))]
    wide["fav_px"] = [max(u, d) for u, d in zip(wide["up"], wide.get("down", 1.0 - wide["up"]))]
    wide["won"] = [(fs == "up") == w for fs, w in zip(wide.fav_side, wide.winner_up)]
    wide.to_parquet(OUT / "path_features.parquet")

    calib = []
    for off, g in wide.groupby("offset_s"):
        for lo, hi in BUCKETS:
            s = g[(g["up"] >= lo) & (g["up"] < hi)]
            if len(s):
                calib.append({"offset_s": int(off), "basis": "up_price", "lo": lo, "hi": hi,
                              "n": int(len(s)), "resolve_rate": float(s.winner_up.mean()),
                              "mean_px": float(s["up"].mean())})
            f = g[(g.fav_px >= lo) & (g.fav_px < hi)]
            if len(f):
                calib.append({"offset_s": int(off), "basis": "favourite_price", "lo": lo, "hi": hi,
                              "n": int(len(f)), "resolve_rate": float(f.won.mean()),
                              "mean_px": float(f.fav_px.mean())})
    summary["calibration"] = calib

    sweep = []
    for off, g in wide.groupby("offset_s"):
        for th in THRESHOLDS:
            s = g[(g.fav_px >= th)]
            if len(s) == 0:
                continue
            fee = s.fav_px.map(_fee)
            net = 1.0 * s.won.astype(float) - s.fav_px - fee
            lo, hi = _boot_ci(list(net), list(s.expiry_hour))
            sweep.append({
                "offset_s": int(off), "threshold": th, "n": int(len(s)),
                "hit_rate": float(s.won.mean()), "mean_px": float(s.fav_px.mean()),
                "fee_per_share": float(fee.mean()),
                "mean_net_per_share": float(net.mean()),
                "sum_net_per_share": float(net.sum()),
                "mean_net_pct_of_stake": float((net / s.fav_px).mean()),
                "worst_trade": float(net.min()),
                "share_losses": float((net < 0).mean()),
                "boot_lo": lo, "boot_hi": hi,
                "max_ask_still_profitable": float(s.won.mean()) - float(fee.mean()),
            })
    summary["threshold_sweep"] = sweep

    # per hour-block and per day for the two thresholds that matter
    detail = []
    for off in (60, 10, 300):
        for th in (0.95, 0.98, 0.99):
            s = wide[(wide.offset_s == off) & (wide.fav_px >= th)].copy()
            if s.empty:
                continue
            s["fee"] = s.fav_px.map(_fee)
            s["net"] = s.won.astype(float) - s.fav_px - s.fee
            for (day, hour), gg in s.groupby(["day", "hour"]):
                detail.append({"offset_s": off, "threshold": th, "day": day, "hour": hour,
                               "n": int(len(gg)), "hit_rate": float(gg.won.mean()),
                               "mean_net": float(gg.net.mean())})
    summary["by_day_hour"] = detail

    # ---- latency: what is measurable with a 1-minute grid ----
    lat = {}
    pth = json.loads((ROOT / "data" / "derived" / "local_latency.json").read_text())
    lat["clob_time_ttfb_p50_ms"] = pth["endpoints"]["clob_time"]["ttfb_p50_ms"]
    lat["clob_time_ttfb_p95_ms"] = pth["endpoints"]["clob_time"]["ttfb_p95_ms"]
    g = wide.pivot_table(index="condition_id", columns="offset_s", values="up")
    if 60 in g and 10 in g:
        d = (g[10] - g[60]).abs().dropna()
        lat["abs_mid_change_last_60s"] = {"n": int(len(d)), "median": float(d.median()),
                                         "p90": float(d.quantile(0.9))}
        # diffusion scaling: a 1 s drift is ~sqrt(1/60) of a 60 s drift
        lat["implied_abs_change_last_1s_median"] = float(d.median() / (60 ** 0.5))
    lat["offset_staleness_median_s"] = {int(k): float(v) for k, v in
                                        paths.groupby("offset_s").staleness_s.median().items()}
    summary["latency"] = lat

    summary["sample"] = {
        "markets": int(len(mk)),
        "markets_with_path_data": int(wide.condition_id.nunique()),
        "market_horizon_obs": int(len(wide)),
        "window_days": sorted(set(detail and [x["day"] for x in detail])),
        "independence": ("trades are grouped by expiry hour; an hour's markets share one "
                         "BTC/ETH/SOL volatility regime, so the cluster bootstrap over "
                         "expiry-hour blocks is the reported CI"),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    log.info(f"report written: market_horizons={len(wide)} "
             f"calib_rows={len(calib)} sweep_rows={len(sweep)}")
    for r in sweep:
        if r["offset_s"] in (60, 10) and r["threshold"] in (0.95, 0.98, 0.99):
            log.info(f"  off={r['offset_s']}s thr={r['threshold']}: n={r['n']} "
                     f"hit={r['hit_rate']:.4f} px={r['mean_px']:.4f} fee={r['fee_per_share']:.6f} "
                     f"net/share={r['mean_net_per_share']:+.5f} boot95=[{r['boot_lo']:+.5f},"
                     f"{r['boot_hi']:+.5f}]")

# --------------------------------------------------------------------------
# 7. report from the recorded book (fresh top-of-book at the horizons)
# --------------------------------------------------------------------------

def cmd_report2(args) -> None:
    import json as _json
    con = duckdb.connect()
    con.execute("set threads=4")
    mk = con.sql(f"select * from '{OUT.as_posix()}/markets.parquet'").df()
    bk = con.sql(f"""select * from read_parquet('{BOOKS.as_posix()}/date=*/hour=*.parquet',
                    union_by_name=true)""").df()
    meta = mk.set_index("condition_id")
    b = bk[bk.best_ask.notna() & bk.best_bid.notna()].copy()
    b = b[b.tob_staleness_ms <= args.max_stale_ms]
    wide = b.pivot_table(index=["condition_id", "offset_ms"], columns="side",
                         values=["best_bid", "best_ask"]).reset_index()
    wide.columns = ["|".join([str(c) for c in col if c != ""]) if isinstance(col, tuple) else col
                    for col in wide.columns]
    need = ["best_bid|up", "best_ask|up", "best_bid|down", "best_ask|down"]
    wide = wide.dropna(subset=[c for c in need if c in wide.columns])
    if wide.empty:
        log.warning("report2: no complete two-sided top-of-book at any horizon")
        return
    wide["winner_up"] = wide.condition_id.map(meta.winner_up).astype(bool)
    wide["interval_minutes"] = wide.condition_id.map(meta.interval_minutes)
    wide["fee_rate"] = wide.condition_id.map(meta.fee_rate).fillna(0.07).astype(float)
    wend = wide.condition_id.map(meta.window_end_ms)
    wide["expiry_hour"] = (wend // 3_600_000).astype("int64")
    wide["day"] = [datetime.fromtimestamp(int(x), timezone.utc).strftime("%Y-%m-%d")
                   for x in wide.expiry_hour * 3600]
    wide["hour"] = [datetime.fromtimestamp(int(x), timezone.utc).strftime("%H")
                    for x in wide.expiry_hour * 3600]
    wide["mid_up"] = (wide["best_bid|up"] + wide["best_ask|up"]) / 2
    wide["mid_down"] = (wide["best_bid|down"] + wide["best_ask|down"]) / 2
    wide["fav_side"] = ["up" if u >= d else "down" for u, d in zip(wide.mid_up, wide.mid_down)]
    wide["fav_ask"] = [a if s == "up" else bb for s, a, bb in
                       zip(wide.fav_side, wide["best_ask|up"], wide["best_ask|down"])]
    wide["fav_bid"] = [a if s == "up" else bb for s, a, bb in
                       zip(wide.fav_side, wide["best_bid|up"], wide["best_bid|down"])]
    wide["spread"] = wide.fav_ask - wide.fav_bid
    wide["won"] = [(s == "up") == w for s, w in zip(wide.fav_side, wide.winner_up)]
    wide["fee"] = [_fee(a, r) for a, r in zip(wide.fav_ask, wide.fee_rate)]
    wide["net"] = wide.won.astype(float) - wide.fav_ask - wide.fee
    wide["mid_sum"] = wide.mid_up + wide.mid_down
    wide.to_parquet(OUT / "book_features.parquet")

    sweep = []
    for off, g in wide.groupby("offset_ms"):
        for th in THRESHOLDS:
            s = g[g.fav_ask >= th]
            if len(s) < 3:
                continue
            lo, hi = _boot_ci(list(s.net), list(s.expiry_hour))
            sweep.append({
                "offset_s": int(off) / 1000, "threshold": th, "n": int(len(s)),
                "hit_rate": float(s.won.mean()), "mean_ask": float(s.fav_ask.mean()),
                "mean_fee": float(s.fee.mean()), "mean_net_per_share": float(s.net.mean()),
                "sum_net_per_share": float(s.net.sum()),
                "mean_net_pct_of_stake": float((s.net / s.fav_ask).mean()),
                "worst_trade": float(s.net.min()), "share_losses": float((s.net < 0).mean()),
                "boot_lo": lo, "boot_hi": hi,
                "median_spread": float(s.spread.median()),
                "break_even_ask": float(s.won.mean() - s.fee.mean()),
            })
    calib = []
    for off, g in wide.groupby("offset_ms"):
        for lo_, hi_ in BUCKETS:
            s = g[(g.fav_ask >= lo_) & (g.fav_ask < hi_)]
            if len(s):
                calib.append({"offset_s": int(off) / 1000, "lo": lo_, "hi": hi_,
                              "n": int(len(s)), "hit_rate": float(s.won.mean()),
                              "mean_ask": float(s.fav_ask.mean()),
                              "mean_net": float(s.net.mean())})
    detail = []
    for off in (60000, 10000):
        for th in (0.95, 0.98):
            s = wide[(wide.offset_ms == off) & (wide.fav_ask >= th)]
            for (day, hour), gg in s.groupby(["day", "hour"]):
                detail.append({"offset_s": off / 1000, "threshold": th, "day": day,
                               "hour": hour, "n": int(len(gg)),
                               "hit_rate": float(gg.won.mean()),
                               "mean_net": float(gg.net.mean())})
    lat = {}
    g10 = wide[wide.offset_ms == 10000].set_index("condition_id")
    for later, label in ((9500, "T-9.5s"), (9000, "T-9s")):
        gl = wide[wide.offset_ms == later].set_index("condition_id")
        common = g10.index.intersection(gl.index)
        if len(common) < 3:
            continue
        s = gl.loc[common]
        s = s[s.fav_ask >= 0.98]
        lat[label] = {"n": int(len(s)), "hit_rate": float(s.won.mean()),
                      "mean_ask": float(s.fav_ask.mean()),
                      "mean_net": float(s.net.mean()),
                      "share_ask_rose": float((gl.loc[common].fav_ask >
                                               g10.loc[common].fav_ask).mean())}
    summary = {
        "sample": {
            "markets_in_universe": int(len(mk)),
            "markets_with_two_sided_book_at_a_horizon": int(wide.condition_id.nunique()),
            "market_horizon_obs": int(len(wide)),
            "horizons_s": sorted({int(x) / 1000 for x in wide.offset_ms.unique()}),
            "days": sorted(wide.day.unique()),
            "intervals": {str(k): int(v) for k, v in
                          wide.interval_minutes.value_counts().sort_index().items()},
            "winner_known": int(mk.winner_up.notna().sum()),
            "mid_sum_median": float(wide.mid_sum.median()),
            "max_staleness_ms": args.max_stale_ms,
            "independence": ("one trade per market per horizon; markets expiring in the same "
                             "hour share a volatility regime, so CIs are cluster-bootstrapped "
                             "over expiry-hour blocks"),
        },
        "threshold_sweep_ask": sweep,
        "calibration_by_ask": calib,
        "by_day_hour": detail,
        "latency": lat,
        "fee_model": {
            "formula": "fee_shares = rate * shares * (p*(1-p))^exponent, takers only",
            "rate": 0.07, "exponent": 1.0,
            "source": ("each market's own feeSchedule in the CLOB tape (rate 0.07, exponent 1, "
                       "taker_only true, rebate_rate 0.2) and polymarketbot/fees.py::taker_fee "
                       "with configs/fees.yaml fee_types.crypto_fees_v2"),
        },
    }
    (OUT / "summary_book.json").write_text(_json.dumps(summary, indent=1, default=str))
    log.info(f"report2: markets={summary['sample']['markets_with_two_sided_book_at_a_horizon']} "
             f"obs={len(wide)} days={summary['sample']['days']}")
    for r in sweep:
        if r["offset_s"] in (600.0, 10.0) and r["threshold"] in (0.95, 0.98, 0.99):
            log.info(f"  off={r['offset_s']}s ask>={r['threshold']}: n={r['n']} "
                     f"hit={r['hit_rate']:.4f} ask={r['mean_ask']:.4f} fee={r['mean_fee']:.6f} "
                     f"net={r['mean_net_per_share']:+.5f} ci=[{r['boot_lo']:+.5f},"
                     f"{r['boot_hi']:+.5f}] spr={r['median_spread']:.3f}")


def cmd_bets(args) -> None:
    """Bet-level economics of buying the favourite at/above a threshold.

    The unit of risk is (market, token bought), not the fill. Every fill inside
    one market shares that market's single outcome, so averaging over FILLS
    over-weights the markets that traded a lot - and those are the ones that
    were never in doubt. Measured on 2026-11-10 at ask >= 0.99: the fill-weighted
    mean was +0.46 % of stake and the bet-weighted mean was -0.93 %, because
    winning bets carried 18.3 fills each and losing bets 4.7.

    A negative-skew carry cannot be sized off a mean at all, so this also reports
    the Kelly fraction (which is the growth-optimal stake, and goes negative the
    moment the loss rate crosses the break-even) and the expected shortfall.
    """
    import json as _json

    con = duckdb.connect()
    con.execute("set threads=4")
    thr = float(args.threshold)
    window_ms = int(args.window_s) * 1000
    bets = con.sql(f"""
        WITH m AS (
            SELECT condition_id, window_end_ms, up_token,
                   CAST(winner_up AS INTEGER) AS wup, interval_minutes, fee_rate
            FROM '{OUT.as_posix()}/markets.parquet'
        ),
        t AS (
            SELECT condition_id, asset_id, price, venue_ts_ms
            FROM '{(TAPE / "clob_trades").as_posix()}/date=*/*/*.parquet'
            WHERE price >= {thr} AND side = 'BUY'
        )
        SELECT m.condition_id, m.interval_minutes,
               CAST(CASE WHEN t.asset_id = m.up_token THEN 1 ELSE 0 END AS INTEGER) AS bought_up,
               count(*) AS fills, avg(t.price) AS px,
               CAST(CASE WHEN t.asset_id = m.up_token THEN m.wup
                         ELSE (CASE WHEN m.wup = 0 THEN 1 ELSE 0 END) END AS INTEGER) AS won
        FROM t JOIN m USING (condition_id)
        WHERE t.venue_ts_ms BETWEEN m.window_end_ms - {window_ms} AND m.window_end_ms
        GROUP BY 1, 2, 3, 6
    """).df()
    if bets.empty:
        log.warning(f"bets: no trades at ask >= {thr} in the final {args.window_s}s")
        return

    bets["won"] = bets.won.astype(int)
    bets["gross"] = [(1.0 - p) if w else -p for p, w in zip(bets.px, bets.won)]
    n_bets = len(bets)
    n_loss = int((bets.won == 0).sum())
    loss_rate = n_loss / n_bets

    # Wilson interval: with a handful of losses the normal approximation is
    # useless, and the tail is the whole question.
    z = 1.96
    den = 1 + z * z / n_bets
    centre = (loss_rate + z * z / (2 * n_bets)) / den
    half = z * ((loss_rate * (1 - loss_rate) / n_bets + z * z / (4 * n_bets * n_bets)) ** 0.5) / den
    loss_ci = [max(0.0, centre - half), centre + half]

    ev_bet = float(bets.gross.mean())
    sd = float(bets.gross.std(ddof=1)) if n_bets > 1 else 0.0
    se = sd / (n_bets ** 0.5) if n_bets > 1 else 0.0
    ev_fill = float((bets.gross * bets.fills).sum() / bets.fills.sum())
    px = float(bets.px.mean())
    b_odds = (1.0 - px) / px
    kelly = ((1 - loss_rate) * b_odds - loss_rate) / b_odds

    ordered = bets.gross.sort_values().to_numpy()
    def es(alpha: float) -> float:
        k = max(1, int(round((1 - alpha) * len(ordered))))
        return float(ordered[:k].mean())

    out = {
        "threshold": thr,
        "window_s": int(args.window_s),
        "unit": "one bet = (market, token bought); all fills in a market share one outcome",
        "bets": n_bets,
        "fills": int(bets.fills.sum()),
        "losing_bets": n_loss,
        "loss_rate": loss_rate,
        "loss_rate_ci95": loss_ci,
        "break_even_loss_rate": (1.0 - px) / 1.0,
        "mean_px": px,
        "ev_per_share_bet_weighted": ev_bet,
        "ev_per_share_se": se,
        "ev_per_share_ci95": [ev_bet - 1.96 * se, ev_bet + 1.96 * se],
        "ev_per_share_fill_weighted": ev_fill,
        "fill_weighting_bias": ev_fill - ev_bet,
        "kelly_fraction": kelly,
        "expected_shortfall_95": es(0.95),
        "expected_shortfall_99": es(0.99),
        "worst_bet": float(ordered[0]),
        "fills_per_winning_bet": float(bets[bets.won == 1].fills.mean()) if (bets.won == 1).any() else None,
        "fills_per_losing_bet": float(bets[bets.won == 0].fills.mean()) if n_loss else None,
        "by_interval": {
            str(int(k)): {
                "bets": int(len(g)), "losing": int((g.won == 0).sum()),
                "loss_rate": float(1 - g.won.mean()),
                "ev_per_share_bet_weighted": float(g.gross.mean()),
            }
            for k, g in bets.groupby("interval_minutes")
        },
    }
    (OUT / "bets.json").write_text(_json.dumps(out, indent=1, default=str))
    log.info(
        f"bets ask>={thr} last {args.window_s}s: {n_bets} bets / {out['fills']} fills, "
        f"loss {n_loss} ({loss_rate:.2%}, 95% CI [{loss_ci[0]:.2%}, {loss_ci[1]:.2%}]), "
        f"breakeven {out['break_even_loss_rate']:.2%}"
    )
    log.info(
        f"  EV/share bet-weighted {ev_bet:+.5f} (CI [{out['ev_per_share_ci95'][0]:+.5f}, "
        f"{out['ev_per_share_ci95'][1]:+.5f}]) vs fill-weighted {ev_fill:+.5f} "
        f"-> fill weighting adds {out['fill_weighting_bias']:+.5f}"
    )
    log.info(
        f"  Kelly {kelly:+.3f} (negative = do not bet), "
        f"CVaR95 {out['expected_shortfall_95']:+.4f}, worst bet {out['worst_bet']:+.4f}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    sub.add_parser("markets").set_defaults(fn=cmd_markets)
    sub.add_parser("outcomes").set_defaults(fn=cmd_outcomes)
    bp = sub.add_parser("books"); bp.add_argument("--only", default=None)
    bp.set_defaults(fn=cmd_books)
    sub.add_parser("prices").set_defaults(fn=cmd_prices)
    sub.add_parser("minutes").set_defaults(fn=cmd_minutes)
    sub.add_parser("windows").set_defaults(fn=cmd_windows)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    rp = sub.add_parser("report2"); rp.add_argument("--max-stale-ms", type=float, default=60000.0)
    rp.set_defaults(fn=cmd_report2)
    bt = sub.add_parser("bets")
    bt.add_argument("--threshold", type=float, default=0.99)
    bt.add_argument("--window-s", type=int, default=300)
    bt.set_defaults(fn=cmd_bets)
    a = ap.parse_args()
    a.fn(a)
