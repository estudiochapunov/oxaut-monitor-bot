import asyncio
import requests
from datetime import datetime, timezone
from collections import deque
import os
import json
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
DEXSCREENER_URL = (
    "https://api.dexscreener.com/latest/dex/pairs/"
    "worldchain/0x84c7cc9107afad860db12dfadf49c1dac2e0723b"
)
LOG_FILE = "precios_oxaut.log"
CONFIG_FILE = "config_telegram.json"
INTERVAL_SECONDS = 10
DROP_THRESHOLD = 0.25   # 25% de caída

# cuántos pasos atrás miramos (con intervalo de 10s)
STEPS_10S  = 1   # 10 segundos
STEPS_30S  = 3   # 30 segundos
STEPS_1MIN = 6   # 1 minuto
STEPS_5MIN = 30  # 5 minutos

HISTORY_MAXLEN = STEPS_5MIN + 2  # por las dudas

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

# Global state
logged_on = False
history = deque(maxlen=HISTORY_MAXLEN)
last_price = None

# ----------------------------------------

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

async def send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str, is_alert: bool = False):
    prefix = "[ALERTA] " if is_alert else "[INFO] "
    full_message = prefix + message if is_alert else message
    await context.bot.send_message(chat_id=chat_id, text=full_message, parse_mode="Markdown")

def get_price_usd():
    resp = requests.get(DEXSCREENER_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    pairs = data.get("pairs", [])
    if not pairs:
        raise RuntimeError("No se encontraron pares en la respuesta de DexScreener")
    price_str = pairs[0].get("priceUsd")
    if price_str is None:
        raise RuntimeError("La respuesta no tiene campo 'priceUsd'")
    return float(price_str)

def log_price(timestamp, price):
    line = f"{timestamp.isoformat()},{price:.2f}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)

def check_drops(history, now, current_price):
    alerts = []
    def compare(step_back, label):
        if len(history) > step_back:
            past_price = history[-(step_back + 1)][1]
            if past_price > 0:
                change = (current_price - past_price) / past_price
                if change <= -DROP_THRESHOLD:
                    alerts.append((label, past_price, change))
    compare(STEPS_10S,  "10s")
    compare(STEPS_30S,  "30s")
    compare(STEPS_1MIN, "1min")
    compare(STEPS_5MIN, "5min")
    return alerts

async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    global logged_on, history, last_price
    if not logged_on:
        return
    try:
        now = datetime.now(timezone.utc)
        price = get_price_usd()
        last_price = price
        print(f"[{now.isoformat()}] Precio oXAUT: {price:.2f} USD")
        log_price(now, price)
        history.append((now, price))
        alerts = check_drops(history, now, price)
        if alerts:
            msg = (
                f"POZO ACTIVADO en oXAUT a las {now.isoformat()}.\n"
                f"Precio actual: {price:.2f} USD.\n"
                + "\n".join(
                    f"Ventana {label}: de {past:.2f} a {price:.2f} ({change*100:.2f}%)"
                    for (label, past, change) in alerts
                )
            )
            await send_message(context, context.job.chat_id, msg, is_alert=True)
    except Exception as e:
        print(f"[WARN] Error obteniendo precio: {e}")

async def logon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global logged_on, history, last_price
    chat_id = update.effective_chat.id
    if logged_on:
        await update.message.reply_text("Ya estás logueado.")
        return
    logged_on = True
    history.clear()
    try:
        price = get_price_usd()
        last_price = price
        await send_message(context, chat_id, f"Log activado. Precio actual: {price:.2f} USD")
        # Start monitoring job
        context.job_queue.run_repeating(monitor_job, interval=INTERVAL_SECONDS, first=0, chat_id=chat_id)
    except Exception as e:
        await update.message.reply_text(f"Error activando log: {e}")
        logged_on = False

async def logoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global logged_on
    if not logged_on:
        await update.message.reply_text("No estás logueado.")
        return
    logged_on = False
    await update.message.reply_text("Log desactivado.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global logged_on, last_price
    status_msg = f"Estado: {'Logueado' if logged_on else 'No logueado'}"
    if last_price is not None:
        status_msg += f"\nÚltimo precio: {last_price:.2f} USD"
    await update.message.reply_text(status_msg)

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("logon", logon))
    application.add_handler(CommandHandler("logoff", logoff))
    application.add_handler(CommandHandler("status", status))

    print("Bot iniciado. Esperando comandos...")
    application.run_polling()

if __name__ == "__main__":
    main()