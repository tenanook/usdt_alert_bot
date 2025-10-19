#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USDT pairs daily-signal alert bot for Telegram (Binance Spot)
Strategies supported (1 script per process):
  - EMA50+EMAexit        (entry: close>EMA50, exit: close<EMA50)
  - EMA100+EMAexit       (entry: close>EMA100, exit: close<EMA50)
  - EMA50+MACDexit       (entry: close>EMA50, exit: MACD cross under)

Run example:
  STRATEGY="EMA50+EMAexit"  TELEGRAM_TOKEN="7038494046:AAF40EdChgpYkeNW8RZS0JeWh00z-_cneZU" CHAT_ID="-4677658866" python usdt_alert_bot.py
"""

import os, time, json, math, requests
from datetime import datetime, timezone
import numpy as np

BINANCE = "https://api.binance.com"
INTERVAL = "1d"    # TF 1D fixed
LIMIT_KLINES = 300 # warmup for EMA
TIMEOUT = 20

# ======= ENV ======= #
STRATEGY = os.getenv("STRATEGY", "EMA50+EMAexit")   # choose one per process
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # group/channel id (bot must be added)

assert TELEGRAM_TOKEN and CHAT_ID, "Please set TELEGRAM_TOKEN and CHAT_ID envs"

STATE_FILE = f"state_{STRATEGY.replace('+','_')}.json"

# ======= helpers ======= #
def ema(arr, length):
    if length <= 1: return np.array(arr, dtype=float)
    alpha = 2/(length+1)
    out = np.zeros_like(arr, dtype=float)
    out[:] = np.nan
    s = 0.0; w = 0.0
    # seed with SMA first 'length'
    if len(arr) >= length:
        seed = np.mean(arr[:length])
        out[length-1] = seed
        prev = seed
        for i in range(length, len(arr)):
            prev = alpha*arr[i] + (1-alpha)*prev
            out[i] = prev
    return out

def macd_cross_under(close, fast=12, slow=26, signal=9):
    macd = ema(close, fast) - ema(close, slow)
    sig  = ema(macd, signal)
    cu = (macd[1] >= sig[1]) and (macd[0] < sig[0])  # cross under on latest bar close
    return cu

def binance_symbols_usdt():
    ex = requests.get(f"{BINANCE}/api/v3/exchangeInfo", timeout=TIMEOUT).json()
    syms = []
    for s in ex["symbols"]:
        if s["status"] != "TRADING": continue
        if s["quoteAsset"] != "USDT": continue
        sym = s["symbol"]
        # exclude leveraged/fiat/indices tokens
        bad = ("UPUSDT","DOWNUSDT","BULLUSDT","BEARUSDT","3LUSDT","3SUSDT","5LUSDT","5SUSDT")
        if sym.endswith(bad): continue
        if any(x in sym for x in ["BUSD","TUSD","FDUSD","USDC"]): continue
        syms.append(sym)
    return sorted(syms)

def klines(symbol):
    r = requests.get(f"{BINANCE}/api/v3/klines",
        params={"symbol":symbol,"interval":INTERVAL,"limit":LIMIT_KLINES},
        timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    close = np.array([float(x[4]) for x in rows], dtype=float)
    tms   = [int(x[6]) for x in rows]  # close time ms
    return close, tms

def last_bar_closed_utc(ms):
    # Binance 1d bars close exactly at timestamp (ms) in UTC
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc)

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode":"HTML", "disable_web_page_preview": True}
    requests.post(url, json=payload, timeout=TIMEOUT)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(st):
    with open(STATE_FILE,"w",encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

# ======= strategy logic (bar-close signals) ======= #
def signal_for(symbol):
    close, tms = klines(symbol)
    if len(close) < 200: return None

    c = close
    c0, c1 = c[-1], c[-2]

    if STRATEGY == "EMA50+EMAexit":
        ema50 = ema(c, 50)
        e0, e1 = ema50[-1], ema50[-2]
        entry  = (c1 <= e1) and (c0 > e0)   # cross up on latest close
        exit_  = (c1 >= e1) and (c0 < e0)   # cross down on latest close

    elif STRATEGY == "EMA100+EMAexit":
        ema50  = ema(c, 50)
        ema100 = ema(c, 100)
        e50_0, e50_1 = ema50[-1], ema50[-2]
        e100_0, e100_1 = ema100[-1], ema100[-2]
        entry  = (c1 <= e100_1) and (c0 > e100_0)
        exit_  = (c1 >= e50_1)  and (c0 < e50_0)

    elif STRATEGY == "EMA50+MACDexit":
        ema50 = ema(c, 50)
        e0, e1 = ema50[-1], ema50[-2]
        entry  = (c1 <= e1) and (c0 > e0)
        exit_  = macd_cross_under(c)

    else:
        return None

    when = last_bar_closed_utc(tms[-1]).strftime("%Y-%m-%d %H:%M UTC")
    if entry:
        return {"side":"BUY", "when":when, "price":c0}
    if exit_:
        return {"side":"SELL","when":when, "price":c0}
    return None

def main():
    state = load_state()  # { symbol: { "last_when": "...", "last_side": "BUY/SELL" } }
    symbols = binance_symbols_usdt()

    alerts = []
    for sym in symbols:
        try:
            sig = signal_for(sym)
        except Exception:
            continue
        if not sig: continue
        prev = state.get(sym, {})
        key = f"{sig['when']}_{sig['side']}"
        if prev.get("key") == key:
            continue  # already alerted for this symbol/side on this bar
        state[sym] = {"key": key}
        alerts.append((sym, sig))

    # sort by symbol alphabetically
    alerts.sort(key=lambda x: x[0])

    if alerts:
        lines = [f"📣 <b>{STRATEGY}</b> | TF 1D | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"]
        for sym, sig in alerts:
            lines.append(f"{sym}: <b>{sig['side']}</b> @ {sig['price']:.4f}  ({sig['when']})")
        send_telegram("\n".join(lines))
        save_state(state)

if __name__ == "__main__":
    main()
