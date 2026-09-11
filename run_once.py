"""
SSMT EUR/GBP liquidity-sweep divergence strategy -- single-shot runner for GitHub Actions.

Unlike live_trader.py (which loops forever in one process), this script is designed to be
invoked fresh every ~15 minutes by a GitHub Actions cron schedule. State (which quarter we're
in, per-case trigger/invalidation progress, the last candle timestamp processed) is persisted
to state.json and committed back to the repo by the workflow after each run, so the strategy
logic picks up exactly where it left off on the next invocation.

Rules match analysis.ipynb section 3 exactly (frozen, no re-tuning here):
  - Across every consecutive 6-hour quarter transition, a leader pair sweeping its own
    prior-quarter extreme while the lagger fails to follow means SSMT is active.
  - While SSMT stays active, every genuine touch of the lagger's 25%-retracement line (the
    line must fall inside that candle's own high/low range) is tracked -- the FIRST touch is
    always ignored, the trade only fires on the SECOND touch.
  - Stop: fixed 10 pips. Target: fixed 20 pips (2:1). Risk: fixed $100/trade.
  - At most one trade per quarter per instrument -- if both the high-side and low-side case
    would fire for the same lagger in the same quarter, only the earlier one is taken.

SAFETY: DRY_RUN defaults to true (reads the DRY_RUN env var). In dry-run, no real orders are
placed -- only Telegram alerts are sent describing what *would* have been taken.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ============================================================================
# Config (secrets/vars come from the GitHub Actions environment)
# ============================================================================
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

OANDA_API_TOKEN = os.environ["OANDA_API_TOKEN"]
OANDA_ACCOUNT_ID = os.environ["OANDA_ACCOUNT_ID"]
OANDA_BASE = "https://api-fxpractice.oanda.com"
OANDA_HEADERS = {"Authorization": f"Bearer {OANDA_API_TOKEN}", "Content-Type": "application/json"}

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

INSTRUMENTS = {"eur": "EUR_USD", "gbp": "GBP_USD"}
NY_TZ = "America/New_York"

PIP = 0.0001
RISK_PER_TRADE = 100.0
FIXED_SL_PIPS = 10.0
FIXED_TP_PIPS = 20.0
RETRACE_PCT = 0.25

Q_ORDER = {"Q1 (Asia)": 0, "Q2 (London)": 1, "Q3 (NY AM)": 2, "Q4 (NY PM)": 3}

CASES = [
    dict(key="eur_high", leader="eur", lagger="gbp", side="high", direction="short"),
    dict(key="gbp_high", leader="gbp", lagger="eur", side="high", direction="short"),
    dict(key="eur_low", leader="eur", lagger="gbp", side="low", direction="long"),
    dict(key="gbp_low", leader="gbp", lagger="eur", side="low", direction="long"),
]

BACKFILL_DAYS = 3  # first-ever run only: enough to cover current + previous full quarter
STATE_PATH = Path(__file__).parent / "state.json"
LOG_PATH = Path(__file__).parent / "run_log.csv"


def log(event: str, case: str = "", detail: str = ""):
    line = f"{pd.Timestamp.now(tz='UTC').isoformat()},{event},{case},{detail}"
    print(line)
    is_new = not LOG_PATH.exists()
    with open(LOG_PATH, "a") as f:
        if is_new:
            f.write("timestamp_utc,event,case,detail\n")
        f.write(line + "\n")


def notify_telegram(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        log("TELEGRAM_ERROR", detail=str(e))


def daily_quarter_of(ts: pd.Timestamp) -> str:
    h = ts.hour
    if 18 <= h < 24:
        return "Q1 (Asia)"
    if 0 <= h < 6:
        return "Q2 (London)"
    if 6 <= h < 12:
        return "Q3 (NY AM)"
    return "Q4 (NY PM)"


def trading_day_of(ts: pd.Timestamp) -> pd.Timestamp:
    return (ts - pd.Timedelta(hours=18)).normalize()


def quarter_seq_id_of(ts: pd.Timestamp) -> str:
    return f"{trading_day_of(ts).strftime('%Y-%m-%d')} {daily_quarter_of(ts)}"


def trading_week_key(ts: pd.Timestamp) -> str:
    """Forex trading week: Sunday 17:00 ET -> Friday 17:00 ET. Shifting back 17h lines the
    week boundary up with midnight Sunday, so a plain Sun-start weekly period groups correctly."""
    shifted = ts.tz_localize(None) - pd.Timedelta(hours=17)
    return str(shifted.to_period("W-SAT"))


def fetch_candles(instrument: str, start: pd.Timestamp) -> pd.DataFrame:
    start_utc = start.tz_convert("UTC") if start.tzinfo is not None else start.tz_localize("UTC")
    params = {"granularity": "M15", "price": "M", "from": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 500}
    r = requests.get(f"{OANDA_BASE}/v3/instruments/{instrument}/candles", headers=OANDA_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    candles = [c for c in r.json()["candles"] if c["complete"]]
    rows = [{"timestamp": c["time"], "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
             "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} for c in candles]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    if len(df):
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(NY_TZ)
    return df


def place_market_order(instrument: str, units: int, sl_price: float, tp_price: float, precision: int = 5):
    if DRY_RUN:
        log("DRY_RUN_ORDER", detail=f"{instrument} units={units} sl={sl_price:.5f} tp={tp_price:.5f} (not sent)")
        return {"dry_run": True}

    body = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{sl_price:.{precision}f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.{precision}f}"},
        }
    }
    r = requests.post(f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT_ID}/orders", headers=OANDA_HEADERS, json=body, timeout=30)
    resp = r.json()
    if r.status_code != 201:
        log("ORDER_ERROR", detail=str(resp))
    else:
        fill = resp.get("orderFillTransaction", {})
        trade_id = fill.get("tradeOpened", {}).get("tradeID")
        log("ORDER_FILLED", detail=f"{instrument} units={units} price={fill.get('price')} tradeID={trade_id}")
    return resp


def fetch_oanda_trade(trade_id: str) -> dict:
    r = requests.get(f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}", headers=OANDA_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["trade"]


# ---------------------------------------------------------------------------
# State load/save
# ---------------------------------------------------------------------------
def new_case_state():
    return {"triggered": False, "invalidated": False, "entered": False, "touch_count": 0,
            "leader_cum_high": -np.inf, "leader_cum_low": np.inf,
            "lagger_cum_high": -np.inf, "lagger_cum_low": np.inf,
            "threshold": None, "extreme": None}


class LiveState:
    def __init__(self):
        self.current_qid = None
        self.prior_range = None
        self.cur_range_acc = None
        self.case_state = {c["key"]: new_case_state() for c in CASES}
        self.last_ts = None
        self.open_trades = []  # list of dicts: pending dry-run trades not yet resolved to SL/TP
        self.weekly_pnl = 0.0
        self.weekly_key = None
        self.instrument_lock = {"eur": False, "gbp": False}  # 1-trade-per-quarter-per-instrument cap

    def start_new_window(self, prior_range):
        self.prior_range = prior_range
        self.case_state = {c["key"]: new_case_state() for c in CASES}
        self.instrument_lock = {"eur": False, "gbp": False}
        if prior_range is not None:
            for c in CASES:
                rng = prior_range[f"high_{c['lagger']}"] - prior_range[f"low_{c['lagger']}"]
                extreme = prior_range[f"high_{c['lagger']}"] if c["side"] == "high" else prior_range[f"low_{c['lagger']}"]
                threshold = extreme - RETRACE_PCT * rng if c["side"] == "high" else extreme + RETRACE_PCT * rng
                self.case_state[c["key"]]["threshold"] = threshold
                self.case_state[c["key"]]["extreme"] = extreme

    def to_dict(self):
        return {
            "current_qid": self.current_qid,
            "prior_range": self.prior_range,
            "cur_range_acc": self.cur_range_acc,
            "case_state": self.case_state,
            "last_ts": self.last_ts.isoformat() if self.last_ts is not None else None,
            "open_trades": self.open_trades,
            "weekly_pnl": self.weekly_pnl,
            "weekly_key": self.weekly_key,
            "instrument_lock": self.instrument_lock,
        }

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.current_qid = d["current_qid"]
        s.prior_range = desanitize(d["prior_range"])
        s.cur_range_acc = desanitize(d["cur_range_acc"])
        s.case_state = desanitize(d["case_state"])
        s.last_ts = pd.Timestamp(d["last_ts"]).tz_convert(NY_TZ) if d["last_ts"] else None
        s.open_trades = d.get("open_trades", [])
        s.weekly_pnl = d.get("weekly_pnl", 0.0)
        s.weekly_key = d.get("weekly_key")
        s.instrument_lock = d.get("instrument_lock", {"eur": False, "gbp": False})
        return s

    def credit_weekly_pnl(self, ts: pd.Timestamp, amount: float):
        key = trading_week_key(ts)
        if self.weekly_key != key:
            self.weekly_key = key
            self.weekly_pnl = 0.0
        self.weekly_pnl += amount


def sanitize(obj):
    """json can't represent +/-inf natively; swap for sentinel strings round-tripped by desanitize."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, float):
        if obj == float("inf"):
            return "inf"
        if obj == float("-inf"):
            return "-inf"
    return obj


