USE THIS PACKAGE TO RESTORE ONLY THE EUROMILLIONS DASHBOARD ON RENDER.

FILES TO KEEP IN YOUR GITHUB REPO:
- app.py
- requirements.txt
- render.yaml
- euromillions_live_dashboard_v2.py
- euromillions_export_2026-03-16.csv

YOU CAN IGNORE OR DELETE THE SUPERENALOTTO FILES FOR NOW.

RENDER SETTINGS:
- Build Command: pip install -r requirements.txt
- Start Command: gunicorn app:app
- You do NOT need PYTHON_VERSION for this package.
