import asyncio
import requests
from datetime import datetime, timezone
from collections import deque
import os
import json
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue

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

class BotConfig:
    def __init__(self):
        self.drop_threshold = 0.25
        self.interval_seconds = 10
        self.active_windows = ['10s', '30s', '1min', '5min']

    def update_threshold(self, new_threshold):
        if 0 < new_threshold <= 1:
            self.drop_threshold = new_threshold
            return True
        return False

# Global state
config = BotConfig()
logged_on = False
history = deque(maxlen=HISTORY_MAXLEN)
last_price = None
update_counter = 0

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

def check_drops(history, now, current_price, config):
    alerts = []
    def compare(step_back, label):
        if label in config.active_windows and len(history) > step_back:
            past_price = history[-(step_back + 1)][1]
            if past_price > 0:
                change = (current_price - past_price) / past_price
                if change <= -config.drop_threshold:
                    alerts.append((label, past_price, change))
    compare(STEPS_10S,  "10s")
    compare(STEPS_30S,  "30s")
    compare(STEPS_1MIN, "1min")
    compare(STEPS_5MIN, "5min")
    return alerts

async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    global logged_on, history, last_price, update_counter
    if not logged_on:
        return
    try:
        now = datetime.now(timezone.utc)
        price = get_price_usd()
        last_price = price
        print(f"[{now.isoformat()}] Precio oXAUT: {price:.2f} USD")
        log_price(now, price)
        history.append((now, price))
        alerts = check_drops(history, now, price, config)
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
        # Periodic update every 5 minutes (30 * 10s)
        update_counter += 1
        if update_counter % 30 == 0:
            await send_message(context, context.job.chat_id, f"Precio actual: {price:.2f} USD - Online")
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
        await update.message.reply_text(f"Log activado. Precio actual: {price:.2f} USD")
        # Start monitoring job
        context.job_queue.run_repeating(monitor_job, interval=config.interval_seconds, first=0, chat_id=chat_id)
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

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        current_price = get_price_usd()
        await update.message.reply_text(f"Precio actual de oXAUT: {current_price:.2f} USD")
    except Exception as e:
        await update.message.reply_text(f"Error obteniendo precio: {e}")

async def setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not context.args:
        await update.message.reply_text(f"Umbral actual: {config.drop_threshold * 100:.0f}%\nUso: /setthreshold <porcentaje> (ej: /setthreshold 20)")
        return
    try:
        new_pct = float(context.args[0])
        if config.update_threshold(new_pct / 100):
            await update.message.reply_text(f"Umbral actualizado a {new_pct:.0f}%")
        else:
            await update.message.reply_text("Porcentaje inválido. Debe ser entre 1 y 100.")
    except ValueError:
        await update.message.reply_text("Uso: /setthreshold <porcentaje> (ej: /setthreshold 20)")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global history
    if not history:
        await update.message.reply_text("No hay historial disponible.")
        return
    minutes = 5  # default
    if context.args:
        try:
            minutes = int(context.args[0])
        except ValueError:
            pass
    # Show last N minutes (approx 6 entries per minute)
    entries = int(minutes * 6)
    recent = list(history)[-entries:]
    if not recent:
        await update.message.reply_text("No hay datos suficientes para ese período.")
        return
    msg = f"Historial de precios (últimos {minutes} min):\n"
    for ts, p in recent:
        msg += f"{ts.strftime('%H:%M:%S')}: {p:.2f} USD\n"
    await update.message.reply_text(msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """
Comandos disponibles:
/logon - Inicia monitoreo de precios
/logoff - Detiene monitoreo
/status - Muestra estado actual
/price - Precio actual inmediato
/setthreshold <%> - Cambia umbral de caída (ej: /setthreshold 20)
/history [min] - Historial de precios (últimos min, default 5)
/help - Muestra esta ayuda
    """
    await update.message.reply_text(help_text)

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).job_queue(JobQueue()).build()

    application.add_handler(CommandHandler("logon", logon))
    application.add_handler(CommandHandler("logoff", logoff))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("setthreshold", setthreshold))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("help", help_command))

    print("Bot iniciado. Esperando comandos...")
    application.run_polling()

if __name__ == "__main__":
    main()