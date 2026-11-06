# Thin levels are sweep magnets, deep levels are not

claim:      Fills taken on a side whose touch level size is in the bottom quartile of its own trailing distribution carry markout of -1.0 c/share or worse, versus -0.2 c/share or better in the top quartile, so shrinking the thin side (and never the deep one) improves paired net by >= $0.001/share at matched fill count.

mechanism:  A print reaches our quote only after consuming the level ahead of it; when the displayed level is thin, one aggressive print clears several ticks and the fill is a sweep, while a deep level absorbs the same print within the level and the fill is noise. Depth is the barrier that decides whether the trade carries information, which is the same reason price impact scales inversely with depth in Cont, Kukanov & Stoikov (2014). The measured logistic here already says fill probability falls with level size (coefficient -0.5733 on log1p(size)); the claim is that the *conditional* markout rises faster than the probability falls, so exposure should be pulled from thin levels. This uses level depth, not our FIFO position, and not any price signal.

data:       DecisionContext only: bid_size_at_level, ask_size_at_level, bid_queue_ratio, ask_queue_ratio, mid. A 200-event per-side ring buffer of level sizes is kept inside the module from on_book/decide. No derived table.

module:     Maintain two deques of level size per side (last 200 observations, no clock). In decide compute the p25 and p75 of each side's own history; for a side whose current size_at_level is below p25 and whose queue ratio is >= 0.5, return SHRINK with scale 0.01 (engine floor = min size); for a side above p75 return QUOTE at full scale; between them keep the current action. If a buffer holds fewer than 30 observations, return QUOTE both (no distribution yet).

metric:     Paired per-condition net against reactionary, with the markout difference between the bottom and top level-size quartiles reported as the mechanism check; the deciding number is the paired net delta per share.

kill:       Kill if the quartile markout gap is below 0.3 c/share, or if the bottom-quartile fills are not worse than -0.5 c/share, or if the paired net delta is negative on either slice.

sources:    Cont, Kukanov & Stoikov, "The Price Impact of Order Book Events" — impact is linear in order-flow imbalance and inversely proportional to depth: https://arxiv.org/abs/1011.6402. Queue-position value and adverse-selection cost of depth ahead: Moallemi & Yuan (2016), https://moallemi.com/ciamac/papers/queue-value-2016.pdf. Measured fill model in this repo: src/polymarketbot/paper_trading/orchestrator.py.
