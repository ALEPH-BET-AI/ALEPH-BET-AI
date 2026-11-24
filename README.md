# 👑 ALEPH BET AI 5.1

Bot Telegram de pronostics sportifs multi-sports : football, basketball et hockey sur glace.

## Commandes
- `/start` — Démarrer
- `/aide` — Aide
- `/premium` — Centre multi-sports
- `/statut` — État de DataSportsAPI
- `/matchs` — Matchs de football à venir
- `/foot` ou `/pronostics` — Analyse football
- `/basket` — Analyse basketball
- `/hockey` — Analyse hockey
- `/bilan` — Bilan des sélections enregistrées

## Fichiers
- `main.py` — application Telegram
- `requirements.txt` — dépendances Python

## Variables Render
- `TELEGRAM_BOT_TOKEN` (ou `BOT_TOKEN`)
- `DATASPORTS_API_KEY`
- `WEBHOOK_URL`
- `WEBHOOK_SECRET` (facultatif)
- `PORT` (Render le fournit généralement)
- `DATABASE_URL` (facultatif, PostgreSQL)

## Déploiement
Build command : `pip install -r requirements.txt`
Start command : `python main.py`

## Stockage
Avec `DATABASE_URL`, l'historique utilise PostgreSQL et nécessite `psycopg`.
Sans `DATABASE_URL`, l'historique est conservé dans un fichier JSON local. Sur un hébergement éphémère, ce fichier peut être perdu au redémarrage ou au redéploiement.

## Limites
Les indices sont heuristiques, pas des probabilités. Les données de forme et les champs de résultats doivent correspondre au schéma effectivement renvoyé par le compte DataSportsAPI. Le code doit être testé en conditions réelles avant toute utilisation. Aucun pari n'est garanti.
