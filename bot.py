import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import ta
import aiohttp
import logging

# ================= CONFIG =================
MODE = "PAPER"  # PAPER | TESTNET | LIVE

BINANCE_API_KEY = "r6hhHQubpwwnDYkYhhdSlk3MQPjTomUggf59gfXJ21hnBcfq3K4BIoSd1eE91V3N"
BINANCE_SECRET = "B7ioAXzVHyYlxPOz3AtxzMC6FQBZaRj6i8A9FenSbsK8rBeCdGZHDhX6Dti22F2x"
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]
TIMEFRAME = "1m"

BASE_USD = 25
MAX_POSITIONS = 3

TP = 0.002   # 0.2%
SL = 0.003   # 0.3%

RSI_BUY = 30

# =========================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("HFT")

positions = []

# ================ TELEGRAM =================
async def telegram(msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        await s.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        })

# ================ EXCHANGE =================
def get_exchange():
    if MODE == "PAPER":
        return None

    ex = ccxt.binance({
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"}
    })

    if MODE == "TESTNET":
        ex.set_sandbox_mode(True)

    return ex

# ================ STRATEGY =================
def scalping_signal(df):
    rsi = ta.momentum.RSIIndicator(df["close"]).rsi().iloc[-1]
    return rsi < RSI_BUY

def should_close(entry, price):
    pnl = (price - entry) / entry
    return pnl >= TP or pnl <= -SL

# ================ MAIN LOOP =================
async def run():
    await telegram("⚡ <b>HFT Scalper Started</b>")
    exchange = get_exchange()
    data_client = ccxt.binance()

    while True:
        for symbol in SYMBOLS:
            try:
                ohlcv = await data_client.fetch_ohlcv(symbol, TIMEFRAME, limit=30)
                df = pd.DataFrame(ohlcv, columns=["t","open","high","low","close","volume"])
                price = df["close"].iloc[-1]

                # ---- BUY ----
                if len(positions) < MAX_POSITIONS and not any(p["symbol"] == symbol for p in positions):
                    if scalping_signal(df):
                        amount = BASE_USD / price
                        if exchange:
                            await exchange.create_market_buy_order(symbol, amount)

                        positions.append({
                            "symbol": symbol,
                            "entry": price,
                            "amount": amount
                        })

                        await telegram(f"⚡ <b>BUY</b> {symbol}\n{price:.4f}")

                # ---- SELL ----
                for p in positions[:]:
                    if p["symbol"] != symbol:
                        continue

                    if should_close(p["entry"], price):
                        if exchange:
                            await exchange.create_market_sell_order(symbol, p["amount"])

                        pnl = (price - p["entry"]) * p["amount"]
                        positions.remove(p)

                        emoji = "💰" if pnl > 0 else "📉"
                        await telegram(f"{emoji} <b>CLOSE</b> {symbol}\nPnL: ${pnl:.2f}")

            except Exception as e:
                log.error(f"{symbol} error: {e}")

        await asyncio.sleep(3)

# ================= START ===================
if __name__ == "__main__":
    asyncio.run(run())
