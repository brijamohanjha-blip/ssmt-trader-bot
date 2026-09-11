# SSMT EUR/GBP Liquidity-Sweep Divergence Strategy

A 24/7 trading bot that watches EUR/USD and GBP/USD for a quarterly-theory liquidity-sweep
divergence setup, takes the trade on OANDA's practice (demo money) account, and alerts on
Telegram for every signal and every close. Rules are frozen exactly to match the backtest in
`analysis.ipynb` at the root of this project.

## Sessions ("quarters")

Each trading day is split into four 6-hour blocks (NY time):

| Quarter | Session | NY time |
|---|---|---|
| Q1 | Asia | 18:00 – 00:00 |
| Q2 | London | 00:00 – 06:00 |
| Q3 | NY AM | 06:00 – 12:00 |
| Q4 | NY PM | 12:00 – 18:00 |

The strategy is checked at every consecutive quarter-to-next-quarter transition (Asia→London,
London→NYAM, NYAM→NYPM, NYPM→next day's Asia).

## The setup: SSMT divergence

For each transition, checked on **both** EUR-leads/GBP-lags and GBP-leads/EUR-lags, on **both**
the high side and low side (4 cases total, evaluated independently every candle):

1. **Is SSMT active?** The **leader** pair must **take** (wick beyond) its own prior-quarter
   extreme, while the **lagger** pair has **not** taken its own prior-quarter extreme on the same
   side up to that point — that divergence between the two correlated pairs is the signal.
   Invalidated the instant the lagger *does* take its own prior-quarter extreme.
2. **Genuine touch of the 25% retracement line.** The line is the lagger's own prior-quarter
   extreme, retraced 25% back into its prior-quarter range. A touch only counts if the line falls
   inside that candle's own `[low, high]` range — not merely a level the lagger already happens to
   be past without actually trading through it that candle.
3. **Take the SECOND touch, not the first.** Every genuine touch is tracked while SSMT stays
   active; the first is always ignored, the trade only fires on the second. If a quarter's setup
   never produces a second touch before being invalidated (or the quarter ends), there is no trade
   for that case that quarter — no fallback to the first touch.
4. The trade is taken on the **lagger** — short on the high-side case, long on the low-side case.

## Risk model

- **Stop:** fixed 10 pips (not floored or distance-based).
- **Target:** fixed 20 pips (2:1 reward:risk, 33.3% breakeven win rate).
- **Risk:** fixed $100/trade — a win pays a flat $200, a loss costs exactly -$100.
- **Position cap:** at most **one trade per quarter per instrument**. The high-side and low-side
  case can share the same lagger instrument in the same quarter (e.g. EUR leading on both sides
  against GBP) — if both would fire a second-touch entry, only the earlier one is taken.

## Backtest results (2005–2026, `analysis.ipynb`)

- 9,520 trades, 37.3% win rate, total PnL **+$112,400** on $100 fixed risk/trade ($10,000 start)
- Max drawdown: -$4,000
- **22/22 years net profitable** (smallest: +$500 in 2012; largest: +$13,200 in 2008)
- No spread/slippage modeled — live results will run below this due to real execution costs.

## How the live bot works (`run_once.py`)

Unlike a long-running process, this script runs fresh on every invocation — state (which quarter
each case is in, touch counts, per-quarter instrument lock, open trades, weekly PnL) is persisted
to `state.json` and committed back to the repo after each run.

- **Schedule:** triggered every 5 minutes by an external cron-job.org job calling GitHub's
  `workflow_dispatch` API (GitHub's own `schedule:` trigger is unreliable under load — it can lag
  hours, so it's not relied on alone).
- **Detection:** only acts on *completed* 15-minute OANDA candles, matching the notebook's
  candle-based genuine-touch definition exactly. This means detection lag is bounded by up to
  ~15 minutes (waiting for the candle to close) plus up to ~5 minutes (poll interval) — worst case
  ~20 minutes between the actual touch and the order firing. In calm markets the resulting price
  drift is usually small; in fast markets it can be more.
- **Execution:** once a second touch is confirmed, places a real **market order** (with SL/TP
  attached) on the OANDA practice account — still zero real financial risk (demo money), but real
  broker-side execution. This is why SL/TP fire at the exact correct tick and price: OANDA's own
  engine manages them once the order is live, independent of this bot's poll frequency. Entry
  price can differ slightly from the strategy's theoretical threshold ("intended" in the alert)
  due to the detection lag above — this is normal market-order slippage, not a bug.
- **Trade close confirmation:** for live orders, the bot queries OANDA's own trade history
  (`realizedPL`, `averageClosePrice`, `closeTime`) rather than re-simulating from candles, so the
  close alert reflects what OANDA actually recorded.
- **Weekly PnL:** realized PnL accumulates for the current forex trading week (Sunday 17:00 ET →
  Friday 17:00 ET), resetting at each week boundary, and is reported in every alert.
- **Backfill:** on a fresh state (first run, or after a logic change resets `state.json`), the bot
  catches up on the last 3 days of candles. Historical signals from that catch-up are simulated
  (candle-based SL/TP race, conservative same-candle tie-break: SL assumed first) for weekly PnL
  accuracy, but no real order is placed and no Telegram alert is sent for them — only forward-
  looking live signals trade and alert.

## Files

| File | Purpose |
|---|---|
| `run_once.py` | Strategy logic + OANDA execution + Telegram alerts, run once per invocation |
| `state.json` | Persisted strategy state between runs (committed by the workflow) |
| `run_log.csv` | Append-only event log (triggers, touches, entries, closes, errors) |
| `.github/workflows/trader.yml` | GitHub Actions job definition |
| `requirements.txt` | Python dependencies |

## Secrets (GitHub repo secrets, not in code)

- `OANDA_API_TOKEN`, `OANDA_ACCOUNT_ID` — OANDA practice account
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — alert delivery
