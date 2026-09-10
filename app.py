import sqlite3
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st

DB_NAME = "tft_analytics.db"

st.set_page_config(
    page_title="Global TFT Performance Tracker",
    page_icon="⚔️",
    layout="wide"
)

def get_connection():
    return sqlite3.connect(DB_NAME, timeout=30.0)

def to_brt_string(utc_str: str) -> str:
    """Converts a UTC 'YYYY-MM-DD HH:MM:SS' string to BRT (UTC -3)."""
    try:
        dt_utc = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
        dt_brt = dt_utc - timedelta(hours=3)
        return dt_brt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str

def load_runs():
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT run_id, run_timestamp, run_datetime FROM pipeline_runs ORDER BY run_id DESC", 
        conn
    )
    conn.close()
    return df

def load_snapshots_for_runs(run_ids):
    if not run_ids:
        return pd.DataFrame()
    conn = get_connection()
    placeholders = ",".join(["?"] * len(run_ids))
    query = f"""
        SELECT 
            run_id,
            region,
            player_id,
            riot_id,
            tier,
            league_points,
            total_games,
            total_top4,
            total_top4_pct,
            lp_gain,
            window_games,
            window_top4,
            window_top1
        FROM player_snapshots 
        WHERE run_id IN ({placeholders})
    """
    df = pd.read_sql_query(query, conn, params=run_ids)
    conn.close()
    return df

st.title("⚔️ Global TFT Top-Performer Analytics Dashboard")

df_runs = load_runs()

if df_runs.empty:
    st.warning("No run data found in database. Please run `python tft_pipeline.py` first.")
    st.stop()

# Sidebar: Window Selection
st.sidebar.header("Time Window Settings")

total_available_runs = len(df_runs)
max_possible_windows = total_available_runs - 1

if total_available_runs <= 1:
    st.sidebar.info("Only 1 run stored so far. Window deltas will activate starting on your 2nd run.")
    num_windows = 1
elif max_possible_windows == 1:
    st.sidebar.info("2 runs stored: 1 comparison window active.")
    num_windows = 1
else:
    num_windows = st.sidebar.slider(
        "Lookback Span (Windows)",
        min_value=1,
        max_value=min(9, max_possible_windows),
        value=1,
        help="1 window = last run only. 2+ windows = sums deltas across consecutive runs."
    )

# Determine the run_ids included in the chosen window
selected_run_rows = df_runs.iloc[:num_windows]
selected_run_ids = selected_run_rows["run_id"].tolist()

latest_run = df_runs.iloc[0]
baseline_run_index = min(num_windows, total_available_runs - 1)
baseline_run = df_runs.iloc[baseline_run_index]

# Convert timestamps to BRT (UTC -3)
latest_dt_str = to_brt_string(latest_run["run_datetime"])
baseline_dt_str = to_brt_string(baseline_run["run_datetime"])

st.sidebar.caption(f"**Latest Run:** {latest_dt_str} BRT")
st.sidebar.caption(f"**Baseline Run:** {baseline_dt_str} BRT")

st.sidebar.markdown("---")
st.sidebar.header("Leaderboard Filters")

raw_snapshots = load_snapshots_for_runs(selected_run_ids)

if raw_snapshots.empty:
    st.warning("No snapshot data found for the selected runs.")
    st.stop()

# Region & Tier Filters
available_regions = sorted(raw_snapshots["region"].unique().tolist())
selected_region = st.sidebar.selectbox("Server Region", options=["ALL"] + available_regions)

available_tiers = ["ALL", "CHALLENGER", "GRANDMASTER", "MASTER"]
selected_tier = st.sidebar.selectbox("Minimum Rank Tier", options=available_tiers)

ranking_metric = st.sidebar.selectbox(
    "Rank Players By",
    options=["LP Gain", "Window Top 1%", "Window Top 4%"]
)

min_games = st.sidebar.number_input(
    "Min. Games in Window", 
    min_value=1, 
    max_value=50, 
    value=max(2, num_windows * 2),
    help="Filters out players with too few games across all ranking metrics."
)

# Multi-Window Aggregation Logic
latest_run_id = int(latest_run["run_id"])
latest_metadata = raw_snapshots[raw_snapshots["run_id"] == latest_run_id].copy()

def sum_top1(series):
    valid = series.dropna()
    return valid.sum() if not valid.empty else None

