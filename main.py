
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

# SEUILS EXPERIMENTAUX
MIN_WIN_PERCENT = 65
MIN_DOUBLE_CHANCE_PERCENT = 78

# QUOTA API
MAX_PREDICTION_CALLS = 25
RESERVE_API_CALLS = 3

# OBJECTIF : 2 OU 3 MATCHS MAXIMUM
MAX_DISPLAY_PICKS = 3
MIN_DISPLAY_PICKS = 2

# Cache de 30 minutes
CACHE_DURATION = 1800

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

analysis_cache = {
    "time": 0,
    "date": None,
    "result": None,
}


# =====================================
# CONNEXION API
# =====================================

def api_get(endpoint, params=None):

    if not API_KEY:
        raise RuntimeError("Clé API absente.")

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

    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))

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
        return None, None

    requests_info = response.get("requests", {})

    if not isinstance(requests_info, dict):
        return None, None

    return (
        requests_info.get("current"),
        requests_info.get("limit_day")
    )


def today():

    return datetime.now(
        ZoneInfo(TIMEZONE)
    ).date().isoformat()


# =====================================
# INFORMATIONS MATCHS
# =====================================

def format_match_time(fixture):

    date_text = fixture.get(
        "fixture", {}
    ).get("date")

    if not date_text:
        return "Inconnue"

    try:

        match_date = datetime.fromisoformat(
            date_text.replace("Z", "+00:00")
        )

        match_date = match_date.astimezone(
            ZoneInfo(TIMEZONE)
        )

        return match_date.strftime("%H:%M")

    except Exception:

        return "Inconnue"


def is_upcoming(fixture):

    status = fixture.get(
        "fixture", {}
    ).get(
        "status", {}
    ).get("short", "")

    return status in ("NS", "TBD")


def get_fixture_name(fixture):

    teams = fixture.get("teams", {})

    home = teams.get(
        "home", {}
    ).get("name", "Domicile")

    away = teams.get(
        "away", {}
    ).get("name", "Extérieur")

    return f"{home} - {away}"


def get_league_name(fixture):

    return fixture.get(
        "league", {}
    ).get("name", "Championnat inconnu")


def parse_percent(value):

    if value is None:
        return None

    try:

        number = float(
            str(value).replace("%", "").strip()
        )

        if 0 <= number <= 100:
            return number

    except (ValueError, TypeError):
        pass

    return None


# =====================================
# ANALYSE DES MARCHES
# =====================================

def extract_markets(prediction_data):

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

    if home is None or draw is None or away is None:
        return None

    markets = []

    # VICTOIRE DOMICILE
    if home >= MIN_WIN_PERCENT:

        markets.append({
            "market": "Victoire domicile",
            "selection": "1",
            "percent": home,
            "type": "victoire",
        })

    # VICTOIRE EXTERIEURE
    if away >= MIN_WIN_PERCENT:

        markets.append({
            "market": "Victoire extérieure",
            "selection": "2",
            "percent": away,
            "type": "victoire",
        })

    # MATCH NUL
    if draw >= MIN_WIN_PERCENT:

        markets.append({
            "market": "Match nul",
            "selection": "N",
            "percent": draw,
            "type": "victoire",
        })

    # DOUBLE CHANCE 1X
    chance_1x = home + draw

    if chance_1x >= MIN_DOUBLE_CHANCE_PERCENT:

        markets.append({
            "market": "Double chance",
            "selection": "1X",
            "percent": chance_1x,
            "type": "double",
        })

    # DOUBLE CHANCE X2
    chance_x2 = away + draw

    if chance_x2 >= MIN_DOUBLE_CHANCE_PERCENT:

        markets.append({
            "market": "Double chance",
            "selection": "X2",
            "percent": chance_x2,
            "type": "double",
        })

    # DOUBLE CHANCE 12
    chance_12 = home + away

    if chance_12 >= MIN_DOUBLE_CHANCE_PERCENT:

        markets.append({
            "market": "Double chance",
            "selection": "12",
            "percent": chance_12,
            "type": "double",
        })

    # ORIENTATION SUR LES BUTS
    under_over = predictions.get("under_over")

    goals = predictions.get("goals", {})

    if not isinstance(goals, dict):
        goals = {}

    goal_home = goals.get("home")
    goal_away = goals.get("away")

    advice = predictions.get("advice")

    # Classement des marchés ayant un pourcentage
    markets.sort(
        key=lambda item: item["percent"],
        reverse=True
    )

    return {
        "markets": markets,
        "under_over": under_over,
        "goal_home": goal_home,
        "goal_away": goal_away,
        "advice": advice,
        "home_percent": home,
        "draw_percent": draw,
        "away_percent": away,
    }


