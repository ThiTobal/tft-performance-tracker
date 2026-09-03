# Format Riot ID to ensure no row ever shows "None"
def format_display_name(row):
    if pd.notna(row["riot_id"]) and str(row["riot_id"]).strip() != "" and str(row["riot_id"]).strip() != "None":
        return str(row["riot_id"])
    pid = str(row["player_id"])
    return f"Player ({row['region']}_{pid[:6]})"

df_filtered["display_name"] = df_filtered.apply(format_display_name, axis=1)

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