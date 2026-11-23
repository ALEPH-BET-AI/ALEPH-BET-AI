import os
import json
import logging
import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# ALEPH BET AI 3.3
# Analyse prudente des matchs à venir + suivi des résultats
# ============================================================

VERSION = "3.5"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", "10000"))

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Africa/Abidjan"
LOCAL_TZ = ZoneInfo(TIMEZONE)

# Filtres de sélection. Ce sont des seuils de tri, pas des garanties.
MIN_WIN_PERCENT = 65.0
MIN_DOUBLE_CHANCE_PERCENT = 78.0
MIN_1X2_SUM = 97.0
MAX_1X2_SUM = 103.0
MAX_ACCEPTED_PERCENT = 96.0

# Quota API : limiter les appels pour préserver l'offre gratuite.
MAX_PREDICTION_CALLS = 12
RESERVE_API_CALLS = 5
MAX_DISPLAY_PICKS = 3
ONE_PICK_PER_LEAGUE = True

CACHE_DURATION = 1800
DATA_FILE = Path(os.getenv("ALEPH_DATA_FILE", "aleph_bet_data.json"))
REQUEST_TIMEOUT = 20

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

analysis_cache = {"time": 0, "date": None, "result": None}


# ------------------------- Horloge -------------------------

def now_local():
    return datetime.now(LOCAL_TZ)


def today():
    return now_local().date().isoformat()


def parse_fixture_date(fixture):
    raw = fixture.get("fixture", {}).get("date")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TZ)
    except (ValueError, TypeError):
        return None


def format_match_date(fixture):
    value = parse_fixture_date(fixture)
    return value.strftime("%d/%m/%Y") if value else "Date inconnue"


def format_match_time(fixture):
    value = parse_fixture_date(fixture)
    return value.strftime("%H:%M") if value else "Heure inconnue"


def is_upcoming(fixture):
    status = fixture.get("fixture", {}).get("status", {}).get("short", "")
    kickoff = parse_fixture_date(fixture)
    # NS seulement : on exclut TBD, live, terminé, reporté et annulé.
    return status == "NS" and kickoff is not None and kickoff > now_local()


# ------------------------- API -----------------------------

def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("Clé API-Football absente.")

    response = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"x-apisports-key": API_KEY},
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )

    # Conserver le détail renvoyé par le serveur en cas d'erreur HTTP.
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        try:
            body = response.json()
            detail = body.get("errors") or body.get("message") or response.text[:300]
        except ValueError:
            detail = response.text[:300]
        raise RuntimeError(
            f"API-Football HTTP {response.status_code}: {str(detail)[:350]}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Réponse API invalide : JSON illisible.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Réponse API invalide : format inattendu.")
    if data.get("errors"):
        raise RuntimeError(f"Erreur API-Football : {str(data['errors'])[:350]}")
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
    info = response.get("requests", {})
    if not isinstance(info, dict):
        return None, None
    try:
        current = int(info["current"]) if info.get("current") is not None else None
        limit = int(info["limit_day"]) if info.get("limit_day") is not None else None
        return current, limit
    except (TypeError, ValueError):
        return None, None


# --------------------- Infos match -------------------------

def get_fixture_name(fixture):
    teams = fixture.get("teams", {})
    home = teams.get("home", {}).get("name", "Domicile")
    away = teams.get("away", {}).get("name", "Extérieur")
    return f"{home} - {away}"


def get_league_name(fixture):
    league = fixture.get("league", {})
    name = league.get("name", "Championnat inconnu")
    country = league.get("country")
    return f"{name} ({country})" if country else name


def parse_percent(value):
    if value is None:
        return None
    try:
        number = float(str(value).replace("%", "").strip())
        return number if 0 <= number <= 100 else None
    except (ValueError, TypeError):
        return None


def spread_fixtures(fixtures, count):
    if count <= 0:
        return []
    if len(fixtures) <= count:
        return fixtures
    if count == 1:
        return [fixtures[0]]
    indexes = [round(i * (len(fixtures) - 1) / (count - 1)) for i in range(count)]
    output, seen = [], set()
    for index in indexes:
        if index not in seen:
            output.append(fixtures[index])
            seen.add(index)
    return output