def desanitize(obj):
    if isinstance(obj, dict):
        return {k: desanitize(v) for k, v in obj.items()}
    if obj == "inf":
        return float("inf")
    if obj == "-inf":
        return float("-inf")
    return obj


def load_state() -> LiveState:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return LiveState.from_dict(json.load(f))
    return LiveState()


def save_state(state: LiveState):
    with open(STATE_PATH, "w") as f:
        json.dump(sanitize(state.to_dict()), f, indent=2)


# ---------------------------------------------------------------------------
# Strategy state machine
# ---------------------------------------------------------------------------
def process_candle(state: LiveState, row: pd.Series, live: bool):
    ts = row["timestamp"]
    qid = quarter_seq_id_of(ts)
    q_label = daily_quarter_of(ts)

    if state.current_qid is None:
        state.current_qid = qid
        state.cur_range_acc = dict(high_eur=row["high_eur"], low_eur=row["low_eur"],
                                    high_gbp=row["high_gbp"], low_gbp=row["low_gbp"])
        state.start_new_window(None)
        return

    if qid != state.current_qid:
        prev_q_label = state.current_qid.split(" ", 1)[1]
        valid = (Q_ORDER[q_label] - Q_ORDER[prev_q_label]) % 4 == 1
        completed_range = state.cur_range_acc
        state.current_qid = qid
        state.cur_range_acc = dict(high_eur=row["high_eur"], low_eur=row["low_eur"],
                                    high_gbp=row["high_gbp"], low_gbp=row["low_gbp"])
        state.start_new_window(completed_range if valid else None)
        if live:
            log("NEW_WINDOW", detail=f"{qid} (prior valid: {valid})")
    else:
        state.cur_range_acc["high_eur"] = max(state.cur_range_acc["high_eur"], row["high_eur"])
        state.cur_range_acc["low_eur"] = min(state.cur_range_acc["low_eur"], row["low_eur"])
        state.cur_range_acc["high_gbp"] = max(state.cur_range_acc["high_gbp"], row["high_gbp"])
        state.cur_range_acc["low_gbp"] = min(state.cur_range_acc["low_gbp"], row["low_gbp"])

    if state.prior_range is None:
        return

    for c in CASES:
        st = state.case_state[c["key"]]
        if st["entered"] or st["invalidated"]:
            continue
        leader_h, leader_l = row[f"high_{c['leader']}"], row[f"low_{c['leader']}"]
        lagger_h, lagger_l = row[f"high_{c['lagger']}"], row[f"low_{c['lagger']}"]

        if not st["triggered"]:
            st["leader_cum_high"] = max(st["leader_cum_high"], leader_h)
            st["leader_cum_low"] = min(st["leader_cum_low"], leader_l)
            st["lagger_cum_high"] = max(st["lagger_cum_high"], lagger_h)
            st["lagger_cum_low"] = min(st["lagger_cum_low"], lagger_l)

            leader_extreme = state.prior_range[f"high_{c['leader']}"] if c["side"] == "high" else state.prior_range[f"low_{c['leader']}"]
            broke = st["leader_cum_high"] > leader_extreme if c["side"] == "high" else st["leader_cum_low"] < leader_extreme
            if broke:
                lagger_extreme = state.prior_range[f"high_{c['lagger']}"] if c["side"] == "high" else state.prior_range[f"low_{c['lagger']}"]
                lagger_ok = st["lagger_cum_high"] <= lagger_extreme if c["side"] == "high" else st["lagger_cum_low"] >= lagger_extreme
                if lagger_ok:
                    st["triggered"] = True
                    if live:
                        log("TRIGGER", c["key"], f"leader={c['leader'].upper()} broke its prior-quarter {c['side']}")
                else:
                    st["invalidated"] = True
                    continue

        if st["triggered"] and not st["invalidated"] and not st["entered"]:
            lagger_extreme = state.prior_range[f"high_{c['lagger']}"] if c["side"] == "high" else state.prior_range[f"low_{c['lagger']}"]
            broke_own_extreme = lagger_h > lagger_extreme if c["side"] == "high" else lagger_l < lagger_extreme
            if broke_own_extreme:
                st["invalidated"] = True
                if live:
                    log("INVALIDATED", c["key"])
                continue

            genuine_touch = lagger_l <= st["threshold"] <= lagger_h
            if genuine_touch:
                st["touch_count"] += 1
                if st["touch_count"] < 2:
                    if live:
                        log("FIRST_TOUCH_IGNORED", c["key"], f"threshold={st['threshold']:.5f} candle_time={row['timestamp']}")
                else:
                    if state.instrument_lock.get(c["lagger"], False):
                        st["entered"] = True  # instrument already taken this quarter by the sibling case
                        if live:
                            log("SKIPPED_DEDUPE", c["key"], f"{c['lagger'].upper()} already traded this quarter")
                    else:
                        state.instrument_lock[c["lagger"]] = True
                        fire_entry(state, c, st, row, live)
                        st["entered"] = True


