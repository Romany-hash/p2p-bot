import os
import asyncio
import requests
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = int(os.environ.get("CHAT_ID", "0"))

if not BOT_TOKEN or CHAT_ID == 0:
    raise ValueError("BOT_TOKEN and CHAT_ID must be set in environment variables")

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
HSBC_SUPPORTED_CURRENCIES = [
    "GBP", "EUR", "USD", "AUD", "ZAR", "PLN", "CAD", "NZD",
    "CHF", "SEK", "HKD", "AED", "CZK", "NOK", "DKK", "SGD",
    "JPY", "CNY", "EGP",
]

ALLOWED_PAYMENT_METHODS = {
    "Bank Transfer",
    "Faster Payment",
    "Instant Transfer",
}

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────
state = {
    "usdt_amount":   100.0,
    "threshold_egp": None,
    "auto_interval": 60,
    "auto_active":   False,
    "last_fetch":    None,
    "last_results":  [],
    "alerted_keys":  set(),
}

# ─────────────────────────────────────────────────────────────
# BACKEND
# ─────────────────────────────────────────────────────────────
def get_exchange_rate_to_egp(from_currency, amount):
    if from_currency == "EGP":
        return amount
    try:
        url = f"https://api.exchangerate-api.com/v4/latest/{from_currency}"
        r = requests.get(url, timeout=5)
        data = r.json()
        if "rates" in data and "EGP" in data["rates"]:
            return amount * data["rates"]["EGP"]
        r2    = requests.get("https://api.exchangerate-api.com/v4/latest/EGP", timeout=5)
        data2 = r2.json()
        if "rates" in data2 and from_currency in data2["rates"]:
            return amount / data2["rates"][from_currency]
    except Exception:
        pass
    return None


