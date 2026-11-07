# BasketParity (Former Domain 3) — Strategy Archival & Institutional Post-Mortem

**Strategy Identifier:** `BasketParity` (formerly Domain 3 / NegRisk Parity Arbitrage)  
**Status:** **PERMANENTLY GATED AT $0.00 LIVE CAPITAL ALLOCATION**  
**Date of Decommission:** November 7, 2026  
**Audited Artifacts:** `archive/basket_parity/audit_domain3_l2_tape.py`, `archive/basket_parity/m-event-basket-legs.md`, `archive/basket_parity/domain3_tape_audit_summary.json`

---

## 1. Executive Summary

`BasketParity` was conceptualized as a structural arbitrage engine designed to exploit cross-outcome pricing inefficiencies in Polymarket’s Conditional Token Framework (CTF) and Negative Risk (NegRisk) multi-outcome condition sets. The mathematical premise asserted that across $K$ mutually exclusive and exhaustive outcomes, the true probability sum satisfies $\sum_{i=1}^K p_i = 1.00$. When order book fragmentation causes $\sum_{i=1}^K \text{Ask}_i < 1.00$, an automated taker could simultaneously purchase all $K$ legs, invoke the CTF `mergePositions` or NegRisk adapter contract, and redeem the synthetic complete set for $\$1.00$ USDC par, capturing riskless basis.

However, comprehensive empirical replay against 250M+ rows of nanosecond-resolution Polygon/CLOB L2 tape conclusively demonstrated that **the strategy has a negative net expected value ($\mathbb{E}[\text{PnL}] < 0$) in live market conditions**. The strategy has been permanently decommissioned and allocated $0.00 live capital.

---

## 2. Strategy Thesis & Structural Design

### Theoretical Alpha Thesis
1. **Multi-Outcome NegRisk Arbitrage:**
   In an event with $K$ candidates (e.g., presidential election, sports champion):
   $$\alpha_{\text{gross}} = 1.00 - \sum_{i=1}^K \text{Ask}_i$$
   If $\alpha_{\text{gross}} > 0$, taking the best ask on all $K$ outcomes produces 1 complete set redeemable for $1.00 USDC.
2. **Binary Complementary Parity:**
   In binary conditions ($p_{\text{YES}} + p_{\text{NO}} = 1.00$):
   $$\alpha_{\text{gross}} = 1.00 - (\text{Ask}_{\text{YES}} + \text{Ask}_{\text{NO}})$$
   Simultaneously sweeping both sides locks in immediate riskless redemption margin.

---

## 3. Why BasketParity Failed in Practice: The 3 Fatal Flaws

### Flaw 1: Non-Linear Polymarket Taker Fee Drag ($C \times \text{rate} \times p(1-p)$)
Polymarket enforces a non-linear taker fee formula on CLOB executions:
$$\text{Fee}_{\text{taker}}(p) = C \cdot \text{rate} \cdot p(1 - p)$$
where $\text{rate} = 0.04$ (4.0% base) and $C$ is share size.
* For an event with $K$ legs, the taker pays fees on **every single leg**:
  $$\text{Fee}_{\text{total}} = \sum_{i=1}^K \text{Fee}(p_i) = \text{rate} \cdot \sum_{i=1}^K p_i (1 - p_i)$$
* Across the 4-hour empirical tape audit (226 identified parity anomalies), the cumulative taker fee averaged **1.43% (143 bps)** of trade notional.
* While the mean gross spread was 3.92%, the **median gross spread was only 1.00%**. 
* **Result:** After deducting non-linear taker fees, **44.7% of all observed gross anomalies became immediate net losses**. For the remaining 55.3%, the median net spread was compressed to a razor-thin **0.13% (13 bps)**, entirely inadequate to absorb execution slippage.

### Flaw 2: The Broken-Basket Execution Breakdown (Sequential Leg Risk)
Polymarket's Central Limit Order Book does **not** provide atomic multi-leg contingent order types (no All-Or-None multi-asset bundle matching). Each leg of a $K$-leg basket must be submitted as an independent REST/WebSocket order:
* **Empirical Fill Failure Rate:** In live markets, competing market makers and latency snipers react within 15–40 ms. The probability of successfully executing all $K=4$ legs at quoted top-of-book prices was empirically estimated at:
  $$P(\text{Bundle Complete}) = p_{\text{fill}}^K \approx (0.92)^4 \approx 71.6\%$$
  This yields a **28.4% broken-basket rate**.