def fire_entry(state: LiveState, case: dict, st: dict, row: pd.Series, live: bool):
    entry_price = st["threshold"]
    sl_pips = FIXED_SL_PIPS
    sl_price = entry_price + sl_pips * PIP if case["side"] == "high" else entry_price - sl_pips * PIP
    tp_price = entry_price - FIXED_TP_PIPS * PIP if case["direction"] == "short" else entry_price + FIXED_TP_PIPS * PIP

    units = int(round((RISK_PER_TRADE / (sl_pips * PIP))))
    if case["direction"] == "short":
        units = -units

    detail = (f"{case['lagger'].upper()} {case['direction']} entry={entry_price:.5f} sl={sl_price:.5f} "
              f"tp={tp_price:.5f} sl_pips={sl_pips:.2f} units={units} candle_time={row['timestamp']}")
    instrument = INSTRUMENTS[case["lagger"]]

    if not live:
        # Historical (backfill) signal -- track via candle simulation only, no real order.
        state.open_trades.append({
            "case_key": case["key"], "pair_key": case["lagger"], "instrument": instrument,
            "direction": case["direction"], "entry_price": entry_price, "sl_price": sl_price,
            "tp_price": tp_price, "sl_pips": sl_pips, "units": units,
            "entry_time": row["timestamp"].isoformat(), "oanda_trade_id": None,
        })
        log("BACKFILL_SIGNAL_SKIPPED", case["key"], detail)
        return

    log("ENTRY_SIGNAL", case["key"], detail)

    if DRY_RUN:
        state.open_trades.append({
            "case_key": case["key"], "pair_key": case["lagger"], "instrument": instrument,
            "direction": case["direction"], "entry_price": entry_price, "sl_price": sl_price,
            "tp_price": tp_price, "sl_pips": sl_pips, "units": units,
            "entry_time": row["timestamp"].isoformat(), "oanda_trade_id": None,
        })
        place_market_order(instrument, units, sl_price, tp_price)
        notify_telegram(
            f"<b>SSMT signal -- DRY RUN (no real order)</b>\n"
            f"Pair: {instrument}\nDirection: {case['direction'].upper()}\nEntry: {entry_price:.5f}\n"
            f"Stop: {sl_price:.5f} ({sl_pips:.1f} pips)\nTarget: {tp_price:.5f} ({FIXED_TP_PIPS:.0f} pips)\n"
            f"Units: {units}\nCandle: {row['timestamp']}\nWeekly PnL so far: ${state.weekly_pnl:,.2f}"
        )
        return

    # Live: place a real market order on the OANDA practice (demo money) account, with SL/TP
    # attached so OANDA's own engine executes the exit at the exact price/time -- not our polling.
    resp = place_market_order(instrument, units, sl_price, tp_price)
    fill = resp.get("orderFillTransaction")
    if not fill:
        notify_telegram(
            f"<b>SSMT signal -- ORDER FAILED</b>\n"
            f"Pair: {instrument}\nDirection: {case['direction'].upper()}\nIntended entry: {entry_price:.5f}\n"
            f"Reason: {resp}"
        )
        return

    real_entry_price = float(fill["price"])
    trade_id = fill.get("tradeOpened", {}).get("tradeID")
    state.open_trades.append({
        "case_key": case["key"], "pair_key": case["lagger"], "instrument": instrument,
        "direction": case["direction"], "entry_price": real_entry_price, "sl_price": sl_price,
        "tp_price": tp_price, "sl_pips": sl_pips, "units": units,
        "entry_time": row["timestamp"].isoformat(), "oanda_trade_id": trade_id,
    })
    notify_telegram(
        f"<b>SSMT signal -- LIVE ORDER PLACED (demo account)</b>\n"
        f"Pair: {instrument}\nDirection: {case['direction'].upper()}\n"
        f"Fill price: {real_entry_price:.5f} (intended: {entry_price:.5f})\n"
        f"Stop: {sl_price:.5f} ({sl_pips:.1f} pips)\nTarget: {tp_price:.5f} ({FIXED_TP_PIPS:.0f} pips)\n"
        f"Units: {units}\nOANDA trade ID: {trade_id}\nCandle: {row['timestamp']}\n"
        f"Weekly PnL so far: ${state.weekly_pnl:,.2f}"
    )


