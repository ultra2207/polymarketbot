# RSS headline versus book: which side of the race are we on

claim:      A headline whose tokens match a condition question precedes the PM book reprice by at least 60 s in 30 % or more of matches; fast-cancelling both sides on a match avoids at least 1 c/share of markout.

mechanism:  The collector polls RSS every 60-120 s while the PM book needs a taker, or a faster maker, to reprice. The headline is the same information a sniper read, so the only open question is whether our poll lands before or after the book moves. If it lands after, the gate is worthless, which is exactly what the test measures.

data:       `news_items` (source, title, summary, url, published_ts_ms, recv_ts_ns; 5,624 rows 11-04 00:37 -> 11-06 04:59 from news.google.com, decrypt, cointelegraph, coindesk) + `data/tape/universe/*.json` (question) + `clob_events`.

module:     `news_match_strength` is the token Jaccard between the headline and the question, computed in `prepare.sql`, so `decide` only compares numbers. If `news_age_s <= 120` and match >= 0.6: PULL both sides for 60 s. If the match is 0.4-0.6: SHRINK both to 0.25 for 60 s. The module never takes a direction from a headline; the claim is about timing only.

metric:     Paired per-condition net; secondary = share of news-matched conditions whose |mid move| in the next 60 s exceeds twice their own baseline 60 s |move|.

kill:       Kill if the median (news recv_ts -> first PM top change) latency is negative in > 60 % of matches, i.e. the book had already moved when we received the item, or if matched conditions forward moves are indistinguishable from baseline.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-news-headline-shock/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `news_match_strength` | 0..1 | token Jaccard of headline vs question | 120 s |
| `news_age_s` | seconds | now - news_items.recv_ts_ns | 120 s |
| `news_polarity` | -1/0/+1 | keyword polarity in prepare.sql; 0 = unused | 120 s |

## prepare.sql

```sql
COPY (
  WITH q AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  ),
  cond AS (
    SELECT m.condition_id,
           list_distinct(list_filter(string_split(lower(regexp_replace(m.question, '[^a-zA-Z ]', ' ', 'g')), ' '),
                                     t -> length(t) >= 4
                                      AND t NOT IN ('will','does','this','that','have','with','from',
                                                    'before','after','than','more','less','reach'))) AS toks
    FROM q
  ),
  n AS (
    SELECT recv_ts_ns, title,
           list_distinct(list_filter(string_split(lower(regexp_replace(title, '[^a-zA-Z ]', ' ', 'g')), ' '),
                                     t -> length(t) >= 4)) AS toks
    FROM read_parquet('data/tape/news_items/**/*.parquet')
  ),
  j AS (
    SELECT n.recv_ts_ns, cond.condition_id,
           len(list_intersect(n.toks, cond.toks))::DOUBLE
             / nullif(len(list_union(n.toks, cond.toks)), 0) AS news_match_strength
    FROM n JOIN cond ON len(list_intersect(n.toks, cond.toks)) >= 2
  )
  SELECT condition_id, recv_ts_ns AS ts_ms, news_match_strength
  FROM j WHERE news_match_strength >= 0.4
) TO 'data/derived/ideas/cross-news-headline-shock/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `news_items.recv_ts_ns|title|published_ts_ms|source` is on the tape (5,624 rows over 2 days). Population limited: 4 free feeds, and GDELT was removed on 2026-11-05 after sustained HTTP 429, so coverage is thin and the item rate is a few thousand per day.

## Provenance

- `docs/data-collection.md` News section: publisher RSS at 60-120 s, GDELT removed, no AP/Tree News wire available.
- `docs/alpha-pipeline.md` C5 (news latency) is recorded as blocked on a free real-time wire; this idea is the honest test of the free wire we do have.
- Headline-to-jump microstructure: `docs/institutional-exogenous-market-making.md` section 2.1 (two-tier matcher, fast-cancel then quantify).

## What would change our mind

If our poll lands after the book in most matches, the free RSS wire is a monitoring feed with no defensive value and the family needs a paid low-latency wire or nothing; that closes the news branch on this data budget.
