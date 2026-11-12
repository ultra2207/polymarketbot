# H2: Does Kalshi taker flow initiate Polymarket trading?

> **Tools (2026-11-11).** `scripts/report_kalshi_trade_lead.py` (and every
> other `scripts/*.py` named here) was deleted in the 2026-11-10 scripts
> cleanup; it has no `python -m` replacement. The measurements are kept as the
> record of that run and cannot be reproduced from this tree.

**Method.** Event pairing on the two trade tapes, both of which carry venue
timestamps at millisecond resolution. For every Kalshi print on a mapped ticker:
how long until the next Polymarket print on the paired condition, and does it
agree in direction at the YES level? Three controls decide whether the answer
means anything:

1. **Null** — the same statistic at random times in the window, never within
   5 s of a Kalshi print. A busy market cannot manufacture a lead by itself.
2. **Chance agreement** — `p(+)q(+) + p(-)q(-)` from the observed marginals. A
   market where most prints are YES-buys shows agreement above one half with no
   lead at all.
3. **Reverse** — Polymarket print → next Kalshi print. Symmetric agreement means
   one common event (co-movement); a lead shows in one direction only.
4. **Per-condition** — the condition is the independent unit. Prints cluster
   inside a condition, so the event-level z overstates precision.

Reproduce: `.venv/Scripts/python.exe scripts/report_kalshi_trade_lead.py`
(writes `data/derived/kalshi_trade_lead.parquet`, reads only sealed hours that
both tapes have).

## First result — 6 sealed hours (2026-11-05 14:00–20:00 UTC), 29 conditions

6,841 mapped Kalshi prints, 4,169 Polymarket prints on the paired tokens, no
block prints in the window, print size p50 = 31, p90 = 306 contracts.

| direction | H=1s, agree | H=5s | H=30s | chance | n at 1 s |
|---|---|---|---|---|---|
| Kalshi → PM, all prints | **0.696** | 0.635 | 0.569 | 0.503 | 1,143 |
| Kalshi → PM, size ≥ p90 | **0.942** | 0.925 | 0.831 | 0.501 | 189 |
| PM → Kalshi (reverse) | **0.227** | 0.292 | 0.330 | 0.502 | 731 |

Promptness: the next Polymarket print arrives **14.3 s** after a Kalshi print
on average versus **26.3 s** at a random time (censored at 60 s); 17 % of Kalshi
prints see a Polymarket print within 1 s versus **1 %** at random.

Per condition (n ≥ 20 matched prints, H=5 s): **7 of 8 conditions beat their own
chance agreement** (median 0.76). The largest condition (n=455) is only +0.06
over chance, so the effect is not uniform. For the top size decile, 3 of 4
conditions beat chance; one of those is only +0.03, another +0.13, the strongest
+0.29.

## What this supports

* Kalshi prints **precede** Polymarket prints at the same second, and the
  direction carries through: a Kalshi taker buy is followed by a Polymarket
  taker buy of the same side far more often than chance.
* The asymmetry with the reverse direction (0.23, i.e. opposite-side prints)
  rules out "both venues react to one common event at the same instant" as the
  whole explanation: a common shock would give agreement above chance in both
  directions.
* Large Kalshi prints carry the signal: 0.94 agreement at 1 s for the top size
  decile.

## What this does not support

* **Not a PnL claim.** This measures direction agreement, not markout. Sizing a
  defensive pull needs the markout arm of H1/H3 on the same events.
* **Not enough conditions.** 8 conditions over 6 hours is a hint, not a
  measurement. The effect is absent in the largest condition and weak in two of
  four top-decile conditions. The register's bar is ≥ 50 pairs.
* **No sub-second resolution.** The tapes carry millisecond event times, but
  detection is only as fast as the poll: at the previous 8 s cadence the signal
  was unusable (PM follows within 1 s). The trades stream now polls at **2 s**
  with cursor catch-up, which is the fastest safe cadence (`/markets/trades`
  allows ~2 req/s on the basic tier).

## Operational consequence

A large Kalshi print on a mapped ticker is a **cancel trigger** for the
Polymarket side it threatens: pull/shrink the threatened quote, keep the other
side. That is the same defensive action the basis monitor takes, but driven by
an event that arrives in milliseconds rather than a 2 s book snapshot. It stays
opt-in (`--orchestrator-signal kalshi`) until per-condition agreement holds on
≥ 50 conditions.
