import sqlite3

DB_NAME = "tft_analytics.db"

def rollback_latest_run():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. View recent runs
    cursor.execute("SELECT run_id, run_datetime FROM pipeline_runs ORDER BY run_id DESC LIMIT 5")
    runs = cursor.fetchall()

    if not runs:
        print("No runs found in database.")
        conn.close()
        return

    print("--- Recent Runs Stored in Database ---")
    for r_id, r_dt in runs:
        print(f"Run ID: {r_id} | Timestamp (UTC): {r_dt}")
    print("--------------------------------------")

    latest_run_id = runs[0][0]
    latest_run_dt = runs[0][1]

    # Ask for confirmation before deleting
    confirm = input(f"\nAre you sure you want to delete Run ID {latest_run_id} ({latest_run_dt})? (y/n): ")
    if confirm.strip().lower() != "y":
        print("Operation cancelled. Nothing was deleted.")
        conn.close()
        return

    # 2. Delete snapshots associated with this run
    cursor.execute("DELETE FROM player_snapshots WHERE run_id = ?", (latest_run_id,))
    snapshots_deleted = cursor.rowcount

    # 3. Delete the run watermark
    cursor.execute("DELETE FROM pipeline_runs WHERE run_id = ?", (latest_run_id,))
    runs_deleted = cursor.rowcount

    conn.commit()
    conn.close()

    print(f"\nSuccessfully deleted Run ID {latest_run_id}!")
    print(f"- Removed {snapshots_deleted} player snapshot records.")
    print(f"- Removed {runs_deleted} watermark record from pipeline_runs.")
    print("Your previous run is now back to being the active latest run.")

if __name__ == "__main__":
    rollback_latest_run()