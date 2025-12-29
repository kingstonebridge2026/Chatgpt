import asyncio, json, aiohttp, logging, requests
import numpy as np, pandas as pd, ta
from collections import deque
from river import linear_model, preprocessing, compose, metrics
from nltk.sentiment.vader import SentimentIntensityAnalyzer
import nltk

# Download the sentiment lexicon (only needs to run once)
nltk.download('vader_lexicon', quiet=True)

# ================= CONFIG =================
CRYPTOPANIC_API_KEY = "936ee60c210fd21b853971b458bfdf6ef2515eb3"
TELEGRAM_TOKEN = "8488789199:AAHhViKmhXlvE7WpgZGVDS4WjCjUuBVtqzQ"
TELEGRAM_CHAT_ID = "5665906172"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BASE_USD = 10.0

class MLSentimentTrader:
    def __init__(self):
        self.wallet = 1000.0
        self.sia = SentimentIntensityAnalyzer()
        self.current_sentiment = 0.0 # -1.0 to +1.0
        
        # ML Pipeline: Now includes Sentiment as a feature
        self.model = compose.Pipeline(
            preprocessing.StandardScaler(),
            linear_model.LogisticRegression()
        )
        self.metric = metrics.Accuracy()
        self.state = {s: {"price_history": deque(maxlen=100), "position": None} for s in SYMBOLS}
    async def update_global_sentiment(self):
        """Fetches news and updates sentiment using the correct CryptoPanic format."""
        # Use the base posts endpoint as per docs
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {
            "auth_token": CRYPTOPANIC_API_KEY,
            "public": "true",
            "kind": "news" # Filter for actual news articles
        }
        
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    # Passing params as a dict handles encoding correctly
                    async with session.get(url, params=params) as response:
                        if response.status == 200:
                            # content_type=None avoids the mimetype error
                            data = await response.json(content_type=None)
                            results = data.get('results', [])
                            
                            if results:
                                titles = [post['title'] for post in results[:10]]
                                scores = [self.sia.polarity_scores(t)['compound'] for t in titles]
                                self.current_sentiment = np.mean(scores)
                                log.info(f"📰 Sentiment Analysis Complete: {self.current_sentiment:+.2f}")
                        else:
                            log.warning(f"⚠️ CryptoPanic API Status {response.status}. Check API Key.")
                            
            except Exception as e:
                log.error(f"🌐 News Module connectivity issue: {e}")
            
            # Update every 10 minutes to stay safely within free-tier limits
            await asyncio.sleep(600)


    def get_features(self, symbol):
        s = self.state[symbol]
        prices = pd.Series(s["price_history"])
        if len(prices) < 30: return None
        
        return {
            "rsi": ta.momentum.rsi(prices).iloc[-1],
            "zscore": (prices.iloc[-1] - prices.mean()) / (prices.std() + 1e-9),
            "news_mood": self.current_sentiment # <--- The NEW ML Feature
        }

    async def run_strategy(self):
        async with aiohttp.ClientSession() as session:
            streams = "/".join([f"{s.lower()}@trade" for s in SYMBOLS])
            async with session.ws_connect(f"wss://stream.binance.com:9443/stream?streams={streams}") as ws:
                async for msg in ws:
                    data = json.loads(msg.data).get("data")
                    if not data: continue
                    
                    symbol, price = data["s"], float(data["p"])
                    s = self.state[symbol]
                    s["price_history"].append(price)

                    features = self.get_features(symbol)
                    if not features: continue

                    # ML Probability: Is it a good time to buy?
                    prob_buy = self.model.predict_proba_one(features).get(True, 0.5)

                    # LOGIC: High Confidence (>75%) AND News isn't crashing (Sentiment > -0.2)
                    if not s["position"] and prob_buy > 0.75 and self.current_sentiment > -0.2:
                        s["position"] = {"entry": price, "qty": BASE_USD/price, "f": features}
                        self.wallet -= BASE_USD
                        logging.info(f"✅ [BUY] {symbol} | ML: {prob_buy:.1%} | News: {self.current_sentiment:+.2f}")

                    elif s["position"]:
                        pnl = (price - s["position"]["entry"]) / s["position"]["entry"]
                        if pnl >= 0.01 or pnl <= -0.005:
                            is_win = pnl > 0
                            self.model.learn_one(s["position"]["f"], is_win) # Learn from this trade
                            self.wallet += (s["position"]["qty"] * price)
                            s["position"] = None

async def main():
    bot = MLSentimentTrader()
    # Run news fetcher and trading engine together
    await asyncio.gather(
        bot.update_global_sentiment(),
        bot.run_strategy()
    )

if __name__ == "__main__":
    asyncio.run(main())

