# Kalshi integration runbook

> **Status (2026-11-11).** The Kalshi subsystem this runbook documents is **gone from the
> tree**: `src/polymarketbot/kalshi_auth.py`, `polymarketbot.tools.kalshi_probe`,
> `polymarketbot.data.kalshi_*`, the collector's `kalshi`/`kalshi_books` streams and
> `paper_trading/kalshi_signal.py` were deleted with the collector (commit `999ec0a`) and
> `scripts/` (commit `76886b5`). Every command below raises `ModuleNotFoundError`, and the
> "State" column in §1 describes what ran on 2026-11-06, not what runs now. The file is kept
> as the record of the signing contract, the rate-limit arithmetic and the measured basis;
> nothing in the live or paper path uses Kalshi today.

What exists, how to run it, what is measured, and what is still unmeasured.
Numbers are from this repo on 2026-11-06; each states its sample.

## 1. What is wired

| Piece | Where | State |
|---|---|---|
| REST auth (Ed25519/RSA-PSS, auto-detect) | `src/polymarketbot/kalshi_auth.py` | working; **demo key only** |
| Auth probe | `polymarketbot.tools.kalshi_probe --host demo\|prod` | exit 0 accepted, 1 rejected |
| Market/trade poller | collector stream `kalshi` | running, 107 mapped tickers + global trade window |
| Full bid ladders, 5 s | collector stream `kalshi_books` | running since 2026-11-06 00:58 IST |
| 1-minute candles, 90 d | `data/raw/kalshi/candles/month=YYYY-MM/ticker=<t>.parquet` | 484 files, 2.09 M bars, 20 MB, 141 tickers (107 mapped + 34 v1 leftovers) |
| Cross-venue basis monitor | `src/polymarketbot/paper_trading/kalshi_signal.py` | implemented, off by default |
| Quote policy using it | `paper_trading/orchestrator.py` `signal="kalshi"` | implemented, threshold **not measured** |
| Mapping v2 (audited) | `data/derived/kalshi_mapping_v2.parquet` | 107 pairs; v1 was 64 % precise (n=25) and is superseded |

## 2. Key scope (important)

**Production Kalshi access is out of scope for this deployment.** Kalshi is a
US-regulated venue and a production API key requires US identity/KYC, which an
India-resident operator cannot obtain. So:

* every production path uses **public, unauthenticated REST** — which needs no
  key and is what the collector runs;
* the production **WebSocket is permanently unavailable** here (its handshake
  requires the signed key), and no production code depends on it;
* the key held in `.env` is a **demo** key, used only by `kalshi_probe.py` to
  prove the signing implementation and by nothing in the data path.

Do not plan around sub-second Kalshi books; the floor is the REST cadence
(2 s for the mapped set) plus p50 340 ms network latency.

Verified 2026-11-06:

* `demo-api.kalshi.co/trade-api/v2/portfolio/balance` → **200**;
* `api.elections.kalshi.com` (production REST) → **401** with this key;
* production WebSocket handshake (`wss://external-api-ws.kalshi.com/trade-api/ws/v2`) → **HTTP 401**;
* demo WebSocket (`wss://demo-api.kalshi.co/trade-api/ws/v2`, signed path `/trade-api/ws/v2`) → connects, acks subscriptions, delivers `orderbook_snapshot` and deltas.

Consequences as built:

* The book stream samples at **2 s** (`poll_seconds: 2`): 107 tickers = 2
  requests per cycle = 1 req/s ≈ 10 read tokens/s, well inside the Basic budget.
* The trade tape polls at **2 s** with cursor catch-up: H2 shows Polymarket
  follows a Kalshi print inside 1 s, so an 8 s poll cannot act on it. The
  catch-up keeps the tape contiguous at either cadence.
* The demo-verified WS path stays in the repo as documentation of the signing
  contract only; no stream or signal uses it.

Env contract (`.env`, gitignored; template in `.env.example`):

```
KALSHI_KEY_ID=<api key id>
KALSHI_PRIVATE_KEY_PATH=secrets/kalshi_private_key.pem
KALSHI_ENV=demo        # demo | prod
```

`secrets/`, `key.txt`, and `.env` are gitignored. The key id and PEM must never
appear in logs; `kalshi_auth.KalshiAuthError` reports only the file path and the
exception class.

## 3. Running

