"""
ALEPH BET AI 5.1
Bot Telegram multi-sports : football, basketball et hockey sur glace.
API : DataSportsAPI v2.
Stockage PostgreSQL si DATABASE_URL est défini, sinon fichier JSON local.
Les indices sont heuristiques et ne sont jamais des probabilités.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

VERSION = "5.1 DATA SPORTS MULTI-SPORTS"
TZ = ZoneInfo("Africa/Abidjan")
API_BASE = "https://api.datasportsapi.com"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
API_KEY = os.getenv("DATASPORTS_API_KEY", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
HISTORY_FILE = Path(os.getenv("ALEPH_DATA_FILE", "aleph_bet_history.json"))

SPORTS = {
    "football": {"slug": "football", "label": "⚽ FOOTBALL"},
    "basketball": {"slug": "basketball", "label": "🏀 BASKETBALL"},
    "hockey": {"slug": "ice-hockey", "label": "🏒 HOCKEY SUR GLACE"},
}
CACHE_SECONDS = 900
MIN_REQUEST_INTERVAL = 6.2
DAYS_AHEAD = 2
MAX_PAGES_PER_DAY = 3
MAX_CANDIDATES = 5
MAX_PICKS_PER_SPORT = 2
MAX_RESULT_CHECKS = 5
MIN_KICKOFF_MARGIN_MINUTES = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("aleph-bet-ai")
logging.getLogger("httpx").setLevel(logging.WARNING)

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = asyncio.Lock()
_api_lock = asyncio.Lock()
_last_request = 0.0


def now_local() -> datetime:
    return datetime.now(TZ)


def local_date(offset: int = 0) -> str:
    return (now_local().date() + timedelta(days=offset)).isoformat()


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def fmt_dt(value: Any) -> str:
    dt = parse_dt(value)
    return dt.strftime("%d/%m/%Y à %H:%M") if dt else "Date indisponible"


async def api_get(path: str, params: dict | None = None) -> dict:
    global _last_request
    if not API_KEY:
        raise RuntimeError("DATASPORTS_API_KEY absente dans Render.")
    async with _api_lock:
        delay = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        _last_request = time.monotonic()

    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    timeout = httpx.Timeout(25.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(API_BASE + path, headers=headers, params=params or {})
        except httpx.TimeoutException as exc:
            raise RuntimeError("DataSportsAPI ne répond pas dans le délai prévu.") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Connexion DataSportsAPI impossible : {exc}") from exc

    if response.status_code == 401:
        raise RuntimeError("Clé DataSportsAPI refusée (401).")
    if response.status_code == 403:
        raise RuntimeError("Accès refusé (403) : vérifie le plan ou les droits API.")
    if response.status_code == 404:
        raise RuntimeError("Donnée introuvable chez DataSportsAPI (404).")
    if response.status_code == 429:
        retry = response.headers.get("Retry-After")
        raise RuntimeError("Quota ou limite API atteint (429)." + (f" Réessaie dans {retry} s." if retry else ""))
    if response.status_code >= 500:
        raise RuntimeError(f"Erreur serveur DataSportsAPI ({response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Réponse JSON DataSportsAPI illisible.") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"DataSportsAPI HTTP {response.status_code}: {str(payload)[:180]}")
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError(f"Erreur DataSportsAPI : {str(payload['errors'])[:180]}")
    if not isinstance(payload, dict):
        raise RuntimeError("Format de réponse DataSportsAPI inattendu.")
    return payload


async def cached_get(path: str, params: dict | None = None) -> dict:
    params = params or {}
    key = (path, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    async with _cache_lock:
        saved = _cache.get(key)
        if saved and time.monotonic() - saved[0] < CACHE_SECONDS:
            return saved[1]
    data = await api_get(path, params)
    async with _cache_lock:
        _cache[key] = (time.monotonic(), data)
    return data


def data_of(payload: dict, default=None):
    return payload.get("data", default)


def status_of(fixture: dict) -> str:
    status = fixture.get("status")
    return str(status.get("type") or "").lower() if isinstance(status, dict) else ""


def side_of(fixture: dict, side: str) -> dict:
    value = fixture.get(side)
    return value if isinstance(value, dict) else {}


def team_name(side: dict) -> str:
    return str(side.get("name") or "Équipe inconnue")


def fixture_names(fixture: dict) -> tuple[str, str]:
    return team_name(side_of(fixture, "home")), team_name(side_of(fixture, "away"))


def league_name(fixture: dict) -> str:
    league = fixture.get("league")
    return str(league.get("name") or "Compétition non précisée") if isinstance(league, dict) else "Compétition non précisée"


def eligible(fixture: dict) -> bool:
    if not isinstance(fixture, dict) or status_of(fixture) != "notstarted":
        return False
    kickoff = parse_dt(fixture.get("startTime"))
    if not kickoff or kickoff <= now_local() + timedelta(minutes=MIN_KICKOFF_MARGIN_MINUTES):
        return False
    home, away = fixture_names(fixture)
    return bool(fixture.get("id")) and home != "Équipe inconnue" and away != "Équipe inconnue"


async def upcoming(sport: str) -> tuple[list[dict], bool]:
    found: dict[str, dict] = {}
    partial = False
    slug = SPORTS[sport]["slug"]
    for day_offset in range(DAYS_AHEAD):
        for page in range(MAX_PAGES_PER_DAY):
            payload = await cached_get("/v2/fixtures", {
                "date": local_date(day_offset), "sport": slug, "page": page
            })
            rows = data_of(payload, [])
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("id") is not None:
                        found[str(row["id"])] = row
            meta = payload.get("meta") or {}
            if isinstance(meta, dict) and meta.get("complete") is False:
                partial = True
            if not isinstance(meta, dict) or not meta.get("hasNextPage"):
                break
            if page == MAX_PAGES_PER_DAY - 1:
                partial = True
    matches = [x for x in found.values() if eligible(x)]
    matches.sort(key=lambda x: parse_dt(x.get("startTime")) or now_local())
    return matches, partial


def normalize_result(value: Any) -> str | None:
    value = str(value or "").strip().lower()
    if value in {"win", "won", "w", "victory"}: return "win"
    if value in {"draw", "tie", "d", "drawn"}: return "draw"
    if value in {"loss", "lost", "l", "defeat"}: return "loss"
    return None


def extract_results(rows: Any, fallback: Any) -> list[str]:
    results = []
    if isinstance(rows, list):
        results = [r for row in rows if isinstance(row, dict)
                   if (r := normalize_result(row.get("result")))]
    if len(results) >= 3:
        return results[-5:]
    backup = [r for value in fallback if (r := normalize_result(value))] if isinstance(fallback, list) else []
    return (backup or results)[-5:]


def form_rate(results: list[str]) -> float | None:
    if not results:
        return None
    return sum({"win": 1.0, "draw": 0.5, "loss": 0.0}[x] for x in results) / len(results)


def number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool): return None
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


async def analyze(sport: str, fixture: dict) -> dict | None:
    fid = fixture["id"]
    try:
        form_payload = await cached_get(f"/v2/fixtures/{fid}/form")
        team_payload = await cached_get(f"/v2/fixtures/{fid}/team-form")
    except Exception as exc:
        log.info("Forme indisponible pour %s : %s", fid, exc)
        return None

    form = data_of(form_payload, {})
    team = data_of(team_payload, {})
    form = form if isinstance(form, dict) else {}
    team = team if isinstance(team, dict) else {}
    hf = form.get("home") if isinstance(form.get("home"), dict) else {}
    af = form.get("away") if isinstance(form.get("away"), dict) else {}
    hr = extract_results(team.get("home"), hf.get("results", []))
    ar = extract_results(team.get("away"), af.get("results", []))
    if len(hr) < 3 or len(ar) < 3:
        return None

    hrate, arate = form_rate(hr), form_rate(ar)
    if hrate is None or arate is None:
        return None

    h2h = {}
    try:
        raw = data_of(await cached_get(f"/v2/fixtures/{fid}/h2h"), {})
        if isinstance(raw, dict): h2h = raw
    except Exception:
        pass
    hw, draws, aw = (number(h2h.get(k)) or 0.0 for k in ("homeWins", "draws", "awayWins"))
    total = hw + draws + aw
    h2h_edge = (hw - aw) / total if total else 0.0

    hp, ap = number(hf.get("position")), number(af.get("position"))
    position_edge = 0.0
    if hp and ap and hp > 0 and ap > 0:
        position_edge = clamp((ap - hp) / max(hp, ap, 10.0), -1.0, 1.0)
    hrating, arating = number(hf.get("avgRating")), number(af.get("avgRating"))
    rating_edge = clamp((hrating - arating) / 2.0, -1.0, 1.0) if hrating is not None and arating is not None else 0.0

    score = round(clamp(50 + 30 * (hrate - arate) + 10 * h2h_edge + 6 * position_edge + 4 * rating_edge + 4, 0, 100))
    home, away = fixture_names(fixture)
    market = selection = side = None
    quality = abs(score - 50)

    if sport == "football":
        if score >= 70: market, selection, side = "Victoire domicile (1)", home, "home"
        elif score <= 30: market, selection, side = "Victoire extérieur (2)", away, "away"
        elif score >= 62: market, selection, side = "Double chance 1X", f"{home} ou nul", "home_or_draw"
        elif score <= 38: market, selection, side = "Double chance X2", f"{away} ou nul", "away_or_draw"
    else:
        if score >= 68: market, selection, side = "Victoire domicile", home, "home"
        elif score <= 32: market, selection, side = "Victoire extérieur", away, "away"

    if not market:
        return None
    symbols = {"win": "V", "draw": "N", "loss": "D"}
    reason = (f"Forme récente : {home} {'-'.join(symbols[x] for x in hr)} / "
              f"{away} {'-'.join(symbols[x] for x in ar)}. "
              f"Face-à-face : {int(hw)} victoire(s) domicile, {int(draws)} nul(s), {int(aw)} victoire(s) extérieur."
              if total else f"Forme récente : {home} {'-'.join(symbols[x] for x in hr)} / {away} {'-'.join(symbols[x] for x in ar)}. Face-à-face indisponible.")
    return {
        "sport": sport, "fixture_id": str(fid), "home": home, "away": away,
        "league": league_name(fixture), "start_time": fixture.get("startTime"),
        "market": market, "selection": selection, "selection_side": side,
        "internal_score": score, "quality_rank": quality, "reason": reason,
        "outcome": "pending", "recorded_at": now_local().isoformat()
    }


def load_history() -> list[dict]:
    if DATABASE_URL:
        try:
            import psycopg
            with psycopg.connect(DATABASE_URL) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS aleph_picks (
                    id BIGSERIAL PRIMARY KEY, fixture_id TEXT NOT NULL, sport TEXT NOT NULL,
                    selection_side TEXT NOT NULL, payload JSONB NOT NULL,
                    UNIQUE(fixture_id, sport, selection_side))""")
                rows = conn.execute("SELECT payload FROM aleph_picks ORDER BY id").fetchall()
                return [row[0] for row in rows]
        except Exception as exc:
            log.error("PostgreSQL indisponible : %s", exc)
            return []
    try:
        if HISTORY_FILE.exists():
            content = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return content if isinstance(content, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Historique JSON illisible : %s", exc)
    return []


def save_pick(pick: dict) -> None:
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS aleph_picks (
                id BIGSERIAL PRIMARY KEY, fixture_id TEXT NOT NULL, sport TEXT NOT NULL,
                selection_side TEXT NOT NULL, payload JSONB NOT NULL,
                UNIQUE(fixture_id, sport, selection_side))""")
            conn.execute("""INSERT INTO aleph_picks (fixture_id,sport,selection_side,payload)
                VALUES (%s,%s,%s,%s) ON CONFLICT (fixture_id,sport,selection_side) DO NOTHING""",
                (pick["fixture_id"], pick["sport"], pick["selection_side"], json.dumps(pick)))
        return
    history = load_history()
    key = (str(pick["fixture_id"]), pick["sport"], pick["selection_side"])
    if not any((str(x.get("fixture_id")), x.get("sport"), x.get("selection_side")) == key for x in history):
        history.append(pick)
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = HISTORY_FILE.with_suffix(HISTORY_FILE.suffix + ".tmp")
        temp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, HISTORY_FILE)


def update_pick(pick: dict) -> None:
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("""UPDATE aleph_picks SET payload=%s
                WHERE fixture_id=%s AND sport=%s AND selection_side=%s""",
                (json.dumps(pick), pick["fixture_id"], pick["sport"], pick["selection_side"]))
        return
    history = load_history()
    for i, old in enumerate(history):
        if (str(old.get("fixture_id")), old.get("sport"), old.get("selection_side")) == (str(pick.get("fixture_id")), pick.get("sport"), pick.get("selection_side")):
            history[i] = pick
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def winner(fixture: dict) -> str | None:
    value = fixture.get("winner")
    if value in {"home", "away", "draw"}: return value
    hs, aws = fixture.get("homeScore"), fixture.get("awayScore")
    hs = hs.get("current") if isinstance(hs, dict) else hs
    aws = aws.get("current") if isinstance(aws, dict) else aws
    hs, aws = number(hs), number(aws)
    if hs is None or aws is None: return None
    return "home" if hs > aws else "away" if aws > hs else "draw"


def settle(pick: dict, fixture: dict) -> str | None:
    result = winner(fixture)
    if result is None: return None
    side = pick.get("selection_side")
    if side == "home": return "won" if result == "home" else "lost"
    if side == "away": return "won" if result == "away" else "lost"
    if side == "home_or_draw": return "won" if result in {"home", "draw"} else "lost"
    if side == "away_or_draw": return "won" if result in {"away", "draw"} else "lost"
    return None


async def refresh_results(history: list[dict]) -> None:
    count = 0
    for pick in history:
        if count >= MAX_RESULT_CHECKS: break
        if pick.get("outcome") != "pending": continue
        kickoff = parse_dt(pick.get("start_time"))
        if not kickoff or kickoff + timedelta(hours=3) > now_local(): continue
        try:
            payload = await cached_get(f"/v2/fixtures/{pick['fixture_id']}")
            fixture = data_of(payload, {})
            if not isinstance(fixture, dict): continue
            state = status_of(fixture)
            if state in {"canceled", "cancelled"}:
                pick["outcome"] = "void"
            elif state == "finished":
                pick["outcome"] = "void" if fixture.get("awarded") is True else (settle(pick, fixture) or "pending")
            else:
                continue
            if pick["outcome"] != "pending":
                pick["settled_at"] = now_local().isoformat()
                await asyncio.to_thread(update_pick, pick)
                count += 1
        except Exception as exc:
            log.info("Résultat non actualisé pour %s : %s", pick.get("fixture_id"), exc)


async def reply(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        payload = await cached_get("/v2/sports")
        sports = data_of(payload, [])
        count = len(sports) if isinstance(sports, list) else "inconnu"
        await reply(update, f"👑 ALEPH BET AI {VERSION}\n🟢 DataSportsAPI accessible\n📚 Sports renvoyés : {count}\n🔐 Clé conservée côté serveur.")
    except Exception as exc:
        await reply(update, f"👑 ALEPH BET AI {VERSION}\n🔴 {str(exc)[:300]}")


async def analyze_cmd(update: Update, sport: str) -> None:
    try:
        fixtures, partial = await upcoming(sport)
    except Exception as exc:
        await reply(update, f"⚠️ Impossible de récupérer les matchs :\n{str(exc)[:300]}")
        return
    candidates = fixtures[:MAX_CANDIDATES]
    picks = []
    for fixture in candidates:
        pick = await analyze(sport, fixture)
        if pick: picks.append(pick)
    picks.sort(key=lambda x: x["quality_rank"], reverse=True)
    selected, leagues = [], set()
    for pick in picks:
        league = pick["league"].lower()
        if league in leagues: continue
        selected.append(pick)
        leagues.add(league)
        if len(selected) >= MAX_PICKS_PER_SPORT: break
    for pick in selected:
        try: await asyncio.to_thread(save_pick, pick)
        except Exception as exc: log.error("Sauvegarde impossible : %s", exc)

    lines = [f"👑 ALEPH BET AI {VERSION}", SPORTS[sport]["label"],
             f"🕒 {now_local():%d/%m/%Y %H:%M} (Côte d'Ivoire)",
             f"🔎 Matchs examinés : {len(candidates)}", ""]
    if not selected:
        lines += ["📭 Aucune sélection ne franchit les filtres.",
                  "Le bot préfère ne rien proposer plutôt que forcer un pronostic."]
    else:
        for i, p in enumerate(selected, 1):
            lines += [f"{i}. {p['home']} — {p['away']}", f"🏆 {p['league']}",
                      f"🗓 {fmt_dt(p['start_time'])}", f"🎯 {p['market']}",
                      f"📌 Sélection : {p['selection']}",
                      f"🧭 Indice interne : {p['internal_score']}/100",
                      f"📝 {p['reason']}", ""]
    if partial: lines.append("ℹ️ Couverture partielle : certaines pages n'ont pas été parcourues.")
    lines += ["", "⚠️ L'indice interne n'est pas une probabilité. Aucun pari n'est garanti."]
    await reply(update, "\n".join(lines))


async def foot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await analyze_cmd(update, "football")


async def basket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await analyze_cmd(update, "basketball")


async def hockey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await analyze_cmd(update, "hockey")


async def matchs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        fixtures, partial = await upcoming("football")
    except Exception as exc:
        await reply(update, f"⚠️ Impossible de récupérer les matchs : {str(exc)[:300]}")
        return
    if not fixtures:
        await reply(update, "📭 Aucun match de football admissible trouvé.")
        return
    lines = [f"👑 ALEPH BET AI {VERSION} — ⚽ FOOTBALL", f"📅 {now_local():%d/%m/%Y %H:%M}", ""]
    for i, f in enumerate(fixtures[:15], 1):
        home, away = fixture_names(f)
        lines.append(f"{i}. {home} — {away}\n🏆 {league_name(f)}\n🗓 {fmt_dt(f.get('startTime'))}")
    if partial: lines.append("ℹ️ Liste partielle : l'API signale d'autres pages.")
    await reply(update, "\n\n".join(lines))


async def bilan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not API_KEY:
        await reply(update, "⚠️ DATASPORTS_API_KEY doit être configurée dans Render.")
        return
    history = await asyncio.to_thread(load_history)
    if not history:
        await reply(update, "📊 BILAN ALEPH BET AI\nAucune sélection enregistrée.")
        return
    await refresh_results(history)
    history = await asyncio.to_thread(load_history)
    lines = [f"📊 BILAN ALEPH BET AI {VERSION}", f"🕒 Mis à jour : {now_local():%d/%m/%Y %H:%M}", ""]
    labels = {"football": "⚽ Football", "basketball": "🏀 Basketball", "hockey": "🏒 Hockey"}
    for sport, label in labels.items():
        rows = [x for x in history if x.get("sport") == sport]
        if not rows: continue
        wins = sum(x.get("outcome") == "won" for x in rows)
        losses = sum(x.get("outcome") == "lost" for x in rows)
        voids = sum(x.get("outcome") == "void" for x in rows)
        pending = sum(x.get("outcome") == "pending" for x in rows)
        decided = wins + losses
        rate = f"{wins / decided * 100:.1f}%" if decided else "non calculable"
        lines += [label, f"✅ Gagnés : {wins} | ❌ Perdus : {losses}",
                  f"📈 Taux observé : {rate}", f"⏳ En attente : {pending} | Annulés : {voids}", ""]
    lines.append("Le taux observé concerne uniquement les sélections réglées et enregistrées.")
    await reply(update, "\n".join(lines))


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, f"👑 ALEPH BET AI {VERSION}\nCentre multi-sports\n\n⚽ /foot\n🏀 /basket\n🏒 /hockey\n📅 /matchs\n📊 /bilan\n🔧 /statut\n📘 /aide")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, f"👑 Bienvenue sur ALEPH BET AI {VERSION}\n\nCommandes :\n/start — Démarrer\n/aide — Aide\n/premium — Centre multi-sports\n/statut — État API\n/matchs — Matchs football\n/foot et /pronostics — Analyse football\n/basket — Analyse basketball\n/hockey — Analyse hockey\n/bilan — Bilan")


async def aide_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, f"📘 ALEPH BET AI {VERSION}\n/foot ou /pronostics : analyse football\n/matchs : matchs à venir\n/basket : analyse basketball\n/hockey : analyse hockey\n/bilan : résultats enregistrés\n/premium : centre multi-sports\n/statut : vérifie DataSportsAPI\n\nLes matchs à moins de {MIN_KICKOFF_MARGIN_MINUTES} minutes du début sont exclus. L'indice interne n'est pas une probabilité. Aucun pari n'est garanti.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Variable TELEGRAM_BOT_TOKEN (ou BOT_TOKEN) absente dans Render.")
    if not API_KEY:
        raise RuntimeError("Variable DATASPORTS_API_KEY absente dans Render.")
    if not WEBHOOK_URL:
        raise RuntimeError("Variable WEBHOOK_URL absente dans Render.")

    app = Application.builder().token(TOKEN).build()
    for command, callback in [
        ("start", start_cmd), ("aide", aide_cmd), ("statut", status_cmd),
        ("premium", premium_cmd), ("foot", foot_cmd), ("pronostics", foot_cmd),
        ("matchs", matchs_cmd), ("basket", basket_cmd), ("hockey", hockey_cmd),
        ("bilan", bilan_cmd),
    ]:
        app.add_handler(CommandHandler(command, callback))

    kwargs = {
        "listen": "0.0.0.0", "port": PORT, "url_path": "telegram",
        "webhook_url": WEBHOOK_URL + "/telegram",
        "allowed_updates": Update.ALL_TYPES,
    }
    if WEBHOOK_SECRET:
        kwargs["secret_token"] = WEBHOOK_SECRET
    log.info("Démarrage %s", VERSION)
    app.run_webhook(**kwargs)


if __name__ == "__main__":
    main()
