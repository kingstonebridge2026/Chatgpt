import asyncio, aiohttp, numpy as np, pandas as pd
from binance import AsyncClient, BinanceSocketManager
from binance.enums import *

# --- CREDENTIALS ---
API_KEY = 'Et7oRtg2CLHyaRGBoQOoTFt7LSixfav28k0bnVfcgzxd2KTal4xPlxZ9aO6sr1EJ'
API_SECRET = '2LfotApekUjBH6jScuzj1c47eEnq1ViXsNRIP4ydYqYWl6brLhU3JY4vqlftnUIo'
TG_TOKEN = '8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA'
TG_CHAT_ID = '5665906172'

SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 
    'ADAUSDT', 'DOGEUSDT', 'TRXUSDT', 'LINKUSDT', 'AVAXUSDT',
    'DOTUSDT', 'LTCUSDT', 'BCHUSDT', 'SUIUSDT', 'NEARUSDT',
    'SHIBUSDT', 'PEPEUSDT', 'XMRUSDT', 'STXUSDT', 'UNIUSDT'
]

# Tracking dictionary for Floating P&L
active_positions = {} # { 'BTCUSDT': {'buy_price': 60000, 'qty': 0.001} }

async def send_tg_async(msg):
    """Non-blocking Telegram sender using aiohttp"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    params = {'chat_id': TG_CHAT_ID, 'text': msg}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                return await resp.json()
    except Exception: pass

async def get_signals(client, symbol):
    klines = await client.get_klines(symbol=symbol, interval='1m', limit=30)
    df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qa','nt','tb','tq','i']).astype(float)
    
    df['tp'] = (df['h'] + df['l'] + df['c']) / 3
    vwap = (df['tp'] * df['v']).sum() / (df['v'].sum() + 0.0001)
    z_score = (df['c'].iloc[-1] - df['c'].mean()) / (df['c'].std() + 0.00001)
    
    depth = await client.get_order_book(symbol=symbol, limit=10)
    bid_v = sum([float(b[1]) for b in depth['bids']])
    ask_v = sum([float(a[1]) for a in depth['asks']])
    obi = (bid_v - ask_v) / (bid_v + ask_v)
    
    return z_score, obi, vwap, df['c'].iloc[-1]

async def monitor_portfolio(client):
    """Sends periodic updates of balance and floating P&L"""
    while True:
        try:
            # 1. Check USDT Balance
            acc_info = await client.get_account()
            usdt_balance = next((item['free'] for item in acc_info['balances'] if item['asset'] == 'USDT'), 0)
            
            # 2. Calculate Floating P&L
            total_unrealized_pnl = 0
            pos_count = len(active_positions)
            
            for sym, data in active_positions.items():
                ticker = await client.get_symbol_ticker(symbol=sym)
                current_price = float(ticker['price'])
                pnl = (current_price - data['buy_price']) * data['qty']
                total_unrealized_pnl += pnl

            status_msg = (
                f"📊 **PORTFOLIO UPDATE**\n"
                f"💰 USDT Balance: {float(usdt_balance):.2f}\n"
                f"📈 Open Positions: {pos_count}\n"
                f"💸 Floating P&L: {'+' if total_unrealized_pnl >= 0 else ''}{total_unrealized_pnl:.4f} USDT"
            )
            await send_tg_async(status_msg)
            await asyncio.sleep(60) # Send update every 60 seconds
        except Exception as e:
            await asyncio.sleep(10)

async def trade_logic(client, symbol):
    trade_amount = 0.00015 # Note: In real trading, adjust per symbol lot size
    print(f"📡 Monitoring {symbol}...")
    
    while True:
        try:
            z, obi, vwap, price = await get_signals(client, symbol)
            
            # ENTRY LOGIC
            if symbol not in active_positions and z < -2.1 and obi > 0.4 and price < vwap:
                await client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=trade_amount)
                active_positions[symbol] = {'buy_price': price, 'qty': trade_amount}
                await send_tg_async(f"🚀 {symbol} BUY\nPrice: {price}\nZ: {z:.2f}")

            # EXIT LOGIC
            elif symbol in active_positions:
                buy_p = active_positions[symbol]['buy_price']
                # Exit if Overbought OR 0.25% Profit reached
                if z > 1.9 or price > (buy_p * 1.0025):
                    await client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=trade_amount)
                    profit = (price - buy_p) * trade_amount
                    del active_positions[symbol]
                    await send_tg_async(f"💰 {symbol} SELL\nPrice: {price}\nProfit: {profit:.4f} USDT")
            
            await asyncio.sleep(2)
        except Exception:
            await asyncio.sleep(5)

async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
    await send_tg_async("🌑 APEX MULTI-HFT CORE ACTIVE\nZ-Score + OBI + Floating P&L Tracking Enabled")
    
    # Run trading logic for all symbols AND the portfolio monitor
    tasks = [trade_logic(client, sym) for sym in SYMBOLS]
    tasks.append(monitor_portfolio(client)) 
    
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())



