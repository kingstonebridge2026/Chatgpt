
# scalper_with_telegram.py
"""
Scalping bot with detailed Telegram notifications.

FIXED VERSION:
- Relaxed entry conditions that were too strict
- Added diagnostic logging to see why trades aren't triggering
- Fixed volatility calculation with proper time windowing
- Added entry signal scoring system
- Better spread handling

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



SYMBOL_POOL = 20           # Updated to top 20 crypto symbols
MAX_POSITIONS = 20
COOLDOWN = 5                 # seconds per symbol after entry
ENTRY_WAIT = 1.5             # seconds to wait for limit fill before fallback
MAX_SPREAD_PCT = 0.003       # RELAXED: skip symbols with spread > 0.3% (was 0.2%)
MIN_NOTIONAL = 5.0

TP_PCT = 0.0025              # 0.25% take-profit
SL_PCT = 0.0018              # 0.18% stop-loss
VOL_WINDOW = 30              # seconds of recent trades for micro-volatility

HEARTBEAT_INTERVAL = 300     # seconds

# -------- ENTRY THRESHOLDS (RELAXED) --------
MIN_OBI_THRESHOLD = 0.03     # RELAXED from 0.08: Order book imbalance threshold (3% instead of 8%)
MAX_VOL_THRESHOLD = 0.003    # RELAXED from 0.0008: Max volatility (0.3% instead of 0.08%)
MIN_VOL_THRESHOLD = 0.0001   # NEW: Minimum volatility (need some movement to scalp)

# Scoring system for entries (NEW)
ENTRY_SCORE_THRESHOLD = 2.0  # Minimum score needed to enter

# Diagnostic logging interval
DIAG_LOG_INTERVAL = 30       # Log diagnostics every N seconds per symbol

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- STATE ----------------
symbol_filters = {}
positions = defaultdict(list)   # {symbol: [pos,...]} pos={'p','q','sl','tp','t'}
last_trade_time = {}
last_diag_log = {}              # NEW: Track last diagnostic log per symbol
recent_trades = defaultdict(lambda: deque(maxlen=500))  # INCREASED from 200
trade_stats = defaultdict(lambda: {'checks': 0, 'obi_pass': 0, 'vol_pass': 0, 'spread_pass': 0, 'entries': 0})
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
            pass

def _fmt_ts(ts=None):
    t = datetime.datetime.utcfromtimestamp(ts or time.time())
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")

async def send_tg_info(title, body_lines):
    header = f"{title}\nTime: {_fmt_ts()}\n"
    body = "\n".join(body_lines)
    msg = header + "\n" + body
    await send_tg_raw(msg)

async def send_tg_entry(symbol, side, qty, entry_price, sl, tp, usd_alloc, score=0, filled=True):
    body = [
        f"Action: ENTRY {side}",
        f"Symbol: {symbol}",
        f"Qty: {qty}",
        f"Entry Price: {entry_price}",
        f"Stop Loss: {sl}",
        f"Take Profit: {tp}",
        f"Allocated USDT: {usd_alloc:.2f}",
        f"Entry Score: {score:.2f}",
        f"Filled: {filled}"
    ]
    await send_tg_info("🟢 Scalper Entry", body)

async def send_tg_exit(symbol, side, qty, exit_price, entry_price, reason, realized_usdt=None):
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0.0
    emoji = "🟢" if pnl_pct > 0 else "🔴"
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
    await send_tg_info(f"{emoji} Scalper Exit", body)

async def send_tg_error(symbol, err):
    body = [
        f"Symbol: {symbol}",
        f"Error: {str(err)}"
    ]
    await send_tg_info("⚠️ Scalper Error", body)

async def send_tg_heartbeat(total_symbols, total_positions, usdt_balance=None, stats=None):
    body = [
        f"Symbols Monitored: {total_symbols}",
        f"Open Positions: {total_positions}"
    ]
    if usdt_balance is not None:
        body.append(f"USDT Free: {usdt_balance:.2f}")
    if stats:
        body.append(f"Total Checks: {stats.get('checks', 0)}")
        body.append(f"OBI Pass Rate: {stats.get('obi_rate', 0):.1f}%")
        body.append(f"Vol Pass Rate: {stats.get('vol_rate', 0):.1f}%")
        body.append(f"Spread Pass Rate: {stats.get('spread_rate', 0):.1f}%")
        body.append(f"Total Entries: {stats.get('entries', 0)}")
    await send_tg_info("💓 Scalper Heartbeat", body)

async def send_tg_diagnostic(symbol, obi, vol, spread, score, reason):
    """Send diagnostic info when a trade is almost triggered"""
    body = [
        f"Symbol: {symbol}",
        f"OBI: {obi:.4f} (need > {MIN_OBI_THRESHOLD})",
        f"Volatility: {vol:.6f} (need {MIN_VOL_THRESHOLD} < x < {MAX_VOL_THRESHOLD})",
        f"Spread: {spread:.4f} (need < {MAX_SPREAD_PCT})",
        f"Entry Score: {score:.2f} (need >= {ENTRY_SCORE_THRESHOLD})",
        f"Status: {reason}"
    ]
    await send_tg_info("🔍 Near-Entry Signal", body)

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
        now = time.time()
        for t in trades:
            # Use actual trade time from API, convert from ms to seconds
            trade_time = t.get('time', now * 1000) / 1000.0
            dq.append((float(t['price']), float(t['qty']), t.get('isBuyerMaker', False), trade_time))
    except Exception as e:
        logging.debug("Failed to update trades for %s: %s", symbol, e)

def micro_volatility(symbol):
    """Calculate volatility from trades within the VOL_WINDOW timeframe"""
    dq = recent_trades[symbol]
    if len(dq) < 10:
        return 0.0
    
    now = time.time()
    # FIXED: Filter trades by time window
    recent = [(p, q, bm, t) for p, q, bm, t in dq if now - t <= VOL_WINDOW]
    
    if len(recent) < 5:
        # Fallback to all trades if not enough in window
        recent = list(dq)[-50:]
    
    if len(recent) < 5:
        return 0.0
    
    prices = np.array([p for p, _, _, _ in recent])
    # Coefficient of variation (std / mean)
    vol = float(np.std(prices) / (np.mean(prices) + 1e-9))
    return vol

def calculate_trade_flow(symbol):
    """NEW: Calculate buy/sell trade flow imbalance from recent trades"""
    dq = recent_trades[symbol]
    if len(dq) < 10:
        return 0.0
    
    now = time.time()
    recent = [(p, q, bm, t) for p, q, bm, t in dq if now - t <= VOL_WINDOW]
    
    if len(recent) < 5:
        return 0.0
    
    buy_vol = sum(q for _, q, bm, _ in recent if not bm)  # Taker buys
    sell_vol = sum(q for _, q, bm, _ in recent if bm)      # Taker sells
    total = buy_vol + sell_vol
    
    if total == 0:
        return 0.0
    
    return (buy_vol - sell_vol) / total

async def get_orderbook_imbalance(client, symbol, depth=10):  # INCREASED depth from 5
    try:
        ob = await client.get_order_book(symbol=symbol, limit=depth)
        
        if not ob['bids'] or not ob['asks']:
            return 0.0, 1.0, 0.0
        
        # Calculate weighted imbalance (closer to spread = more weight)
        bid_vol = 0
        ask_vol = 0
        for i, (b, a) in enumerate(zip(ob['bids'], ob['asks'])):
            weight = 1.0 / (i + 1)  # Higher weight for levels closer to spread
            bid_vol += float(b[1]) * weight
            ask_vol += float(a[1]) * weight
        
        best_bid = float(ob['bids'][0][0])
        best_ask = float(ob['asks'][0][0])
        spread = (best_ask - best_bid) / best_bid
        obi = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
        mid = (best_ask + best_bid) / 2.0
        return obi, spread, mid
    except Exception as e:
        logging.debug("Order book error for %s: %s", symbol, e)
        return 0.0, 1.0, 0.0

def calculate_entry_score(obi, vol, spread, trade_flow):
    """
    NEW: Calculate a composite entry score instead of hard thresholds.
    This allows trades even when individual metrics are slightly off.
    """
    score = 0.0
    
    # OBI contribution (0-2 points)
    if obi > 0.10:
        score += 2.0
    elif obi > 0.05:
        score += 1.5
    elif obi > 0.02:
        score += 1.0
    elif obi > 0.01:
        score += 0.5
    
    # Volatility contribution (0-1 points) - want moderate volatility
    if MIN_VOL_THRESHOLD < vol < MAX_VOL_THRESHOLD:
        score += 1.0
    elif vol < MAX_VOL_THRESHOLD * 1.5:
        score += 0.5
    
    # Spread contribution (0-1 points) - lower is better
    if spread < 0.001:
        score += 1.0
    elif spread < 0.002:
        score += 0.5
    elif spread < MAX_SPREAD_PCT:
        score += 0.25
    
    # Trade flow contribution (0-1 points)
    if trade_flow > 0.15:
        score += 1.0
    elif trade_flow > 0.05:
        score += 0.5
    
    return score

# ---------------- ENTRY / EXIT ----------------
async def try_entry(client, symbol, mid_price, usd_alloc):
    filt = symbol_filters.get(symbol, {})
    min_notional = filt.get('minNotional', MIN_NOTIONAL)
    qty = format_quantity(symbol, usd_alloc / mid_price)
    if qty <= 0 or usd_alloc < min_notional:
        logging.debug("Entry skipped for %s: qty=%s, alloc=%s, min_notional=%s", symbol, qty, usd_alloc, min_notional)
        return None

    limit_price = round(mid_price * 0.9998, 8)  # ADJUSTED: slightly better price (was 0.9995)
    try:
        if PAPER_MODE:
            filled = True
            logging.info("📈 PAPER BUY %s qty=%s @ %s", symbol, qty, limit_price)
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
            logging.info("📉 PAPER SELL %s qty=%s", symbol, pos['q'])
            return True
        await client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=pos['q'])
        return True
    except Exception as e:
        logging.exception("Exit failed for %s: %s", symbol, e)
        return False

# ---------------- TRADING LOOP ----------------
async def scalper_loop(client, symbol):
    positions.setdefault(symbol, [])
    last_diag_log[symbol] = 0
    
    while True:
        if emergency_stop:
            await asyncio.sleep(1)
            continue
        try:
            # Update micro data
            await update_recent_trades(client, symbol)
            obi, spread, mid = await get_orderbook_imbalance(client, symbol, depth=10)
            vol = micro_volatility(symbol)
            trade_flow = calculate_trade_flow(symbol)

            now = time.time()
            stats = trade_stats[symbol]
            stats['checks'] += 1

            # Skip if in cooldown
            if now - last_trade_time.get(symbol, 0) < COOLDOWN:
                await asyncio.sleep(0.4)
                continue

            # Track pass rates for diagnostics
            spread_ok = spread <= MAX_SPREAD_PCT
            obi_ok = obi > MIN_OBI_THRESHOLD
            vol_ok = MIN_VOL_THRESHOLD < vol < MAX_VOL_THRESHOLD
            
            if spread_ok:
                stats['spread_pass'] += 1
            if obi_ok:
                stats['obi_pass'] += 1
            if vol_ok:
                stats['vol_pass'] += 1

            # Skip if spread too wide
            if not spread_ok:
                await asyncio.sleep(0.4)
                continue

            # Calculate entry score
            score = calculate_entry_score(obi, vol, spread, trade_flow)
            
            # Diagnostic logging (not too frequent)
            if now - last_diag_log.get(symbol, 0) > DIAG_LOG_INTERVAL:
                logging.info(
                    "📊 %s | OBI: %.4f | Vol: %.6f | Spread: %.4f | Flow: %.4f | Score: %.2f",
                    symbol, obi, vol, spread, trade_flow, score
                )
                last_diag_log[symbol] = now

            # Entry condition using scoring system
            can_enter = total_open_positions() < MAX_POSITIONS
            score_ok = score >= ENTRY_SCORE_THRESHOLD
            
            if can_enter and score_ok:
                # Compute allocation
                if PAPER_MODE:
                    usdt_free = 1000.0
                else:
                    acc = await client.get_account()
                    usdt_free = float(next((b['free'] for b in acc['balances'] if b['asset'] == 'USDT'), 0.0))
                
                # Scale allocation by score (higher score = more confidence)
                base_alloc = usdt_free * 0.01  # INCREASED from 0.5% to 1% per scalp
                score_multiplier = min(1.5, score / ENTRY_SCORE_THRESHOLD)  # Up to 1.5x for strong signals
                vol_adj = max(0.5, 1.0 - (vol * 100))  # Reduce size if vol high
                usd_alloc = base_alloc * score_multiplier * vol_adj

                if usd_alloc >= MIN_NOTIONAL:
                    entry = await try_entry(client, symbol, mid, usd_alloc)
                    if entry and entry.get('filled'):
                        entry_price = entry['entry_price']
                        q = entry['q']
                        sl = entry_price * (1 - SL_PCT)
                        tp = entry_price * (1 + TP_PCT)
                        positions[symbol].append({'p': entry_price, 'q': q, 'sl': sl, 'tp': tp, 't': now})
                        last_trade_time[symbol] = now
                        stats['entries'] += 1
                        await send_tg_entry(
                            symbol=symbol, side="BUY", qty=q, entry_price=entry_price,
                            sl=sl, tp=tp, usd_alloc=usd_alloc, score=score, filled=True
                        )
            
            # Near-miss logging (for debugging)
            elif can_enter and score >= ENTRY_SCORE_THRESHOLD * 0.7:
                if now - last_diag_log.get(f"{symbol}_near", 0) > 60:
                    reasons = []
                    if not obi_ok:
                        reasons.append(f"OBI low ({obi:.4f})")
                    if not vol_ok:
                        reasons.append(f"Vol out of range ({vol:.6f})")
                    if score < ENTRY_SCORE_THRESHOLD:
                        reasons.append(f"Score low ({score:.2f})")
                    
                    logging.info("⚡ Near-entry for %s: %s", symbol, ", ".join(reasons))
                    last_diag_log[f"{symbol}_near"] = now

            # Check exits for each position
            for pos in positions[symbol][:]:
                price = mid
                exit_triggered = False
                reason = ""
                
                if price <= pos['sl']:
                    exit_triggered = True
                    reason = "SL"
                elif price >= pos['tp']:
                    exit_triggered = True
                    reason = "TP"
                elif obi < -0.05:  # RELAXED from -0.08: Exit on bearish imbalance
                    exit_triggered = True
                    reason = "Bearish Signal"
                elif trade_flow < -0.15:  # NEW: Exit on heavy selling
                    exit_triggered = True
                    reason = "Sell Pressure"
                
                # Time-based exit (don't hold too long for scalping)
                elif now - pos['t'] > 120:  # 2 minutes max hold time
                    exit_triggered = True
                    reason = "Time Limit"
                
                if exit_triggered:
                    ok = await try_exit(client, symbol, pos)
                    if ok:
                        positions[symbol].remove(pos)
                        realized = pos['q'] * price
                        await send_tg_exit(
                            symbol=symbol, side="SELL", qty=pos['q'],
                            exit_price=price, entry_price=pos['p'],
                            reason=reason, realized_usdt=realized
                        )

            await asyncio.sleep(0.5)  # FASTER loop (was 0.9)
        except Exception as e:
            logging.exception("Loop error %s: %s", symbol, e)
            await send_tg_error(symbol, e)
            await asyncio.sleep(1)

# ---------------- HEARTBEAT ----------------
async def heartbeat_task(client, symbols, interval=HEARTBEAT_INTERVAL):
    await asyncio.sleep(30)  # Initial delay to collect some stats
    while True:
        try:
            if PAPER_MODE:
                usdt_free = 1000.0
            else:
                acc = await client.get_account()
                usdt_free = float(next((b['free'] for b in acc['balances'] if b['asset'] == 'USDT'), 0.0))
            
            # Aggregate stats
            total_checks = sum(s['checks'] for s in trade_stats.values())
            total_obi = sum(s['obi_pass'] for s in trade_stats.values())
            total_vol = sum(s['vol_pass'] for s in trade_stats.values())
            total_spread = sum(s['spread_pass'] for s in trade_stats.values())
            total_entries = sum(s['entries'] for s in trade_stats.values())
            
            stats = {
                'checks': total_checks,
                'obi_rate': (total_obi / total_checks * 100) if total_checks > 0 else 0,
                'vol_rate': (total_vol / total_checks * 100) if total_checks > 0 else 0,
                'spread_rate': (total_spread / total_checks * 100) if total_checks > 0 else 0,
                'entries': total_entries
            }
            
            logging.info(
                "💓 Heartbeat | Symbols: %d | Positions: %d | USDT: %.2f | Entries: %d",
                len(symbols), total_open_positions(), usdt_free, total_entries
            )
            logging.info(
                "📈 Pass Rates | OBI: %.1f%% | Vol: %.1f%% | Spread: %.1f%%",
                stats['obi_rate'], stats['vol_rate'], stats['spread_rate']
            )
            
            await send_tg_heartbeat(
                total_symbols=len(symbols),
                total_positions=total_open_positions(),
                usdt_balance=usdt_free,
                stats=stats
            )
        except Exception as e:
            logging.exception("Heartbeat error: %s", e)
            await send_tg_error("heartbeat", e)
        await asyncio.sleep(interval)

# ---------------- STARTUP DIAGNOSTICS ----------------
async def run_startup_diagnostics(client, symbols):
    """NEW: Run quick diagnostics to verify conditions are achievable"""
    logging.info("🔍 Running startup diagnostics on %d symbols...", len(symbols))
    
    results = {'spread_ok': 0, 'obi_ok': 0, 'vol_ok': 0, 'total': len(symbols)}
    
    for symbol in symbols[:10]:  # Check first 10 symbols
        await update_recent_trades(client, symbol)
        obi, spread, mid = await get_orderbook_imbalance(client, symbol, depth=10)
        vol = micro_volatility(symbol)
        
        if spread <= MAX_SPREAD_PCT:
            results['spread_ok'] += 1
        if obi > MIN_OBI_THRESHOLD:
            results['obi_ok'] += 1
        if MIN_VOL_THRESHOLD < vol < MAX_VOL_THRESHOLD:
            results['vol_ok'] += 1
        
        logging.info(
            "  %s: OBI=%.4f (%s) | Vol=%.6f (%s) | Spread=%.4f (%s)",
            symbol,
            obi, "✓" if obi > MIN_OBI_THRESHOLD else "✗",
            vol, "✓" if MIN_VOL_THRESHOLD < vol < MAX_VOL_THRESHOLD else "✗",
            spread, "✓" if spread <= MAX_SPREAD_PCT else "✗"
        )
        await asyncio.sleep(0.1)
    
    logging.info(
        "📊 Diagnostic Summary: Spread OK: %d/10 | OBI OK: %d/10 | Vol OK: %d/10",
        results['spread_ok'], results['obi_ok'], results['vol_ok']
    )
    
    return results

# ---------------- MAIN ----------------
async def main():
    global PAPER_MODE
    
    logging.info("=" * 60)
    logging.info("🚀 Starting Scalper Bot")
    logging.info("=" * 60)
    logging.info("Config: PAPER_MODE=%s, USE_TESTNET=%s", PAPER_MODE, USE_TESTNET)
    logging.info("Entry Thresholds: OBI>%.3f, Vol<%.5f, Spread<%.4f, Score>=%.1f",
                 MIN_OBI_THRESHOLD, MAX_VOL_THRESHOLD, MAX_SPREAD_PCT, ENTRY_SCORE_THRESHOLD)
    logging.info("=" * 60)
    
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=USE_TESTNET)
    symbols = await setup_filters(client)
    logging.info("✅ Loaded %d tradeable symbols", len(symbols))
    
    # Run startup diagnostics
    await run_startup_diagnostics(client, symbols)
    
    await send_tg_info("🚀 Scalper Started", [
        f"Symbols: {len(symbols)}",
        f"Paper mode: {PAPER_MODE}",
        f"Testnet: {USE_TESTNET}",
        f"Entry Score Threshold: {ENTRY_SCORE_THRESHOLD}",
        f"OBI Threshold: {MIN_OBI_THRESHOLD}",
        f"Max Volatility: {MAX_VOL_THRESHOLD}"
    ])

    # Warm recent trades
    logging.info("⏳ Warming up trade data...")
    for s in symbols:
        await update_recent_trades(client, s)
        await asyncio.sleep(0.05)
    logging.info("✅ Trade data warmed up")

    tasks = [scalper_loop(client, s) for s in symbols]
    tasks.append(heartbeat_task(client, symbols, interval=HEARTBEAT_INTERVAL))
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("⛔ Stopped by user")
