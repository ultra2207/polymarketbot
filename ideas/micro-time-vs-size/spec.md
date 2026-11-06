# The reward is linear in size, so size is a scaling decision and not a strategy

claim:      Score is linear in size and in time-in-band, so reward dollars per unit of fill notional are invariant to the (size, presence) mix within 10 %; therefore min-size quoting with full presence leaves reward per notional unchanged, and since the baseline's own net per share is negative (-0.00628 $/share on 777 marked fills), cutting size to the qualifying floor improves paired net by >= $0.001/share without any signal.

mechanism:  Q_epoch sums per-minute scores, each linear in size, so doubling size and halving presence is score-neutral; and Q_normal = Q_min/(Q_min + C) is concave in our own size, which makes *more samples at smaller size* weakly better than fewer at larger size for the same total. The cost side is not neutral: fill notional is what gets adversely selected, and the venue's min-size rule means the smallest qualifying order still scores. The pool therefore cannot make an unprofitable per-share book profitable by paying more per share, and the counterparty is paid by the same linearity. The test is an invariance check with a directional decision attached - if per-share net is scale-invariant, the only rational size is the floor.

data:       DecisionContext only: inventory, queue ratios, sizes; rewards_min_size at setup. No derived table, no clock.

module:     Return SHRINK bid_scale = ask_scale = 0.01 on every side, which the engine floors to rewards_min_size on both; keep both sides present at all times so the presence term is maximal. Only one override: PULL the side that would push |inventory| past 0.85 of the cap, and never PULL for any flow or price reason. No flow, queue, variance or price signal enters - by construction this isolates the size dimension.

metric:     Two numbers, both required: (a) reward_usd / fill_notional at min size versus at full size, which is the invariance check, and (b) paired per-condition net against reactionary, the deciding number. A violation of (a) beyond 10 % means the model is wrong, not that the strategy is good.

kill:       Kill if reward_usd / fill_notional changes by more than 25 % between min and full size (the linearity assumption is false and every reward-sizing idea here needs rebasing), or if paired net is not positive on both slices, or if fills fall below the 200-fill / 50-condition gate.

sources:    Polymarket liquidity rewards: Q_epoch sums per-sample linear-in-size scores and Q_normal is a concave share (eqs. 1, 5, 6): https://docs.polymarket.com/programs/liquidity-rewards. Minimum qualifying size as a real floor (median 20 shares, 1,256 markets in this universe): data/tape/universe/20261106T092900.json. Measured baseline net per share: docs/predictive-mm-analysis.md §3.
