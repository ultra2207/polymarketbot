# Gamma comment bursts: attention converts into one-sided flow

claim:      A burst of Gamma comments on a condition (3x its own 24 h rate) precedes a one-sided taker surge and worse markout; shrinking both sides for 15 min after a burst earns at least 0.4 c/share.

mechanism:  Comments are retail attention, and attention converts into uninformed directional flow at the tick. Uninformed flow is normally the maker revenue, but a burst also raises the chance that a marginal informed reader joins the crowd. The measurable claim is that burst minutes have worse markout; the module never claims to know the direction.

data:       `gamma_comments` (parent_entity_id, parent_entity_type, body, created_at_ms, author, recv_ts_ns; 32,903 rows, 40 distinct parents) + `clob_events` + `data/tape/universe/*.json` (condition_id -> event id, to join comments posted against an Event).

module:     `comment_burst_z` = comments in the trailing 10 min divided by the condition own trailing 24 h mean rate (minimum denominator 1). Burst >= 3 -> SHRINK both to 0.5 for 15 min; >= 6 -> PULL both for 5 min, then 0.5 for 10 min. No directional use, and no state across markets. Comments that arrive out of order (created_at_ms older than the last seen) refresh the count but cannot reset the window backwards.

metric:     Paired per-condition net; secondary = markout in burst minutes versus matched non-burst minutes of the same condition (matched on time-of-hour and quoted size).

kill:       Kill if burst-minute markout is not worse by >= 0.4 c/share, or if bursts cover < 5 % of quoted minutes in the commented conditions (no population at 40 commented conditions).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-comment-attention-burst/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `comments_10m` | count | gamma_comments rows for the condition in the trailing 10 min | 120 s |
| `comment_burst_z` | ratio | comments_10m / condition 24 h mean rate (floor 1) | 120 s |
| `comment_age_s` | seconds | now - newest comment recv_ts_ns | 120 s |

## prepare.sql

```sql
COPY (
  WITH c AS (
    SELECT parent_entity_id AS entity_id, recv_ts_ns
    FROM read_parquet('data/tape/gamma_comments/**/*.parquet')
    WHERE parent_entity_type = 'Event'
  ),
  cnt AS (
    SELECT entity_id,
           recv_ts_ns,
           count(*) OVER (PARTITION BY entity_id ORDER BY recv_ts_ns
                          RANGE BETWEEN 600000000000 PRECEDING AND CURRENT ROW) AS comments_10m,
           count(*) OVER (PARTITION BY entity_id ORDER BY recv_ts_ns
                          RANGE BETWEEN 86400000000000 PRECEDING AND CURRENT ROW) AS comments_24h
    FROM c
  ),
  u AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  ),
  ce AS (SELECT DISTINCT m.condition_id, m.event_id FROM u)
  SELECT ce.condition_id, cnt.recv_ts_ns AS ts_ms, cnt.comments_10m,
         cnt.comments_10m / greatest(cnt.comments_24h / 144.0, 1.0) AS comment_burst_z
  FROM cnt JOIN ce ON ce.event_id = cnt.entity_id
) TO 'data/derived/ideas/cross-comment-attention-burst/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `gamma_comments.parent_entity_id|parent_entity_type|recv_ts_ns` is on the tape (32,903 rows, 40 parents, 2 days). Population caveat: only the top Gamma-comment events are polled, so the signal exists for tens of conditions, not the universe.

## Provenance

- `docs/data-collection.md` News section: Gamma comments are polled for the top-`gamma_comment_events` markets per Event, free and always on.
- `docs/data-collection.md` section on news source metrics shows the comment feed is a first-class table (`gamma_comments`), not a scrape.

## What would change our mind

If burst minutes are not more adverse, attention is harmless noise and comments are only a sentiment dataset for a directional model (which the tape says does not work); the idea dies cleanly.