def resolve_open_trades(state: LiveState, row: pd.Series, live: bool):
    """Close out any pending trade that has hit SL/TP. Trades with a real OANDA trade ID
    (placed live, not DRY_RUN/backfill) are resolved by asking OANDA what actually happened --
    the broker's own fill price/time, not our estimate. Simulated trades (DRY_RUN or backfill)
    are resolved by racing SL vs TP through this candle's high/low, with SL assumed to hit first
    if a single candle's range spans both levels (conservative, matches the backtest convention)."""
    ts = row["timestamp"]
    still_open = []
    for tr in state.open_trades:
        if tr.get("oanda_trade_id"):
            trade = fetch_oanda_trade(tr["oanda_trade_id"])
            if trade["state"] != "CLOSED":
                still_open.append(tr)
                continue
            exit_price = float(trade["averageClosePrice"])
            pnl = float(trade["realizedPL"])
            outcome = "WIN" if pnl >= 0 else "LOSS"
            close_ts = pd.Timestamp(trade["closeTime"]).tz_convert(NY_TZ)
            state.credit_weekly_pnl(close_ts, pnl)
            log("TRADE_CLOSED", tr["case_key"], f"{outcome} pnl={pnl:.2f} exit={exit_price:.5f} oanda_close_time={close_ts}")
            notify_telegram(
                f"<b>SSMT trade closed -- {outcome} (OANDA-confirmed)</b>\n"
                f"Pair: {tr['instrument']}\nDirection: {tr['direction'].upper()}\n"
                f"Entry: {tr['entry_price']:.5f} -> Exit: {exit_price:.5f}\n"
                f"PnL: ${pnl:,.2f}\nClosed: {close_ts}\nWeekly PnL now: ${state.weekly_pnl:,.2f}"
            )
            continue

        h, l = row[f"high_{tr['pair_key']}"], row[f"low_{tr['pair_key']}"]
        sl_price, tp_price = tr["sl_price"], tr["tp_price"]

        if tr["direction"] == "long":
            sl_hit, tp_hit = l <= sl_price, h >= tp_price
        else:
            sl_hit, tp_hit = h >= sl_price, l <= tp_price

        if not (sl_hit or tp_hit):
            still_open.append(tr)
            continue

        exit_price = sl_price if sl_hit else tp_price
        outcome = "LOSS" if sl_hit else "WIN"
        pnl = (exit_price - tr["entry_price"]) * tr["units"]
        state.credit_weekly_pnl(ts, pnl)

        log("TRADE_CLOSED", tr["case_key"], f"{outcome} pnl={pnl:.2f} exit={exit_price:.5f} candle_time={ts}")
        if live:
            notify_telegram(
                f"<b>SSMT trade closed -- {outcome} (simulated)</b>\n"
                f"Pair: {tr['instrument']}\n"
                f"Direction: {tr['direction'].upper()}\n"
                f"Entry: {tr['entry_price']:.5f} -> Exit: {exit_price:.5f}\n"
                f"PnL: ${pnl:,.2f}\n"
                f"Weekly PnL now: ${state.weekly_pnl:,.2f}"
            )
    state.open_trades = still_open


