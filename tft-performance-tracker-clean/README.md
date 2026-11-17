# TFT Apex Ladder Tracker

A private tool that snapshots the official Teamfight Tactics Challenger, Grandmaster and Master ladders twice a day, so a small closed group of players can see who is climbing right now and study those players' games afterward.

**Not public, not monetized, no in-game features.** The dashboard is a Streamlit app restricted to invited viewers (the owner and 5 friends).

## What it does

1. A scheduled GitHub Actions job (08:00 and 20:00 UTC) reads the apex ladders in NA, EUW, EUNE, KR, BR and JP.
2. For each player it stores LP, games and top-4 counts, and computes the change since the previous snapshot: LP gained, top-4 rate and, for the ~200 biggest climbers, top-1 rate from their completed matches in that window.
3. The dashboard ranks players by those changes so we can pick whose past games to study.

It only mirrors Riot's official ladder. It does **not** create a rating, MMR or alternative ranking, and it gives **no** recommendations, live data, scouting or in-game information.

## Riot API usage

| Endpoint | Purpose |
|---|---|
| `tft-league-v1` `/tft/league/v1/{challenger,grandmaster,master}` | Ladder snapshots (18 calls per run) |
| `tft-match-v1` `/tft/match/v1/matches/by-puuid/{puuid}/ids`, `/tft/match/v1/matches/{matchId}` | Top-1 counts for the biggest climbers in each window |
| `account-v1` `/riot/account/v1/accounts/by-puuid/{puuid}` | Current Riot ID shown next to a PUUID |

Typically 2,000–3,000 requests per run (at most ~6,400). A client-side rate limiter keeps every request under the key's limits and honors `Retry-After`.

## Data and privacy

- **Storage:** the SQLite database lives in a separate **private** repository. This public repo contains code only.
- **Retention:** ladder snapshots keep the last 10 runs (~5 days); Riot IDs not seen again within 30 days are deleted. No match data is stored beyond the per-window counts.
- **Identity:** PUUID is the key. Riot IDs are only current display labels and are not kept as a history.
- **Access:** invited viewers only. The app reads the stored snapshot and never calls the Riot API.
- Nothing is sold, shared or exposed through an API.

## API key handling

The Riot API key is stored as an encrypted GitHub Actions secret (`RIOT_API_KEY`) and read from the environment at runtime. It is never in source code or committed files. Locally it comes from a gitignored `.env`. All requests use HTTPS.

## Running locally

```bash
pip install -r requirements.txt
echo "RIOT_API_KEY=RGAPI-..." > .env      # gitignored
python tft_pipeline.py                    # writes tft_analytics.db (gitignored)
streamlit run app.py
```

Set `DB_PATH` to use a different database file. To read the database from the private data repo instead, copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml` and fill it in.

## Legal

TFT Apex Ladder Tracker isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
