import asyncio, json, aiohttp, logging, time
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

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","LINKUSDT",
    "POLUSDT","NEARUSDT","LTCUSDT","UNIUSDT","APTUSDT",
    "ARBUSDT","OPUSDT","INJUSDT","TIAUSDT","SUIUSDT"
]

BASE_USD = 20
TP, SL = 0.0060, 0.0035
WS_URL = "wss://stream-testnet.bybit.com/v5/public/spot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT")

# ================= AI BRAIN =================
class MLFilter:
    def predict(self, rsi, z, ofi, ema_signal, squeeze):
        score = 0
        if rsi < 35: score += 25
        if z < -1.3: score += 25
        if ofi > 0: score += 20
        if ema_signal > 0: score += 15
        if squeeze: score += 15
        return score

# ================= BOT ENGINE =================
class AlphaHFT:
    def __init__(self):
        self.exchange = ccxt.bybit({
            "apiKey": BYBIT_API_KEY,
            "secret": BYBIT_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "accountType": "UNIFIED"}
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
        try:
            bal = await self.exchange.fetch_balance()
            usdt = bal["total"].get("USDT", 0)
            log.info(f"✅ ACCOUNT READY | USDT: {usdt}")
            return usdt
        except Exception as e: 
            log.error(f"Balance Check Failed: {e}")
            return 0

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
                active = [d for d in self.state.values() if d["position"]]
                total_floating = sum((d["current_price"] - d["position"]["entry"]) * d["position"]["amount"] for d in active)

                msg = (
                    f"<b>🤖 ALPHA HFT ENGINE (BYBIT)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Active Trades:</b> {len(active)} | <b>Banked:</b> ${total_banked:+.2f}\n"
                    f"<b>Floating P&L:</b> ${total_floating:+.4f}\n"
                    f"<b>Target Score:</b> 65+ | <b>Mode:</b> HFT-Aggressive\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏱ {datetime.utcnow().strftime('%H:%M:%S')} UTC"
                )

                if not self.tg_id:
                    r = await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
                    )
                    res = await r.json()
                    if res.get("ok"): self.tg_id = res["result"]["message_id"]
                else:
                    await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
                        json={"chat_id": TELEGRAM_CHAT_ID, "message_id": self.tg_id, "text": msg, "parse_mode": "HTML"}
                    )
            except Exception as e: 
                log.error(f"Telegram Dashboard Error: {e}")
            await asyncio.sleep(10)

    async def ws_loop(self, session):
        while True:
            try:
                async with session.ws_connect(WS_URL, heartbeat=20) as ws:
                    await ws.send_json({"op": "subscribe", "args": [f"publicTrade.{s}" for s in SYMBOLS]})
                    log.info("🚀 Monitoring Ticker Stream...")

                    async for msg in ws:
                        raw = json.loads(msg.data)
                        if "data" not in raw: continue

                        for t in raw["data"]:
                            symbol = t['s']
                            if symbol not in self.state: continue

                            price = float(t["p"])
                            s = self.state[symbol]
                            s["current_price"] = price
                            s["price_history"].append(price)
                            s["trade_flow"].append(float(t["v"]) if t["S"] == "Buy" else -float(t["v"]))

                            if len(s["price_history"]) < 10: continue  # faster demo trades

                            # --- AI INDICATORS ---
                            prices = pd.Series(s["price_history"])
                            rsi = ta.momentum.rsi(prices, 14).iloc[-1]
                            z = (price - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-9)
                            ema9 = ta.trend.ema_indicator(prices, 9).iloc[-1]
                            ema21 = ta.trend.ema_indicator(prices, 21).iloc[-1]
                            ema_signal = 1 if ema9 > ema21 else -1
                            bb_h = ta.volatility.bollinger_hband(prices, 20, 2).iloc[-1]
                            bb_l = ta.volatility.bollinger_lband(prices, 20, 2).iloc[-1]
                            squeeze = (bb_h - bb_l) / price < 0.0018
                            ofi = sum(s["trade_flow"])
                            symbol_ccxt = f"{symbol[:-4]}/USDT"

                            # --- EXECUTION ---
                            if not s["position"]:
                                score = self.ai.predict(rsi, z, ofi, ema_signal, squeeze)
                                if score >= 65:
                                    qty = BASE_USD / price
                                    try:
                                        await self.exchange.create_order(
                                            symbol_ccxt, 'market', 'buy', qty,
                                            params={'wallet': 'unified'}
                                        )
                                        s["position"] = {"entry": price, "amount": qty}
                                        log.info(f"🚀 BUY {symbol_ccxt} | Score: {score}")
                                    except Exception as e:
                                        log.error(f"Buy Fail: {e}")

                            elif s["position"]:
                                pnl = (price - s["position"]["entry"]) / s["position"]["entry"]
                                if pnl >= TP or pnl <= -SL:
                                    try:
                                        await self.exchange.create_order(
                                            symbol_ccxt, 'market', 'sell', s["position"]["amount"],
                                            params={'wallet': 'unified'}
                                        )
                                        self.closed_trades.append((price - s["position"]["entry"]) * s["position"]["amount"])
                                        log.info(f"💰 SELL {symbol_ccxt} | PnL: {pnl:+.4f}")
                                        s["position"] = None
                                    except Exception as e:
                                        log.error(f"Sell Fail: {e}")

            except Exception as e:
                log.error(f"WS Error: {e}")
                await asyncio.sleep(5)

    async def run(self):
        await self.verify_balance()
        async with aiohttp.ClientSession() as session:
            asyncio.create_task(self.telegram_dashboard(session))
            await self.ws_loop(session)

if __name__ == "__main__":
    asyncio.run(AlphaHFT().run())
