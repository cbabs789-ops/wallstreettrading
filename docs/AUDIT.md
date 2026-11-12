# Security, reliability and production audit (Nov 12, 2026)

Method: read every module for secrets handling, injection, unsafe parsing, failure modes, data integrity and
look-ahead; then wrote a test for each fix. Ranked by how much damage the problem could do.

## Fixed in this audit (each has a test)

| # | Problem | Risk | Fix |
|---|---|---|---|
| 1 | Risk engine sized orders at the quote price, but fills include slippage, so approved orders could be rejected by the broker | Orders silently not placed | Sizing uses the fill price; a test proves every risk-sized order fills |
| 2 | Schwab OAuth had no `state` check | A forged redirect could plant someone else's login | One-time random state, stored 0600, constant-time compare |
| 3 | Schwab token errors could surface raw responses | Token material in logs or tracebacks | Errors wrapped; bodies never echoed |
| 4 | Schwab account numbers would pass through unmasked | Account info exposed on screens or in logs | Masked to last 4 inside the connector |
| 5 | A bad tick (say a 60% "drop") could fill a paper order | Fake losses, wrong signals | Quotes 30%+ from the last close are refused |
| 6 | Inconsistent price bars (high below open) were accepted | Corrupt indicators and backtests | Dropped and logged |
| 7 | Two `daily` runs (cron plus manual) could overlap | Double processing | File lock |
| 8 | HTTP responses had no size cap | Memory exhaustion from a bad source | 40 MB cap |
| 9 | Data folder was world-readable | Other local users could read the account history | Folder set to 0700 |
| 10 | Entry-bar fills: a target already below the open filled at the target | Fake losses when grading older calls | Fill at the open; shared simulator for backtest and grading |
| 11 | News volume "spike" fired for everything on day one | Inflated catalyst scores | Needs 3 weeks of our own collection first |
| 12 | Headline classifier called a congressman's stock purchase an M&A deal | Misleading catalysts | Tighter rules; categories re-applied at read time |
| 13 | Irrelevant headlines (market-research spam, other companies' news) | Noise presented as company news | Relevance filter and pruning, 13 test cases |
| 14 | FRED silently dropped our requests | Each failure burned ~100 s of retries | Contact-style User-Agent for government sources |
| 15 | Log file grew without limit | Disk fills on a long-running server | Rotating log, 5 x 5 MB |

## Telegram and server (added Nov 12, each tested)

- Only the owner's chat id is obeyed; other chats are ignored and logged. Buttons carry a per-proposal random token.
- Approval is atomic (pending to approving in one update), so a double tap can't place two orders.
- Approval re-checks price drift (2%), expiry, earnings within 4 days and every risk limit before ordering.
- The bot token never appears in errors or logs (the request URL contains it, so errors are rewritten).
- Every Telegram message is HTML-escaped; only http/https links are allowed.
- The dashboard refuses to listen beyond the machine without a key, and returns 403 without it. It is plain HTTP:
  fine for paper data, not for a real account (use a VPN such as Tailscale before anything real is shown).
- Stats publishing refuses to upload any file containing the value of a secret setting.
- The systemd service runs as its own user, with a read-only system, access only to its data folder, and a 700 MB memory cap.
- End-to-end test of the real service against a fake Telegram API found and fixed one crash: a parameter-name clash
  killed the polling thread on its first call. The loop now survives any unexpected error.

## Verified safe

- **No real order path:** tests fail if the Schwab module gains an order method or any module besides `paper.py` writes orders.
- **SQL injection:** every query is parameterised. The only string-built SQL uses fixed table names from the code.
- **Dashboard:**
  - Binds to 127.0.0.1.
  - GET only; anything else returns 405.
  - Unknown paths return 404, including path traversal.
  - Strict CSP with a per-response nonce.
  - No external resources.
  - Values are inserted as text, never HTML.
  - Links only for http/https.
  - Embedded JSON is escaped so it can't close its script tag. All of this is tested.
- **XML parsing:** the standard library parser with expat 2.9.0, which blocks entity-expansion attacks. No external entities.
- **Secrets:**
  - None in code.
  - `.env`, the token file and `var/` are git-ignored.
  - No password is ever requested.
- **Dependencies:** none outside the Python standard library, so there is no supply-chain surface.
- **Look-ahead:**
  - Indicators are tested for no look-ahead.
  - Today's unfinished bar is dropped.
  - Grading is tested by poisoning all earlier bars.
  - Replay uses validation-window evidence only.

## Open issues, ranked

1. **Survivorship bias (high, research validity).** The universe is today's large caps, so every backtest and replay is flattered. Needs historical index membership data.
2. **Price source is unofficial (high, reliability).** If Yahoo changes the endpoint, prices stop. The code fails loudly (stale-data alert, orders refused after 4 days), but the fix is a real feed: Schwab market data once the developer app is approved.
3. **Schwab connector untested (medium).** It was written to the documented API but never run against a real account.
4. **Paper fills at the delayed quote, not the next open (medium, measurement).** The backtest buys at the next open; the paper autopilot buys at the latest quote when `daily` runs. Grading of predictions uses next-open rules, so it matches the backtest; the paper account shows a practical execution. Expect small differences.
5. **Earnings skip is a deviation from the tested rules (low).** The autopilot skips setups with earnings within 4 days. It's a deliberate risk rule the backtest didn't apply.
6. **Nasdaq earnings API is unofficial (medium).** Same failure mode as prices; dates stored as unconfirmed.
7. **No dashboard login (medium if exposed).** Safe on 127.0.0.1. Exposing it beyond the machine needs a VPN or an authenticating proxy; the server prints a warning if bound elsewhere.
8. **No database backup (medium for a long-running server).** Copy `var/wallstreet.db` nightly (SQLite's `.backup`, or a file copy while `daily` isn't running).
9. **"Today" uses the server's local time zone (low).** The daily-loss baseline resets at local midnight. Run the server on US Eastern time or set `TZ=America/New_York`.
10. **Alerts are local only (low).** They are stored, logged and shown; no push or email. Adding one means sending undelivered rows and marking them delivered.
11. **Sector map is hand-written (low).** Symbols outside the default 40 show as "Unknown" and skip the sector check.

## Bottlenecks

- First `refresh` takes about 5 minutes (SEC rate limit; about 400 requests). Later runs only fetch new filings.
- `intel` takes about 1 minute (about 80 news requests, 15 earnings calendar days, 7 FRED series).
- Walk-forward over 40 symbols x 10 years takes about 1 minute. `daily` re-runs it weekly.
- Replay of the test window takes about 10 seconds.
