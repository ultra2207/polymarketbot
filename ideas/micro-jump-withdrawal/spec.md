# Pull on the jump, not after it

claim:      A >= 2-tick mid move inside <= 3 book events identifies an information arrival; quotes that stay resting through the following 5 events are filled at markout 1.0 c/share worse than quotes pulled for those 5 events, and a jump-triggered withdrawal improves paired net by >= $0.001/share while costing < 5 % of reward time.

mechanism:  The tape is event-driven, so a two-tick repricing in three events is not drift, it is someone acting on news; the counterparty takes the stale side of every quote left behind, and our cancel path upper bound (217.5 ms measured CLOB minimum RTT) is shorter than the taker's round trip, which is why the repo's Binance lead is defensive rather than directional. The measured asymmetry is that the engine re-prices on the next book update anyway, so the only decision left is whether to be present in the gap; the OFI gate's 60 s trailing mean cannot see a jump until it is over. This is information arrival, distinct from bounce and from queue state.

data:       DecisionContext only: book.mid and best_bid/best_ask per update; the module keeps the previous mid and a small event counter. No derived table, no clock.

module:     Keep last_mid and a jump window counter. In decide, if |mid - last_mid| >= 2 * tick_size, set cooldown = 5 and record the direction. While cooldown > 0 (decremented once per decide call), return PULL on both sides - a jump is directionally unresolved at the moment of the move and the engine's own reprice will re-establish the quote afterwards. When cooldown hits zero, return QUOTE both. Never extend the cooldown from the same jump (a second, larger move is a new jump by construction).

metric:     Paired per-condition net, with the markout of fills within 5 events of a jump versus fills outside such windows as the mechanism check, and time_quoted_frac as the reward cost.

kill:       Kill if the in-window fill markout is not worse by 0.3 c/share, or if jumps occur in fewer than 2 % of updates (too rare to matter), or if the reward forfeiture exceeds the markout saving on both slices.

sources:    Stale-quote sniping and the cancel/oracle race: Aquilina, Budish & O'Neill, "Quantifying the High-Frequency Trading Arms Race", https://www.aeaweb.org/conference/2021/preliminary/paper/Tt353Bff. Exchange latency and liquidity: "Need for Speed? Exchange Latency and Liquidity", https://efmaefm.org/0efmameetings/EFMA%20ANNUAL%20MEETINGS/2015-Amsterdam/papers/EFMA2015_0302_fullpaper.pdf. Measured Polymarket cancel path and Binance lead: docs/predictive-mm-analysis.md.
