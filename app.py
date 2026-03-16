from pathlib import Path
from flask import Flask, Response
import euromillions_live_dashboard as euro

# Override file locations so the app works from the repo root on Render
BASE = Path(__file__).resolve().parent
euro.BASE_DIR = BASE
euro.LOCAL_HISTORY = BASE / "euromillions_history_live.csv"
euro.USER_ORIGINAL = BASE / "euromillions_export_2026-03-16.csv"

euro.ensure_base_dir = lambda: None

app = Flask(__name__)

@app.route("/")
def home():
    return """
    <html>
    <head><title>EuroMillions Dashboard</title></head>
    <body style="background:#0b0f19;color:white;font-family:Arial;padding:40px;">
      <h1>EuroMillions Dashboard</h1>
      <p>Open the live dashboard here:</p>
      <p><a href='/euromillions' style='color:#4dd0ff;font-size:22px;'>/euromillions</a></p>
    </body>
    </html>
    """

@app.route("/euromillions")
def euromillions():
    payload = euro.build_dashboard_payload()
    html = euro.render_html(payload)
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