* **Unwind Penalty:** When 1 to $K-1$ legs fill and the remaining legs reprice or cancel, the bot is left holding unhedged, toxic directional exposure. Unwinding these orphaned positions into the bid requires crossing the spread again, incurring an average loss of **1.80% plus additional taker fees**:
  $$\text{Loss}_{\text{unwind}} \approx \text{TradeNotional} \times (0.018 + \text{Fee}_{\text{taker}}) \approx 3.23\%$$
* **Net Mathematical Expectancy:**
  $$\mathbb{E}[\text{Net PnL}] = 0.716 \times (+0.13\%) + 0.284 \times (-3.23\%) = +0.093\% - 0.917\% = \mathbf{-0.824\%}$$
  Every broken basket wipes out the cumulative net edge of over 20 successful fills.

### Flaw 3: Phantom Serialization & Order Book Depth Illusions
An analysis of the 226 detected tape anomalies revealed:
1. **Phantom Serialization:** 90 of the 226 anomalies (39.8%) were artifacts of venue timestamp serialization (venue duration $\le 1\text{ ms}$). These were not real persistent arbitrage opportunities; they occurred because the matching engine processed cancellations or updates sequentially on the order bus. A taker reacting to these gets rejected or fills at worse tiers.
2. **Shallow Order Book Depth:** The median available dollar depth across valid opportunities was merely **$24.81** (median 29.66 shares). Deploying meaningful institutional capital ($500 - $1,000) walked the order book, creating Kyle's lambda market impact that instantaneously destroyed any remaining basis:
   $$\Delta P_{\text{impact}} = 0.004 \cdot \left(\frac{\text{OrderUSD}}{\text{DepthUSD}}\right)^{1.2}$$

---

## 4. Empirical Tape Audit Summary Table

Data compiled from `data/derived/domain3_tape_audit_summary.json` across 4 representative high-volume trading hours:

| Metric | Empirical Tape Value | Impact on Strategy |
| :--- | :--- | :--- |
| **Audited BBO Updates** | 1,455,288 rows | Full L2 depth reconstruction |
| **Gross Anomalies Found** | 226 instances | Infrequent occurrence rate (~56/hour) |
| **Phantom Serialization Artifacts** | 90 instances (39.8%) | Unexecutable race condition |
| **Genuine Persistent Anomalies** | 136 instances (60.2%) | True mispricings on book |
| **Gross Spread (Mean / Median)** | 3.92% / 1.00% | Appears profitable on naive surface |
| **Taker Fee Drag (Mean)** | 1.43% (143 bps) | Consumes >100% of median gross spread |
| **Net Spread (Mean / Median)** | 2.49% / 0.13% | Median margin is only 13 bps |
| **Profitable After Fees Fraction** | 55.3% | 44.7% of anomalies are mathematically dead |
| **Median Venue Duration** | 39.0 ms | Exceeds competitive taker race window |
| **Median Order Book Depth** | $24.81 (29.66 shares) | Severe liquidity bottleneck |
| **Sequential Leg Breakdown Rate** | 28.4% ($K=4$) | Unwind loss dominates expected value |

---

## 5. Formal Gating Decision

**Decision:** BasketParity is permanently gated at **$0.00 live bankroll allocation**.

**Key Lessons Transferred to Active Production Domains:**
1. **Taker Execution on Multi-Leg Contracts is Unviable:** Never deploy multi-leg basket strategies that pay taker fees across multiple condition tokens.
2. **Maker-Only Complete Set Generation (`CtfSplitter`):** Instead of buying all legs as a taker when $\sum \text{asks} < 1$, the active architecture uses `CtfSplitter` to mint complete sets on-chain at $\$1.00$ USDC par and sell individual legs passively as a **Maker**, paying 0% fees and collecting maker rebates.
3. **Capital Focus:** $100\%$ of trading capital is allocated to proven alpha engines:
   * **Domain 1:** `RewardHarvester` (continuous quadratic liquidity rewards & spread capture) & `LatencySniper` (CEX-DEX lead-lag jump sniper).
   * **Domain 2:** `SettlementHarvester` (UMA pre-settlement discount with OR-Tools risk allocation).
   * **Domain 4:** `CtfSplitter` (passive maker complete set arbitrage).
