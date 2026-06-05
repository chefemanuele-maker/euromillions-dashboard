USE THIS PACKAGE TO RESTORE ONLY THE EUROMILLIONS DASHBOARD ON RENDER.

FILES TO KEEP IN YOUR GITHUB REPO:
- app.py
- requirements.txt
- render.yaml
- euromillions_live_dashboard.py
- euromillions_export_2026-03-16.csv
- trigger_admin_refresh.py

RENDER SETTINGS:
- Build Command: pip install -r requirements.txt
- Start Command: gunicorn app:app
- Cron Refresh Command: python trigger_admin_refresh.py
- Required env var for cron/admin refresh: ADMIN_REFRESH_TOKEN=<strong-random-token>
- Required env var for cron target: EUROMILLIONS_DASHBOARD_URL=<https://your-render-service>

LOCAL DEVELOPMENT:
- Runtime dependencies: pip install -r requirements.txt
- Test dependencies: pip install -r requirements-dev.txt
- Run tests: python -m unittest -v test_dashboard_core && python test_engine_core.py
- Run locally: gunicorn app:app

NOTES:
- /euromillions first tries to serve the existing dashboard cache for the selected line count.
- If no valid cache exists, /euromillions attempts an online refresh and stores a runtime cache.
- If online refresh is unavailable, /euromillions falls back to local CSV history.
- Loading local history is read-only; it does not rewrite the CSV during normal page/API/download requests.
- Online refresh and cache writes happen through stale/missing cache repair, refresh_job.py, or the token-protected /admin/refresh endpoint.
- Render cron calls the web service /admin/refresh endpoint so the web runtime cache is updated; running refresh_job.py in a separate cron container does not update the web service filesystem.
- /admin/refresh is locked unless ADMIN_REFRESH_TOKEN is configured and supplied via ?token=... or X-Admin-Token.
- Runtime files are intentionally ignored by Git: euromillions_history_live.csv, euromillions_refresh_state.json, and euromillions_dashboard_payload.json.
- JSON endpoints: /api/odds?lines=5 and /api/suggested?lines=5.
- The engine includes exact EuroMillions combinatorics, prize-tier odds, pack odds, value scoring, lower shared-prize-risk scoring, and diversified line generation.
- Important truth: every valid line has the same jackpot odds. The model cannot predict random lottery results; it improves data quality, line diversification, budget clarity, and avoids common human patterns that may split prizes.
