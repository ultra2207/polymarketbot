# Signal audit: every feed we collect, and whether it pays

One question per feed: does it predict a Polymarket move or the toxicity of a
fill, better than Polymarket's own book already does? Measured on the collected
tape, 2026-11-04 to 11-06. Method throughout: 1-second grid, **as-of alignment**
(`searchsorted`, never `np.interp`, which invents steps between sparse updates),
hit rate compared against the base rate on the same conditions.

## The table

| feed | what we measured | verdict | why it fails / what it would need |
|---|---|---|---|
| **`clob_events`** (12 GB) | book of 623 markets/hour, 10.8 M rows/hour | **the only feed that pays** | it is the venue itself: books, synthetic prints, tape markout, universe ranking |
| **`clob_trades`** | 4 k prints/hour over 744 assets; zero rows on the busiest markets (they pay no rewards) | partial | only covers the collector's subscriptions, so it cannot test liquid markets |
| **Binance BBO** | 5 symbols, 129 k rows/symbol-hour; PM tracks BTC contemporaneously (corr 0.26-0.70 at lag 0, collapsing by lag 1) | **no** | crypto is 0.045 % of the reward pool ($50 of $111,857); the 62 five-minute Up/Down markets where it would matter pay $0 and are not recorded |
| **Kalshi books** | 107 tickers; 13 matched to actively-traded PM markets, all $0 pool | **no** | Kalshi's mid took 85 distinct values in 14 hours against PM's 1,834: the book is asleep, and no lag from -20 to +20 s shows correlation |
| **News** (`news_items`) | see `docs/news-findings.md` | pending | 5,636 items; the binding question is how many resolve to a tradable condition |
| **Whales** (`whale_activity`) | 165 k rows, 316 wallets, median arrival **6 h 26 m** late; 59 % of prints are the wallet acting as *maker* | **no** | wrong sign: whale-maker fills mark out **-184 bps** at 300 s (t=-3.25) — they are the ones getting picked off |
| **Comments** (`gamma_comments`) | 32,915 rows, 40 events, 81 markets with any book | **no** | the apparent volatility effect (x1.69, t=3.68) is zero-inflation from stale mids; with exact-zero changes excluded it is x1.04, t=0.32 |
| **UMA** (`uma_events`) | 272 events; `condition_id` null on all 234 proposals; 84 map by question text | **untestable** | 0 of 307 observations show the two tokens sum below 1 (`S_mid` = 1.0000, sd 0.00045), so the "losing-token discount" does not exist on this tape |
| **On-chain** (`polygon_logs`) | 11.3 M CTF logs: 1.37 M splits, 248 k merges, 670 k redeems | **no for signal, yes for cost** | flow direction is noise (up-share 51.0 % vs base 49.6 %, Spearman -0.001) and arrives 13.5 s late; but it prices gas at **$0.0016/merge**, 9.7x below the $0.015 the code assumed |
| **Speculative inventory total** | — | **one useful number** | the gas measurement is the only artefact from six feeds that changes a decision |

## What the reward-universe baseline actually earns

Three 30-minute slices, top 500 markets by liquidity, `reactionary`:

| slice | fills | rewards | maker rebate | tape_net |
|---|---|---|---|---|
| 04:10 | 102 | $13.10 | $7.37 | **+$118.43** |
| 04:50 | 136 | $10.70 | $9.30 | **+$51.31** |
| 05:30 | 111 | $9.68 | $8.98 | **-$65.06** |

Two numbers here were missing from every earlier result:

1. **The maker rebate is 70-90 % of reward income.** 1,788 of 2,152 markets
   enable fees and return 15-25 % of the taker fee to the maker. The P&L identity
   now includes it; before, 60 % of revenue was simply absent.
2. **The sign is not stable.** tape_net is +118, +51, -65 on three slices of the
   same day. The rewards and rebates are steady; the markout noise swamps them.

So the honest description of the business is: **reward income plus rebate, minus
adverse selection with a variance larger than the edge at one day of data.**

## What is left that could pay

In order of cost:

1. **Record the 62 five-minute crypto Up/Down markets** (no rewards, $278 k of
   historical volume, $49 k resting). Binance has no forward window against the
   markets we hold, but a 5-minute binary is a different race.
2. **A real trade feed on the liquid universe.** Every liquid-market number in
   `docs/replay-results.md` comes from prints rebuilt out of book deltas, which
   produce 47x more fills than the trade feed does on the same slice (2,795
   against 59). The magnitudes are unvalidated until those markets are subscribed.
3. **More hours, not more feeds.** The tape is ~8 hours on 11-06; every effect we
   found is 1-1.5 % of baseline with t of 1-2, which is what an 8-hour sample
   buys. Six speculative feeds have now produced one number between them.
