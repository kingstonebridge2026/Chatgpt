import asyncio
import json
import aiohttp
import logging
import numpy as np
import pandas as pd
import ta
import ccxt.async_support as ccxt
from collections import deque
from telegram import Bot

# ================= CONFIG =================
# Replace with your Binance Demo/Testnet API Keys
BINANCE_API_KEY = "ehIwFKbbHe2L8jawoGVTZ0kZ0ZmqmVdOAbavOk8hMtFlm3ljm94n5ChmESiJqslS"
BINANCE_SECRET = "VUSOCq2Ev3QiQVKhxKhigBK8bISTEBWOWESima3J3nAUXWs0G4uxKtqcTLqVWnd5"

# Telegram Settings
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

# Top 20 Crypto Symbols (Binance WebSocket format)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", 
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
    "TRXUSDT", "MATICUSDT", "SHIBUSDT", "TONUSDT", "LTCUSDT",
    "DAIUSDT", "BCHUSDT", "NEARUSDT", "LEOUSDT", "UNIUSDT"
]

BASE_USD = 20  # USDT allocation per trade
WS_URL = "wss://demo-stream.binance.com/ws"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT")

class AlphaHFT:
    def __init__(self):
        # Initialize Exchange
        self.exchange = ccxt.binance({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"}
        })
        self.exchange.set_sandbox_mode(True) # Force Demo/Testnet logic
        
        # Initialize Telegram
        self.tg_bot = Bot(token=TELEGRAM_TOKEN)
        
        # State Management
        self.state = {
            s: {
                "price_history": deque(maxlen=50), 
                "trade_flow": deque(maxlen=20), 
                "position": None
            } for s in SYMBOLS
        }

    async def notify(self, message):
        """Sends asynchronous notifications to Telegram."""
        try:
            await self.tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🤖 *Alpha-HFT Update:*\n{message}", parse_mode="Markdown")
        except Exception as e:
            log.error(f"Telegram Error: {e}")

    async def run(self):
        # 1. Load markets for precision and limits
        await self.exchange.load_markets()
        log.info("✅ Binance Demo Engine Started.")
        await self.notify("🚀 Engine Online - Monitoring Top 20 Symbols")

        async with aiohttp.ClientSession() as session:
            # Binance combined stream: /ws/symbol1@trade/symbol2@trade
            streams = "/".join([f"{s.lower()}@trade" for s in SYMBOLS])
            full_ws_url = f"{WS_URL}/{streams}"
            
            async with session.ws_connect(full_ws_url) as ws:
                async for msg in ws:
                    raw = json.loads(msg.data)
                    
                    # Binance trade fields: s=symbol, p=price, q=quantity, m=maker(True=Sell/False=Buy)
                    symbol = raw.get('s')
                    if not symbol: continue
                    
                    price = float(raw["p"])
                    vol = float(raw["q"])
                    s = self.state[symbol]
                    
                    s["price_history"].append(price)
                    # Flow logic: Maker=True means the trade was a sell-taker
                    s["trade_flow"].append(-vol if raw["m"] else vol)

                    if len(s["price_history"]) < 10: continue

                    # --- SIGNAL CALCULATION ---
                    prices = pd.Series(s["price_history"])
                    rsi = ta.momentum.rsi(prices, window=min(len(prices), 14)).iloc[-1]
                    z_score = (price - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-9)
                    
                    score = 0
                    if rsi < 45: score += 40      # Oversold
                    if z_score < -1.5: score += 40 # Price drop outlier
                    if sum(s["trade_flow"]) > 0: score += 20 # Buy pressure
                    
                    # Convert to CCXT symbol format (e.g., BTC/USDT)
                    symbol_ccxt = f"{symbol[:-4]}/USDT"

                    # --- EXECUTION LOGIC ---
                    # 1. ENTER POSITION
                    if not s["position"] and score >= 80:
                        try:
                            qty = self.exchange.amount_to_precision(symbol_ccxt, BASE_USD / price)
                            log.info(f"⚡ BUY SIGNAL: {symbol_ccxt} | Score: {score}")
                            
                            order = await self.exchange.create_order(symbol_ccxt, 'market', 'buy', qty)
                            s["position"] = {"entry": price, "amount": qty}
                            
                            await self.notify(f"🟢 *BUY* {symbol_ccxt}\nPrice: {price}\nScore: {score}")
                        except Exception as e:
                            log.error(f"❌ BUY FAILED ({symbol}): {e}")

                    # 2. EXIT POSITION (Take Profit / Stop Loss)
                    elif s["position"]:
                        pnl = (price - s["position"]["entry"]) / s["position"]["entry"]
                        
                        if pnl >= 0.006 or pnl <= -0.003: # 0.6% TP / 0.3% SL
                            try:
                                await self.exchange.create_order(symbol_ccxt, 'market', 'sell', s["position"]["amount"])
                                status = "PROFIT 💰" if pnl > 0 else "LOSS 📉"
                                
                                log.info(f"🏁 EXIT {symbol_ccxt} | {status} | PnL: {pnl:+.2%}")
                                await self.notify(f"🔴 *SELL* {symbol_ccxt}\nResult: {status}\nPnL: {pnl:+.2%}")
                                s["position"] = None
                            except Exception as e:
                                log.error(f"❌ EXIT FAILED ({symbol}): {e}")

if __name__ == "__main__":
    bot = AlphaHFT()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("System Halted.")

