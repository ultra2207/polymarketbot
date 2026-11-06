# Substitute the sibling token instead of dropping a side

claim:      When protection says pull one side of token A, moving that size to the complementary token B on the same Q side (Q1 = YES bid + NO ask) keeps Q_min within 10 % of the fully two-sided baseline while still removing the threatened exposure, improving paired net by >= $0.001/share over a per-side pull.

mechanism:  The official score pairs YES bids with NO asks into Q1 and YES asks with NO bids into Q2, then takes max(min(Q1,Q2), max(Q1,Q2)/3) inside the mid band (Polymarket docs, eqs. 2-4). Our two tokens are one condition, so the harness gives one strategy instance both books; a maker who drops A's threatened ask loses Q2's contribution while the economically equivalent exposure on B is untouched and could carry the score. The pool underpays the maker who thinks per token instead of per Q side, and the counterparty is the taker on the side we abandoned. This is a pairing/eligibility mechanism: the signal is unchanged, only where the size lives changes.

data:       DecisionContext for both assets of the condition (available because setup binds one module instance per condition and the harness calls decide for each token), plus MarketView.tokens. No derived table.

module:     Keep a dict keyed by asset_id holding the last decision, mid and touch prices for both tokens of the condition. When the protective rule (queue ratio >= 0.75 or |OFI| above threshold) fires on A's ask, look up B's bid: if B's bid is QUOTE and its s satisfies the band, PULL A's ask and raise B's bid scale to min(1.0, A_scale + B_scale) so the Q2 contribution moves rather than disappears; if B is not quoting (or the mid is outside [0.10, 0.90] where strict two-sidedness is required), keep A's ask at the min-size floor instead of pulling. Mirror the logic for a threatened bid using B's ask.

metric:     Condition-level paired net versus queue_aware_ofi, plus a condition-level two-sidedness proxy (both Q sides non-zero at the epoch sample, computed from the per-token epoch snapshots); deciding number is the paired net delta with the two-sidedness change printed beside it. Caveat: the engine scores each token additively with no Q1/Q2 min, so the proxy and not rewards_usd is the falsifying number until rewards.compute_epoch_payout is patched.

kill:       Kill if the two-sidedness fraction improves by less than 20 percentage points, or if condition-level inventory variance rises by more than 10 %, or if paired net is not positive on both slices.

sources:    Polymarket liquidity rewards, Q_one/Q_two construction across a market and its complement: https://docs.polymarket.com/programs/liquidity-rewards. Merge/complement mechanics used by this repo: src/polymarketbot/rewards/merger.py. Two-sided depth as the scored property: https://docs.polymarket.us/incentives/liquidity.
