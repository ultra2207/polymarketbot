# YES of one market plus NO of another is not a complete set — the only cross-market netting is neg-risk conversion

`slug:` m-cross-market-merge-basis
`focus:` treasury — is merging YES+NO inventory across two markets ever worth the gas?
`market scope:` multi-leg neg-risk events only (elections, sports, awards). On binary-only
markets the answer is structural and no test is needed.

## Mechanism

A complete set is defined by a `conditionId`. `execute_merge` reads the two tokens of *one*
condition through `_get_yes_no_positions` (`src/polymarketbot/paper_trading/engine.py:1064-1081`)
and burns `min(yes, no)` of that same condition (`engine.py:1170-1180`); the on-chain call
is `mergePositions` for that single condition (`src/polymarketbot/rewards/merger.py:167-198`).
YES in market A and NO in market B are different ERC-1155 ids with different payout vectors,
so there is no operation that nets them. **Cross-market merging does not exist, at any gas
price.** That is the direct answer to the question.

The one real cross-market primitive is neg-risk conversion *inside a single event*: 1 NO in
any leg converts into 1 YES in every other leg (`docs/polymarket-mechanics.md` §3, NegRisk
Adapter `0xd91E…` §11). The arithmetic is a basis trade, not a merge:

```
burn 1 NO_A          cost = 1 - p_A
receive 1 YES_i      value = sum_{i != A} p_i = S - p_A      (S = sum of all leg mids)
profit per share     = S - 1 - gas_per_share
```

So the conversion is profitable exactly when the event's overround `S - 1` exceeds gas. With
Polygon gas of order $0.02 per call and an overround of 1c, the break-even size is about two
shares; at 0.1c it is about twenty. Gas is therefore almost never the binding cost — the
binding costs are leg liquidity (you must own the NO leg) and the fact that holding the
basket to resolution pays exactly $1.

Because the engine has no conversion path, the trade cannot execute in the replay today. The
value of this proposal is to price it before anyone builds it. Note also that the tape is
CTF-only but does contain `NEGRISK_CTF_EXCHANGE` fills (`docs/polymarket-mechanics.md` §1),
so neg-risk legs *are* in tape scope.

## Rule

Shadow rule, `prepare.sql`, per event-minute: `event_overround = sum(mid_i) - 1` over
fresh legs only (staleness <= 60 s), plus `event_leg_count`, `min_leg_depth_shares`, and a
mapped `leg_to_convert` (the leg with the largest `p`). Then:

```
convert_gate = (event_overround >= 0.01)
           and (min_leg_depth_shares >= N*)
           and (event_leg_count >= 3)
```

`N* = gas_usd / (event_overround - 0.002)` shares. The module side reads
`signals["event_overround"]` and `signals["event_leg_count"]` in `decide` and, while the
gate is false, does not attempt to hold a leg purely to convert it (it quotes normally).
The event-level risk half is already implemented but dormant: `_event_exposure`
(`engine.py:370-378`) feeds `validate_quote`, which only runs when a risk manager is
attached (`engine.py:645-700`) — and the replay attaches none
(`scripts/replay_all_markets.py:363-368`).

**Already implemented:** same-condition merging (`engine.py:1137,1162`), gas accounting,
and event-aggregated exposure. **Not implemented:** any conversion/neg-risk path, and any
use of the event overround as a signal.

## Why not ruled out

`cross-negrisk-convert-flow` quotes the leg the converters are forced to buy — it trades
*their* direction and never converts. Nothing in the registry computes the overround as a
carry, and nothing asks whether a *basket* can be netted across markets. Facts 1-8 are
single-market facts. The gas arithmetic above is new to the repo.

## Kill criterion

- Kill if fewer than 0.5 % of event-minutes on the tape clear `overround >= 1c + gas` at a
  size the legs can supply: the carry is a curiosity, not a strategy.
- Kill if `N*` exceeds the median qualifying leg depth in more than half the clearing
  episodes (you cannot source the basket; it is an observation, not a trade).
- Kill if `overround` p50 is below 0.2c — then the conversion is below the tick and the
  whole channel is noise.

## Data needed

Tape only: `data/tape/universe/*.json` (condition_id, event_id, `neg_risk`, clob_tokens),
`clob_events` for every leg of the event, and `data/derived/universe_metadata.parquet` for
the fee side. No new feed. The conversion itself would need a new treasury method and the
NegRisk Adapter address, which is a build decision this proposal does not assume.
