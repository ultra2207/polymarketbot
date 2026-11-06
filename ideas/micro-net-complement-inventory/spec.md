# YES plus NO is one dollar, so inventory is a sum, not two positions

claim:      Because a filled YES bid plus a filled NO bid at a combined price below $1 is a riskless $1 claim, the true inventory is the pair, not each token; sizing into the cheap complement when mid_YES + mid_NO <= 0.99 completes >= 10 % of pairs and adds >= $0.005/share of carry with no additional inventory risk, improving paired net per condition.

mechanism:  The two tokens of one condition are claims on the same $1: buying both for 0.98 locks 0.02, and the repo already merges them (src/polymarketbot/rewards/merger.py). The engine, however, tracks positions per token and halts each side at max_inventory_shares independently, so a maker who is long YES is treated as having no NO exposure and is stopped from completing the pair exactly when completing it would remove all risk. The pool pays for both quotes meanwhile. The counterparty is the taker who sells each leg separately and does not net them. This is complement arithmetic, distinct from any per-token inventory rule.

data:       DecisionContext for both tokens (one module instance per condition) plus MarketView.tokens; the module reads mid_YES and mid_NO from the two books it already receives. No derived table.

module:     Keep the last mid per asset_id. When deciding token A, compute s = mid_A + mid_B (using the sibling's last mid; if the sibling is unseen, QUOTE both). If s <= 0.99, treat A's bid and B's bid as a paired buy: bid_scale = 1.0 on both, and suppress inventory-based shrinking on the bid side while s stays cheap. If s >= 1.01, mirror on the ask side (selling both legs for more than $1 is the riskless side). Otherwise, apply a per-token inventory cap of 0.8 * max_inventory as usual and shrink the adding side to the floor beyond 0.6.

metric:     Pair-completion rate (fraction of conditions where both legs fill within the slice) and the paired per-condition net; the deciding number is the net delta per share against reactionary, with inventory_mtm variance reported as the risk check.

kill:       Kill if pair completion is below 10 % of conditions, or if the realised carry per completed pair is below $0.002/share, or if condition-level inventory variance rises by more than 10 % on both slices.

sources:    Complement scoring Q1 = YES bid + NO ask in the venue's own rule set: https://docs.polymarket.com/programs/liquidity-rewards. Merge/complement mechanics as implemented here: src/polymarketbot/rewards/merger.py. Inventory-risk framing: Avellaneda & Stoikov, https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf.
