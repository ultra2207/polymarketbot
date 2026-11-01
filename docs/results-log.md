# Results log

Every screen, including the failures. The point is to stop re-testing the same idea
and to keep the record of what was actually measured, on what window, at what precision.

Format: idea → window → sample → result → verdict → what would change it.

---

## #1 Longshot / favourite bias — **no edge after costs** (2026-11-01)

**Window:** markets created 2024-01-01 → 2026-03-31; entry at T−24 h → T−6 h before
resolution; hold to resolution; on-chain payout labels; fees via `schedule_at`.

**Sample:** 152,895 token-entries across 8 quarters (path
`data/processed/screen_calibration_parts/entry=T-24h..T-6h/`).

| Entry bucket | n | mean price | mean payout | net edge/share | t | 2026 fee |
|---|---|---|---|---|---|---|
| 0.00–0.05 | 6,463 | 0.019 | 0.013 | −0.0066 | −4.8 | 0.16 % |
| 0.05–0.10 | 3,881 | 0.075 | 0.051 | −0.0239 | −6.8 | 0.18 % |
| 0.10–0.20 | 8,509 | 0.152 | 0.128 | −0.0241 | −6.7 | 0.19 % |
| 0.20–0.35 | 17,831 | 0.278 | 0.256 | −0.0229 | −7.1 | 0.18 % |
| 0.35–0.50 | 31,778 | 0.445 | 0.445 | −0.0008 | −0.3 | 0.33 % |
| 0.50–0.65 | 29,098 | 0.558 | 0.569 | +0.0093 | +3.2 | 0.25 % |
| 0.65–0.80 | 20,552 | 0.723 | 0.754 | +0.0307 | +10.3 | 0.07 % |
| 0.80–0.90 | 10,654 | 0.849 | 0.884 | +0.0343 | +11.1 | 0.03 % |
| 0.90–0.95 | 5,915 | 0.926 | 0.955 | +0.0287 | +10.7 | 0.02 % |
| 0.95–0.98 | 6,207 | 0.967 | 0.983 | +0.0156 | +9.4 | 0.00 % |
| 0.98–1.00 | 17,007 | 0.994 | 0.997 | +0.0032 | +7.3 | 0.00 % |

**Verdict: dead after costs.** The bias is real and strongly significant (t ≈ 7–11),
about 2–3 pp per share on favourites and −2.3 pp on longshots. But:

- the entry is a **VWAP of executed trades**, i.e. roughly mid. A taker pays about half
  the quoted spread, and the median quoted spread is **2 ¢** — ~1.2 pp of notional on a
  0.85 favourite. That consumes most of the +3.4 pp.
- fees were ~0 for most of the sample. In 2026-Q1, the only era with fees, the effect had
  already shrunk to **+1.2 pp** on favourites against ~0.8 % in fees.
- standard errors here are `sd/√n` and ignore that the two tokens of a market are perfectly
  anti-correlated, so the true interval is wider than printed.

**What would change the verdict:** measuring the *quoted ask* at entry rather than the VWAP
(needs the book, i.e. 2026-04-13 onward), and a 2026-only window. If the ask-side version
still clears fees plus half-spread, the idea comes back — but at that point it is a
capacity-limited 1 pp trade, not a franchise.

### Correction recorded, because it nearly went the other way

The first version of this screen used entry = VWAP of the market's **first 48 h**, and
reported a +7.6 pp gap at 0.80–0.90 and −21.6 pp at 0.20–0.35 — twenty times larger. That was
an artefact: for the median 24-hour market, "first 48 h" is the *entire life*, and averaging
a price path that terminates at 0 or 1 pulls the average toward 0.5, so the realised 0/1 is
mechanically more extreme than the measured entry. The tell was that the gap was exactly
antisymmetric around 0.5 and cancelled to ~0 in the aggregate (crypto: −0.187 low bucket,
+0.203 high bucket, +0.015 overall).

**Rule that follows:** an entry price must be a point-in-time price with resolution still
ahead of it. Never average across a window that contains the terminal price.
