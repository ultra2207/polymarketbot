# Skew the size, not the price

claim:      Because the engine's price skew is capped at 0.5 c and moves both quotes (pushing one of them out of the reward band), size skewing the side that increases |inventory| cuts peak |inventory| by >= 30 % and inventory_mtm variance by >= 25 % with a reward loss under 5 %, giving a paired net at least as good as the baseline.

mechanism:  Inventory risk is q * sigma * sqrt(horizon); the standard response is a reservation-price shift (Avellaneda-Stoikov), which this engine already implements as a 0.5 c cap. But that cap is small relative to a 1 c tick, so at high inventory the price skew either does nothing or pushes the adding side's quote a full tick farther from mid, cutting its score by more than it cuts its fill probability. Shifting *size* instead leaves both quotes at their scored distance and simply stops adding to the position, which is the same risk reduction with no score penalty. The pool underpays price-skewed quotes at 1 c ticks; the counterparty is the informed taker who fills the adding side while we are already long.

data:       DecisionContext only: inventory_shares, max_inventory_shares, mid, spread_cents. No derived table, no clock.

module:     Compute inv_ratio = inventory_shares / max_inventory_shares. If inv_ratio >= 0, bid_scale = clamp(1 - inv_ratio, 0.01, 1.0) (engine floor = min size) and ask_scale = 1.0; if inv_ratio < 0, mirror (ask_scale = clamp(1 + inv_ratio, 0.01, 1.0)). Above |inv_ratio| >= 0.9, PULL the adding side outright. Always keep the reducing side at full scale so the position can decay without a market order.

metric:     Paired per-condition net (cash_pnl + inventory_mtm + rewards_usd) against reactionary, with peak |inventory| and inventory_mtm standard deviation reported as the mechanism check, and rewards_usd as the cost.

kill:       Kill if peak |inventory| falls by less than 15 %, or if rewards_usd falls by more than 10 %, or if paired net is not positive on both slices.

sources:    Avellaneda & Stoikov, "High-frequency trading in a limit order book" — reservation price r(s,q,t) = s - q*gamma*sigma^2*(T-t) and the size/spread response: https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf. Inventory-risk practice and its implementation as an offset: https://hummingbot.org/blog/guide-to-the-avellaneda--stoikov-strategy. Engine's capped price skew: src/polymarketbot/paper_trading/engine.py (max_skew_cents = 0.5).
