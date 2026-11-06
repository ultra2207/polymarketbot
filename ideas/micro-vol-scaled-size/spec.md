# Quote size should scale with 1/variance, not with the pool

claim:      Realized path variance computed from the mid alone separates good and bad quoting regimes: conditions in the top volatility tercile carry markout -1.5 c/share or worse while the bottom tercile is at least 0, so scaling both sides' size by min(1, (sigma_ref/sigma)^2) with sigma_ref set at the universe median improves paired net by >= $0.002/share.

mechanism:  Avellaneda-Stoikov makes the optimal quote size and spread functions of sigma^2: the inventory term is q*gamma*sigma^2*(T-t), so the risk carried by a fixed share scales with variance while the reward for it does not (the score is variance-free). A maker who quotes the same size in a quiet and a violent condition is therefore mis-sizing by the ratio of their variances. This is not the jump or widening rule: no single event triggers it, and the same mid path can be smooth or violent with identical spreads. The counterparty in the violent tercile is the informed taker who is paid for exactly the size we over-supplied; in the quiet tercile we are under-supplied relative to what the pool pays.

data:       DecisionContext only: successive book.mid values. The module computes sigma_hat = mean(|dmid|) over the last 100 changes * 100 (cents, time-free), plus a long-run reference from setup-time defaults. No derived table, no clock.

module:     Keep a 100-entry deque of |mid change| in cents. In decide compute sigma = mean(deque) and ratio = clamp((0.5 / max(0.1, sigma)) ** 2, 0.01, 1.0), where 0.5 c is the reference (one half-tick of noise on a 1 c grid). Return SHRINK bid_scale = ask_scale = ratio on both sides; the engine floors anything positive at min size. When fewer than 30 changes are buffered, QUOTE both. Never PULL on this signal alone - the claim is about size, not presence.

metric:     Paired per-condition net against reactionary, with the volatility-tercile markout ladder and the distribution of the applied scale reported; the deciding number is the paired net delta per share.

kill:       Kill if the tercile markout ladder is flat (spread below 0.3 c/share), or if the applied scale is 1.0 or the floor for more than 90 % of updates (the signal does not vary), or if paired net is not positive on both slices.

sources:    Avellaneda & Stoikov (2008), inventory term scaling with sigma^2: https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf; implementation notes: https://hummingbot.org/blog/guide-to-the-avellaneda--stoikov-strategy. Volatility as a toxicity gauge in prediction markets: https://law.stanford.edu/press/adverse-selection-in-prediction-markets-evidence-from-kalshi.
