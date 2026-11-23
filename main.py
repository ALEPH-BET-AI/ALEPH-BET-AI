import os
import json
import logging
import asyncio
import time
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# ALEPH BET AI 3.8 PREMIUM
# Appels API temporises, reprises 429, filtres prudents,
# suivi des resultats et protection du quota quotidien.
# ============================================================

VERSION = "3.8 PREMIUM"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", "10000"))

API_BASE = "https://v3.football.api-sports.io"
TIMEZONE = "Africa/Abidjan"
LOCAL_TZ = ZoneInfo(TIMEZONE)

MIN_WIN_PERCENT = 65.0
MIN_DOUBLE_CHANCE_PERCENT = 80.0
MIN_1X2_SUM = 97.0
MAX_1X2_SUM = 103.0
MAX_ACCEPTED_PERCENT = 96.0

MIN_COMPARISON_METRICS = 4
MIN_QUALITY_SCORE = 62.0
MAX_PREDICTION_CALLS = 8
RESERVE_API_CALLS = 8
MAX_DISPLAY_PICKS = 3
ONE_PICK_PER_LEAGUE = True
MIN_MINUTES_BEFORE_KICKOFF = 10

# Une requete au plus toutes les 7 secondes environ.
# Cela reduit fortement le risque de limite par minute.
MIN_REQUEST_INTERVAL = 7.0
MAX_429_RETRIES = 3
RETRY_BASE_SECONDS = 20
REQUEST_TIMEOUT = 25