# ------------------- Analyse des marchés -------------------

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
        return {"markets": [], "reason": "Pourcentages 1N2 incomplets"}

    total = home + draw + away
    if not MIN_1X2_SUM <= total <= MAX_1X2_SUM:
        return {"markets": [], "reason": f"Somme 1N2 incohérente ({total:.1f}%)"}

    markets = []
    if MIN_WIN_PERCENT <= home <= MAX_ACCEPTED_PERCENT:
        markets.append({
            "market": "Victoire domicile", "selection": "1",
            "percent": home, "type": "victoire",
        })
    if MIN_WIN_PERCENT <= away <= MAX_ACCEPTED_PERCENT:
        markets.append({
            "market": "Victoire extérieure", "selection": "2",
            "percent": away, "type": "victoire",
        })

    # Double chance calculée à partir des estimations 1N2 de l'API.
    # Ce calcul n'est pas une probabilité indépendante ni un taux historique.
    double_chances = [
        ("1X", home + draw),
        ("X2", away + draw),
        ("12", home + away),
    ]
    for selection, value in double_chances:
        value = min(100.0, value)
        if MIN_DOUBLE_CHANCE_PERCENT <= value <= MAX_ACCEPTED_PERCENT:
            markets.append({
                "market": "Double chance", "selection": selection,
                "percent": value, "type": "double",
            })

    # L'API fournit souvent un conseil de buts sans probabilité chiffrée.
    # On ne fabrique donc aucun pourcentage Over/Under.
    markets.sort(key=lambda item: item["percent"], reverse=True)
    return {
        "markets": markets,
        "reason": None,
        "home_percent": home,
        "draw_percent": draw,
        "away_percent": away,
        "sum_percent": total,
        "advice": predictions.get("advice"),
        "under_over": predictions.get("under_over"),
    }


# ---------------------- Suivi local ------------------------

def load_store():
    if not DATA_FILE.exists():
        return {"picks": []}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("picks"), list):
            return {"picks": []}
        return data
    except (OSError, json.JSONDecodeError):
        logger.warning("Fichier de suivi illisible : démarrage avec un suivi vide.")
        return {"picks": []}


def save_store(data):
    temp_file = DATA_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    temp_file.replace(DATA_FILE)


def register_picks(picks):
    store = load_store()
    existing = {
        (str(item.get("fixture_id")), item.get("selection"), item.get("market_type"))
        for item in store["picks"]
    }
    added = 0
    for pick in picks:
        key = (str(pick["fixture_id"]), pick["selection"], pick["market_type"])
        if key in existing:
            continue
        store["picks"].append({
            "fixture_id": pick["fixture_id"],
            "match": pick["match"],
            "league": pick["league"],
            "date": pick["date"],
            "time": pick["time"],
            "market": pick["market"],
            "selection": pick["selection"],
            "market_type": pick["market_type"],
            "estimated_percent": pick["percent"],
            "created_at": now_local().isoformat(),
            "result": "pending",
            "score": None,
        })
        existing.add(key)
        added += 1
    save_store(store)
    return added


def settle_pick(pick, fixture):
    status = fixture.get("fixture", {}).get("status", {}).get("short", "")
    if status not in ("FT", "AET", "PEN"):
        return None

    goals = fixture.get("goals", {})
    home_goals, away_goals = goals.get("home"), goals.get("away")
    if not isinstance(home_goals, int) or not isinstance(away_goals, int):
        return None

    selection = pick.get("selection")
    if selection == "1":
        outcome = home_goals > away_goals
    elif selection == "2":
        outcome = away_goals > home_goals
    elif selection == "1X":
        outcome = home_goals >= away_goals
    elif selection == "X2":
        outcome = away_goals >= home_goals
    elif selection == "12":
        outcome = home_goals != away_goals
    else:
        return None

    pick["score"] = f"{home_goals}-{away_goals}"
    pick["result"] = "win" if outcome else "loss"
    return pick["result"]


async def refresh_results():
    """Actualise le suivi sans utiliser le paramètre ids (interdit en Free)."""
    store = load_store()
    pending = [p for p in store["picks"] if p.get("result") == "pending"]
    if not pending:
        return store, 0, 0, 0, []

    today_local = now_local().date()
    dates_to_check = set()

    for pick in pending:
        raw_date = str(pick.get("date", "")).strip()
        parsed_date = None
        for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                parsed_date = datetime.strptime(raw_date, date_format).date()
                break
            except ValueError:
                continue
        if parsed_date and parsed_date <= today_local:
            dates_to_check.add(parsed_date.isoformat())

    updated = 0
    checked_dates = 0
    failed_dates = 0
    errors = []

    for match_date in sorted(dates_to_check):
        try:
            data = await api_call(
                "fixtures", {"date": match_date, "timezone": TIMEZONE}
            )
            checked_dates += 1
            fixtures = get_response_list(data)
            by_id = {
                str(f.get("fixture", {}).get("id")): f
                for f in fixtures
                if f.get("fixture", {}).get("id") is not None
            }

            for pick in pending:
                fixture = by_id.get(str(pick.get("fixture_id")))
                if fixture and settle_pick(pick, fixture):
                    updated += 1

        except Exception as error:
            failed_dates += 1
            detail = f"{match_date} : {str(error)[:350]}"
            errors.append(detail)
            logger.exception("Erreur actualisation bilan pour %s", match_date)

    save_store(store)
    return store, updated, checked_dates, failed_dates, errors


