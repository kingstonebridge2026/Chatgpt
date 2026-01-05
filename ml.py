
#!/usr/bin/env python3
"""
Binance Micro Scalper - High-Frequency Trading Bot

Features:
- Monitors 100 coins simultaneously
- Opens many trades per hour (scalping mode)
- Uses OBI, VWAP, Z-Score, Volume analysis
- Multiple concurrent positions
- Real-time Telegram notifications

RISK WARNING: Scalping involves high frequency trading with significant risk.
Never invest more than you can afford to lose.

Install:
    pip install python-binance aiohttp numpy pandas

Run:
    python binance_micro_trader.py
"""

import os
import asyncio
import math
import time
import logging
import datetime
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from enum import Enum

import aiohttp
import numpy as np
from binance import AsyncClient
from binance.enums import *

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# API Keys
API_KEY = os.getenv("BINANCE_API_KEY", "Et7oRtg2CLHyaRGBoQOoTFt7LSixfav28k0bnVfcgzxd2KTal4xPlxZ9aO6sr1EJ")
API_SECRET = os.getenv("BINANCE_API_SECRET", "2LfotApekUjBH6jScuzj1c47eEnq1ViXsNRIP4ydYqYWl6brLhU3JY4vqlftnUIo")

# Trading Mode
PAPER_MODE = True           # True = simulate trades
USE_TESTNET = False         # True = Binance testnet

