# Price every pull in forfeited reward dollars

claim:      A pull that is decided on risk alone over-pulls, because reward income is proportional to time-in-band while fills arrive at roughly 1 % per episode; pricing each pull as (forfeited epoch reward) versus (expected fill saving) improves paired net by >= 5 % at the same or lower fill count than the fixed ratio-0.60 gate, while keeping time_quoted_frac above 0.90.

mechanism:  The reward is sampled once per minute at a random offset and summed over the epoch, so being out of the book for a second costs reward in expectation at the full rate, whereas the fill it avoids is a ~1 % event (the repo's backtester measures 0.98 % fills per episode). The existing queue gate spends reward without a price: it compares a queue ratio to 0.60 and never asks whether the saved markout exceeds the forfeited dollars. The pool underpays any gate whose saving is below the reward it discards, and the taker profits from exactly those pulls. No new signal is needed; the mechanism is the unit conversion.

data:       DecisionContext (queue ratios, sizes, inventory, ofi) + MarketView (rate_per_day, rewards_min_size, rewards_max_spread). Priors on markout from docs/predictive-mm-analysis.md (front +0.36 c/share, back -2.01 c/share, baseline -0.628 c/share on 777 marked fills). No derived table.

module:     Maintain a per-side counter of decision events and pulled events, and the per-side score fraction f = ((v - s)/v)^2. Expected forfeited reward per pulled event = (rate_per_day / 1440) * share_hat / events_per_epoch * f, where share_hat is our observed v_score/(v_score + comp) when available from ctx.signals and 0.05 otherwise; expected saving = P_fill(ratio, level_size) * markout_prior(ratio) * quote_size, using the published logistic coefficients. Pull only when saving >= 2x forfeited; otherwise SHRINK to the min-size floor (which keeps the score but cuts the fill notional), and release the shrink once the ratio falls back below 0.45.

metric:     Paired per-condition net with rewards_usd, fills and time_quoted_frac reported together; deciding number is net at equal-or-lower fills versus queue_aware_ofi, with the 0.90 time_quoted_frac gate applied.

kill:       Kill if priced and unpriced gates land within $0.001/share on both slices, or if the priced gate needs time_quoted_frac below 0.90 to show its gain, or if the markout prior's error on the calibration hour exceeds 50 % (the conversion would be meaningless).

sources:    Polymarket liquidity rewards (one random sample per minute, 1,440 samples per epoch): https://docs.polymarket.com/programs/liquidity-rewards. Fill probability as a function of queue position: Moallemi & Yuan (2016), https://moallemi.com/ciamac/papers/queue-value-2016.pdf. Cancel-race economics under latency: Aquilina, Budish & O'Neill, "Quantifying the High-Frequency Trading Arms Race", https://www.aeaweb.org/conference/2021/preliminary/paper/Tt353Bff.
