# Reward per dollar and cent-risk per dollar are both 1/price-shaped, until the tails break

claim:      A share earns the same score and the same cent markout at any price, so reward per committed dollar and risk per committed dollar both scale as 1/price; if the measured per-share markout is price-flat the two cancel, but on this tape the extremes carry worse markout, so sizing proportional to 1 - |2*mid - 1| (full at 0.50, min size outside 0.20-0.80) improves net per committed dollar by >= 10 % versus constant-share sizing.

mechanism:  The score counts shares, the fill cost is cents per share, and the inventory cap is shares, while capital is dollars - so at mid 0.05 the same share count buys a twentieth of the score-relevant capital but carries the same $0.01-per-share adverse move, i.e. 20 % of price. Prediction-market prices near the boundary are where the repo's own tape work finds the losing side of the favourite-longshot spread (20-30 % priced resolves 12.2 % in sports) and where a single news item reprices whole cents; the boundary is also where the venue requires strict two-sidedness, so the reward is not richer to compensate. The pool therefore pays the same per share in the tails while the counterparty is more informed there. This is grid-position sizing, not band eligibility and not a variance estimate.

data:       DecisionContext only: book.mid, spread_cents, inventory_shares, max_inventory_shares; rewards_min_size at setup. No derived table.

module:     In decide compute tail = |2 * mid - 1| (0 at 0.50, 1 at the boundary) and scale = clamp(1 - tail, 0.01, 1.0), applied to both sides of the token; the engine floors any positive size at rewards_min_size, so 0.01 means "qualify at the minimum". Outside mid 0.20-0.80 additionally apply the inventory rule: never let the adding side exceed the floor. Do not PULL on price alone - the claim is about size per dollar, and pulling would confound it with the inventory rules tested in micro-inventory-skew-size.

metric:     Net per committed dollar per condition (net over mean quoted notional), paired against reactionary, with markout_5m_bps and rewards_usd reported separately by price bucket (0.00-0.20, 0.20-0.80, 0.80-1.00); the deciding number is the paired improvement in net per dollar.

kill:       Kill if per-share markout is flat across the three price buckets (spread below 0.3 c/share), or if the paired net-per-dollar improvement is under 5 % on both slices, or if fewer than 50 conditions have a token outside 0.20-0.80.

sources:    Polymarket liquidity rewards (score counted in shares, payout in dollars): https://docs.polymarket.com/programs/liquidity-rewards; the strict two-sided rule outside [0.10, 0.90]: same page, eq. 4. Favourite-longshot pricing in this repo's data: docs/strategy-thesis.md. Prediction-market adverse selection: https://law.stanford.edu/press/adverse-selection-in-prediction-markets-evidence-from-kalshi.
