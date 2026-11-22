
import os
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

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
API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", "10000"))

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Africa/Abidjan"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# API FOOTBALL
def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY absente dans Render.")

    response = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"x-apisports-key": API_KEY},
        params=params or {},
        timeout=20,
    )

    response.raise_for_status()
    data = response.json()

    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))

    return data


async def api_call(endpoint, params=None):
    return await asyncio.to_thread(api_get, endpoint, params)


def today():
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


# CONNEXION API
async def test_api():
    data = await api_call("status")
    return data


# RECUPERATION DES MATCHS
async def get_matches():
    data = await api_call(
        "fixtures",
        {
            "date": today(),
            "timezone": TIMEZONE,
        },
    )
    return data.get("response", [])


def fixture_text(item):
    fixture = item.get("fixture", {})
    teams = item.get("teams", {})
    league = item.get("league", {})

    home = teams.get("home", {}).get("name", "Inconnu")
    away = teams.get("away", {}).get("name", "Inconnu")
    competition = league.get("name", "Compétition inconnue")

    date = fixture.get("date", "")
    status = fixture.get("status", {}).get("long", "")

    return (
        f"⚽ {home} - {away}\n"
        f"🏆 {competition}\n"
        f"🕒 {date}\n"
        f"📌 {status}"
    )


# ACCUEIL
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "👑 Bienvenue sur ALEPH BET AI !\n\n"
        "⚽ Assistant intelligent de pronostics sportifs.\n"
        "🎯 Objectif : 2 pistes de pronostics maximum.\n"
        "📊 Aucun pourcentage ou score inventé.\n\n"
        "Commandes :\n"
        "/start - Accueil\n"
        "/aide - Instructions\n"
        "/statut - Connexions\n"
        "/matchs - Matchs du jour\n"
        "/pronostics - Analyse des matchs"
    )

    if update.message:
        await update.message.reply_text(message)


# AIDE
async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "📖 ALEPH BET AI\n\n"
        "/matchs : consulter les matchs du jour.\n"
        "/pronostics : rechercher deux pistes.\n"
        "/statut : vérifier les connexions.\n\n"
        "Les pronostics ne garantissent aucun gain."
    )

    if update.message:
        await update.message.reply_text(message)


# STATUT
async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "🔍 Vérification des connexions..."
    )

    try:
        data = await test_api()

        account = data.get("response", [{}])[0]
        requests_info = account.get("requests", {})

        current = requests_info.get("current", "?")
        limit = requests_info.get("limit_day", "?")

        message = (
            "🤖 ALEPH BET AI\n\n"
            "Telegram : ✅ Connecté\n"
            "Serveur : ✅ Actif\n"
            "API-Football : ✅ Réponse reçue\n"
            f"Requêtes utilisées : {current}\n"
            f"Limite quotidienne : {limit}\n"
            "Moteur : 🟡 Données en test"
        )

    except Exception as error:
        logger.exception("Erreur API")
        message = (
            "🤖 ALEPH BET AI\n\n"
            "Telegram : ✅ Connecté\n"
            "Serveur : ✅ Actif\n"
            "API-Football : ❌ Non vérifiée\n"
            f"Erreur : {str(error)[:250]}"
        )

    await update.message.reply_text(message)


# MATCHS DU JOUR
async def matchs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "⚽ Recherche des matchs du jour..."
    )

    try:
        fixtures = await get_matches()

        if not fixtures:
            await update.message.reply_text(
                "Aucun match disponible aujourd'hui."
            )
            return

        lines = [
            f"⚽ MATCHS DU {today()}\n",
        ]

        for item in fixtures[:15]:
            lines.append(fixture_text(item))

        lines.append(
            "\nAffichage limité aux 15 premiers matchs."
        )

        await update.message.reply_text(
            "\n\n".join(lines)
        )

    except Exception as error:
        logger.exception("Erreur matchs")
        await update.message.reply_text(
            f"❌ Impossible de récupérer les matchs : {str(error)[:250]}"
        )


# PRONOSTICS
async def pronostics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message:
        return

    await update.message.reply_text(
        "🧠 ALEPH BET AI analyse les données disponibles..."
    )

    try:
        fixtures = await get_matches()

        upcoming = [
            item for item in fixtures
            if item.get("fixture", {})
            .get("status", {}).get("short") in ("NS", "TBD")
        ]

        if not upcoming:
            await update.message.reply_text(
                "Aucun match à venir disponible aujourd'hui."
            )
            return

        candidates = []

        # Limitation pour préserver le quota API
        for item in upcoming[:5]:
            fixture_id = item.get("fixture", {}).get("id")

            if not fixture_id:
                continue

            data = await api_call(
                "predictions",
                {"fixture": fixture_id},
            )

            results = data.get("response", [])

            if not results:
                continue

            prediction = results[0].get("predictions", {})
            advice = prediction.get("advice")
            winner = prediction.get("winner", {})
            winner_name = winner.get("name")

            if advice or winner_name:
                candidates.append({
                    "match": item,
                    "advice": advice,
                    "winner": winner_name,
                })

        if not candidates:
            await update.message.reply_text(
                "⚠️ Les matchs sont disponibles, "
                "mais les données de prédiction sont insuffisantes."
            )
            return

        lines = [
            "👑 ALEPH BET AI — ANALYSE DU JOUR",
            "",
            "📊 Pistes issues des prédictions API-Football :",
        ]

        for index, candidate in enumerate(candidates[:2], 1):
            lines.append(
                f"\n🎯 PISTE {index}\n"
                f"{fixture_text(candidate['match'])}\n"
                f"🏆 Équipe indiquée : "
                f"{candidate['winner'] or 'Non déterminée'}\n"
                f"💡 Conseil API : "
                f"{candidate['advice'] or 'Non disponible'}"
            )

        lines.append(
            "\n⚠️ Ces pistes ne constituent pas "
            "des probabilités garanties de réussite."
        )

        await update.message.reply_text(
            "\n".join(lines)
        )

    except Exception as error:
        logger.exception("Erreur pronostics")
        await update.message.reply_text(
            f"❌ Erreur d'analyse : {str(error)[:250]}"
        )


# DEMARRAGE
def main():

    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN absent.")

    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL absent.")

    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET absent.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CommandHandler("statut", statut))
    app.add_handler(CommandHandler("matchs", matchs))
    app.add_handler(CommandHandler("pronostics", pronostics))

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
