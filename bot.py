import asyncio, json, aiohttp, logging, time
import numpy as np
import pandas as pd
import ta
import ccxt.async_support as ccxt
from collections import deque
from datetime import datetime

# ================= CONFIG (BYBIT KEYS) =================
BYBIT_API_KEY = "JLcYfu22SuYIzGNuEr"
BYBIT_SECRET = "otU6K2Q8qnqlfunz47Y6kXSmPca7DZQVLfDx"
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"
USER_DEVICE_IP = "176.123.17.227"

SYMBOLS = ["btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "adausdt", "avaxusdt", "dogeusdt", "dotusdt", "linkusdt", "polusdt", "nearusdt", "ltcusdt", "uniusdt", "aptusdt", "arbusdt", "opusdt", "injusdt", "tiausdt", "suiusdt"]
BASE_USD = 25
TP, SL = 0.0045, 0.0030

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
        self.exchange = ccxt.bybit({
            'apiKey': BYBIT_API_KEY,
            'secret': BYBIT_SECRET,
            'enableRateLimit': True,
        })
        self.state = {s: {"price_history": deque(maxlen=100), "position": None, "kalman_x": 0.0, "kalman_p": 1.0, "current_price": 0.0, "trade_flow": deque(maxlen=50)} for s in SYMBOLS}
        self.closed_trades = []
        self.tg_id = None
        self.ai = MLFilter()
        self.ws_url = None

    async def verify_and_set_env(self):
        """Checks Testnet first, then falls back to Mainnet if needed."""
        envs = [
            {"name": "Testnet", "sandbox": True, "ws": "wss://stream-testnet.bybit.com/v5/public/spot"},
            {"name": "Mainnet", "sandbox": False, "ws": "wss://stream.bybit.com/v5/public/spot"}
        ]
        
        for env in envs:
            try:
                self.exchange.set_sandbox_mode(env["sandbox"])
                balance = await self.exchange.fetch_balance()
                usdt = balance['total'].get('USDT', 0)
                self.ws_url = env["ws"]
                log.info(f"✅ BYBIT {env['name'].upper()} ACTIVE: Balance ${usdt} USDT")
                return True
            except Exception:
                continue
        
        log.error("❌ ALL AUTH ATTEMPTS FAILED. Check: 1. Typos 2. IP Whitelist (208.77.244.24)")
        return False

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
                active_list = [s.upper() for s, d in self.state.items() if d["position"]]
                total_floating = sum([(d["current_price"] - d["position"]["entry"]) * d["position"]["amount"] for d in self.state.values() if d["position"]])
                
                msg = (f"<b>🤖 AI HFT TOP 20 (BYBIT)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                       f"<b>Active:</b> {len(active_list)} | <b>PnL:</b> ${total_floating:+.4f}\n"
                       f"<b>Banked:</b> ${total_banked:+.2f}\n━━━━━━━━━━━━━━━━━━━━\n"
                       f"<b>Update:</b> {datetime.utcnow().strftime('%H:%M:%S')} UTC")
                
                if not self.tg_id:
                    async with session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}) as r:
                        res = await r.json()
                        self.tg_id = res.get("result", {}).get("message_id")
                else:
                    await session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json={"chat_id": TELEGRAM_CHAT_ID, "message_id": self.tg_id, "text": msg, "parse_mode": "HTML"})
            except: pass
            await asyncio.sleep(2)

    async def run(self):
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get('https://api.ipify.org') as resp:
                    server_ip = await resp.text()
                    log.info(f"🌐 SERVER IP: {server_ip} | 📱 DEVICE IP: {USER_DEVICE_IP}")
            except: pass

            if not await self.verify_and_set_env():
                await self.exchange.close()
                return
            
            asyncio.create_task(self.telegram_dashboard(session))
            
            try:
                async with session.ws_connect(self.ws_url) as ws:
                    sub_msg = {"op": "subscribe", "args": [f"publicTrade.{s.upper()}" for s in SYMBOLS]}
                    await ws.send_json(sub_msg)
                    log.info(f"🚀 Monitoring Top 20 via Bybit Stream")
                    
                    async for msg in ws:
                        raw = json.loads(msg.data)
                        if "data" not in raw: continue
                        
                        for trade in raw["data"]:
                            symbol = trade["s"].lower()
                            if symbol not in self.state: continue
                            
                            s = self.state[symbol]
                            s["current_price"] = float(trade["p"])
                            s["price_history"].append(s["current_price"])
                            flow = float(trade["v"]) if trade["S"] == "Buy" else -float(trade["v"])
                            s["trade_flow"].append(flow)

                            if len(s["price_history"]) < 30: continue
                            
                            rsi = ta.momentum.rsi(pd.Series(list(s["price_history"])), window=14).iloc[-1]
                            z_score = (s["current_price"] - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-10)
                            trend = self.kalman_filter(symbol, s["current_price"])

                            if not s["position"] and self.ai.predict(rsi, z_score, sum(s["trade_flow"]), trend) >= 80:
                                qty = BASE_USD / s["current_price"]
                                await self.exchange.create_market_buy_order(symbol.upper(), qty)
                                s["position"] = {"entry": s["current_price"], "amount": qty}
                                log.info(f"✅ BUY {symbol.upper()}")

                            elif s["position"]:
                                pnl = (s["current_price"] - s["position"]["entry"]) / s["position"]["entry"]
                                if pnl >= TP or pnl <= -SL:
                                    await self.exchange.create_market_sell_order(symbol.upper(), s["position"]["amount"])
                                    self.closed_trades.append((s["current_price"] - s["position"]["entry"]) * s["position"]["amount"])
                                    s["position"] = None
                                    log.info(f"💰 SELL {symbol.upper()} | PnL: {pnl:.4f}")
            except Exception as e:
                log.error(f"Loop Error: {e}")
            finally:
                await self.exchange.close()

if __name__ == "__main__":
    bot = AlphaHFT()
    try: asyncio.run(bot.run())
    except KeyboardInterrupt: pass