CACHE_DURATION = 1800
DATA_FILE = Path(os.getenv("ALEPH_DATA_FILE", "aleph_bet_data.json"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

analysis_cache = {"time": 0, "date": None, "result": None}
_request_lock = threading.Lock()
_last_request_at = 0.0


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
    return status == "NS" and kickoff is not None and kickoff > now_local()


def is_safe_for_pick(fixture):
    """Autorise une selection seulement si le coup d'envoi est a plus de 10 min."""
    kickoff = parse_fixture_date(fixture)
    return (
        is_upcoming(fixture)
        and kickoff is not None
        and kickoff > now_local() + timedelta(minutes=MIN_MINUTES_BEFORE_KICKOFF)
    )


def pick_is_safe(pick):
    """Revalide l'heure d'une selection, meme lorsqu'elle vient du cache."""
    raw = pick.get("kickoff_at")
    kickoff = None
    if raw:
        try:
            kickoff = datetime.fromisoformat(str(raw))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=LOCAL_TZ)
            kickoff = kickoff.astimezone(LOCAL_TZ)
        except (ValueError, TypeError):
            kickoff = None
    if kickoff is None:
        try:
            kickoff = datetime.strptime(
                f"{pick.get('date')} {pick.get('time')}", "%d/%m/%Y %H:%M"
            ).replace(tzinfo=LOCAL_TZ)
        except (ValueError, TypeError):
            return False
    return kickoff > now_local() + timedelta(minutes=MIN_MINUTES_BEFORE_KICKOFF)


def filter_safe_picks(picks):
    return [pick for pick in picks if pick_is_safe(pick)]


# --------------------- Couche API robuste ------------------

def _paced_request():
    """Serialise les appels et impose un intervalle minimum entre eux."""
    global _last_request_at
    with _request_lock:
        elapsed = time.monotonic() - _last_request_at
        wait_for = MIN_REQUEST_INTERVAL - elapsed
        if wait_for > 0:
            time.sleep(wait_for)
        _last_request_at = time.monotonic()


def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("Cle API-Football absente.")

    url = f"{API_BASE}/{endpoint}"
    last_error = None

    for attempt in range(MAX_429_RETRIES + 1):
        _paced_request()
        try:
            response = requests.get(
                url,
                headers={"x-apisports-key": API_KEY},
                params=params or {},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = RuntimeError(f"Erreur reseau API-Football: {str(exc)[:200]}")
            if attempt < MAX_429_RETRIES:
                time.sleep(min(5 * (attempt + 1), 15))
                continue
            raise last_error from exc

        if response.status_code == 429:
            detail = "Limite de requetes par minute atteinte."
            try:
                body = response.json()
                detail = body.get("errors") or body.get("message") or detail
            except ValueError:
                pass
            last_error = RuntimeError(
                f"API-Football HTTP 429: {str(detail)[:220]}"
            )
            if attempt < MAX_429_RETRIES:
                delay = RETRY_BASE_SECONDS * (attempt + 1)
                logger.warning(
                    "HTTP 429 sur %s. Nouvelle tentative dans %s secondes (%s/%s).",
                    endpoint, delay, attempt + 1, MAX_429_RETRIES
                )
                time.sleep(delay)
                continue
            raise last_error

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            try:
                body = response.json()
                detail = body.get("errors") or body.get("message") or response.text[:250]
            except ValueError:
                detail = response.text[:250]
            raise RuntimeError(
                f"API-Football HTTP {response.status_code}: {str(detail)[:220]}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Reponse API-Football invalide : JSON illisible.") from exc

        if data.get("errors"):
            raise RuntimeError(f"Erreur API-Football : {str(data['errors'])[:220]}")
        return data

    raise last_error or RuntimeError("Echec API-Football inconnu.")


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
    away = teams.get("away", {}).get("name", "Exterieur")
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


# --------------- Comparaison premium API ------------------

COMPARISON_LABELS = {
    "form": "forme",
    "att": "attaque",
    "def": "defense",
    "poisson_distribution": "Poisson",
    "goals": "buts",
    "h2h": "confrontations",
}


def comparison_quality(prediction_data, side):
    comparison = prediction_data.get("comparison", {})
    if not isinstance(comparison, dict):
        return None, [], []

    weights = {
        "form": 1.25,
        "att": 1.0,
        "def": 1.0,
        "poisson_distribution": 1.25,
        "goals": 1.0,
        "h2h": 0.50,
    }
    values, details = [], []
    favorable = 0

    for key, label in COMPARISON_LABELS.items():
        block = comparison.get(key)
        if not isinstance(block, dict):
            continue
        own = parse_percent(block.get(side))
        opponent = parse_percent(block.get("away" if side == "home" else "home"))
        if own is None or opponent is None:
            continue

        # Un ecart tres faible ne compte pas comme un avantage net.
        if own >= opponent + 3:
            favorable += 1
        values.append((own, weights[key]))
        details.append(f"{label}: {own:.0f}-{opponent:.0f}")

    if len(values) < MIN_COMPARISON_METRICS:
        return None, details, []

    weighted_score = sum(v * w for v, w in values) / sum(w for _, w in values)
    support_ratio = favorable / len(values)
    quality = weighted_score * 0.75 + support_ratio * 100 * 0.25

    # La defense doit etre presente et ne pas etre nettement defavorable.
    defense = comparison.get("def")
    if isinstance(defense, dict):
        own_def = parse_percent(defense.get(side))
        opp_def = parse_percent(defense.get("away" if side == "home" else "home"))
        if own_def is not None and opp_def is not None and own_def + 10 < opp_def:
            quality -= 8.0
    else:
        return None, details, []

    return round(max(0.0, min(100.0, quality)), 1), details, values


def extract_markets(prediction_data):
    predictions = prediction_data.get("predictions", {})
    if not isinstance(predictions, dict):
        return {"markets": [], "reason": "Predictions absentes"}

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
        return {"markets": [], "reason": f"Somme 1N2 incoherente ({total:.1f}%)"}

    candidates = []
    if MIN_WIN_PERCENT <= home <= MAX_ACCEPTED_PERCENT:
        candidates.append(("1", "Victoire domicile", home, "home", "win"))
    if MIN_WIN_PERCENT <= away <= MAX_ACCEPTED_PERCENT:
        candidates.append(("2", "Victoire exterieure", away, "away", "win"))

    for selection, value, side in (
        ("1X", home + draw, "home"),
        ("X2", away + draw, "away"),
    ):
        value = min(100.0, value)
        if MIN_DOUBLE_CHANCE_PERCENT <= value <= MAX_ACCEPTED_PERCENT:
            candidates.append((selection, "Double chance", value, side, "double"))

    markets, rejected = [], []
    for selection, label, estimate, side, market_type in candidates:
        quality, details, _ = comparison_quality(prediction_data, side)
        if quality is None:
            rejected.append(f"{selection}: comparaison ou defense insuffisante")
            continue
        if quality < MIN_QUALITY_SCORE:
            rejected.append(f"{selection}: indice qualite {quality:.1f}/100 trop faible")
            continue
        markets.append({
            "market": label,
            "selection": selection,
            "percent": estimate,
            "type": market_type,
            "quality": quality,
            "comparison_details": details,
        })

    markets.sort(key=lambda item: (item["quality"], item["percent"]), reverse=True)
    return {
        "markets": markets,
        "reason": "; ".join(rejected[:3]) if not markets and rejected else None,
        "home_percent": home,
        "draw_percent": draw,
        "away_percent": away,
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
        logger.warning("Fichier de suivi illisible : suivi vide.")
        return {"picks": []}


def save_store(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
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
            "kickoff_at": pick.get("kickoff_at"),
            "market": pick["market"],
            "selection": pick["selection"],
            "market_type": pick["market_type"],
            "estimated_percent": pick["percent"],
            "quality_score": pick["quality"],
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

    score = fixture.get("score", {})
    fulltime = score.get("fulltime") if isinstance(score, dict) else None
    if isinstance(fulltime, dict):
        home_goals, away_goals = fulltime.get("home"), fulltime.get("away")
    else:
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
    else:
        return None

    pick["score"] = f"{home_goals}-{away_goals}"
    pick["result"] = "win" if outcome else "loss"
    return pick["result"]


async def refresh_results():
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

    updated = checked_dates = failed_dates = 0
    errors = []
    for match_date in sorted(dates_to_check):
        try:
            data = await api_call("fixtures", {"date": match_date, "timezone": TIMEZONE})
            fixtures = get_response_list(data)
            by_id = {
                str(f.get("fixture", {}).get("id")): f
                for f in fixtures
                if f.get("fixture", {}).get("id") is not None
            }
            checked_dates += 1
            for pick in pending:
                fixture = by_id.get(str(pick.get("fixture_id")))
                if fixture:
                    if settle_pick(pick, fixture):
                        updated += 1
        except Exception as error:
            failed_dates += 1
            errors.append(f"{match_date}: {str(error)[:200]}")
            logger.warning("Erreur actualisation bilan %s: %s", match_date, error)

    save_store(store)
    return store, updated, checked_dates, failed_dates, errors


# ---------------------- Moteur premium ----------------------

async def analyze_all_matches():
    current_date = today()
    if (
        analysis_cache["date"] == current_date
        and analysis_cache["result"] is not None
        and time.time() - analysis_cache["time"] < CACHE_DURATION
    ):
        cached_result = analysis_cache["result"]
        cached_result["picks"] = filter_safe_picks(cached_result.get("picks", []))
        cached_result["generated_at"] = now_local().strftime("%d/%m/%Y %H:%M:%S")
        return cached_result

    status_data = await api_call("status")
    current, limit = get_status_info(status_data)
    fixtures_data = await api_call(
        "fixtures", {"date": current_date, "timezone": TIMEZONE}
    )
    fixtures = get_response_list(fixtures_data)

    upcoming = [f for f in fixtures if is_upcoming(f)]
    upcoming.sort(key=lambda f: f.get("fixture", {}).get("date", ""))

    if isinstance(current, int) and isinstance(limit, int):
        # Deux appels fixes (status + fixtures) et une reserve pour /bilan.
        available = max(0, limit - current - RESERVE_API_CALLS - 2)
        prediction_budget = min(MAX_PREDICTION_CALLS, available)
    else:
        prediction_budget = min(4, MAX_PREDICTION_CALLS)

    safe_upcoming = [f for f in upcoming if is_safe_for_pick(f)]
    chosen_fixtures = spread_fixtures(safe_upcoming, prediction_budget)
    candidates = []
    attempts = analyzed = no_prediction = invalid_data = api_errors = 0
    error_details = []

    for fixture in chosen_fixtures:
        if not is_safe_for_pick(fixture):
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
                "kickoff_at": parse_fixture_date(fixture).isoformat(),
                "market": best["market"],
                "selection": best["selection"],
                "percent": best["percent"],
                "quality": best["quality"],
                "comparison_details": best["comparison_details"],
                "market_type": best["type"],
                "home_percent": analysis["home_percent"],
                "draw_percent": analysis["draw_percent"],
                "away_percent": analysis["away_percent"],
                "advice": analysis.get("advice"),
                "under_over": analysis.get("under_over"),
            })
        except Exception as error:
            api_errors += 1
            detail = f"Match {fixture_id}: {str(error)[:170]}"
            if len(error_details) < 5:
                error_details.append(detail)
            logger.warning("Erreur prediction fixture %s: %s", fixture_id, error)

    candidates.sort(key=lambda item: (item["quality"], item["percent"]), reverse=True)
    selected, used_leagues = [], set()
    fixture_by_id = {
        str(f.get("fixture", {}).get("id")): f for f in upcoming
    }
    for candidate in candidates:
        if len(selected) >= MAX_DISPLAY_PICKS:
            break
        original = fixture_by_id.get(str(candidate["fixture_id"]))
        if not original or not is_safe_for_pick(original) or not pick_is_safe(candidate):
            continue
        if ONE_PICK_PER_LEAGUE and candidate["league"] in used_leagues:
            continue
        selected.append(candidate)
        used_leagues.add(candidate["league"])

    selected = filter_safe_picks(selected)
    tracked = register_picks(selected) if selected else 0
    result = {
        "date": current_date,
        "generated_at": now_local().strftime("%d/%m/%Y %H:%M:%S"),
        "total": len(fixtures),
        "upcoming": len(upcoming),
        "attempts": attempts,
        "analyzed": analyzed,
        "no_prediction": no_prediction,
        "invalid_data": invalid_data,
        "api_errors": api_errors,
        "error_details": error_details,
        "prediction_budget": prediction_budget,
        "picks": selected,
        "tracked": tracked,
    }
    analysis_cache.update({"time": time.time(), "date": current_date, "result": result})
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
        f"ð BIENVENUE SUR ALEPH BET AI {VERSION} !\n\n"
        "â½ Analyse multi-criteres des matchs a venir.\n"
        "ð Forme, attaque, defense, Poisson et confrontations si disponibles.\n"
        "ð¯ Maximum 3 selections, aucune selection forcee.\n"
        "ð Dates completes et heures de Cote d'Ivoire.\n"
        "ð Suivi des resultats reels avec /bilan.\n\n"
        "COMMANDES : /matchs /pronostics /bilan /statut /aide\n\n"
        "â ï¸ Les estimations et indices ne garantissent aucun gain."
    )


async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ð¤ ALEPH BET AI {VERSION}\n\n"
        "/start : Accueil\n/matchs : Matchs a venir aujourd'hui\n"
        "/pronostics : Analyse premium\n/bilan : Suivi des resultats\n"
        "/statut : Connexion et quota\n\n"
        "MARCHES : 1, 2, 1X et X2.\n"
        "Les marches Over/Under ne sont pas proposes sans probabilite fiable.\n\n"
        "L'indice qualite mesure la coherence des comparaisons API ; "
        "ce n'est pas une probabilite ni un taux historique."
    )


