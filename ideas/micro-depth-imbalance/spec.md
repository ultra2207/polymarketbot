# Resting depth imbalance is the stock version of OFI

claim:      The instantaneous imbalance between bid-side and ask-side size at the touch, |(bid_size - ask_size)| / (bid_size + ask_size) >= 0.5, identifies the threatened side at the very next book update with >= 60 % hit rate, versus ~55 % for the trailing 60 s OFI sign, and gating on it improves paired net by >= $0.001/share.

mechanism:  OFI is a *flow* average over a trailing window: it needs history and lags every repricing. The two level sizes in our context are a *stock*: they say which side of the book is thin right now, and a thin side is the one a sweep reaches first - the same depth logic that makes price impact inversely proportional to depth (Cont, Kukanov & Stoikov 2014). The two are not substitutes: a book can have zero net flow and still be lopsided because one side's resting size was never replenished. The counterparty is the taker who arrives into the thin side while our quote sits on it. No fill model and no queue position is used.

data:       DecisionContext only: bid_size_at_level, ask_size_at_level, mid, best_bid, best_ask, spread_cents. No derived table, no clock.

module:     In decide compute di = (bid_size_at_level - ask_size_at_level) / max(1e-9, bid_size_at_level + ask_size_at_level). If di >= 0.5, the ask is thin (buy-side pressure): SHRINK the ask to the engine floor and QUOTE the bid at full scale; if di <= -0.5, mirror. If |di| < 0.5, QUOTE both. Guard: if either level size is 0 (empty side), do not trade on the ratio - QUOTE both - because a genuinely empty side is a data state, not a signal.

metric:     Next-update hit rate of the predicted threatened side (does that side's best price worsen or does its level shrink?) and the paired per-condition net; the deciding number is the paired net delta per share against queue_aware_ofi.

kill:       Kill if the hit rate is below 55 %, or if the resulting markout gap between quoted and shrunk sides is below 0.3 c/share, or if the paired net delta is not positive on both slices.

sources:    Cont, Kukanov & Stoikov, "The Price Impact of Order Book Events" — imbalance, not trades, moves short-horizon price, with impact scaled by depth: https://arxiv.org/abs/1011.6402. Depth-ahead adverse selection: Moallemi & Yuan (2016), https://moallemi.com/ciamac/papers/queue-value-2016.pdf. Repo's price-only OFI result and its limits: docs/predictive-mm-analysis.md §1, §3.
