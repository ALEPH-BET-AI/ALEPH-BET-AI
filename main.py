
import os
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# CONFIGURATION
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", "10000"))

# JOURNALISATION
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ACCUEIL
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "👑 Bienvenue sur ALEPH BET AI !\n\n"
        "⚽ Ton assistant de pronostics sportifs.\n"
        "🎯 Objectif : 2 meilleurs pronostics par analyse.\n"
        "📊 Score exact uniquement si les données sont suffisantes.\n\n"
        "Le moteur de données est en cours de configuration.\n"
        "Aucun pronostic ne sera inventé.\n\n"
        "Commandes disponibles : /start /aide /statut"
    )
    if update.message:
        await update.message.reply_text(message)


# AIDE
async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "📖 ALEPH BET AI\n\n"
        "/start - Accueil\n"
        "/aide - Aide\n"
        "/statut - État du robot\n\n"
        "Les analyses seront activées après "
        "la connexion aux données sportives."
    )
    if update.message:
        await update.message.reply_text(message)


# STATUT
async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "🤖 ALEPH BET AI\n\n"
        "Bot Telegram : connecté.\n"
        "Serveur : actif.\n"
        "Moteur de pronostics : en configuration.\n"
        "Données des matchs : à connecter."
    )
    if update.message:
        await update.message.reply_text(message)


# DEMARRAGE
def main():

    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN est absent.")

    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL est absent.")

    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET est absent.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CommandHandler("statut", statut))

    logger.info("ALEPH BET AI démarre en mode webhook...")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="telegram",
        webhook_url=f"{WEBHOOK_URL.rstrip('/')}/telegram",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