async def statut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call("status")
        current, limit = get_status_info(data)
        message = (
            f"ð ALEPH BET AI {VERSION}\n"
            "ð¢ Connexion API-Football : active\n"
            f"â±ï¸ Intervalle API : environ {MIN_REQUEST_INTERVAL:.0f} secondes\n"
        )
        if current is not None and limit is not None:
            message += (
                f"ð Requetes utilisees : {current}\n"
                f"ð¦ Limite quotidienne : {limit}\n"
                f"ð Restantes : {max(0, limit - current)}\n"
            )
        await update.message.reply_text(message)
    except Exception as error:
        logger.exception("Erreur statut")
        await update.message.reply_text("ð´ Connexion API impossible.\n" + str(error)[:250])


async def matchs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await api_call("fixtures", {"date": today(), "timezone": TIMEZONE})
        fixtures = [f for f in get_response_list(data) if is_upcoming(f)]
        fixtures.sort(key=lambda f: f.get("fixture", {}).get("date", ""))
        if not fixtures:
            await update.message.reply_text("â½ Aucun match a venir aujourd'hui.")
            return
        lines = [
            f"â½ MATCHS A VENIR â {now_local().strftime('%d/%m/%Y')}",
            f"Total : {len(fixtures)}", "",
        ]
        for fixture in fixtures[:30]:
            lines.extend([
                f"ð {format_match_date(fixture)}",
                f"ð {format_match_time(fixture)} (Cote d'Ivoire)",
                f"ð {get_league_name(fixture)}",
                f"â½ {get_fixture_name(fixture)}", "",
            ])
        await send_long_message(update, "\n".join(lines))
    except Exception as error:
        logger.exception("Erreur matchs")
        await update.message.reply_text("â Erreur recuperation matchs : " + str(error)[:250])


