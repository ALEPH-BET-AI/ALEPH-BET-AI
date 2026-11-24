import os
import json
import time
import logging
import asyncio
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ============================================================
# ALEPH BET AI 5.0 — DATA SPORTS API — MULTI-SPORTS
# Football + Basketball + Ice Hockey
#
# Les données sont analysées séparément par sport.
# L'indice interne n'est PAS une probabilité.
# Le plan gratuit DataSportsAPI ne fournit pas les cotes.
# Aucun pari n'est garanti et aucune sélection n'est forcée.
# ============================================================

VERSION = "5.0 DATA SPORTS MULTI-SPORTS"
TIMEZONE = "Africa/Abidjan"
TZ = ZoneInfo(TIMEZONE)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))

DATASPORTS_API_KEY = os.getenv("DATASPORTS_API_KEY", "").strip()
API_BASE = "https://api.datasportsapi.com"

SPORTS = {
    "football": {
        "slug": "football",
        "label": "⚽ FOOTBALL",
    },
    "basketball": {
        "slug": "basketball",
        "label": "🏀 BASKETBALL",
    },
    "hockey": {
        "slug": "ice-hockey",
        "label": "🏒 HOCKEY SUR GLACE",
    },
}

# Limites prudentes pour le plan gratuit.
CACHE_SECONDS = 900
MIN_REQUEST_INTERVAL = 6.2  # environ 9,7 requêtes/minute au maximum
DAYS_AHEAD = 2
MAX_FIXTURE_PAGES_PER_DAY = 2
MAX_ANALYSIS_CANDIDATES = 3
MAX_PICKS_PER_SPORT = 2
MAX_RESULT_CHECKS_PER_BILAN = 5
MIN_KICKOFF_MARGIN_MINUTES = 30

# Le fichier est local au service Render. Sur le plan gratuit, le système
# de fichiers peut être réinitialisé lors d'un redémarrage/redéploiement.
HISTORY_FILE = Path(
    os.getenv("ALEPH_DATA_FILE", "aleph_bet_history.json")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("aleph-bet-ai")

CACHE = {}
CACHE_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
LAST_REQUEST_AT = 0.0


# ------------------------- Horloge -------------------------

def now_local():
    return datetime.now(TZ)


def date_local(offset=0):
    # Côte d'Ivoire est en UTC : la date locale correspond à la date UTC.
    return (now_local().date() + timedelta(days=offset)).isoformat()


def parse_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def format_datetime(value):
    dt = parse_datetime(value)
    return dt.strftime("%d/%m/%Y à %H:%M") if dt else "Date non disponible"


# ------------------------- API DataSportsAPI -------------------------

def wait_for_api_slot():
    """Respecte la limite de 10 requêtes/minute du compte gratuit."""
    global LAST_REQUEST_AT
    with RATE_LOCK:
        elapsed = time.monotonic() - LAST_REQUEST_AT
        wait_seconds = MIN_REQUEST_INTERVAL - elapsed
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        LAST_REQUEST_AT = time.monotonic()


def api_get(path, params=None):
    if not DATASPORTS_API_KEY:
        raise RuntimeError(
            "Variable DATASPORTS_API_KEY absente dans Render."
        )

    wait_for_api_slot()

    url = API_BASE + path
    headers = {
        "Authorization": f"Bearer {DATASPORTS_API_KEY}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params or {},
            timeout=25,
        )
    except requests.Timeout:
        raise RuntimeError("DataSportsAPI ne répond pas dans le délai prévu.")
    except requests.RequestException as exc:
        raise RuntimeError(f"Connexion DataSportsAPI impossible : {exc}")

    if response.status_code == 401:
        raise RuntimeError(
            "Clé DataSportsAPI refusée (401). Vérifie DATASPORTS_API_KEY dans Render."
        )
    if response.status_code == 403:
        raise RuntimeError(
            "Accès refusé par DataSportsAPI (403). Vérifie le plan ou l'accès aux données."
        )
    if response.status_code == 404:
        raise RuntimeError("Donnée introuvable chez DataSportsAPI (404).")
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        suffix = f" Réessaie dans {retry_after} secondes." if retry_after else " Réessaie plus tard."
        raise RuntimeError(
            "Limite de requêtes ou quota DataSportsAPI atteint (429)." + suffix
        )
    if response.status_code >= 500:
        raise RuntimeError(
            f"DataSportsAPI rencontre une erreur serveur ({response.status_code})."
        )

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("Réponse JSON DataSportsAPI illisible.")

    if response.status_code >= 400:
        detail = payload.get("errors") or payload.get("error") or "Erreur API"
        raise RuntimeError(f"DataSportsAPI HTTP {response.status_code}: {str(detail)[:180]}")

    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"Erreur DataSportsAPI : {str(errors)[:220]}")

    return payload


