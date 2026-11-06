# Terminal jumps are the only risk that never mean-reverts

claim:      Fills in the last 60 minutes before a market's end_date carry markout at least 2x worse than the same market's earlier fills, and flattening (min size, no adding) in the final 6 hours with a full pull in the final hour improves paired net by >= $0.002/share on non-sentinel markets.

mechanism:  A binary mid near expiry is a martingale that can jump to 0 or 1 on a single news item, so the inventory term is bounded above by the distance to the boundary while the reward term does not grow at all; Avellaneda-Stoikov's (T-t) term goes to zero, meaning the inventory penalty per unit time is largest exactly when the remaining time is shortest - but the size that carries it should shrink, and the engine has no expiry input. The counterparty is the informed trader who knows the outcome before the mid does. The repo's own caveat applies: end_date is not resolution time (median end_date to resolved_ts is +4 h, p10 -133 h), so the rule is deliberately one-sided (flatten early, never last-minute).

data:       Universe snapshot end_date/slug plus clob_events for the market's own tape; prepare.sql materialises data/derived/ideas/micro-expiry-flatten/ttr.parquet with (asset_id, hour_utc, ttr_seconds, is_sentinel) joining the universe manifest to the tape's asset list, and the harness injects signals["ttr_seconds"].

module:     At setup store ttr_seconds when injected (else None). In decide: if ttr_seconds <= 3600, PULL the side that would increase |inventory| and keep only the reducing side at the min-size floor; if 3600 < ttr_seconds <= 21600, SHRINK both sides to the floor (scale 0.01) and stop adding beyond 0.5 * max_inventory; if ttr_seconds > 21600 or the signal is missing, QUOTE both at full scale. Sentinel markets (`sentinel: true` in the snapshot) are never scored below 6 h, because their end_date is a rolling placeholder.

metric:     Paired per-condition net, with the markout of final-hour fills versus the same condition's earlier fills as the mechanism check, and rewards_usd reported as the forfeited term; the deciding number is the paired net delta per share.

kill:       Kill if the final-hour markout is within 0.5 c/share of the same market's earlier markout, or if the end_date-to-resolution error makes the window fire in fewer than 20 conditions, or if reward loss exceeds the markout saving on both slices.

sources:    Avellaneda & Stoikov (2008), the (T-t) inventory term: https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf. Prediction-market expiry behaviour and resolution timing in this repo: docs/strategy-thesis.md §"Not established" (end_date vs resolved_ts, median +4 h, p10 -133 h); docs/kalshi-hypotheses.md H4 on settlement windows. Market metadata fields: https://docs.polymarket.com/market-data/market-details.