def main():
    state = load_state()
    first_run = state.last_ts is None

    start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=BACKFILL_DAYS)) if first_run else (state.last_ts + pd.Timedelta(minutes=15))

    try:
        eur = fetch_candles(INSTRUMENTS["eur"], start)
        gbp = fetch_candles(INSTRUMENTS["gbp"], start)
    except requests.exceptions.RequestException as e:
        log("POLL_ERROR", detail=str(e))
        notify_telegram(f"SSMT bot: OANDA fetch failed -- {e}")
        sys.exit(1)

    merged = pd.merge(eur, gbp, on="timestamp", suffixes=("_eur", "_gbp")).sort_values("timestamp").reset_index(drop=True)

    if not len(merged):
        log("NO_NEW_CANDLES")
        save_state(state)
        return

    for _, row in merged.iterrows():
        resolve_open_trades(state, row, live=not first_run)
        process_candle(state, row, live=not first_run)
        state.last_ts = row["timestamp"]

    if first_run:
        log("BACKFILL_DONE", detail=f"caught up to {state.last_ts}")
    else:
        log("PROCESSED", detail=f"{len(merged)} new candle(s), caught up to {state.last_ts}")

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("UNEXPECTED_ERROR", detail=str(e))
        notify_telegram(f"SSMT bot: unexpected error -- {e}")
        raise
