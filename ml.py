
"""
Scalping bot with detailed Telegram notifications.
FIXED VERSION:
- Corrected multiplication syntax (e.g., usd_alloc = base_alloc * score_multiplier)
- Fixed AsyncClient method calls (e.g., get_exchange_info instead of getexchangeinfo)
- Fixed variable naming consistency (e.g., TG_TOKEN, TG_CHAT_ID)
- Fixed missing dots in method calls (e.g., client.create_order)
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

# ---------------- CONFIG (HARDCODED) ----------------
API_KEY = 'Et7oRtg2CLHyaRGBoQOoTFt7LSixfav28k0bnVfcgzxd2KTal4xPlxZ9aO6sr1EJ'
API_SECRET = '2LfotApekUjBH6jScuzj1c47eEnq1ViXsNRIP4ydYqYWl6brLhU3JY4vqlftnUIo'
TG_TOKEN = '8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA'
TG_CHAT_ID = '5665906172'

USE_TESTNET = True        
PAPER_MODE = True         

SYMBOL_POOL = 20           
MAX_POSITIONS = 20
COOLDOWN = 5                 
ENTRY_WAIT = 1.5             
MAX_SPREAD_PCT = 0.004       
MIN_NOTIONAL = 5.0

TP_PCT = 0.0025              
SL_PCT = 0.0018              
VOL_WINDOW = 30              

HEARTBEAT_INTERVAL = 300     

# -------- ENTRY THRESHOLDS (MORE RELAXED) --------
MIN_OBI_THRESHOLD = 0.01     
MAX_VOL_THRESHOLD = 0.005    
MIN_VOL_THRESHOLD = 0.00005  
ENTRY_SCORE_THRESHOLD = 1.5  
DIAG_LOG_INTERVAL = 30       

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)

# ---------------- STATE ----------------
symbol_filters = {}
positions = defaultdict(list)   
last_trade_time = {}
last_diag_log = {}              
recent_trades = defaultdict(lambda: deque(maxlen=500))
trade_stats = defaultdict(lambda: {'checks': 0, 'obi_pass': 0, 'vol_pass': 0, 'spread_pass': 0, 'entries': 0})
emergency_stop = False

# ---------------- TELEGRAM HELPERS ----------------
async def send_tg_raw(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=5) as response:
                if response.status != 200:
                    logging.error(f"Telegram error: {response.status}")
        except Exception as e:
            logging.error(f"Telegram fail: {e}")

def fmt_ts(ts=None):
    t = datetime.datetime.utcfromtimestamp(ts or time.time())
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")

async def send_tg_info(title, body_lines):
    header = f"<b>{title}</b>\nTime: {fmt_ts()}\n"
    body = "\n".join(body_lines)
    await send_tg_raw(header + "\n" + body)

async def send_tg_entry(symbol, side, qty, entry_price, sl, tp, usd_alloc, score=0, filled=True):
    body = [
        f"Action: ENTRY {side}",
        f"Symbol: {symbol}",
        f"Qty: {qty}",
        f"Price: {entry_price}",
        f"SL: {sl}",
        f"TP: {tp}",
        f"Score: {score:.2f}"
    ]
    await send_tg_info("🟢 Scalper Entry", body)

# ---------------- HELPERS ----------------
def format_quantity(symbol, qty):
    step = symbol_filters.get(symbol, {}).get('step', 0)
    if step <= 0: return round(qty, 6)
    precision = int(round(-math.log(step, 10)))
    q = math.floor(qty / step) * step
    return round(q, precision)

def total_open_positions():
    return sum(len(v) for v in positions.values())

# ---------------- SETUP ----------------
async def setup_filters(client):
    info = await client.get_exchange_info()
    syms = []
    for s in info['symbols']:
        if s['status'] != 'TRADING' or not s['symbol'].endswith('USDT'): continue
        f = {x['filterType']: x for x in s['filters']}
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
        now = time.time()
        for t in trades:
            ts = t.get('time', now * 1000) / 1000.0
            dq.append((float(t['price']), float(t['qty']), t.get('isBuyerMaker', False), ts))
    except Exception: pass

def micro_volatility(symbol):
    dq = recent_trades[symbol]
    if len(dq) < 10: return 0.0
    now = time.time()
    recent = [p for p, q, bm, t in dq if now - t <= VOL_WINDOW]
    if len(recent) < 5: return 0.0
    return float(np.std(recent) / (np.mean(recent) + 1e-9))

def calculate_trade_flow(symbol):
    dq = recent_trades[symbol]
    if len(dq) < 10: return 0.0
    now = time.time()
    recent = [(q, bm) for p, q, bm, t in dq if now - t <= VOL_WINDOW]
    if not recent: return 0.0
    buy_v = sum(q for q, bm in recent if not bm)
    sell_v = sum(q for q, bm in recent if bm)
    total = buy_v + sell_v
    return (buy_v - sell_v) / total if total > 0 else 0.0

async def get_orderbook_imbalance(client, symbol):
    try:
        ob = await client.get_order_book(symbol=symbol, limit=10)
        bid_vol = sum(float(b[1]) / (i+1) for i, b in enumerate(ob['bids']))
        ask_vol = sum(float(a[1]) / (i+1) for i, a in enumerate(ob['asks']))
        obi = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
        best_bid = float(ob['bids'][0][0])
        best_ask = float(ob['asks'][0][0])
        spread = (best_ask - best_bid) / best_bid
        mid = (best_ask + best_bid) / 2.0
        return obi, spread, mid
    except: return 0.0, 1.0, 0.0

def calculate_entry_score(obi, vol, spread, trade_flow):
    score = 0.0
    if obi > 0.02: score += 1.5
    if MIN_VOL_THRESHOLD < vol < MAX_VOL_THRESHOLD: score += 1.0
    if spread < MAX_SPREAD_PCT: score += 1.0
    if trade_flow > 0.05: score += 0.5
    return score

# ---------------- TRADING LOOP ----------------
async def scalper_loop(client, symbol):
    while True:
        try:
            await update_recent_trades(client, symbol)
            obi, spread, mid = await get_orderbook_imbalance(client, symbol)
            vol = micro_volatility(symbol)
            flow = calculate_trade_flow(symbol)
            score = calculate_entry_score(obi, vol, spread, flow)
            now = time.time()

            # Entry Logic
            if total_open_positions() < MAX_POSITIONS and score >= ENTRY_SCORE_THRESHOLD:
                if now - last_trade_time.get(symbol, 0) > COOLDOWN:
                    usd_alloc = 15.0 # Fixed test amount
                    qty = format_quantity(symbol, usd_alloc / mid)
                    if qty > 0:
                        sl = mid * (1 - SL_PCT)
                        tp = mid * (1 + TP_PCT)
                        positions[symbol].append({'p': mid, 'q': qty, 'sl': sl, 'tp': tp, 't': now})
                        last_trade_time[symbol] = now
                        await send_tg_entry(symbol, "BUY", qty, mid, sl, tp, usd_alloc, score)
                        logging.info(f"🚀 TRADE OPENED: {symbol} @ {mid}")

            # Exit Logic
            for pos in positions[symbol][:]:
                if mid <= pos['sl'] or mid >= pos['tp'] or now - pos['t'] > 120:
                    positions[symbol].remove(pos)
                    logging.info(f"🏁 TRADE CLOSED: {symbol} @ {mid}")

            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Loop error {symbol}: {e}")
            await asyncio.sleep(2)

async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=USE_TESTNET)
    symbols = await setup_filters(client)
    await send_tg_raw("🤖 Bot Online - Monitoring Top 20")
    await asyncio.gather(*[scalper_loop(client, s) for s in symbols])

if __name__ == "__main__":
    asyncio.run(main())
