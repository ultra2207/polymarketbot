# When the book widens without a move, the makers know something

claim:      A spread widening of >= 2 ticks with no mid change in the same window is followed by fills at markout 1.0 c/share worse than the market's quiet-state fills; pulling both sides for 5 events after such an episode improves paired net by >= $0.001/share and costs less than 5 % of reward time.

mechanism:  Other makers widening is itself information: it is the visible half of a withdrawal decision, and it is observable in our context one event before the sweep it precedes. This is the mirror image of the jump rule - here the price has not moved yet, so the OFI gate sees nothing and the mid-based signals are silent, while the spread says the book's own risk premium just doubled. The general result that liquidity provision contracts ahead of informed trading (and that the race is between cancel and snipe) is exactly what a widening encodes. The counterparty is the informed taker who is about to cross a now-wide spread; our quote, if left inside it, is the cheapest thing to hit. Distinct from jump detection (no price move), from bounce (spread is not oscillating), and from depth imbalance (both sides moved).

data:       DecisionContext only: spread_cents, tick_size, mid, best_bid, best_ask per update; the module keeps a 50-event deque of spread_cents and of mids. No derived table, no clock.

module:     Keep last 50 spread observations per condition and the median. In decide, if spread_cents >= median_spread + 2 * tick_size * 100 and |mid - mid_50_events_ago| < 1 tick, set cooldown = 5; while cooldown > 0, PULL both sides. If the widening came with a >= 2-tick move, treat it as a jump rather than a widening (micro-jump-withdrawal owns that mechanism; here, do nothing special). Resume QUOTE both when the cooldown expires or the spread returns to its median.

metric:     Paired per-condition net with the post-widening versus quiet markout gap as the mechanism check, and time_quoted_frac as the reward cost; the deciding number is the paired net delta per share.

kill:       Kill if the post-widening markout gap is below 0.3 c/share, or if widening episodes occur in fewer than 2 % of updates, or if reward forfeiture exceeds the markout saving on both slices.

sources:    Liquidity withdrawal around informed trading and the cancel-vs-snipe race: Aquilina, Budish & O'Neill, "Quantifying the High-Frequency Trading Arms Race", https://www.aeaweb.org/conference/2021/preliminary/paper/Tt353Bff; "Need for Speed? Exchange Latency and Liquidity", https://efmaefm.org/0efmameetings/EFMA%20ANNUAL%20MEETINGS/2015-Amsterdam/papers/EFMA2015_0302_fullpaper.pdf. Adverse selection in prediction markets specifically: https://law.stanford.edu/press/adverse-selection-in-prediction-markets-evidence-from-kalshi.
