# Flow on YES threatens the NO bid first

claim:      The two tokens of one condition are anti-correlated, so YES-side order-flow imbalance threatens the NO-side bid before NO's own OFI moves; using the sibling token's OFI (sign-flipped) as an additional threat signal raises the gate's markout improvement by >= 0.2 c/share at the same fill count.

mechanism:  YES and NO must sum to about $1, so a buy program on YES that lifts the YES offer is simultaneously a sell program on NO that hits the NO bid; the NO book lags because its market makers react to their own book, not to the sibling's. A maker who reads both books therefore sees the NO threat one or more events early, in the same way cross-venue leads are used defensively (Kalshi trade flow leads Polymarket by 1 s at 0.696 agreement in this repo's own measurement). The counterparty is the taker working the pair. This uses cross-token flow, not the sibling's price, and not the same-token OFI the baseline already consumes.

data:       DecisionContext for both assets of the condition (one module instance per condition, decide called per token), so the module can record each token's ctx.ofi and observe the sibling's last value. No derived table, no clock.

module:     Keep last_ofi[asset_id] updated on every decide call. When deciding token A, compute threat = ofi_A and sibling_threat = -last_ofi[B] (sign flip: YES flow up threatens the NO bid... and NO flow up threatens the YES bid). If |sibling_threat| > |threat| and |sibling_threat| > threshold, use it to choose the threatened side, then apply the queue rule (keep both at the front, shrink the threatened side at ratio >= 0.60). If the sibling has never been seen, fall back to the asset's own OFI only.

metric:     Paired per-condition net against queue_aware_ofi, with the lead statistic reported (fraction of threat episodes where the sibling signal fires at least one book update before the own-token signal) and markout_5m_bps as the mechanism check.

kill:       Kill if the sibling leads in fewer than 10 % of threat episodes, or if the added markout improvement is below 0.1 c/share, or if the paired net delta is not positive on both slices.

sources:    Cross-venue flow lead measured in this repo: docs/kalshi-hypotheses.md H2 (Kalshi->PM direction agreement 0.696 at 1 s, 0.942 for size >= p90); docs/kalshi-trade-lead.md. OFI as the driver of short-horizon price: Cont, Kukanov & Stoikov, https://arxiv.org/abs/1011.6402. Complement pricing (YES+NO = $1): src/polymarketbot/rewards/merger.py and docs/predictive-mm-analysis.md.
