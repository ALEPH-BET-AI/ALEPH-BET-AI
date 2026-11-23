
import os
import logging
import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =====================================
# ALEPH BET AI 3.2
# =====================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", "10000"))

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Africa/Abidjan"

MIN_WIN_PERCENT = 65.0
MIN_DOUBLE_CHANCE_PERCENT = 78.0

MIN_1X2_SUM = 97.0
MAX_1X2_SUM = 103.0
MAX_ACCEPTED_MARKET_PERCENT = 98.9

MAX_PREDICTION_CALLS = 12
MAX_ODDS_CHECKS = 8
MAX_FINAL_CHECKS = 3
RESERVE_API_CALLS = 5

MAX_DISPLAY_PICKS = 3
ONE_PICK_PER_LEAGUE = True

CACHE_DURATION = 1800
MAX_FIXTURE_AGE_SECONDS = 0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Évite notamment d'afficher le token Telegram dans les logs HTTP.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

analysis_cache = {
    "time": 0,
    "date": None,
    "result": None,
}


# =====================================
# HORLOGE
# =====================================

def now_local():
    return datetime.now(ZoneInfo(TIMEZONE))


def today():
    return now_local().date().isoformat()


def parse_fixture_date(fixture):
    date_text = fixture.get("fixture", {}).get("date")

    if not date_text:
        return None

    try:
        parsed = datetime.fromisoformat(
            date_text.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(ZoneInfo(TIMEZONE))

    except (ValueError, TypeError):
        return None


def format_match_date(fixture):
    match_date = parse_fixture_date(fixture)

    if not match_date:
        return "Date inconnue"

    return match_date.strftime("%d/%m/%Y")


def format_match_time(fixture):
    match_date = parse_fixture_date(fixture)

    if not match_date:
        return "Heure inconnue"

    return match_date.strftime("%H:%M")


def is_upcoming(fixture):
    """
    Seuls les matchs NS sont acceptés.
    TBD, LIVE, HT, FT, reportés et annulés sont exclus.
    """

    status = (
        fixture.get("fixture", {})
        .get("status", {})
        .get("short", "")
    )

    if status != "NS":
        return False

    kickoff = parse_fixture_date(fixture)

    if not kickoff:
        return False

    return kickoff > now_local()


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


# =====================================
# INFORMATIONS MATCH
# =====================================

def get_fixture_name(fixture):
    teams = fixture.get("teams", {})

    home = teams.get("home", {}).get("name", "Domicile")
    away = teams.get("away", {}).get("name", "Extérieur")

    return f"{home} - {away}"


def get_league_name(fixture):
    return fixture.get("league", {}).get(
        "name",
        "Championnat inconnu"
    )


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

    chosen = []
    seen = set()

    for index in indexes:
        if index not in seen:
            chosen.append(fixtures[index])
            seen.add(index)

    return chosen


# =====================================
# ANALYSE DES PRONOSTICS
# =====================================

def extract_markets(prediction_data):
    predictions = prediction_data.get("predictions", {})

    if not isinstance(predictions, dict):
        return {"markets": [], "reason": "Prédictions absentes"}

    percent = predictions.get("percent", {})

    if not isinstance(percent, dict):
        return {"markets": [], "reason": "Pourcentages absents"}

    home = parse_percent(percent.get("home"))
    draw = parse_percent(percent.get("draw"))
    away = parse_percent(percent.get("away"))

    if home is None or draw is None or away is None:
        return {"markets": [], "reason": "1N2 incomplet"}

    total = home + draw + away

    if not MIN_1X2_SUM <= total <= MAX_1X2_SUM:
        return {
            "markets": [],
            "reason": f"Somme 1N2 incohérente : {total:.1f}%",
        }

    markets = []

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

    if draw >= MIN_WIN_PERCENT:
        markets.append({
            "market": "Match nul",
            "selection": "N",
            "percent": draw,
            "type": "nul",
        })

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

    markets = [
        market for market in markets
        if market["percent"] <= MAX_ACCEPTED_MARKET_PERCENT
    ]

    markets.sort(
        key=lambda item: item["percent"],
        reverse=True
    )

    return {
        "markets": markets,
        "reason": None,
        "home_percent": home,
        "draw_percent": draw,
        "away_percent": away,
        "sum_percent": total,
    }


# =====================================
# RECHERCHE BOOKMAKER 1XBET
# =====================================

async def find_1xbet():
    data = await api_call(
        "odds/bookmakers",
        {"search": "1xBet"}
    )

    bookmakers = get_response_list(data)

    for bookmaker in bookmakers:
        name = str(bookmaker.get("name", "")).lower()

        if "1xbet" in name.replace(" ", ""):
            bookmaker_id = bookmaker.get("id")

            if bookmaker_id:
                return {
                    "id": bookmaker_id,
                    "name": bookmaker.get("name", "1xBet"),
                }

    return None


# =====================================
# COTES 1X2 1XBET
# =====================================

def extract_1x2_odds(odds_data):
    responses = get_response_list(odds_data)

    if not responses:
        return None

    for item in responses:
        bookmakers = item.get("bookmakers", [])

        for bookmaker in bookmakers:
            bookmaker_name = str(
                bookmaker.get("name", "")
            ).lower().replace(" ", "")

            if "1xbet" not in bookmaker_name:
                continue

            result = {
                "home": None,
                "draw": None,
                "away": None,
            }

            for bet in bookmaker.get("bets", []):
                bet_name = str(bet.get("name", "")).lower()

                if bet_name not in (
                    "match winner",
                    "1x2",
                    "fulltime result",
                ):
                    continue

                for value in bet.get("values", []):
                    label = str(
                        value.get("value", "")
                    ).strip().lower()

                    odd = value.get("odd")

                    try:
                        odd = float(odd)
                    except (TypeError, ValueError):
                        continue

                    if label in ("home", "1", "team 1"):
                        result["home"] = odd

                    elif label in ("draw", "x", "tie"):
                        result["draw"] = odd

                    elif label in ("away", "2", "team 2"):
                        result["away"] = odd

            if all(result.values()):
                return result

    return None


async def check_1xbet_odds(fixture_id, bookmaker):
    data = await api_call(
        "odds",
        {
            "fixture": fixture_id,
            "bookmaker": bookmaker["id"],
        },
    )

    responses = get_response_list(data)

    if not responses:
        return None

    for item in responses:
        for book in item.get("bookmakers", []):
            name = str(
                book.get("name", "")
            ).lower().replace(" ", "")

            if "1xbet" not in name:
                continue

            if not book.get("bets"):
                continue

            odds = extract_1x2_odds({"response": [item]})

            return {
                "bookmaker": book.get("name", "1xBet"),
                "odds": odds,
            }

    return None


# =====================================
# DERNIÈRE VÉRIFICATION DU MATCH
# =====================================

async def final_fixture_check(fixture_id):
    data = await api_call(
        "fixtures",
        {"id": fixture_id, "timezone": TIMEZONE},
    )

    fixtures = get_response_list(data)

    if not fixtures:
        return None

    fixture = fixtures[0]

    if not is_upcoming(fixture):
        return None

    return fixture


# =====================================
# MOTEUR 3.2
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
        {
            "date": current_date,
            "timezone": TIMEZONE,
        },
    )

    fixtures = get_response_list(fixtures_data)

    upcoming = [
        fixture for fixture in fixtures
        if is_upcoming(fixture)
    ]

    upcoming.sort(
        key=lambda item: item.get("fixture", {}).get("date", "")
    )

    if isinstance(current, int) and isinstance(limit, int):
        remaining = max(
            0,
            limit - current - RESERVE_API_CALLS - 2
        )

        total_extra_budget = min(
            MAX_PREDICTION_CALLS
            + MAX_ODDS_CHECKS
            + MAX_FINAL_CHECKS,
            remaining,
        )
    else:
        total_extra_budget = 10

    # Répartition prudente du quota disponible.
    prediction_limit = min(
        MAX_PREDICTION_CALLS,
        max(0, int(total_extra_budget * 0.55)),
    )

    odds_limit = min(
        MAX_ODDS_CHECKS,
        max(0, int(total_extra_budget * 0.30)),
    )

    final_limit = min(
        MAX_FINAL_CHECKS,
        max(0, total_extra_budget - prediction_limit - odds_limit),
    )

    candidates = []

    attempts = 0
    analyzed = 0
    no_prediction = 0
    invalid_data = 0
    api_errors = 0

    fixtures_to_analyze = spread_fixtures(
        upcoming,
        prediction_limit,
    )

    for fixture in fixtures_to_analyze:
        fixture_id = fixture.get("fixture", {}).get("id")

        if not fixture_id:
            continue

        if not is_upcoming(fixture):
            continue

        attempts += 1

        try:
            data = await api_call(
                "predictions",
                {"fixture": fixture_id},
            )

            responses = get_response_list(data)

            if not responses:
                no_prediction += 1
                continue

            analyzed += 1

            prediction = extract_markets(responses[0])

            if prediction.get("reason"):
                invalid_data += 1
                continue

            if not prediction["markets"]:
                invalid_data += 1
                continue

            best_market = prediction["markets"][0]

            candidates.append({
                "fixture_id": fixture_id,
                "match": get_fixture_name(fixture),
                "league": get_league_name(fixture),
                "date": format_match_date(fixture),
                "time": format_match_time(fixture),
                "market": best_market["market"],
                "selection": best_market["selection"],
                "percent": best_market["percent"],
                "market_type": best_market["type"],
                "home_percent": prediction["home_percent"],
                "draw_percent": prediction["draw_percent"],
                "away_percent": prediction["away_percent"],
                "sum_percent": prediction["sum_percent"],
            })

        except Exception as error:
            api_errors += 1
            logger.warning(
                "Erreur prédiction %s : %s",
                fixture_id,
                error,
            )

    candidates.sort(
        key=lambda item: item["percent"],
        reverse=True
    )

    bookmaker = None

    if candidates and odds_limit > 0:
        try:
            bookmaker = await find_1xbet()
        except Exception as error:
            api_errors += 1
            logger.warning(
                "Recherche bookmaker 1xBet : %s",
                error,
            )

    verified_candidates = []
    odds_checks = 0

    if bookmaker:
        for candidate in candidates:
            if odds_checks >= odds_limit:
                break

            odds_checks += 1

            try:
                odds_result = await check_1xbet_odds(
                    candidate["fixture_id"],
                    bookmaker,
                )

                if not odds_result:
                    continue

                candidate["bookmaker"] = odds_result["bookmaker"]
                candidate["odds_1x2"] = odds_result["odds"]

                verified_candidates.append(candidate)

            except Exception as error:
                api_errors += 1
                logger.warning(
                    "Erreur cotes 1xBet %s : %s",
                    candidate["fixture_id"],
                    error,
                )

    selected = []
    used_leagues = set()
    final_checks = 0

    for candidate in verified_candidates:
        if len(selected) >= MAX_DISPLAY_PICKS:
            break

        if (
            ONE_PICK_PER_LEAGUE
            and candidate["league"] in used_leagues
        ):
            continue

        if final_checks >= final_limit:
            break

        final_checks += 1

        try:
            fresh_fixture = await final_fixture_check(
                candidate["fixture_id"]
            )

            if not fresh_fixture:
                continue

            # Actualisation de la date et de l'heure.
            candidate["date"] = format_match_date(fresh_fixture)
            candidate["time"] = format_match_time(fresh_fixture)

            selected.append(candidate)
            used_leagues.add(candidate["league"])

        except Exception as error:
            api_errors += 1
            logger.warning(
                "Vérification finale %s : %s",
                candidate["fixture_id"],
                error,
            )

    result = {
        "date": current_date,
        "total": len(fixtures),
        "upcoming": len(upcoming),
        "attempts": attempts,
        "analyzed": analyzed,
        "no_prediction": no_prediction,
        "invalid_data": invalid_data,
        "api_errors": api_errors,
        "quota_limit": total_extra_budget,
        "bookmaker_found": bookmaker is not None,
        "odds_checks": odds_checks,
        "final_checks": final_checks,
        "picks": selected,
    }

    analysis_cache["time"] = time.time()
    analysis_cache["date"] = current_date
    analysis_cache["result"] = result

    return result


