import os
import io
import traceback
from pathlib import Path

from flask import Flask, Response, jsonify, send_file
import euromillions_live_dashboard as euro

app = Flask(__name__)

BASE = Path(__file__).resolve().parent
euro.BASE_DIR = BASE
euro.LOCAL_HISTORY = BASE / "euromillions_history_live.csv"
euro.USER_ORIGINAL = BASE / "euromillions_export_2026-03-16.csv"
euro.REFRESH_STATE_FILE = BASE / "euromillions_refresh_state.json"
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
                display:block;
                margin:12px 0;
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
        <a href="/euromillions">Open EuroMillions Dashboard</a>
        <a href="/admin/refresh">Run Admin Refresh Check</a>
        <a href="/download/history">Download History CSV</a>
        <a href="/download/suggested">Download Suggested Lines CSV</a>
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
        state = euro.load_refresh_state()
        return jsonify({
            "ok": refresh.ok,
            "source": refresh.source,
            "message": refresh.message,
            "draws_added": refresh.draws_added,
            "latest_date": refresh.latest_date,
            "rows": len(df),
            "last_success_at": state.get("last_success_at"),
            "last_attempt_at": state.get("last_attempt_at"),
            "local_history_file": str(euro.LOCAL_HISTORY),
            "user_original_file": str(euro.USER_ORIGINAL),
        })
    except Exception:
        return jsonify({
            "ok": False,
            "error": traceback.format_exc()
        }), 500


@app.route("/download/history")
def download_history():
    try:
        df = euro.load_local_history()
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        return send_file(
            io.BytesIO(csv_bytes),
            mimetype="text/csv",
            as_attachment=True,
            download_name="euromillions_history_live.csv",
        )
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


@app.route("/download/suggested")
def download_suggested():
    try:
        df = euro.load_local_history()
        data = euro.build_dashboard_data(df)
        suggested_df = euro.suggested_to_dataframe(data["suggested"])
        csv_bytes = suggested_df.to_csv(index=False).encode("utf-8")
        return send_file(
            io.BytesIO(csv_bytes),
            mimetype="text/csv",
            as_attachment=True,
            download_name="euromillions_suggested_lines.csv",
        )
    except Exception:
        return jsonify({"ok": False, "error": traceback.format_exc()}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)