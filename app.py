from pathlib import Path
from flask import Flask, Response, jsonify
import traceback
import euromillions_live_dashboard as euro

app = Flask(__name__)

BASE = Path(__file__).resolve().parent
euro.BASE_DIR = BASE
euro.LOCAL_HISTORY = BASE / "euromillions_history_live.csv"
euro.USER_ORIGINAL = BASE / "euromillions_export_2026-03-16.csv"
euro.ensure_base_dir = lambda: None


@app.route("/")
def home():
    return """
    <html>
    <head>
        <title>EuroMillions Dashboard</title>
        <style>
            body {
                background:#0b0f19;
                color:white;
                font-family:Arial;
                padding:40px;
            }
            a {
                color:#4dd0ff;
                font-size:22px;
            }
            pre {
                white-space: pre-wrap;
                background:#111827;
                padding:20px;
                border-radius:12px;
            }
        </style>
    </head>
    <body>
        <h1>EuroMillions Dashboard</h1>
        <p>Server running on Render</p>
        <p><a href="/euromillions">Open EuroMillions Dashboard</a></p>
        <p><a href="/admin/refresh">Run Admin Refresh Check</a></p>
    </body>
    </html>
    """


@app.route("/euromillions")
def euromillions():
    try:
        df, refresh = euro.refresh_history()
        data = euro.build_dashboard_data(df)
        html = euro.render_dashboard(data, refresh)

        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception:
        err = traceback.format_exc()
        return f"""
        <html>
        <body style="background:#0b0f19;color:white;font-family:Arial;padding:40px;">
            <h1>EuroMillions error</h1>
            <pre>{err}</pre>
        </body>
        </html>
        """, 500


@app.route("/admin/refresh")
def admin_refresh():
    try:
        df, refresh = euro.refresh_history()
        return jsonify({
            "ok": refresh.ok,
            "source": refresh.source,
            "message": refresh.message,
            "draws_added": refresh.draws_added,
            "latest_date": refresh.latest_date,
            "rows": len(df),
            "local_history_file": str(euro.LOCAL_HISTORY),
            "user_original_file": str(euro.USER_ORIGINAL),
        })
    except Exception:
        return jsonify({
            "ok": False,
            "error": traceback.format_exc()
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)