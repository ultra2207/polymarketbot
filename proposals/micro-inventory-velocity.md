# Inventory velocity is the fill-side confession: stop quoting while it is one-way

`slug:` micro-inventory-velocity
`claim:` The rate at which `inventory_shares` changes is a direct measurement of who is taking from us; when the signed fill rate stays above a per-market baseline for two minutes, we are the exit liquidity for a one-way flow, and standing down until the rate reverts improves paired net without changing price or size.
`market scope:` all markets; fires rarely on thin markets, which is fine.
`data:` `DecisionContext.inventory_shares`, `DecisionContext.max_inventory_shares`, `DecisionContext.fills_count`, `DecisionContext.realized_pnl`, `DecisionContext.seconds_elapsed`, `DecisionContext.book.mid`. Module state only; no prepared signals.

## Mechanism

Fills are the only event that changes `inventory_shares` (the engine merges change it too, but merges are rare and visible as a jump of both tokens). So `d(inventory)/dt` between two `decide` calls is our signed fill flow, in shares per second, without needing any trade feed or any knowledge of which side filled.

Sustained one-way inventory accumulation has a specific meaning: our quotes are systematically on the wrong side of the *informed* flow, not the noise flow. A noise taker buys and sells; a noise-dominated market leaves inventory oscillating around zero. A trend-dominated market shows a persistent drift with the same sign as the mid's eventual move. That is the definition of being adversely selected, and it is measured from our own account rather than inferred from the book.

The action is a full stand-down, not a size change and not a one-sided pull: both sides come off for a bounded window, and the plateau is re-entered when the signed rate decays. The reward cost is the minutes spent out; the benefit is not being the counterparty to the rest of the trend.

Distinct from neighbours: `micro-adaptive-exit` uses the *level* of `realized_pnl` and withdraws semi-permanently; `micro-inventory-skew-size` changes size; `micro-gate-inventory-interaction` treats a flattening fill as valuable. This rule uses the *derivative* of inventory and treats it as a temporary, self-clearing state.

## Rule

```python
# state
self.prev_inv, self.prev_t, self.ema   = 0.0, 0.0, 0.0
self.off_until                          = -inf

def decide(ctx):
    dt  = max(1e-3, ctx.seconds_elapsed - self.prev_t)
    din = ctx.inventory_shares - self.prev_inv
    rate = din / dt                                   # shares per second, signed
    self.ema = 0.9 * self.ema + 0.1 * rate            # ~10-decision horizon
    self.prev_inv, self.prev_t = ctx.inventory_shares, ctx.seconds_elapsed

    cap  = max(1.0, ctx.max_inventory_shares)
    norm = abs(self.ema) * 60.0 / cap                 # fraction of cap per minute

    if abs(self.ema) * 60.0 >= 0.25 * cap:            # a quarter of the cap per minute
        self.off_until = ctx.seconds_elapsed + 120.0
    if ctx.seconds_elapsed < self.off_until:
        return PULL both, f"one-way fill flow {self.ema*60/cap:.2f} cap/min"
    return QUOTE both
```

The 0.25-cap-per-minute threshold matters because the engine already zeroes the *adding* side at the cap (`generate_quotes`, `inv < max_inventory_shares`): at that point the book is one-sided and the score is already collapsing. This rule exits before the cap does, at a moment when the loss is still avoidable, and re-enters when the flow is no longer one-way. Run at `--pool-multiplier 1.0` and with `max_inventory_shares = 3 * quote_size` (the harness default) so the threshold is in the same units for every market.

## Why not ruled out

- Fact 1: the quote's price never changes.
- Fact 2: sizing up is right, and this rule does the opposite of shrinking — it keeps full size and reduces *time*, which is the only lever fact 2 leaves open.
- Fact 3 (markout adverse at every width) is the loss being avoided.
- Fact 4/5 are about side-size symmetry and time-vs-size; neither speaks to a stand-down triggered by our own fill rate.
- Fact 7 (`SHRINK` no-op) is irrelevant: no shrink.
- The engine's `realized_pnl` is already per condition and available in ctx; this rule is deliberately *not* using it (level) but the flow that produces it (derivative), which is the part that predicts.

## Kill criterion

Kill if `|ema| * 60 >= 0.25 * cap` fires on fewer than 3 % of markets in a 10-minute slice (too few fills to measure — fact 1 says 59 fills across 500 markets, so this is the main risk and should be checked first on a longer slice), or if fills made in the 5 minutes after a stand-down trigger have markout no worse than baseline (then the flow was noise and the rule only costs reward), or if paired net is not positive on both slices.

## Replay cost

No SQL, no signals. The main cost is slice length: with 59 fills per 10 minutes across 500 markets, run at least a full hour (`date=2026-11-06/hour=07`) and ideally three hours, because the trigger is a fill-rate event. The strategy itself adds only a few arithmetic operations per decision.