# ---------------------- Moteur 3.4 -------------------------

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
        "fixtures", {"date": current_date, "timezone": TIMEZONE}
    )
    fixtures = get_response_list(fixtures_data)
    upcoming = [f for f in fixtures if is_upcoming(f)]
    upcoming.sort(key=lambda f: f.get("fixture", {}).get("date", ""))

    if isinstance(current, int) and isinstance(limit, int):
        remaining = max(0, limit - current - RESERVE_API_CALLS)
        prediction_budget = min(MAX_PREDICTION_CALLS, remaining)
    else:
        # Budget réduit si l'API ne renvoie pas les informations de quota.
        prediction_budget = min(6, MAX_PREDICTION_CALLS)

    chosen_fixtures = spread_fixtures(upcoming, prediction_budget)
    candidates = []
    attempts = analyzed = no_prediction = invalid_data = api_errors = 0

    for fixture in chosen_fixtures:
        if not is_upcoming(fixture):
            continue
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
            analysis = extract_markets(responses[0])
            if analysis.get("reason") or not analysis["markets"]:
                invalid_data += 1
                continue

            best = analysis["markets"][0]
            candidates.append({
                "fixture_id": fixture_id,
                "match": get_fixture_name(fixture),
                "league": get_league_name(fixture),
                "date": format_match_date(fixture),
                "time": format_match_time(fixture),
                "market": best["market"],
                "selection": best["selection"],
                "percent": best["percent"],
                "market_type": best["type"],
                "home_percent": analysis["home_percent"],
                "draw_percent": analysis["draw_percent"],
                "away_percent": analysis["away_percent"],
                "advice": analysis.get("advice"),
                "under_over": analysis.get("under_over"),
            })
        except Exception as error:
            api_errors += 1
            logger.warning("Erreur prédiction fixture %s : %s", fixture_id, error)

    # Recontrôle horaire local juste avant la sélection finale.
    candidates.sort(key=lambda item: item["percent"], reverse=True)
    selected, used_leagues = [], set()
    for candidate in candidates:
        if len(selected) >= MAX_DISPLAY_PICKS:
            break
        original = next(
            (f for f in upcoming if f.get("fixture", {}).get("id") == candidate["fixture_id"]),
            None,
        )
        if not original or not is_upcoming(original):
            continue
        if ONE_PICK_PER_LEAGUE and candidate["league"] in used_leagues:
            continue
        selected.append(candidate)
        used_leagues.add(candidate["league"])

    tracked = register_picks(selected) if selected else 0

    result = {
        "date": current_date,
        "total": len(fixtures),
        "upcoming": len(upcoming),
        "attempts": attempts,
        "analyzed": analyzed,
        "no_prediction": no_prediction,
        "invalid_data": invalid_data,
        "api_errors": api_errors,
        "prediction_budget": prediction_budget,
        "picks": selected,
        "tracked": tracked,
    }
    analysis_cache.update({
        "time": time.time(),
        "date": current_date,
        "result": result,
    })
    return result


# ----------------------- Telegram -------------------------

async def send_long_message(update, message):
    while message:
        if len(message) <= 3500:
            await update.message.reply_text(message)
            break
        split_at = message.rfind("\n", 0, 3500)
        if split_at < 1:
            split_at = 3500
        await update.message.reply_text(message[:split_at])
        message = message[split_at:].lstrip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👑 BIENVENUE SUR ALEPH BET AI {VERSION} !\n\n"
        "⚽ Analyse des matchs de football à venir.\n"
        "📅 Dates complètes et heures de Côte d'Ivoire.\n"
        "🎯 Maximum 3 sélections, aucune sélection forcée.\n"
        "📊 Suivi des résultats réels avec /bilan.\n"
        "🛑 Les matchs commencés ou terminés sont exclus.\n\n"
        "COMMANDES :\n"
        "/matchs - Matchs à venir aujourd'hui\n"
        "/pronostics - Analyse des meilleurs matchs\n"
        "/bilan - Résultats des sélections suivies\n"
        "/statut - API et quota\n"
        "/aide - Aide\n\n"
        "⚠️ Aucun pronostic ni pourcentage ne garantit un gain."
    )


