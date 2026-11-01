# Data limitations

What Polymarket does not publish, why it is not published, and what we do instead.

Every item here is a **design constraint**, not a TODO. If a strategy's edge depends on
one of these, the strategy is not testable with free data and should be rejected at the
hypothesis stage rather than after a backtest.

Companion to `roadmap.md` §2.7.

---

## 1. Individual order placement and cancellation

### How an order actually lives

Polymarket is a hybrid venue. The order book is operated **off-chain**; only settlement is
on-chain.

```
user signs order (EIP-712)
        │
        ▼
POST /order  ──────────────►  Polymarket operator
                                │  order rests in the operator's book
                                │  (visible only as aggregate size at a price level)
                                │
                    match  ─────┤
                                ▼
                     on-chain settlement on Polygon
                     CTF Exchange emits OrderFilled
```

Consequences:

- **A resting order is never on-chain.** It becomes on-chain only at the moment it fills.
- **A cancelled order is never on-chain.** `DELETE /order` is an authenticated call to the
  operator. No event, no log, no transaction.
- **An unfilled order's hash never appears anywhere public.** The `orderHash` in an
  `OrderFilled` event exists only because that order was filled.
- The only public views of the book are **aggregated by price level**: `/book`, `/books`,
  and the WebSocket `price_change` message (which carries the *new aggregate size* at a
  level, not the orders that compose it).
- There is **no public endpoint** that lists other participants' orders. `/data/orders`
  is authenticated and scoped to your own account.

So this is not "unindexed" data that someone could scrape. It is data that is **never
published in any form**. Only the operator holds it.

### Why it is not published

- **Competitive:** a live order feed is a market maker's entire strategy. Publishing
  placements and cancels would let anyone front-run resting liquidity and reconstruct
  each participant's quoting behaviour.
- **Architectural:** off-chain matching exists precisely so that order management is cheap
  and private. Putting it on-chain would cost gas per cancel and leak everything.
- **No incentive:** Polymarket gains nothing from publishing it and would lose its market
  makers.

### What can be approximated

You can decompose book changes, but not attribute them:

```
Δsize(level, t) = added − cancelled − filled
```

- `Δsize` is observable exactly (the `price_change` message carries the new aggregate size).
- `filled` at that level is observable from the trade tape (`last_trade_price` / `OrderFilled`).
- The residual `added − cancelled` is observable, but **cannot be split** into adds vs
  cancels, **cannot be attributed** to wallets, and carries no order identity or ordering.

This is enough to measure *net* liquidity provision and to detect large liquidity
withdrawals around news. It is not enough for:

- order-to-trade ratios
- cancellation rates
- spoofing / layering detection
- maker inventory behaviour
- anything requiring "who placed what, when"

### What this rules out

Any strategy whose PnL depends on modelling other participants' order lifecycle. In
practice: market making, liquidity provision, spoof-detection signals, and "follow the
maker" strategies.

---

## 2. Queue position at a price level

### Why it is not observable

At a given price level the book is a queue. Your fill probability for a passive order
depends on **how much size is ahead of you** and **how fast that size turns over**.

The public book reports only:

```
{ "price": "0.42", "size": "1850.00" }
```

That is one number for the whole level. It does not tell you:

- how many distinct orders make up 1,850
- when each arrived
- which of them are yours relative to the rest
- whether the size ahead is sticky or flickering

Even a perfect 1 ms snapshot stream cannot recover this: the information is aggregated away
at the source. The queue itself lives inside the matching engine.

### Why it is not published

Publishing per-order queue position would:

- let participants front-run specific resting orders
- reveal each market maker's quoting and refresh behaviour
- turn a private matching engine into a public one

There is no version of the public API where this is exposed, and no amount of scraping or
reconstruction gets it back.

### What we do instead

**Bracket the fill, and require the strategy to survive the pessimistic end.**

| Assumption | Fill rule | Interpretation |
|---|---|---|
| Front of queue | any trade at your price fills you | best case, not achievable in practice |
| Back of queue (default) | cumulative traded volume at or through your price must exceed the size that was resting at your level when you placed | conservative, realistic |
| Pro-rata | fill proportional to your share of the level | middle case, appropriate for large levels |

If a strategy is only profitable at front-of-queue, it is not a strategy — it is an
assumption. This is why the simulator exposes `queue_share ∈ {0.0, 0.5, 1.0}` and why the
kill criteria require the edge to survive the pessimistic setting.

### What this rules out

Passive-only market making, and any "I would have been filled at the touch" assumption.
Taker entries are unaffected: crossing the spread needs no queue.

---

## 3. Historical comments / sentiment

### Why it is not reconstructable

Comments are stored in Polymarket's off-chain application database. The public surfaces are:

