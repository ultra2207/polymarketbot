# Ideas

One directory per testable idea. The registry (`registry.json`) is the only
index; the harness (`scripts/replay_all_markets.py`) loads
`ideas.<slug>.module:STRATEGY` and nothing else from here.

```
ideas/<slug>/
  spec.md        claim, mechanism, data, module sketch, metric, kill condition
  module.py      STRATEGY = <StrategyModule instance>
  prepare.sql    optional: materialises derived data from the tape
  result.json    written by the scheduler, never by hand
```

Rules (from `plan.md` §3–5):

- A module reads only its `DecisionContext`. No tape access, no clock, no
  network, no other idea's files.
- `spec.md` fields are literal: `claim`, `mechanism`, `data`, `module`,
  `metric`, `kill`. An idea without a kill condition is rejected.
- Two ideas are the same idea if their `mechanism` matches: what moves the
  price, and why we get paid for being on the other side. Parameters are not
  new ideas.
- `result.json` is produced by `scripts/run_idea_queue.py`. An agent may not
  write a result it did not run, and may not promote its own idea.
