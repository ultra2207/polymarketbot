# The inventory cap silently triples the reward penalty

claim:      When |inventory| reaches max_inventory_shares the engine zeroes the adding side, leaving a one-sided book that scores max(Q1,Q2)/3 at best and zero outside [0.10, 0.90]; a soft ramp that starts shrinking at 0.6 and pulls at 0.85 keeps the single-sided fraction under 1 % of events instead of the baseline's >= 5 %, without lowering net.

mechanism:  The engine implements the cap as a binary stop (`bid_size = 0 if inv >= max`), which is the correct risk control and the wrong reward control: the venue's Q_min is the minimum of the two book sides, so losing one side loses two-thirds of the score, and near the price boundaries it loses all of it. The maker therefore pays a discrete reward cliff for a marginal risk reduction that a size ramp delivers smoothly. The pool underpays the capped maker relative to the ramped one at identical average inventory. This is a control-shape mechanism, not a risk-premium or signal mechanism.

data:       DecisionContext only: inventory_shares, max_inventory_shares, mid, spread_cents. No derived table, no clock.

module:     inv_ratio = inventory_shares / max_inventory_shares. Define add_scale = 1.0 for |ratio| < 0.6; linearly down to 0.05 between 0.6 and 0.85; PULL the adding side at |ratio| >= 0.85. The reducing side is always 1.0. At mid outside [0.10, 0.90], tighten the ramp by 0.15 (start at 0.45) because the single-sided penalty there is total rather than one-third. Never return PULL on both sides from this ramp (that would trade the cliff for the eligibility screen).

metric:     Fraction of events with a single-sided book (one side size 0) and the paired per-condition net; the deciding number is the paired net delta per share with the single-sided fraction as the mechanism check.

kill:       Kill if the baseline's single-sided fraction is below 1 % of events (nothing to fix), or if peak |inventory| rises by more than 10 % under the ramp, or if paired net is not positive on both slices.

sources:    Polymarket liquidity rewards, Q_min = max(min(Q1,Q2), max(Q1,Q2)/3) and the strict two-sided rule outside [0.10, 0.90]: https://docs.polymarket.com/programs/liquidity-rewards. Engine cap implementation: src/polymarketbot/paper_trading/engine.py generate_quotes. Reward-forfeiture framing of a one-sided book: docs/predictive-mm-analysis.md §3b.
