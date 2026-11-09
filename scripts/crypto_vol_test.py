"""Price Polymarket BTC 15-minute up/down contracts from the underlying spot + realised vol.

For every snapshot we compute the digital-option probability
    P(up) = Phi( ln(S_t / S_0) / (sigma * sqrt(tau)) )
with sigma = trailing 60-minute realised vol, and compare it to the market's price.
"""

import math

import duckdb

from polymarketbot.logs import get_logger

log = get_logger(__name__)

QUERIES = {}

con = duckdb.connect()
con.execute("SET threads=8")
con.execute("SET memory_limit='10GB'")
con.execute("SET temp_directory='data/tmp_duck'")

# ---- 1. one-minute bars from the binance spot feed -------------------------
con.execute("""
CREATE OR REPLACE TEMP TABLE bars AS
SELECT (ts_ms//60000)*60000 AS minute_ms, arg_max(price, ts_ms) AS px
FROM 'data/raw/hf/aliplayer1/spot/part-0.parquet'
WHERE symbol='btcusdt' AND source='binance' AND price>0
GROUP BY 1 ORDER BY 1
""")
con.execute("""
CREATE OR REPLACE TEMP TABLE vol AS
SELECT minute_ms,
       ln(px / lag(px) OVER (ORDER BY minute_ms)) AS r
FROM bars
""")
con.execute("""
CREATE OR REPLACE TEMP TABLE rvol AS
SELECT minute_ms,
       stddev_samp(r) OVER (ORDER BY minute_ms ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
         * sqrt(525600.0) AS sigma_1h,
       stddev_samp(r) OVER (ORDER BY minute_ms ROWS BETWEEN 14 PRECEDING AND CURRENT ROW)
         * sqrt(525600.0) AS sigma_15m
FROM vol
""")
log.info('{!s} {!s}', "vol minutes:", con.sql("SELECT count(*) FROM rvol").fetchall()[0][0])

# ---- 2. markets and outcomes ----------------------------------------------
con.execute("""
CREATE OR REPLACE TEMP TABLE mk AS
SELECT h.market_id, h.condition_id, h.start_ts, h.end_ts, l.payout_0 AS payout
FROM 'data/raw/hf/aliplayer1/markets.parquet' h
JOIN read_parquet('data/processed/market_labels.parquet') l USING(condition_id)
WHERE h.crypto='BTC' AND h.timeframe='15-minute'
  AND h.start_ts IS NOT NULL AND h.end_ts IS NOT NULL
  AND l.resolved AND NOT coalesce(l.is_void,false)
""")
log.info('{!s} {!s}', "markets with outcome:", con.sql("SELECT count(*) FROM mk").fetchall()[0][0])

# ---- 3. spot at market start ----------------------------------------------
con.execute("""
CREATE OR REPLACE TEMP TABLE spot AS
SELECT ts_ms, price FROM 'data/raw/hf/aliplayer1/spot/part-0.parquet'
WHERE symbol='btcusdt' AND source='binance' AND price>0 ORDER BY ts_ms
""")
con.execute("""
CREATE OR REPLACE TEMP TABLE mk0 AS
SELECT mk.*, s.price AS spot_0
FROM mk ASOF LEFT JOIN spot s ON s.ts_ms <= mk.start_ts*1000
""")
log.info('{!s} {!s}', "with spot_0:", con.sql("SELECT count(*) FROM mk0 WHERE spot_0 IS NOT NULL").fetchall()[0][0])

# ---- 4. join price path + spot + vol --------------------------------------
con.execute("""
CREATE OR REPLACE TEMP TABLE j AS
SELECT p.market_id, p.timestamp, p.up_price, p.down_price,
       m.start_ts, m.end_ts, m.payout, m.spot_0,
       s.price AS spot_t, v.sigma_1h, v.sigma_15m
FROM 'data/raw/hf/aliplayer1/prices_btc15m/part-0.parquet' p
JOIN mk0 m USING(market_id)
ASOF LEFT JOIN spot s ON s.ts_ms <= p.timestamp*1000
ASOF LEFT JOIN rvol v ON v.minute_ms <= (p.timestamp//60)*60000
""")
log.info('{!s} {!s}', "joined rows:", con.sql("SELECT count(*) FROM j WHERE spot_0 IS NOT NULL AND spot_t IS NOT NULL").fetchall()[0][0])

