import asyncio
import ccxt.pro as ccxt
import numpy as np
from telegram import Bot

# --- CONFIG ---
API_KEY = 'p4FKZRHA26Z3VWyRPF5xZFc3aDU8vxTy7OCULtX7wCgN6l3EaNl882q4JzruPIsE'
SECRET_KEY = 'CXqJa5JaKxwVPT5ik3EBbB2Mm0IMp4J0I0OSEY1Q6SKk7PkWHwf7tyDBhxKwLn1b'
TELEGRAM_TOKEN = '8560134874:AAHF4efOAdsg2Y01eBHF-2DzEUNf9WAdniA'
CHAT_ID = '8560134874'
SYMBOL = 'BTC/USDT'
LEVERAGE = 10

# Initialize
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'
    }
})
tg_bot = Bot(token=TELEGRAM_TOKEN)

class PropBot:
    def __init__(self):
        self.entry_price = 0
        self.position_size = 0
        self.pnl_history = []
        self.is_running = True

    async def update_telegram_pnl(self):
        """Dynamic floating PnL updated every 5 seconds"""
        message_id = None
        while self.is_running:
            if self.position_size != 0:
                ticker = await exchange.fetch_ticker(SYMBOL)
                current_price = ticker['last']
                # Floating PnL calculation
                pnl = (current_price - self.entry_price) * self.position_size * LEVERAGE
                pnl_pct = (pnl / (self.entry_price * abs(self.position_size))) * 100
                
                status = "🟢 LONG" if self.position_size > 0 else "🔴 SHORT"
                text = f"📊 *Live Trading Status*\nPair: {SYMBOL}\nPos: {status}\n*Floating PnL: ${pnl:.2f} ({pnl_pct:.2f}%)*"
                
                try:
                    if not message_id:
                        msg = await tg_bot.send_message(CHAT_ID, text, parse_mode='Markdown')
                        message_id = msg.message_id
                    else:
                        await tg_bot.edit_message_text(text, CHAT_ID, message_id, parse_mode='Markdown')
                except Exception as e:
                    print(f"TG Error: {e}")
            await asyncio.sleep(5)

    async def trade_logic(self):
        """HFT Order Flow Scalping Engine"""
        while self.is_running:
            try:
                # Get L2 Order Book
                ob = await exchange.watch_order_book(SYMBOL)
                bids = np.array(ob['bids'][:5]) # Top 5 levels
                asks = np.array(ob['asks'][:5])
                
                # Calculate Imbalance (Quant Logic)
                bid_vol = np.sum(bids[:, 1])
                ask_vol = np.sum(asks[:, 1])
                imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)

                # Scalp Signal (If imbalance > 0.8, heavy buy pressure)
                if imbalance > 0.8 and self.position_size <= 0:
                    print(">>> HFT AI SIGNAL: AGGRESSIVE BUY")
                    # self.entry_price = bids[0][0]
                    # self.position_size = 0.01 
                    # await exchange.create_market_buy_order(SYMBOL, 0.01)

                elif imbalance < -0.8 and self.position_size >= 0:
                    print(">>> HFT AI SIGNAL: AGGRESSIVE SELL")
                    # await exchange.create_market_sell_order(SYMBOL, 0.01)

            except Exception as e:
                print(f"Trading Error: {e}")
                await asyncio.sleep(1)

async def start():
    bot = PropBot()
    # Run both the high-speed trader and the telegram updater concurrently
    await asyncio.gather(bot.trade_logic(), bot.update_telegram_pnl())

if __name__ == "__main__":
    asyncio.run(start())


