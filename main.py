"""
ALEPH BET AI 5.0 — FOOTBALL ONLY
DataSportsAPI v2 | Telegram | PostgreSQL séparé ou JSON local

Important:
- L'indice de solidité est un score heuristique, jamais une probabilité.
- L'endpoint team-form connu fournit les séries home/away de l'affiche.
- Les confrontations directes et les statistiques domicile/extérieur détaillées
  ne sont utilisées que si elles sont réellement présentes dans les données.
- Le règlement des sélections est manuel via /regler.
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

APP_NAME = "ALEPH BET AI 5.0 FOOTBALL"
API_BASE = os.getenv("DATASPORTS_API_BASE", "https://api.datasportsapi.com").rstrip("/")
API_KEY = os.getenv("DATASPORTS_API_KEY", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))

# Fichier et table dédiés au 5.0 : ne pas réutiliser ceux du 5.1.
DATA_FILE = Path(os.getenv("ALEPH_DATA_FILE", "aleph_football_5_0.json"))
DB_TABLE = "aleph_football_picks"

REQUEST_GAP = max(0.0, float(os.getenv("API_REQUEST_GAP", "6.3")))
MAX_PAGES = max(1, min(int(os.getenv("MAX_FIXTURE_PAGES", "3")), 20))
MAX_FIXTURES = max(1, min(int(os.getenv("MAX_FIXTURES_PER_ANALYSIS", "10")), 30))
CACHE_SECONDS = max(0, int(os.getenv("CACHE_SECONDS", "600")))
LOCAL_TZ = timezone.utc  # Côte d'Ivoire = UTC

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("aleph-bet-ai-5.0")


class APIError(Exception):
    pass


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
        return str(team.get("name") or ("Équipe domicile" if side == "home" else "Équipe extérieure"))
    return str(team)


def safe_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class DataSportsClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Accept": "application/json",
                "User-Agent": "ALEPH-BET-AI/5.0",
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
                raise APIError(f"Erreur réseau DataSportsAPI : {exc}") from exc
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
            raise APIError("Réponse JSON invalide de DataSportsAPI.") from exc
        if not isinstance(payload, dict):
            raise APIError("Format inattendu renvoyé par DataSportsAPI.")
        if cache:
            self._cache[key] = (time.monotonic(), payload)
        return payload

    async def fixtures_for_date(self, date_utc: str):
        fixtures: list[dict] = []
        complete = True
        pages_used = 0
        for page in range(MAX_PAGES):
            payload = await self.get(
                "/v2/fixtures",
                {"date": date_utc, "sport": "football", "page": page},
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

    async def upcoming(self):
        now = datetime.now(timezone.utc)
        all_rows: list[dict] = []
        coverage_complete = True
        total_pages = 0
        for offset in (0, 1):
            day = (now + timedelta(days=offset)).date().isoformat()
            rows, complete, pages = await self.fixtures_for_date(day)
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


def summarize_form(matches: list[dict]) -> dict | None:
    """Calcule les statistiques à partir des champs confirmés scoreFor/scoreAgainst."""
    rows = matches[-10:]
    valid = []
    counts = {"win": 0, "draw": 0, "loss": 0}
    marks = []

    for match in rows:
        result = str(match.get("result") or "").lower()
        if result in counts:
            counts[result] += 1
        marks.append({"win": "V", "draw": "N", "loss": "D"}.get(result, "?"))
        gf = safe_number(match.get("scoreFor"))
        ga = safe_number(match.get("scoreAgainst"))
        if gf is not None and ga is not None and gf >= 0 and ga >= 0:
            valid.append((gf, ga))

    if len(rows) < 5 or len(valid) < 5:
        return None

    gf_avg = sum(x[0] for x in valid) / len(valid)
    ga_avg = sum(x[1] for x in valid) / len(valid)
    total_avg = gf_avg + ga_avg
    ppg = (counts["win"] * 3 + counts["draw"]) / len(rows)
    scored_rate = sum(1 for gf, _ in valid if gf > 0) / len(valid)
    conceded_rate = sum(1 for _, ga in valid if ga > 0) / len(valid)

    return {
        "n": len(rows),
        "valid": len(valid),
        "form": "-".join(marks[-5:]),
        "wins": counts["win"],
        "draws": counts["draw"],
        "losses": counts["loss"],
        "ppg": ppg,
        "gf": gf_avg,
        "ga": ga_avg,
        "total": total_avg,
        "scored_rate": scored_rate,
        "conceded_rate": conceded_rate,
    }


def build_candidates(home: dict, away: dict) -> list[dict]:
    """Produit des pistes à partir de seuils conservateurs, sans les appeler probabilités."""
    candidates: list[dict] = []
    combined_total = (home["total"] + away["total"]) / 2
    both_score = (home["scored_rate"] + away["scored_rate"]) / 2
    both_concede = (home["conceded_rate"] + away["conceded_rate"]) / 2
    ppg_gap = home["ppg"] - away["ppg"]

    # Marchés de buts : les deux équipes doivent avoir au moins cinq scores exploitables.
    if combined_total >= 2.75 and home["gf"] >= 0.95 and away["gf"] >= 0.95:
        strength = min(88, 55 + (combined_total - 2.75) * 12
                       + (both_score - 0.65) * 25)
        candidates.append({
            "market": "Total de buts",
            "selection": "Plus de 1,5 but",
            "strength": round(strength),
            "reason": f"Moyenne de buts cumulés observée : {combined_total:.2f}",
        })

    if combined_total <= 2.25 and home["ga"] <= 1.25 and away["ga"] <= 1.25:
        strength = min(86, 54 + (2.25 - combined_total) * 14
                       + (1.0 - both_concede) * 12)
        candidates.append({
            "market": "Total de buts",
            "selection": "Moins de 3,5 buts",
            "strength": round(strength),
            "reason": f"Moyenne de buts cumulés observée : {combined_total:.2f}",
        })

    if home["scored_rate"] >= 0.70 and away["scored_rate"] >= 0.70 \
            and home["conceded_rate"] >= 0.60 and away["conceded_rate"] >= 0.60:
        strength = min(84, 53 + (both_score - 0.70) * 25
                       + (both_concede - 0.60) * 15)
        candidates.append({
            "market": "Les deux équipes marquent",
            "selection": "Oui",
            "strength": round(strength),
            "reason": (
                f"Équipes ayant marqué : domicile {home['scored_rate']:.0%}, "
                f"extérieur {away['scored_rate']:.0%}"
            ),
        })

    # La forme générale ne remplace pas des statistiques spécifiques domicile/extérieur.
    # Seuil de différentiel élevé pour ne proposer une double chance qu'en cas d'écart net.
    if abs(ppg_gap) >= 0.85:
        if ppg_gap > 0:
            selection = "1X (domicile ou nul)"
            fav_name = "équipe à domicile"
        else:
            selection = "X2 (extérieur ou nul)"
            fav_name = "équipe à l'extérieur"
        strength = min(82, 54 + (abs(ppg_gap) - 0.85) * 12)
        candidates.append({
            "market": "Double chance",
            "selection": selection,
            "strength": round(strength),
            "reason": f"Écart de points par match en faveur de l'{fav_name} : {abs(ppg_gap):.2f}",
        })

    # Écarte les signaux faibles et trie par solidité heuristique.
    return sorted(
        [c for c in candidates if c["strength"] >= 60],
        key=lambda item: item["strength"],
        reverse=True,
    )


def make_analysis(fixture: dict, form_payload: dict) -> dict | None:
    data = form_payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    home_rows = data.get("home") or []
    away_rows = data.get("away") or []
    if not isinstance(home_rows, list) or not isinstance(away_rows, list):
        return None

    home = summarize_form([r for r in home_rows if isinstance(r, dict)])
    away = summarize_form([r for r in away_rows if isinstance(r, dict)])
    if not home or not away:
        return None

    candidates = build_candidates(home, away)
    if not candidates:
        return None

    home_name = team_name(fixture, "home")
    away_name = team_name(fixture, "away")
    league_obj = fixture.get("league") or {}
    league = league_obj.get("name", "Compétition non précisée") \
        if isinstance(league_obj, dict) else str(league_obj)
    start = parse_dt(fixture.get("startTime"))
    kickoff = start.strftime("%d/%m/%Y à %H:%M UTC") if start else "Horaire indisponible"

    # On conserve au maximum deux marchés distincts par rencontre.
    selected = []
    used_markets = set()
    for candidate in candidates:
        if candidate["market"] in used_markets:
            continue
        selected.append(candidate)
        used_markets.add(candidate["market"])
        if len(selected) == 2:
            break

    return {
        "fixture_id": str(fixture.get("id")),
        "home": home_name,
        "away": away_name,
        "league": league,
        "kickoff": kickoff,
        "home_form": home,
        "away_form": away,
        "selections": selected,
        "data_note": (
            "Forme récente toutes compétitions confondues. "
            "Les séries reçues ne garantissent pas un découpage domicile/extérieur."
        ),
    }


class PickStore:
    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.path = DATA_FILE
        self.lock = asyncio.Lock()
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)
        if self.database_url and psycopg is None:
            raise RuntimeError("DATABASE_URL défini mais dépendance psycopg manquante.")

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
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {DB_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    chat_id BIGINT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    home TEXT NOT NULL,
                    away TEXT NOT NULL,
                    league TEXT,
                    kickoff TEXT,
                    market TEXT NOT NULL,
                    selection TEXT NOT NULL,
                    strength INTEGER NOT NULL,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(chat_id, fixture_id, market, selection)
                )
            """)

    async def add(self, chat_id: int, fixture: dict, candidate: dict):
        item = {
            "chat_id": int(chat_id),
            "fixture_id": str(fixture["fixture_id"]),
            "home": fixture["home"],
            "away": fixture["away"],
            "league": fixture["league"],
            "kickoff": fixture["kickoff"],
            "market": candidate["market"],
            "selection": candidate["selection"],
            "strength": int(candidate["strength"]),
            "reason": candidate["reason"],
            "status": "pending",
        }
        if self.database_url:
            await asyncio.to_thread(self._add_pg, item)
            return
        async with self.lock:
            data = self._read_json()
            identity = (item["chat_id"], item["fixture_id"], item["market"], item["selection"])
            if not any(
                (r.get("chat_id"), str(r.get("fixture_id")), r.get("market"), r.get("selection")) == identity
                for r in data["picks"]
            ):
                item["id"] = max([int(r.get("id", 0)) for r in data["picks"]] + [0]) + 1
                item["created_at"] = datetime.now(timezone.utc).isoformat()
                data["picks"].append(item)
                self._write_json(data)

    def _add_pg(self, item):
        with self.connect() as conn:
            conn.execute(f"""
                INSERT INTO {DB_TABLE}
                (chat_id, fixture_id, home, away, league, kickoff, market, selection, strength, reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (chat_id, fixture_id, market, selection) DO NOTHING
            """, (
                item["chat_id"], item["fixture_id"], item["home"], item["away"],
                item["league"], item["kickoff"], item["market"], item["selection"],
                item["strength"], item["reason"],
            ))

    async def list_for_chat(self, chat_id: int):
        if self.database_url:
            return await asyncio.to_thread(self._list_pg, chat_id)
        async with self.lock:
            return [r for r in self._read_json()["picks"] if int(r.get("chat_id", 0)) == int(chat_id)]

    def _list_pg(self, chat_id):
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {DB_TABLE} WHERE chat_id=%s ORDER BY id DESC",
                (chat_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    async def settle(self, chat_id: int, pick_id: int, status: str) -> bool:
        if self.database_url:
            return await asyncio.to_thread(self._settle_pg, chat_id, pick_id, status)
        async with self.lock:
            data = self._read_json()
            for row in data["picks"]:
                if int(row.get("chat_id", 0)) == int(chat_id) and int(row.get("id", 0)) == pick_id:
                    row["status"] = status
                    self._write_json(data)
                    return True
        return False

    def _settle_pg(self, chat_id, pick_id, status):
        with self.connect() as conn:
            result = conn.execute(
                f"UPDATE {DB_TABLE} SET status=%s WHERE chat_id=%s AND id=%s",
                (status, chat_id, pick_id),
            )
            return result.rowcount > 0

    def _read_json(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("picks"), list):
                return value
        except (OSError, json.JSONDecodeError):
            pass
        return {"picks": []}

    def _write_json(self, data):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def services(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["services"]


async def send_long(update: Update, message: str):
    while len(message) > 3900:
        cut = message.rfind("\n\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        await update.effective_message.reply_text(message[:cut])
        message = message[cut:].lstrip()
    if message:
        await update.effective_message.reply_text(message)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"👑 Bienvenue sur {APP_NAME}\n\n"
        "Commandes :\n"
        "/foot — analyser les matchs à venir\n"
        "/pronostics — alias de /foot\n"
        "/matchs — calendrier des matchs\n"
        "/bilan — résultats des sélections\n"
        "/regler ID gagne|perdu|annule — régler une sélection\n"
        "/statut — état API et stockage\n"
        "/aide — guide\n\n"
        "L'indice de solidité est heuristique, pas une probabilité. Aucun pari n'est sûr."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"👑 {APP_NAME}\n\n"
        "• /foot ou /pronostics : analyse football\n"
        "• /matchs : matchs à venir\n"
        "• /bilan : sélections enregistrées\n"
        "• /regler ID gagne|perdu|annule : enregistrer le résultat réel d'une sélection\n"
        "• /statut : connexion API et stockage\n\n"
        "Le moteur exige au moins cinq matchs exploitables par équipe. "
        "Il peut ne proposer aucune sélection si les données sont insuffisantes."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    svc = services(context)
    try:
        payload = await svc["api"].get("/v2/sports", cache=False)
        rows = payload.get("data") or []
        count = len(rows) if isinstance(rows, list) else 0
        storage = "PostgreSQL (table dédiée 5.0)" if svc["store"].database_url else "JSON local (non durable sur Render)"
        await update.effective_message.reply_text(
            f"👑 {APP_NAME}\n"
            "🟢 Connexion DataSportsAPI : accessible\n"
            f"📚 Sports renvoyés par l'API : {count}\n"
            f"🗄️ Stockage : {storage}\n"
            "🔐 Clés conservées côté serveur."
        )
    except APIError as exc:
        await update.effective_message.reply_text(f"🔴 DataSportsAPI inaccessible.\n{exc}")


async def list_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api = services(context)["api"]
    await update.effective_message.reply_text("🔎 Recherche des matchs de football à venir…")
    try:
        fixtures, complete, _ = await api.upcoming()
    except APIError as exc:
        await update.effective_message.reply_text(f"⚠️ Impossible de récupérer les matchs.\n{exc}")
        return
    if not fixtures:
        await update.effective_message.reply_text("📭 Aucun match futur trouvé dans les dates vérifiées.")
        return

    lines = [f"👑 {APP_NAME} — ⚽ MATCHS À VENIR", ""]
    for index, fixture in enumerate(fixtures[:20], 1):
        start_time = parse_dt(fixture.get("startTime"))
        when = start_time.strftime("%d/%m à %H:%M UTC") if start_time else "Horaire inconnu"
        league_obj = fixture.get("league") or {}
        league = league_obj.get("name", "Compétition inconnue") if isinstance(league_obj, dict) else str(league_obj)
        lines.append(
            f"{index}. {team_name(fixture, 'home')} — {team_name(fixture, 'away')}\n"
            f"🏆 {league}\n🗓 {when}"
        )
    if not complete:
        lines.append("ℹ️ Liste partielle : des pages supplémentaires peuvent exister.")
    await send_long(update, "\n\n".join(lines))


async def foot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    svc = services(context)
    await update.effective_message.reply_text("🔎 Analyse football approfondie en cours…")
    try:
        fixtures, complete, _ = await svc["api"].upcoming()
    except APIError as exc:
        await update.effective_message.reply_text(f"⚠️ Récupération impossible.\n{exc}")
        return
    if not fixtures:
        await update.effective_message.reply_text("📭 Aucun match futur trouvé dans les dates vérifiées.")
        return

    reports = []
    errors = 0
    examined = 0
    for fixture in fixtures[:MAX_FIXTURES]:
        examined += 1
        try:
            payload = await svc["api"].team_form(int(fixture["id"]))
            analysis = make_analysis(fixture, payload)
            if analysis:
                reports.append(analysis)
                for candidate in analysis["selections"]:
                    await svc["store"].add(update.effective_chat.id, analysis, candidate)
        except (APIError, ValueError, TypeError, KeyError) as exc:
            errors += 1
            log.warning("Analyse fixture %s : %s", fixture.get("id"), exc)

    reports.sort(
        key=lambda item: max((s["strength"] for s in item["selections"]), default=0),
        reverse=True,
    )
    lines = [
        f"👑 {APP_NAME}",
        f"🕒 Vérification : {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')} (Côte d'Ivoire)",
        f"🔎 Matchs examinés : {examined}",
        "",
    ]
    if not reports:
        lines.extend([
            "📭 Aucune sélection ne franchit les filtres.",
            "Le moteur préfère s'abstenir plutôt que forcer un pronostic.",
        ])
    else:
        for index, report in enumerate(reports[:5], 1):
            hf = report["home_form"]
            af = report["away_form"]
            lines.extend([
                f"{index}. {report['home']} — {report['away']}",
                f"🏆 {report['league']}",
                f"🗓 {report['kickoff']}",
                f"📊 Forme domicile : {hf['form']} | Buts marqués/match : {hf['gf']:.2f} | encaissés : {hf['ga']:.2f}",
                f"📊 Forme extérieur : {af['form']} | Buts marqués/match : {af['gf']:.2f} | encaissés : {af['ga']:.2f}",
            ])
            for candidate in report["selections"]:
                lines.extend([
                    f"🎯 {candidate['market']} : {candidate['selection']}",
                    f"🧭 Indice de solidité interne : {candidate['strength']}/100",
                    f"📝 {candidate['reason']}",
                ])
            lines.append(f"ℹ️ {report['data_note']}")
            lines.append("")

    if not complete:
        lines.append("ℹ️ Couverture partielle : certaines pages de matchs n'ont pas été récupérées.")
    if errors:
        lines.append(f"⚠️ Données indisponibles pour {errors} match(s), ignorés.")
    lines.extend([
        "⚠️ L'indice n'est ni une probabilité ni un taux de réussite.",
        "Les confrontations directes ne sont pas inventées si l'API ne les fournit pas.",
    ])
    await send_long(update, "\n".join(lines))


async def bilan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await services(context)["store"].list_for_chat(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("📊 Aucun pronostic enregistré pour le moment.")
        return
    wins = sum(r.get("status") == "won" for r in rows)
    losses = sum(r.get("status") == "lost" for r in rows)
    pending = sum(r.get("status", "pending") == "pending" for r in rows)
    void = sum(r.get("status") == "void" for r in rows)
    settled = wins + losses
    rate = f"{wins / settled * 100:.1f}%" if settled else "pas encore calculable"
    lines = [
        f"📊 BILAN {APP_NAME}",
        f"🕒 Mis à jour : {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')} (Côte d'Ivoire)",
        f"✅ Gagnés : {wins} | ❌ Perdus : {losses}",
        f"📈 Taux observé : {rate}",
        f"⏳ En attente : {pending} | Annulés : {void}",
        "",
        "Dernières sélections :",
    ]
    for row in rows[:15]:
        status_label = {"pending": "EN ATTENTE", "won": "GAGNÉ", "lost": "PERDU", "void": "ANNULÉ"}.get(row.get("status"), row.get("status"))
        lines.append(
            f"ID {row.get('id', '?')} — {row.get('home')} / {row.get('away')}\n"
            f"{row.get('market')} : {row.get('selection')} — {status_label}"
        )
    lines.extend([
        "",
        "Le taux observé ne porte que sur les sélections réglées.",
        "Le règlement est manuel : vérifie le score final avant d'utiliser /regler.",
    ])
    await send_long(update, "\n\n".join(lines))


async def regler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Utilisation : /regler ID gagne|perdu|annule\nExemple : /regler 12 gagne"
        )
        return
    try:
        pick_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("L'ID doit être un nombre.")
        return
    status_map = {"gagne": "won", "perdu": "lost", "annule": "void"}
    status_value = status_map.get(context.args[1].lower())
    if not status_value:
        await update.effective_message.reply_text("Résultat attendu : gagne, perdu ou annule.")
        return
    ok = await services(context)["store"].settle(update.effective_chat.id, pick_id, status_value)
    await update.effective_message.reply_text(
        f"✅ Sélection ID {pick_id} mise à jour." if ok
        else f"⚠️ Sélection ID {pick_id} introuvable dans ton historique."
    )


async def post_init(application: Application):
    api = DataSportsClient()
    store = PickStore()
    await store.init()
    application.bot_data["services"] = {"api": api, "store": store}
    log.info("%s initialisé.", APP_NAME)


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
    application.add_handler(CommandHandler("statut", status))
    application.add_handler(CommandHandler("matchs", list_matches))
    application.add_handler(CommandHandler(["foot", "pronostics"], foot))
    application.add_handler(CommandHandler("bilan", bilan))
    application.add_handler(CommandHandler("regler", regler))

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
