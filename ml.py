import asyncio, aiohttp, numpy as np, pandas as pd, math
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
    for s in info['symbols']:
        f = {filt['filterType']: filt for filt in s['filters']}
        symbol_filters[s['symbol']] = {
            'step': float(f['LOT_SIZE']['stepSize']),
            'minNotional': float(f.get('MIN_NOTIONAL', f.get('NOTIONAL', {'minNotional': 5}))['minNotional'])
        }
    return [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['symbol'].endswith('USDT')][:100]

def format_quantity(symbol, quantity):
    step = symbol_filters[symbol]['step']
    precision = int(round(-math.log(step, 10), 0))
    qty = math.floor(quantity / step) * step
    return round(qty, precision)

async def get_elite_signals(client, symbol):
    klines = await client.get_klines(symbol=symbol, interval='1m', limit=20)
    df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qav','nt','tbv','tqv','i']).astype(float)
    df['delta'] = df['tbv'] - (df['v'] - df['tbv'])
    
    depth = await client.get_order_book(symbol=symbol, limit=10)
    bid_v = sum([float(b[1]) for b in depth['bids']])
    ask_v = sum([float(a[1]) for a in depth['asks']])
    obi = (bid_v - ask_v) / (bid_v + ask_v)
    
    z_score = (df['c'].iloc[-1] - df['c'].mean()) / (df['c'].std() + 0.00001)
    return z_score, obi, df['delta'].iloc[-1], df['c'].iloc[-1]

async def trade_logic(client, symbol):
    # --- COMPOUNDING SETTINGS ---
    # We split our total balance into 15 "slots"
    # If balance is $100 -> $6.66 per trade. If $200 -> $13.33 per trade.
    total_slots = 15 

    while True:
        try:
            z, obi, delta, price = await get_elite_signals(client, symbol)
            
            if symbol not in active_positions and len(active_positions) < total_slots:
                if z < -1.5 and obi > 0.1 and delta > 0:
                    # DYNAMIC COMPOUNDING CALCULATION
                    acc = await client.get_account()
                    usdt_free = float(next(i['free'] for i in acc['balances'] if i['asset'] == 'USDT'))
                    
                    # Allocate portion of balance to this trade
                    usd_to_spend = (usdt_free / (total_slots - len(active_positions))) * 0.95 
                    
                    if usd_to_spend > symbol_filters[symbol]['minNotional']:
                        qty = format_quantity(symbol, usd_to_spend / price)
                        await client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                        active_positions[symbol] = {'p': price, 'q': qty}
                        await send_tg_async(f"🚀 *COMPOUND ENTRY:* {symbol}\nSize: `${usd_to_spend:.2f}` | Z: `{z:.2f}`")

            elif symbol in active_positions:
                buy_p = active_positions[symbol]['p']
                qty_pos = active_positions[symbol]['q']
                
                # Take Profit (0.2%) OR Stop Loss (0.7% for sleep protection)
                if price > (buy_p * 1.002) or z > 1.3:
                    await client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty_pos)
                    del active_positions[symbol]
                    await send_tg_async(f"💰 *PROFIT HARVEST:* {symbol}")
                
                elif price < (buy_p * 0.993): # Hard Stop Loss
                    await client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=qty_pos)
                    del active_positions[symbol]
                    await send_tg_async(f"⚠️ *STOP LOSS:* {symbol} (Market too volatile)")

            await asyncio.sleep(3)
        except Exception: await asyncio.sleep(5)

async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
    symbols = await setup_filters(client)
    await send_tg_async(f"❄️ *SNOWBALL MODE ACTIVE*\nCompounding enabled across {len(symbols)} pairs.")
    tasks = [trade_logic(client, s) for s in symbols]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
