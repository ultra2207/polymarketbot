# The $1 floor makes small-share markets a zero-reward game

claim:      At least 15 % of the rewarded universe projects a daily payout below the venue's $1 minimum at min-size quoting, so on those conditions the reward term is exactly zero and quoting both sides is pure adverse selection; pulling them removes their fills with zero reward loss and improves paired net by >= $0.001/share.

mechanism:  The venue pays only if the day's total exceeds $1 (docs: "The minimum reward payout is $1"). A median market here has rate_per_day = $33 and min_size = 20 shares, so our score fraction must exceed 3 % of the pool for the day to pay anything at all; with full depth in the band from larger makers that fraction can sit below the floor for the whole day. In that state the entire reward channel is dead and the only remaining term is the fill markout, which the repo measures at -0.00628 $/share for the unconditional both-sides quote. The counterparty is the taker who knows the reward is not compensating us.

data:       Universe snapshot (daily_pool, min_size, max_spread) + clob_events for competing band depth. prepare.sql materialises data/derived/ideas/micro-payout-floor/proj_payout.parquet with (condition_id, asset_id, date, comp_score_median, our_min_size_score, proj_daily_usd_at_min, proj_daily_usd_at_full); proj_daily_usd = rate_per_day * S_ours/(S_ours + comp_score_median). Harness injects signals["proj_daily_usd_at_full"].

module:     At setup compute S_min = ((v - 0.75)/v)^2 * min_size and S_full = S_min * (quote_size / min_size). In decide read nothing but the injected projection (or, when absent, the setup-time prior rate_per_day/1440 * 0.03): if proj_daily_usd_at_full < 1.0, PULL both sides for the whole market (reason "below payout floor"); otherwise QUOTE both at full scale. Include a hysteresis band (resume only above $1.25 projected) so the module does not flap at the floor.

metric:     Fraction of rewarded conditions projected below the floor, and the paired net difference on that subset; the deciding number is the paired net delta per share on below-floor conditions with the rewards_usd delta printed next to it.

kill:       Kill if fewer than 5 % of rewarded conditions fall below the floor, or if below-floor conditions show reactionary markout >= +0.001 $/share (i.e. they are profitable to quote anyway), or if the projection's error versus realised epoch payouts exceeds 2x on the calibration slice.

sources:    Polymarket liquidity rewards ($1 minimum payout, Q_normal normalisation): https://docs.polymarket.com/programs/liquidity-rewards. Reward-market metadata fields ratePerDay / rewardsMinSize / rewardsMaxSpread: https://docs.polymarket.com/market-data/market-details. Payout floors as a known feature of liquidity programs: Gemini's prediction-market rewards page, https://developer.gemini.com/prediction-markets/liquidity-rewards-program.
