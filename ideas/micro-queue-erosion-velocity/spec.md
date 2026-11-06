# Queue erosion velocity, not queue level

claim:      The rate at which our own queue ratio falls, not its level, predicts the next adverse fill: fills arriving after a >= 0.2 drop in bid/ask queue ratio within 10 events carry markout 1.5 c/share worse than fills at a stable ratio, so shrinking on erosion improves paired net by >= $0.001/share over a level-only threshold.

mechanism:  The queue ratio is q_ahead/(q_ahead + q_behind). A ratio that falls quickly means the size in front of us is disappearing - either cancels (harmless, and the level is being abandoned) or fills (the level is being eaten, and we are next). The level alone cannot tell those apart; velocity can, because a level being eaten moves the whole ladder while cancels typically thin it in place. Moallemi and Yuan value queue position precisely because depth ahead raises the adverse-selection cost of the fill you do receive; a fast-shrinking queue is that cost arriving in real time. The counterparty is the taker already consuming the level ahead of us.

data:       DecisionContext only: bid_queue_ratio, ask_queue_ratio, bid_size_at_level, ask_size_at_level, mid per update; the module keeps a 10-event history per side. No derived table, no clock.

module:     Keep a 10-entry deque of queue ratio per side (append on each decide call, since decide is called per book update). Compute erosion_bid = ratio_bid[t] - max(ratio_bid over the window) and the mirror for the ask. If erosion <= -0.2 and the mid has not moved in the opposite direction, SHRINK that side to the engine floor; if erosion <= -0.2 and >= 2 events of the window had a mid change, PULL that side. Otherwise keep the current action. With fewer than 5 events of history, QUOTE both.

metric:     Paired per-condition net against queue_aware_ofi, with the eroded-versus-stable markout gap as the mechanism check; the deciding number is the paired net delta per share.

kill:       Kill if the eroded-versus-stable markout gap is below 0.4 c/share, or if fewer than 30 conditions show any >= 0.2 erosion episode, or if the paired net delta is negative on either slice.

sources:    Moallemi & Yuan, "A Model for Queue Position Valuation in a Limit Order Book" — queue position changes both fill probability and adverse-selection cost: https://moallemi.com/ciamac/papers/queue-value-2016.pdf. Cont, Kukanov & Stoikov on cancels as part of the imbalance that moves price: https://arxiv.org/abs/1011.6402. Repo queue construction (q_ahead/q_behind): scripts/build_l3_queue.py; measured split in docs/predictive-mm-analysis.md §3b.
