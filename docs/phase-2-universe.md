# Phase 2 — point-in-time universe

> **Status (2026-11-11).** Design record only. The `polymarketbot.universe` module this file
> imports (`universe_at`, `tradable_at`, `settle`) and every `polymarketbot.data.builders.*`
> command in §8 were deleted with the 2026-11-10 cleanup; nothing in `src/` provides these
> entry points today, so §1 and §8 raise `ModuleNotFoundError` and the "41 universe
> invariants" test file is gone with them. The rules and measurements below are still the
> reference for what a point-in-time universe has to mean.

What "a market existed and was tradable at time *t*" means in this repo, why each rule is
there, and what it deliberately ignores.

Companion to `roadmap.md` Phase 2.

---

## 1. The three entry points

```python
from polymarketbot.universe.markets import universe_at, tradable_at, settle
```

| Function | Lookahead | Use for |
|---|---|---|
| `universe_at(con, t)` | **none** | driving a simulation |
| `tradable_at(con, t)` | ex-post (excludes voids) | analysis populations, hit rates |
| `settle(con)` | ex-post | resolution labels |

`tradable_at` exists because a void has no win/loss interpretation. It must never drive a
simulation: at time *t* you cannot know a market will void.

### `universe_at(t)` rules

A market is in the universe at `t` when **all** of these hold:

1. `created_at <= t`
2. `first_fill_ts <= t` — at least one non-zero fill has happened by `t`
3. `end_date > t`

Rule 2 is the zero-volume filter, and it comes from the **tape**, not from
`markets.parquet.volume`. `volume` is a latest-snapshot value; using it would leak the
future into every historical universe. `first_fill_ts` is computed by
`polymarketbot.data.builders.build_market_stats` from fills only.

Rule 3 is why `end_date < created_at` markets never appear: if `end_date < created_at <= t`
then `end_date <= t`, so they are excluded automatically. That anomaly affects 32,720
markets (0.8%), concentrated in 2022–2024.

---

## 2. Void handling

A void is **not** a loss and **not** a win. It settles at its on-chain payout:

- standard binary void: `payoutNumerators = [1,1]`, denominator `2` → **0.5 per share**
- voids stay in `universe_at` (they were tradable)
- voids are excluded from `tradable_at` (no win/loss label)
- `settle()` reports `outcome = 'void'` separately

SII encodes "void" in three different ways (`0.5/0.5`, `1/1`, `0/0`), so void detection is
done from on-chain payouts, never from the price string.

---

## 3. Labels

`payout_0` is the per-share payout of `token1`. This is **proven**, not assumed:
`positionIds[0] == token1` for 934,889 of 934,957 binary conditions.

| Era | Source | Coverage |
|---|---|---|
| 2022-11 → 2026-03 | on-chain `payoutNumerators` (`moose-code/condition`) | complete |
| 2026-04 | on-chain | 77.8 % (the rest is post-snapshot) |
| 2026-05 → 2026-10 | `/v2/tokens` `final_price` | not yet fetched; 685,918 forward-test markets |

`outcome_prices` in SII is a **final price, not a payout**. Example: SII
`0.5109462677…` vs on-chain `503870/1000000 = 0.50387`. Never threshold it.

64 resolved binary markets pay something other than 0/1. They are labelled `fractional`
rather than being forced into a winner.

---

## 4. Tick size

Precedence is Gamma (per-market, authoritative) → Dome snapshots (2025-10-14 →
2026-03-31) → explicit `0.01` assumption. `tick_size_source` records which path won.

| Source | Markets | Note |
|---|---|---|
| `gamma` | 2,432,590 | per-market `minimumTickSize`; the whole 2024+ traded universe |
| `assumed` | 1,617,256 | 2021–2023 and never-traded markets, flagged |
| `dome` | 9 | Gamma had no value and Dome did |

The Gamma enrichment (`python -m polymarketbot.data.builders.enrich_market_metadata`) closed what used to be a
3.9 M-market assumption. Never treat `assumed` as knowledge: 165/200 sampled markets are
`0.001`, so the old `0.01` default was wrong more often than right.

57,139 markets had more than one tick across their life (Dome records per-token,
per-snapshot values). The stored value is the **finest** tick observed. PMXT
`tick_size_change` events (Phase 4) can refine this per timestamp later.

---

## 5. Protocol eras

