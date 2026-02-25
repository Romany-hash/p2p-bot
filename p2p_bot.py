import os
import requests
import datetime
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = int(os.environ.get("CHAT_ID", "0"))
if not BOT_TOKEN or CHAT_ID == 0:
    raise ValueError("BOT_TOKEN and CHAT_ID must be set in environment variables")

# ── Constants ─────────────────────────────────────────────────────────────────
ASSETS     = ["USDT", "BTC", "BNB"]
FIAT_LIST  = ["GBP", "EUR", "USD", "AUD", "ZAR", "PLN", "CAD", "NZD",
              "CHF", "SEK", "HKD", "AED", "CZK", "NOK", "DKK", "SGD",
              "JPY", "CNY", "EGP"]
PAY_METHODS = {"Bank Transfer", "Faster Payment", "Instant Transfer"}

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "amount_gbp": 500.0,
    "asset":      "USDT",   # USDT | BTC | BNB
    "trade_type": "SELL",   # SELL | BUY
    "last_fetch": None,
    "alert_threshold": None,
    "auto_job": None,
}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Binance P2P ───────────────────────────────────────────────────────────────
def fetch_p2p(fiat: str, asset: str, trade_type: str, amount: float) -> dict:
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "fiat": fiat,
        "page": 1,
        "rows": 20,
        "tradeType": trade_type,
        "asset": asset,
        "countries": [],
        "proMerchantAds": False,
        "shieldMerchantAds": False,
        "filterType": "all",
        "periods": [],
        "additionalKycVerifyFilter": 0,
        "publisherType": None,
        "payTypes": [],
        "classifies": ["mass", "profession", "fiat_trade"],
        "transAmount": str(amount),
    }
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://p2p.binance.com",
        "Referer": "https://p2p.binance.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code != 200:
            return {"success": False, "error": f"HTTP {r.status_code}"}
        ads = r.json().get("data") or []
        if not ads:
            return {"success": False, "error": "No ads found"}

        # filter by amount range
        valid = [
            a for a in ads
            if float(a["adv"]["minSingleTransAmount"]) <= amount
            <= float(a["adv"]["dynamicMaxSingleTransAmount"])
            and float(a["adv"]["surplusAmount"]) >= amount
        ]
        if not valid:
            return {"success": False, "error": "No ads match your amount"}

        # filter by payment method
        valid = [
            a for a in valid
            if any(m["tradeMethodName"] in PAY_METHODS
                   for m in a["adv"]["tradeMethods"])
        ]
        if not valid:
            return {"success": False, "error": "No ads with allowed payment methods"}

        # SELL → highest price wins  |  BUY → lowest price wins
        if trade_type == "SELL":
            best = max(valid, key=lambda x: float(x["adv"]["price"]))
        else:
            best = min(valid, key=lambda x: float(x["adv"]["price"]))

        price   = float(best["adv"]["price"])
        methods = [m["tradeMethodName"] for m in best["adv"]["tradeMethods"]
                   if m["tradeMethodName"] in PAY_METHODS]

        return {
            "success":      True,
            "fiat":         fiat,
            "asset":        asset,
            "trade_type":   trade_type,
            "price":        price,
            "merchant":     best["advertiser"]["nickName"],
            "completion":   round(float(best["advertiser"].get("monthFinishRate", 0)) * 100, 1),
            "orders":       int(best["advertiser"].get("monthOrderCount", 0)),
            "methods":      methods,
            "min":          float(best["adv"]["minSingleTransAmount"]),
            "max":          float(best["adv"]["dynamicMaxSingleTransAmount"]),
            "available":    float(best["adv"]["surplusAmount"]),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_egp_rate(fiat: str) -> float | None:
    """Get how many EGP = 1 unit of fiat via exchangerate-api."""
    try:
        r = requests.get(
            f"https://api.exchangerate-api.com/v4/latest/{fiat}", timeout=5
        )
        return r.json()["rates"].get("EGP")
    except Exception:
        return None


# ── Core fetch logic ──────────────────────────────────────────────────────────
def run_fetch(amount_gbp: float, asset: str, trade_type: str) -> str:
    ts = datetime.datetime.now().strftime("%d %b %Y  %H:%M:%S")
    emoji_asset = {"USDT": "💵", "BTC": "₿", "BNB": "🔶"}.get(asset, "💰")
    emoji_trade = "📤 SELL" if trade_type == "SELL" else "📥 BUY"
    egp_gbp = get_egp_rate("GBP")

    lines = [
        f"{emoji_asset} *{asset} — {emoji_trade} — HSBC UK Currencies*",
        f"🕐 `{ts}`",
        f"💷 Amount : `{amount_gbp:,.2f}` GBP"
        + (f"  ≈  `{amount_gbp * egp_gbp:,.0f}` EGP" if egp_gbp else ""),
        "",
    ]

    results = []
    for fiat in FIAT_LIST:
        # For non-GBP fiats convert GBP → fiat to know the transaction amount
        if fiat == "GBP":
            amount_fiat = amount_gbp
        else:
            rate = get_egp_rate(fiat)    # EGP per 1 fiat
            egp_per_gbp = egp_gbp        # EGP per 1 GBP
            if rate and egp_per_gbp:
                amount_fiat = amount_gbp * egp_per_gbp / rate
            else:
                amount_fiat = amount_gbp  # fallback

        res = fetch_p2p(fiat, asset, trade_type, amount_fiat)
        if not res["success"]:
            continue

        # EGP equivalent of the fiat price
        egp_rate = get_egp_rate(fiat)
        egp_price = res["price"] * egp_rate if egp_rate else None

        res["egp_price"]  = egp_price
        res["amount_fiat"] = amount_fiat
        results.append(res)

    if not results:
        return "❌ No results found for any currency."

    # Sort: SELL → highest EGP price first  |  BUY → lowest EGP price first
    results.sort(
        key=lambda x: x["egp_price"] or 0,
        reverse=(trade_type == "SELL")
    )

    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(results[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        egp_str = f"`{r['egp_price']:,.2f}` EGP" if r["egp_price"] else "N/A"
        lines.append(
            f"{medal} *{r['fiat']}*  •  "
            f"`{r['price']:,.6f}` {r['fiat']}/{r['asset']}  "
            f"≈  {egp_str}\n"
            f"     👤 {r['merchant']} ({r['completion']}%  {r['orders']} orders)\n"
            f"     💳 {' | '.join(r['methods'])}\n"
            f"     📊 Limits: {r['min']:,.0f} – {r['max']:,.0f} {r['fiat']}\n"
        )

    state["last_fetch"] = datetime.datetime.now()
    return "\n".join(lines)


# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 SELL", callback_data="set_trade_SELL"),
            InlineKeyboardButton("📥 BUY",  callback_data="set_trade_BUY"),
        ],
        [
            InlineKeyboardButton("💵 USDT", callback_data="set_asset_USDT"),
            InlineKeyboardButton("₿ BTC",   callback_data="set_asset_BTC"),
            InlineKeyboardButton("🔶 BNB",  callback_data="set_asset_BNB"),
        ],
        [
            InlineKeyboardButton("🔍 Fetch Now", callback_data="do_fetch"),
        ],
    ])


# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Binance P2P Bot*\n\n"
        "Commands:\n"
        "/setgbp `500`  — set GBP amount\n"
        "/fetch         — fetch best price now\n"
        "/sell          — switch to SELL mode\n"
        "/buy           — switch to BUY mode\n"
        "/usdt          — switch to USDT\n"
        "/btc           — switch to BTC\n"
        "/bnb           — switch to BNB\n"
        "/autostart `120` — auto-fetch every N seconds\n"
        "/autostop      — stop auto-fetch\n"
        "/setalert `650000` — alert when EGP price exceeds value\n"
        "/status        — current settings\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())


async def cmd_setgbp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
        state["amount_gbp"] = amount
        await update.message.reply_text(
            f"✅ Amount set to `{amount:,.2f}` GBP", parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/setgbp 500`", parse_mode="Markdown")


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["trade_type"] = "SELL"
    await update.message.reply_text("✅ Mode set to *SELL*", parse_mode="Markdown")


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["trade_type"] = "BUY"
    await update.message.reply_text("✅ Mode set to *BUY*", parse_mode="Markdown")


async def cmd_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["asset"] = "USDT"
    await update.message.reply_text("✅ Asset set to *USDT*", parse_mode="Markdown")


async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["asset"] = "BTC"
    await update.message.reply_text("✅ Asset set to *BTC*", parse_mode="Markdown")


async def cmd_bnb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["asset"] = "BNB"
    await update.message.reply_text("✅ Asset set to *BNB*", parse_mode="Markdown")


async def cmd_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching best prices...", parse_mode="Markdown")
    result = run_fetch(state["amount_gbp"], state["asset"], state["trade_type"])
    await msg.edit_text(result, parse_mode="Markdown")


async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        threshold = float(context.args[0])
        state["alert_threshold"] = threshold
        await update.message.reply_text(
            f"🔔 Alert set for `{threshold:,.2f}` EGP", parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/setalert 650000`", parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_s = state["last_fetch"].strftime("%H:%M:%S") if state["last_fetch"] else "Never"
    auto   = "Running ✅" if state["auto_job"] else "Stopped ❌"
    msg = (
        f"⚙️ *Status*\n\n"
        f"  Amount     : `{state['amount_gbp']:,.2f}` GBP\n"
        f"  Asset      : `{state['asset']}`\n"
        f"  Trade      : `{state['trade_type']}`\n"
        f"  Last fetch : `{last_s}`\n"
        f"  Auto-fetch : {auto}\n"
        f"  Alert      : `{state['alert_threshold']:,.2f}` EGP"
        if state["alert_threshold"] else
        f"⚙️ *Status*\n\n"
        f"  Amount     : `{state['amount_gbp']:,.2f}` GBP\n"
        f"  Asset      : `{state['asset']}`\n"
        f"  Trade      : `{state['trade_type']}`\n"
        f"  Last fetch : `{last_s}`\n"
        f"  Auto-fetch : {auto}\n"
        f"  Alert      : Not set"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Auto-fetch job ─────────────────────────────────────────────────────────────
async def auto_fetch_job(context: ContextTypes.DEFAULT_TYPE):
    result = run_fetch(state["amount_gbp"], state["asset"], state["trade_type"])
    threshold = state["alert_threshold"]

    if threshold:
        # Check if any result exceeds threshold
        for line in result.split("\n"):
            if "EGP" in line:
                try:
                    egp_val = float(
                        line.split("`")[3].replace(",", "")
                    )
                    if (state["trade_type"] == "SELL" and egp_val >= threshold) or \
                       (state["trade_type"] == "BUY"  and egp_val <= threshold):
                        await context.bot.send_message(
                            chat_id=CHAT_ID,
                            text=f"🚨 *ALERT TRIGGERED!*\n\n{result}",
                            parse_mode="Markdown"
                        )
                        return
                except Exception:
                    pass
    else:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=result,
            parse_mode="Markdown"
        )


async def cmd_autostart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = int(context.args[0])
        if interval < 30:
            await update.message.reply_text("❌ Minimum interval is 30 seconds.")
            return

        # Remove existing job if any
        if state["auto_job"]:
            state["auto_job"].schedule_removal()
            state["auto_job"] = None

        job = context.job_queue.run_repeating(
            auto_fetch_job,
            interval=interval,
            first=5,
            chat_id=CHAT_ID,
        )
        state["auto_job"] = job
        await update.message.reply_text(
            f"▶️ Auto-fetch every `{interval}` seconds started.", parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: `/autostart 120`", parse_mode="Markdown")


async def cmd_autostop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state["auto_job"]:
        state["auto_job"].schedule_removal()
        state["auto_job"] = None
        await update.message.reply_text("⏹️ Auto-fetch stopped.")
    else:
        await update.message.reply_text("ℹ️ Auto-fetch is not running.")


# ── Inline button callbacks ────────────────────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("set_trade_"):
        state["trade_type"] = data.split("_")[2]
        await query.edit_message_reply_markup(reply_markup=main_keyboard())
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Mode set to *{state['trade_type']}*",
            parse_mode="Markdown"
        )

    elif data.startswith("set_asset_"):
        state["asset"] = data.split("_")[2]
        await query.edit_message_reply_markup(reply_markup=main_keyboard())
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Asset set to *{state['asset']}*",
            parse_mode="Markdown"
        )

    elif data == "do_fetch":
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="⏳ Fetching best prices..."
        )
        result = run_fetch(state["amount_gbp"], state["asset"], state["trade_type"])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=result,
            parse_mode="Markdown"
        )


# ── Handle plain number messages ──────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(",", ""))
        if amount > 0:
            state["amount_gbp"] = amount
            await update.message.reply_text(
                f"✅ Amount updated to `{amount:,.2f}` GBP", parse_mode="Markdown"
            )
    except ValueError:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("setgbp",     cmd_setgbp))
    app.add_handler(CommandHandler("sell",       cmd_sell))
    app.add_handler(CommandHandler("buy",        cmd_buy))
    app.add_handler(CommandHandler("usdt",       cmd_usdt))
    app.add_handler(CommandHandler("btc",        cmd_btc))
    app.add_handler(CommandHandler("bnb",        cmd_bnb))
    app.add_handler(CommandHandler("fetch",      cmd_fetch))
    app.add_handler(CommandHandler("setalert",   cmd_setalert))
    app.add_handler(CommandHandler("autostart",  cmd_autostart))
    app.add_handler(CommandHandler("autostop",   cmd_autostop))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
