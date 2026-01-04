
# scalper_with_telegram.py
"""
Scalping bot with detailed Telegram notifications.

- Defaults: PAPER_MODE=True, USE_TESTNET=True
- Replace env vars BINANCE_API_KEY, BINANCE_API_SECRET, TG_TOKEN, TG_CHAT_ID to run live.
- Install: pip install python-binance aiohttp pandas numpy
- Run: python scalper_with_telegram.py
"""

import os
import asyncio
import math
import time
import logging
import datetime
from collections import defaultdict, deque

import aiohttp
import numpy as np
import pandas as pd
from binance import AsyncClient
from binance.enums import *

# ---------------- CONFIG ----------------
API_KEY = 'Et7oRtg2CLHyaRGBoQOoTFt7LSixfav28k0bnVfcgzxd2KTal4xPlxZ9aO6sr1EJ'
API_SECRET = '2LfotApekUjBH6jScuzj1c47eEnq1ViXsNRIP4ydYqYWl6brLhU3JY4vqlftnUIo'
TG_TOKEN = '8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA'
TG_CHAT_ID = '5665906172'
USE_TESTNET = True        # keep True for safety while testing
PAPER_MODE = True         # True = simulate orders, False = send real orders



SYMBOL_POOL = 30
MAX_POSITIONS = 30
COOLDOWN = 5                 # seconds per symbol after entry
ENTRY_WAIT = 1.5             # seconds to wait for limit fill before fallback
MAX_SPREAD_PCT = 0.002       # skip symbols with spread > 0.2%
MIN_NOTIONAL = 5.0

TP_PCT = 0.0025              # 0.25% take-profit
SL_PCT = 0.0018              # 0.18% stop-loss
VOL_WINDOW = 30              # seconds of recent trades for micro-volatility

HEARTBEAT_INTERVAL = 300     # seconds

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- STATE ----------------
symbol_filters = {}
positions = defaultdict(list)   # {symbol: [pos,...]} pos={'p','q','sl','tp','t'}
last_trade_time = {}
recent_trades = defaultdict(lambda: deque(maxlen=200))  # store recent trade sizes/prices
emergency_stop = False

