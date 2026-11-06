# The engine's OFI counts quote moves; money counts shares

`slug:` micro-volume-imbalance-gate
`claim:` `DecisionContext.ofi` is the mean sign of top-of-book *steps*, so a 5-share flicker and a 5,000-share sweep contribute equally; a share-weighted taker imbalance over the same window flips sign relative to `ofi` on a measurable fraction of events, and gating participation on the weighted version improves markout on liquid markets.
`market scope:` high-volume only — needs prints to weight; on the reward universe the share-weighted signal is undefined and the rule must fall back to baseline.
`data:` `TradeContext.size`, `TradeContext.side`, `TradeContext.price`, `TradeContext.seconds_elapsed` via `on_trade`; `DecisionContext.ofi` (for the disagreement test), `DecisionContext.book.best_bid`, `DecisionContext.book.best_ask`, `DecisionContext.book.spread_cents`. Optional `prepare.sql` over `data/tape/clob_trades` (`asset_id, venue_ts_ms, price, size, side`) for a replay-native version.

## Mechanism

The engine builds `ofi` from `_prev_top` deltas: `e_bid + e_ask` per book update, then a time-window mean (`microstructure_ofi_window_s`, default 60 s). That is a *count* of quote-direction steps. It is a good gate when every update is one maker moving one tick — the calibrated A/B in `generate_quotes` shows it. It is the wrong statistic when size carries the information: a single 5,000-share lift is one step, and so is a 5-share probe.

Share-weighted taker imbalance over the same 60 s,

```
voi = (sum of taker-BUY size - sum of taker-SELL size) / (sum of all size)
```

is the standard informed-flow proxy precisely because informed traders trade size, not ticks. The two statistics disagree exactly when it matters: a burst of small two-way probes around a large one-way block. In that state `ofi` reads ≈ 0 while `voi` reads 0.8, and our quotes are on the wrong side of the block.

The rule is a symmetric market-level gate: when the weighted flow is extreme and *persistent*, both sides come off for a bounded window. Nothing about the quote changes; the reward cost is the minutes out.

## Rule

```python
# on_trade: bucket prints into 10 s bins by seconds_elapsed
bin = int(t.seconds_elapsed // 10)
vol[bin] += t.size if t.side == "BUY" else -t.size
tot[bin] += t.size

# decide
window = last 6 bins (60 s)
buy  = sum(+v for v in window if v > 0); sell = sum(-v for v in window if v < 0)
voi  = (buy - sell) / max(1e-9, buy + sell)
same_sign_bins = count of consecutive bins with sign(vol[bin]) == sign(voi)

if buy + sell < 100.0:                       return QUOTE both        # no sample
if abs(voi) >= 0.60 and same_sign_bins >= 3:
    self.off_until = ctx.seconds_elapsed + 60.0
if ctx.seconds_elapsed < self.off_until:     return PULL both
if abs(voi) <= 0.20:                         return QUOTE both        # re-entry only on decay
return last_state
```

Diagnostic to log with the replay: the fraction of decisions where `sign(voi) != sign(ctx.ofi)` and both magnitudes exceed their trigger. If that fraction is near zero, the idea is dead on arrival and should be reported as such rather than tuned.

## Why not ruled out

- Fact 1 (spread) and fact 2 (size) are untouched; the gate removes *time*, not width or size.
- Fact 3 (adverse markout at every width) is the loss; a width change cannot address a flow-driven move, which is the point of using a flow statistic.
- The measured A/B in the engine comments says the *count*-based gate flips net per share positive on 29 asset-hours / 780 fills — that is evidence for the family, not against the weighted variant; it also says the live size-weighted 850/1.5c thresholds did not, so the weighted statistic must be tested with a *share-ratio* normalisation (0-1) rather than raw share thresholds. That is what this rule does.
- Fact 6 (Kalshi) is irrelevant.
- On the highvol universe `daily_pool = 0`, so `net` is spread capture minus markout; a flow gate is the only lever left there.

## Kill criterion

Kill if `sign(voi) != sign(ofi)` at high magnitude in under 5 % of decisions (the two statistics are redundant — then just keep the existing gate), or if fills made while `|voi| >= 0.6` have 5-minute markout within 25 % of baseline, or if the gate removes more than 30 % of a liquid market's quoting time without a matching net improvement.

## Replay cost

In-module: no SQL, no signals, same pass. The trade feed only covers reward markets — the harness says the collector's trade subscription excludes all high-volume markets — so for the highvol universe either run `--synthetic-prints` (rebuilds taker prints from `price_change` level decreases, already implemented in `replay_all_markets.py`) or precompute `voi` via `prepare.sql` over `data/tape/clob_trades` plus the synthetic-print derivation. Either way the data already exists on the tape; no new feed.
