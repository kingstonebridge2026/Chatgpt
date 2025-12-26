import asyncio
import json
import aiohttp
import numpy as np
import pandas as pd
import logging
from collections import deque
from datetime import datetime

# ================= CONFIG =================
MODE = "PAPER"  # PAPER | TESTNET | LIVE
SYMBOL = "btcusdt"
BASE_USD = 25
TP = 0.0018
SL = 0.0025
RSI_MIN = 30
RSI_MAX = 70

BINANCE_API_KEY = "r6hhHQubpwwnDYkYhhdSlk3MQPjTomUggf59gfXJ21hnBcfq3K4BIoSd1eE91V3N"
BINANCE_SECRET = "B7ioAXzVHyYlxPOz3AtxzMC6FQBZaRj6i8A9FenSbsK8rBeCdGZHDhX6Dti22F2x"
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

WS_URL = f"wss://stream.binance.com:9443/ws/{SYMBOL}@kline_1m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("HFT-WS")

# ================= STATE =================
prices_window = deque(maxlen=50)
returns_window = deque(maxlen=50)
position = None
closed_trades = []
telegram_msg_id = None

# ================= KALMAN FILTER =================
class Kalman:
    def __init__(self):
        self.x = 0.0
        self.p = 1.0
        self.q = 0.0001
        self.r = 0.01
    def update(self, z):
        self.p += self.q
        k = self.p / (self.p + self.r)
        self.x += k * (z - self.x)
        self.p *= (1 - k)
        return self.x

kalman = Kalman()

# ================= EWMA =================
def ewma(values, alpha=0.2):
    s = values[0]
    for v in values[1:]:
        s = alpha * v + (1 - alpha) * s
    return s

# ================= ONLINE LINEAR REG =================
def online_regression(y):
    x = np.arange(len(y))
    A = np.vstack([x, np.ones(len(x))]).T
    m, _ = np.linalg.lstsq(A, y, rcond=None)[0]
    return m

# ================= ORDER FLOW IMBALANCE =================
def order_flow_imbalance(df):
    buy_vol = df[df["c"] > df["o"]]["v"].sum()
    sell_vol = df[df["c"] < df["o"]]["v"].sum()
    if buy_vol + sell_vol == 0:
        return 0
    return (buy_vol - sell_vol) / (buy_vol + sell_vol)

# ================= TELEGRAM =================
async def telegram_edit(msg):
    global telegram_msg_id
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    async with aiohttp.ClientSession() as session:
        if telegram_msg_id is None:
            r = await session.post(f"{url}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            })
            telegram_msg_id = (await r.json())["result"]["message_id"]
        else:
            await session.post(f"{url}/editMessageText", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": telegram_msg_id,
                "text": msg,
                "parse_mode": "HTML"
            })

# ================= STRATEGY DECISION =================
def should_buy(price, rsi, z, trend, reg_slope, ofi):
    score = 0
    score += 1 if z < -1 else 0
    score += 1 if trend > 0 else 0
    score += 1 if reg_slope > 0 else 0
    score += 1 if ofi > 0.1 else 0
    score += 1 if RSI_MIN < rsi < RSI_MAX else 0
    return score >= 4

def should_close(entry, price):
    pnl = (price - entry) / entry
    return pnl >= TP or pnl <= -SL

# ================= DASHBOARD =================
def render_dashboard(price):
    floating = 0
    if position:
        floating = (price - position["entry"]) * position["amount"]
    return f"""
<b>⚡ HFT AI WebSocket</b>
<b>Price:</b> {price:.2f}
<b>Position:</b> {"OPEN" if position else "NONE"}
<b>Floating PnL:</b> {floating:.2f} $

<b>Closed Trades:</b> {len(closed_trades)}
<b>Total PnL:</b> {sum(t["pnl"] for t in closed_trades):.2f} $

<b>Status:</b> LIVE
<b>Time:</b> {datetime.utcnow().strftime("%H:%M:%S")}
"""

# ================= WEBSOCKET LOOP =================
async def ws_loop():
    global position
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            async for msg in ws:
                data = json.loads(msg.data)
                candle = data["k"]
                if not candle["x"]:
                    continue
                price = float(candle["c"])
                prices_window.append(price)
                if len(prices_window) < 20:
                    continue
                prev = prices_window[-2]
                returns_window.append(price - prev)
                rsi = np.mean([p/price*100 for p in returns_window])  # proxy RSI
                z = (price - np.mean(prices_window)) / (np.std(prices_window)+1e-8)
                trend = kalman.update(price - prev)
                reg_slope = online_regression(list(prices_window))
                ofi = order_flow_imbalance(pd.DataFrame([{"c":c,"o":c*0.999,"v":1} for c in prices_window]))
                # ---- BUY ----
                if position is None and should_buy(price, rsi, z, trend, reg_slope, ofi):
                    amount = BASE_USD / price
                    position = {"entry": price, "amount": amount}
                # ---- SELL ----
                if position and should_close(position["entry"], price):
                    pnl = (price - position["entry"]) * position["amount"]
                    closed_trades.append({"pnl": pnl})
                    position = None
                await telegram_edit(render_dashboard(price))

# ================= MAIN =================
async def main():
    await telegram_edit("🚀 HFT AI WebSocket Starting...")
    await ws_loop()

if __name__ == "__main__":
    asyncio.run(main())
