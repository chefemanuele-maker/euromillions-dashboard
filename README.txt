USE THIS PACKAGE TO RESTORE ONLY THE EUROMILLIONS DASHBOARD ON RENDER.

FILES TO KEEP IN YOUR GITHUB REPO:
- app.py
- requirements.txt
- render.yaml
- euromillions_live_dashboard.py
- euromillions_export_2026-03-16.csv

RENDER SETTINGS:
- Build Command: pip install -r requirements.txt
- Start Command: gunicorn app:app
- Cron Refresh Command: python refresh_job.py
- Optional env var for manual admin refresh endpoint: ADMIN_REFRESH_TOKEN=<strong-random-token>

LOCAL DEVELOPMENT:
- Runtime dependencies: pip install -r requirements.txt
- Test dependencies: pip install -r requirements-dev.txt
- Run tests: python -m unittest -v test_dashboard_core && python test_engine_core.py
- Run locally: gunicorn app:app

NOTES:
- /euromillions first tries to serve the existing dashboard cache for the selected line count.
- If no matching cache exists, /euromillions builds the dashboard from local CSV history only.
- Loading local history is read-only; it does not rewrite the CSV during normal page/API/download requests.
- Online refresh and cache writes happen only through explicit refresh flows: the Render cron job, refresh_job.py, or the token-protected /admin/refresh endpoint.
- /admin/refresh is locked unless ADMIN_REFRESH_TOKEN is configured and supplied via ?token=... or X-Admin-Token.
- JSON endpoints: /api/odds?lines=5 and /api/suggested?lines=5.
- The engine includes exact EuroMillions combinatorics, prize-tier odds, pack odds, value scoring, lower shared-prize-risk scoring, and diversified line generation.
- Important truth: every valid line has the same jackpot odds. The model cannot predict random lottery results; it improves data quality, line diversification, budget clarity, and avoids common human patterns that may split prizes.
