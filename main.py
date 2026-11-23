import os
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# ALEPH BET AI 4.0 MULTI-SPORTS
# Football + Basketball + Hockey
# Les API fournissent des données ; aucune certitude de pari.
# ============================================================

VERSION = "4.0 MULTI-SPORTS"
TZ_NAME = "Africa/Abidjan"
TZ = ZoneInfo(TZ_NAME)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))

# Le compte API-Sports peut utiliser la même clé sur ses produits.
# On permet toutefois des clés séparées si l'utilisateur en configure.
FOOT_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
BASKET_KEY = os.getenv("API_BASKETBALL_KEY", FOOT_KEY).strip()
HOCKEY_KEY = os.getenv("API_HOCKEY_KEY", FOOT_KEY).strip()

APIS = {
    "football": {
        "base": "https://v3.football.api-sports.io",
        "key": FOOT_KEY,
        "header": "x-apisports-key",
    },
    "basketball": {
        "base": "https://v1.basketball.api-sports.io",
        "key": BASKET_KEY,
        "header": "x-apisports-key",
    },
    "hockey": {
        "base": "https://v1.hockey.api-sports.io",
        "key": HOCKEY_KEY,
        "header": "x-apisports-key",
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("aleph-bet-ai")

# Cache mémoire court pour éviter de répéter inutilement les mêmes appels.
CACHE = {}
CACHE_SECONDS = 180


def now_local():
    return datetime.now(TZ)


def date_local(offset=0):
    return (now_local().date() + timedelta(days=offset)).isoformat()


def parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def fmt_datetime(value):
    dt = parse_datetime(value)
    return dt.strftime("%d/%m/%Y à %H:%M") if dt else "Date non disponible"


def api_get(sport, endpoint, params=None):
    cfg = APIS[sport]
    if not cfg["key"]:
        raise RuntimeError(f"Clé API {sport} absente dans Render.")
    url = cfg["base"] + endpoint
    headers = {cfg["header"]: cfg["key"]}
    response = requests.get(url, headers=headers, params=params or {}, timeout=25)
    if response.status_code == 429:
        raise RuntimeError("Limite de requêtes API atteinte (429). Réessaie plus tard.")
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"Erreur API {sport}: {errors}")
    return payload


async def cached_api_get(sport, endpoint, params=None):
    params = params or {}
    cache_key = (sport, endpoint, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    saved = CACHE.get(cache_key)
    if saved and (now_local().timestamp() - saved[0] < CACHE_SECONDS):
        return saved[1]
    result = await asyncio.to_thread(api_get, sport, endpoint, params)
    CACHE[cache_key] = (now_local().timestamp(), result)
    return result


def response_list(payload):
    value = payload.get("response", [])
    return value if isinstance(value, list) else []


def match_status(match):
    status = match.get("fixture", {}).get("status", {})
    if not status:
        status = match.get("status", {})
    return str(status.get("short", status.get("code", ""))).upper()


def match_date(match):
    fixture = match.get("fixture", {})
    return fixture.get("date") or match.get("date") or match.get("start_time")


def team_names(match):
    teams = match.get("teams", {})
    home = teams.get("home", {})
    away = teams.get("away", {})
    if isinstance(home, dict) and isinstance(away, dict):
        return home.get("name", "Équipe inconnue"), away.get("name", "Équipe inconnue")
    return "Équipe inconnue", "Équipe inconnue"


def league_name(match):
    league = match.get("league", {})
    if isinstance(league, dict):
        return league.get("name", "Compétition non précisée")
    return "Compétition non précisée"


def is_future_match(match):
    dt = parse_datetime(match_date(match))
    status = match_status(match)
    # On exclut les rencontres commencées, terminées, reportées ou annulées.
    blocked = {
        "1H", "HT", "2H", "ET", "P", "LIVE", "INPLAY",
        "Q1", "Q2", "Q3", "Q4", "OT", "P1", "P2", "P3",
        "FT", "AET", "PEN", "POST", "CANC", "SUSP", "ABD",
        "AWD", "WO", "BT", "INTR"
    }
    if not dt or dt <= now_local() or status in blocked:
        return False
    # Pour les API qui utilisent un statut explicite, seuls les statuts pré-match sont acceptés.
    if status and status not in {"NS", "NOT STARTED", "SCHEDULED", "TBD"}:
        return False
    return True


def prediction_percent(prediction, key):
    predictions = prediction.get("predictions", {})
    percentages = predictions.get("percent", {})
    raw = percentages.get(key)
    if raw is None:
        return None
    try:
        return float(str(raw).replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def football_recommendation(prediction):
    """N'affiche que les marchés dont le pourcentage est fourni par l'API."""
    candidates = []
    home, away = team_names(prediction)
    p_home = prediction_percent(prediction, "home")
    p_away = prediction_percent(prediction, "away")
    p_draw = prediction_percent(prediction, "draw")

    if p_home is not None and p_home >= 65:
        candidates.append((p_home, f"Victoire {home}", "1"))
    if p_away is not None and p_away >= 65:
        candidates.append((p_away, f"Victoire {away}", "2"))
    if p_home is not None and p_draw is not None and p_home + p_draw >= 80:
        candidates.append((p_home + p_draw, f"Double chance 1X ({home} ou nul)", "1X"))
    if p_away is not None and p_draw is not None and p_away + p_draw >= 80:
        candidates.append((p_away + p_draw, f"Double chance X2 (nul ou {away})", "X2"))

    if not candidates:
        return None
    # Pour éviter d'afficher plusieurs marchés du même match, on garde le plus élevé.
    return max(candidates, key=lambda item: item[0])


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"👑 ALEPH BET AI {VERSION}", f"🕒 Heure locale : {now_local():%d/%m/%Y %H:%M}", ""]
    for sport, cfg in APIS.items():
        if not cfg["key"]:
            lines.append(f"⚪ {sport.capitalize()} : clé API non configurée")
            continue
        try:
            payload = await cached_api_get(sport, "/status")
            response = payload.get("response", {})
            account = response.get("account", {}) if isinstance(response, dict) else {}
            subscription = response.get("subscription", {}) if isinstance(response, dict) else {}
            requests_info = response.get("requests", {}) if isinstance(response, dict) else {}
            used = requests_info.get("current")
            limit = requests_info.get("limit")
            quota = f" | Requêtes : {used}/{limit}" if used is not None and limit is not None else ""
            lines.append(f"🟢 {sport.capitalize()} : API accessible{quota}")
        except Exception as exc:
            lines.append(f"🔴 {sport.capitalize()} : {str(exc)[:180]}")
    lines += ["", "⚠️ Une connexion active ne garantit ni la disponibilité de tous les championnats ni la fiabilité d'un pari."]
    await update.message.reply_text("\n".join(lines))


async def get_upcoming(sport, days=2):
    endpoint = "/fixtures" if sport == "football" else "/games"
    results = []
    for offset in range(days):
        payload = await cached_api_get(sport, endpoint, {"date": date_local(offset)})
        results.extend(response_list(payload))
    unique = {}
    for item in results:
        if is_future_match(item):
            key = str(item.get("fixture", {}).get("id") or item.get("id") or (match_date(item), team_names(item)))
            unique[key] = item
    return sorted(unique.values(), key=lambda x: parse_datetime(match_date(x)) or now_local())


async def sport_matches(update, sport, days=2, limit=12):
    try:
        matches = await get_upcoming(sport, days=days)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Impossible de récupérer les matchs {sport} :\n{str(exc)[:350]}")
        return

    if not matches:
        await update.message.reply_text(
            f"📭 Aucun match {sport} à venir trouvé dans les {days} prochains jours.\n"
            "Cela peut aussi signifier que la couverture de l'API est limitée."
        )
        return

    title = f"⚽ FOOTBALL" if sport == "football" else ("🏀 BASKETBALL" if sport == "basketball" else "🏒 HOCKEY")
    lines = [f"👑 ALEPH BET AI 4.0 — {title}", f"📅 Vérification : {now_local():%d/%m/%Y %H:%M}", ""]
    for index, match in enumerate(matches[:limit], start=1):
        home, away = team_names(match)
        lines.append(
            f"{index}. {home} — {away}\n"
            f"🏆 {league_name(match)}\n"
            f"🗓 {fmt_datetime(match_date(match))}"
        )
    lines += ["", "ℹ️ Liste de matchs à venir, pas une recommandation de mise."]
    await update.message.reply_text("\n\n".join(lines))


async def foot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        matches = await get_upcoming("football", days=2)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Erreur Football : {str(exc)[:350]}")
        return

    if not matches:
        await update.message.reply_text("📭 Aucun match de football à venir trouvé.")
        return

    lines = [
        f"👑 ALEPH BET AI {VERSION} — PRÉ-ANALYSE FOOTBALL",
        f"📅 {now_local():%d/%m/%Y %H:%M} (Côte d'Ivoire)",
        ""
    ]
    attempts = 0
    shown = 0
    # Appels limités à 6 pour préserver le quota gratuit.
    for match in matches[:6]:
        fixture_id = match.get("fixture", {}).get("id")
        if not fixture_id:
            continue
        attempts += 1
        try:
            payload = await cached_api_get("football", "/predictions", {"fixture": fixture_id})
            rows = response_list(payload)
            if not rows:
                continue
            row = rows[0]
            recommendation = football_recommendation(row)
            if not recommendation:
                continue
            percent, label, market = recommendation
            home, away = team_names(match)
            lines.append(
                f"⚽ {home} — {away}\n"
                f"🏆 {league_name(match)}\n"
                f"🗓 {fmt_datetime(match_date(match))}\n"
                f"🎯 Marché API : {label}\n"
                f"📊 Estimation fournie par l'API : {percent:.0f}%"
            )
            shown += 1
            if shown >= 3:
                break
        except Exception as exc:
            log.warning("Prediction fixture %s: %s", fixture_id, exc)

    if shown == 0:
        await update.message.reply_text(
            "🔎 Aucun pronostic ne passe actuellement les filtres.\n"
            f"Matchs examinés : {min(len(matches), 6)} | Appels de prédiction tentés : {attempts}\n"
            "C'est volontaire : le bot préfère ne rien proposer plutôt que forcer un choix."
        )
        return

    lines += [
        "",
        "⚠️ Pourcentages indicatifs de l'API, pas des taux de réussite vérifiés.",
        "Aucun pari n'est sûr. Vérifie les informations et limite strictement tes mises."
    ]
    await update.message.reply_text("\n\n".join(lines))


async def basket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sport_matches(update, "basketball", days=2, limit=12)


async def hockey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sport_matches(update, "hockey", days=2, limit=12)


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Présente les données des trois sports séparément ; ne mélange pas leurs probabilités.
    await update.message.reply_text(
        f"👑 ALEPH BET AI {VERSION}\n"
        "Centre multi-sports activé.\n\n"
        "⚽ /foot — pré-analyse football\n"
        "🏀 /basket — matchs de basketball à venir\n"
        "🏒 /hockey — matchs de hockey à venir\n"
        "📊 /statut — état des trois connexions API\n\n"
        "Le mode Premium ne force pas de sélection : basketball et hockey sont en phase de vérification des données."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👑 Bienvenue sur ALEPH BET AI {VERSION}\n\n"
        "Commandes disponibles :\n"
        "/premium — centre multi-sports\n"
        "/foot — analyse football\n"
        "/pronostics — alias football\n"
        "/matchs — matchs de football à venir\n"
        "/basket — matchs de basketball à venir\n"
        "/hockey — matchs de hockey à venir\n"
        "/statut — état des API\n"
        "/aide — aide"
    )


async def aide_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 AIDE ALEPH BET AI 4.0\n\n"
        "/foot : pré-analyse football, maximum 3 sélections si les données le permettent.\n"
        "/pronostics : même fonction que /foot.\n"
        "/matchs : liste les matchs de football à venir.\n"
        "/basket : liste les matchs de basketball à venir.\n"
        "/hockey : liste les matchs de hockey à venir.\n"
        "/premium : centre multi-sports.\n"
        "/statut : vérifie les connexions API.\n\n"
        "Les matchs affichés sont filtrés pour exclure les rencontres déjà commencées ou terminées.\n"
        "Les pourcentages football sont des estimations du fournisseur, pas des performances historiques."
    )


async def matchs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sport_matches(update, "football", days=2, limit=15)


def main():
    if not TOKEN:
        raise RuntimeError("Variable TELEGRAM_BOT_TOKEN absente.")
    if not WEBHOOK_URL:
        raise RuntimeError("Variable WEBHOOK_URL absente dans Render.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("aide", aide_command))
    app.add_handler(CommandHandler("statut", status_command))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("foot", foot_command))
    app.add_handler(CommandHandler("pronostics", foot_command))
    app.add_handler(CommandHandler("matchs", matchs_command))
    app.add_handler(CommandHandler("basket", basket_command))
    app.add_handler(CommandHandler("hockey", hockey_command))

    webhook_args = {
        "listen": "0.0.0.0",
        "port": PORT,
        "url_path": "telegram",
        "webhook_url": WEBHOOK_URL + "/telegram",
        "allowed_updates": Update.ALL_TYPES,
    }
    if WEBHOOK_SECRET:
        webhook_args["secret_token"] = WEBHOOK_SECRET

    log.info("Starting %s", VERSION)
    app.run_webhook(**webhook_args)


if __name__ == "__main__":
    main()
