
import asyncio, json, aiohttp, logging
import numpy as np
import pandas as pd
import ta
from collections import deque
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= CONFIG =================
TELEGRAM_TOKEN = "8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA"
TELEGRAM_CHAT_ID = "5665906172"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT",
    "TRXUSDT", "MATICUSDT", "SHIBUSDT", "TONUSDT", "LTCUSDT",
    "DAIUSDT", "BCHUSDT", "NEARUSDT", "LEOUSDT", "UNIUSDT"
]

BASE_USD = 20.0       # Cost per trade
INITIAL_WALLET = 1000.0
WS_URL = "wss://stream.binance.com:9443/stream"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("Alpha-HFT-Sim")

class AlphaHFTSimulator:
    def __init__(self):
        self.wallet = INITIAL_WALLET
        self.trades_count = 0
        self.start_time = pd.Timestamp.now()
        
        # State Management
        self.state = {
            s: {
                "price": 0.0,
                "price_history": deque(maxlen=100),
                "trade_flow": deque(maxlen=30),
                "position": None, # Stores {'entry': price, 'qty': qty}
            } for s in SYMBOLS
        }

    def get_floating_pnl(self):
        """Calculates total P/L of currently open positions."""
        float_pnl = 0.0
        for s, data in self.state.items():
            if data["position"]:
                current_val = data["price"] * data["position"]["qty"]
                entry_val = data["position"]["entry"] * data["position"]["qty"]
                float_pnl += (current_val - entry_val)
        return float_pnl

    async def send_tg(self, message):
        """Standard notification helper."""
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        except Exception as e:
            log.error(f"TG Error: {e}")

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Responds to /status command in Telegram."""
        float_pnl = self.get_floating_pnl()
        open_positions = [s for s, d in self.state.items() if d["position"]]
        
        msg = (
            f"📊 *Live Account Status*\n"
            f"--------------------------\n"
            f"💰 *Wallet:* `${self.wallet:.2f}`\n"
            f"📈 *Floating P/L:* `${float_pnl:+.2f}`\n"
            f"🔥 *Total Equity:* `${self.wallet + float_pnl:.2f}`\n"
            f"📦 *Open Trades:* `{len(open_positions)}`\n"
            f"✅ *Completed Trades:* `{self.trades_count}`\n"
            f"⏱ *Uptime:* `{str(pd.Timestamp.now() - self.start_time).split('.')[0]}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def run_strategy(self):
        """Main engine loop."""
        log.info("🚀 Engine starting with 20 symbols...")
        await self.send_tg("🚀 *Alpha-HFT Simulator Online*\nMonitoring 20 Top Symbols.\nSend `/status` for updates.")

        async with aiohttp.ClientSession() as session:
            streams = "/".join([f"{s.lower()}@trade" for s in SYMBOLS])
            async with session.ws_connect(f"{WS_URL}?streams={streams}") as ws:
                async for msg in ws:
                    raw = json.loads(msg.data)
                    data = raw.get("data")
                    if not data: continue

                    symbol = data["s"]
                    price = float(data["p"])
                    vol = float(data["q"])
                    is_maker = data["m"]

                    s = self.state[symbol]
                    s["price"] = price # Update current price for floating P/L
                    s["price_history"].append(price)
                    s["trade_flow"].append(-vol if is_maker else vol)

                    if len(s["price_history"]) < 20: continue

                    # --- LOGIC ---
                    prices = pd.Series(s["price_history"])
                    rsi = ta.momentum.rsi(prices, window=14).iloc[-1] if len(prices) >= 14 else 50
                    z_score = (price - np.mean(s["price_history"])) / (np.std(s["price_history"]) + 1e-9)
                    
                    score = 0
                    if rsi < 42: score += 40
                    if z_score < -1.8: score += 40
                    if sum(s["trade_flow"]) > 0: score += 20

                    # --- EXECUTION ---
                    # 1. BUY
                    if not s["position"] and score >= 80 and self.wallet >= BASE_USD:
                        qty = BASE_USD / price
                        s["position"] = {"entry": price, "qty": qty}
                        self.wallet -= BASE_USD # Deduct cost
                        log.info(f"🟢 [BUY] {symbol} @ {price}")

                    # 2. SELL (Take Profit / Stop Loss)
                    elif s["position"]:
                        entry = s["position"]["entry"]
                        pnl_pct = (price - entry) / entry
                        
                        if pnl_pct >= 0.008 or pnl_pct <= -0.004:
                            # Calculate exit value
                            exit_value = s["position"]["qty"] * price
                            trade_pnl_usd = exit_value - BASE_USD
                            
                            self.wallet += exit_value # Add proceeds back
                            self.trades_count += 1
                            
                            log.info(f"🔴 [SELL] {symbol} | PnL: {pnl_pct:+.2%}")
                            await self.send_tg(
                                f"🏁 *Trade Closed: {symbol}*\n"
                                f"Result: `{'PROFIT' if trade_pnl_usd > 0 else 'LOSS'}`\n"
                                f"PnL: `{pnl_pct:+.2%}` (${trade_pnl_usd:+.2f})\n"
                                f"Wallet: `${self.wallet:.2f}`"
                            )
                            s["position"] = None

async def main():
    # Initialize Engine
    engine = AlphaHFTSimulator()
    
    # Initialize Telegram Application for Commands
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", engine.handle_status))
    app.add_handler(CommandHandler("balance", engine.handle_status))

    # Run WebSocket and TG Bot concurrently
    await asyncio.gather(
        engine.run_strategy(),
        app.initialize(),
        app.start(),
        app.updater.start_polling()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
