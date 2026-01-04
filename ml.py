import asyncio, aiohttp, numpy as np, pandas as pd, math, time
from binance import AsyncClient
from binance.enums import *

# --- CREDENTIALS ---
API_KEY = 'Et7oRtg2CLHyaRGBoQOoTFt7LSixfav28k0bnVfcgzxd2KTal4xPlxZ9aO6sr1EJ'
API_SECRET = '2LfotApekUjBH6jScuzj1c47eEnq1ViXsNRIP4ydYqYWl6brLhU3JY4vqlftnUIo'
TG_TOKEN = '8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA'
TG_CHAT_ID = '5665906172'


symbol_filters = {}
active_positions = {}      # {symbol: list of positions}
last_trade_time = {}       # cooldown tracking

MAX_POSITIONS = 25
COOLDOWN = 90  # seconds per symbol

# ---------------- TELEGRAM ----------------
async def send_tg_async(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        await s.get(url, params={'chat_id': TG_CHAT_ID, 'text': msg})

# ---------------- SETUP ----------------
async def setup_filters(client):
    info = await client.get_exchange_info()
    symbols = []

    for s in info['symbols']:
        if s['status'] != 'TRADING': continue
        if not s['symbol'].endswith('USDT'): continue

        f = {x['filterType']: x for x in s['filters']}
        if 'LOT_SIZE' not in f or 'NOTIONAL' not in f: continue

        symbol_filters[s['symbol']] = {
            'step': float(f['LOT_SIZE']['stepSize']),
            'minNotional': float(f['NOTIONAL']['minNotional'])
        }
        symbols.append(s['symbol'])

    return symbols[:40]  # smaller = faster & safer

# ---------------- UTILS ----------------
def format_quantity(symbol, qty):
    step = symbol_filters[symbol]['step']
    precision = int(round(-math.log(step, 10)))
    return round(math.floor(qty / step) * step, precision)

# ---------------- SIGNALS ----------------
async def get_elite_signals(client, symbol):
    kl = await client.get_klines(symbol=symbol, interval='1m', limit=15)
    df = pd.DataFrame(kl, columns=list(range(12))).astype(float)

    z = (df[4].iloc[-1] - df[4].mean()) / (df[4].std() + 1e-6)

    depth = await client.get_order_book(symbol=symbol, limit=5)
    bid = sum(float(b[1]) for b in depth['bids'])
    ask = sum(float(a[1]) for a in depth['asks'])
    obi = (bid - ask) / (bid + ask + 1e-6)

    return z, obi, df[4].iloc[-1]

# ---------------- TRADING ----------------
async def trade_logic(client, symbol):
    active_positions.setdefault(symbol, [])

    while True:
        try:
            z, obi, price = await get_elite_signals(client, symbol)

            now = time.time()
            if now - last_trade_time.get(symbol, 0) < COOLDOWN:
                await asyncio.sleep(1)
                continue

            if len(active_positions) < MAX_POSITIONS:
                if z < -1.0 and obi > 0.02:

                    acc = await client.get_account()
                    usdt = float(next(b['free'] for b in acc['balances'] if b['asset']=='USDT'))

                    usd = (usdt / (MAX_POSITIONS - len(active_positions))) * 0.95
                    if usd < symbol_filters[symbol]['minNotional']:
                        await asyncio.sleep(1)
                        continue

                    qty = format_quantity(symbol, usd / price)
                    await client.create_order(
                        symbol=symbol,
                        side=SIDE_BUY,
                        type=ORDER_TYPE_MARKET,
                        quantity=qty
                    )

                    active_positions[symbol].append({'p': price, 'q': qty})
                    last_trade_time[symbol] = now
                    await send_tg_async(f"🚀 BUY {symbol} @ {price}")

            # ---- EXIT LOGIC ----
            for pos in active_positions[symbol][:]:
                if price > pos['p'] * 1.002 or z > 1.1:
                    await client.create_order(
                        symbol=symbol,
                        side=SIDE_SELL,
                        type=ORDER_TYPE_MARKET,
                        quantity=pos['q']
                    )
                    active_positions[symbol].remove(pos)
                    await send_tg_async(f"💎 SELL {symbol}")

            await asyncio.sleep(1)

        except Exception as e:
            await asyncio.sleep(2)

# ---------------- MAIN ----------------
async def main():
    client = await AsyncClient.create(
        API_KEY,
        API_SECRET,
        testnet=True
    )

    symbols = await setup_filters(client)
    await send_tg_async(f"🔥 LIVE — {len(symbols)} symbols")

    tasks = [trade_logic(client, s) for s in symbols]
    await asyncio.gather(*tasks)

asyncio.run(main())

