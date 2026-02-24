
import os
import requests
import datetime
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
if not BOT_TOKEN or CHAT_ID == 0:
    raise ValueError("BOT_TOKEN and CHAT_ID must be set in environment variables")

# Supported currencies for HSBC UK
HSBC_SUPPORTED_CURRENCIES = [
    "GBP", "EUR", "USD", "AUD", "ZAR", "PLN", "CAD", "NZD",
    "CHF", "SEK", "HKD", "AED", "CZK", "NOK", "DKK", "SGD",
    "JPY", "CNY", "EGP"
]

# Allowed payment methods
ALLOWED_PAYMENT_METHODS = {
    "Bank Transfer",
    "Faster Payment",
    "Instant Transfer"
}

# State management
state = {
    "gbp_amount": None,
    "last_fetch": None,
    "last_results": [],
    "last_usdt_price": None
}

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Exchange rate API
def get_exchange_rate(from_currency, to_currency="EGP"):
    try:
        url = f"https://api.exchangerate-api.com/v4/latest/{from_currency}"
        r = requests.get(url, timeout=5)
        data = r.json()
        if to_currency in data["rates"]:
            return data["rates"][to_currency]
    except Exception:
        pass
    return None

# Get best buy price for currency
def get_p2p_buy_price_for_currency(currency, usdt_amount):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "fiat": currency,
        "page": 1,
        "rows": 20,
        "tradeType": "BUY",  # Changed to BUY instead of SELL
        "asset": "USDT",
        "countries": [],
        "proMerchantAds": False,
        "shieldMerchantAds": False,
        "filterType": "all",
        "periods": [],
        "additionalKycVerifyFilter": 0,
        "publisherType": None,
        "payTypes": [],
        "classifies": ["mass", "profession", "fiat_trade"],
        "transAmount": usdt_amount,
    }
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://p2p.binance.com",
        "Referer": "https://p2p.binance.com/en/trade/buy/USDT",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code != 200:
            return {"currency": currency, "success": False, "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        ads = data.get("data") or []
        if not ads:
            return {"currency": currency, "success": False, "error": "No ads"}

        valid = []
        for ad in ads:
            mn = float(ad["adv"]["minSingleTransAmount"])
            mx = float(ad["adv"]["dynamicMaxSingleTransAmount"])
            avl = float(ad["adv"]["surplusAmount"])
            if mn <= usdt_amount <= mx and avl >= usdt_amount:
                valid.append(ad)

        if not valid:
            return {"currency": currency, "success": False, "error": f"No merchant accepts {usdt_amount} USDT"}

        # Filter by allowed payment methods
        valid = [
            ad for ad in valid
            if any(
                m["tradeMethodName"] in ALLOWED_PAYMENT_METHODS
                for m in ad["adv"]["tradeMethods"]
            )
        ]

        if not valid:
            return {"currency": currency, "success": False, "error": "No ads with allowed payment methods"}

        best = max(valid, key=lambda x: float(x["adv"]["price"]))
        price = float(best["adv"]["price"])
        pays = [
            m["tradeMethodName"] for m in best["adv"]["tradeMethods"]
            if m["tradeMethodName"] in ALLOWED_PAYMENT_METHODS
        ]

        return {
            "currency": currency,
            "price": price,
            "total_usdt": usdt_amount * price,
            "merchant": best["advertiser"]["nickName"],
            "payment_methods": pays,
            "min": float(best["adv"]["minSingleTransAmount"]),
            "max": float(best["adv"]["dynamicMaxSingleTransAmount"]),
            "available": float(best["adv"]["surplusAmount"]),
            "completion_pct": round(float(best["advertiser"].get("monthFinishRate", 0)) * 100, 2),
            "monthly_orders": int(best["advertiser"].get("monthOrderCount", 0)),
            "success": True,
        }
    except Exception as e:
        return {"currency": currency, "success": False, "error": str(e)}

# Format results
def fmt_results(results, gbp_amount, usdt_price):
    if not results:
        return "❌ No results found."

    ts = datetime.datetime.now().strftime("%d %b %Y  %H:%M:%S")
    best = results[0]

    # Calculate EGP equivalent for the GBP amount
    gbp_to_egp = get_exchange_rate("GBP", "EGP")
    if gbp_to_egp:
        gbp_egp = gbp_amount * gbp_to_egp
    else:
        gbp_egp = "N/A"

    # Calculate how much USDT we'd get for the GBP amount
    if usdt_price:
        usdt_amount = gbp_amount / usdt_price
        usdt_egp = usdt_amount * get_exchange_rate("USDT", "EGP") if get_exchange_rate("USDT", "EGP") else "N/A"
    else:
        usdt_amount = "N/A"
        usdt_egp = "N/A"

    lines = [
        f"*💰 Best Buy Rates for {gbp_amount} GBP*",
        f"🕐 `{ts}`",
        f"💵 Current USDT/GBP rate: `{usdt_price:.6f}` USDT/GBP (from previous fetch)",
        "",
        "🏆 *BEST OFFER*",
        f"  Currency : `{best['currency']}`",
        f"  Price    : `{best['price']:.6f}` USDT/{best['currency']}",
        f"  For {gbp_amount} GBP you'd get: `{usdt_amount:.2f}` USDT",
        f"  EGP equiv : `{usdt_egp}` EGP",
        f"  Merchant : `{best['merchant']}` ({best['completion_pct']}%)",
        f"  Payment  : {', '.join(best['payment_methods'][:3])}",
        f"  Limits   : {best['min']:.0f} - {best['max']:.0f} {best['currency']}",
        "",
        "─────────────────────────",
        f"*TOP {min(5, len(results))} OFFERS*",
        "",
    ]

    for i, r in enumerate(results[:5]):
        usdt_for_gbp = gbp_amount / r["price"]
        usdt_egp = usdt_for_gbp * get_exchange_rate("USDT", "EGP") if get_exchange_rate("USDT", "EGP") else "N/A"
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
        lines.append(
            f"{medal} `{r['currency']}` "
            f"price `{r['price']:.6f}` "
            f"= `{usdt_for_gbp:.2f}` USDT "
            f"= `{usdt_egp}` EGP "
            f"| {r['merchant']}"
        )

    return "\n".join(lines)

# Main command handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Binance P2P Buy Bot*\n\n"
        "How to use:\n\n"
        "1. Send `/setgbp 1000` to specify how much GBP you want to sell\n"
        "2. Send `/fetch` to get current best buy rates\n"
        "3. Reply with any number to update your GBP amount\n"
        "4. Use `/status` to check current settings"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_setgbp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/setgbp 1000`", parse_mode="Markdown")
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
        state["gbp_amount"] = amount
        state["last_results"] = []
        state["last_usdt_price"] = None
        await update.message.reply_text(f"✅ Set to sell `{amount}` GBP", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid amount (e.g., `/setgbp 1000`)", parse_mode="Markdown")

async def cmd_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state["gbp_amount"] is None:
        await update.message.reply_text("❌ Please set your GBP amount first with `/setgbp X`", parse_mode="Markdown")
        return

    # First get the current USDT/GBP rate
    try:
        gbp_to_usdt = get_exchange_rate("GBP", "USDT")
        if gbp_to_usdt:
            state["last_usdt_price"] = 1 / gbp_to_usdt
        else:
            state["last_usdt_price"] = None
    except:
        state["last_usdt_price"] = None

    # Calculate how much USDT we'd get for our GBP amount
    usdt_for_gbp = state["gbp_amount"] / state["last_usdt_price"] if state["last_usdt_price"] else None

    # Now get the best buy prices for all supported currencies
    results = []
    for currency in HSBC_SUPPORTED_CURRENCIES:
        # Calculate how much USDT we need to buy to get equivalent value
        if currency == "GBP":
            usdt_needed = usdt_for_gbp if usdt_for_gbp else 100
        else:
            # For other currencies, we'll just check the standard amount
            usdt_needed = 100

        result = get_p2p_buy_price_for_currency(currency, usdt_needed)
        if result["success"]:
            # Convert to GBP equivalent
            if currency != "GBP":
                price_in_gbp = result["price"] * get_exchange_rate(currency, "GBP")
                if price_in_gbp:
                    result["price_in_gbp"] = price_in_gbp
                    results.append(result)

    # Sort by price (best first)
    results.sort(key=lambda x: x.get("price_in_gbp", 0), reverse=True)

    # Format and send results
    if not results:
        msg = "❌ No results found for any currency"
    else:
        msg = fmt_results(results, state["gbp_amount"], state["last_usdt_price"])

    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gbp_amount = state["gbp_amount"]
    last_fetch = state["last_fetch"]
    last_s = last_fetch.strftime("%H:%M:%S") if last_fetch else "Never"
    usdt_price = state["last_usdt_price"]

    msg = (
        f"⚙️ *Current Settings*\n\n"
        f"  GBP amount to sell : `{gbp_amount}` GBP\n"
        f"  Current USDT/GBP rate : `{usdt_price:.6f}` USDT/GBP\n"
        f"  Last fetch          : `{last_s}`\n"
        f"  Allowed payment    : `{', '.join(ALLOWED_PAYMENT_METHODS)}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# Handle text messages for updating GBP amount
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        amount = float(text)
        if amount > 0:
            state["gbp_amount"] = amount
            state["last_results"] = []
            state["last_usdt_price"] = None
            await update.message.reply_text(f"✅ Updated to sell `{amount}` GBP", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Please enter a positive number", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number", parse_mode="Markdown")

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setgbp", cmd_setgbp))
    app.add_handler(CommandHandler("fetch", cmd_fetch))
    app.add_handler(CommandHandler("status", cmd_status))

    # Handle any text message as potential GBP amount update
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
