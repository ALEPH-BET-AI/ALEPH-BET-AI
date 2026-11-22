
import os
import asyncio
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# COMMANDE START
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


# COMMANDE AIDE
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


# COMMANDE STATUT
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


# DEMARRAGE DU BOT
async def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN est absent.")

    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL est absent.")

    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET est absent.")

    app = (
        Application.builder()
        .token(TOKEN)
        .updater(None)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CommandHandler("statut", statut))

    logger.info("ALEPH BET AI démarre en mode webhook...")

    await app.initialize()
    await app.start()

    await app.bot.set_webhook(
        url=f"{WEBHOOK_URL.rstrip('/')}/telegram",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
    )

    from telegram.ext import TypeHandler

    async def process_update(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ):
        await app.process_update(update)

    async def health(request):
        from aiohttp import web
        return web.Response(text="ALEPH BET AI OK")

    async def telegram_webhook(request):
        from aiohttp import web

        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")

        if secret != WEBHOOK_SECRET:
            return web.Response(status=403, text="Forbidden")

        try:
            data = await request.json()
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
            return web.Response(text="OK")
        except Exception:
            logger.exception("Erreur lors du traitement Telegram")
            return web.Response(status=500, text="Internal Server Error")

    from aiohttp import web

    server = web.Application()
    server.router.add_get("/", health)
    server.router.add_post("/telegram", telegram_webhook)

    runner = web.AppRunner(server)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info("ALEPH BET AI est opérationnel sur le port %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
