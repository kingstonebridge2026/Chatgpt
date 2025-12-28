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
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT"]

BASE_USD = 20  # Total USDT per trade
WS_URL = "wss://stream-testnet.bybit.com/v5/public/spot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT")

class AlphaHFT:
    def __init__(self):
        self.exchange = ccxt.bybit({
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"}
        })
        self.exchange.set_sandbox_mode(True) # Force Testnet logic
        
        self.state = {s: {"price_history": deque(maxlen=50), "trade_flow": deque(maxlen=20), "position": None} for s in SYMBOLS}
        self.closed_trades = []

    async def run(self):
        # 1. Load markets to get precision rules
        await self.exchange.load_markets()
        log.info("✅ Markets Loaded. Starting Engine...")

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                await ws.send_json({"op": "subscribe", "args": [f"publicTrade.{s}" for s in SYMBOLS]})
                
                async for msg in ws:
                    raw = json.loads(msg.data)
                    if "data" not in raw: continue

                    for t in raw["data"]:
                        symbol = t['s']
                        price = float(t["p"])
                        s = self.state[symbol]
                        s["price_history"].append(price)
                        s["trade_flow"].append(float(t["v"]) if t["S"] == "Buy" else -float(t["v"]))

                        if len(s["price_history"]) < 5: continue # Ultra-fast start

                        # --- AI SIGNAL ---
                        prices = pd.Series(s["price_history"])
                        rsi = ta.momentum.rsi(prices, len(prices) if len(prices) < 14 else 14).iloc[-1]
                        z = (price - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-9)
                        
                        # LOG EVERYTHING TO SEE WHY IT ISN'T TRADING
                        score = 0
                        if rsi < 50: score += 40 
                        if z < 0: score += 40
                        if sum(s["trade_flow"]) > 0: score += 20
                        
                        # log.info(f"{symbol} | Score: {score} | RSI: {rsi:.1f} | Z: {z:.2f}")

                        symbol_ccxt = f"{symbol[:-4]}/USDT"
                        if not s["position"] and score >= 70:
                            try:
                                # FIX: Calculate QTY with correct exchange precision
                                market = self.exchange.market(symbol_ccxt)
                                raw_qty = BASE_USD / price
                                qty = self.exchange.amount_to_precision(symbol_ccxt, raw_qty)
                                
                                log.info(f"🚀 ATTEMPTING BUY: {symbol_ccxt} Qty: {qty}")
                                order = await self.exchange.create_order(
                                    symbol_ccxt, 'market', 'buy', qty, 
                                    params={'category': 'spot'}
                                )
                                s["position"] = {"entry": price, "amount": qty}
                                log.info(f"✅ SUCCESS: {symbol_ccxt} Filled")
                            except Exception as e:
                                log.error(f"❌ ORDER REJECTED: {e}")

                        elif s["position"]:
                            pnl = (price - s["position"]["entry"]) / s["position"]["entry"]
                            if pnl >= 0.005 or pnl <= -0.003: # 0.5% TP / 0.3% SL
                                try:
                                    await self.exchange.create_order(
                                        symbol_ccxt, 'market', 'sell', s["position"]["amount"],
                                        params={'category': 'spot'}
                                    )
                                    log.info(f"💰 PROFIT TAKEN: {pnl:+.4f}")
                                    s["position"] = None
                                except Exception as e: log.error(f"Exit Fail: {e}")

if __name__ == "__main__":
    asyncio.run(AlphaHFT().run())
