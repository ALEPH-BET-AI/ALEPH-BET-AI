
import os
import logging
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =====================================
# CONFIGURATION
# =====================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
API_KEY = os.getenv("API_FOOTBALL_KEY")

PORT = int(os.getenv("PORT", "10000"))

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Africa/Abidjan"

# Critères expérimentaux de sélection
MIN_PERCENT = 55
MIN_GAP = 8

# Limites de consommation API
MAX_PREDICTION_CALLS = 25
RESERVE_API_CALLS = 3

# Nombre maximal de pronostics affichés
MAX_DISPLAY_PICKS = 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Cache temporaire
analysis_cache = {
    "time": 0,
    "date": None,
    "result": None,
}


# =====================================
# API FOOTBALL
# =====================================

def api_get(endpoint, params=None):

    if not API_KEY:
        raise RuntimeError(
            "La clé API_FOOTBALL_KEY est absente."
        )

    headers = {
        "x-apisports-key": API_KEY
    }

    response = requests.get(
        f"{API_BASE}/{endpoint}",
        headers=headers,
        params=params or {},
        timeout=25,
    )

    response.raise_for_status()

    data = response.json()

    errors = data.get("errors")

    if errors:
        raise RuntimeError(
            f"Erreur API-Football : {errors}"
        )

    return data


async def api_call(endpoint, params=None):

    return await asyncio.to_thread(
        api_get,
        endpoint,
        params
    )


def get_response_list(data):

    result = data.get("response", [])

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        return [result]

    return []


def get_status_info(data):

    response = data.get("response", {})

    if isinstance(response, list):
        response = response[0] if response else {}

    if not isinstance(response, dict):
        response = {}

    requests_info = response.get("requests", {})

    if not isinstance(requests_info, dict):
        requests_info = {}

    current = requests_info.get("current")
    limit = requests_info.get("limit_day")

    return current, limit


def today():

    return datetime.now(
        ZoneInfo(TIMEZONE)
    ).date().isoformat()


# =====================================
# INFORMATIONS SUR LES MATCHS
# =====================================

def format_match_time(fixture):

    # CORRECTION : la date est dans fixture.date
    date_text = (
        fixture.get("fixture", {}).get("date")
    )

    if not date_text:
        return "Horaire inconnu"

    try:

        match_date = datetime.fromisoformat(
            date_text.replace("Z", "+00:00")
        )

        match_date = match_date.astimezone(
            ZoneInfo(TIMEZONE)
        )

        return match_date.strftime("%H:%M")

    except Exception:

        return "Horaire inconnu"


def is_upcoming(fixture):

    status = (
        fixture.get("fixture", {})
        .get("status", {})
        .get("short", "")
    )

    return status in ("NS", "TBD")


def get_fixture_name(fixture):

    teams = fixture.get("teams", {})

    home = teams.get(
        "home", {}
    ).get("name", "Équipe domicile")

    away = teams.get(
        "away", {}
    ).get("name", "Équipe extérieure")

    return f"{home} - {away}"


def get_league_name(fixture):

    return fixture.get(
        "league", {}
    ).get("name", "Championnat inconnu")


def parse_percent(value):

    if value is None:
        return None

    try:

        cleaned = str(value).replace("%", "").strip()

        number = float(cleaned)

        if 0 <= number <= 100:
            return number

    except (ValueError, TypeError):

        pass

    return None


# =====================================
# EXTRACTION DES ESTIMATIONS
# =====================================

