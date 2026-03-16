from flask import Flask, Response
import traceback
import euromillions_live_dashboard as euro

app = Flask(__name__)

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
    </body>
    </html>
    """

@app.route("/euromillions")
def euromillions():
    try:
        payload = euro.build_dashboard_data()
        html = euro.render_html(payload)
        return Response(html, mimetype="text/html")
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