def get_p2p_price_for_currency(currency, usdt_amount):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    payload = {
        "fiat": currency, "page": 1, "rows": 20,
        "tradeType": "SELL", "asset": "USDT",
        "countries": [], "proMerchantAds": False, "shieldMerchantAds": False,
        "filterType": "all", "periods": [], "additionalKycVerifyFilter": 0,
        "publisherType": None, "payTypes": [],
        "classifies": ["mass", "profession", "fiat_trade"],
        "transAmount": usdt_amount,
    }
    headers = {
        "Accept": "*/*", "Content-Type": "application/json",
        "Origin": "https://p2p.binance.com",
        "Referer": "https://p2p.binance.com/en/trade/sell/USDT",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code != 200:
            return {"currency": currency, "success": False,
                    "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        ads  = data.get("data") or []
        if not ads:
            return {"currency": currency, "success": False, "error": "No ads"}

        # Filter by amount and availability
        valid = []
        for ad in ads:
            mn  = float(ad["adv"]["minSingleTransAmount"])
            mx  = float(ad["adv"]["dynamicMaxSingleTransAmount"])
            avl = float(ad["adv"]["surplusAmount"])
            if mn <= usdt_amount <= mx and avl >= usdt_amount:
                valid.append(ad)

        if not valid:
            return {"currency": currency, "success": False,
                    "error": f"No merchant accepts {usdt_amount} USDT"}

        # Filter by allowed payment methods
        valid = [
            ad for ad in valid
            if any(
                m["tradeMethodName"] in ALLOWED_PAYMENT_METHODS
                for m in ad["adv"]["tradeMethods"]
            )
        ]

        if not valid:
            return {"currency": currency, "success": False,
                    "error": "No ads with allowed payment methods"}

        best  = max(valid, key=lambda x: float(x["adv"]["price"]))
        price = float(best["adv"]["price"])
        pays  = [
            m["tradeMethodName"] for m in best["adv"]["tradeMethods"]
            if m["tradeMethodName"] in ALLOWED_PAYMENT_METHODS
        ]

        return {
            "currency":          currency,
            "price":             price,
            "total_in_currency": usdt_amount * price,
            "merchant":          best["advertiser"]["nickName"],
            "payment_methods":   pays,
            "min":               float(best["adv"]["minSingleTransAmount"]),
            "max":               float(best["adv"]["dynamicMaxSingleTransAmount"]),
            "available":         float(best["adv"]["surplusAmount"]),
            "completion_pct":    round(
                float(best["advertiser"].get("monthFinishRate", 0)) * 100, 2
            ),
            "monthly_orders":    int(best["advertiser"].get("monthOrderCount", 0)),
            "success":           True,
        }
    except requests.exceptions.Timeout:
        return {"currency": currency, "success": False, "error": "Timeout"}
    except Exception as e:
        return {"currency": currency, "success": False, "error": str(e)}


def fetch_all_blocking(usdt_amount, max_workers=10):
    """Runs in a background thread — do NOT await this."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(get_p2p_price_for_currency, c, usdt_amount): c
            for c in HSBC_SUPPORTED_CURRENCIES
        }
        for fut in as_completed(futures):
            r = fut.result()
            if r["success"]:
                egp = get_exchange_rate_to_egp(r["currency"], r["total_in_currency"])
                r["egp_equivalent"] = egp
                results.append(r)

    results.sort(key=lambda x: x.get("egp_equivalent") or 0, reverse=True)
    return results


# ─────────────────────────────────────────────────────────────
# FORMATTERS
# ─────────────────────────────────────────────────────────────
def fmt_results(results, usdt_amount, label="📊 Latest Rates"):
    if not results:
        return "❌ No results found."

    ts       = datetime.datetime.now().strftime("%d %b %Y  %H:%M:%S")
    best     = results[0]
    egp_best = f"{best['egp_equivalent']:,.2f}" if best.get("egp_equivalent") else "N/A"

    lines = [
        f"*{label}*",
        f"💵 Amount: `{usdt_amount} USDT`",
        f"🕐 `{ts}`",
        "",
        "🏆 *BEST OFFER*",
        f"  Currency : `{best['currency']}`",
        f"  Price    : `{best['price']:.4f}` {best['currency']}/USDT",
        f"  You get  : `{best['total_in_currency']:,.2f}` {best['currency']}",
        f"  EGP      : `{egp_best}` EGP",
        f"  Merchant : `{best['merchant']}` ({best['completion_pct']}%)",
        f"  Payment  : {', '.join(best['payment_methods'][:3])}",
        f"  Limits   : {best['min']:.0f} - {best['max']:.0f} USDT",
        "",
        "─────────────────────────",
        f"*TOP {min(10, len(results))} OFFERS*",
        "",
    ]

    for i, r in enumerate(results[:10]):
        egp_s = f"{r['egp_equivalent']:,.2f}" if r.get("egp_equivalent") else "N/A"
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
        lines.append(
            f"{medal} `{r['currency']}` "
            f"price `{r['price']:.4f}` "
            f"= `{egp_s}` EGP "
            f"| {r['merchant']}"
        )

    lines += ["", f"_Checked {len(HSBC_SUPPORTED_CURRENCIES)} currencies_"]
    return "\n".join(lines)


def fmt_alert(r, usdt_amount, threshold):
    allowed_pays = [p for p in r["payment_methods"] if p in ALLOWED_PAYMENT_METHODS]
    ts  = datetime.datetime.now().strftime("%H:%M:%S")
    egp = f"{r['egp_equivalent']:,.2f}" if r.get("egp_equivalent") else "N/A"
    return "\n".join([
        f"🚨 *PRICE ALERT* `{ts}`",
        "",
        f"  Currency  : `{r['currency']}`",
        f"  Price     : `{r['price']:.4f}` {r['currency']}/USDT",
        f"  You get   : `{r['total_in_currency']:,.2f}` {r['currency']}",
        f"  EGP equiv : `{egp}` EGP",
        f"  Threshold : `{threshold:,.2f}` EGP exceeded",
        f"  Merchant  : `{r['merchant']}` ({r['completion_pct']}%)",
        f"  Orders/mo : {r['monthly_orders']}",
        f"  Payment   : {', '.join(allowed_pays)}",
        f"  Limits    : {r['min']:.0f} - {r['max']:.0f} USDT",
        f"  Available : {r['available']:.2f} USDT",
    ])


# ─────────────────────────────────────────────────────────────
# ALERT CHECKER
# ─────────────────────────────────────────────────────────────
async def check_and_send_alerts(bot, results, usdt_amount):
    threshold = state["threshold_egp"]
    if threshold is None:
        return

    for r in results:
        egp = r.get("egp_equivalent")
        if not egp or egp <= threshold:
            continue

        allowed_pays = [p for p in r["payment_methods"] if p in ALLOWED_PAYMENT_METHODS]
        if not allowed_pays:
            continue

        key = (r["currency"], round(r["price"], 4))
        if key in state["alerted_keys"]:
            continue
        state["alerted_keys"].add(key)

        msg = fmt_alert(r, usdt_amount, threshold)
        try:
            await bot.send_message(
                chat_id=CHAT_ID, text=msg, parse_mode="Markdown"
            )
            logger.info(f"Alert sent: {r['currency']} @ {r['price']}")
        except Exception as e:
            logger.error(f"Alert send failed: {e}")


# ─────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Binance P2P Rate Bot*\n\n"
        "Commands:\n\n"
        "/fetch — fetch prices now\n"
        "/setamount 500 — set USDT amount\n"
        "/setalert 650000 — alert when EGP exceeds value\n"
        "/cancelalert — disable alert\n"
        "/autostart 60 — auto check every N seconds\n"
        "/autostop — stop auto refresh\n"
        "/status — show current settings\n"
        "/top5 — top 5 from last fetch\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⏳ Fetching rates for `{state['usdt_amount']} USDT`...",
        parse_mode="Markdown"
    )
    state["alerted_keys"].clear()

    loop    = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None, fetch_all_blocking, state["usdt_amount"]
    )

    state["last_results"] = results
    state["last_fetch"]   = datetime.datetime.now()

    msg = fmt_results(results, state["usdt_amount"])
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID, text=msg, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Send error: {e}")
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)

    await check_and_send_alerts(context.bot, results, state["usdt_amount"])


async def cmd_setamount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
        state["usdt_amount"] = amount
        state["alerted_keys"].clear()
        await update.message.reply_text(
            f"✅ Amount set to `{amount}` USDT", parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Usage: `/setamount 500`", parse_mode="Markdown"
        )


async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        threshold = float(context.args[0].replace(",", ""))
        if threshold <= 0:
            raise ValueError
        state["threshold_egp"] = threshold
        state["alerted_keys"].clear()
        await update.message.reply_text(
            f"🔔 Alert set for `{threshold:,.2f}` EGP\n"
            f"_Only Bank Transfer, Faster Payment, Instant Transfer_",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Usage: `/setalert 650000`", parse_mode="Markdown"
        )


async def cmd_cancelalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["threshold_egp"] = None
    state["alerted_keys"].clear()
    await update.message.reply_text("🔕 Alert disabled.")


async def cmd_autostart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = int(context.args[0]) if context.args else 60
        if interval < 10:
            raise ValueError
        state["auto_interval"] = interval
        state["auto_active"]   = True

        current = context.job_queue.get_jobs_by_name("auto_refresh")
        for job in current:
            job.schedule_removal()

        context.job_queue.run_repeating(
            _auto_refresh_job,
            interval=interval,
            first=5,
            name="auto_refresh"
        )
        await update.message.reply_text(
            f"▶️ Auto-refresh every `{interval}` seconds started.",
            parse_mode="Markdown"
        )
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Usage: `/autostart 60` (min 10 seconds)",
            parse_mode="Markdown"
        )


async def cmd_autostop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["auto_active"] = False
    for job in context.job_queue.get_jobs_by_name("auto_refresh"):
        job.schedule_removal()
    await update.message.reply_text("⏹ Auto-refresh stopped.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    threshold = state["threshold_egp"]
    last      = state["last_fetch"]
    last_s    = last.strftime("%H:%M:%S") if last else "Never"
    auto_s    = f"every {state['auto_interval']}s" if state["auto_active"] else "OFF"
    msg = (
        f"⚙️ *Settings*\n\n"
        f"  USDT amount   : `{state['usdt_amount']}`\n"
        f"  Alert thresh  : `{f'{threshold:,.2f} EGP' if threshold else 'Disabled'}`\n"
        f"  Auto-refresh  : `{auto_s}`\n"
        f"  Last fetch    : `{last_s}`\n"
        f"  Cached results: `{len(state['last_results'])}`\n"
        f"  Allowed pay   : `{', '.join(ALLOWED_PAYMENT_METHODS)}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_top5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = state["last_results"]
    if not results:
        await update.message.reply_text("No data yet. Use /fetch first.")
        return
    msg = fmt_results(results[:5], state["usdt_amount"], label="📊 Top 5 Offers")
    try:
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(msg)


# ─────────────────────────────────────────────────────────────
# AUTO REFRESH JOB
# ─────────────────────────────────────────────────────────────
async def _auto_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    if not state["auto_active"]:
        return
    logger.info("Auto-refresh running...")

    loop    = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None, fetch_all_blocking, state["usdt_amount"]
    )

    state["last_results"] = results
    state["last_fetch"]   = datetime.datetime.now()
    await check_and_send_alerts(context.bot, results, state["usdt_amount"])


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_start))
    app.add_handler(CommandHandler("fetch",       cmd_fetch))
    app.add_handler(CommandHandler("setamount",   cmd_setamount))
    app.add_handler(CommandHandler("setalert",    cmd_setalert))
    app.add_handler(CommandHandler("cancelalert", cmd_cancelalert))
    app.add_handler(CommandHandler("autostart",   cmd_autostart))
    app.add_handler(CommandHandler("autostop",    cmd_autostop))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("top5",        cmd_top5))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
