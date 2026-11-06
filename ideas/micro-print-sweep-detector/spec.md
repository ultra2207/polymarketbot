# A print that clears half the level is a sweep, not noise

claim:      Prints whose size is at least half of the displayed level size identify in-progress sweeps: they occur in >= 5 % of prints and the fill markout in the following 10 events is at least 1.5x worse than after small prints, so shrinking on them improves paired net by >= $0.001/share while the absolute-size threshold that failed before stays out of the rule.

mechanism:  The repo's live size-weighted gate (850 shares / 1.5 c) failed because a fixed share count means nothing across markets with 20-share and 1,000-share minimums; the same print is enormous in one market and trivial in another. Size *relative to the level it just consumed* is scale-free and says whether the trade ate the queue (informed aggression, likely to continue into the next level) or nibbled it (noise). The trailing price-only OFI mean cannot see this because it ignores print size and needs 60 s of history. The counterparty is the aggressor's next slice, which arrives before the next book update settles. This conditions on prints, not on our own fill (that is micro-post-fill-defence) and not on queue position.

data:       The on_trade hook (price, size, side) plus the last observed bid_size_at_level / ask_size_at_level from the context. No derived table, no clock.

module:     Keep last_level_size per side and last touch prices from the previous decide. In on_trade, compute consumed = size / max(1e-9, level_size_on_the_hit_side); if consumed >= 0.5 and the print is at or through our quote, set sweep_cooldown = 10. In decide, while sweep_cooldown > 0 (decrement per call), SHRINK both sides to the engine floor if the mid has not reversed, and PULL the side the print hit if the next book update moved the touch away from us. Otherwise QUOTE both at full scale. Ignore prints with level_size unknown (0).

metric:     Paired per-condition net against queue_aware_ofi, with the markout of fills inside sweep windows versus outside as the mechanism check, and the median consumed fraction reported so the 0.5 threshold is auditable.

kill:       Kill if sweep prints are under 5 % of prints, or if the in-window markout gap is below 0.3 c/share, or if the rule repeats the 850/1.5 c failure (paired net at or below baseline on both slices).

sources:    Liquidity-consumption and price impact: Cont, Kukanov & Stoikov, "The Price Impact of Order Book Events", https://arxiv.org/abs/1011.6402. The failed absolute-size gate (850 shares / 1.5 c, 101 fills excluded, no PnL fix): docs/predictive-mm-analysis.md §3. Scale-free toxicity measures in prediction markets: https://law.stanford.edu/press/adverse-selection-in-prediction-markets-evidence-from-kalshi (VPIN, which normalises volume by depth).
