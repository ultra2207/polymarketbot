-- cross-uma-liveness-convergence / signals
--
-- MEASURED LIMITATION - read this before trusting the table size.
--   * uma_events holds 272 rows (11-05 05:59 .. 11-06 04:10): 234 ProposePrice,
--     38 RequestPrice, 0 Settle/Dispute.
--   * 234/234 ProposePrice rows carry condition_id = NULL, so the proposal that
--     the idea actually trades cannot be keyed by asset from that column.
--   * 37/234 (15.8 %) proposals can be attributed by exact question text to a
--     universe market; 0 of those 37 are in the 2026-11-06 universe snapshot and
--     0 of them have any quote points in data/derived/market_scorecard.parquet,
--     i.e. they are resolved markets the harness will never replay.
--   * 10/38 RequestPrice rows carry a condition_id; all 10 are in the current
--     universe and are quoted, but RequestPrice rows in this tape have
--     expiry = NULL and no matching ProposePrice shares their
--     request_timestamp, so a phase-2 row is never produced for a live market.
--   * No settle event exists in the tape, so phase 3 has no rows.
--
-- Rows produced: one per (token, attributed event) = 47 conditions x 2 tokens.
-- No time window is applied because the whole table is 272 rows.
--
-- Signal semantics (as-of backwards: a row stays valid until the next row for
-- that asset):
--   phase           1 = RequestPrice seen, 2 = ProposePrice seen (0/3 unused)
--   proposed_p      proposed_price / 1e18 (UMA fixed point); NULL on phase 1
--   secs_to_expiry  expiry - event time, seconds; NULL when expiry is absent
WITH u AS (                    -- newest universe snapshot per condition
    SELECT m.condition_id, m.question, m.clob_tokens
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000) f,
         unnest(f.markets) t(m)
    QUALIFY row_number() OVER (PARTITION BY m.condition_id ORDER BY f.generated_at DESC) = 1
),
uq AS (                        -- exact-title lookup for rows with no condition_id
    SELECT question, min(condition_id) AS condition_id
    FROM u
    WHERE question IS NOT NULL
    GROUP BY question
),
e AS (
    SELECT recv_ts_ns, event, question, condition_id,
           TRY_CAST(proposed_price AS DOUBLE) / 1e18 AS proposed_p,
           expiry,
           CASE event WHEN 'RequestPrice' THEN 1 WHEN 'ProposePrice' THEN 2
                      WHEN 'Settle' THEN 3 ELSE 0 END AS phase
    FROM read_parquet('data/tape/uma_events/*/*/*.parquet')
),
attributed AS (
    SELECT COALESCE(e.condition_id, uq.condition_id) AS condition_id,
           e.recv_ts_ns, e.phase, e.proposed_p, e.expiry
    FROM e LEFT JOIN uq ON uq.question = e.question
    WHERE COALESCE(e.condition_id, uq.condition_id) IS NOT NULL
)
SELECT tok.asset_id,
       (a.recv_ts_ns / 1000000)::BIGINT AS ts_ms,
       CAST(a.phase AS DOUBLE) AS phase,
       a.proposed_p AS proposed_p,
       CAST(a.expiry AS DOUBLE) - a.recv_ts_ns / 1e9 AS secs_to_expiry
FROM attributed a
JOIN u ON u.condition_id = a.condition_id,
     unnest(u.clob_tokens) AS tok(asset_id)
