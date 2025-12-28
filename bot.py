import asyncio, json, aiohttp, logging
import numpy as np
import pandas as pd
import ta
import ccxt.async_support as ccxt
from collections import deque
from datetime import datetime

# ================= CONFIG =================
BYBIT_API_KEY = "UYr9b62FtiRiN9hHue"
BYBIT_SECRET = "rUDdj0QM2lQJQjVY1EeoUuPw29LldrNLtzKI"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
BASE_USD = 25
TP, SL = 0.0045, 0.0030

WS_URL = "wss://stream-testnet.bybit.com/v5/public/spot"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("Alpha-HFT")

# ================= AI =================
class MLFilter:
    def predict(self, rsi, z, ofi, trend):
        score = 0
        if rsi < 32: score += 30
        if z < -1.8: score += 30
        if ofi > 0: score += 20
        if trend > 0: score += 20
        return score

# ================= BOT =================
class AlphaHFT:
    def __init__(self):
        self.exchange = ccxt.bybit({
            'apiKey': BYBIT_API_KEY,
            'secret': BYBIT_SECRET,
            'enableRateLimit': True,
            'options': {
                'accountType': 'UNIFIED',
                'defaultType': 'spot',
            }
        })

        # 🚨 DO NOT USE sandbox mode with Bybit V5
        self.exchange.urls['api'] = {
            'public': 'https://api-testnet.bybit.com',
            'private': 'https://api-testnet.bybit.com',
        }

        self.state = {
            s: {
                "price": deque(maxlen=100),
                "position": None,
                "flow": deque(maxlen=50),
                "kalman": 0.0,
                "p": 1.0
            } for s in SYMBOLS
        }

        self.ai = MLFilter()

    async def verify(self):
        bal = await self.exchange.fetch_balance()
        log.info(f"✅ DEMO OK | USDT: {bal['total'].get('USDT', 0)}")

    def kalman(self, sym, z):
        s = self.state[sym]
        s["p"] += 0.0001
        k = s["p"] / (s["p"] + 0.01)
        s["kalman"] += k * (z - s["kalman"])
        s["p"] *= (1 - k)
        return s["kalman"]

    async def run(self):
        await self.verify()

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                await ws.send_json({
                    "op": "subscribe",
                    "args": [f"publicTrade.{s.replace('/', '')}" for s in SYMBOLS]
                })

                async for msg in ws:
                    raw = json.loads(msg.data)
                    if "data" not in raw:
                        continue

                    for t in raw["data"]:
                        sym = f"{t['s'][:-4]}/USDT"
                        if sym not in self.state:
                            continue

                        price = float(t["p"])
                        s = self.state[sym]
                        s["price"].append(price)
                        s["flow"].append(float(t["v"]) if t["S"] == "Buy" else -float(t["v"]))

                        if len(s["price"]) < 30:
                            continue

                        rsi = ta.momentum.rsi(pd.Series(s["price"]), 14).iloc[-1]
                        z = (price - np.mean(s["price"])) / (np.std(s["price"]) + 1e-9)
                        trend = self.kalman(sym, price)

                        if not s["position"] and self.ai.predict(rsi, z, sum(s["flow"]), trend) >= 80:
                            qty = BASE_USD / price
                            await self.exchange.create_order(
                                sym, 'market', 'buy', qty,
                                params={'category': 'spot'}
                            )
                            s["position"] = price
                            log.info(f"BUY {sym}")

                        elif s["position"]:
                            pnl = (price - s["position"]) / s["position"]
                            if pnl >= TP or pnl <= -SL:
                                await self.exchange.create_order(
                                    sym, 'market', 'sell', qty,
                                    params={'category': 'spot'}
                                )
                                s["position"] = None
                                log.info(f"SELL {sym} | PnL {pnl:.4f}")

if __name__ == "__main__":
    asyncio.run(AlphaHFT().run())
