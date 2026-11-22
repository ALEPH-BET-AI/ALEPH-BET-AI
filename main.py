import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

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
    await update.message.reply_text(message)

async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 ALEPH BET AI\n"
        "/start - Accueil\n"
        "/aide - Aide\n"
        "/statut - État du robot\n\n"
        "Les analyses seront activées après la connexion aux données sportives."
    )

async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ALEPH BET AI\n"
        "Bot Telegram : opérationnel si ce message apparaît.\n"
        "Moteur de pronostics : en configuration.\n"
        "Données des matchs : à connecter."
    )

def main():
    if not TOKEN:
        raise RuntimeError("La variable BOT_TOKEN est absente.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CommandHandler("statut", statut))
    print("ALEPH BET AI démarre...")
    app.run_polling()

if __name__ == "__main__":
    main()
