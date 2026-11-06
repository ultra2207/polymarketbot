# Fills beget fills: defend after our own fill, not only ahead of it

claim:      The 10 book events after one of our own fills carry at least 1.5x the adverse markout of the same market's steady state, so a post-fill defensive window on the filled side (and on both sides if the mid has moved in the fill's direction) improves markout_5m_bps by >= 0.3 c/share at matched fill count.

mechanism:  A fill on our bid means a seller crossed into us; if that seller is the first slice of an informed order, the same information keeps arriving, and the quotes we leave behind are the next slices in line. This is continuation, not prediction: the module reacts to its own execution, which is the one event the venue gives us for free and which no price-only signal encodes. The measured markout here is -0.02014 $/share for back-of-queue fills on 281 observations, which is consistent with fills clustering inside informed episodes. The counterparty is the informed taker's second and third slice. Distinct from the OFI gate, which needs a trailing 60 s price window and never looks at our executions.

data:       Context plus the on_trade hook (price, size, side). Our own fill is identified by a print at or through our resting quote price on the matching side, which decide can reconstruct from the previous best_bid/best_ask it observed. No derived table.

module:     In on_trade, if side == "SELL" and price <= our_last_bid, or side == "BUY" and price >= our_last_ask, set a per-side counter filled_side = 10 and record the fill direction. In decide, decrement the counter once per call; while it is positive, SHRINK the same side to the min-size floor and, when the mid has moved >= 1 tick in the fill's direction, SHRINK the opposite side too. When the counter reaches zero, return QUOTE both.

metric:     Paired per-condition net with markout_5m_bps as the primary mechanism number; deciding statistic is the paired markout improvement on post-fill fills, with net required to be positive too.

kill:       Kill if post-fill markout is not worse than pre-fill by 0.3 c/share pooled across conditions, or if the module's fill count falls below 200 on both slices.

sources:    Roll's bid-ask bounce and transitory vs permanent impact framing; permanent-impact continuation after a trade: Cont, Kukanov & Stoikov (2014), https://arxiv.org/abs/1011.6402. Queue-position adverse selection: Moallemi & Yuan (2016), https://moallemi.com/ciamac/papers/queue-value-2016.pdf. Repo measurement of back-of-queue fill cost: docs/predictive-mm-analysis.md §3b.