async def cached_api_get(path, params=None):
    params = params or {}
    cache_key = (
        path,
        tuple(sorted((str(key), str(value)) for key, value in params.items())),
    )

    with CACHE_LOCK:
        saved = CACHE.get(cache_key)
        if saved and time.monotonic() - saved[0] < CACHE_SECONDS:
            return saved[1]

    payload = await asyncio.to_thread(api_get, path, params)

    with CACHE_LOCK:
        CACHE[cache_key] = (time.monotonic(), payload)

    return payload


def payload_data(payload, default=None):
    if not isinstance(payload, dict):
        return default
    return payload.get("data", default)


# ------------------------- Lecture des matchs -------------------------

def fixture_status(fixture):
    status = fixture.get("status")
    if not isinstance(status, dict):
        return ""
    return str(status.get("type") or "").lower()


def fixture_home(fixture):
    side = fixture.get("home")
    return side if isinstance(side, dict) else {}


def fixture_away(fixture):
    side = fixture.get("away")
    return side if isinstance(side, dict) else {}


def team_name(side):
    return str(side.get("name") or "Équipe inconnue")


def fixture_names(fixture):
    return team_name(fixture_home(fixture)), team_name(fixture_away(fixture))


def fixture_league(fixture):
    league = fixture.get("league")
    if isinstance(league, dict):
        return str(league.get("name") or "Compétition non précisée")
    return "Compétition non précisée"


def fixture_is_eligible(fixture):
    if not isinstance(fixture, dict):
        return False

    # Seuls les matchs explicitement pas encore commencés sont acceptés.
    if fixture_status(fixture) != "notstarted":
        return False

    kickoff = parse_datetime(fixture.get("startTime"))
    if kickoff is None:
        return False

    cutoff = now_local() + timedelta(minutes=MIN_KICKOFF_MARGIN_MINUTES)
    if kickoff <= cutoff:
        return False

    home, away = fixture_names(fixture)
    if home == "Équipe inconnue" or away == "Équipe inconnue":
        return False

    if not fixture.get("id"):
        return False

    return True


def fixture_level_priority(fixture):
    levels = fixture.get("levels") or []
    labels = []

    for item in levels:
        if isinstance(item, str):
            labels.append(item.lower())
        elif isinstance(item, dict):
            labels.extend(str(value).lower() for value in item.values())

    if any("topcompetition" in value for value in labels):
        return 0
    if any(value == "pro" or value.endswith(":pro") for value in labels):
        return 1
    return 2


async def get_upcoming_fixtures(sport_key):
    config = SPORTS[sport_key]
    slug = config["slug"]
    fixtures_by_id = {}
    partial_coverage = False

    for day_offset in range(DAYS_AHEAD):
        day = date_local(day_offset)

        for page in range(MAX_FIXTURE_PAGES_PER_DAY):
            payload = await cached_api_get(
                "/v2/fixtures",
                {"date": day, "sport": slug, "page": page},
            )

            rows = payload_data(payload, [])
            if isinstance(rows, list):
                for fixture in rows:
                    if isinstance(fixture, dict) and fixture.get("id"):
                        fixtures_by_id[str(fixture["id"])] = fixture

            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            if isinstance(meta, dict) and meta.get("complete") is False:
                partial_coverage = True

            has_next = bool(meta.get("hasNextPage")) if isinstance(meta, dict) else False
            if not has_next:
                break

            if page == MAX_FIXTURE_PAGES_PER_DAY - 1:
                partial_coverage = True

    eligible = [
        fixture
        for fixture in fixtures_by_id.values()
        if fixture_is_eligible(fixture)
    ]

    eligible.sort(
        key=lambda item: (
            fixture_level_priority(item),
            parse_datetime(item.get("startTime")) or now_local(),
        )
    )

    return eligible, partial_coverage


