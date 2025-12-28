import asyncio, json, aiohttp, logging
import numpy as np
import pandas as pd
import ta
import ccxt.async_support as ccxt
from collections import deque
from datetime import datetime

# ================= CONFIG (STRICTLY PRESERVED) =================
BYBIT_API_KEY = "UYr9b62FtiRiN9hHue"
BYBIT_SECRET = "rUDdj0QM2lQJQjVY1EeoUuPw29LldrNLtzKI"
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT",
    "POLUSDT","NEARUSDT","LTCUSDT","UNIUSDT","APTUSDT",
    "ARBUSDT","OPUSDT","INJUSDT","TIAUSDT","SUIUSDT"
]

BASE_USD = 25
TP, SL = 0.0045, 0.0030
WS_URL = "wss://stream-testnet.bybit.com/v5/public/spot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT")

# ================= AI BRAIN (UNCHANGED) =================
class MLFilter:
    def predict(self, rsi, z_score, ofi, trend):
        score = 0
        if rsi < 32: score += 30
        if z_score < -1.8: score += 30
        if ofi > 0.05: score += 20
        if trend > 0: score += 20
        return score

# ================= ENGINE =================
class AlphaHFT:
    def __init__(self):
        self.exchange = ccxt.bybit({
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "accountType": "UNIFIED"
            }
        })

        self.exchange.urls["api"] = {
            "public": "https://api-testnet.bybit.com",
            "private": "https://api-testnet.bybit.com",
        }

        self.state = {
            s: {
                "price_history": deque(maxlen=100),
                "trade_flow": deque(maxlen=50),
                "position": None,
                "kalman_x": 0.0,
                "kalman_p": 1.0,
                "current_price": 0.0
            } for s in SYMBOLS
        }

        self.closed_trades = []
        self.tg_id = None
        self.ai = MLFilter()

    async def verify_balance(self):
        bal = await self.exchange.fetch_balance()
        usdt = bal["total"].get("USDT", 0)
        log.info(f"✅ DEMO OK | USDT: {usdt}")

    def kalman_filter(self, symbol, z):
        s = self.state[symbol]
        s["kalman_p"] += 0.0001
        k = s["kalman_p"] / (s["kalman_p"] + 0.01)
        s["kalman_x"] += k * (z - s["kalman_x"])
        s["kalman_p"] *= (1 - k)
        return s["kalman_x"]

    async def telegram_dashboard(self, session):
        while True:
            try:
                total_banked = sum(self.closed_trades)
                total_floating = sum(
                    (d["current_price"] - d["position"]["entry"]) * d["position"]["amount"]
                    for d in self.state.values() if d["position"]
                )

                msg = (
                    f"<b>🤖 AI HFT TOP 20 (BYBIT DEMO)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Active Trades:</b> {len([d for d in self.state.values() if d['position']])}\n"
                    f"<b>Floating P&L:</b> ${total_floating:+.4f}\n"
                    f"<b>Banked PnL:</b> ${total_banked:+.2f}\n"
                    f"<b>⏱</b> {datetime.utcnow().strftime('%H:%M:%S')} UTC"
                )

                if not self.tg_id:
                    r = await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
                    )
                    self.tg_id = (await r.json())["result"]["message_id"]
                else:
                    await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
                        json={"chat_id": TELEGRAM_CHAT_ID, "message_id": self.tg_id, "text": msg, "parse_mode": "HTML"}
                    )
            except:
                pass

            await asyncio.sleep(5)

    async def ws_loop(self, session):
        while True:  # 🔥 CRITICAL FOR RAILWAY
            try:
                async with session.ws_connect(WS_URL, heartbeat=20) as ws:
                    await ws.send_json({
                        "op": "subscribe",
                        "args": [f"publicTrade.{s}" for s in SYMBOLS]
                    })
                    log.info("🚀 Bybit WS Connected")

                    async for msg in ws:
                        raw = json.loads(msg.data)
                        if "data" not in raw:
                            continue

                        for t in raw["data"]:
                            symbol = t["s"]
                            if symbol not in self.state:
                                continue

                            price = float(t["p"])
                            s = self.state[symbol]
                            s["current_price"] = price
                            s["price_history"].append(price)
                            s["trade_flow"].append(
                                float(t["v"]) if t["S"] == "Buy" else -float(t["v"])
                            )

                            if len(s["price_history"]) < 30:
                                continue

                            rsi = ta.momentum.rsi(pd.Series(s["price_history"]), 14).iloc[-1]
                            z = (price - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-9)
                            trend = self.kalman_filter(symbol, price)
                            ofi = sum(s["trade_flow"])

                            symbol_ccxt = f"{symbol[:-4]}/USDT"

                            if not s["position"] and self.ai.predict(rsi, z, ofi, trend) >= 80:
                                qty = BASE_USD / price
                                await self.exchange.create_market_buy_order(symbol_ccxt, qty)
                                s["position"] = {"entry": price, "amount": qty}
                                log.info(f"✅ BUY {symbol_ccxt}")

                            elif s["position"]:
                                pnl = (price - s["position"]["entry"]) / s["position"]["entry"]
                                if pnl >= TP or pnl <= -SL:
                                    await self.exchange.create_market_sell_order(
                                        symbol_ccxt, s["position"]["amount"]
                                    )
                                    self.closed_trades.append(
                                        (price - s["position"]["entry"]) * s["position"]["amount"]
                                    )
                                    s["position"] = None
                                    log.info(f"💰 SELL {symbol_ccxt} | {pnl:.4f}")

            except Exception as e:
                log.error(f"WS Error → reconnecting in 3s: {e}")
                await asyncio.sleep(3)

    async def run(self):
        await self.verify_balance()
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(self.telegram_dashboard(session))
            await self.ws_loop(session)  # 🔥 NEVER RETURNS

if __name__ == "__main__":
    asyncio.run(AlphaHFT().run())
