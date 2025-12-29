import asyncio, json, aiohttp, logging, numpy as np, pandas as pd, ta
from collections import deque
from river import linear_model, preprocessing, compose, metrics
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= CONFIG =================
TELEGRAM_TOKEN = "8488789199:AAHhViKmhXlvE7WpgZGVDS4WjCjUuBVtqzQ"
TELEGRAM_CHAT_ID = "5665906172"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"] # Start with top 5 for ML stability
BASE_USD = 10.0
WS_URL = "wss://stream.binance.com:9443/stream"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-ML-Bot")

class MLTrader:
    def __init__(self):
        self.wallet = 1000.0
        # Pipeline: Scaler + Logistic Regression (Online Learning)
        self.model = compose.Pipeline(
            preprocessing.StandardScaler(),
            linear_model.LogisticRegression()
        )
        self.metric = metrics.Accuracy()
        
        self.state = {s: {
            "price_history": deque(maxlen=200),
            "flow": deque(maxlen=50),
            "position": None,
            "last_features": None
        } for s in SYMBOLS}

    def extract_features(self, symbol):
        s = self.state[symbol]
        prices = pd.Series(s["price_history"])
        if len(prices) < 30: return None
        
        # ML Features
        features = {
            "rsi": ta.momentum.rsi(prices, window=14).iloc[-1],
            "zscore": (prices.iloc[-1] - prices.mean()) / (prices.std() + 1e-9),
            "mfi": ta.volume.money_flow_index(prices, prices, prices, pd.Series([1]*len(prices)), window=14).iloc[-1],
            "volatility": prices.pct_change().std(),
            "flow_imbalance": sum(s["flow"])
        }
        return features

    async def run_engine(self):
        log.info("🤖 ML Engine starting...")
        async with aiohttp.ClientSession() as session:
            streams = "/".join([f"{s.lower()}@trade" for s in SYMBOLS])
            async with session.ws_connect(f"{WS_URL}?streams={streams}") as ws:
                async for msg in ws:
                    data = json.loads(msg.data).get("data")
                    if not data: continue
                    
                    symbol, price = data["s"], float(data["p"])
                    s = self.state[symbol]
                    s["price_history"].append(price)
                    s["flow"].append(-float(data["q"]) if data["m"] else float(data["q"]))

                    features = self.extract_features(symbol)
                    if not features: continue

                    # --- ML PREDICTION ---
                    # Model gives probability of "Price goes up"
                    prob_up = self.model.predict_proba_one(features).get(True, 0.5)

                    # --- EXECUTION ---
                    if not s["position"] and prob_up > 0.75:
                        s["position"] = {"entry": price, "features": features, "qty": BASE_USD/price}
                        self.wallet -= BASE_USD
                        log.info(f"📈 [ML BUY] {symbol} | Confidence: {prob_up:.2%}")

                    elif s["position"]:
                        pnl = (price - s["position"]["entry"]) / s["position"]["entry"]
                        
                        # Exit Logic
                        if pnl >= 0.01 or pnl <= -0.005:
                            is_win = pnl > 0
                            # --- ONLINE LEARNING (The "Super Accurate" Part) ---
                            # Tell the model: "With these features, the result was a Win (True) or Loss (False)"
                            self.model.learn_one(s["position"]["features"], is_win)
                            self.metric.update(is_win, True)
                            
                            self.wallet += (s["position"]["qty"] * price)
                            log.info(f"📉 [ML SELL] {symbol} | PnL: {pnl:+.2%} | Acc: {self.metric.get():.2%}")
                            s["position"] = None

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"💰 Wallet: ${self.wallet:.2f}\n🎯 ML Accuracy: {self.metric.get():.2%}")

async def main():
    bot = MLTrader()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", bot.status))
    
    async with app:
        await app.start()
        await app.updater.start_polling()
        await bot.run_engine()

if __name__ == "__main__":
    asyncio.run(main())