```bash
# collector (10 streams incl. kalshi + kalshi_books); 300 s first-run cap applies
.venv/Scripts/python.exe -m polymarketbot.data.collector --duration 0 --allow-long

# healthy? newest health partition, stream counters, per-stream rows
.venv/Scripts/python.exe -m polymarketbot.ops.monitor

# candles: resumable, one (ticker, month) per file; ~220-290 s per 30 tickers
.venv/Scripts/python.exe -m polymarketbot.data.kalshi_backfill --offset 0 --limit 30 --days 90

# paper engine: `scripts/run_paper_trading.py` was deleted in the 2026-11-10
# scripts cleanup. The entry point is now `polymarketbot.paper.main`, which has
# no `--markets`/`--orchestrator*` flags (the Kalshi cross-venue signal went
# with the Kalshi streams). The command below runs the paper engine alone.
.venv/Scripts/python.exe -m polymarketbot.paper.main --duration 600
```

Supervision: Windows task `\PolymarketCollector` runs
`.venv/Scripts/python.exe -m polymarketbot.ops.watchdog --once` **every 2 hours**; it starts the collector
if none is running and never kills a live one. The collector was last restarted
2026-11-06 00:58 IST to pick up the 10-stream config and the v2 mapping.

## 4. Data layout and semantics

`data/tape/kalshi_books/date=/hour=/part-*.parquet` — one row per (ticker,
snapshot): `yes_best_bid/ask`, `no_best_bid/ask`, `mid`, `spread`, top sizes,
1/2/5/10¢ depth bands, and `yes_bids`/`no_bids` JSON ladders (best first).

* Kalshi publishes **bids only**: `yes_ask = 1 − no_best_bid`. A one-sided book
  leaves the derived ask and the mid null — that is a fact about the book, not a
  zero.
* Unchanged books are suppressed and re-emitted every 60 s, so a missing tick
  means "unchanged", not "no data"; the heartbeat bounds how long a stale row
  can look fresh.
* `data/tape/kalshi_markets`/`kalshi_trades` remain the BBO/trade tape; the
  book tape is the level-level one.

`data/raw/kalshi/candles/month=YYYY-MM/ticker=<t>.parquet` — 1-minute bars,
`open/high/low/close` from `price`, separate `yes_bid_*`/`yes_ask_*` OHLC,
`volume`, `open_interest`, `end_period_ts` (Unix s). A quiet bar has only
`previous_close`; the OHLC stay null rather than zero.

## 5. Measured limits

| item | measured |
|---|---|
| `/markets/orderbooks` batch | 100 tickers max (101 → 400); repeated `tickers=` params, comma-joined silently becomes one ticker |
| orderbook latency | p50 340 ms, p95 478 ms, ~52 KB per 100-ticker batch |
| book stream cost | 62.6 MB/h network, ~1.3 GB stored over 14 days at 5 s cadence; ~156 MB/h and ~3.3 GB at the production 2 s cadence |
| `/markets/trades` | limit 1000, 305 KB/page, 226 prints/s global, 0.449/s in the mapped set; `cursor` + `min_ts` both honoured |
| candlesticks | 1-min bars ≥ 90 d back, 5,000-candle cap per request (3-day blocks) |
| rate limits | ~10 tokens/s at 1 req/s; the whole book stream is ≈6 tokens/s |

## 6. Failure modes seen

* **TLS resets** (`WinError 10054/64`) to Kalshi are routine on this host.
  Always go through `install_doh_fallback()` + the pinned `EdgePool` session;
  the auth probe and both streams do.
* **Mapping decay** — v1 was built against a 2026-10 universe; only 55 of its
  233 rows still existed on 2026-11-05. Rebuild v2 when events settle:
  `polymarketbot.data.kalshi_mapping --artifact v2 --refresh --refresh-markets`
  (~180 requests cold).
* **Empty books** — illiquid mapped tickers return empty ladders; the stream
  counts them (`empty_books`) and writes null mids, by design.
* **Prod WS 401** — expected with the demo key; see §2.

## 7. What is still unmeasured

The signal's threshold. `docs/kalshi-hypotheses.md` defines the tests; the first
results are:

* 1-minute candle cross-check (`docs/kalshi-candle-crosscheck.md`): pooled
  r = 0.652 over 39 markets, elections-dominated; crypto ladder r = 0.156 on
  2 markets, treasury 0.022; H7 (direction validation) failed as written.
* The 5 s basis harness (`scripts/report_kalshi_hf_basis.py`) first run: 1 hour,
  2,914 aligned rows, 101 pairs, mean per-pair slope of the forward PM return
  on the basis +0.207 @5 s (52 pairs, t 3.40), +0.311 @15 s, +0.441 @60 s;
  deciles not monotone and only 17 pairs at the 50-snapshot floor. The
  register requires ≥ 7 days and ≥ 50 usable pairs before any threshold is set.

Until then `--orchestrator-signal kalshi` must stay off in any run whose result
is used for a decision, and `kalshi_basis_threshold` has no measured value.
