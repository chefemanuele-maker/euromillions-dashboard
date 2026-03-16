from flask import Flask, Response
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
    payload = euro.build_dashboard_payload()
    html = euro.render_html(payload)
    return Response(html, mimetype="text/html")