async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 ALEPH BET AI {VERSION}\n\n"
        "/start : Accueil\n"
        "/matchs : Matchs à venir\n"
        "/pronostics : Analyse approfondie\n"
        "/bilan : Suivi des résultats\n"
        "/statut : Connexion et quota\n\n"
        "MARCHÉS ANALYSÉS :\n"
        "1 : Victoire domicile\n"
        "2 : Victoire extérieure\n"
        "1X : Domicile ou nul\n"
        "X2 : Extérieur ou nul\n"
        "12 : Une des deux équipes gagne\n\n"
        "Les estimations viennent de l'API. Elles ne sont pas "
        "des taux de réussite historiques. Les marchés de buts "
        "ne sont pas chiffrés si l'API ne fournit pas de probabilité."
    )


async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call("status")
        current, limit = get_status_info(data)
        message = (
            f"👑 ALEPH BET AI {VERSION}\n"
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
            "🔴 Connexion API impossible.\n" + str(error)[:250]
        )


async def matchs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call(
            "fixtures", {"date": today(), "timezone": TIMEZONE}
        )
        fixtures = [f for f in get_response_list(data) if is_upcoming(f)]
        fixtures.sort(key=lambda f: f.get("fixture", {}).get("date", ""))
        if not fixtures:
            await update.message.reply_text("⚽ Aucun match à venir aujourd'hui.")
            return

        lines = [
            f"⚽ MATCHS À VENIR — {now_local().strftime('%d/%m/%Y')}",
            f"Total : {len(fixtures)}", "",
        ]
        for fixture in fixtures[:30]:
            lines.extend([
                f"📅 {format_match_date(fixture)}",
                f"🕒 {format_match_time(fixture)} (Côte d'Ivoire)",
                f"🏆 {get_league_name(fixture)}",
                f"⚽ {get_fixture_name(fixture)}", "",
            ])
        await send_long_message(update, "\n".join(lines))
    except Exception as error:
        logger.exception("Erreur matchs")
        await update.message.reply_text(
            "❌ Erreur récupération matchs : " + str(error)[:250]
        )


async def pronostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👑 ALEPH BET AI {VERSION}\n"
        "🔎 Analyse des matchs à venir...\n"
        "🛡️ Exclusion des matchs commencés ou terminés...\n"
        "📊 Comparaison des estimations disponibles..."
    )
    try:
        result = await analyze_all_matches()
        lines = [
            f"👑 ALEPH BET AI {VERSION} — RAPPORT DU JOUR",
            f"📅 Date : {result['date']}",
            "",
            f"⚽ Matchs récupérés : {result['total']}",
            f"🕒 Matchs à venir : {result['upcoming']}",
            f"🔍 Tentatives d'analyse : {result['attempts']}",
            f"📊 Réponses API : {result['analyzed']}",
            f"📭 Sans prédiction : {result['no_prediction']}",
            f"🧹 Données écartées : {result['invalid_data']}",
            f"⚠️ Erreurs API : {result['api_errors']}",
            f"🔋 Budget d'analyse : {result['prediction_budget']} appels maximum",
            "",
        ]

        if not result["picks"]:
            lines.extend([
                "🛑 AUCUNE SÉLECTION RETENUE",
                "Les données disponibles ne satisfont pas les filtres.",
                "Le robot préfère s'abstenir plutôt que forcer un pari.",
            ])
        else:
            lines.extend([
                f"🎯 {len(result['picks'])} SÉLECTION(S)",
                "Les matchs sont à retrouver manuellement sur 1xBet.",
                "",
            ])
            for index, pick in enumerate(result["picks"], 1):
                lines.extend([
                    "━━━━━━━━━━━━━━━━",
                    f"🏅 SÉLECTION #{index}",
                    f"⚽ {pick['match']}",
                    f"🏆 {pick['league']}",
                    f"📅 Date : {pick['date']}",
                    f"🕒 Heure : {pick['time']} (Côte d'Ivoire)",
                    "",
                    f"🎯 Marché : {pick['market']}",
                    f"📌 Choix : {pick['selection']}",
                    f"📈 Estimation API : {pick['percent']:.1f} %",
                    (
                        "📊 Base 1N2 API : "
                        f"1={pick['home_percent']:.1f}% | "
                        f"N={pick['draw_percent']:.1f}% | "
                        f"2={pick['away_percent']:.1f}%"
                    ),
                ])
                if pick.get("advice"):
                    lines.append(f"💡 Conseil API : {str(pick['advice'])[:180]}")
                if pick.get("under_over"):
                    lines.append(
                        f"⚽ Indication buts API : {str(pick['under_over'])[:80]} "
                        "(sans probabilité chiffrée)"
                    )
                lines.append("")

        lines.extend([
            "━━━━━━━━━━━━━━━━",
            "",
            "ℹ️ Les pourcentages sont des estimations API, pas des taux de réussite.",
            "ℹ️ Les doubles chances sont calculées depuis les estimations 1N2.",
            "ℹ️ Aucune cote 1xBet n'est vérifiée ni garantie par le robot.",
            "⚠️ Aucun pari n'est sûr. Mise faible et limites strictes.",
            f"📚 Nouvelles sélections ajoutées au suivi : {result['tracked']}",
            "Utilise /bilan pour consulter les résultats suivis.",
        ])
        await send_long_message(update, "\n".join(lines))
    except Exception as error:
        logger.exception("Erreur pronostics")
        await update.message.reply_text(
            "❌ Erreur pendant l'analyse.\n" + str(error)[:250]
        )