rows = con.sql("""
SELECT timestamp, end_ts, spot_0, spot_t, up_price, down_price, sigma_1h, sigma_15m, payout
FROM j
WHERE spot_0 IS NOT NULL AND spot_t IS NOT NULL AND up_price>0 AND up_price<1
  AND sigma_1h IS NOT NULL AND sigma_1h>0.05 AND sigma_1h<5
""").fetchall()
log.info('{!s} {!s}', "analysis rows:", f"{len(rows):,}")


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p):
    """Acklam's rational approximation for the inverse normal CDF."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ---- 5. model vs market ----------------------------------------------------
SECONDS_PER_YEAR = 31_536_000.0
recs = []
for ts, end_ts, s0, st, up_px, dn_px, s1h, s15, payout in rows:
    tau = end_ts - ts
    if tau <= 0 or tau > 1200:
        continue
    s0 = float(s0); st = float(st)
    if s0 <= 0 or st <= 0:
        continue
    logratio = math.log(st / s0)
    horizon = math.sqrt(tau / SECONDS_PER_YEAR)
    for sigma, tag in ((float(s1h), '1h'), (float(s15), '15m')):
        z = logratio / (sigma * horizon) if sigma > 0 else 0.0
        model = norm_cdf(z)
        # implied vol from the market's price
        p = min(max(float(up_px), 1e-6), 1 - 1e-6)
        zi = norm_ppf(p)
        sig_impl = logratio / (zi * horizon) if zi != 0 else float('nan')
        recs.append((tau, float(up_px), float(dn_px), float(payout), model, sigma, sig_impl, tag))

log.info('{!s} {!s}', "model records:", f"{len(recs):,}")

import statistics

def spearman(xs, ys):
    def rk(a):
        s = sorted(range(len(a)), key=lambda i: a[i])
        r = [0.0]*len(a)
        i = 0
        while i < len(s):
            j = i
            while j+1 < len(s) and a[s[j+1]] == a[s[i]]:
                j += 1
            avg = (i+j)/2.0
            for k in range(i, j+1):
                r[s[k]] = avg
            i = j+1
        return r
    rx, ry = rk(xs), rk(ys)
    n = len(xs); mx = sum(rx)/n; my = sum(ry)/n
    num = sum((a-mx)*(b-my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a-mx)**2 for a in rx)); dy = math.sqrt(sum((b-my)**2 for b in ry))
    return num/(dx*dy) if dx and dy else float('nan')

for tag in ('1h', '15m'):
    sub = [r for r in recs if r[7] == tag]
    if not sub:
        continue
    edge_m = sum(r[3]-r[4] for r in sub)/len(sub)
    edge_k = sum(r[3]-r[1] for r in sub)/len(sub)
    log.info(f"\n=== sigma={tag}: n={len(sub):,} ===")
    log.info(f"  mean  market - model      {sum(r[1]-r[4] for r in sub)/len(sub):+.4f}")
    log.info(f"  mean  outcome - model     {edge_m:+.4f}")
    log.info(f"  mean  outcome - market    {edge_k:+.4f}")
    log.info(f"  Spearman(market-model, outcome-model) {spearman([r[1]-r[4] for r in sub],[r[3]-r[4] for r in sub]):+.4f}")

    # edge when the model disagrees with the market
    log.info("\n  |model-market| >=   n        outcome-market edge   t-stat")
    for th in (0.02, 0.05, 0.10, 0.15, 0.20):
        pos = [r for r in sub if r[4]-r[1] >= th]
        neg = [r for r in sub if r[1]-r[4] >= th]
        if len(pos) > 30:
            m = sum(r[3]-r[4] for r in pos)/len(pos)
            sd = statistics.pstdev([r[3]-r[4] for r in pos]) or 1e-9
            log.info(f"    model>market {th:.2f}  {len(pos):>8,}   {m:+.4f}   {m/(sd/math.sqrt(len(pos))):+.1f}")
        if len(neg) > 30:
            m = sum((1-r[3])-r[2] for r in neg)/len(neg)
            sd = statistics.pstdev([(1-r[3])-r[2] for r in neg]) or 1e-9
            log.info(f"    market>model {th:.2f}  {len(neg):>8,}   {m:+.4f}   {m/(sd/math.sqrt(len(neg))):+.1f}")

    # implied vol vs realised
    iv = [r[6] for r in sub if r[6] is not None and not math.isnan(r[6]) and 0.01 < r[6] < 20]
    if iv:
        log.info(f"\n  implied vol: median {statistics.median(iv):.3f}  mean {sum(iv)/len(iv):.3f}  n={len(iv):,}")
        rv = [r[5] for r in sub]
        log.info(f"  realised vol: median {statistics.median(rv):.3f}")