async def pronostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ð ALEPH BET AI {VERSION}\n"
        "ð Analyse premium des matchs a venir...\n"
        "â±ï¸ Requetes API espacees pour eviter les limites par minute...\n"
        "ð¡ï¸ Filtrage des donnees insuffisantes..."
    )
    try:
        result = await analyze_all_matches()
        result["picks"] = filter_safe_picks(result.get("picks", []))
        result["generated_at"] = now_local().strftime("%d/%m/%Y %H:%M:%S")
        lines = [
            f"ð ALEPH BET AI {VERSION} â RAPPORT PREMIUM",
            f"ð Date : {result['date']}",
            f"ð Rapport genere : {result['generated_at']} (Cote d'Ivoire)",
            f"ð¡ï¸ Marge avant coup d'envoi : {MIN_MINUTES_BEFORE_KICKOFF} min",
            f"â½ Matchs recuperes : {result['total']}",
            f"ð Matchs a venir : {result['upcoming']}",
            f"ð Tentatives d'analyse : {result['attempts']}",
            f"ð Reponses API : {result['analyzed']}",
            f"ð­ Sans prediction : {result['no_prediction']}",
            f"ð§¹ Donnees ecartees : {result['invalid_data']}",
            f"â ï¸ Erreurs API : {result['api_errors']}",
            f"ð Budget d'analyse : {result['prediction_budget']} appels maximum", "",
        ]
        if result["error_details"]:
            lines.extend(["DETAIL DES ERREURS :"])
            lines.extend(f"â¢ {item}" for item in result["error_details"])
            lines.append("")

        if not result["picks"]:
            lines.extend([
                "ð AUCUNE SELECTION ASSEZ SOLIDE",
                "Les donnees disponibles ne passent pas les filtres premium.",
                "Le robot prefere s'abstenir plutot que forcer un pronostic.",
            ])
        else:
            lines.extend([
                f"ð¯ {len(result['picks'])} SELECTION(S) RETENUE(S)",
                "Matchs a retrouver manuellement sur 1xBet.", "",
            ])
            for index, pick in enumerate(result["picks"], 1):
                lines.extend([
                    "ââââââââââââââââ",
                    f"ð SELECTION #{index}",
                    f"â½ {pick['match']}",
                    f"ð {pick['league']}",
                    f"ð Date : {pick['date']}",
                    f"ð Heure : {pick['time']} (Cote d'Ivoire)", "",
                    f"ð¯ Marche : {pick['market']}",
                    f"ð Choix : {pick['selection']}",
                    f"ð Estimation API : {pick['percent']:.1f} %",
                    f"ð§  Indice qualite : {pick['quality']:.1f}/100",
                    (
                        "ð Base 1N2 API : "
                        f"1={pick['home_percent']:.1f}% | "
                        f"N={pick['draw_percent']:.1f}% | "
                        f"2={pick['away_percent']:.1f}%"
                    ),
                    "ð¬ Comparaison : " + " | ".join(pick["comparison_details"]),
                ])
                if pick.get("advice"):
                    lines.append(f"ð¡ Conseil API : {str(pick['advice'])[:180]}")
                if pick.get("under_over"):
                    lines.append(
                        f"â½ Indication buts API : {str(pick['under_over'])[:80]} "
                        "(sans probabilite chiffree)"
                    )
                lines.append("")

        lines.extend([
            "ââââââââââââââââ", "",
            "â¹ï¸ L'estimation API n'est pas un taux de reussite historique.",
            "â¹ï¸ L'indice qualite est un score interne, pas une probabilite.",
            "â¹ï¸ Les comparaisons API ne remplacent pas un historique verifie.",
            "â¹ï¸ Aucune cote ni disponibilite 1xBet n'est verifiee.",
            "â ï¸ Aucun pari n'est sur. Ne mise que ce que tu peux perdre.",
            f"ð Nouvelles selections ajoutees au suivi : {result['tracked']}",
            "Utilise /bilan pour consulter les resultats suivis.",
        ])
        await send_long_message(update, "\n".join(lines))
    except Exception as error:
        logger.exception("Erreur pronostics")
        await update.message.reply_text("â Erreur pendant l'analyse.\n" + str(error)[:250])


