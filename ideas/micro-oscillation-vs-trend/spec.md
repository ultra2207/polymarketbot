# Bid-ask bounce is noise; same-sign runs are information

claim:      Updates whose mid changes alternate sign (flip fraction >= 0.7 over the last 30 changes) carry markout of +0.2 c/share or better, while low-flip updates (<= 0.3) carry -1.0 c/share or worse; conditioning quote size on that flip fraction improves paired net by >= $0.001/share versus the queue gate.

mechanism:  A top-of-book that oscillates between two prices with no direction is the classic bid-ask bounce: trades hit alternating sides and the mid returns, so a resting maker is paid the spread and nothing informative is happening. A top that steps in one direction is real information being repriced one tick at a time, and every quote on the wrong side of that path is stale. The distinction is in the *sign pattern* of mid changes, not their magnitude and not the signed mean the OFI gate uses; a choppy but symmetric book has OFI near zero yet is the worst state for a maker, while a one-way drift has a large OFI that the gate catches late. The counterparty is the taker who is repricing, not the one who is bouncing.

data:       DecisionContext only: each decide carries book.mid; the module keeps the last 30 distinct mid values and derives the sign of each change, then flip_frac = share of consecutive pairs with opposite signs. No derived table, no clock.

module:     In on_book (or at the top of decide) append mid when it differs from the last observed mid; keep a 30-entry deque. In decide: if fewer than 10 changes are buffered, QUOTE both. If flip_frac >= 0.7, QUOTE both at full scale (bounce regime, spread capture is the edge). If flip_frac <= 0.3, PULL the side the run points into (last change up -> pull the ask) and keep the other side at min size. Otherwise QUOTE both.

metric:     Markout ladder across flip_frac terciles (the mechanism check) and the paired per-condition net against queue_aware_ofi; deciding number is the paired net delta per share.

kill:       Kill if the tercile markout spread is below 0.3 c/share, or if the high-flip tercile is not the better one, or if the paired net delta is not positive on both slices.

sources:    The transitory/permanent decomposition of quote changes (bid-ask bounce vs informed moves), Roll (1984) and its modern treatment in Cont, Kukanov & Stoikov, "The Price Impact of Order Book Events", https://arxiv.org/abs/1011.6402. Latency-race evidence that a repricing book is where stale quotes die: https://www.aeaweb.org/conference/2021/preliminary/paper/Tt353Bff.
