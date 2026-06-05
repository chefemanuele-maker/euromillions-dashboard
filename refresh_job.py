from pathlib import Path
import euromillions_live_dashboard as euro

BASE = Path(__file__).resolve().parent
euro.BASE_DIR = BASE
euro.LOCAL_HISTORY = BASE / "euromillions_history_live.csv"
euro.BASELINE_HISTORY = BASE / "euromillions_export_2026-06-02.csv"
euro.USER_ORIGINAL = BASE / "euromillions_export_2026-03-16.csv"
euro.REFRESH_STATE_FILE = BASE / "euromillions_refresh_state.json"
euro.DASHBOARD_CACHE = BASE / "euromillions_dashboard_payload.json"
euro.ensure_base_dir = lambda: None

if __name__ == "__main__":
    data, refresh = euro.build_and_store_dashboard_cache()
    print("REFRESH JOB RESULT")
    print({
        "ok": refresh.ok,
        "source": refresh.source,
        "message": refresh.message,
        "draws_added": refresh.draws_added,
        "latest_date": refresh.latest_date,
        "rows": data.get("history_rows"),
    })