# =====================================
# TELEGRAM
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
    await update.message.reply_text(
        "👑 BIENVENUE SUR ALEPH BET AI 3.2 !\n\n"
        "⚽ Analyse de matchs de football.\n"
        "📅 Dates complètes et heures ivoiriennes.\n"
        "🔎 Vérification des cotes 1xBet disponibles.\n"
        "🛑 Exclusion des matchs commencés ou terminés.\n"
        "🎯 Maximum 3 sélections, sans obligation.\n\n"
        "COMMANDES :\n"
        "/matchs - Matchs à venir\n"
        "/pronostics - Analyse approfondie\n"
        "/statut - API et quota\n"
        "/aide - Aide\n\n"
        "⚠️ Une estimation n'est jamais une garantie."
    )


async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ALEPH BET AI 3.2\n\n"
        "/start : Accueil\n"
        "/matchs : Matchs à venir\n"
        "/pronostics : Sélections vérifiées\n"
        "/statut : Connexion et quota\n\n"
        "MARCHÉS :\n"
        "1 : Victoire domicile\n"
        "N : Match nul\n"
        "2 : Victoire extérieure\n"
        "1X : Domicile ou nul\n"
        "X2 : Extérieur ou nul\n"
        "12 : Une des deux équipes gagne\n\n"
        "Les pourcentages sont des estimations API, "
        "pas des taux de réussite historiques."
    )


