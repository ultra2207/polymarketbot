-- Kalshi YES mid minus Polymarket YES mid, in logit points, as-of joined.
-- One hour at a time: the as-of join is the expensive part and a single hour is
-- enough to calibrate; the caller widens the window by concatenating hours.
with pm as (
    select asset_id,
           cast(floor(venue_ts_ms / 1000) * 1000 as bigint) as ts_ms,
           arg_max(best_bid, venue_ts_ms) as best_bid,
           arg_max(best_ask, venue_ts_ms) as best_ask
    from read_parquet(['${TAPE}/clob_events/date=2026-11-06/hour=02/*.parquet','${TAPE}/clob_events/date=2026-11-06/hour=03/*.parquet'])
    where best_bid is not null and best_ask is not null
      and best_bid > 0.001 and best_ask < 0.999
    group by 1, 2
),
pm_mid as (
    select asset_id, ts_ms, (best_bid + best_ask) / 2.0 as mid
    from pm
)
-- ASOF needs the right side ordered by (equality key, asof column).
select k.asset_id,
       k.ts_ms,
       ln(k.k_mid / (1 - k.k_mid)) - ln(p.mid / (1 - p.mid)) as basis,
       (k.ts_ms - p.ts_ms) / 1000.0 as pm_age_s
from (
    select m.polymarket_yes_token as asset_id,
           cast(b.recv_ts_ns / 1000000 as bigint) as ts_ms,
           b.mid as k_mid
    from read_parquet(['${TAPE}/kalshi_books/date=2026-11-06/hour=02/*.parquet','${TAPE}/kalshi_books/date=2026-11-06/hour=03/*.parquet']) b
    join read_parquet('data/derived/kalshi_mapping_v2.parquet') m
      on m.kalshi_ticker = b.ticker
    where b.mid > 0.001 and b.mid < 0.999
) k
asof left join (select * from pm_mid order by asset_id, ts_ms) p
    on p.asset_id = k.asset_id and p.ts_ms <= k.ts_ms
where p.mid is not null and p.mid > 0.001 and p.mid < 0.999
