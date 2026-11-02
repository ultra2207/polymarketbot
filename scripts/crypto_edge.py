"""Price Polymarket BTC 15-minute up/down contracts from the underlying and test the edge.

Corrections vs the first attempt:
  * the market's life is ~24 h but the *contract window* is the final 900 s;
    S_0 is the spot at (end_ts - 900), not at market creation.
  * use trade prices from the tick file, not the CLOB midpoint (which is 0.50 when empty).
"""

import math
import statistics

import duckdb

con = duckdb.connect()
con.execute("SET threads=8")
con.execute("SET memory_limit='10GB'")
con.execute("SET temp_directory='data/tmp_duck'")

# --- spot 1-minute bars and trailing realised vol ---------------------------
con.execute("""
CREATE OR REPLACE TEMP TABLE spot AS
SELECT ts_ms, price FROM 'data/raw/hf/aliplayer1/spot/part-0.parquet'
WHERE symbol='btcusdt' AND source='binance' AND price>0 ORDER BY ts_ms
""")
con.execute("""
CREATE OR REPLACE TEMP TABLE bars AS
SELECT (ts_ms//60000)*60000 AS m, arg_max(price, ts_ms) AS px FROM spot GROUP BY 1 ORDER BY 1
""")
con.execute("""
CREATE OR REPLACE TEMP TABLE rvol AS
SELECT m, ln(px/lag(px) OVER (ORDER BY m)) r FROM bars
""")
con.execute("""
CREATE OR REPLACE TEMP TABLE vol AS
SELECT m,
  stddev_samp(r) OVER (ORDER BY m ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)*sqrt(525600.0) s60,
  stddev_samp(r) OVER (ORDER BY m ROWS BETWEEN 14 PRECEDING AND CURRENT ROW)*sqrt(525600.0) s15
FROM rvol
""")

# --- markets and the window -------------------------------------------------
con.execute("""
CREATE OR REPLACE TEMP TABLE mk AS
SELECT h.market_id, h.condition_id, h.end_ts, l.payout_0 AS payout
FROM 'data/raw/hf/aliplayer1/markets.parquet' h
JOIN read_parquet('data/processed/market_labels.parquet') l USING(condition_id)
WHERE h.crypto='BTC' AND h.timeframe='15-minute' AND h.end_ts IS NOT NULL
  AND l.resolved AND NOT coalesce(l.is_void,false)
""")
# spot at window start (end_ts - 900)
con.execute("""
CREATE OR REPLACE TEMP TABLE mk0 AS
SELECT mk.*, s.price AS spot_0
FROM mk ASOF LEFT JOIN spot s ON s.ts_ms <= (mk.end_ts-900)*1000
""")
print("markets:", con.sql("SELECT count(*), count(spot_0) FROM mk0").fetchall()[0])

# --- ticks inside the window, joined to spot and vol -----------------------
con.execute("""
CREATE OR REPLACE TEMP TABLE t AS
SELECT k.market_id, k.timestamp_ms/1000.0 AS ts, k.outcome, k.side, k.price, k.size_usdc,
       m.end_ts, m.payout, m.spot_0,
       s.price AS spot_t, v.s60, v.s15
FROM 'data/raw/hf/aliplayer1/ticks_btc15m/part-0.parquet' k
JOIN mk0 m USING(market_id)
ASOF LEFT JOIN spot s ON s.ts_ms <= k.timestamp_ms
ASOF LEFT JOIN vol v ON v.m <= (k.timestamp_ms//60000)*60000
WHERE k.timestamp_ms/1000.0 >= m.end_ts-900 AND k.timestamp_ms/1000.0 < m.end_ts
""")
r = con.sql("""
SELECT count(*), count(spot_0), count(spot_t), count(s60),
       count(*) FILTER (WHERE outcome='Up'), count(*) FILTER (WHERE outcome='Down')
FROM t""").fetchall()[0]
print(f"window ticks: {r[0]:,}  spot0 {r[1]:,}  spot_t {r[2]:,}  vol {r[3]:,}  Up {r[4]:,} Down {r[5]:,}")

rows = con.sql("""
SELECT ts, end_ts, spot_0, spot_t, price, outcome, payout, s60, s15, size_usdc
FROM t
WHERE spot_0 IS NOT NULL AND spot_t IS NOT NULL AND s60 IS NOT NULL
  AND s60 BETWEEN 0.05 AND 5 AND price>0 AND price<1 AND spot_0>0 AND spot_t>0
""").fetchall()
print("analysis ticks:", f"{len(rows):,}")


def norm_cdf(x):
    return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))


YEAR = 31_536_000.0
recs = []
for ts, end_ts, s0, st, px, outcome, payout, s60, s15, usd in rows:
    tau = end_ts-ts
    if tau <= 0:
        continue
    horizon = math.sqrt(tau/YEAR)
    logratio = math.log(st/s0)
    out_up = float(payout)
    for sigma, tag in ((s60, '60m'), (s15, '15m')):
        z = logratio/(sigma*horizon)
        model_up = norm_cdf(z)
        model = model_up if outcome == 'Up' else 1-model_up
        realised = out_up if outcome == 'Up' else 1-out_up
        recs.append((tau, float(px), model, realised, sigma, tag, float(usd)))
print("records:", f"{len(recs):,}")


def spearman(xs, ys):
    def rk(a):
        s = sorted(range(len(a)), key=lambda i: a[i]); r = [0.0]*len(a); i = 0
        while i < len(s):
            j = i
            while j+1 < len(s) and a[s[j+1]] == a[s[i]]:
                j += 1
            avg = (i+j)/2.0
            for k in range(i, j+1):
                r[s[k]] = avg
            i = j+1
        return r
    rx, ry = rk(xs), rk(ys); n = len(xs)
    mx = sum(rx)/n; my = sum(ry)/n
    num = sum((a-mx)*(b-my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a-mx)**2 for a in rx)); dy = math.sqrt(sum((b-my)**2 for b in ry))
    return num/(dx*dy) if dx and dy else float('nan')


for tag in ('60m', '15m'):
    sub = [r for r in recs if r[5] == tag]
    print(f"\n=== sigma={tag}  n={len(sub):,} ===")
    print(f"  mean price {sum(r[1] for r in sub)/len(sub):.4f}   mean model {sum(r[2] for r in sub)/len(sub):.4f}   mean realised {sum(r[3] for r in sub)/len(sub):.4f}")
    print(f"  edge vs price   {(sum(r[3] for r in sub)-sum(r[1] for r in sub))/len(sub):+.4f}")
    print(f"  Spearman(model-price, realised-price) {spearman([r[2]-r[1] for r in sub],[r[3]-r[1] for r in sub]):+.4f}")
    print("\n  disagreement   n         edge         t")
    for th in (0.02, 0.05, 0.10, 0.15):
        for name, sel in (("model>mkt", [r for r in sub if r[2]-r[1] >= th]),
                          ("mkt>model", [r for r in sub if r[1]-r[2] >= th])):
            if len(sel) < 50:
                continue
            e = [r[3]-r[1] for r in sel]
            m = sum(e)/len(e); sd = statistics.pstdev(e) or 1e-9
            print(f"  {name} {th:.2f}   {len(sel):>8,}   {m:+.4f}   {m/(sd/math.sqrt(len(e))):+.1f}")
