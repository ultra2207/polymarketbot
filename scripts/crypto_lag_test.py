"""Timing test: when the model disagrees with the market, does the price converge?

For every trade we compute the deviation (model - price) and the change in the trade
price over the next observed trade in the same token. If the market lags the spot,
the deviation must predict that forward change.
"""

import math
from collections import defaultdict

import duckdb

con = duckdb.connect()
con.execute("SET threads=8"); con.execute("SET memory_limit='10GB'")
con.execute("SET temp_directory='data/tmp_duck'")
con.execute("""CREATE OR REPLACE TEMP TABLE spot AS
SELECT ts_ms, price FROM 'data/raw/hf/aliplayer1/spot/part-0.parquet'
WHERE symbol='btcusdt' AND source='binance' AND price>0 ORDER BY ts_ms""")
con.execute("""CREATE OR REPLACE TEMP TABLE bars AS
SELECT (ts_ms//60000)*60000 m, arg_max(price, ts_ms) px FROM spot GROUP BY 1 ORDER BY 1""")
con.execute("""CREATE OR REPLACE TEMP TABLE vol AS
SELECT m, ln(px/lag(px) OVER (ORDER BY m)) r FROM bars""")
con.execute("""CREATE OR REPLACE TEMP TABLE rv AS
SELECT m, stddev_samp(r) OVER (ORDER BY m ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)*sqrt(525600.0) s60
FROM vol""")
con.execute("""CREATE OR REPLACE TEMP TABLE mk AS
SELECT h.market_id, h.condition_id, h.end_ts, l.payout_0 AS payout
FROM 'data/raw/hf/aliplayer1/markets.parquet' h
JOIN read_parquet('data/processed/market_labels.parquet') l USING(condition_id)
WHERE h.crypto='BTC' AND h.timeframe='15-minute' AND h.end_ts IS NOT NULL
  AND l.resolved AND NOT coalesce(l.is_void,false)""")
con.execute("""CREATE OR REPLACE TEMP TABLE mk0 AS
SELECT mk.*, s.price spot_0 FROM mk ASOF LEFT JOIN spot s ON s.ts_ms <= (mk.end_ts-900)*1000""")
rows = con.sql("""
SELECT k.market_id, k.outcome, k.timestamp_ms/1000.0 ts, k.price,
       m.end_ts, m.payout, m.spot_0, s.price spot_t, v.s60
FROM 'data/raw/hf/aliplayer1/ticks_btc15m/part-0.parquet' k
JOIN mk0 m USING(market_id)
ASOF LEFT JOIN spot s ON s.ts_ms <= k.timestamp_ms
ASOF LEFT JOIN rv v ON v.m <= (k.timestamp_ms//60000)*60000
WHERE k.timestamp_ms/1000.0 >= m.end_ts-900 AND k.timestamp_ms/1000.0 < m.end_ts
  AND k.price>0 AND k.price<1 AND m.spot_0>0 AND s.price>0
  AND v.s60 BETWEEN 0.05 AND 5
ORDER BY k.market_id, k.outcome, k.timestamp_ms
""").fetchall()
print("ticks:", f"{len(rows):,}")

YEAR = 31_536_000.0
def ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))

# group by token
tok = defaultdict(list)
for mid, out, ts, px, end_ts, payout, s0, st, s60 in rows:
    tok[(mid, out)].append((ts, px, end_ts, payout, s0, st, s60))

records = []  # (deviation, forward change, dt, tau, price, realised, usd)
for key, seq in tok.items():
    seq.sort()
    n = len(seq)
    if n < 3:
        continue
    up = 1.0 if key[1] == 'Up' else 0.0
    for i in range(n-1):
        ts, px, end_ts, payout, s0, st, s60 = seq[i]
        ts2, px2 = seq[i+1][0], seq[i+1][1]
        tau = end_ts - ts
        dt = ts2 - ts
        if tau <= 0 or dt <= 0 or dt > 120:
            continue
        mu = ncdf(math.log(st/s0)/(s60*math.sqrt(tau/YEAR)))
        model = mu if key[1] == 'Up' else 1-mu
        realised = float(payout) if key[1] == 'Up' else 1-float(payout)
        records.append((model-px, px2-px, dt, tau, px, realised, model))
print("records:", f"{len(records):,}")


def corr(xs, ys):
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    num = sum((a-mx)*(b-my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a-mx)**2 for a in xs)); dy = math.sqrt(sum((b-my)**2 for b in ys))
    return num/(dx*dy) if dx and dy else float('nan')


print("\n=== does the deviation predict the NEXT price change? ===")
for lo, hi, label in ((0, 1, '<=1s'), (1, 5, '1-5s'), (5, 30, '5-30s'), (30, 120, '30-120s')):
    sub = [r for r in records if lo < r[2] <= hi]
    if len(sub) < 500:
        continue
    xs = [r[0] for r in sub]; ys = [r[1] for r in sub]
    m = sum(ys)/len(ys)
    print(f"  dt {label:<8} n={len(sub):>9,}  corr(dev, dP)={corr(xs, ys):+.4f}  mean dP={m:+.5f}  "
          f"mean dev={sum(xs)/len(xs):+.4f}")

print("\n=== if we could trade the deviation: edge of buying the model's side ===")
print("  threshold   n          edge      t")
import statistics
for th in (0.02, 0.05, 0.10):
    sel = [r for r in records if abs(r[0]) >= th]
    if not sel:
        continue
    # buy at the current price on the side the model prefers
    edge = []
    for dev, dp, dt, tau, px, realised, model in sel:
        if model > px:
            edge.append(realised - px)
        else:
            edge.append((1-realised) - (1-px))
    m = sum(edge)/len(edge); sd = statistics.pstdev(edge) or 1e-9
    print(f"  {th:>9.2f}  {len(sel):>9,}   {m:+.4f}   {m/(sd/math.sqrt(len(edge))):+.1f}")