# Telegram
TG_TOKEN = os.getenv("TG_TOKEN", "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "5665906172")

# ═══════════════ SCALPING SETTINGS ═══════════════
INITIAL_CAPITAL = 10.0      # Starting USDT
SYMBOLS_TO_MONITOR = 100    # Monitor top 100 coins
MAX_POSITIONS = 30          # Many concurrent positions for scalping
POSITION_SIZE_PCT = 0.03    # 3% of capital per trade
MIN_TRADE_VALUE = 5.0       # Minimum USDT per trade

# Scalping Targets (tight for quick profits)
TP_PERCENT = 0.003          # 0.3% take profit
SL_PERCENT = 0.002          # 0.2% stop loss
TRAILING_STOP_PCT = 0.0015  # 0.15% trailing

# Timing
TRADE_COOLDOWN = 3          # 3 seconds cooldown per symbol
MAX_HOLD_TIME = 180         # Exit after 3 minutes max
SCAN_INTERVAL = 0.3         # Scan every 300ms

# Risk Management
MAX_DAILY_LOSS_PCT = 0.15   # Stop if down 15%
MAX_HOURLY_TRADES = 100     # Max trades per hour
MAX_SPREAD_PCT = 0.002      # Max 0.2% spread

# ═══════════════ SIGNAL THRESHOLDS ═══════════════
# OBI (Order Book Imbalance)
OBI_STRONG_BUY = 0.15       # Strong buy signal
OBI_BUY = 0.08              # Normal buy signal
OBI_DEPTH = 10              # Order book depth

# VWAP
VWAP_DEVIATION_BUY = -0.002  # Buy when price 0.2% below VWAP
VWAP_DEVIATION_SELL = 0.003  # Sell when price 0.3% above VWAP

# Z-Score
ZSCORE_BUY = -1.5           # Buy when Z-score below -1.5
ZSCORE_SELL = 1.5           # Sell when Z-score above 1.5
ZSCORE_LOOKBACK = 20        # Periods for Z-score

# Volume
VOLUME_SURGE_MULT = 2.0     # Volume must be 2x average
VOLUME_LOOKBACK = 20        # Periods for volume average

# Combined Score
MIN_ENTRY_SCORE = 3.0       # Minimum score to enter
MIN_EXIT_SCORE = -2.0       # Score to trigger exit

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: float
    entry_time: float
    stop_loss: float
    take_profit: float
    highest_price: float
    entry_score: float
    obi: float
    zscore: float

@dataclass 
class MarketData:
    price: float
    bid: float
    ask: float
    spread: float
    obi: float
    vwap: float
    vwap_dev: float
    zscore: float
    volume_ratio: float
    score: float

@dataclass
class Stats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    hourly_trades: int = 0
    last_hour_reset: float = 0.0

# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════════════════════════

class State:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.filters: Dict[str, dict] = {}
        self.last_trade: Dict[str, float] = {}
        
        # Price/Volume history for each symbol
        self.prices: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.volumes: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.trades_data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        
        # Balances
        self.balance: float = INITIAL_CAPITAL
        self.paper_balance: float = INITIAL_CAPITAL
        self.start_balance: float = INITIAL_CAPITAL
        
        # Stats
        self.stats = Stats(last_hour_reset=time.time())
        self.running = True

state = State()

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def tg_send(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={'chat_id': TG_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
            )
    except:
        pass

async def notify_entry(pos: Position):
    msg = f"""🟢 <b>SCALP ENTRY</b>
{pos.symbol} @ ${pos.entry_price:.6f}
Qty: {pos.quantity:.4f}
Score: {pos.entry_score:.2f} | OBI: {pos.obi:.3f} | Z: {pos.zscore:.2f}
SL: ${pos.stop_loss:.6f} | TP: ${pos.take_profit:.6f}"""
    await tg_send(msg)

async def notify_exit(symbol: str, entry: float, exit_p: float, pnl: float, pnl_pct: float, reason: str):
    emoji = "🟢" if pnl > 0 else "🔴"
    msg = f"""{emoji} <b>EXIT - {reason}</b>
{symbol}: ${entry:.6f} → ${exit_p:.6f}
PnL: ${pnl:+.4f} ({pnl_pct:+.3f}%)
Balance: ${state.balance:.2f}"""
    await tg_send(msg)

async def notify_stats():
    s = state.stats
    wr = (s.wins / s.trades * 100) if s.trades > 0 else 0
    msg = f"""📊 <b>HOURLY STATS</b>
Trades: {s.trades} | Win Rate: {wr:.1f}%
PnL: ${s.total_pnl:+.2f}
Balance: ${state.balance:.2f}
Positions: {len(state.positions)}/{MAX_POSITIONS}"""
    await tg_send(msg)

# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def fmt_qty(symbol: str, qty: float) -> float:
    step = state.filters.get(symbol, {}).get('step', 0.00001)
    if step <= 0:
        return round(qty, 8)
    prec = max(0, int(round(-math.log10(step))))
    return round(math.floor(qty / step) * step, prec)

def fmt_price(symbol: str, price: float) -> float:
    tick = state.filters.get(symbol, {}).get('tick', 0.00000001)
    if tick <= 0:
        return round(price, 8)
    prec = max(0, int(round(-math.log10(tick))))
    return round(price, prec)

# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def calc_obi(bids: List, asks: List, depth: int = OBI_DEPTH) -> float:
    """Order Book Imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol)"""
    if not bids or not asks:
        return 0.0
    
    # Weighted by distance from mid (closer = higher weight)
    bid_vol = sum(float(b[1]) / (i + 1) for i, b in enumerate(bids[:depth]))
    ask_vol = sum(float(a[1]) / (i + 1) for i, a in enumerate(asks[:depth]))
    
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    
    return (bid_vol - ask_vol) / total

def calc_vwap(trades: deque) -> Tuple[float, float]:
    """Calculate VWAP and current deviation from it."""
    if len(trades) < 10:
        return 0.0, 0.0
    
    recent = list(trades)[-100:]
    
    total_pv = sum(t['p'] * t['q'] for t in recent)
    total_v = sum(t['q'] for t in recent)
    
    if total_v == 0:
        return 0.0, 0.0
    
    vwap = total_pv / total_v
    current_price = recent[-1]['p']
    deviation = (current_price - vwap) / vwap if vwap > 0 else 0.0
    
    return vwap, deviation

def calc_zscore(prices: deque, lookback: int = ZSCORE_LOOKBACK) -> float:
    """Z-Score: (price - mean) / std"""
    if len(prices) < lookback:
        return 0.0
    
    recent = list(prices)[-lookback:]
    mean = np.mean(recent)
    std = np.std(recent)
    
    if std == 0:
        return 0.0
    
    current = recent[-1]
    return (current - mean) / std

def calc_volume_ratio(volumes: deque, lookback: int = VOLUME_LOOKBACK) -> float:
    """Current volume / average volume."""
    if len(volumes) < lookback:
        return 1.0
    
    recent = list(volumes)[-lookback:]
    avg = np.mean(recent[:-1]) if len(recent) > 1 else recent[0]
    current = recent[-1]
    
    if avg == 0:
        return 1.0
    
    return current / avg

def calc_trade_flow(trades: deque, window: int = 30) -> float:
    """Net trade flow: buy volume - sell volume."""
    if len(trades) < 5:
        return 0.0
    
    now = time.time()
    recent = [t for t in trades if now - t.get('time', now) < window]
    
    if not recent:
        return 0.0
    
    buy_vol = sum(t['q'] for t in recent if not t.get('m', False))
    sell_vol = sum(t['q'] for t in recent if t.get('m', False))
    total = buy_vol + sell_vol
    
    if total == 0:
        return 0.0
    
    return (buy_vol - sell_vol) / total

def calc_entry_score(obi: float, vwap_dev: float, zscore: float, vol_ratio: float, trade_flow: float) -> float:
    """Calculate combined entry score."""
    score = 0.0
    
    # OBI Score (0-3 points)
    if obi >= OBI_STRONG_BUY:
        score += 3.0
    elif obi >= OBI_BUY:
        score += 2.0
    elif obi >= OBI_BUY * 0.5:
        score += 1.0
    
    # VWAP Score (0-2 points) - buy below VWAP
    if vwap_dev <= VWAP_DEVIATION_BUY:
        score += 2.0
    elif vwap_dev <= 0:
        score += 1.0
    
    # Z-Score (0-2 points) - buy when oversold
    if zscore <= ZSCORE_BUY:
        score += 2.0
    elif zscore <= ZSCORE_BUY * 0.5:
        score += 1.0
    
    # Volume confirmation (0-2 points)
    if vol_ratio >= VOLUME_SURGE_MULT:
        score += 2.0
    elif vol_ratio >= VOLUME_SURGE_MULT * 0.7:
        score += 1.0
    
    # Trade flow bonus (0-1 point)
    if trade_flow > 0.2:
        score += 1.0
    elif trade_flow > 0.1:
        score += 0.5
    
    return score

# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

async def get_top_symbols(client: AsyncClient) -> List[str]:
    """Get top 100 volume USDT pairs."""
    try:
        tickers = await client.get_ticker()
        
        usdt = [
            t for t in tickers
            if t['symbol'].endswith('USDT')
            and 'UP' not in t['symbol']
            and 'DOWN' not in t['symbol']
            and 'BEAR' not in t['symbol']
            and 'BULL' not in t['symbol']
            and float(t.get('quoteVolume', 0)) > 1000000  # Min $1M volume
        ]
        
        usdt.sort(key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
        return [t['symbol'] for t in usdt[:SYMBOLS_TO_MONITOR]]
    
    except Exception as e:
        logger.error(f"Failed to get symbols: {e}")
        return ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']

async def setup_filters(client: AsyncClient, symbols: List[str]):
    """Load trading rules."""
    try:
        info = await client.get_exchange_info()
        
        for s in info['symbols']:
            if s['symbol'] not in symbols:
                continue
            
            f = {x['filterType']: x for x in s['filters']}
            
            state.filters[s['symbol']] = {
                'step': float(f.get('LOT_SIZE', {}).get('stepSize', 0.00001)),
                'tick': float(f.get('PRICE_FILTER', {}).get('tickSize', 0.00000001)),
                'min_notional': float(f.get('NOTIONAL', {}).get('minNotional', 5.0))
            }
    except Exception as e:
        logger.error(f"Failed to load filters: {e}")

async def fetch_orderbook(client: AsyncClient, symbol: str) -> Tuple[List, List, float, float, float]:
    """Fetch order book and calculate spread."""
    try:
        ob = await client.get_order_book(symbol=symbol, limit=OBI_DEPTH)
        
        if not ob['bids'] or not ob['asks']:
            return [], [], 0, 0, 1.0
        
        bids = ob['bids']
        asks = ob['asks']
        
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        spread = (best_ask - best_bid) / mid
        
        return bids, asks, best_bid, best_ask, spread
    
    except Exception:
        return [], [], 0, 0, 1.0

async def fetch_recent_trades(client: AsyncClient, symbol: str):
    """Fetch recent trades for VWAP calculation."""
    try:
        trades = await client.get_recent_trades(symbol=symbol, limit=100)
        
        now = time.time()
        for t in trades:
            state.trades_data[symbol].append({
                'p': float(t['price']),
                'q': float(t['qty']),
                'm': t.get('isBuyerMaker', False),
                'time': t.get('time', now * 1000) / 1000
            })
    except Exception:
        pass

async def analyze_symbol(client: AsyncClient, symbol: str) -> Optional[MarketData]:
    """Full analysis of a symbol."""
    try:
        # Fetch order book
        bids, asks, bid, ask, spread = await fetch_orderbook(client, symbol)
        
        if spread > MAX_SPREAD_PCT or bid == 0:
            return None
        
        mid = (bid + ask) / 2
        
        # Update price history
        state.prices[symbol].append(mid)
        
        # Fetch recent trades (for VWAP)
        await fetch_recent_trades(client, symbol)
        
        # Calculate indicators
        obi = calc_obi(bids, asks, OBI_DEPTH)
        vwap, vwap_dev = calc_vwap(state.trades_data[symbol])
        zscore = calc_zscore(state.prices[symbol], ZSCORE_LOOKBACK)
        
        # Volume from trades
        recent_trades = list(state.trades_data[symbol])[-50:]
        if recent_trades:
            current_vol = sum(t['q'] for t in recent_trades[-10:])
            avg_vol = sum(t['q'] for t in recent_trades[:-10]) / max(1, len(recent_trades) - 10)
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
        else:
            vol_ratio = 1.0
        
        # Trade flow
        trade_flow = calc_trade_flow(state.trades_data[symbol])
        
        # Combined score
        score = calc_entry_score(obi, vwap_dev, zscore, vol_ratio, trade_flow)
        
        return MarketData(
            price=mid,
            bid=bid,
            ask=ask,
            spread=spread,
            obi=obi,
            vwap=vwap,
            vwap_dev=vwap_dev,
            zscore=zscore,
            volume_ratio=vol_ratio,
            score=score
        )
    
    except Exception as e:
        logger.debug(f"Analysis failed for {symbol}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

async def open_position(client: AsyncClient, symbol: str, data: MarketData) -> bool:
    """Open a new scalping position."""
    # Check limits
    if len(state.positions) >= MAX_POSITIONS:
        return False
    
    if symbol in state.positions:
        return False
    
    # Cooldown check
    if time.time() - state.last_trade.get(symbol, 0) < TRADE_COOLDOWN:
        return False
    
    # Hourly trade limit
    if state.stats.hourly_trades >= MAX_HOURLY_TRADES:
        return False
    
    # Calculate size
    balance = state.paper_balance if PAPER_MODE else state.balance
    trade_value = balance * POSITION_SIZE_PCT
    
    min_notional = state.filters.get(symbol, {}).get('min_notional', MIN_TRADE_VALUE)
    if trade_value < max(min_notional, MIN_TRADE_VALUE):
        return False
    
    qty = fmt_qty(symbol, trade_value / data.price)
    if qty <= 0:
        return False
    
    actual_value = qty * data.price
    if actual_value > balance * 0.95:
        return False
    
    try:
        if PAPER_MODE:
            entry_price = data.ask  # Simulate buying at ask
            state.paper_balance -= actual_value
            logger.info(f"📈 PAPER BUY {symbol}: {qty:.6f} @ ${entry_price:.6f} (Score: {data.score:.1f})")
        else:
            order = await client.create_order(
                symbol=symbol,
                side=SIDE_BUY,
                type=ORDER_TYPE_MARKET,
                quantity=qty
            )
            fills = order.get('fills', [])
            entry_price = float(fills[0]['price']) if fills else data.ask
            logger.info(f"✅ BUY {symbol}: {qty:.6f} @ ${entry_price:.6f}")
        
        # Create position
        pos = Position(
            symbol=symbol,
            entry_price=entry_price,
            quantity=qty,
            entry_time=time.time(),
            stop_loss=fmt_price(symbol, entry_price * (1 - SL_PERCENT)),
            take_profit=fmt_price(symbol, entry_price * (1 + TP_PERCENT)),
            highest_price=entry_price,
            entry_score=data.score,
            obi=data.obi,
            zscore=data.zscore
        )
        
        state.positions[symbol] = pos
        state.last_trade[symbol] = time.time()
        state.stats.hourly_trades += 1
        
        await notify_entry(pos)
        return True
    
    except Exception as e:
        logger.error(f"Failed to open {symbol}: {e}")
        return False

async def close_position(client: AsyncClient, symbol: str, current_price: float, reason: str) -> bool:
    """Close a position."""
    pos = state.positions.get(symbol)
    if not pos:
        return False
    
    try:
        if PAPER_MODE:
            exit_price = current_price * 0.9999  # Simulate selling at slightly lower
            value = pos.quantity * exit_price
            state.paper_balance += value
            logger.info(f"📉 PAPER SELL {symbol}: {pos.quantity:.6f} @ ${exit_price:.6f} ({reason})")
        else:
            order = await client.create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=pos.quantity
            )
            fills = order.get('fills', [])
            exit_price = float(fills[0]['price']) if fills else current_price
            logger.info(f"✅ SELL {symbol}: {pos.quantity:.6f} @ ${exit_price:.6f}")
        
        # Calculate PnL
        pnl = (exit_price - pos.entry_price) * pos.quantity
        pnl_pct = (exit_price / pos.entry_price - 1) * 100
        
        # Update stats
        state.stats.trades += 1
        state.stats.total_pnl += pnl
        if pnl > 0:
            state.stats.wins += 1
        else:
            state.stats.losses += 1
        
        # Update balance
        if PAPER_MODE:
            state.balance = state.paper_balance
        
        # Remove position
        del state.positions[symbol]
        
        await notify_exit(symbol, pos.entry_price, exit_price, pnl, pnl_pct, reason)
        return True
    
    except Exception as e:
        logger.error(f"Failed to close {symbol}: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

async def manage_positions(client: AsyncClient, prices: Dict[str, float]):
    """Check all positions for exit conditions."""
    now = time.time()
    
    for symbol, pos in list(state.positions.items()):
        price = prices.get(symbol)
        if not price:
            continue
        
        # Update trailing stop
        if price > pos.highest_price:
            pos.highest_price = price
            new_sl = price * (1 - TRAILING_STOP_PCT)
            if new_sl > pos.stop_loss:
                pos.stop_loss = fmt_price(symbol, new_sl)
        
        exit_reason = None
        
        # Stop loss
        if price <= pos.stop_loss:
            exit_reason = "SL"
        
        # Take profit
        elif price >= pos.take_profit:
            exit_reason = "TP"
        
        # Trailing stop
        elif pos.highest_price > pos.entry_price * 1.001:
            trailing_sl = pos.highest_price * (1 - TRAILING_STOP_PCT)
            if price <= trailing_sl:
                exit_reason = "TRAIL"
        
        # Max hold time
        elif now - pos.entry_time > MAX_HOLD_TIME:
            pnl_pct = (price / pos.entry_price - 1) * 100
            if pnl_pct > -0.1:  # Only exit if not in significant loss
                exit_reason = "TIME"
        
        # Dynamic exit based on signals turning negative
        if not exit_reason and len(state.prices[symbol]) > 20:
            zscore = calc_zscore(state.prices[symbol])
            if zscore > ZSCORE_SELL:
                pnl_pct = (price / pos.entry_price - 1) * 100
                if pnl_pct > 0:
                    exit_reason = "SIGNAL"
        
        if exit_reason:
            await close_position(client, symbol, price, exit_reason)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN TRADING LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def scan_for_entries(client: AsyncClient, symbols: List[str]):
    """Scan all symbols for entry opportunities."""
    # Get current prices for position management
    try:
        tickers = await client.get_all_tickers()
        prices = {t['symbol']: float(t['price']) for t in tickers}
    except:
        prices = {}
    
    # Manage existing positions
    await manage_positions(client, prices)
    
    # Check if we can open more positions
    if len(state.positions) >= MAX_POSITIONS:
        return
    
    # Shuffle symbols to avoid always checking same ones first
    import random
    check_symbols = random.sample(symbols, min(30, len(symbols)))
    
    for symbol in check_symbols:
        if symbol in state.positions:
            continue
        
        if len(state.positions) >= MAX_POSITIONS:
            break
        
        data = await analyze_symbol(client, symbol)
        
        if data and data.score >= MIN_ENTRY_SCORE:
            logger.info(
                f"🎯 {symbol}: Score={data.score:.1f} OBI={data.obi:.3f} "
                f"Z={data.zscore:.2f} VWAP_dev={data.vwap_dev:.4f}"
            )
            await open_position(client, symbol, data)
        
        await asyncio.sleep(0.05)  # Small delay between checks

async def trading_loop(client: AsyncClient, symbols: List[str]):
    """Main scalping loop."""
    logger.info(f"🚀 Starting scalping loop with {len(symbols)} symbols")
    
    iteration = 0
    last_stats_time = time.time()
    
    while state.running:
        try:
            iteration += 1
            now = time.time()
            
            # Reset hourly stats
            if now - state.stats.last_hour_reset >= 3600:
                await notify_stats()
                state.stats.hourly_trades = 0
                state.stats.last_hour_reset = now
            
            # Check daily loss limit
            daily_pnl_pct = (state.balance - state.start_balance) / state.start_balance
            if daily_pnl_pct < -MAX_DAILY_LOSS_PCT:
                if iteration % 300 == 0:
                    logger.warning(f"⚠️ Daily loss limit reached: {daily_pnl_pct*100:.2f}%")
                await asyncio.sleep(60)
                continue
            
            # Scan for trades
            await scan_for_entries(client, symbols)
            
            # Status update every 30 seconds
            if now - last_stats_time >= 30:
                s = state.stats
                wr = (s.wins / s.trades * 100) if s.trades > 0 else 0
                logger.info(
                    f"💰 Bal: ${state.balance:.2f} | Pos: {len(state.positions)}/{MAX_POSITIONS} | "
                    f"Trades: {s.trades} (W:{s.wins} L:{s.losses} {wr:.0f}%) | "
                    f"PnL: ${s.total_pnl:+.3f}"
                )
                last_stats_time = now
            
            await asyncio.sleep(SCAN_INTERVAL)
        
        except Exception as e:
            logger.exception(f"Loop error: {e}")
            await asyncio.sleep(1)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    logger.info("=" * 70)
    logger.info("⚡ BINANCE MICRO SCALPER")
    logger.info("=" * 70)
    logger.info(f"Mode: {'PAPER TRADING' if PAPER_MODE else '🔴 LIVE TRADING'}")
    logger.info(f"Capital: ${INITIAL_CAPITAL:.2f}")
    logger.info(f"Symbols: {SYMBOLS_TO_MONITOR} | Max Positions: {MAX_POSITIONS}")
    logger.info(f"TP: {TP_PERCENT*100:.2f}% | SL: {SL_PERCENT*100:.2f}%")
    logger.info(f"Entry Score Threshold: {MIN_ENTRY_SCORE}")
    logger.info("=" * 70)
    
    if not PAPER_MODE and API_KEY == "YOUR_API_KEY_HERE":
        logger.error("❌ Set API keys for live trading!")
        return
    
    try:
        client = await AsyncClient.create(
            api_key=API_KEY,
            api_secret=API_SECRET,
            testnet=USE_TESTNET
        )
        
        # Get balance
        if not PAPER_MODE:
            acc = await client.get_account()
            for b in acc['balances']:
                if b['asset'] == 'USDT':
                    state.balance = float(b['free'])
                    state.start_balance = state.balance
                    break
            logger.info(f"💰 Live Balance: ${state.balance:.2f}")
        else:
            state.balance = INITIAL_CAPITAL
            state.paper_balance = INITIAL_CAPITAL
            state.start_balance = INITIAL_CAPITAL
        
        # Get symbols
        symbols = await get_top_symbols(client)
        logger.info(f"📊 Monitoring: {symbols[:5]}... ({len(symbols)} total)")
        
        # Setup filters
        await setup_filters(client, symbols)
        logger.info("✅ Filters loaded")
        
        # Notify start
        await tg_send(
            f"""⚡ <b>SCALPER STARTED</b>
Mode: {'Paper' if PAPER_MODE else 'Live'}
Capital: ${state.balance:.2f}
Symbols: {len(symbols)}
Max Positions: {MAX_POSITIONS}
TP: {TP_PERCENT*100:.2f}% | SL: {SL_PERCENT*100:.2f}%"""
        )
        
        # Start
        await trading_loop(client, symbols)
    
    except KeyboardInterrupt:
        logger.info("⛔ Stopped")
    except Exception as e:
        logger.exception(f"Fatal: {e}")
    finally:
        # Close all positions on shutdown (paper mode)
        if PAPER_MODE and state.positions:
            logger.info(f"Closing {len(state.positions)} positions...")
        
        await tg_send(f"⛔ <b>SCALPER STOPPED</b>\nFinal PnL: ${state.stats.total_pnl:+.2f}")

if __name__ == "__main__":
    asyncio.run(main())
