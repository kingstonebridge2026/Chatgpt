
import asyncio, json, aiohttp, logging
import numpy as np
import pandas as pd
import ta
import ccxt.async_support as ccxt
from collections import deque
from telegram import Bot

# ================= CONFIG =================
# Keys provided from your Demo Portal
BINANCE_API_KEY = "ehIwFKbbHe2L8jawoGVTZ0kZ0ZmqmVdOAbavOk8hMtFlm3ljm94n5ChmESiJqslS"
BINANCE_SECRET = "VUSOCq2Ev3QiQVKhxKhigBK8bISTEBWOWESima3J3nAUXWs0G4uxKtqcTLqVWnd5"

TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

# Top 20 Crypto Symbols
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", 
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
    "TRXUSDT", "MATICUSDT", "SHIBUSDT", "TONUSDT", "LTCUSDT",
    "DAIUSDT", "BCHUSDT", "NEARUSDT", "LEOUSDT", "UNIUSDT"
]

BASE_USD = 20  
WS_URL = "wss://demo-stream.binance.com/ws"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT")

class AlphaHFT:
    def __init__(self):
        self.exchange = ccxt.binance({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True
            }
        })
        
        # MANDATORY: Explicit URL redirect for Demo Trading Portal
        self.exchange.urls['api']['public'] = 'https://demo-api.binance.com/api/v3'
        self.exchange.urls['api']['private'] = 'https://demo-api.binance.com/api/v3'
        
        self.tg_bot = Bot(token=TELEGRAM_TOKEN)
        self.state = {s: {"price_history": deque(maxlen=50), "trade_flow": deque(maxlen=20), "position": None} for s in SYMBOLS}

    async def notify(self, message):
        try:
            await self.tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🤖 *Demo Bot:* {message}", parse_mode="Markdown")
        except Exception as e:
            log.error(f"Telegram Notification Error: {e}")

    async def check_balance(self):
        """Validates key permissions and balance."""
        try:
            balance = await self.exchange.fetch_balance()
            usdt = balance.get('free', {}).get('USDT', 0)
            log.info(f"💰 Connection Verified. Wallet: {usdt} USDT")
            return True
        except Exception as e:
            log.error(f"❌ Key Permission Error: {e}")
            return False

    async def run(self):
        # 1. Verify connection before entering loop
        if not await self.check_balance():
            await self.notify("❌ Connection Failed. Check API Key permissions.")
            return

        await self.exchange.load_markets()
        log.info("✅ Engine Online. Scanning Top 20...")

        async with aiohttp.ClientSession() as session:
            # Multi-stream connection
            streams = "/".join([f"{s.lower()}@trade" for s in SYMBOLS])
            async with session.ws_connect(f"{WS_URL}/{streams}") as ws:
                async for msg in ws:
                    raw = json.loads(msg.data)
                    symbol = raw.get('s')
                    if not symbol: continue
                    
                    price = float(raw["p"])
                    vol = float(raw["q"])
                    s = self.state[symbol]
                    
                    s["price_history"].append(price)
                    s["trade_flow"].append(-vol if raw["m"] else vol)

                    if len(s["price_history"]) < 10: continue

                    # --- LOGIC ---
                    prices = pd.Series(s["price_history"])
                    rsi = ta.momentum.rsi(prices, window=min(len(prices), 14)).iloc[-1]
                    z_score = (price - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-9)
                    
                    score = 0
                    if rsi < 45: score += 40 
                    if z_score < -1.5: score += 40
                    if sum(s["trade_flow"]) > 0: score += 20
                    
                    symbol_ccxt = f"{symbol[:-4]}/USDT"

                    # --- EXECUTION ---
                    if not s["position"] and score >= 80:
                        try:
                            qty = self.exchange.amount_to_precision(symbol_ccxt, BASE_USD / price)
                            log.info(f"🚀 BUY {symbol_ccxt} | Price: {price} | Score: {score}")
                            
                            await self.exchange.create_order(symbol_ccxt, 'market', 'buy', qty)
                            s["position"] = {"entry": price, "amount": qty}
                            await self.notify(f"🟢 *BUY* {symbol_ccxt}\nPrice: {price}\nScore: {score}")
                        except Exception as e:
                            log.error(f"❌ Order Failed: {e}")

                    elif s["position"]:
                        pnl = (price - s["position"]["entry"]) / s["position"]["entry"]
                        if pnl >= 0.006 or pnl <= -0.003: # 0.6% Profit / 0.3% Loss
                            try:
                                await self.exchange.create_order(symbol_ccxt, 'market', 'sell', s["position"]["amount"])
                                res = "PROFIT 💰" if pnl > 0 else "LOSS 📉"
                                await self.notify(f"🔴 *SELL* {symbol_ccxt}\nResult: {res}\nPnL: {pnl:+.2%}")
                                s["position"] = None
                            except Exception as e:
                                log.error(f"❌ Exit Error: {e}")

if __name__ == "__main__":
    bot = AlphaHFT()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Stopping...")