def extract_prediction(prediction_data):

    predictions = prediction_data.get(
        "predictions", {}
    )

    if not isinstance(predictions, dict):
        return None

    percent = predictions.get("percent", {})

    if not isinstance(percent, dict):
        return None

    home = parse_percent(percent.get("home"))
    draw = parse_percent(percent.get("draw"))
    away = parse_percent(percent.get("away"))

    values = {
        "Domicile": home,
        "Match nul": draw,
        "Extérieur": away,
    }

    available = {
        name: value
        for name, value in values.items()
        if value is not None
    }

    if len(available) < 2:
        return None

    ranked = sorted(
        available.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    best_name = ranked[0][0]
    best_percent = ranked[0][1]
    second_percent = ranked[1][1]

    gap = best_percent - second_percent

    winner = predictions.get("winner", {})

    winner_name = (
        winner.get("name")
        if isinstance(winner, dict)
        else None
    )

    advice = predictions.get("advice")

    return {
        "choice": best_name,
        "percent": best_percent,
        "gap": gap,
        "winner_name": winner_name,
        "advice": advice,
    }


# =====================================
# MOTEUR D'ANALYSE
# =====================================

async def analyze_all_matches():

    current_date = today()

    # Réutilisation du résultat pendant 30 minutes
    if (
        analysis_cache["date"] == current_date
        and analysis_cache["result"] is not None
        and time.time() - analysis_cache["time"] < 1800
    ):

        return analysis_cache["result"]

    # Vérification du quota
    status_data = await api_call("status")

    current, limit = get_status_info(status_data)

    # Récupération de tous les matchs du jour
    fixtures_data = await api_call(
        "fixtures",
        {
            "date": current_date,
            "timezone": TIMEZONE,
        },
    )

    fixtures = get_response_list(fixtures_data)

    upcoming = [
        fixture
        for fixture in fixtures
        if is_upcoming(fixture)
    ]

    # Calcul des requêtes disponibles
    if isinstance(current, int) and isinstance(limit, int):

        remaining = max(
            0,
            limit - current - RESERVE_API_CALLS,
        )

        call_limit = min(
            MAX_PREDICTION_CALLS,
            remaining,
        )

    else:

        call_limit = 10

    candidates = []
    analyzed = 0

    upcoming.sort(
        key=lambda item: (
            item.get("fixture", {}).get("date", "")
        )
    )

    # Analyse dans la limite du quota
    for fixture in upcoming[:call_limit]:

        fixture_id = (
            fixture.get("fixture", {}).get("id")
        )

        if not fixture_id:
            continue

        try:

            data = await api_call(
                "predictions",
                {
                    "fixture": fixture_id
                },
            )

            responses = get_response_list(data)

            if not responses:
                continue

            prediction = extract_prediction(
                responses[0]
            )

            analyzed += 1

            if not prediction:
                continue

            # Filtres expérimentaux
            if prediction["percent"] < MIN_PERCENT:
                continue

            if prediction["gap"] < MIN_GAP:
                continue

            candidates.append({

                "match": get_fixture_name(fixture),

                "league": get_league_name(fixture),

                "time": format_match_time(fixture),

                "choice": prediction["choice"],

                "percent": prediction["percent"],

                "gap": prediction["gap"],

                "advice": prediction["advice"],

                "winner": prediction["winner_name"],

            })

        except Exception as error:

            logger.warning(
                "Analyse impossible pour un match : %s",
                error,
            )

    # Classement des meilleures estimations
    candidates.sort(
        key=lambda item: (
            item["percent"],
            item["gap"],
        ),
        reverse=True,
    )

    result = {

        "date": current_date,

        "total": len(fixtures),

        "upcoming": len(upcoming),

        "analyzed": analyzed,

        "quota_limit": call_limit,

        "picks": candidates[:MAX_DISPLAY_PICKS],

    }

    analysis_cache["time"] = time.time()

    analysis_cache["date"] = current_date

    analysis_cache["result"] = result

    return result


# =====================================
# ENVOI DES MESSAGES TELEGRAM
# =====================================

async def send_long_message(update, message):

    max_length = 3500

    while message:

        if len(message) <= max_length:

            await update.message.reply_text(message)

            break

        split_at = message.rfind(
            "\n",
            0,
            max_length
        )

        if split_at == -1:
            split_at = max_length

        await update.message.reply_text(
            message[:split_at]
        )

        message = message[split_at:].lstrip()


# =====================================
# COMMANDE START
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "👑 BIENVENUE SUR ALEPH BET AI !\n\n"
        "⚽ Assistant d'analyse des matchs de football.\n"
        "📊 Recherche des meilleures estimations disponibles.\n"
        "🎯 Jusqu'à 10 sélections si les données le permettent.\n"
        "🛑 Aucun pronostic forcé lorsque les critères ne sont pas atteints.\n\n"
        "Commandes :\n"
        "/matchs - Voir les matchs à venir\n"
        "/pronostics - Lancer l'analyse\n"
        "/statut - Vérifier la connexion API\n"
        "/aide - Voir les commandes\n\n"
        "⚠️ Les estimations ne garantissent jamais un résultat."
    )

    await update.message.reply_text(message)


# =====================================
# COMMANDE AIDE
# =====================================

async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "🤖 AIDE ALEPH BET AI\n\n"
        "/start : Accueil du robot\n"
        "/matchs : Matchs à venir aujourd'hui\n"
        "/pronostics : Analyse et classement des sélections\n"
        "/statut : État de l'API et quota\n\n"
        "Le robot utilise les estimations disponibles sur API-Football.\n"
        "Les seuils actuels sont expérimentaux et devront être évalués "
        "avec les résultats réels.\n\n"
        "Ne mise jamais de l'argent que tu ne peux pas te permettre "
        "de perdre."
    )

    await update.message.reply_text(message)


# =====================================
# COMMANDE STATUT
# =====================================

async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        data = await api_call("status")

        current, limit = get_status_info(data)

        message = (
            "🤖 ALEPH BET AI\n"
            "🟢 Connexion API-Football : active\n"
        )

        if current is not None and limit is not None:

            message += (
                f"📊 Requêtes utilisées : {current}\n"
                f"📦 Limite quotidienne : {limit}\n"
                f"🔋 Restantes : {max(0, limit - current)}\n"
            )

        else:

            message += (
                "ℹ️ Le quota n'a pas pu être lu automatiquement.\n"
            )

        await update.message.reply_text(message)

    except Exception as error:

        logger.exception("Erreur commande statut")

        await update.message.reply_text(
            "🔴 Impossible de joindre API-Football.\n"
            f"Détail : {str(error)[:500]}"
        )


