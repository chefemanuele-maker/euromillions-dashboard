import os
import io
from pathlib import Path

from flask import Flask, Response, jsonify, send_file, request
import euromillions_live_dashboard as euro

app = Flask(__name__)

BASE = Path(__file__).resolve().parent
euro.BASE_DIR = BASE
euro.LOCAL_HISTORY = BASE / "euromillions_history_live.csv"
euro.BASELINE_HISTORY = BASE / "euromillions_export_2026-06-02.csv"
euro.USER_ORIGINAL = BASE / "euromillions_export_2026-03-16.csv"
euro.REFRESH_STATE_FILE = BASE / "euromillions_refresh_state.json"
euro.DASHBOARD_CACHE = BASE / "euromillions_dashboard_payload.json"
euro.ensure_base_dir = lambda: None


def admin_authorized() -> bool:
    token = os.environ.get("ADMIN_REFRESH_TOKEN", "").strip()
    # For public deployments, leave admin refresh disabled unless a token is set.
    if not token:
        return False
    supplied = request.args.get("token") or request.headers.get("X-Admin-Token") or ""
    return supplied == token


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
        </style>
    </head>
    <body>
        <h1>EuroMillions Dashboard</h1>
        <p>Server running on Render</p>
        <a href="/euromillions">Open EuroMillions Dashboard</a>
        <!-- Admin refresh is token-protected. Use Render cron/job or /euromillions auto-refresh. -->
        <a href="/download/history">Download History CSV</a>
        <a href="/download/suggested">Download Suggested Lines CSV</a>
    </body>
    </html>
    """


@app.route("/euromillions")
def euromillions():
    try:
        lines_count = request.args.get("lines", default=5, type=int)
        if lines_count not in [1, 3, 5, 10]:
            lines_count = 5

        payload = euro.build_dashboard_payload(premium_line_count=lines_count)
        data = dict(payload["data"])
        data["runtime_status"] = {
            "cache_used": payload.get("cache_used", False),
            "generated_at": payload.get("generated_at"),
        }
        refresh = euro.refresh_from_dict(payload["refresh"])
        html = euro.render_dashboard(data, refresh)

        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception:
        app.logger.exception("EuroMillions dashboard failed")
        return f"""
        <html>
        <body style="background:#0b0f19;color:white;font-family:Arial;padding:40px;">
            <h1>EuroMillions error</h1>
            <p>The dashboard could not load right now. Please try again later.</p>
        </body>
        </html>
        """, 500


@app.route("/admin/refresh", methods=["GET", "POST"])
def admin_refresh():
    if not admin_authorized():
        return jsonify({
            "ok": False,
            "error": "admin_refresh_locked",
            "message": "Set ADMIN_REFRESH_TOKEN and call with ?token=... or X-Admin-Token."
        }), 403
    try:
        data, refresh = euro.build_and_store_dashboard_cache()
        df = euro.load_local_history()
        state = euro.load_refresh_state()
        return jsonify({
            "ok": refresh.ok,
            "source": refresh.source,
            "message": refresh.message,
            "draws_added": refresh.draws_added,
            "latest_date": refresh.latest_date,
            "rows": len(df),
            "cache_history_rows": data.get("history_rows"),
            "quality": euro.history_quality_report(df),
            "last_success_at": state.get("last_success_at"),
            "last_attempt_at": state.get("last_attempt_at"),
            "local_history_file": str(euro.LOCAL_HISTORY),
            "user_original_file": str(euro.USER_ORIGINAL),
        })
    except Exception:
        app.logger.exception("Admin refresh failed")
        return jsonify({
            "ok": False,
            "error": "admin_refresh_failed",
            "message": "The refresh job failed. Check server logs for details."
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
        app.logger.exception("History download failed")
        return jsonify({
            "ok": False,
            "error": "history_download_failed",
            "message": "The history CSV could not be generated."
        }), 500


@app.route("/download/suggested")
def download_suggested():
    try:
        df = euro.load_local_history()
        data = euro.build_dashboard_data(df, premium_line_count=10)
        suggested_df = euro.suggested_to_dataframe(data["suggested"])
        csv_bytes = suggested_df.to_csv(index=False).encode("utf-8")
        return send_file(
            io.BytesIO(csv_bytes),
            mimetype="text/csv",
            as_attachment=True,
            download_name="euromillions_suggested_lines.csv",
        )
    except Exception:
        app.logger.exception("Suggested lines download failed")
        return jsonify({
            "ok": False,
            "error": "suggested_download_failed",
            "message": "The suggested lines CSV could not be generated."
        }), 500


@app.route("/api/odds")
def api_odds():
    lines_count = request.args.get("lines", default=5, type=int)
    if lines_count not in [1, 3, 5, 10]:
        lines_count = 5
    return jsonify({
        "odds": euro.pack_jackpot_probability(lines_count),
        "strategy": euro.budget_strategy(lines_count),
    })


@app.route("/api/suggested")
def api_suggested():
    try:
        lines_count = request.args.get("lines", default=5, type=int)
        if lines_count not in [1, 3, 5, 10]:
            lines_count = 5
        payload = euro.build_dashboard_payload(premium_line_count=lines_count)
        refresh = euro.refresh_from_dict(payload["refresh"])
        data = payload["data"]
        return jsonify({
            "ok": True,
            "latest_draw": data["latest_draw"],
            "history_end": data["history_end"],
            "generated_at": payload.get("generated_at"),
            "cache_used": payload.get("cache_used", False),
            "refresh": refresh.__dict__,
            "odds": data["odds"],
            "best_line": data["best_line"],
            "best_line_reason": data["best_line_reason"],
            "suggested": data["suggested"],
            "quality": data["quality"],
            "strategy": data["strategy"],
            "diversity": data["diversity"],
        })
    except Exception:
        app.logger.exception("Suggested API failed")
        return jsonify({
            "ok": False,
            "error": "suggested_api_failed",
            "message": "Suggested lines are unavailable right now."
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
