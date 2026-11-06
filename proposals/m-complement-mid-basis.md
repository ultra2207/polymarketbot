# The venue measures a NO order against the YES mid, so a cross-book basis is a scoring subsidy

`slug:` m-complement-mid-basis
`focus:` venue formula — which mid the distance term is measured from
`market scope:` conditions where both books are live and cross updates; the effect only
exists while the two mids disagree, which is a fast-book state.

## Mechanism

In a binary market the venue's scoring mid for NO is the **complement** of the YES mid, not
the NO token's own quoted mid: `mid_price_no = 1.0 - mid_price_yes`, implemented at
`src/polymarketbot/rewards/scoring.py:100` and used for every NO order's spread at
`scoring.py:119-138`. Our realised distance on a NO order at price `p` is therefore

```
s_venue = |p - (1 - mid_YES)| * 100
s_true  = |p - mid_NO|       * 100
s_venue - s_true = -(mid_NO - (1 - mid_YES)) = -delta
```

When `mid_NO > 1 - mid_YES` (`delta > 0`) the venue credits a *smaller* distance than the
maker actually took, and the quadratic pays more for the same order:
`((v - s + delta)/v)^2` versus `((v - s)/v)^2`, i.e. about `2*delta*(v-s)/v^2` extra score per
share, for free. When `delta < 0` the credit is negative. The maker should therefore stand on
the token with the favourable basis, and the basis is directly observable because one module
instance sees both books of the condition (`scripts/replay_all_markets.py:387-401`; the
engine calls `decide` once per asset, `engine.py:571-576`).

The replay cannot see any of this: each token's spread is computed from that token's own mid
(`engine.py:700-711`) and its competition from that token's own band (`engine.py:311-313`).
So the basis is an unmodelled incentive, and the first question is its size. The sibling
observation that the cross-token mid sum deviates from 1 by ~2.5c in a live hour (measured on
188 conditions; `proposals/micro-complement-staleness.md`) says the basis is not a rounding
artefact — it is of order the whole band.

## Rule

In `setup` store both token ids (`MarketView.tokens`, `base.py:61`). Each `decide`:

```
delta = (1 - mid_YES) - mid_NO           # both from ctx.book.mid (base.py:71)
if abs(delta) < tick_cents:      QUOTE both sides of both tokens (basis immaterial)
elif delta > 0:                  QUOTE both sides of NO; PULL both sides of YES
else:                            QUOTE both sides of YES; PULL both sides of NO
```

Re-evaluate at most every 60 s of `ctx.seconds_elapsed` (`base.py:98`) — a basis that flips
faster than that is stale-data noise, and switching costs queue position
(`engine.py:723-733`).

This is *not* `micro-complement-staleness`: that idea uses the gap as a **markout** signal
(the lagging token is adversely selected) and pulls. This one uses the gap as a **score**
argument — the venue's credited distance is computed from the other token's mid, so a
favourable basis is paid, not penalised. The two point in the same direction on the same
token, which makes them a natural pair: staleness says "do not be the lagging maker", basis
says "the venue under-charges distance on that side". If the basis idea fails, staleness is
untouched, and vice versa.

**Already implemented:** the complement mid in the research scorer
(`rewards/scoring.py:100,119-138`). **Not implemented:** it in the replay (per-token mids,
`engine.py:700-711`), and any token-selection rule based on the basis.

## Why not ruled out

The registry has no idea that references the scoring mid for NO, and the replay's per-token
treatment means no past result could have been contaminated (or informed) by it. Facts 1-8 are
about width, size, markout and coverage; fact 8's price cohorts are about *adverse selection*
by mid level, not about which mid the score is measured from.

## Kill criterion

- Kill if p90 `|delta|` on the slice is below 0.2c: the credit is under 4 % of a 1c band and
  below the tick grid, so it cannot be acted on.
- Kill if the realised reward per share of the basis-favoured token is not higher than the
  sibling's in the predicted direction on the same asset-minutes (this is measurable offline
  by repricing each token's quote under both distance conventions).
- Kill if `delta` is a stale-data artefact: i.e. it reverts within one book update in more
  than 90 % of episodes, so no order can rest through it.

## Data needed

`DecisionContext` for both assets of the condition (both `book.mid`s), no prepared signal.
Validating the size of the effect needs `clob_events` only, to compute `mid_YES + mid_NO - 1`
per second; no new feed. Note that `docs/polymarket-mechanics.md` §2 fixes token order
(`positionIds[0]` is YES) so the YES/NO labelling is safe.