- `gamma-api.polymarket.com/comments` — a paginated read
- the `comments` topic on the RTDS WebSocket — a live stream

Neither is an archive. Specifically:

- **No bulk export exists.** There is no documented endpoint or dataset that returns all
  comments across all markets for a historical window.
- **The API reflects current state, not as-of state.** If a comment is edited or deleted,
  the prior version is not recoverable. A historical reconstruction is therefore not
  reproducible even for comments that still exist.
- **Deep pagination coverage is undocumented.** You can crawl *some* history per market,
  but there is no guarantee of completeness, and completeness is exactly what research
  needs.
- **Third-party mirrors are partial and unlicensed.** Nothing we found provides a
  research-grade historical comment corpus.

So the honest classification is: **forward-only**. Anything before the day you start
recording is unavailable at research quality.

### What we do instead

1. **Record from now on.** The RTDS `comments` topic plus a periodic `/comments` crawl,
   stored raw with receipt timestamps. Cheap, and it compounds.
2. **Substitute external sentiment for history.** These have real archives and are free:
   - GDELT (global news events, 2015→present)
   - news RSS archives
   - Reddit / X archives via their APIs (rate-limited but historical)
   - Wikipedia pageviews, Google Trends
3. Accept that any comment-derived feature is **out-of-sample by construction** until the
   recording window is long enough.

### What this rules out

Comment-based sentiment signals for 2024–2025. Any model that claims to use "Polymarket
discussion" as a historical feature is either using a partial scrape or leaking.

---

## 4. Order book depth after 2026-08-10

This one is different: **not impossible, just unrecorded.**

- PMXT v2 (the only free full-L2 archive) stops at 2026-08-10T00; `2026-08-11T00` onward
  returns 404.
- Dome, the previous archive, died on 2026-04-28.
- No free source has recorded it since.

Unlike items 1–3, this gap **closes over time** if you run a recorder. Every day of
recording is a day of future history. That is why `roadmap.md` §8 treats the recorder as
the highest-ROI work in the project, and why Phase 1 mirrors the existing archive before
anything else: it is the only copy.

---

## 5. Lifecycle and resolution data stop at 2026-04

The trade tape and the lifecycle archive have **different end dates**, and the gap lands
exactly on the forward-test window.

| Dataset | Coverage end |
|---|---|
| SII `trades.parquet` | 2026-10-04 |
| `moose-code` `condition`, `split`, `merge`, `redemption`, `neg_risk_conversion`, `fee_refunded` | **2026-04-28** |
| `moose-code` `condition` coverage of markets created in 2026-05 or later | **0 %** |

What this breaks, precisely:

- **Resolution labels for 2026-05 → 2026-10.** 685,918 markets with volume > 0 in the
  forward-test window have no local on-chain payout. Use `/v2/tokens` `final_price` per
  market (validated 225/225 against on-chain payouts where both exist), or backfill
  `ConditionResolution` logs from Polygon. Do **not** substitute `outcome_prices` — it is a
  final price, and it differs from the payout (see `roadmap.md` §2.5).
- **Position reconstruction after 2026-04.** Splits, merges and redemptions are unavailable
  offline for that era. `/v2/activity?user=` closes this **per wallet** (it returns maker and
  taker trades plus splits/merges/redeems), so reconstruction of a chosen wallet is possible;
  bulk lifecycle events for all wallets are not.
- **Fee validation after 2026-04.** `fee_refunded` stops at the same date, so a fee
  formula can only be validated against realised rebates up to 2026-04.

This is fixable — it is a missing index, not missing chain history — but it is a real cost,
and it is why Phase 3 must not assume redemption events exist.

---

## 6. Impact by strategy class

| Strategy class | Order events | Queue position | Historical comments | Post-Aug depth |
|---|---|---|---|---|
| Fundamental / event-driven, taker entry, hold to resolution | irrelevant | irrelevant | substitute | irrelevant |
| Cross-market / neg-risk arbitrage | low | medium (one leg may be passive) | irrelevant | medium |
| Momentum, taker entry | irrelevant | irrelevant | low | **blocking** |
| Momentum, passive entry | medium | **blocking** | low | **blocking** |
| Market making | **blocking** | **blocking** | irrelevant | **blocking** |

Read this table before writing a hypothesis. If a row has a blocking cell, the strategy
cannot be validated with the free stack, and no amount of engineering changes that.

---

## 7. Reproducibility rule

Any claim in a research write-up that depends on one of these four items must be labelled:

- **observed** — measured from data we hold
- **bounded** — bracketed between explicit best/worst assumptions (queue, impact)
- **assumed** — an explicit parameter with a stated value and a sensitivity check

An unlabelled assumption is how backtests become fiction.
