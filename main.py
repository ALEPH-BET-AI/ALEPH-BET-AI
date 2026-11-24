"""
ALEPH BET AI 5.1
Telegram multi-sports bot: football, basketball and ice hockey.
DataSportsAPI v2. PostgreSQL when DATABASE_URL is configured;
JSON fallback otherwise (not durable on ephemeral hosting).
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
from urllib.parse import urlencode

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
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
REQUEST_GAP = max(0.0, float(os.getenv("API_REQUEST_GAP", "6.3")))
MAX_PAGES = max(1, min(int(os.getenv("MAX_FIXTURE_PAGES", "3")), 20))
MAX_FIXTURES = max(1, min(int(os.getenv("MAX_FIXTURES_PER_ANALYSIS", "8")), 30))
CACHE_SECONDS = max(0, int(os.getenv("CACHE_SECONDS", "600")))
LOCAL_TZ = timezone.utc  # CÃ´te d'Ivoire = UTC

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
    """Readable DataSportsAPI error."""


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def team_name(fixture: dict, side: str) -> str:
    team = fixture.get(side) or {}
    if isinstance(team, dict):
        return str(team.get("name") or ("Ãquipe domicile" if side == "home" else "Ãquipe extÃ©rieure"))
    return str(team)


class DataSportsClient:
    """HTTP client with shared request pacing and a small in-memory cache."""

    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Accept": "application/json",
                "User-Agent": "ALEPH-BET-AI/5.1",
            },
        )
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, params: dict | None = None, cache: bool = True) -> dict:
        normalized = urlencode(sorted((params or {}).items()))
        key = f"{path}?{normalized}"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cache and cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]

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
        fixtures: list[dict] = []
        complete = True
        pages_used = 0

        for page in range(MAX_PAGES):
            payload = await self.get(
                "/v2/fixtures",
                {"date": date_utc, "sport": sport, "page": page},
            )
            rows = payload.get("data") or []
            if isinstance(rows, list):
                fixtures.extend(row for row in rows if isinstance(row, dict))

            meta = payload.get("meta") or {}
            pages_used += 1
            if meta.get("complete") is False:
                complete = False
            if not meta.get("hasNextPage", False):
                break
        else:
            complete = False

        return fixtures, complete, pages_used

    async def upcoming(self, sport: str):
        now = datetime.now(timezone.utc)
        all_rows: list[dict] = []
        coverage_complete = True
        total_pages = 0

        for offset in (0, 1):
            day = (now + timedelta(days=offset)).date().isoformat()
            rows, complete, pages = await self.fixtures_for_date(sport, day)
            all_rows.extend(rows)
            coverage_complete = coverage_complete and complete
            total_pages += pages

        unique: dict[str, dict] = {}
        for fixture in all_rows:
            fixture_id = fixture.get("id")
            start = parse_dt(fixture.get("startTime"))
            status = (fixture.get("status") or {}).get("type", "")
            if fixture_id is None or start is None or start <= now + timedelta(minutes=25):
                continue
            if status not in ("notstarted", "scheduled", ""):
                continue
            unique[str(fixture_id)] = fixture

        ordered = sorted(
            unique.values(),
            key=lambda row: parse_dt(row.get("startTime")) or now,
        )
        return ordered, coverage_complete, total_pages

    async def team_form(self, fixture_id: int):
        return await self.get(f"/v2/fixtures/{fixture_id}/team-form")


def form_summary(matches: list[dict]):
    if not matches:
        return "forme indisponible", 0, None, {"win": 0, "draw": 0, "loss": 0}

    counts = {"win": 0, "draw": 0, "loss": 0}
    marks = []
    totals = []

    for match in matches:
        result = str(match.get("result") or "").lower()
        if result in counts:
            counts[result] += 1
        marks.append({"win": "V", "draw": "N", "loss": "D"}.get(result, "?"))

        scored = match.get("scoreFor")
        conceded = match.get("scoreAgainst")
        if isinstance(scored, (int, float)) and isinstance(conceded, (int, float)):
            totals.append(scored + conceded)

    average_total = sum(totals) / len(totals) if totals else None
    return "-".join(marks[-5:]), len(matches), average_total, counts


def make_analysis(fixture: dict, form_payload: dict, sport: str):
    data = form_payload.get("data") or {}
    if not isinstance(data, dict):
        return None

    home_rows = data.get("home") or []
    away_rows = data.get("away") or []
    if not isinstance(home_rows, list) or not isinstance(away_rows, list):
        return None

    home_rows = [row for row in home_rows if isinstance(row, dict)]
    away_rows = [row for row in away_rows if isinstance(row, dict)]
    if len(home_rows) < 3 or len(away_rows) < 3:
        return None

    hform, hn, havg, hc = form_summary(home_rows)
    aform, an, aavg, ac = form_summary(away_rows)

    hpoints = hc["win"] * 3 + hc["draw"]
    apoints = ac["win"] * 3 + ac["draw"]
    hrate = hpoints / max(1, hn * 3)
    arate = apoints / max(1, an * 3)
    gap = hrate - arate

    # This is an internal heuristic score, never a probability.
    quality = round(min(85, 42 + abs(gap) * 45 + min(hn, an) * 1.5))
    home = team_name(fixture, "home")
    away = team_name(fixture, "away")
    league_obj = fixture.get("league") or {}
    league = league_obj.get("name", "CompÃ©tition non prÃ©cisÃ©e") if isinstance(league_obj, dict) else str(league_obj)
    start = parse_dt(fixture.get("startTime"))
    kickoff = start.strftime("%d/%m/%Y Ã  %H:%M UTC") if start else "Horaire indisponible"

    selection = None
    market = None
    explanation = (
        f"{home} : {hform} ({hn} matchs) / {away} : {aform} ({an} matchs). "
        "Ãchantillons toutes compÃ©titions confondues."
    )

    if sport == "football":
        averages = [value for value in (havg, aavg) if value is not None]
        if len(averages) == 2:
            combined = sum(averages) / 2
            if combined >= 2.8:
                market, selection = "Total de buts", "Plus de 1,5 but"
            elif combined <= 2.0:
                market, selection = "Total de buts", "Moins de 3,5 buts"
            explanation += f" Moyenne de buts cumulÃ©s observÃ©e : {combined:.2f}."
    else:
        if abs(gap) >= 0.30 and quality >= 60:
            market = "Tendance vainqueur (forme rÃ©cente)"
            selection = f"{home} (tendance forme)" if gap > 0 else f"{away} (tendance forme)"
        explanation += (
            f" Indice de rÃ©sultats rÃ©cents : domicile {hpoints}/{hn * 3}, "
            f"extÃ©rieur {apoints}/{an * 3}."
        )

    if not selection:
        return None

    return {
        "sport": sport,
        "fixture_id": str(fixture.get("id")),
        "home": home,
        "away": away,
        "league": league,
        "kickoff": kickoff,
        "market": market,
        "selection": selection,
        "score": quality,
        "explanation": explanation,
    }


class PickStore:
    """PostgreSQL persistence with JSON fallback."""

    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.path = DATA_FILE
        self.lock = asyncio.Lock()

        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)
        if self.database_url and psycopg is None:
            log.error("DATABASE_URL est dÃ©fini mais psycopg n'est pas installÃ©.")
            raise RuntimeError("DÃ©pendance psycopg manquante pour PostgreSQL.")

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    async def init(self):
        if self.database_url:
            await asyncio.to_thread(self._init_pg)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self._write_json({"picks": []})

    def _init_pg(self):
        with self.connect() as conn:
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
                    market TEXT NOT NULL,
                    selection TEXT NOT NULL,
                    score INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(chat_id, fixture_id, market, selection)
                )
            """)

    async def add(self, chat_id: int, pick: dict):
        if self.database_url:
            await asyncio.to_thread(self._add_pg, chat_id, pick)
            return

        async with self.lock:
            data = self._read_json()
            identity = (
                str(chat_id), str(pick["fixture_id"]),
                pick["market"], pick["selection"],
            )
            exists = any(
                (
                    str(row.get("chat_id")), str(row.get("fixture_id")),
                    row.get("market"), row.get("selection"),
                ) == identity
                for row in data["picks"]
            )
            if not exists:
                item = dict(pick)
                item.update({
                    "chat_id": chat_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending",
                })
                data["picks"].append(item)
                self._write_json(data)

    def _add_pg(self, chat_id, pick):
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO aleph_picks
                (chat_id, sport, fixture_id, home, away, league, kickoff,
                 market, selection, score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (chat_id, fixture_id, market, selection) DO NOTHING
            """, (
                chat_id, pick["sport"], str(pick["fixture_id"]),
                pick["home"], pick["away"], pick["league"], pick["kickoff"],
                pick["market"], pick["selection"], pick["score"],
            ))

    async def list_for_chat(self, chat_id: int):
        if self.database_url:
            return await asyncio.to_thread(self._list_pg, chat_id)
        async with self.lock:
            return [
                row for row in self._read_json()["picks"]
                if int(row.get("chat_id", 0)) == int(chat_id)
            ]

    def _list_pg(self, chat_id):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM aleph_picks WHERE chat_id=%s ORDER BY id DESC",
                (chat_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _read_json(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and isinstance(value.get("picks"), list) else {"picks": []}
        except (OSError, json.JSONDecodeError):
            return {"picks": []}

    def _write_json(self, data):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def services(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["services"]


async def send_long(update: Update, text: str):
    while len(text) > 3900:
        cut = text.rfind("\n\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        await update.effective_message.reply_text(text[:cut])
        text = text[cut:].lstrip()
    if text:
        await update.effective_message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
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
    await update.effective_message.reply_text(
        f"ð {APP_NAME}\n\n"
        "â¢ /foot ou /pronostics : analyse football\n"
        "â¢ /basket : analyse basketball\n"
        "â¢ /hockey : analyse hockey sur glace\n"
        "â¢ /matchs : calendrier football\n"
        "â¢ /bilan : sÃ©lections enregistrÃ©es\n"
        "â¢ /statut : connexion API et stockage\n\n"
        "Les analyses nÃ©cessitent au moins trois matchs de forme par Ã©quipe. "
        "L'indice affichÃ© est un indicateur interne, jamais une probabilitÃ© de gain."
    )


async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"ð {APP_NAME} â CENTRE MULTI-SPORTS\n\n"
        "â½ /foot â Football\n"
        "ð /basket â Basketball\n"
        "ð /hockey â Hockey sur glace\n"
        "ð /matchs â Calendrier football\n"
        "ð /bilan â Historique\n"
        "ð /statut â API et stockage\n\n"
        "Architecture extensible : les autres sports seront ajoutÃ©s aprÃ¨s validation de leurs donnÃ©es."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    svc = services(context)
    try:
        payload = await svc["api"].get("/v2/sports", cache=False)
        rows = payload.get("data") or []
        count = len(rows) if isinstance(rows, list) else 0
        storage = "PostgreSQL" if svc["store"].database_url else "JSON local (non durable sur Render)"
        await update.effective_message.reply_text(
            f"ð {APP_NAME}\n"
            "ð¢ Connexion DataSportsAPI : accessible\n"
            f"ð Sports renvoyÃ©s par l'API : {count}\n"
            f"ðï¸ Stockage : {storage}\n"
            "ð ClÃ© API conservÃ©e cÃ´tÃ© serveur."
        )
    except APIError as exc:
        await update.effective_message.reply_text(f"ð´ DataSportsAPI inaccessible.\n{exc}")


async def list_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api = services(context)["api"]
    await update.effective_message.reply_text("ð Recherche des matchs de football Ã  venirâ¦")
    try:
        fixtures, complete, pages = await api.upcoming("football")
    except APIError as exc:
        await update.effective_message.reply_text(f"â ï¸ Impossible de rÃ©cupÃ©rer les matchs.\n{exc}")
        return

    if not fixtures:
        await update.effective_message.reply_text("ð­ Aucun match futur trouvÃ© dans les dates vÃ©rifiÃ©es.")
        return

    lines = [f"ð {APP_NAME} â â½ MATCHS Ã VENIR", ""]
    for index, fixture in enumerate(fixtures[:20], 1):
        start = parse_dt(fixture.get("startTime"))
        when = start.strftime("%d/%m Ã  %H:%M UTC") if start else "Horaire inconnu"
        league_obj = fixture.get("league") or {}
        league = league_obj.get("name", "CompÃ©tition inconnue") if isinstance(league_obj, dict) else str(league_obj)
        lines.append(
            f"{index}. {team_name(fixture, 'home')} â {team_name(fixture, 'away')}\n"
            f"ð {league}\nð {when}"
        )
    if not complete:
        lines.append("â¹ï¸ Liste partielle : l'API indique que des pages supplÃ©mentaires peuvent exister.")
    lines.append("â¹ï¸ Liste de matchs uniquement, sans recommandation de mise.")
    await send_long(update, "\n\n".join(lines))


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE, sport_key: str):
    svc = services(context)
    spec = SPORTS[sport_key]
    await update.effective_message.reply_text(f"ð Analyse {spec['label'].lower()} en coursâ¦")

    try:
        fixtures, complete, pages = await svc["api"].upcoming(spec["slug"])
    except APIError as exc:
        await update.effective_message.reply_text(f"â ï¸ RÃ©cupÃ©ration impossible.\n{exc}")
        return

    if not fixtures:
        await update.effective_message.reply_text("ð­ Aucun match futur trouvÃ© dans les dates vÃ©rifiÃ©es.")
        return

    picks = []
    errors = 0
    examined = 0

    for fixture in fixtures[:MAX_FIXTURES]:
        examined += 1
        try:
            payload = await svc["api"].team_form(int(fixture["id"]))
            pick = make_analysis(fixture, payload, spec["slug"])
            if pick:
                picks.append(pick)
                await svc["store"].add(update.effective_chat.id, pick)
        except (APIError, ValueError, TypeError, KeyError) as exc:
            errors += 1
            log.warning("Analyse fixture %s : %s", fixture.get("id"), exc)

    now_text = datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")
    lines = [
        f"ð {APP_NAME}",
        f"{spec['emoji']} {spec['label']}",
        f"ð VÃ©rification : {now_text} (CÃ´te d'Ivoire)",
        f"ð Matchs examinÃ©s : {examined}",
        "",
    ]

    if not picks:
        lines.extend([
            "ð­ Aucune sÃ©lection ne franchit les filtres.",
            "Le bot prÃ©fÃ¨re ne rien proposer plutÃ´t que forcer un pronostic.",
        ])
    else:
        for index, pick in enumerate(picks[:5], 1):
            lines.extend([
                f"{index}. {pick['home']} â {pick['away']}",
                f"ð {pick['league']}",
                f"ð {pick['kickoff']}",
                f"ð¯ MarchÃ© : {pick['market']}",
                f"ð SÃ©lection : {pick['selection']}",
                f"ð§­ Indice de cohÃ©rence interne : {pick['score']}/100",
                f"ð {pick['explanation']}",
                "",
            ])

    if not complete:
        lines.append("â¹ï¸ Couverture partielle : certaines pages n'ont pas Ã©tÃ© rÃ©cupÃ©rÃ©es.")
    if errors:
        lines.append(f"â ï¸ DonnÃ©es indisponibles pour {errors} match(s), ignorÃ©s.")
    lines.extend([
        "",
        "â ï¸ L'indice interne n'est ni une probabilitÃ© ni un taux de rÃ©ussite.",
        "Aucun pari n'est sÃ»r. VÃ©rifie les informations et reste prudent.",
    ])
    await send_long(update, "\n".join(lines))


async def foot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await analyze(update, context, "football")


async def basket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await analyze(update, context, "basket")


async def hockey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await analyze(update, context, "hockey")


async def bilan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = services(context)["store"]
    rows = await store.list_for_chat(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("ð Aucun pronostic enregistrÃ© pour le moment.")
        return

    lines = [
        f"ð BILAN {APP_NAME}",
        f"ð Mis Ã  jour : {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')} (CÃ´te d'Ivoire)",
        "",
    ]
    for key, spec in SPORTS.items():
        subset = [row for row in rows if row.get("sport") == spec["slug"]]
        wins = sum(row.get("status") == "won" for row in subset)
        losses = sum(row.get("status") == "lost" for row in subset)
        pending = sum(row.get("status", "pending") == "pending" for row in subset)
        void = sum(row.get("status") == "void" for row in subset)
        settled = wins + losses
        rate = f"{wins / settled * 100:.1f}%" if settled else "pas encore calculable"
        if subset:
            lines.extend([
                f"{spec['emoji']} {spec['label']}",
                f"â GagnÃ©s : {wins} | â Perdus : {losses}",
                f"ð Taux observÃ© : {rate}",
                f"â³ En attente : {pending} | AnnulÃ©s : {void}",
                "",
            ])

    lines.append(
        "Le taux observÃ© porte uniquement sur les sÃ©lections rÃ©glÃ©es. "
        "Le bot ne certifie pas encore automatiquement les rÃ©sultats des matchs."
    )
    await send_long(update, "\n".join(lines))


async def post_init(application: Application):
    api = DataSportsClient()
    store = PickStore()
    await store.init()
    application.bot_data["services"] = {"api": api, "store": store}
    log.info("%s initialisÃ©.", APP_NAME)


async def post_shutdown(application: Application):
    svc = application.bot_data.get("services")
    if svc:
        await svc["api"].close()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Variable TELEGRAM_BOT_TOKEN manquante.")
    if not API_KEY:
        raise RuntimeError("Variable DATASPORTS_API_KEY manquante.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("aide", help_cmd))
    application.add_handler(CommandHandler("premium", premium))
    application.add_handler(CommandHandler("statut", status))
    application.add_handler(CommandHandler("matchs", list_matches))
    application.add_handler(CommandHandler(["foot", "pronostics"], foot))
    application.add_handler(CommandHandler("basket", basket))
    application.add_handler(CommandHandler("hockey", hockey))
    application.add_handler(CommandHandler("bilan", bilan))

    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
    else:
        application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
