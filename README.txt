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

NOTES:
- /euromillions refreshes from public sources when the dashboard loads.
- The Render cron job also refreshes history using refresh_job.py.
- /admin/refresh is now locked unless ADMIN_REFRESH_TOKEN is configured and supplied via ?token=... or X-Admin-Token.
- The engine now includes exact EuroMillions combinatorics, prize-tier odds, pack odds, value scoring, lower shared-prize-risk scoring, and diversified line generation.
- JSON endpoints: /api/odds?lines=5 and /api/suggested?lines=5.
- Important truth: every valid line has the same jackpot odds. The model cannot predict random lottery results; it improves data quality, line diversification, budget clarity, and avoids common human patterns that may split prizes.