# =====================================
# MOTEUR ALEPH BET AI
# =====================================

async def analyze_all_matches():

    current_date = today()

    # CACHE
    if (
        analysis_cache["date"] == current_date
        and analysis_cache["result"] is not None
        and time.time() - analysis_cache["time"] < CACHE_DURATION
    ):

        return analysis_cache["result"]

    # VERIFICATION QUOTA
    status_data = await api_call("status")

    current, limit = get_status_info(status_data)

    # RECUPERATION MATCHS
    fixtures_data = await api_call(
        "fixtures",
        {
            "date": current_date,
            "timezone": TIMEZONE,
        }
    )

    fixtures = get_response_list(fixtures_data)

    upcoming = [
        fixture
        for fixture in fixtures
        if is_upcoming(fixture)
    ]

    upcoming.sort(
        key=lambda item: item.get(
            "fixture", {}
        ).get("date", "")
    )

    # CALCUL DU QUOTA
    if isinstance(current, int) and isinstance(limit, int):

        remaining = max(
            0,
            limit - current - RESERVE_API_CALLS
        )

        call_limit = min(
            MAX_PREDICTION_CALLS,
            remaining
        )

    else:

        call_limit = 10

    candidates = []

    attempts = 0
    analyzed = 0
    no_prediction = 0

    # ANALYSE DES MATCHS
    for fixture in upcoming[:call_limit]:

        fixture_id = fixture.get(
            "fixture", {}
        ).get("id")

        if not fixture_id:
            continue

        attempts += 1

        try:

            data = await api_call(
                "predictions",
                {
                    "fixture": fixture_id
                }
            )

            responses = get_response_list(data)

            if not responses:
                no_prediction += 1
                continue

            prediction = extract_markets(
                responses[0]
            )

            analyzed += 1

            if not prediction:
                continue

            if not prediction["markets"]:
                continue

            # UN SEUL MARCHE PRINCIPAL PAR MATCH
            best_market = prediction["markets"][0]

            candidates.append({

                "fixture_id": fixture_id,

                "match": get_fixture_name(fixture),

                "league": get_league_name(fixture),

                "time": format_match_time(fixture),

                "market": best_market["market"],

                "selection": best_market["selection"],

                "percent": best_market["percent"],

                "market_type": best_market["type"],

                "under_over": prediction["under_over"],

                "goal_home": prediction["goal_home"],

                "goal_away": prediction["goal_away"],

                "advice": prediction["advice"],

                "home_percent": prediction["home_percent"],

                "draw_percent": prediction["draw_percent"],

                "away_percent": prediction["away_percent"],

            })

        except Exception as error:

            logger.warning(
                "Erreur analyse match %s : %s",
                fixture_id,
                error
            )

    # CLASSEMENT
    candidates.sort(
        key=lambda item: item["percent"],
        reverse=True
    )

    # EVITER PLUSIEURS MATCHS DU MEME CHAMPIONNAT
    selected = []
    used_fixtures = set()

    for candidate in candidates:

        if candidate["fixture_id"] in used_fixtures:
            continue

        selected.append(candidate)

        used_fixtures.add(
            candidate["fixture_id"]
        )

        if len(selected) >= MAX_DISPLAY_PICKS:
            break

    result = {

        "date": current_date,

        "total": len(fixtures),

        "upcoming": len(upcoming),

        "attempts": attempts,

        "analyzed": analyzed,

        "no_prediction": no_prediction,

        "quota_limit": call_limit,

        "picks": selected,

    }

    analysis_cache["time"] = time.time()
    analysis_cache["date"] = current_date
    analysis_cache["result"] = result

    return result


