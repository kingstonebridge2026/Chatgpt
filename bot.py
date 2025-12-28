import asyncio
import json
import aiohttp
import logging
import numpy as np
import pandas as pd
import ta
import ccxt.async_support as ccxt
from collections import deque

# ================= CONFIG =================
BYBIT_API_KEY = "UYr9b62FtiRiN9hHue"
BYBIT_SECRET = "
rUDdj0QM2lQJQjVY1EeoUuPw29LldrNLtzKI"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT"
]

BASE_USD = 20
WS_URL = "wss://stream-testnet.bybit.com/v5/public/spot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT")


class AlphaHFT:
    def __init__(self):
        self.exchange = ccxt.bybit({
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "testnet": True
            }
        })

        self.state = {
            s: {
                "price_history": deque(maxlen=50),
                "trade_flow": deque(maxlen=20),
                "position": None
            } for s in SYMBOLS
        }

    async def run(self):
        await self.exchange.load_markets()
        log.info("✅ Markets Loaded")

        # --- CHECK SPOT BALANCE ---
        balance = await self.exchange.fetch_balance()
        usdt = balance.get("USDT", {}).get("free", 0)
        log.info(f"💰 USDT Spot Balance: {usdt}")

        if usdt < BASE_USD:
            log.error("❌ Not enough USDT in SPOT wallet")
            return

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                await ws.send_json({
                    "op": "subscribe",
                    "args": [f"publicTrade.{s}" for s in SYMBOLS]
                })

                async for msg in ws:
                    data = json.loads(msg.data)
                    if "data" not in data:
                        continue

                    for t in data["data"]:
                        symbol = t["s"]
                        price = float(t["p"])
                        s = self.state[symbol]

                        s["price_history"].append(price)
                        vol = float(t["v"])
                        s["trade_flow"].append(vol if t["S"] == "Buy" else -vol)

                        if len(s["price_history"]) < 10:
                            continue

                        prices = pd.Series(s["price_history"])
                        rsi = ta.momentum.rsi(prices, 14).iloc[-1]
                        z = (price - prices.mean()) / (prices.std() + 1e-9)

                        score = 0
                        if rsi < 45:
                            score += 40
                        if z < -0.5:
                            score += 40
                        if sum(s["trade_flow"]) > 0:
                            score += 20

                        symbol_ccxt = f"{symbol[:-4]}/USDT"

                        # ================= BUY =================
                        if not s["position"] and score >= 70:
                            try:
                                raw_qty = BASE_USD / price
                                qty = float(
                                    self.exchange.amount_to_precision(symbol_ccxt, raw_qty)
                                )

                                if qty <= 0:
                                    continue

                                log.info(f"🚀 BUY {symbol_ccxt} | Qty: {qty}")

                                await self.exchange.create_market_buy_order(
                                    symbol_ccxt,
                                    qty,
                                    params={"category": "spot"}
                                )

                                s["position"] = {
                                    "entry": price,
                                    "amount": qty
                                }

                            except Exception as e:
                                log.error(f"❌ BUY FAIL {symbol_ccxt}: {e}")

                        # ================= SELL =================
                        elif s["position"]:
                            entry = s["position"]["entry"]
                            pnl = (price - entry) / entry

                            if pnl >= 0.005 or pnl <= -0.003:
                                try:
                                    await self.exchange.create_market_sell_order(
                                        symbol_ccxt,
                                        s["position"]["amount"],
                                        params={"category": "spot"}
                                    )

                                    log.info(f"💰 EXIT {symbol_ccxt} | PnL: {pnl:.4f}")
                                    s["position"] = None

                                except Exception as e:
                                    log.error(f"❌ SELL FAIL {symbol_ccxt}: {e}")


if __name__ == "__main__":
    asyncio.run(AlphaHFT().run())