async def bilan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("ð Mise a jour des resultats en cours...")
        store, updated, checked_dates, failed_dates, errors = await refresh_results()
        picks = store["picks"]
        wins = sum(1 for p in picks if p.get("result") == "win")
        losses = sum(1 for p in picks if p.get("result") == "loss")
        pending = sum(1 for p in picks if p.get("result") == "pending")
        settled = wins + losses
        rate = wins / settled * 100 if settled else None

        lines = [
            f"ð ALEPH BET AI {VERSION} â BILAN",
            f"ð Nouveaux resultats verifies : {updated}",
            f"ð Journees consultees avec succes : {checked_dates}",
            f"â ï¸ Journees en erreur : {failed_dates}",
            f"â Gagnes : {wins}",
            f"â Perdus : {losses}",
            f"â³ En attente : {pending}",
            f"ð Total enregistre : {len(picks)}",
        ]
        if errors:
            lines.extend(["", "DETAIL DES ERREURS API :"])
            lines.extend(f"â¢ {detail}" for detail in errors[:3])
        if rate is not None:
            lines.append(f"ð Reussite observee : {rate:.1f}% ({wins}/{settled})")
        else:
            lines.append("ð Taux reel : pas encore calculable (aucun match termine).")

        lines.extend(["", "DERNIERES SELECTIONS :"])
        for pick in reversed(picks[-10:]):
            icon = {"win": "â", "loss": "â", "pending": "â³"}.get(
                pick.get("result"), "â"
            )
            score = f" â {pick['score']}" if pick.get("score") else ""
            lines.append(
                f"{icon} {pick.get('date')} {pick.get('time')} | "
                f"{pick.get('match')} | {pick.get('selection')}{score}"
            )
        lines.extend([
            "",
            "â ï¸ Bilan limite aux selections enregistrees par le bot.",
            "Il ne prouve pas un benefice financier et ne garantit pas les prochains resultats.",
            "â ï¸ Sur Render gratuit, le fichier JSON local peut etre perdu lors d'un redemarrage ou redeploiement.",
        ])
        await send_long_message(update, "\n".join(lines))
    except Exception as error:
        logger.exception("Erreur bilan")
        await update.message.reply_text("â Impossible d'actualiser le bilan : " + str(error)[:250])


# ------------------------- Demarrage -----------------------

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

    logger.info("ALEPH BET AI %s demarre...", VERSION)
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