# =====================================
# MESSAGES TELEGRAM
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
# START
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "👑 BIENVENUE SUR ALEPH BET AI 3.0 !\n\n"
        "⚽ Assistant intelligent d'analyse football.\n"
        "🎯 Objectif : 2 à 3 meilleures sélections maximum.\n"
        "📊 Victoire, double chance et buts.\n"
        "🔍 Analyse des probabilités disponibles.\n"
        "🛑 Aucun pronostic forcé.\n\n"
        "COMMANDES :\n"
        "/matchs - Matchs à venir\n"
        "/pronostics - Analyse approfondie\n"
        "/statut - Vérifier API et quota\n"
        "/aide - Aide\n\n"
        "⚠️ Les probabilités ne garantissent jamais un gain."
    )

    await update.message.reply_text(message)


# =====================================
# AIDE
# =====================================

async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "🤖 ALEPH BET AI 3.0\n\n"
        "/start : Accueil\n"
        "/matchs : Matchs à venir\n"
        "/pronostics : Meilleures sélections\n"
        "/statut : Connexion et quota\n\n"
        "MARCHES ANALYSES :\n"
        "1 : Victoire domicile\n"
        "N : Match nul\n"
        "2 : Victoire extérieure\n"
        "1X : Domicile ou nul\n"
        "X2 : Extérieur ou nul\n"
        "12 : Une des deux équipes gagne\n"
        "Buts : Orientation Over / Under\n\n"
        "Maximum 3 matchs par analyse.\n"
        "Les seuils sont expérimentaux et doivent être "
        "évalués sur les résultats réels."
    )

    await update.message.reply_text(message)


# =====================================
# STATUT
# =====================================

async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        data = await api_call("status")

        current, limit = get_status_info(data)

        message = (
            "🤖 ALEPH BET AI 3.0\n"
            "🟢 Connexion API-Football : active\n"
        )

        if current is not None and limit is not None:

            message += (
                f"📊 Requêtes utilisées : {current}\n"
                f"📦 Limite quotidienne : {limit}\n"
                f"🔋 Restantes : {max(0, limit - current)}\n"
            )

        await update.message.reply_text(message)

    except Exception as error:

        logger.exception("Erreur statut")

        await update.message.reply_text(
            "🔴 Connexion API impossible.\n"
            f"{str(error)[:400]}"
        )


# =====================================
# MATCHS
# =====================================