`protocol` is derived from the fill contracts, not from a hard-coded date:

| Era | Markets | Traded |
|---|---|---|
| `v1` | 829,553 | $27.20 B |
| `v2` | 1,584,937 | $13.70 B |
| `mixed` (crossed the 2026-04-28 cutover) | 21,381 | $4.12 B |
| `unknown` (never traded) | 1,613,984 | — |

V2 is 41% of all fills, so it is a first-class era, not an edge case.

---

## 6. Categories

Rule-based, because SII has no tag column, Gamma's `category` is null for most markets, and
keyset pagination caps at 100 rows/page.

- rules: `configs/categories.yaml` (ordered; first match wins)
- code: `src/polymarketbot/universe/categories.py`
- enrichment: `python -m polymarketbot.data.builders.enrich_categories_gamma` fetches **Gamma tags only for markets the
  rules could not classify** (107 k candidates since 2024, ~1,100 requests) and reruns the
  same rules over tag labels.

Result: 106,566 markets fetched, **99.4 % reclassified out of `other`**. Final residual
`other` is **2.7 % of markets and 0.1 % of volume**; within the instantaneous open universe it
is **0.3–0.8 %**.

`category_source` records which path produced the value: `rule` (97.4 %) or `gamma_tags`
(2.6 %).

---

## 7. Performance

Measured on this machine (8 threads, 709 MB `markets_pit.parquet`):

| Operation | Cost |
|---|---|
| `universe_at(t)` count | **~92 ms** |
| `universe_at(t)` + category breakdown | ~95 ms |
| `build_market_stats.py` (1.27 B rows) | ~2 min |

A daily loop over three years is ~1,100 calls, i.e. ~100 s — acceptable. A simulator
should still precompute per-day universe snapshots rather than calling `universe_at` inside
a per-trade loop.

Profiling note: the first version of `build_market_stats.py` computed `count(DISTINCT
maker)` and `count(DISTINCT taker)` per condition. That turns a 4 M-group aggregation into
a per-group hash set over 1.27 B rows and effectively hangs. Removing them took the job
from "indefinite" to ~2 minutes. Those columns are not needed for the universe; compute
them separately if Phase 6b needs them.

---

## 8. Reproduce
```bash
uv run python -m polymarketbot.data.builders.build_labels          # labels_chain + market_labels
uv run python -m polymarketbot.data.builders.build_categories      # market_categories
uv run python -m polymarketbot.data.builders.build_market_stats    # per-condition tape stats (1.27 B rows)
uv run python -m polymarketbot.data.builders.build_tick_sizes      # market_tick (Dome)
uv run python -m polymarketbot.data.builders.build_universe        # markets_pit
uv run pytest -q                        # 41 universe invariants
uv run python -m polymarketbot.tools.check_universe --n 24 # invariants + live Gamma spot-check
```

Order matters: `build_universe.py` reads the other four outputs.

---

## 9. Known limitations (kept, not hidden)

- **`end_date` leaks the future.** SII ships only the latest value, and deadlines can be
  extended after creation. A market whose deadline was extended appears in
  `universe_at(t)` for longer than it truly was open. There is no free point-in-time
  `end_date` history; the Gamma daily snapshots (`polymarketbot.data.gamma_snapshot`) close this gap
  going forward.
- **6,386 markets traded before their recorded `created_at`** (median 3.6 h; 5,191 of
  them in 2025; six cases are ~3 years off and are clearly metadata errors). We keep
  `created_at` as the birth time because it is conservative and cannot introduce
  lookahead — those markets simply enter the universe slightly late. Flagged as
  `traded_before_created`; not silently backdated, because backdating the six corrupt cases
  would fabricate three years of tradability.
- **`closed` is latest-only** and therefore never used by `universe_at`.
- **Resolution timing is unknown offline.** `settle()` says *what* a market paid, not *when*
  it was decided. UMA logs (Phase 1b) would be needed for the timing.
- **Public market pages are not a durable archive.** `polymarket.com/market/<slug>` redirects to
  `/event/<event>/<market>`, and old markets may not render the specific market at all. Gate 2's
  human spot-check therefore uses recent markets that still resolve; for delisted history,
  Gamma `closed=true` and on-chain payouts are the accessible record.
- **Concurrent open-market counts are small** (hundreds in 2024, tens of thousands in 2026)
  because most markets live days or minutes. That is correct, not a bug.
