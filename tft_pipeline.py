import os
import time
import sqlite3
import logging
from datetime import datetime, timedelta
import requests

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("tft_pipeline.log"), logging.StreamHandler()]
)

# Configuration Parameters
API_KEY = os.getenv("RIOT_API_KEY", "RGAPI-01166014-0d64-4839-8a07-6731f0b78d8c")
DB_NAME = "tft_analytics.db"
TARGET_REGIONS = ["na1", "euw1", "kr", "eun1", "br1", "jp1"]
TIERS = ["challenger", "grandmaster", "master"]
TOP_CANDIDATE_LIMIT = 200  # Enriched with Top 1s
ACTIVE_RESOLVE_LIMIT_PER_REGION = 30  # Resolves names for top active climbers per region
RETENTION_DAYS = 5

REGION_TO_CLUSTER = {
    "na1": "americas",
    "br1": "americas",
    "euw1": "europe",
    "eun1": "europe",
    "kr": "asia",
    "jp1": "asia"
}

def get_connection():
    return sqlite3.connect(DB_NAME, timeout=60.0)

def initialize_database():
    """Initializes tables for pipeline watermarks, persistent identities, and snapshots."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Table to track execution watermarks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp INTEGER NOT NULL,
            run_datetime TEXT NOT NULL
        );
    """)

    # Permanent cache of player Riot IDs (persists across 5-day retention purges)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS player_identities (
            player_id TEXT PRIMARY KEY,
            riot_id TEXT NOT NULL,
            last_updated TEXT NOT NULL
        );
    """)

    # Rolling player snapshot metrics
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS player_snapshots (
            run_id INTEGER NOT NULL,
            region TEXT NOT NULL,
            player_id TEXT NOT NULL,
            riot_id TEXT,
            tier TEXT NOT NULL,
            league_points INTEGER NOT NULL,
            total_games INTEGER NOT NULL,
            total_top4 INTEGER NOT NULL,
            total_top4_pct REAL NOT NULL,
            lp_gain INTEGER DEFAULT 0,
            window_games INTEGER DEFAULT 0,
            window_top4 INTEGER DEFAULT 0,
            window_top4_pct REAL DEFAULT 0.0,
            window_top1 INTEGER DEFAULT NULL,
            window_top1_pct REAL DEFAULT NULL,
            PRIMARY KEY (run_id, region, player_id)
        );
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_run ON player_snapshots(run_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_lookup ON player_snapshots(player_id, run_id);")
    conn.commit()
    conn.close()

def riot_request(url: str):
    """Robust API request wrapper with automated HTTP 429 retry-after handling."""
    headers = {"X-Riot-Token": API_KEY}
    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
        except requests.exceptions.RequestException as e:
            logging.warning(f"Network error: {e}. Retrying in 5s...")
            time.sleep(5)
            continue

        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            logging.warning(f"Rate limited (429). Sleeping for {retry_after + 1}s...")
            time.sleep(retry_after + 1)
        elif resp.status_code == 404:
            return None
        elif resp.status_code == 403:
            logging.error("HTTP 403: Riot API Key is invalid or expired.")
            return None
        else:
            logging.error(f"API Error {resp.status_code}: {resp.text}")
            return None

def fetch_ladder():
    """Ingests current macro stats for apex leagues across all targeted regions."""
    ladder_data = []
    for region in TARGET_REGIONS:
        for tier in TIERS:
            url = f"https://{region}.api.riotgames.com/tft/league/v1/{tier}"
            data = riot_request(url)
            if not data or "entries" not in data:
                continue
            
            tier_name = data.get("tier", tier.upper())
            for entry in data["entries"]:
                player_id = entry.get("puuid") or entry.get("summonerId")
                if not player_id:
                    continue
                
                wins = int(entry.get("wins") or 0)
                losses = int(entry.get("losses") or 0)
                total_games = wins + losses
                top4_pct = round((wins / total_games * 100), 2) if total_games > 0 else 0.0

                ladder_data.append({
                    "region": region,
                    "player_id": str(player_id),
                    "tier": tier_name,
                    "league_points": int(entry.get("leaguePoints") or 0),
                    "total_games": total_games,
                    "total_top4": wins,
                    "total_top4_pct": top4_pct
                })
            logging.info(f"Loaded {len(data['entries'])} players for {region.upper()} {tier_name}.")
            time.sleep(1.2)
    return ladder_data

def cache_identities(id_map: dict):
    """Saves resolved identities into persistent player_identities table."""
    if not id_map:
        return
    conn = get_connection()
    cursor = conn.cursor()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    rows = [(pid, rid, today_str) for pid, rid in id_map.items()]
    cursor.executemany("""
        INSERT OR REPLACE INTO player_identities (player_id, riot_id, last_updated)
        VALUES (?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()

def load_cached_identities():
    """Loads all known player identities from persistent cache."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT player_id, riot_id FROM player_identities")
    mapping = dict(cursor.fetchall())
    conn.close()
    return mapping

def enrich_top1_match_history(candidates, min_ts, max_ts):
    """
    Enriches candidates with exact Top 1s from match history and
    harvests all 8 participants from each lobby into the persistent identity cache.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    match_cache = {}
    harvested_identities = {}
    total_candidates = len(candidates)
    
    logging.info(f"Starting Top 1 enrichment for {total_candidates} top performers...")

    for idx, c in enumerate(candidates, start=1):
        region = c["region"]
        puuid = c["player_id"]
        run_id = c["run_id"]
        cluster = REGION_TO_CLUSTER.get(region, "americas")
        
        match_ids_url = (
            f"https://{cluster}.api.riotgames.com/tft/match/v1/matches/by-puuid/{puuid}/ids"
            f"?startTime={min_ts}&endTime={max_ts}&count=30"
        )
        match_ids = riot_request(match_ids_url)
        time.sleep(0.05)
        
        if not match_ids:
            cursor.execute("""
                UPDATE player_snapshots 
                SET window_top1 = 0, window_top1_pct = 0.0
                WHERE run_id = ? AND region = ? AND player_id = ?
            """, (run_id, region, puuid))
            conn.commit()
            continue

        top1_count = 0
        candidate_riot_id = None

        for mid in match_ids:
            if mid in match_cache:
                match_data = match_cache[mid]
            else:
                detail_url = f"https://{cluster}.api.riotgames.com/tft/match/v1/matches/{mid}"
                match_data = riot_request(detail_url)
                time.sleep(0.05)
                if match_data:
                    match_cache[mid] = match_data

            if not match_data:
                continue

            info = match_data.get("info", {})
            for p in info.get("participants", []):
                p_puuid = p.get("puuid")
                gname = p.get("riotIdGameName")
                tag = p.get("riotIdTagline")
                
                # Harvest all players in this match lobby
                if p_puuid and gname and tag:
                    harvested_identities[p_puuid] = f"{gname}#{tag}"

                if p_puuid == puuid:
                    if p.get("placement") == 1:
                        top1_count += 1
                    if gname and tag:
                        candidate_riot_id = f"{gname}#{tag}"

        window_games = c["window_games"]
        top1_pct = round((top1_count / window_games * 100), 1) if window_games > 0 else 0.0

        cursor.execute("""
            UPDATE player_snapshots 
            SET window_top1 = ?, window_top1_pct = ?, riot_id = COALESCE(?, riot_id)
            WHERE run_id = ? AND region = ? AND player_id = ?
        """, (top1_count, top1_pct, candidate_riot_id, run_id, region, puuid))
        conn.commit()

        if idx % 10 == 0 or idx == total_candidates:
            logging.info(f"Enriched {idx}/{total_candidates} candidates. Harvested {len(harvested_identities)} lobby names so far.")

    conn.close()
    if harvested_identities:
        cache_identities(harvested_identities)

def resolve_active_unnamed_players(run_id):
    """
    Finds active players who gained LP or played games in this window 
    whose names are still missing, and resolves them via Account-v1.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Pick top active players per region whose riot_id is NULL
    unnamed_to_resolve = []
    for region in TARGET_REGIONS:
        cursor.execute("""
            SELECT player_id, region 
            FROM player_snapshots 
            WHERE run_id = ? AND region = ? AND riot_id IS NULL AND window_games >= 1
            ORDER BY lp_gain DESC 
            LIMIT ?
        """, (run_id, region, ACTIVE_RESOLVE_LIMIT_PER_REGION))
        unnamed_to_resolve.extend(cursor.fetchall())

    if not unnamed_to_resolve:
        conn.close()
        return

    logging.info(f"Resolving names directly for {len(unnamed_to_resolve)} active climbers without names...")
    new_identities = {}

    for pid, reg in unnamed_to_resolve:
        cluster = REGION_TO_CLUSTER.get(reg, "americas")
        url = f"https://{cluster}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{pid}"
        acc = riot_request(url)
        time.sleep(0.05)
        if acc:
            gname = acc.get("gameName")
            tag = acc.get("tagLine")
            if gname and tag:
                riot_id = f"{gname}#{tag}"
                new_identities[pid] = riot_id
                cursor.execute("""
                    UPDATE player_snapshots 
                    SET riot_id = ? 
                    WHERE run_id = ? AND player_id = ?
                """, (riot_id, run_id, pid))
    
    conn.commit()
    conn.close()

    if new_identities:
        cache_identities(new_identities)
        logging.info(f"Successfully resolved and cached {len(new_identities)} additional player names.")

def run_pipeline():
    initialize_database()
    conn = get_connection()
    cursor = conn.cursor()

    current_ts = int(time.time())
    current_dt = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Determine previous run watermark
    cursor.execute("SELECT run_id, run_timestamp FROM pipeline_runs ORDER BY run_id DESC LIMIT 1")
    prev_run = cursor.fetchone()

    if prev_run:
        prev_run_id, min_ts = prev_run
        logging.info(f"Previous run detected (ID: {prev_run_id}). Window: {min_ts} -> {current_ts}")
    else:
        prev_run_id = None
        min_ts = current_ts - (24 * 3600)
        logging.info(f"Initial run. Window defaulting to past 24h: {min_ts} -> {current_ts}")

    # Register current run
    cursor.execute("INSERT INTO pipeline_runs (run_timestamp, run_datetime) VALUES (?, ?)", (current_ts, current_dt))
    current_run_id = cursor.lastrowid
    conn.commit()

    # Load persistent identity cache
    known_identities = load_cached_identities()
    logging.info(f"Loaded {len(known_identities)} known player identities from local cache.")

    # Ingest Current Ladder
    current_ladder = fetch_ladder()

    # Load previous stats for delta calculation
    prev_stats = {}
    if prev_run_id:
        cursor.execute("""
            SELECT region, player_id, league_points, total_games, total_top4
            FROM player_snapshots WHERE run_id = ?
        """, (prev_run_id,))
        for row in cursor.fetchall():
            prev_stats[(row[0], row[1])] = {
                "lp": row[2],
                "games": row[3],
                "top4": row[4]
            }

    # Store Current Snapshots
    records = []
    for p in current_ladder:
        reg = p["region"]
        pid = p["player_id"]
        key = (reg, pid)

        lp_gain = 0
        w_games = 0
        w_top4 = 0
        w_top4_pct = 0.0

        if key in prev_stats:
            prev = prev_stats[key]
            lp_gain = p["league_points"] - prev["lp"]
            w_games = p["total_games"] - prev["games"]
            w_top4 = p["total_top4"] - prev["top4"]
            if w_games > 0:
                w_top4_pct = round((w_top4 / w_games * 100), 1)

        # Look up Riot ID from persistent cache
        riot_id = known_identities.get(pid)

        records.append((
            current_run_id,
            reg,
            pid,
            riot_id,
            p["tier"],
            p["league_points"],
            p["total_games"],
            p["total_top4"],
            p["total_top4_pct"],
            lp_gain,
            w_games,
            w_top4,
            w_top4_pct
        ))

    cursor.executemany("""
        INSERT INTO player_snapshots (
            run_id, region, player_id, riot_id, tier, league_points,
            total_games, total_top4, total_top4_pct,
            lp_gain, window_games, window_top4, window_top4_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    logging.info(f"Successfully recorded {len(records)} snapshots for Run ID {current_run_id}.")

    # Identify Top Performers for Top 1 Enrichment + Lobby Harvesting
    cursor.execute("""
        SELECT region, player_id, lp_gain, window_games, run_id
        FROM player_snapshots
        WHERE run_id = ? AND window_games >= 1
        ORDER BY lp_gain DESC
        LIMIT ?
    """, (current_run_id, TOP_CANDIDATE_LIMIT))
    
    candidate_rows = cursor.fetchall()
    candidates = [
        {"region": r[0], "player_id": r[1], "lp_gain": r[2], "window_games": r[3], "run_id": r[4]}
        for r in candidate_rows
    ]
    conn.close()

    if candidates:
        enrich_top1_match_history(candidates, min_ts, current_ts)

    # Resolve any remaining active climbers without names per region
    resolve_active_unnamed_players(current_run_id)

    # Sync snapshot table with freshly discovered identities
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE player_snapshots 
        SET riot_id = (SELECT riot_id FROM player_identities WHERE player_identities.player_id = player_snapshots.player_id)
        WHERE run_id = ? AND riot_id IS NULL AND player_id IN (SELECT player_id FROM player_identities)
    """, (current_run_id,))
    conn.commit()
    conn.close()

    # Purge snapshots older than RETENTION_DAYS
    purge_historical_data()

def purge_historical_data():
    """Purges runs and snapshots older than RETENTION_DAYS (preserves player_identities cache)."""
    conn = get_connection()
    cursor = conn.cursor()
    cutoff_ts = int(time.time()) - (RETENTION_DAYS * 86400)
    
    cursor.execute("SELECT run_id FROM pipeline_runs WHERE run_timestamp < ?", (cutoff_ts,))
    old_runs = [r[0] for r in cursor.fetchall()]
    
    if old_runs:
        cursor.execute(f"DELETE FROM player_snapshots WHERE run_id IN ({','.join('?'*len(old_runs))})", old_runs)
        cursor.execute("DELETE FROM pipeline_runs WHERE run_timestamp < ?", (cutoff_ts,))
        conn.commit()
        logging.info(f"Purged {len(old_runs)} runs older than {RETENTION_DAYS} days.")
    conn.close()

if __name__ == "__main__":
    run_pipeline()