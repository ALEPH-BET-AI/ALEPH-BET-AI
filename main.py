"""
ALEPH BET AI 5.1
Multi-sports Telegram bot: football, basketball, ice hockey.
DataSportsAPI v2. PostgreSQL persistence when DATABASE_URL is configured;
otherwise JSON fallback (not durable on ephemeral hosting).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

APP_NAME = "ALEPH BET AI 5.1"
API_BASE = os.getenv("DATASPORTS_API_BASE", "https://api.datasportsapi.com").rstrip("/")
API_KEY = os.getenv("DATASPORTS_API_KEY", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DATA_FILE = Path(os.getenv("ALEPH_DATA_FILE", "aleph_data.json"))
REQUEST_GAP = float(os.getenv("API_REQUEST_GAP", "6.3"))
MAX_PAGES = int(os.getenv("MAX_FIXTURE_PAGES", "3"))
MAX_FIXTURES = int(os.getenv("MAX_FIXTURES_PER_ANALYSIS", "8"))
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "600"))
LOCAL_TZ = timezone(timedelta(hours=0))  # CÃ´te d'Ivoire (UTC)

SPORTS = {
    "football": {"label": "FOOTBALL", "emoji": "â½", "slug": "football"},
    "basket": {"label": "BASKETBALL", "emoji": "ð", "slug": "basketball"},
    "hockey": {"label": "HOCKEY SUR GLACE", "emoji": "ð", "slug": "ice-hockey"},
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("aleph-bet-ai")

class APIError(Exception):
    pass

class DataSportsClient:
    """Small, paced client. One shared gap protects the API quota."""
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(18.0, connect=8.0),
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Accept": "application/json",
                "User-Agent": "ALEPH-BET-AI/5.1",
            },
        )
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, params: dict | None = None, cache: bool = True):
        key = path + "?" + urlencode(sorted((params or {}).items()))
        now = time.monotonic()
        if cache and key in self._cache:
            stamp, value = self._cache[key]
            if now - stamp < CACHE_SECONDS:
                return value
        async with self._lock:
            wait = REQUEST_GAP - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                response = await self.client.get(path, params=params)
            except httpx.HTTPError as exc:
                raise APIError(f"Erreur rÃ©seau DataSportsAPI : {exc}") from exc
            finally:
                self._last_call = time.monotonic()
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("errors") or body.get("error") or body)[:300]
            except Exception:
                detail = response.text[:200]
            raise APIError(f"DataSportsAPI HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise APIError("RÃ©ponse JSON invalide de DataSportsAPI.") from exc
        if not isinstance(payload, dict):
            raise APIError("Format inattendu renvoyÃ© par DataSportsAPI.")
        if cache:
            self._cache[key] = (time.monotonic(), payload)
        return payload

    async def fixtures_for_date(self, sport: str, date_utc: str):
        all_fixtures: list[dict] = []
        pages_seen = 0
        complete = True
        for page in range(max(1, min(MAX_PAGES, 100))):
            payload = await self.get(
                "/v2/fixtures",
                {"date": date_utc, "sport": sport, "page": page},
            )
            data = payload.get("data") or []
            if isinstance(data, list):
                all_fixtures.extend(x for x in data if isinstance(x, dict))
            meta = payload.get("meta") or {}
            pages_seen += 1
            if meta.get("complete") is False:
                complete = False
            if not meta.get("hasNextPage", False):
                break
        else:
            complete = False
        return all_fixtures, complete, pages_seen

    async def upcoming(self, sport: str):
        now = datetime.now(timezone.utc)
        fixtures: list[dict] = []
        coverage_complete = True
        pages = 0
        # API date parameter is UTC. Check today and tomorrow to cover timezone edges.
        for offset in (0, 1):
            day = (now + timedelta(days=offset)).date().isoformat()
            rows, complete, used_pages = await self.fixtures_for_date(sport, day)
            fixtures.extend(rows)
            coverage_complete = coverage_complete and complete
            pages += used_pages
        unique = {}
        for f in fixtures:
            fid = f.get("id")
            start = parse_dt(f.get("startTime"))
            status = (f.get("status") or {}).get("type")
            if fid is None or not start or start <= now + timedelta(minutes=25):
                continue
            if status not in ("notstarted", "scheduled", ""):
                continue
            unique[str(fid)] = f
        ordered = sorted(unique.values(), key=lambda x: parse_dt(x.get("startTime")) or now)
        return ordered, coverage_complete, pages

    async def team_form(self, fixture_id: int):
        return await self.get(f"/v2/fixtures/{fixture_id}/team-form")

def parse_dt(value):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None

def team_name(fixture, side):
    team = fixture.get(side) or {}
    return team.get("name") or ("Ãquipe domicile" if side == "home" else "Ãquipe extÃ©rieure")

def score_value(fixture, key):
    obj = fixture.get(key)
    if isinstance(obj, dict):
        value = obj.get("current")
        return value if isinstance(value, (int, float)) else None
    return obj if isinstance(obj, (int, float)) else None

def result_counts(matches):
    counts = {"win": 0, "draw": 0, "loss": 0}
    for match in matches:
        result = str(match.get("result") or "").lower()
        if result in counts:
            counts[result] += 1
    return counts

def form_summary(matches):
    if not matches:
        return "forme indisponible", 0, None
    marks = []
    for m in matches:
        result = str(m.get("result") or "").lower()
        marks.append({"win": "V", "draw": "N", "loss": "D"}.get(result, "?"))
    goals_for = [m.get("scoreFor") for m in matches if isinstance(m.get("scoreFor"), (int, float))]
    goals_against = [m.get("scoreAgainst") for m in matches if isinstance(m.get("scoreAgainst"), (int, float))]
    avg_total = None
    paired = [
        m["scoreFor"] + m["scoreAgainst"]
        for m in matches
        if isinstance(m.get("scoreFor"), (int, float)) and isinstance(m.get("scoreAgainst"), (int, float))
    ]
    if paired:
        avg_total = sum(paired) / len(paired)
    return "-".join(marks[-5:]), len(matches), avg_total

def make_analysis(fixture, form_payload, sport):
    data = form_payload.get("data") or {}
    home_rows = data.get("home") or []
    away_rows = data.get("away") or []
    if not isinstance(home_rows, list) or not isinstance(away_rows, list):
        return None
    home_rows = [m for m in home_rows if isinstance(m, dict)]
    away_rows = [m for m in away_rows if isinstance(m, dict)]
    # Avoid creating a selection from tiny or missing samples.
    if len(home_rows) < 3 or len(away_rows) < 3:
        return None

    hform, hn, havg = form_summary(home_rows)
    aform, an, aavg = form_summary(away_rows)
    hcounts, acounts = result_counts(home_rows), result_counts(away_rows)
    hpoints = hcounts["win"] * 3 + hcounts["draw"]
    apoints = acounts["win"] * 3 + acounts["draw"]
    hrate = hpoints / max(1, hn * 3)
    arate = apoints / max(1, an * 3)
    gap = hrate - arate
    # Score is a transparent internal consistency index, not a probability.
    quality = round(min(85, 42 + abs(gap) * 45 + min(hn, an) * 1.5))
    home = team_name(fixture, "home")
    away = team_name(fixture, "away")
    league = (fixture.get("league") or {}).get("name") or "CompÃ©tition non prÃ©cisÃ©e"
    start = parse_dt(fixture.get("startTime"))
    kickoff = start.astimezone(LOCAL_TZ).strftime("%d/%m/%Y Ã  %H:%M") if start else "Horaire indisponible"

    selection = None
    market = None
    explanation = (
        f"{home} {hform} ({hn} matchs) / {away} {aform} ({an} matchs). "
        f"Ãchantillons toutes compÃ©titions confondues."
    )
    if sport == "football":
        totals = [x for x in (havg, aavg) if x is not None]
        if len(totals) == 2:
            combined = (totals[0] + totals[1]) / 2
            if combined >= 2.8:
                market, selection = "Total de buts", "Plus de 1,5 but"
            elif combined <= 2.0:
                market, selection = "Total de buts", "Moins de 3,5 buts"
            else:
                market, selection = "Double chance", "Ã Ã©valuer â aucune sÃ©lection automatique"
        explanation += (
            f" Moyenne de buts cumulÃ©s sur les derniers matchs : "
            f"{(sum(totals) / len(totals)):.2f}."
        )
    else:
        # Generic winner selection deliberately requires a clear recent-form gap.
        if abs(gap) >= 0.30 and quality >= 60:
            market = "Vainqueur du match (temps rÃ©glementaire selon sport)"
            selection = f"{home} (tendance forme)" if gap > 0 else f"{away} (tendance forme)"
        explanation += (
            f" Indice de rÃ©sultats rÃ©cents : domicile {hpoints}/{hn*3}, "
            f"extÃ©rieur {apoints}/{an*3}."
        )
    if not selection or selection.startswith("Ã Ã©valuer"):
        return None
    return {
        "sport": sport, "fixture_id": fixture.get("id"),
        "home": home, "away": away, "league": league,
        "kickoff": kickoff, "market": market, "selection": selection,
        "score": quality, "explanation": explanation,
    }

class PickStore:
    """PostgreSQL if configured; otherwise JSON file fallback."""
    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.path = DATA_FILE
        self._json_lock = asyncio.Lock()
        if self.database_url and self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)
        if self.database_url and psycopg is None:
            log.warning("DATABASE_URL dÃ©fini mais psycopg indisponible; bascule JSON.")
            self.database_url = ""

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    async def init(self):
        if self.database_url:
            await asyncio.to_thread(self._init_pg)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.write_text(json.dumps({"picks": []}, ensure_ascii=False), encoding="utf-8")

    def _init_pg(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aleph_picks (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    chat_id BIGINT NOT NULL,
                    sport TEXT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    home TEXT NOT NULL,
                    away TEXT NOT NULL,
                    league TEXT,
                    kickoff TEXT,
                    market TEXT,
                    selection TEXT,
                    score INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(chat_id, fixture_id, market, selection)
                )
            """)

    async def add(self, chat_id, pick):
        if self.database_url:
            await asyncio.to_thread(self._add_pg, chat_id, pick)
            return
        async with self._json_lock:
            data = self._read_json()
            key = (str(chat_id), str(pick["fixture_id"]), pick["market"], pick["selection"])
            if not any((str(x["chat_id"]), str(x["fixture_id"]), x["market"], x["selection"]) == key for x in data["picks"]):
                item = dict(pick)
                item.update({"chat_id": chat_id, "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending"})
                data["picks"].append(item)
                self._write_json(data)

    def _add_pg(self, chat_id, p):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO aleph_picks
                (chat_id,sport,fixture_id,home,away,league,kickoff,market,selection,score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (chat_id,fixture_id,market,selection) DO NOTHING
            """, (chat_id,p["sport"],str(p["fixture_id"]),p["home"],p["away"],p["league"],p["kickoff"],p["market"],p["selection"],p["score"]))

    async def list_for_chat(self, chat_id):
        if self.database_url:
            return await asyncio.to_thread(self._list_pg, chat_id)
        async with self._json_lock:
            return [x for x in self._read_json()["picks"] if int(x.get("chat_id", 0)) == int(chat_id)]

    def _list_pg(self, chat_id):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM aleph_picks WHERE chat_id=%s ORDER BY id DESC", (chat_id,)).fetchall()
            return [dict(x) for x in rows]

    def _read_json(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"picks": []}

    def _write_json(self, data):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

def get_services(context):
    return context.application.bot_data["services"]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ð Bienvenue sur {APP_NAME} â DATA SPORTS MULTI-SPORTS\n\n"
        "Commandes disponibles :\n"
        "/premium â centre multi-sports\n"
        "/foot â analyse football\n"
        "/pronostics â alias football\n"
        "/matchs â matchs de football Ã  venir\n"
        "/basket â analyse basketball\n"
        "/hockey â analyse hockey sur glace\n"
        "/bilan â historique des sÃ©lections\n"
        "/statut â Ã©tat de l'API et du stockage\n"
        "/aide â guide d'utilisation\n\n"
        "â ï¸ Les indices internes ne sont pas des probabilitÃ©s. Aucun pari n'est sÃ»r."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ð ALEPH BET AI 5.1\n\n"
        "â¢ /foot ou /pronostics : analyse football\n"
        "â¢ /basket : analyse basketball\n"
        "â¢ /hockey : analyse hockey sur glace\n"
        "â¢ /matchs : liste des matchs de football\n"
        "â¢ /bilan : sÃ©lections enregistrÃ©es et rÃ©sultats observÃ©s\n"
        "â¢ /statut : connexion API et stockage\n\n"
        "Les analyses s'appuient sur les cinq derniers matchs disponibles de chaque Ã©quipe. "
        "Si l'Ã©chantillon est insuffisant, aucune sÃ©lection n'est forcÃ©e.\n"
        "L'indice de cohÃ©rence est un score interne, jamais une probabilitÃ© de gain."
    )

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ð ALEPH BET AI 5.1 â CENTRE MULTI-SPORTS\n\n"
        "â½ /foot â Football\nð /basket â Basketball\nð /hockey â Hockey sur glace\n"
        "ð /matchs â Calendrier football\nð /bilan â Historique\n"
        "ð /statut â API et stockage\n\n"
        "Architecture extensible : les sports futurs seront ajoutÃ©s aprÃ¨s validation des donnÃ©es."
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services = get_services(context)
    try:
        payload = await services["api"].get("/v2/sports", cache=False)
        rows = payload.get("data") or []
        sport_count = len(rows) if isinstance(rows, list) else 0
        storage = "PostgreSQL" if services["store"].database_url else "JSON local (non durable sur Render)"
        await update.message.reply_text(
            f"ð {APP_NAME}\n"
            "ð¢ Connexion DataSportsAPI : accessible\n"
            f"ð Sports renvoyÃ©s par l'API : {sport_count}\n"
            f"ðï¸ Stockage : {storage}\n"
            "ð ClÃ© API conservÃ©e cÃ´tÃ© serveur."
        )
    except APIError as exc:
        await update.message.reply_text(f"ð´ DataSportsAPI inaccessible.\n{exc}")

async def list_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services = get_services(context)
    await update.message.reply_text("ð Recherche des matchs de football Ã  venirâ¦")
    try:
        fixtures, complete, pages = await services["api"].upcoming("football")
    except APIError as exc:
        await update.message.reply_text(f"â ï¸ Impossible de rÃ©cupÃ©rer les matchs.\n{exc}")
        return
    if not fixtures:
        await update.message.reply_text("ð­ Aucun match futur trouvÃ© dans les dates vÃ©rifiÃ©es.")
        return
    lines = [f"ð {APP_NAME} â â½ MATCHS Ã VENIR", ""]
    for i, f in enumerate(fixtures[:20], 1):
        dt = parse_dt(f.get("startTime"))
        when = dt.astimezone(LOCAL_TZ).strftime("%d/%m Ã  %H:%M") if dt else "Horaire inconnu"
        league = (f.get("league") or {}).get("name") or "CompÃ©tition inconnue"
        lines.append(f"{i}. {team_name(f,'home')} â {team_name(f,'away')}\nð {league}\nð {when}")
    if not complete:
        lines.append("\nâ¹ï¸ Certaines listes de ligues sont temporairement incomplÃ¨tes.")
    lines.append("\nâ¹ï¸ Liste de matchs uniquement, sans recommandation de mise.")
    await send_chunks(update, "\n\n".join(lines))

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE, sport_key: str):
    services = get_services(context)
    spec = SPORTS[sport_key]
    await update.message.reply_text(f"ð Analyse {spec['label'].lower()} en coursâ¦")
    try:
        fixtures, complete, pages = await services["api"].upcoming(spec["slug"])
    except APIError as exc:
        await update.message.reply_text(f"â ï¸ RÃ©cupÃ©ration impossible.\n{exc}")
        return
    if not fixtures:
        await update.message.reply_text("ð­ Aucun match futur trouvÃ© dans les dates vÃ©rifiÃ©es.")
        return

    candidates = fixtures[:MAX_FIXTURES]
    picks = []
    examined = 0
    errors = 0
    for fixture in candidates:
        examined += 1
        try:
            payload = await services["api"].team_form(int(fixture["id"]))
            pick = make_analysis(fixture, payload, spec["slug"])
            if pick:
                picks.append(pick)
                await services["store"].add(update.effective_chat.id, pick)
        except (APIError, ValueError, TypeError, KeyError) as exc:
            errors += 1
            log.warning("Analyse fixture %s: %s", fixture.get("id"), exc)

    now_local = datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")
    lines = [
        f"ð {APP_NAME}",
        f"{spec['emoji']} {spec['label']}",
        f"ð VÃ©rification : {now_local} (CÃ´te d'Ivoire)",
        f"ð Matchs examinÃ©s : {examined}",
        "",
    ]
    if not picks:
        lines.append("ð­ Aucune sÃ©lection ne franchit les filtres aujourd'hui.")
        lines.append("Le bot prÃ©fÃ¨re ne rien proposer plutÃ´t que forcer un pronostic.")
    else:
        for i, p in enumerate(picks[:5], 1):
            lines.extend([
                f"{i}. {p['home']} â {p['away']}",
                f"ð {p['league']}",
                f"ð {p['kickoff']}",
                f"ð¯ MarchÃ© : {p['market']}",
                f"ð SÃ©lection : {p['selection']}",
                f"ð§­ Indice de cohÃ©rence interne : {p['score']}/100",
                f"ð {p['explanation']}",
                "",
            ])
    if not complete:
        lines.append("â¹ï¸ Couverture partielle : certaines pages ou listes de ligues n'ont pas Ã©tÃ© rÃ©cupÃ©rÃ©es.")
    if errors:
        lines.append(f"â ï¸ DonnÃ©es indisponibles pour {errors} match(s) ; ces matchs ont Ã©tÃ© ignorÃ©s.")
    lines.extend([
        "",
        "â ï¸ L'indice interne n'est ni une probabilitÃ© ni un taux de rÃ©ussite.",
        "Aucun pari n'est sÃ»r. VÃ©rifie les informations et reste prudent.",
    ])
    await send_chunks(update, "\n".join(lines))

async def send_chunks(update, text):
    # Telegram message limit is 4096 chars; keep chunks on paragraph boundaries.
    while len(text) > 3900:
        cut = text.rfind("\n\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        await update.message.reply_text(text[:cut])
        text = text[cut:].lstrip()
    if text:
        await update.message.reply_text(text)

async def bilan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services = get_services(context)
    rows = await services["store"].list_for_chat(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("ð Aucun pronostic enregistrÃ© pour le moment.")
        return
    summary = {}
    for key in SPORTS:
        subset = [x for x in rows if x.get("sport") == SPORTS[key]["slug"]]
        wins = sum(1 for x in subset if x.get("status") == "won")
        losses = sum(1 for x in subset if x.get("status") == "lost")
        pending = sum(1 for x in subset if x.get("status", "pending") == "pending")
        cancelled = sum(1 for x in subset if x.get("status") == "void")
        settled = wins + losses
        summary[key] = (wins, losses, pending, cancelled, (wins / settled * 100) if settled else None)
    lines = [f"ð BILAN {APP_NAME}", f"ð Mis Ã  jour : {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')}", ""]
    for key, values in summary.items():
        wins, losses, pending, cancelled, rate = values
        if wins + losses + pending + cancelled:
            rate_text = f"{rate:.1f}%" if rate is not None else "pas encore calculable"
            lines.extend([
                f"{SPORTS[key]['emoji']} {SPORTS[key]['label']}",
                f"â GagnÃ©s : {wins} | â Perdus : {losses}",
                f"ð Taux observÃ© : {rate_text}",
                f"â³ En attente : {pending} | AnnulÃ©s : {cancelled}",
                "",
            ])
    lines.append("Le taux observÃ© ne concerne que les sÃ©lections rÃ©glÃ©es et enregistrÃ©es. Les rÃ©sultats ne sont pas encore automatiquement certifiÃ©s par le bot.")
    await update.message.reply_text("\n".join(lines))

async def post_init(application: Application):
    api = DataSportsClient()
    store = PickStore()
    await store.init()
    application.bot_data["services"] = {"api": api, "store": store}
    log.info("Services ALEPH BET AI 5.1 initialisÃ©s.")

async def post_shutdown(application: Application):
    services = application.bot_data.get("services")
    if services:
        await services["api"].close()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Variable TELEGRAM_BOT_TOKEN manquante.")
    if not API_KEY:
        raise RuntimeError("Variable DATASPORTS_API_KEY manquante.")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("aide", help_cmd))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("statut", status))
    app.add_handler(CommandHandler("matchs", list_matches))
    app.add_handler(CommandHandler(["foot", "pronostics"], lambda u, c: analyze(u, c, "football")))
    app.add_handler(CommandHandler("basket", lambda u, c: analyze(u, c, "basket")))
    app.add_handler(CommandHandler("hockey", lambda u, c: analyze(u, c, "hockey")))
    app.add_handler(CommandHandler("bilan", bilan))

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