async def bilan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("📊 Mise à jour des résultats en cours...")
        store, updated, checked_dates, failed_dates, errors = await refresh_results()
        picks = store["picks"]
        wins = sum(1 for p in picks if p.get("result") == "win")
        losses = sum(1 for p in picks if p.get("result") == "loss")
        pending = sum(1 for p in picks if p.get("result") == "pending")
        settled = wins + losses
        rate = (wins / settled * 100) if settled else None

        lines = [
            f"📊 ALEPH BET AI {VERSION} — BILAN",
            f"🔄 Nouveaux résultats vérifiés : {updated}",
            f"📅 Journées consultées : {checked_dates}",
            f"⚠️ Journées en erreur : {failed_dates}",
            f"✅ Gagnés : {wins}",
            f"❌ Perdus : {losses}",
            f"⏳ En attente : {pending}",
            f"📚 Total enregistré : {len(picks)}",
        ]
        if errors:
            lines.extend(["", "⚠️ DÉTAIL DES ERREURS :"])
            for error_detail in errors:
                lines.append(f"• {error_detail}")

        if rate is not None:
            lines.append(f"📈 Réussite observée : {rate:.1f}% ({wins}/{settled})")
        else:
            lines.append("📈 Taux réel : pas encore calculable (aucun match terminé).")

        lines.extend(["", "DERNIÈRES SÉLECTIONS :"])
        for pick in reversed(picks[-10:]):
            icon = {"win": "✅", "loss": "❌", "pending": "⏳"}.get(
                pick.get("result"), "❔"
            )
            score = f" — {pick['score']}" if pick.get("score") else ""
            lines.append(
                f"{icon} {pick.get('date')} {pick.get('time')} | "
                f"{pick.get('match')} | {pick.get('selection')}{score}"
            )

        lines.extend([
            "",
            "⚠️ Ce bilan porte uniquement sur les sélections enregistrées par le bot.",
            "Il ne prouve pas un bénéfice financier et ne garantit pas les prochains résultats.",
        ])
        await send_long_message(update, "\n".join(lines))
    except Exception as error:
        logger.exception("Erreur bilan")
        await update.message.reply_text(
            "❌ Impossible d'actualiser le bilan : " + str(error)[:250]
        )


# ------------------------- Démarrage -----------------------

def main():
    missing = [
        name for name, value in (
            ("TELEGRAM_BOT_TOKEN", TOKEN),
            ("WEBHOOK_URL", WEBHOOK_URL),
            ("WEBHOOK_SECRET", WEBHOOK_SECRET),
            ("API_FOOTBALL_KEY", API_KEY),
        ) if not value
    ]
    if missing:
        raise RuntimeError("Variables Render manquantes : " + ", ".join(missing))

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CommandHandler("statut", statut))
    app.add_handler(CommandHandler("matchs", matchs))
    app.add_handler(CommandHandler("pronostics", pronostics))
    app.add_handler(CommandHandler("bilan", bilan))

    logger.info("ALEPH BET AI %s démarre...", VERSION)
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
