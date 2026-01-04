
import asyncio, aiohttp, numpy as np, pandas as pd, math, time
from binance import AsyncClient
from binance.enums import *

# --- CREDENTIALS ---
API_KEY = 'Et7oRtg2CLHyaRGBoQOoTFt7LSixfav28k0bnVfcgzxd2KTal4xPlxZ9aO6sr1EJ'
API_SECRET = '2LfotApekUjBH6jScuzj1c47eEnq1ViXsNRIP4ydYqYWl6brLhU3JY4vqlftnUIo'
TG_TOKEN = '8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA'
TG_CHAT_ID = '5665906172'

symbol_filters = {}
active_positions = {} 

async def send_tg_async(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    params = {'chat_id': TG_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp: return await resp.json()
    except: pass

async def setup_filters(client):
    info = await client.get_exchange_info()
    valid_symbols = []
    for s in info['symbols']:
        if not s['symbol'].endswith('USDT') or s['status'] != 'TRADING': continue
        f = {filt['filterType']: filt for filt in s['filters']}
        symbol_filters[s['symbol']] = {
            'step': float(f['LOT_SIZE']['stepSize']),
            'minNotional': float(f.get('NOTIONAL', f.get('MIN_NOTIONAL'))['minNotional'])
        }
        valid_symbols.append(s['symbol'])
    return valid_symbols[:50] # Reduced to 50 for more stability on Testnet

def format_quantity(symbol, quantity):
    step = symbol_filters[symbol]['step']
    precision = int(round(-math.log(step, 10), 0))
    qty = math.floor(quantity / step) * step
    return round(qty, max(precision, 0))

async def get_signals(client, symbol):
    # INCREASED LIMIT TO 30 for more accurate Z-Score
    klines = await client.get_klines(symbol=symbol, interval='1m', limit=30)
    df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qav','nt','tbv','tqv','i']).astype(float)
    
    # Simple Z-Score logic fix
    mean = df['c'].rolling(20).mean().iloc[-1]
    std = df['c'].rolling(20).std().iloc[-1]
    z_score = (df['c'].iloc[-1] - mean) / (std + 0.00000001)
    
    # OBI (Order Book Imbalance)
    depth = await client.get_order_book(symbol=symbol, limit=5)
    bid_v = sum([float(b[1]) for b in depth['bids']])
    ask_v = sum([float(a[1]) for a in depth['asks']])
    obi = (bid_v - ask_v) / (bid_v + ask_v)
    
    return z_score, obi, df['c'].iloc[-1]

async def portfolio_heartbeat(client):
    while True:
        try:
            acc = await client.get_account()
            usdt_free = float(next(i['free'] for i in acc['balances'] if i['asset'] == 'USDT'))
            
            pnl = 0
            if active_positions:
                tickers = await client.get_all_tickers()
                prices = {t['symbol']: float(t['price']) for t in tickers}
                for sym, data in active_positions.items():
                    if sym in prices: pnl += (prices[sym] - data['p']) * data['q']
            
            await send_tg_async(f"🛰 *HEARTBEAT*\n💰 Balance: `{usdt_free:.2f}`\n📦 Slots: `{len(active_positions)}/20` | P&L: `{pnl:+.4f}`")
            await asyncio.sleep(3)
        except: await asyncio.sleep(5)

async def trade_logic(client, symbol):
    while True:
        try:
            z, obi, price = await get_signals(client, symbol)
            
            # --- AGGRESSIVE ENTRY ---
            if symbol not in active_positions and len(active_positions) < 20:
                # LOOSER CRITERIA: Z < -1.0 and positive buy pressure
                if z < -1.0 and obi > 0.01:
                    acc = await client.get_account()
                    bal = float(next(i['free'] for i in acc['balances'] if i['asset'] == 'USDT'))
                    
                    # Snowball: Use 5% of balance per slot
                    trade_usd = (bal / (20 - len(active_positions))) * 0.95
                    
                    if trade_usd > symbol_filters[symbol]['minNotional']:
                        qty = format_quantity(symbol, trade_usd / price)
                        # ACTUAL ORDER
                        await client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                        active_positions[symbol] = {'p': price, 'q': qty}
                        await send_tg_async(f"🚀 *ENTRY:* {symbol}\nPrice: `{price}` | Z: `{z:.2f}`")

            # --- EXIT ---
            elif symbol in active_positions:
                buy_p = active_positions[symbol]['p']
                qty = active_positions[symbol]['q']
                
                # Exit at 0.15% profit or Z-score recovery
                if price > (buy_p * 1.0015) or z > 0.8:
                    await client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty)
                    del active_positions[symbol]
                    await send_tg_async(f"💎 *HARVEST:* {symbol}")

            await asyncio.sleep(1) # HFT speed
        except Exception as e:
            # print(f"Error {symbol}: {e}") # Enable for debugging on Railway
            await asyncio.sleep(2)

async def main():
    # TESTNET RECOVERY
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
    symbols = await setup_filters(client)
    await send_tg_async(f"🌑 *CENTURION ONLINE*\nScanning {len(symbols)} pairs...")
    
    tasks = [trade_logic(client, s) for s in symbols]
    tasks.append(portfolio_heartbeat(client))
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())