async def matchs(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:

        data = await api_call(
            "fixtures",
            {
                "date": today(),
                "timezone": TIMEZONE,
            }
        )

        fixtures = get_response_list(data)

        upcoming = [
            fixture
            for fixture in fixtures
            if is_upcoming(fixture)
        ]

        upcoming.sort(
            key=lambda item: item.get(
                "fixture", {}
            ).get("date", "")
        )

        if not upcoming:

            await update.message.reply_text(
                "⚽ Aucun match à venir aujourd'hui."
            )

            return

        lines = [
            f"⚽ MATCHS À VENIR — {today()}",
            f"Total : {len(upcoming)}",
            ""
        ]

        for fixture in upcoming[:30]:

            lines.append(
                f"🕒 {format_match_time(fixture)}\n"
                f"🏆 {get_league_name(fixture)}\n"
                f"⚽ {get_fixture_name(fixture)}\n"
            )

        if len(upcoming) > 30:

            lines.append(
                f"... et {len(upcoming) - 30} autres matchs."
            )

        await send_long_message(
            update,
            "\n".join(lines)
        )

    except Exception as error:

        logger.exception("Erreur matchs")

        await update.message.reply_text(
            f"❌ Erreur récupération matchs : {str(error)[:400]}"
        )


# =====================================
# PRONOSTICS
# =====================================

async def pronostics(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👑 ALEPH BET AI 3.0\n"
        "🔎 Analyse des meilleures opportunités...\n"
        "📊 Victoire, double chance et buts.\n"
        "🎯 Objectif : 2 à 3 matchs maximum."
    )

    try:

        result = await analyze_all_matches()

        lines = [

            "👑 ALEPH BET AI — RAPPORT DU JOUR",

            f"📅 Date : {result['date']}",

            "",

            f"⚽ Matchs récupérés : {result['total']}",

            f"🕒 Matchs à venir : {result['upcoming']}",

            f"🔍 Tentatives d'analyse : {result['attempts']}",

            f"📊 Réponses analysées : {result['analyzed']}",

            f"📭 Sans prédiction disponible : {result['no_prediction']}",

            "",

        ]

        if not result["picks"]:

            lines.extend([

                "🛑 Aucune sélection suffisamment solide.",

                "Le robot préfère ne pas proposer de pari fragile.",

            ])

        else:

            lines.append(
                f"🎯 {len(result['picks'])} MEILLEURE(S) SÉLECTION(S)"
            )

            lines.append("")

            for index, pick in enumerate(
                result["picks"],
                start=1
            ):

                lines.extend([

                    "━━━━━━━━━━━━━━",

                    f"🏅 SÉLECTION #{index}",

                    f"⚽ {pick['match']}",

                    f"🏆 {pick['league']}",

                    f"🕒 Heure : {pick['time']}",

                    "",

                    f"📌 Marché : {pick['market']}",

                    f"🎯 Choix : {pick['selection']}",

                    f"📊 Probabilité estimée : {pick['percent']:.1f} %",

                ])

                if pick["under_over"]:

                    lines.append(
                        f"⚽ Orientation buts API : {pick['under_over']}"
                    )

                if pick["goal_home"] is not None:

                    lines.append(
                        f"🏠 Buts domicile (ligne API) : {pick['goal_home']}"
                    )

                if pick["goal_away"] is not None:

                    lines.append(
                        f"✈️ Buts extérieur (ligne API) : {pick['goal_away']}"
                    )

                if pick["advice"]:

                    lines.append(
                        f"💡 Conseil API : {pick['advice']}"
                    )

                lines.append("")

        lines.extend([

            "━━━━━━━━━━━━━━",

            f"🔋 Appels de prédiction autorisés : {result['quota_limit']}",

            "",

            "ℹ️ Les probabilités victoire/nul/défaite proviennent "
            "d'API-Football.",

            "ℹ️ Les probabilités double chance sont calculées "
            "à partir de ces estimations.",

            "ℹ️ Les indications de buts sont celles de l'API : "
            "aucun pourcentage de réussite n'est inventé.",

            "",

            "⚠️ Une probabilité estimée n'est pas un taux de "
            "réussite historique vérifié.",

        ])

        await send_long_message(
            update,
            "\n".join(lines)
        )

    except Exception as error:

        logger.exception("Erreur pronostics")

        await update.message.reply_text(
            "❌ Erreur pendant l'analyse.\n"
            f"{str(error)[:400]}"
        )


# =====================================
# DEMARRAGE
# =====================================

def main():

    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant.")

    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL manquant.")

    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET manquant.")

    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY manquant.")

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CommandHandler("statut", statut))
    app.add_handler(CommandHandler("matchs", matchs))
    app.add_handler(CommandHandler("pronostics", pronostics))

    logger.info("ALEPH BET AI 3.0 démarre...")

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