aggregated = raw_snapshots.groupby(["region", "player_id"]).agg({
    "lp_gain": "sum",
    "window_games": "sum",
    "window_top4": "sum",
    "window_top1": sum_top1
}).reset_index()

# Merge back latest rank, LP, total games, and Riot ID
merged = pd.merge(
    aggregated,
    latest_metadata[["region", "player_id", "riot_id", "tier", "league_points", "total_games", "total_top4_pct"]],
    on=["region", "player_id"],
    how="inner"
)

# Compute dynamic percentage rates across the aggregated window
merged["window_top4_pct"] = merged.apply(
    lambda r: round((r["window_top4"] / r["window_games"] * 100), 1) if r["window_games"] > 0 else 0.0,
    axis=1
)

merged["window_top1_pct"] = merged.apply(
    lambda r: round((r["window_top1"] / r["window_games"] * 100), 1) if pd.notna(r["window_top1"]) and r["window_games"] > 0 else None,
    axis=1
)

# Apply Region & Tier Filters
df_filtered = merged.copy()

if selected_region != "ALL":
    df_filtered = df_filtered[df_filtered["region"] == selected_region]

if selected_tier != "ALL":
    tier_scores = {"CHALLENGER": 3, "GRANDMASTER": 2, "MASTER": 1}
    target = tier_scores.get(selected_tier, 0)
    df_filtered["tier_score"] = df_filtered["tier"].map(lambda t: tier_scores.get(t, 0))
    df_filtered = df_filtered[df_filtered["tier_score"] >= target]

# Apply Min Games Filter across ALL ranking modes
df_filtered = df_filtered[df_filtered["window_games"] >= min_games]

# Sorting Logic
if ranking_metric == "LP Gain":
    df_filtered = df_filtered.sort_values(by=["lp_gain", "window_top4_pct"], ascending=[False, False])
elif ranking_metric == "Window Top 1%":
    df_filtered = df_filtered.sort_values(by=["window_top1_pct", "lp_gain"], ascending=[False, False])
else:  # Window Top 4%
    df_filtered = df_filtered.sort_values(by=["window_top4_pct", "lp_gain"], ascending=[False, False])

# Format Display Name cleanly
def format_display_name(row):
    if pd.notna(row["riot_id"]) and str(row["riot_id"]).strip() not in ("", "None"):
        return str(row["riot_id"])
    pid = str(row["player_id"])
    return f"Player ({row['region']}_{pid[:6]})"

df_filtered["display_name"] = df_filtered.apply(format_display_name, axis=1)

# Summary Banner
window_title = f"{num_windows} Window" if num_windows == 1 else f"{num_windows} Windows"
st.subheader(f"Performance Overview: {window_title} ({baseline_dt_str} → {latest_dt_str} BRT)")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Active Filtered Players", len(df_filtered))
col2.metric("Top LP Gain", f"+{int(df_filtered['lp_gain'].max()) if not df_filtered.empty else 0} LP")
col3.metric("Enriched with Top 1s", f"{df_filtered['window_top1'].notna().sum()} players")
col4.metric("Avg Games in Window", f"{df_filtered['window_games'].mean():.1f}" if not df_filtered.empty else "0")

# Build Output Table
output_table = df_filtered[[
    "region",
    "display_name",
    "tier",
    "league_points",
    "lp_gain",
    "window_games",
    "window_top1",
    "window_top1_pct",
    "window_top4",
    "window_top4_pct",
    "total_games",
    "total_top4_pct"
]].copy()

output_table.columns = [
    "Region",
    "Riot ID / Handle",
    "Current Tier",
    "Current LP",
    f"LP Gain ({num_windows}W)",
    "Window Games",
    "Window 1sts",
    "Window Top 1%",
    "Window Top 4s",
    "Window Top 4%",
    "Season Games",
    "Season Top 4%"
]

output_table["Window Top 1%"] = output_table["Window Top 1%"].apply(lambda v: f"{v:.1f}%" if pd.notna(v) else "-")
output_table["Window 1sts"] = output_table["Window 1sts"].apply(lambda v: str(int(v)) if pd.notna(v) else "-")
output_table["Window Top 4%"] = output_table["Window Top 4%"].map("{:.1f}%".format)
output_table["Season Top 4%"] = output_table["Season Top 4%"].map("{:.1f}%".format)

st.dataframe(
    output_table.reset_index(drop=True),
    use_container_width=True,
    height=650
)