# ---------------- TELEGRAM HELPERS ----------------
async def send_tg_raw(text):
    """Low-level Telegram send; no formatting by default."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        try:
            await s.get(url, params={'chat_id': TG_CHAT_ID, 'text': text})
        except Exception:
            # swallow errors to avoid crashing trading loop
            pass

def _fmt_ts(ts=None):
    t = datetime.datetime.utcfromtimestamp(ts or time.time())
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")

async def send_tg_info(title, body_lines):
    """
    Compose a compact, readable Telegram message.
    title: short string
    body_lines: list of strings
    """
    header = f"{title}\nTime: {_fmt_ts()}\n"
    body = "\n".join(body_lines)
    msg = header + "\n" + body
    await send_tg_raw(msg)

async def send_tg_entry(symbol, side, qty, entry_price, sl, tp, usd_alloc, filled=True):
    body = [
        f"Action: ENTRY {side}",
        f"Symbol: {symbol}",
        f"Qty: {qty}",
        f"Entry Price: {entry_price}",
        f"Stop Loss: {sl}",
        f"Take Profit: {tp}",
        f"Allocated USDT: {usd_alloc:.2f}",
        f"Filled: {filled}"
    ]
    await send_tg_info("Scalper Entry", body)

async def send_tg_exit(symbol, side, qty, exit_price, entry_price, reason, realized_usdt=None):
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0.0
    body = [
        f"Action: EXIT {side}",
        f"Symbol: {symbol}",
        f"Qty: {qty}",
        f"Exit Price: {exit_price}",
        f"Entry Price: {entry_price}",
        f"Pnl %: {pnl_pct:.4f}%",
        f"Reason: {reason}"
    ]
    if realized_usdt is not None:
        body.append(f"Realized USDT: {realized_usdt:.4f}")
    await send_tg_info("Scalper Exit", body)

async def send_tg_error(symbol, err):
    body = [
        f"Symbol: {symbol}",
        f"Error: {str(err)}"
    ]
    await send_tg_info("Scalper Error", body)

async def send_tg_heartbeat(total_symbols, total_positions, usdt_balance=None):
    body = [
        f"Symbols Monitored: {total_symbols}",
        f"Open Positions: {total_positions}"
    ]
    if usdt_balance is not None:
        body.append(f"USDT Free: {usdt_balance:.2f}")
    await send_tg_info("Scalper Heartbeat", body)

# ---------------- HELPERS ----------------
def format_quantity(symbol, qty):
    step = symbol_filters.get(symbol, {}).get('step', 0)
    if step <= 0:
        return round(qty, 6)
    precision = int(round(-math.log(step, 10)))
    q = math.floor(qty / step) * step
    return round(q, precision)

def total_open_positions():
    return sum(len(v) for v in positions.values())

async def safe_order(client, **kwargs):
    if PAPER_MODE:
        logging.info("PAPER ORDER: %s", kwargs)
        return {"simulated": True, "order": kwargs}
    return await client.create_order(**kwargs)

# ---------------- SETUP ----------------
async def setup_filters(client):
    info = await client.get_exchange_info()
    syms = []
    for s in info['symbols']:
        if s['status'] != 'TRADING':
            continue
        if not s['symbol'].endswith('USDT'):
            continue
        f = {x['filterType']: x for x in s['filters']}
        if 'LOT_SIZE' not in f or 'NOTIONAL' not in f:
            continue
        symbol_filters[s['symbol']] = {
            'step': float(f['LOT_SIZE']['stepSize']),
            'minNotional': float(f['NOTIONAL']['minNotional'])
        }
        syms.append(s['symbol'])
    return syms[:SYMBOL_POOL]

# ---------------- MICRO SIGNALS ----------------
async def update_recent_trades(client, symbol):
    try:
        trades = await client.get_recent_trades(symbol=symbol, limit=100)
        dq = recent_trades[symbol]
        for t in trades:
            dq.append((float(t['price']), float(t['qty']), t.get('isBuyerMaker', False), time.time()))
    except Exception:
        pass

def micro_volatility(symbol):
    dq = recent_trades[symbol]
    if len(dq) < 10:
        return 0.0
    prices = np.array([p for p,_,_,_ in dq])
    return float(np.std(prices) / (np.mean(prices) + 1e-9))

async def get_orderbook_imbalance(client, symbol, depth=5):
    try:
        ob = await client.get_order_book(symbol=symbol, limit=depth)
        bid_vol = sum(float(b[1]) for b in ob['bids'])
        ask_vol = sum(float(a[1]) for a in ob['asks'])
        spread = (float(ob['asks'][0][0]) - float(ob['bids'][0][0])) / float(ob['bids'][0][0])
        obi = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
        mid = (float(ob['asks'][0][0]) + float(ob['bids'][0][0])) / 2.0
        return obi, spread, mid
    except Exception:
        return 0.0, 1.0, 0.0

# ---------------- ENTRY / EXIT ----------------
async def try_entry(client, symbol, mid_price, usd_alloc):
    filt = symbol_filters.get(symbol, {})
    min_notional = filt.get('minNotional', MIN_NOTIONAL)
    qty = format_quantity(symbol, usd_alloc / mid_price)
    if qty <= 0 or usd_alloc < min_notional:
        return None

    limit_price = round(mid_price * 0.9995, 8)
    try:
        if PAPER_MODE:
            # simulate immediate fill
            filled = True
            return {'entry_price': limit_price, 'q': qty, 'filled': filled}
        else:
            order = await client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_LIMIT,
                                              timeInForce=TIME_IN_FORCE_GTC, quantity=qty, price=str(limit_price),
                                              newOrderRespType='FULL')
            start = time.time()
            while time.time() - start < ENTRY_WAIT:
                o = await client.get_order(symbol=symbol, orderId=order['orderId'])
                if o['status'] == 'FILLED':
                    return {'entry_price': float(o['price']), 'q': qty, 'filled': True}
                await asyncio.sleep(0.15)
            # cancel and fallback to market
            await client.cancel_order(symbol=symbol, orderId=order['orderId'])
            m = await client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
            return {'entry_price': mid_price, 'q': qty, 'filled': True}
    except Exception as e:
        logging.exception("Entry failed for %s: %s", symbol, e)
        return None

async def try_exit(client, symbol, pos):
    try:
        if PAPER_MODE:
            logging.info("PAPER SELL %s qty=%s", symbol, pos['q'])
            return True
        await client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=pos['q'])
        return True
    except Exception as e:
        logging.exception("Exit failed for %s: %s", symbol, e)
        return False

# ---------------- TRADING LOOP ----------------
async def scalper_loop(client, symbol):
    positions.setdefault(symbol, [])
    while True:
        if emergency_stop:
            await asyncio.sleep(1)
            continue
        try:
            # update micro data
            await update_recent_trades(client, symbol)
            obi, spread, mid = await get_orderbook_imbalance(client, symbol, depth=5)
            vol = micro_volatility(symbol)

            now = time.time()
            if now - last_trade_time.get(symbol, 0) < COOLDOWN:
                await asyncio.sleep(0.4)
                continue

            # skip if spread too wide
            if spread > MAX_SPREAD_PCT:
                await asyncio.sleep(0.4)
                continue

            # entry condition: buy-side imbalance and low micro-volatility
            if total_open_positions() < MAX_POSITIONS and obi > 0.08 and vol < 0.0008:
                # compute allocation: small fraction scaled by volatility
                if PAPER_MODE:
                    usdt_free = 1000.0
                else:
                    acc = await client.get_account()
                    usdt_free = float(next((b['free'] for b in acc['balances'] if b['asset']=='USDT'), 0.0))
                base_alloc = usdt_free * 0.005  # 0.5% per scalp
                vol_adj = max(0.25, 1.0 - (vol * 200))  # reduce size if vol high
                usd_alloc = base_alloc * vol_adj

                entry = await try_entry(client, symbol, mid, usd_alloc)
                if entry and entry.get('filled'):
                    entry_price = entry['entry_price']
                    q = entry['q']
                    sl = entry_price * (1 - SL_PCT)
                    tp = entry_price * (1 + TP_PCT)
                    positions[symbol].append({'p': entry_price, 'q': q, 'sl': sl, 'tp': tp, 't': now})
                    last_trade_time[symbol] = now
                    await send_tg_entry(symbol=symbol, side="BUY", qty=q, entry_price=entry_price, sl=sl, tp=tp, usd_alloc=usd_alloc, filled=True)

            # check exits for each position
            for pos in positions[symbol][:]:
                price = mid
                if price <= pos['sl'] or price >= pos['tp'] or (obi < -0.08):
                    ok = await try_exit(client, symbol, pos)
                    if ok:
                        positions[symbol].remove(pos)
                        realized = pos['q'] * price
                        reason = "TP" if price >= pos['tp'] else ("SL" if price <= pos['sl'] else "Signal")
                        await send_tg_exit(symbol=symbol, side="SELL", qty=pos['q'], exit_price=price, entry_price=pos['p'], reason=reason, realized_usdt=realized)

            await asyncio.sleep(0.9)  # tight loop for scalping
        except Exception as e:
            logging.exception("Loop error %s: %s", symbol, e)
            await send_tg_error(symbol, e)
            await asyncio.sleep(1)

# ---------------- HEARTBEAT ----------------
async def heartbeat_task(client, symbols, interval=HEARTBEAT_INTERVAL):
    while True:
        try:
            if PAPER_MODE:
                usdt_free = 1000.0
            else:
                acc = await client.get_account()
                usdt_free = float(next((b['free'] for b in acc['balances'] if b['asset']=='USDT'), 0.0))
            await send_tg_heartbeat(total_symbols=len(symbols), total_positions=total_open_positions(), usdt_balance=usdt_free)
        except Exception as e:
            await send_tg_error("heartbeat", e)
        await asyncio.sleep(interval)

# ---------------- MAIN ----------------
async def main():
    global PAPER_MODE
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=USE_TESTNET)
    symbols = await setup_filters(client)
    logging.info("Scalper loaded %d symbols (paper=%s)", len(symbols), PAPER_MODE)
    await send_tg_info("Scalper Started", [f"Symbols: {len(symbols)}", f"Paper mode: {PAPER_MODE}", f"Testnet: {USE_TESTNET}"])

    # warm recent trades
    for s in symbols:
        await update_recent_trades(client, s)

    tasks = [scalper_loop(client, s) for s in symbols]
    tasks.append(heartbeat_task(client, symbols, interval=HEARTBEAT_INTERVAL))
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Stopped by user")
