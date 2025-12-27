import asyncio, json, aiohttp, logging, time
import numpy as np
import pandas as pd
import ta
import ccxt.async_support as ccxt
from collections import deque
from datetime import datetime

# ================= CONFIG (STRICTLY PRESERVED) =================
BINANCE_API_KEY = "r6hhHQubpwwnDYkYhhdSlk3MQPjTomUggf59gfXJ21hnBcfq3K4BIoSd1eE91V3N"
BINANCE_SECRET = "B7ioAXzVHyYlxPOz3AtxzMC6FQBZaRj6i8A9FenSbsK8rBeCdGZHDhX6Dti22F2x"
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

SYMBOLS = ["btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "adausdt", "avaxusdt", "dogeusdt", "dotusdt", "linkusdt", "polusdt", "nearusdt", "ltcusdt", "uniusdt", "aptusdt", "arbusdt", "opusdt", "injusdt", "tiausdt", "suiusdt"]
BASE_USD = 25
TP, SL = 0.0045, 0.0030

# UPDATED 2025 DEMO WEBSOCKET URL
WS_URL = "wss://demo-stream.binance.com/stream?streams=" + "/".join([f"{s}@aggTrade" for s in SYMBOLS])

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT")

class MLFilter:
    def predict(self, rsi, z_score, ofi, trend):
        score = 0
        if rsi < 32: score += 30
        if z_score < -1.8: score += 30
        if ofi > 0.05: score += 20
        if trend > 0: score += 20
        return score

class AlphaHFT:
    def __init__(self):
        # UPDATED 2025 DEMO API SETTINGS
        self.exchange = ccxt.binance({
            'apiKey': BINANCE_API_KEY,
            'secret': BINANCE_SECRET,
            'enableRateLimit': True,
            'urls': {
                'api': {'public': 'https://demo-api.binance.com/api/v3', 'private': 'https://demo-api.binance.com/api/v3'}
            }
        })
        self.exchange.set_sandbox_mode(True) 

        self.state = {s: {"price_history": deque(maxlen=100), "position": None, "kalman_x": 0.0, "kalman_p": 1.0, "current_price": 0.0, "trade_flow": deque(maxlen=50)} for s in SYMBOLS}
        self.closed_trades = []
        self.tg_id = None
        self.ai = MLFilter()

    async def verify_demo_account(self):
        """Check if Demo Keys are actually working"""
        try:
            balance = await self.exchange.fetch_balance()
            usdt = balance['total'].get('USDT', 0)
            log.info(f"✅ DEMO ACTIVE: Your Wallet has ${usdt} USDT")
            return True
        except Exception as e:
            log.error(f"❌ AUTH FAILED: Check your Testnet Keys. Error: {e}")
            return False

    def kalman_filter(self, symbol, z):
        s = self.state[symbol]
        s["kalman_p"] += 0.0001
        k = s["kalman_p"] / (s["kalman_p"] + 0.01)
        s["kalman_x"] += k * (z - s["kalman_x"])
        s["kalman_p"] *= (1 - k)
        return s["kalman_x"]

    # [Telegram Dashboard Logic Preserved]
    async def telegram_dashboard(self, session):
        while True:
            try:
                total_banked = sum(self.closed_trades)
                active_list = [s.upper() for s, d in self.state.items() if d["position"]]
                total_floating = sum([(d["current_price"] - d["position"]["entry"]) * d["position"]["amount"] for d in self.state.values() if d["position"]])

                msg = (f"<b>🤖 AI HFT TOP 20 (DEMO)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                       f"<b>Active:</b> {len(active_list)} | <b>Assets:</b> {', '.join(active_list) if active_list else 'None'}\n"
                       f"<b>Floating P&L:</b> ${total_floating:+.4f}\n<b>Banked PnL:</b> ${total_banked:+.2f}\n"
                       f"━━━━━━━━━━━━━━━━━━━━\n<b>Update:</b> {datetime.utcnow().strftime('%H:%M:%S')} UTC")
                
                if not self.tg_id:
                    async with session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}) as r:
                        self.tg_id = (await r.json())["result"]["message_id"]
                else:
                    await session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json={"chat_id": TELEGRAM_CHAT_ID, "message_id": self.tg_id, "text": msg, "parse_mode": "HTML"})
            except: pass
            await asyncio.sleep(2)

    async def run(self):
        if not await self.verify_demo_account(): return
        
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(self.telegram_dashboard(session))
            async with session.ws_connect(WS_URL) as ws:
                log.info("🚀 Monitoring Top 20 via 2025 Demo Stream")
                async for msg in ws:
                    try:
                        raw = json.loads(msg.data)
                        data, symbol = raw['data'], raw['stream'].split('@')[0]
                        s = self.state[symbol]
                        s["current_price"] = float(data['p'])
                        s["price_history"].append(s["current_price"])
                        
                        if len(s["price_history"]) < 30: continue
                        
                        rsi = ta.momentum.rsi(pd.Series(list(s["price_history"])), window=14).iloc[-1]
                        z_score = (s["current_price"] - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-10)
                        trend = self.kalman_filter(symbol, s["current_price"])

                        if not s["position"] and self.ai.predict(rsi, z_score, 0, trend) >= 80:
                            qty = BASE_USD / s["current_price"]
                            await self.exchange.create_market_buy_order(symbol.upper().replace("USDT", "/USDT"), qty)
                            s["position"] = {"entry": s["current_price"], "amount": qty}
                            log.info(f"BUY {symbol.upper()}")

                        elif s["position"]:
                            pnl = (s["current_price"] - s["position"]["entry"]) / s["position"]["entry"]
                            if pnl >= TP or pnl <= -SL:
                                await self.exchange.create_market_sell_order(symbol.upper().replace("USDT", "/USDT"), s["position"]["amount"])
                                self.closed_trades.append((s["current_price"] - s["position"]["entry"]) * s["position"]["amount"])
                                s["position"] = None
                    except Exception as e: log.error(f"Loop Error: {e}")

if __name__ == "__main__":
    bot = AlphaHFT()
    asyncio.run(bot.run())
