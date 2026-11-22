import os
import logging
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# Seuils expérimentaux
MIN_WIN_PERCENT = 65.0
MIN_DOUBLE_CHANCE_PERCENT = 78.0

# Contrôle qualité des probabilités
MIN_1X2_SUM = 97.0
MAX_1X2_SUM = 103.0
MAX_ACCEPTED_MARKET_PERCENT = 98.9  # Les valeurs proches de 100 % sont écartées

# Quota API
MAX_PREDICTION_CALLS = 25
RESERVE_API_CALLS = 3

# Maximum 3 sélections ; aucune obligation d'en produire 2
MAX_DISPLAY_PICKS = 3

# Diversification : éviter deux sélections du même championnat
ONE_PICK_PER_LEAGUE = True

# Cache
CACHE_DURATION = 1800

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

analysis_cache = {"time": 0, "date": None, "result": None}


# =====================================
# API
# =====================================

def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("Clé API absente.")

    response = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"x-apisports-key": API_KEY},
        params=params or {},
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))

    return data


async def api_call(endpoint, params=None):
    return await asyncio.to_thread(api_get, endpoint, params)


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

    current = requests_info.get("current")
    limit = requests_info.get("limit_day")

    try:
        current = int(current) if current is not None else None
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        return None, None

    return current, limit


def today():
    return datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()


# =====================================
# MATCHS
# =====================================

def format_match_time(fixture):
    date_text = fixture.get("fixture", {}).get("date")
    if not date_text:
        return "Inconnue"

    try:
        match_date = datetime.fromisoformat(
            date_text.replace("Z", "+00:00")
        ).astimezone(ZoneInfo(TIMEZONE))
        return match_date.strftime("%H:%M")
    except (ValueError, TypeError):
        return "Inconnue"


def is_upcoming(fixture):
    status = fixture.get("fixture", {}).get("status", {}).get("short", "")
    return status in ("NS", "TBD")


def get_fixture_name(fixture):
    teams = fixture.get("teams", {})
    home = teams.get("home", {}).get("name", "Domicile")
    away = teams.get("away", {}).get("name", "Extérieur")
    return f"{home} - {away}"


def get_league_name(fixture):
    return fixture.get("league", {}).get("name", "Championnat inconnu")


def parse_percent(value):
    if value is None:
        return None
    try:
        number = float(str(value).replace("%", "").strip())
        if 0 <= number <= 100:
            return number
    except (ValueError, TypeError):
        pass
    return None


