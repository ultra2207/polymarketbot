# Share is concave in size and linear in competition

claim:      Since our pool share is S/(S + C), once the qualifying band already holds competition C worth >= 4x our full-size score, extra size buys less than 20 % of that size in share; sizing to a fixed 25 % target share instead of quoting full size cuts fill_notional by >= 30 % on the most crowded tercile of conditions while losing <= 10 % of rewards_usd.

mechanism:  The pool pays Q_normal = Q_min / sum(Q_min across makers) each minute (Polymarket docs, eq. 5), so the marginal dollar of reward per marginal share falls as C/(S + C)^2 while the adverse-selection cost per share is constant. The venue's own competing score is not in our DecisionContext today, so the market-maker who ignores C over-sizes exactly where the reward is already crowded and under-sizes where the pool is uncontested. The pool underpays marginal size; the counterparty is whoever is willing to sweep a large resting quote. This is a competition-elasticity mechanism, not a fill-probability or flow mechanism.

data:       clob_events + the universe snapshot, resolved to per-(asset, second) band competition. prepare.sql materialises data/derived/ideas/micro-competition-dilution/comp_score.parquet with columns (asset_id, ts_second, comp_score, band_depth_shares, band_depth_usd), where comp_score is sum over levels with 0 <= (mid - price)*100 <= rewards_max_spread of ((v - s)/v)^2 * size, mirroring compute_competing_depth_score; the harness then injects ctx.signals["comp_score"].

module:     Store min_size, v and quote_size at setup (quote_size = max(min_size, capital/mid) as the harness computes it). In decide read ctx.signals["comp_score"] and the reconstructed per-side s; target score S* = tau * comp_score with tau = 0.25; per side return scale = clamp(S* / (f_side * quote_size * 0.5), 0.01, 1.0), i.e. split the target between the two sides by their score density f_side, and let the engine floor anything positive at min_size. If the signal is absent, return QUOTE both (the module must degrade to the baseline, not guess).

metric:     Paired per-condition net and fill_notional in the top competition tercile, with rewards_usd reported alongside; deciding number is the paired net difference against queue_aware_ofi with the fill reduction as the mechanism check.

kill:       Kill if comp_score varies by less than 5x across the sample (nothing to dilute), or if the reward loss exceeds twice the fill-notional saving on both slices, or if the top tercile contributes more than 25 % of the total (concentration gate).

sources:    Polymarket liquidity rewards, eq. 5 (Q_normal is a share of the maker sum): https://docs.polymarket.com/programs/liquidity-rewards. Size-cutoff/Target-Size walk and the "size beyond target earns nothing" rule: https://docs.polymarket.us/incentives/liquidity. Reward-farming design constraints: https://hummingbot.org/liquidity-mining.pdf.