# ------------------------- Analyse sportive -------------------------

def normalize_result(value):
    value = str(value or "").strip().lower()

    if value in {"win", "won", "w", "victory"}:
        return "win"
    if value in {"draw", "tie", "d", "drawn"}:
        return "draw"
    if value in {"loss", "lost", "l", "defeat"}:
        return "loss"

    return None


def extract_results(team_form_rows, competition_results):
    results = []

    if isinstance(team_form_rows, list):
        for row in team_form_rows:
            if isinstance(row, dict):
                result = normalize_result(row.get("result"))
                if result:
                    results.append(result)

    if len(results) >= 3:
        return results[-5:]

    fallback = []
    if isinstance(competition_results, list):
        for value in competition_results:
            result = normalize_result(value)
            if result:
                fallback.append(result)

    return (fallback or results)[-5:]


def form_rate(results):
    if not results:
        return None

    points = {
        "win": 1.0,
        "draw": 0.5,
        "loss": 0.0,
    }
    return sum(points[item] for item in results) / len(results)


def form_text(results):
    symbols = {
        "win": "V",
        "draw": "N",
        "loss": "D",
    }
    return "-".join(symbols[item] for item in results) if results else "indisponible"


def safe_number(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (ValueError, TypeError):
        return None


def average_recent_goals(home_rows, away_rows):
    totals = []

    for rows in (home_rows, away_rows):
        if not isinstance(rows, list):
            continue

        for row in rows:
            if not isinstance(row, dict):
                continue
            scored = safe_number(row.get("scoreFor"))
            conceded = safe_number(row.get("scoreAgainst"))
            if scored is not None and conceded is not None:
                totals.append(scored + conceded)

    return (sum(totals) / len(totals)) if totals else None


def clamp(value, low, high):
    return max(low, min(high, value))


async def analyze_fixture(sport_key, fixture):
    fixture_id = fixture.get("id")

    try:
        form_payload = await cached_api_get(
            f"/v2/fixtures/{fixture_id}/form"
        )
        team_form_payload = await cached_api_get(
            f"/v2/fixtures/{fixture_id}/team-form"
        )
    except Exception as exc:
        log.warning("Données de forme indisponibles pour %s : %s", fixture_id, exc)
        return None

    form_data = payload_data(form_payload, {})
    team_data = payload_data(team_form_payload, {})

    if not isinstance(form_data, dict):
        form_data = {}
    if not isinstance(team_data, dict):
        team_data = {}

    home_form_data = form_data.get("home") or {}
    away_form_data = form_data.get("away") or {}
    if not isinstance(home_form_data, dict):
        home_form_data = {}
    if not isinstance(away_form_data, dict):
        away_form_data = {}

    home_rows = team_data.get("home") or []
    away_rows = team_data.get("away") or []

    home_results = extract_results(
        home_rows,
        home_form_data.get("results", []),
    )
    away_results = extract_results(
        away_rows,
        away_form_data.get("results", []),
    )

    # Pas de sélection si l'API ne fournit pas assez d'historique des deux côtés.
    if len(home_results) < 3 or len(away_results) < 3:
        return None

    home_rate = form_rate(home_results)
    away_rate = form_rate(away_results)
    if home_rate is None or away_rate is None:
        return None

    h2h_data = {}
    try:
        h2h_payload = await cached_api_get(
            f"/v2/fixtures/{fixture_id}/h2h"
        )
        raw_h2h = payload_data(h2h_payload, {})
        if isinstance(raw_h2h, dict):
            h2h_data = raw_h2h
    except Exception as exc:
        # Le face-à-face reste facultatif : l'analyse peut continuer sans lui.
        log.info("H2H indisponible pour %s : %s", fixture_id, exc)

    h2h_home = safe_number(h2h_data.get("homeWins")) or 0.0
    h2h_draws = safe_number(h2h_data.get("draws")) or 0.0
    h2h_away = safe_number(h2h_data.get("awayWins")) or 0.0
    h2h_total = h2h_home + h2h_draws + h2h_away
    h2h_edge = (
        (h2h_home - h2h_away) / h2h_total
        if h2h_total > 0
        else 0.0
    )

    home_position = safe_number(home_form_data.get("position"))
    away_position = safe_number(away_form_data.get("position"))
    position_edge = 0.0
    if (
        home_position is not None
        and away_position is not None
        and home_position > 0
        and away_position > 0
    ):
        position_edge = clamp(
            (away_position - home_position) / max(home_position, away_position, 10.0),
            -1.0,
            1.0,
        )

    home_rating = safe_number(home_form_data.get("avgRating"))
    away_rating = safe_number(away_form_data.get("avgRating"))
    rating_edge = 0.0
    if home_rating is not None and away_rating is not None:
        rating_edge = clamp((home_rating - away_rating) / 2.0, -1.0, 1.0)

    # Indice de cohérence interne, jamais une probabilité.
    form_edge = home_rate - away_rate
    internal_score = round(
        clamp(
            50
            + 30 * form_edge
            + 10 * h2h_edge
            + 6 * position_edge
            + 4 * rating_edge
            + 4,  # léger avantage du terrain
            0,
            100,
        )
    )

    home_name, away_name = fixture_names(fixture)
    league = fixture_league(fixture)
    kickoff = fixture.get("startTime")

    goal_average = average_recent_goals(home_rows, away_rows)
    market = None
    selection_side = None
    selection = None
    quality_rank = abs(internal_score - 50)

    if sport_key == "football":
        if internal_score >= 70:
            market = "Victoire de l'équipe à domicile (1)"
            selection_side = "home"
            selection = home_name
        elif internal_score <= 30:
            market = "Victoire de l'équipe à l'extérieur (2)"
            selection_side = "away"
            selection = away_name
        elif internal_score >= 62:
            market = "Double chance 1X"
            selection_side = "home_or_draw"
            selection = f"{home_name} ou nul"
        elif internal_score <= 38:
            market = "Double chance X2"
            selection_side = "away_or_draw"
            selection = f"{away_name} ou nul"
        elif goal_average is not None and goal_average >= 3.0:
            market = "Plus de 2,5 buts"
            selection_side = "over_2_5"
            selection = "Plus de 2,5 buts"
            quality_rank = abs(goal_average - 2.5) * 20
        elif goal_average is not None and goal_average <= 2.0:
            market = "Moins de 2,5 buts"
            selection_side = "under_2_5"
            selection = "Moins de 2,5 buts"
            quality_rank = abs(goal_average - 2.5) * 20
    else:
        if internal_score >= 68:
            market = "Victoire de l'équipe à domicile"
            selection_side = "home"
            selection = home_name
        elif internal_score <= 32:
            market = "Victoire de l'équipe à l'extérieur"
            selection_side = "away"
            selection = away_name

    if not market:
        return None

    h2h_text = (
        f"{int(h2h_home)} victoire(s) domicile, "
        f"{int(h2h_draws)} nul(s), {int(h2h_away)} victoire(s) extérieur"
        if h2h_total > 0
        else "face-à-face non disponible"
    )

    reason = (
        f"Forme récente : {home_name} {form_text(home_results)} "
        f"({len(home_results)} matchs) / {away_name} {form_text(away_results)} "
        f"({len(away_results)} matchs). {h2h_text}."
    )

    if goal_average is not None and sport_key == "football":
        reason += f" Moyenne de buts observée sur les derniers matchs : {goal_average:.2f}."

    return {
        "sport": sport_key,
        "fixture_id": int(fixture_id),
        "home": home_name,
        "away": away_name,
        "league": league,
        "start_time": kickoff,
        "market": market,
        "selection": selection,
        "selection_side": selection_side,
        "internal_score": int(internal_score),
        "reason": reason,
        "quality_rank": float(quality_rank),
        "outcome": "pending",
        "recorded_at": now_local().isoformat(),
    }


# ------------------------- Historique et bilan -------------------------

def load_history():
    try:
        if not HISTORY_FILE.exists():
            return []
        with HISTORY_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Historique illisible : %s", exc)
        return []


def save_history(history):
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = HISTORY_FILE.with_suffix(HISTORY_FILE.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(history, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, HISTORY_FILE)
        return True
    except OSError as exc:
        log.error("Impossible d'enregistrer l'historique : %s", exc)
        return False


def record_picks(picks):
    if not picks:
        return

    history = load_history()
    known = {
        (str(item.get("sport")), str(item.get("fixture_id")), str(item.get("selection_side")))
        for item in history
        if isinstance(item, dict)
    }

    changed = False
    for pick in picks:
        key = (
            str(pick.get("sport")),
            str(pick.get("fixture_id")),
            str(pick.get("selection_side")),
        )
        if key in known:
            continue
        history.append(pick)
        known.add(key)
        changed = True

    if changed:
        save_history(history)


def winner_from_fixture(fixture):
    winner = fixture.get("winner")
    if winner in {"home", "away", "draw"}:
        return winner

    home_score = fixture.get("homeScore")
    away_score = fixture.get("awayScore")
    home_score = home_score.get("current") if isinstance(home_score, dict) else None
    away_score = away_score.get("current") if isinstance(away_score, dict) else None

    home_score = safe_number(home_score)
    away_score = safe_number(away_score)

    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


def evaluate_pick(pick, fixture):
    side = pick.get("selection_side")

    if side == "over_2_5" or side == "under_2_5":
        home_score = fixture.get("homeScore")
        away_score = fixture.get("awayScore")
        home_score = home_score.get("current") if isinstance(home_score, dict) else None
        away_score = away_score.get("current") if isinstance(away_score, dict) else None
        home_score = safe_number(home_score)
        away_score = safe_number(away_score)

        if home_score is None or away_score is None:
            return None

        total_goals = home_score + away_score
        if side == "over_2_5":
            return "won" if total_goals > 2.5 else "lost"
        return "won" if total_goals < 2.5 else "lost"

    winner = winner_from_fixture(fixture)
    if winner is None:
        return None

    if side == "home":
        return "won" if winner == "home" else "lost"
    if side == "away":
        return "won" if winner == "away" else "lost"
    if side == "home_or_draw":
        return "won" if winner in {"home", "draw"} else "lost"
    if side == "away_or_draw":
        return "won" if winner in {"away", "draw"} else "lost"

    return None


async def update_pending_results(history):
    pending = []

    for pick in history:
        if not isinstance(pick, dict) or pick.get("outcome") != "pending":
            continue

        kickoff = parse_datetime(pick.get("start_time"))
        if kickoff and kickoff + timedelta(hours=3) <= now_local():
            pending.append(pick)

    updated = False

    for pick in pending[:MAX_RESULT_CHECKS_PER_BILAN]:
        fixture_id = pick.get("fixture_id")
        if not fixture_id:
            continue

        try:
            payload = await cached_api_get(f"/v2/fixtures/{fixture_id}")
            fixture = payload_data(payload, {})
            if not isinstance(fixture, dict):
                continue

            status = fixture_status(fixture)

            if status in {"canceled", "cancelled"}:
                pick["outcome"] = "void"
                pick["settled_at"] = now_local().isoformat()
                updated = True
                continue

            if status != "finished":
                continue

            if fixture.get("awarded") is True:
                pick["outcome"] = "void"
            else:
                outcome = evaluate_pick(pick, fixture)
                if outcome is None:
                    continue
                pick["outcome"] = outcome

            pick["settled_at"] = now_local().isoformat()
            updated = True

        except Exception as exc:
            log.info("Résultat non actualisé pour %s : %s", fixture_id, exc)

    if updated:
        save_history(history)

    return updated


# ------------------------- Commandes Telegram -------------------------

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    if not DATASPORTS_API_KEY:
        await message.reply_text(
            f"👑 ALEPH BET AI {VERSION}\n"
            "🔴 DATASPORTS_API_KEY n'est pas configurée dans Render."
        )
        return

    try:
        payload = await cached_api_get("/v2/sports")
        data = payload_data(payload, [])
        count = len(data) if isinstance(data, list) else "inconnu"
        await message.reply_text(
            f"👑 ALEPH BET AI {VERSION}\n"
            "🟢 Connexion DataSportsAPI : accessible\n"
            f"📚 Sports renvoyés par l'API : {count}\n"
            "🔐 Clé utilisée côté serveur, jamais affichée."
        )
    except Exception as exc:
        await message.reply_text(
            f"👑 ALEPH BET AI {VERSION}\n"
            f"🔴 DataSportsAPI : {str(exc)[:300]}"
        )


async def analysis_command(update: Update, sport_key):
    message = update.effective_message
    if not message:
        return

    if not DATASPORTS_API_KEY:
        await message.reply_text(
            "⚠️ La variable DATASPORTS_API_KEY doit être ajoutée dans Render."
        )
        return

    try:
        fixtures, partial = await get_upcoming_fixtures(sport_key)
    except Exception as exc:
        await message.reply_text(
            f"⚠️ Impossible de récupérer les matchs {sport_key} :\n{str(exc)[:320]}"
        )
        return

    if not fixtures:
        await message.reply_text(
            f"{SPORTS[sport_key]['label']}\n"
            "📭 Aucun match admissible trouvé dans les deux prochains jours.\n"
            f"Les matchs à moins de {MIN_KICKOFF_MARGIN_MINUTES} minutes du coup d'envoi "
            "sont volontairement exclus."
        )
        return

    candidates = fixtures[:MAX_ANALYSIS_CANDIDATES]
    picks = []

    for fixture in candidates:
        result = await analyze_fixture(sport_key, fixture)
        if result:
            picks.append(result)

    # Une seule sélection par compétition pour éviter de concentrer les choix.
    picks.sort(key=lambda item: item.get("quality_rank", 0), reverse=True)
    selected = []
    used_leagues = set()

    for pick in picks:
        league_key = str(pick.get("league") or "").lower()
        if league_key in used_leagues:
            continue
        selected.append(pick)
        used_leagues.add(league_key)
        if len(selected) >= MAX_PICKS_PER_SPORT:
            break

    record_picks(selected)

    lines = [
        f"👑 ALEPH BET AI {VERSION}",
        SPORTS[sport_key]["label"],
        f"🕒 Vérification : {now_local():%d/%m/%Y %H:%M} (Côte d'Ivoire)",
        f"🔎 Matchs examinés : {len(candidates)}",
        "",
    ]

    if not selected:
        lines += [
            "📭 Aucune sélection ne franchit les filtres aujourd'hui.",
            "Le bot préfère ne rien proposer plutôt que forcer un pronostic.",
        ]
    else:
        for index, pick in enumerate(selected, start=1):
            lines += [
                f"{index}. {pick['home']} — {pick['away']}",
                f"🏆 {pick['league']}",
                f"🗓 {format_datetime(pick['start_time'])}",
                f"🎯 Marché : {pick['market']}",
                f"📌 Sélection : {pick['selection']}",
                f"🧭 Indice de cohérence interne : {pick['internal_score']}/100",
                f"📝 {pick['reason']}",
                "",
            ]

    if partial:
        lines.append(
            "ℹ️ Couverture partielle : l'API signale d'autres pages ou une liste incomplète."
        )

    lines += [
        "",
        "⚠️ L'indice interne n'est pas une probabilité ni un taux de réussite.",
        "Le plan gratuit ne fournit pas les cotes. Aucun pari n'est sûr : reste prudent.",
    ]

    await message.reply_text("\n".join(lines))


async def foot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await analysis_command(update, "football")


async def basket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await analysis_command(update, "basketball")


async def hockey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await analysis_command(update, "hockey")


async def matchs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    try:
        fixtures, partial = await get_upcoming_fixtures("football")
    except Exception as exc:
        await message.reply_text(
            f"⚠️ Impossible de récupérer les matchs : {str(exc)[:300]}"
        )
        return

    if not fixtures:
        await message.reply_text("📭 Aucun match de football admissible trouvé.")
        return

    lines = [
        f"👑 ALEPH BET AI {VERSION} — ⚽ FOOTBALL",
        f"📅 Vérification : {now_local():%d/%m/%Y %H:%M}",
        "",
    ]

    for index, fixture in enumerate(fixtures[:12], start=1):
        home, away = fixture_names(fixture)
        lines.append(
            f"{index}. {home} — {away}\n"
            f"🏆 {fixture_league(fixture)}\n"
            f"🗓 {format_datetime(fixture.get('startTime'))}"
        )

    if partial:
        lines.append(
            "\nℹ️ Liste partielle : l'API indique qu'il existe d'autres pages."
        )

    lines.append(
        "\nℹ️ Liste de matchs uniquement, sans recommandation de mise."
    )
    await message.reply_text("\n\n".join(lines))


async def bilan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    if not DATASPORTS_API_KEY:
        await message.reply_text(
            "⚠️ Configure DATASPORTS_API_KEY dans Render pour actualiser les résultats."
        )
        return

    history = load_history()
    if not history:
        await message.reply_text(
            "📊 BILAN ALEPH BET AI\n"
            "Aucune sélection enregistrée pour le moment."
        )
        return

    await update_pending_results(history)

    lines = [
        f"📊 BILAN ALEPH BET AI {VERSION}",
        f"🕒 Mis à jour : {now_local():%d/%m/%Y %H:%M}",
        "",
    ]

    labels = {
        "football": "⚽ Football",
        "basketball": "🏀 Basketball",
        "hockey": "🏒 Hockey",
    }

    for sport_key in ("football", "basketball", "hockey"):
        rows = [
            item for item in history
            if isinstance(item, dict) and item.get("sport") == sport_key
        ]
        if not rows:
            continue

        wins = sum(1 for item in rows if item.get("outcome") == "won")
        losses = sum(1 for item in rows if item.get("outcome") == "lost")
        voids = sum(1 for item in rows if item.get("outcome") == "void")
        pending = sum(1 for item in rows if item.get("outcome") == "pending")
        settled = wins + losses
        hit_rate = (wins / settled * 100) if settled else None

        rate_text = f"{hit_rate:.1f}%" if hit_rate is not None else "pas encore calculable"

        lines += [
            labels[sport_key],
            f"✅ Gagnés : {wins} | ❌ Perdus : {losses}",
            f"📈 Taux observé : {rate_text}",
            f"⏳ En attente : {pending} | Annulés : {voids}",
            "",
        ]

    lines += [
        "Le taux observé porte uniquement sur les sélections réglées et enregistrées par le bot.",
        "⚠️ Sur Render gratuit, cet historique local peut être perdu lors d'un redémarrage ou redéploiement.",
    ]

    await message.reply_text("\n".join(lines))


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        f"👑 ALEPH BET AI {VERSION}\n"
        "Centre d'analyse multi-sports.\n\n"
        "⚽ /foot — analyse football\n"
        "🏀 /basket — analyse basketball\n"
        "🏒 /hockey — analyse hockey sur glace\n"
        "📅 /matchs — matchs de football à venir\n"
        "📊 /bilan — résultats des sélections enregistrées\n"
        "🔧 /statut — état de DataSportsAPI\n"
        "📘 /aide — aide\n\n"
        "Maximum 2 sélections par sport et par analyse. "
        "Aucune sélection n'est forcée."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        f"👑 Bienvenue sur ALEPH BET AI {VERSION}\n\n"
        "Commandes disponibles :\n"
        "/premium — centre multi-sports\n"
        "/foot — analyse football\n"
        "/pronostics — alias football\n"
        "/matchs — matchs de football à venir\n"
        "/basket — analyse basketball\n"
        "/hockey — analyse hockey sur glace\n"
        "/bilan — bilan des sélections enregistrées\n"
        "/statut — état de l'API\n"
        "/aide — aide"
    )


async def aide_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        f"📘 AIDE ALEPH BET AI {VERSION}\n\n"
        "/foot : analyse football.\n"
        "/pronostics : alias de /foot.\n"
        "/matchs : liste les matchs de football à venir.\n"
        "/basket : analyse basketball.\n"
        "/hockey : analyse hockey sur glace.\n"
        "/bilan : met à jour et affiche les résultats enregistrés.\n"
        "/premium : centre multi-sports.\n"
        "/statut : vérifie la connexion DataSportsAPI.\n\n"
        f"Les matchs à moins de {MIN_KICKOFF_MARGIN_MINUTES} minutes du début "
        "sont exclus. Le bot examine la forme récente et le face-à-face "
        "lorsque ces données existent.\n\n"
        "L'indice interne n'est pas une probabilité. Le plan gratuit ne fournit "
        "pas les cotes. Aucun pari n'est garanti."
    )


# ------------------------- Démarrage Render / Telegram -------------------------

def main():
    if not TOKEN:
        raise RuntimeError("Variable TELEGRAM_BOT_TOKEN absente dans Render.")
    if not WEBHOOK_URL:
        raise RuntimeError("Variable WEBHOOK_URL absente dans Render.")
    if not DATASPORTS_API_KEY:
        raise RuntimeError("Variable DATASPORTS_API_KEY absente dans Render.")

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
    app.add_handler(CommandHandler("bilan", bilan_command))

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