def spread_fixtures(fixtures, count):
    """Échantillonne les horaires sur toute la journée, sans analyser seulement le début."""
    if count <= 0:
        return []
    if len(fixtures) <= count:
        return fixtures

    if count == 1:
        return [fixtures[len(fixtures) // 2]]

    indexes = [
        round(i * (len(fixtures) - 1) / (count - 1))
        for i in range(count)
    ]

    # Dédupliquer les index tout en conservant l'ordre
    chosen = []
    seen = set()
    for index in indexes:
        if index not in seen:
            chosen.append(fixtures[index])
            seen.add(index)

    return chosen


# =====================================
# VALIDATION ET MARCHÉS
# =====================================

def extract_markets(prediction_data):
    predictions = prediction_data.get("predictions", {})
    if not isinstance(predictions, dict):
        return {"markets": [], "reason": "Bloc prédictions absent"}

    percent = predictions.get("percent", {})
    if not isinstance(percent, dict):
        return {"markets": [], "reason": "Pourcentages absents"}

    home = parse_percent(percent.get("home"))
    draw = parse_percent(percent.get("draw"))
    away = parse_percent(percent.get("away"))

    if home is None or draw is None or away is None:
        return {"markets": [], "reason": "Pourcentages 1N2 incomplets"}

    total = home + draw + away
    if not (MIN_1X2_SUM <= total <= MAX_1X2_SUM):
        return {
            "markets": [],
            "reason": f"Somme 1N2 incohérente ({total:.1f} %)",
        }

    markets = []

    # Victoires simples
    if home >= MIN_WIN_PERCENT:
        markets.append({
            "market": "Victoire domicile",
            "selection": "1",
            "percent": home,
            "type": "victoire",
        })

    if away >= MIN_WIN_PERCENT:
        markets.append({
            "market": "Victoire extérieure",
            "selection": "2",
            "percent": away,
            "type": "victoire",
        })

    # Le nul est conservé uniquement s'il franchit le seuil expérimental
    if draw >= MIN_WIN_PERCENT:
        markets.append({
            "market": "Match nul",
            "selection": "N",
            "percent": draw,
            "type": "nul",
        })

    # Doubles chances, plafonnées à 100 pour éviter un dépassement dû aux arrondis
    chance_1x = min(100.0, home + draw)
    chance_x2 = min(100.0, away + draw)
    chance_12 = min(100.0, home + away)

    if chance_1x >= MIN_DOUBLE_CHANCE_PERCENT:
        markets.append({
            "market": "Double chance",
            "selection": "1X",
            "percent": chance_1x,
            "type": "double",
        })

    if chance_x2 >= MIN_DOUBLE_CHANCE_PERCENT:
        markets.append({
            "market": "Double chance",
            "selection": "X2",
            "percent": chance_x2,
            "type": "double",
        })

    if chance_12 >= MIN_DOUBLE_CHANCE_PERCENT:
        markets.append({
            "market": "Double chance",
            "selection": "12",
            "percent": chance_12,
            "type": "double",
        })

    # Écarter les valeurs extrêmes : elles ne sont pas traitées comme des certitudes.
    markets = [
        market for market in markets
        if market["percent"] <= MAX_ACCEPTED_MARKET_PERCENT
    ]

    markets.sort(key=lambda item: item["percent"], reverse=True)

    # Données buts : on conserve uniquement les champs réellement fournis par l'API.
    under_over = predictions.get("under_over")
    goals = predictions.get("goals", {})
    if not isinstance(goals, dict):
        goals = {}

    advice = predictions.get("advice")

    return {
        "markets": markets,
        "reason": None,
        "under_over": under_over,
        "goal_home": goals.get("home"),
        "goal_away": goals.get("away"),
        "advice": advice,
        "home_percent": home,
        "draw_percent": draw,
        "away_percent": away,
        "sum_percent": total,
    }


# =====================================
# MOTEUR ALEPH BET AI 3.1
# =====================================

async def analyze_all_matches():
    current_date = today()

    if (
        analysis_cache["date"] == current_date
        and analysis_cache["result"] is not None
        and time.time() - analysis_cache["time"] < CACHE_DURATION
    ):
        return analysis_cache["result"]

    status_data = await api_call("status")
    current, limit = get_status_info(status_data)

    fixtures_data = await api_call(
        "fixtures",
        {"date": current_date, "timezone": TIMEZONE},
    )
    fixtures = get_response_list(fixtures_data)

    upcoming = [fixture for fixture in fixtures if is_upcoming(fixture)]
    upcoming.sort(
        key=lambda item: item.get("fixture", {}).get("date", "")
    )

    if isinstance(current, int) and isinstance(limit, int):
        remaining = max(0, limit - current - RESERVE_API_CALLS)
        call_limit = min(MAX_PREDICTION_CALLS, remaining)
    else:
        call_limit = min(10, MAX_PREDICTION_CALLS)

    fixtures_to_analyze = spread_fixtures(upcoming, call_limit)

    candidates = []
    attempts = 0
    analyzed = 0
    no_prediction = 0
    invalid_data = 0
    api_errors = 0

    for fixture in fixtures_to_analyze:
        fixture_id = fixture.get("fixture", {}).get("id")
        if not fixture_id:
            continue

        attempts += 1

        try:
            data = await api_call("predictions", {"fixture": fixture_id})
            responses = get_response_list(data)

            if not responses:
                no_prediction += 1
                continue

            analyzed += 1
            prediction = extract_markets(responses[0])

            if prediction.get("reason"):
                invalid_data += 1
                logger.info(
                    "Données non retenues fixture %s : %s",
                    fixture_id,
                    prediction["reason"],
                )
                continue

            if not prediction["markets"]:
                invalid_data += 1
                continue

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
                "under_over": prediction.get("under_over"),
                "goal_home": prediction.get("goal_home"),
                "goal_away": prediction.get("goal_away"),
                "advice": prediction.get("advice"),
                "home_percent": prediction["home_percent"],
                "draw_percent": prediction["draw_percent"],
                "away_percent": prediction["away_percent"],
                "sum_percent": prediction["sum_percent"],
            })

        except Exception as error:
            api_errors += 1
            logger.warning(
                "Erreur analyse match %s : %s",
                fixture_id,
                error,
            )

    candidates.sort(key=lambda item: item["percent"], reverse=True)

    selected = []
    used_fixtures = set()
    used_leagues = set()

    for candidate in candidates:
        if candidate["fixture_id"] in used_fixtures:
            continue

        if ONE_PICK_PER_LEAGUE and candidate["league"] in used_leagues:
            continue

        selected.append(candidate)
        used_fixtures.add(candidate["fixture_id"])
        used_leagues.add(candidate["league"])

        if len(selected) >= MAX_DISPLAY_PICKS:
            break

    result = {
        "date": current_date,
        "total": len(fixtures),
        "upcoming": len(upcoming),
        "attempts": attempts,
        "analyzed": analyzed,
        "no_prediction": no_prediction,
        "invalid_data": invalid_data,
        "api_errors": api_errors,
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

        split_at = message.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length

        await update.message.reply_text(message[:split_at])
        message = message[split_at:].lstrip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "👑 BIENVENUE SUR ALEPH BET AI 3.1 !\n\n"
        "⚽ Assistant d'analyse football.\n"
        "🎯 Maximum 3 sélections, sans obligation d'en proposer.\n"
        "📊 Victoire et double chance en priorité.\n"
        "⚽ Indications de buts uniquement si l'API les fournit.\n"
        "🛑 Contrôle des probabilités extrêmes et incohérentes.\n\n"
        "COMMANDES :\n"
        "/matchs - Matchs à venir\n"
        "/pronostics - Analyse approfondie\n"
        "/statut - Vérifier API et quota\n"
        "/aide - Aide\n\n"
        "⚠️ Une estimation n'est jamais une garantie de gain."
    )
    await update.message.reply_text(message)


