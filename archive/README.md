# Archive

Retired strategy code, kept for reference. **Nothing here is importable or wired
into a running process** — the packages were moved out of `src/polymarketbot/`
and every importer was edited to stop importing them. There are no re-export
shims on purpose: if you want this code back, move it back and re-wire it.

The verdicts and the numbers that killed each idea live in
[`docs/alpha-register.md`](../docs/alpha-register.md). Read that first — this
directory exists so the measurement is not re-derived, not so the code is
revived.

## What is here

| Path | Was | Why retired | Register |
|---|---|---|---|
| `strategy/settlement/` | SettlementHarvester: discounted-to-par carry on near-resolved outcomes, with the hazard/expected-shortfall allocator | Edge is paid by another trader; the measured discount sits below fees + spread | #16–#19 |
| `paper/pillars/settlement_runner.py` | Paper trader's settlement pillar | Pillar removed from the paper engine | — |
| `paper/pillars/tail_budget.py` | Tail-budget sizing for settlement | Only the settlement pillar sized against it | — |
| `paper/radar.py`, `paper/uma_poll.py` | Settlement radar and UMA verdict feed | Settlement-only I/O | — |
| `live/settlement/`, `live/supervisor/desk.py` | Live settlement desk | Settlement strategy removed from the live trader | — |
| `tests/*settlement*`, `test_robust_hazard.py`, `test_uma_verdict_poll.py` | Tests for the above | Test the archived code | — |
| `data/uma.py`, `tests/test_uma.py` | UMA Optimistic Oracle / CTF-adapter log decoder and its ABI vectors | No `src/` importer after the collector was deleted; the replay that consumed it is gone too, so `data/derived/uma_events.parquet` is frozen. The decoder is kept because the topic0 table is verified against mainnet | #20, #33 |

## What deliberately did NOT move

- `strategy/onchain/` — the chain layer (CTF positions, merge, redeem, gas). It is
  **not a strategy**: the reward harvester imports `estimate_merge_cost_usd`,
  `calculate_optimal_merge_threshold` and `onchain.ctf.gas` to price inventory
  merges. Archiving it would break the one live strategy.
- `live/engine/merge.py`, `live/engine/onchain.py`, `live/engine/reconcile.py` —
  CTF merge/redeem for freeing capital. That is reward inventory management, not
  the settlement strategy.

## Crypto up/down carry (register #30, #31)

The crypto favourite carry was never a separate module — it lived in
`polymarketbot.tools.crypto_updown_study`. That tool stays in `src/` because it
is cheap, runs on `python -m`, and its `bets` / `maker` subcommands are the
instruments you would want if the favourite-longshot question is ever reopened.
Its conclusions are falsified and recorded; the tool is a measuring device, not a
strategy.
