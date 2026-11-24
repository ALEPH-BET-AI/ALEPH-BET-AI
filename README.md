# ALEPH BET AI 5.1

Bot Telegram multi-sports utilisant DataSportsAPI v2.

## Sports actuellement configurés
- Football
- Basketball
- Hockey sur glace

## Installation locale

Python 3.11 ou supérieur recommandé.

```bash
pip install -r requirements.txt
python main.py
```

## Variables obligatoires

- `TELEGRAM_BOT_TOKEN` : jeton du bot Telegram
- `DATASPORTS_API_KEY` : clé DataSportsAPI

## Variables facultatives

- `DATASPORTS_API_BASE` : URL de base de l'API
- `WEBHOOK_URL` : URL publique Render ; si absente, le bot utilise le polling
- `WEBHOOK_SECRET` : secret du webhook Telegram
- `PORT` : port HTTP, défaut 10000
- `DATABASE_URL` : connexion PostgreSQL
- `ALEPH_DATA_FILE` : chemin du fichier JSON local
- `API_REQUEST_GAP` : délai minimal entre requêtes API, défaut 6.3 secondes
- `MAX_FIXTURE_PAGES` : nombre maximal de pages par date, défaut 3
- `MAX_FIXTURES_PER_ANALYSIS` : nombre maximal de matchs analysés, défaut 8
- `CACHE_SECONDS` : durée du cache, défaut 600 secondes

## Commandes Telegram

- `/start`
- `/aide`
- `/premium`
- `/statut`
- `/foot`
- `/pronostics`
- `/matchs`
- `/basket`
- `/hockey`
- `/bilan`

## Stockage

Si `DATABASE_URL` est défini, le bot utilise PostgreSQL.
Sinon, il utilise `aleph_data.json`.

Le fichier JSON local peut être perdu lors d'un redémarrage ou redéploiement sur un hébergement éphémère.

## Limites importantes

- Les indices sont des heuristiques internes, pas des probabilités.
- Les statistiques dépendent des champs réellement fournis par DataSportsAPI.
- Le bot ne règle pas encore automatiquement les résultats gagnés/perdus.
- La pagination dépend des métadonnées réellement renvoyées par l'API.
- Tester les commandes sur une branche distincte avant de remplacer un service en production.
