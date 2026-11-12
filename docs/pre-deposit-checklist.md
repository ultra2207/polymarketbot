# Pre-deposit checklist

Run in order. Nothing here places an order until step 4.

| # | Action | How you know it passed |
|---|---|---|
| 1 | Create Polymarket API creds + signing key; set `POLY_API_KEY`, `POLY_SECRET`, `POLY_PASSPHRASE`, `PRIVATE_KEY` (aliases: `POLYMARKET_SECRET`, `POLY_PRIVATE_KEY`). The signer address is `POLY_ADDRESS` or `PROXY_WALLET_ADDRESS` | — |
| 2 | **Fund nothing yet.** Run `--live --yes` on the unfunded wallet | It must abort at preflight with a summary naming missing credentials / $0 collateral / POL / UNAPPROVED allowances. It refuses **before** quoting and does not downgrade to dry-run. Do this *before* depositing — once funded the same command trades. |
| 3 | Fund **$40 USDC.e** to the Polymarket proxy wallet + **1–2 POL** for merge gas; approve CTF exchange allowances | Re-run step 2; `PreflightReport.summary()` all green |
| 3b | Run the 30-minute paper soak on the same tree | `uv run python -m polymarketbot.paper.main --capital 3000 --duration 1800 --interval 10 --fresh --db data/paper_soak_telemetry.db` returns; the dashboard quoted both legs and the fill tape recorded activity |
| 4 | Run the canary, 1 hour, on an explicitly chosen **non-neg-risk** rewards market | Command below |

```bash
uv run python -m polymarketbot.live.runner --live --yes \
  --condition-id <non-neg-risk-condition-id> \
  --token-yes <yes-token-id> --token-no <no-token-id> \
  --min-size 5 --quote-size 5 --daily-reward 0 \
  --max-budget 20 --duration 3600 --record-live-data
```

## The order-size rule (why 5 shares is not a size)

The market's `orderMinSize` is a **$5 USDC notional**, not a share count, so a
5-share quote is sub-minimum on any leg priced below $1.00. The runner therefore
sizes both legs at one share count — the count that clears the minimum on the
cheap leg:

```
q = max(configured, ceil(orderMinSize / (cheap_leg_price - one tick)))
pair cost = q * (p_yes + p_no)          # <= q, since a binary pair is <= $1
```

Both legs quote at `q`; the legs are never sized by equal dollars, because that
leaves an unhedged residual in the cheap leg. If one matched pair costs more
than the sizing cap (`--max-budget`, or `--budget-pct` of the wallet), startup
**aborts** with the exact dollar amount needed instead of quoting one leg
against a different share count. At `q=11` on a 0.50/0.50 market the pair is
about $10.9, so `--max-budget 20` leaves headroom for a reprice.

## What step 2 checks

`RealMoneyTradingEngine.preflight_check()` (`live/engine/preflight.py`) reports, in one
summary: CLOB credential presence, Polygon RPC reachability, CLOB free collateral,
POL gas balance, and CTF exchange allowance state. `run_startup_check`
(`live/supervisor/startup.py:174`) returns exit code 1 when `ready_for_live` is false
and never downgrades to dry-run.

## Capture during the canary

1. `cancel_rtt_ms` p50/p95 from the tape's `latency` table.
2. Cancels that lost the race to a fill (`quote_cancelled` after a fill on the same order id).
3. `micro_risk_decision` rows in the tape's `risk_events` table: `action`, `queue_ahead`,
   `queue_ahead_at_entry`, `ofi_10_events` vs `ofi_threshold`, `drift_cents` vs
   `drift_threshold_cents`, `queue_cost_usd` vs `pull_cost_usd`, `measured`.
4. Preflight values at start: USDC, POL, allowances.

Query:

```python
import glob, pandas as pd
rows = pd.concat(pd.read_parquet(p) for p in glob.glob("data/live_tape/*/risk_events/*.parquet"))
rows[rows.kind == "micro_risk_decision"].sort_values("recv_ts_ns")
```

## Limits to carry into the run

- **Startup refuses live without a fitted microstructure calibration.** If
  `data/derived/risk_calibration.json` is absent the engine reports
  `micro_gate_active=False` and the runner aborts (exit 1) rather than trading with
  the toxic-flow/queue veto silently off. See `docs/real-money-blockers-2026-11-12.md` (B2).

- **Below 20 shares earns $0 rewards.** The campaign minimum is 20 shares; the canary
  buys mechanics (place, rest, cancel, fill, detect), not income. A `$40` income attempt
  must quote **at least 20 shares per leg** (the 2026-11-12 scan found one non-neg-risk
  market affordable at 38 shares/leg with a modelled payout clearing `$1/day`; see the
  addendum in `docs/minimum-live-capital.md`). The robust measured bankroll for the
  median head pool is still `$358`.
- **Neg-risk markets are refused live.** `mergePositions` has no NegRiskAdapter path,
  so auto-select now skips a stated neg-risk candidate in live mode and startup still
  refuses one outright. Pass `--condition-id` + tokens for the market you chose.
- **Auto-select at a small cap may find nothing.** At a $10–$20 cap the screen can reject
  all 250 head pools (capital, mid, expiry) and fall back to a pool-less market. Choose
  the market explicitly for the canary.
- **The fee schedule is read at startup.** `startup.py` loads the market's own
  `FeeSchedule` (`source=` in the log) and passes it to the engine; a market with no
  fees logs `source=zero`.
- **The spread offset must be measured or stated.** A market with no qualifying prints
  in the 24 h tape window has no measured optimum; pass `--spread-offset` explicitly
  (the last measured solve was 0.50c at the touch).
- **Reported `realized_pnl` is indicative.** Cash is reconciled from the venue every
  15 s; accumulated PnL is not exact. Use the venue balance for the money answer.

## Already verified in code (no action needed)

- Full suite **926 tests green**; `ruff check src tests` clean.
- Notional sizing rule covered by `tests/test_notional_quote_sizing.py`; exercised in a
  live-path dry-run: configured 5 shares were raised to the notional-clearing count and
  the previously rejected leg was accepted.
- Unified risk engine wired end to end, including the feed wiring and the placement-gate
  veto (`tests/test_risk_micro_gate.py`).
- Pooled calibration artifacts: every partial's sufficient statistics reconcile
  (`unreconciled: {}`), loaders build the risk gate, queue model, deskew and reserve.
- Clock offset vs CLOB `/time`: −0.159 s mean, 0.592 s max; RTT p50 ~191 ms.
