# A fill that flattens us is worth more than its markout

claim:      The toxicity gate is inventory-blind: at the same signal strength, a fill that reduces |inventory| is worth at least 0.5 c/share more *net* (markout plus rebalance value) than a fill that increases it, so suppressing the pull when the threatened side is the reducing side improves paired net by >= $0.001/share over queue_aware_ofi at equal or lower risk.

mechanism:  Inventory has a shadow price: being long makes a sell fill worth markout plus the release of the inventory penalty (the q*gamma*sigma^2 term in Avellaneda-Stoikov), and being short makes a buy fill symmetric. The existing gate decides per side from flow alone and therefore defends the reducing side as hard as the adding side, which throws away the cheapest flattening opportunities exactly when inventory is largest. The counterparty on a reducing fill is a taker buying our inventory at a price that already includes a spread we were paid to hold. This is the interaction of the risk term with the signal, not a new signal and not a size rule.

data:       DecisionContext only: inventory_shares, max_inventory_shares, ofi, queue ratios, sizes. No derived table, no clock.

module:     Apply the baseline queue-aware rule (threatened side = sign of ofi; keep both at ratio < 0.60, shrink the threatened side above it). Then override in one direction only: if the threatened side is the one that would reduce |inventory| (threatened ask while inventory_shares > 0, or threatened bid while inventory_shares < 0) and |inventory| / max_inventory >= 0.25, return QUOTE at full scale on that side instead of shrinking. Never extend the override to the adding side, and never apply it when |inventory| is under 0.25 of the cap (there is nothing to rebalance).

metric:     Paired per-condition net against queue_aware_ofi, with the markout of reducing fills versus adding fills at matched signal strength as the mechanism check, and peak |inventory| as the risk check.

kill:       Kill if the reducing-versus-adding net gap is below 0.2 c/share, or if peak |inventory| rises by more than 15 %, or if paired net is not positive on both slices.

sources:    Avellaneda & Stoikov (2008), inventory shadow price r(s,q,t) = s - q*gamma*sigma^2*(T-t): https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf. The existing gate's inventory blindness: src/polymarketbot/paper_trading/orchestrator.py (decide takes ofi and queue, not inventory). Directional-asymmetry analysis in prediction markets: https://arxiv.org/html/2604.10005v2.
