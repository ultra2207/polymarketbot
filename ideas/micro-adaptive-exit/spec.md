# Withdraw from a market that has stopped paying for itself

claim:      A market whose own realised P&L per fill is negative after at least
            8 fills is not paying for the reward it earns; withdrawing both sides
            from it improves paired net by >= $0.001/share, while keeping every
            market that is still positive.

mechanism:  The reward is a payment for resting, and adverse selection is the
            price. The two are market-specific and persistent within a day: a
            market whose takers are informed keeps producing negative realised
            P&L on the same book shape, while a quiet one pays the reward and
            never fills. The book cannot tell them apart in advance - the only
            evidence is our own fills in that market - so the rule is adaptive
            and per market, evaluated on realised P&L, not on a price signal.

data:       DecisionContext only: realized_pnl, fills_count, rate_per_day. No
            derived table.

module:     Track realized_pnl and fills_count from the context. After 8 fills,
            if realized_pnl < 0 the market is withdrawn (PULL both sides); it is
            re-admitted after 60 decisions if the withdrawal itself preserved the
            reward (that check happens in the next version, not this one).

metric:     Paired per-condition net against reactionary on the withdrawn subset
            and on the kept subset, reported separately.

kill:       Kill if fewer than 20 conditions are withdrawn, or if the kept subset
            loses more than the withdrawn subset gains.