async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call("status")
        current, limit = get_status_info(data)

        message = (
            "👑 ALEPH BET AI 3.2\n"
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
            f"{str(error)[:300]}"
        )


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
            fixture for fixture in fixtures
            if is_upcoming(fixture)
        ]

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
            lines.extend([
                f"📅 {format_match_date(fixture)}",
                f"🕒 {format_match_time(fixture)}",
                f"🏆 {get_league_name(fixture)}",
                f"⚽ {get_fixture_name(fixture)}",
                "",
            ])

        await send_long_message(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur matchs")

        await update.message.reply_text(
            f"❌ Erreur récupération matchs : {str(error)[:300]}"
        )


async def pronostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👑 ALEPH BET AI 3.2\n"
        "🔎 Analyse des matchs à venir...\n"
        "🛡️ Contrôle des horaires et des statuts...\n"
        "🔍 Recherche des cotes 1xBet..."
    )

    try:
        result = await analyze_all_matches()

        lines = [
            "👑 ALEPH BET AI 3.2 — RAPPORT DU JOUR",
            f"📅 Date du rapport : {result['date']}",
            "",
            f"⚽ Matchs récupérés : {result['total']}",
            f"🕒 Matchs à venir : {result['upcoming']}",
            f"🔍 Tentatives d'analyse : {result['attempts']}",
            f"📊 Réponses API : {result['analyzed']}",
            f"📭 Sans prédiction : {result['no_prediction']}",
            f"🧹 Données écartées : {result['invalid_data']}",
            f"⚠️ Erreurs API : {result['api_errors']}",
            f"🔎 Contrôles de cotes : {result['odds_checks']}",
            "",
        ]

        if not result["bookmaker_found"]:
            lines.extend([
                "⚠️ Bookmaker 1xBet non trouvé dans les données API.",
                "Aucun match ne sera présenté comme vérifié.",
                "",
            ])

        if not result["picks"]:
            lines.extend([
                "🛑 AUCUNE SÉLECTION RETENUE",
                "",
                "Le robot n'a pas trouvé de match réunissant",
                "les critères de qualité et de disponibilité 1xBet.",
                "Il préfère ne pas proposer de pari fragile.",
            ])

        else:
            lines.extend([
                f"🎯 {len(result['picks'])} SÉLECTION(S) VÉRIFIÉE(S)",
                "",
            ])

            for index, pick in enumerate(result["picks"], start=1):
                lines.extend([
                    "━━━━━━━━━━━━━━━━",
                    f"🏅 SÉLECTION #{index}",
                    f"⚽ {pick['match']}",
                    f"🏆 {pick['league']}",
                    f"📅 Date : {pick['date']}",
                    f"🕒 Heure : {pick['time']} (Côte d'Ivoire)",
                    "",
                    "📊 COTES 1X2 — 1xBet",
                ])

                odds = pick.get("odds_1x2")

                if odds:
                    lines.extend([
                        f"1️⃣ Domicile : {odds['home']}",
                        f"❌ Nul : {odds['draw']}",
                        f"2️⃣ Extérieur : {odds['away']}",
                    ])
                else:
                    lines.append(
                        "Cotes 1X2 non fournies par l'API."
                    )

                lines.extend([
                    "",
                    f"🎯 Marché : {pick['market']}",
                    f"📌 Choix : {pick['selection']}",
                    f"📈 Estimation API : {pick['percent']:.1f} %",
                    "",
                    (
                        "Base 1N2 : "
                        f"1={pick['home_percent']:.1f}% | "
                        f"N={pick['draw_percent']:.1f}% | "
                        f"2={pick['away_percent']:.1f}%"
                    ),
                    f"🔎 Bookmaker : {pick['bookmaker']}",
                    "",
                ])

        lines.extend([
            "━━━━━━━━━━━━━━━━",
            "",
            "ℹ️ Les estimations ne sont pas des taux de réussite historiques.",
            "ℹ️ Les cotes affichées proviennent des données API disponibles.",
            "ℹ️ La présence dans l'API ne garantit pas la disponibilité",
            "dans chaque version régionale de 1xBet.",
            "⚠️ Aucun pari n'est garanti.",
        ])

        await send_long_message(update, "\n".join(lines))

    except Exception as error:
        logger.exception("Erreur pronostics")

        await update.message.reply_text(
            "❌ Erreur pendant l'analyse.\n"
            f"{str(error)[:300]}"
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

    logger.info("ALEPH BET AI 3.2 démarre...")

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