async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "🤖 ALEPH BET AI 3.1\n\n"
        "/start : Accueil\n"
        "/matchs : Matchs à venir\n"
        "/pronostics : Meilleures sélections\n"
        "/statut : Connexion et quota\n\n"
        "MARCHÉS :\n"
        "1 : Victoire domicile\n"
        "N : Match nul\n"
        "2 : Victoire extérieure\n"
        "1X : Domicile ou nul\n"
        "X2 : Extérieur ou nul\n"
        "12 : Une des deux équipes gagne\n\n"
        "Maximum 3 matchs par rapport.\n"
        "Les probabilités sont des estimations API, pas des taux "
        "de réussite historiques."
    )
    await update.message.reply_text(message)


async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call("status")
        current, limit = get_status_info(data)

        message = (
            "🤖 ALEPH BET AI 3.1\n"
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


async def matchs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call(
            "fixtures",
            {"date": today(), "timezone": TIMEZONE},
        )
        fixtures = get_response_list(data)
        upcoming = [fixture for fixture in fixtures if is_upcoming(fixture)]
        upcoming.sort(
            key=lambda item: item.get("fixture", {}).get("date", "")
        )

        if not upcoming:
            await update.message.reply_text(
                "⚽ Aucun match à venir aujourd'hui."
            )
            return

        lines = [
            f"⚽ MATCHS À VENIR — {today()}",
            f"Total : {len(upcoming)}",
            "",
        ]

        for fixture in upcoming[:30]:
            lines.append(
                f"🕒 {format_match_time(fixture)}\n"
                f"🏆 {get_league_name(fixture)}\n"
                f"⚽ {get_fixture_name(fixture)}\n"
            )

        if len(upcoming) > 30:
            lines.append(f"... et {len(upcoming) - 30} autres matchs.")

        await send_long_message(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur matchs")
        await update.message.reply_text(
            f"❌ Erreur récupération matchs : {str(error)[:400]}"
        )


async def pronostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👑 ALEPH BET AI 3.1\n"
        "🔎 Analyse des matchs répartis sur la journée...\n"
        "🛡️ Vérification des probabilités et des données."
    )

    try:
        result = await analyze_all_matches()

        lines = [
            "👑 ALEPH BET AI 3.1 — RAPPORT DU JOUR",
            f"📅 Date : {result['date']}",
            "",
            f"⚽ Matchs récupérés : {result['total']}",
            f"🕒 Matchs à venir : {result['upcoming']}",
            f"🔍 Tentatives d'analyse : {result['attempts']}",
            f"📊 Réponses API reçues : {result['analyzed']}",
            f"📭 Sans prédiction : {result['no_prediction']}",
            f"🧹 Données écartées / marchés insuffisants : {result['invalid_data']}",
            f"⚠️ Erreurs API : {result['api_errors']}",
            "",
        ]

        if not result["picks"]:
            lines.extend([
                "🛑 Aucune sélection suffisamment solide.",
                "Le robot préfère ne pas proposer de pari fragile.",
            ])
        else:
            lines.extend([
                f"🎯 {len(result['picks'])} SÉLECTION(S) RETENUE(S)",
                "",
            ])

            for index, pick in enumerate(result["picks"], start=1):
                lines.extend([
                    "━━━━━━━━━━━━━━",
                    f"🏅 SÉLECTION #{index}",
                    f"⚽ {pick['match']}",
                    f"🏆 {pick['league']}",
                    f"🕒 Heure : {pick['time']}",
                    "",
                    f"📌 Marché : {pick['market']}",
                    f"🎯 Choix : {pick['selection']}",
                    f"📊 Estimation API : {pick['percent']:.1f} %",
                    (
                        "📈 Base 1N2 API : "
                        f"1={pick['home_percent']:.1f}% | "
                        f"N={pick['draw_percent']:.1f}% | "
                        f"2={pick['away_percent']:.1f}%"
                    ),
                ])

                if pick["under_over"] not in (None, "", "0"):
                    lines.append(
                        f"⚽ Ligne buts indiquée par l'API : {pick['under_over']}"
                    )

                if pick["advice"]:
                    lines.append(f"💡 Conseil API : {pick['advice']}")

                lines.append("")

        lines.extend([
            "━━━━━━━━━━━━━━",
            f"🔋 Appels de prédiction autorisés : {result['quota_limit']}",
            "",
            "ℹ️ Les doubles chances sont calculées à partir des estimations 1N2.",
            "ℹ️ Les marchés proches de 100 % sont écartés par prudence.",
            "ℹ️ Les indications de buts ne sont pas converties en probabilités.",
            "",
            "⚠️ Estimations API uniquement : aucun taux de réussite historique "
            "n'est encore calculé.",
        ])

        await send_long_message(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur pronostics")
        await update.message.reply_text(
            "❌ Erreur pendant l'analyse.\n"
            f"{str(error)[:400]}"
        )


# =====================================
# DÉMARRAGE
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

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CommandHandler("statut", statut))
    app.add_handler(CommandHandler("matchs", matchs))
    app.add_handler(CommandHandler("pronostics", pronostics))

    logger.info("ALEPH BET AI 3.1 démarre...")

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
