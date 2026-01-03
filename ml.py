
import asyncio, requests, numpy as np, pandas as pd
from binance import AsyncClient, BinanceSocketManager
from binance.enums import *

# --- CREDENTIALS ---
API_KEY = 'Et7oRtg2CLHyaRGBoQOoTFt7LSixfav28k0bnVfcgzxd2KTal4xPlxZ9aO6sr1EJ'
API_SECRET = '2LfotApekUjBH6jScuzj1c47eEnq1ViXsNRIP4ydYqYWl6brLhU3JY4vqlftnUIo'
TG_TOKEN = '8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA'
TG_CHAT_ID = '5665906172'

# Top 20 symbols for 2026 setup
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 
    'ADAUSDT', 'DOGEUSDT', 'TRXUSDT', 'LINKUSDT', 'AVAXUSDT',
    'DOTUSDT', 'LTCUSDT', 'BCHUSDT', 'SUIUSDT', 'NEARUSDT',
    'SHIBUSDT', 'PEPEUSDT', 'XMRUSDT', 'STXUSDT', 'UNIUSDT'
]

async def send_tg_async(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage?chat_id={TG_CHAT_ID}&text={msg}"
    try: requests.get(url) # Using requests here for simplicity in TG
    except: pass

async def get_signals(client, symbol):
    # Fetching 1m candles for technical depth
    klines = await client.get_klines(symbol=symbol, interval='1m', limit=30)
    df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qa','nt','tb','tq','i']).astype(float)
    
    # VWAP & Z-Score
    df['tp'] = (df['h'] + df['l'] + df['c']) / 3
    vwap = (df['tp'] * df['v']).sum() / (df['v'].sum() + 0.0001)
    z_score = (df['c'].iloc[-1] - df['c'].mean()) / (df['c'].std() + 0.00001)
    
    # Order Book Imbalance (OBI)
    depth = await client.get_order_book(symbol=symbol, limit=10)
    bid_v = sum([float(b[1]) for b in depth['bids']])
    ask_v = sum([float(a[1]) for a in depth['asks']])
    obi = (bid_v - ask_v) / (bid_v + ask_v)
    
    return z_score, obi, vwap, df['c'].iloc[-1]

async def trade_logic(client, symbol):
    in_pos = False
    buy_price = 0
    trade_amount = 0.00015 # Adjusted per coin
    
    print(f"📡 Monitoring {symbol}...")
    
    while True:
        try:
            z, obi, vwap, price = await get_signals(client, symbol)
            
            # ENTRY: Oversold (Z < -2.1) + Buy Pressure (OBI > 0.4) + Below VWAP
            if not in_pos and z < -2.1 and obi > 0.4 and price < vwap:
                # Note: On Testnet, ensure you have sufficient USDT
                await client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=trade_amount)
                buy_price = price
                in_pos = True
                await send_tg_async(f"🚀 {symbol} BUY\nPrice: {price}\nOBI: {obi:.2f}\nVWAP: Under")

            # EXIT: Overbought (Z > 1.9) OR 0.25% Scalp Profit
            elif in_pos and (z > 1.9 or price > (buy_price * 1.0025)):
                await client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=trade_amount)
                in_pos = False
                await send_tg_async(f"💰 {symbol} PROFIT\nPrice: {price}\nTarget: Scalped ✅")
            
            await asyncio.sleep(2) # 2s Poll for each coin
            
        except Exception as e:
            # print(f"Error on {symbol}: {e}")
            await asyncio.sleep(5)

async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
    await send_tg_async("🌑 APEX MULTI-HFT CORE ACTIVE\nMonitoring 20 Symbols Simultaneously")
    
    # Create a task for every symbol in our list
    tasks = [trade_logic(client, sym) for sym in SYMBOLS]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())


