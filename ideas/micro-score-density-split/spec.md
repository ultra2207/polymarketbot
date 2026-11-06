# Score density: the quadratic pays four times more for the same shares at half the distance

claim:      Because S is quadratic in distance and linear in size, inventory skew plus post-only clamping make the two sides of one book differ in reward per share by >= 1.5x in at least 20 % of updates; reallocating size to the higher-density side raises rewards_usd per unit of fill_notional by >= 10 % with unchanged time_quoted_frac.

mechanism:  S = ((v - s)/v)^2 * size, so a side at s = v/2 earns a quarter of the score of a side at s = 0 for identical shares (Polymarket docs). The engine prices both sides from the same mid +- offset but post-only clamping and inventory skew break the symmetry: the clamped side sits materially farther from mid and earns a smaller fraction of its size. The pool therefore underpays the symmetric quote and overpays the asymmetric one; the fix is a size rule, not a signal. This is not the queue gate: no fill probability and no flow enter, only the score geometry.

data:       DecisionContext only: mid, best_bid, best_ask, tick_size, spread_cents, inventory_shares and max_inventory_shares (for the skew term); rewards_max_spread at setup.

module:     Reconstruct the engine's resting prices rather than the raw mid: bid_px = floor_tick(mid - (0.75 + 0.5 * inv_ratio) / 100) after the post-only clamp, ask_px = ceil_tick(mid + (0.75 - 0.5 * inv_ratio) / 100) after the clamp, then s_bid = max(0, (mid - bid_px) * 100) and s_ask = max(0, (ask_px - mid) * 100). Compute f_bid = ((v - s_bid)/v)^2 and f_ask = ((v - s_ask)/v)^2. Return SHRINK with scale 0.01 (engine floor = min size) on the low-density side and QUOTE at full scale on the high-density side whenever f_high / f_low >= 1.5; return QUOTE both when the ratio is below that, because there is nothing to reallocate.

metric:     rewards_usd / fill_notional per condition (paired against reactionary), with the density-ratio distribution reported alongside; the deciding number is the paired improvement in that ratio.

kill:       Kill if the share of updates with f_high / f_low >= 1.5 is below 10 % (no reallocation leverage exists), or if the paired ratio improvement is under 3 % on both slices, or if fills fall below the 200-fill / 50-condition gate.

sources:    Polymarket liquidity rewards, eq. 1 (quadratic in s, linear in size): https://docs.polymarket.com/programs/liquidity-rewards. Polymarket US program describing the same closeness-to-best-price logic as a discount factor: https://docs.polymarket.us/incentives/liquidity. Tick-size/queue-priority interaction: "Not Too Big, Not Too Small? Towards an Optimal Tick Size", https://acfr.aut.ac.nz/__data/assets/pdf_file/0008/926567/Tick-size-paper.pdf.