# =====================================
# COMMANDE MATCHS
# =====================================

async def matchs(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        data = await api_call(
            "fixtures",
            {
                "date": today(),
                "timezone": TIMEZONE,
            },
        )

        fixtures = get_response_list(data)

        upcoming = [
            fixture
            for fixture in fixtures
            if is_upcoming(fixture)
        ]

        upcoming.sort(
            key=lambda item: (
                item.get("fixture", {}).get("date", "")
            )
        )

        if not upcoming:

            await update.message.reply_text(
                "⚽ Aucun match à venir trouvé pour aujourd'hui."
            )

            return

        lines = [

            f"⚽ MATCHS À VENIR — {today()}",

            f"Total de matchs à venir : {len(upcoming)}",

            "",

        ]

        for fixture in upcoming[:30]:

            lines.append(
                f"🕒 {format_match_time(fixture)}\n"
                f"🏆 {get_league_name(fixture)}\n"
                f"⚽ {get_fixture_name(fixture)}\n"
            )

        if len(upcoming) > 30:

            lines.append(
                f"\n... et {len(upcoming) - 30} autres matchs."
            )

        await send_long_message(
            update,
            "\n".join(lines),
        )

    except Exception as error:

        logger.exception("Erreur commande matchs")

        await update.message.reply_text(
            "❌ Impossible de récupérer les matchs.\n"
            f"Détail : {str(error)[:500]}"
        )


# =====================================
# COMMANDE PRONOSTICS
# =====================================

async def pronostics(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🔎 ALEPH BET AI analyse les matchs disponibles...\n"
        "Cela peut prendre quelques instants."
    )

    try:

        result = await analyze_all_matches()

        lines = [

            "👑 ALEPH BET AI — ANALYSE DU JOUR",

            f"📅 Date : {result['date']}",

            "",

            f"⚽ Matchs récupérés : {result['total']}",

            f"🕒 Matchs à venir : {result['upcoming']}",

            f"📊 Matchs analysés : {result['analyzed']}",

            "",

        ]

        if not result["picks"]:

            lines.extend([

                "🛑 Aucune sélection ne satisfait actuellement "
                "les critères expérimentaux.",

                "",

                "Le robot préfère ne rien proposer plutôt que "
                "de forcer un pronostic.",

            ])

        else:

            lines.append(
                f"🎯 {len(result['picks'])} SÉLECTION(S) RETENUE(S)"
            )

            lines.append("")

            for index, pick in enumerate(
                result["picks"],
                start=1
            ):

                lines.extend([

                    "━━━━━━━━━━━━━━",

                    f"#{index} — {pick['match']}",

                    f"🏆 {pick['league']}",

                    f"🕒 Coup d'envoi : {pick['time']}",

                    f"📌 Issue favorite : {pick['choice']}",

                    f"📊 Estimation API : {pick['percent']:.0f}%",

                    f"📏 Écart avec la 2e issue : {pick['gap']:.0f} points",

                ])

                if pick["advice"]:

                    lines.append(
                        f"💡 Conseil API : {pick['advice']}"
                    )

                lines.append("")

        lines.extend([

            "━━━━━━━━━━━━━━",

            f"🔋 Analyses autorisées par le quota : "
            f"{result['quota_limit']}",

            "",

            "⚠️ Pourcentages fournis par API-Football, "
            "pas des garanties de réussite.",

            "Les seuils de sélection sont expérimentaux "
            "et doivent être vérifiés avec les résultats réels.",

        ])

        await send_long_message(
            update,
            "\n".join(lines),
        )

    except Exception as error:

        logger.exception("Erreur commande pronostics")

        await update.message.reply_text(
            "❌ Une erreur est survenue pendant l'analyse.\n"
            f"Détail : {str(error)[:500]}"
        )


# =====================================
# DEMARRAGE DU BOT
# =====================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN manquant."
        )

    if not WEBHOOK_URL:
        raise RuntimeError(
            "WEBHOOK_URL manquant."
        )

    if not WEBHOOK_SECRET:
        raise RuntimeError(
            "WEBHOOK_SECRET manquant."
        )

    if not API_KEY:
        raise RuntimeError(
            "API_FOOTBALL_KEY manquant."
        )

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("aide", aide)
    )

    app.add_handler(
        CommandHandler("statut", statut)
    )

    app.add_handler(
        CommandHandler("matchs", matchs)
    )

    app.add_handler(
        CommandHandler("pronostics", pronostics)
    )

    logger.info(
        "Démarrage de ALEPH BET AI..."
    )

    app.run_webhook(

        listen="0.0.0.0",

        port=PORT,

        url_path="telegram",

        webhook_url=(
            f"{WEBHOOK_URL.rstrip('/')}/telegram"
        ),

        secret_token=WEBHOOK_SECRET,

        allowed_updates=Update.ALL_TYPES,

    )


if __name__ == "__main__":

    main()
