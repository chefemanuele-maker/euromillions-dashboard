#!/usr/bin/env python3
"""
EuroMillions Live Dashboard v2
- Refreshes from the official UK National Lottery EuroMillions draw-history XML endpoint when available.
- Falls back to local cached history when the official source is unavailable.
- Generates ranked numbers, stars, and suggested lines.
- Highlights a single "best line for next draw" for quick use.
- Serves a local neon / hacker-style dashboard.

This is a statistical helper, not a prediction machine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import random
import re
import threading
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import pandas as pd
except Exception:
    print("This app needs pandas installed. Try: pip3 install pandas requests")
    raise

try:
    import requests
except Exception:
    print("This app needs requests installed. Try: pip3 install requests")
    raise

OFFICIAL_XML_URL = "https://www.national-lottery.co.uk/results/euromillions/draw-history/xml"
OFFICIAL_RESULTS_URL = "https://www.national-lottery.co.uk/results/euromillions"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

DEFAULT_PORT = 8765
BASE_DIR = Path.home() / "Data" / "Euro"
LOCAL_HISTORY = BASE_DIR / "euromillions_history_live.csv"
USER_ORIGINAL = BASE_DIR / "euromillions_export_2026-03-16.csv"

MAIN_RANGE = list(range(1, 51))
STAR_RANGE = list(range(1, 13))


@dataclass
class RefreshResult:
    source: str
    ok: bool
    message: str
    draws_added: int = 0
    latest_date: Optional[str] = None


@dataclass
class BestLineDecision:
    mode: str
    reason: str


def ensure_base_dir() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {c: c.strip().lower() for c in df.columns}
    df = df.rename(columns=rename_map).copy()

    required = [
        "draw_date", "ball_1", "ball_2", "ball_3", "ball_4", "ball_5",
        "lucky_star_1", "lucky_star_2"
    ]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")

    for optional_col in ["draw_number", "uk_millionaire_maker", "jackpot", "source"]:
        if optional_col not in df.columns:
            df[optional_col] = pd.NA if optional_col != "source" else "local"

    df["draw_date"] = pd.to_datetime(df["draw_date"], errors="coerce").dt.date

    num_cols = [
        "ball_1", "ball_2", "ball_3", "ball_4", "ball_5",
        "lucky_star_1", "lucky_star_2"
    ]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    return df.dropna(subset=["draw_date"] + num_cols).copy()


def dedupe_history(df: pd.DataFrame) -> pd.DataFrame:
    keys = [
        "draw_date", "ball_1", "ball_2", "ball_3",
        "ball_4", "ball_5", "lucky_star_1", "lucky_star_2"
    ]
    return (
        df.sort_values(["draw_date"], ascending=True)
          .drop_duplicates(subset=keys, keep="last")
          .sort_values(["draw_date"], ascending=True)
          .reset_index(drop=True)
    )


def persist_history(df: pd.DataFrame) -> None:
    out = df.copy()
    out["draw_date"] = out["draw_date"].astype(str)
    out.to_csv(LOCAL_HISTORY, index=False)


def load_local_history() -> pd.DataFrame:
    ensure_base_dir()
    candidates = []
    if LOCAL_HISTORY.exists():
        candidates.append(LOCAL_HISTORY)
    if USER_ORIGINAL.exists():
        candidates.append(USER_ORIGINAL)

    frames: List[pd.DataFrame] = []
    for path in candidates:
        try:
            frames.append(standardize_columns(pd.read_csv(path)))
        except Exception:
            continue

    if not frames:
        raise FileNotFoundError(
            "No usable EuroMillions CSV found. Put your CSV in ~/Data/Euro/"
        )

    df = dedupe_history(pd.concat(frames, ignore_index=True))
    persist_history(df)
    return df


def parse_official_xml(text: str) -> pd.DataFrame:
    text = text.strip()
    root = ET.fromstring(text)
    rows: List[Dict[str, object]] = []

    for draw in root.findall(".//draw"):
        row: Dict[str, object] = {"source": "official_xml"}

        def grab(*names: str) -> Optional[str]:
            for name in names:
                el = draw.find(name)
                if el is not None and el.text:
                    return el.text.strip()
            return None

        row["draw_date"] = grab("draw-date", "date")
        row["draw_number"] = grab("draw-number", "draw-no", "id")
        row["jackpot"] = grab("jackpot-amount", "jackpot")
        row["uk_millionaire_maker"] = grab("uk-millionaire-maker", "ukmm-code", "millionaire-maker-code")

        direct_balls = [grab(f"ball-{i}") for i in range(1, 6)]
        direct_stars = [grab(f"lucky-star-{i}") for i in range(1, 3)]

        if all(v is not None for v in direct_balls + direct_stars) and row.get("draw_date"):
            for i, v in enumerate(direct_balls, 1):
                row[f"ball_{i}"] = int(re.sub(r"\D", "", str(v)))
            for i, v in enumerate(direct_stars, 1):
                row[f"lucky_star_{i}"] = int(re.sub(r"\D", "", str(v)))
            rows.append(row)
            continue

        values: List[int] = []
        for child in draw.iter():
            if child.text:
                t = child.text.strip()
                if re.fullmatch(r"\d{1,2}", t):
                    values.append(int(t))
        if len(values) >= 7 and row.get("draw_date"):
            for i, v in enumerate(values[:5], 1):
                row[f"ball_{i}"] = v
            row["lucky_star_1"] = values[5]
            row["lucky_star_2"] = values[6]
            rows.append(row)

    if not rows:
        raise ValueError("No draw rows parsed from official XML.")
    return standardize_columns(pd.DataFrame(rows))


def fetch_official_xml(timeout: int = 20) -> pd.DataFrame:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml,text/xml,text/plain,*/*",
        "Referer": OFFICIAL_RESULTS_URL,
    }
    resp = requests.get(OFFICIAL_XML_URL, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return parse_official_xml(resp.text)


def refresh_history() -> Tuple[pd.DataFrame, RefreshResult]:
    df = load_local_history()
    try:
        official = fetch_official_xml()
        before = len(df)
        merged = dedupe_history(pd.concat([df, official], ignore_index=True))
        persist_history(merged)
        added = len(merged) - before
        return merged, RefreshResult(
            source="official_xml",
            ok=True,
            message="Official refresh complete.",
            draws_added=max(0, added),
            latest_date=str(merged["draw_date"].max()),
        )
    except Exception as exc:
        return df, RefreshResult(
            source="local_cache",
            ok=False,
            message=f"Official source unavailable right now. Using local cache. ({exc})",
            draws_added=0,
            latest_date=str(df["draw_date"].max()) if not df.empty else None,
        )


def enrich_history(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ball_cols = [f"ball_{i}" for i in range(1, 6)]
    out["draw_date"] = pd.to_datetime(out["draw_date"])
    out["sum_balls"] = out[ball_cols].astype(int).sum(axis=1)
    out["odd_count"] = out[ball_cols].astype(int).apply(lambda r: sum(v % 2 for v in r), axis=1)
    out["even_count"] = 5 - out["odd_count"]
    out["low_count"] = out[ball_cols].astype(int).apply(lambda r: sum(v <= 25 for v in r), axis=1)
    out["high_count"] = 5 - out["low_count"]
    out["odd_even"] = out["odd_count"].astype(str) + "-" + out["even_count"].astype(str)
    out["low_high"] = out["low_count"].astype(str) + "-" + out["high_count"].astype(str)
    return out.sort_values("draw_date").reset_index(drop=True)


def build_rank_table(df: pd.DataFrame, number_pool: Sequence[int], cols: Sequence[str], kind: str) -> pd.DataFrame:
    n_draws = len(df)
    appearances = {n: 0 for n in number_pool}
    last_seen_index = {n: None for n in number_pool}

    for idx, row in df.reset_index(drop=True).iterrows():
        vals = [int(row[c]) for c in cols]
        for v in vals:
            if v in appearances:
                appearances[v] += 1
                last_seen_index[v] = idx

    rows = []
    for n in number_pool:
        seen = appearances[n]
        freq_rate = seen / n_draws if n_draws else 0.0
        draws_since_seen = n_draws if last_seen_index[n] is None else n_draws - 1 - int(last_seen_index[n])

        hot_score = freq_rate * 100.0
        overdue_score = (draws_since_seen / max(n_draws, 1)) * 100.0
        score = (hot_score * 0.62) + (overdue_score * 0.23) + (min(draws_since_seen, 20) * 0.75)

        rows.append({
            "number": n,
            "kind": kind,
            "times_seen": seen,
            "frequency_pct": round(freq_rate * 100, 3),
            "draws_since_seen": draws_since_seen,
            "score": round(score, 3),
        })

    rank = pd.DataFrame(rows).sort_values(
        ["score", "times_seen", "number"], ascending=[False, False, True]
    ).reset_index(drop=True)
    rank["rank"] = range(1, len(rank) + 1)
    return rank[["rank", "number", "kind", "times_seen", "frequency_pct", "draws_since_seen", "score"]]


def top_pattern_tables(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    odd_even = df["odd_even"].value_counts().rename_axis("pattern").reset_index(name="count")
    odd_even["pct"] = (odd_even["count"] / len(df) * 100).round(2)
    low_high = df["low_high"].value_counts().rename_axis("pattern").reset_index(name="count")
    low_high["pct"] = (low_high["count"] / len(df) * 100).round(2)
    return odd_even, low_high


def weighted_sample_without_replacement(population: Sequence[int], weights: Sequence[float], k: int, rng: random.Random) -> List[int]:
    items = list(population)
    w = list(weights)
    chosen: List[int] = []
    for _ in range(min(k, len(items))):
        total = sum(max(x, 0.00001) for x in w)
        pick = rng.random() * total
        upto = 0.0
        idx = 0
        for i, weight in enumerate(w):
            upto += max(weight, 0.00001)
            if upto >= pick:
                idx = i
                break
        chosen.append(items.pop(idx))
        w.pop(idx)
    return chosen


def line_score(
    balls: Sequence[int],
    stars: Sequence[int],
    main_rank: pd.DataFrame,
    star_rank: pd.DataFrame,
    odd_even_popular: Sequence[str],
    low_high_popular: Sequence[str],
    hist_sum_mean: float,
    hist_sum_std: float,
) -> float:
    main_lookup = main_rank.set_index("number")["score"].to_dict()
    star_lookup = star_rank.set_index("number")["score"].to_dict()
    base = sum(main_lookup.get(n, 0.0) for n in balls) + sum(star_lookup.get(s, 0.0) for s in stars)

    odd = sum(n % 2 for n in balls)
    low = sum(n <= 25 for n in balls)
    odd_even = f"{odd}-{5 - odd}"
    low_high = f"{low}-{5 - low}"

    pattern_bonus = 0.0
    if odd_even in odd_even_popular[:2]:
        pattern_bonus += 12.0
    elif odd_even in odd_even_popular[:4]:
        pattern_bonus += 6.0

    if low_high in low_high_popular[:2]:
        pattern_bonus += 12.0
    elif low_high in low_high_popular[:4]:
        pattern_bonus += 6.0

    total_sum = sum(balls)
    z = abs((total_sum - hist_sum_mean) / hist_sum_std) if hist_sum_std else 0.0
    sum_bonus = max(0.0, 18.0 - (z * 8.0))

    spread = max(balls) - min(balls)
    spread_bonus = 10.0 if spread >= 18 else 3.0

    consecutive_pairs = sum(1 for a, b in zip(sorted(balls), sorted(balls)[1:]) if b == a + 1)
    consecutive_penalty = consecutive_pairs * 4.5

    last_digit_penalty = (len(balls) - len({n % 10 for n in balls})) * 1.5
    return round(base + pattern_bonus + sum_bonus + spread_bonus - consecutive_penalty - last_digit_penalty, 3)


def generate_suggested_lines(df: pd.DataFrame, lines_per_mode: int = 4, seed: int = 42) -> pd.DataFrame:
    main_rank = build_rank_table(df, MAIN_RANGE, [f"ball_{i}" for i in range(1, 6)], "main")
    star_rank = build_rank_table(df, STAR_RANGE, ["lucky_star_1", "lucky_star_2"], "star")
    odd_even, low_high = top_pattern_tables(df)

    hist_sum_mean = float(df["sum_balls"].mean())
    hist_sum_std = float(df["sum_balls"].std(ddof=0) or 1.0)
    odd_even_popular = odd_even["pattern"].tolist()
    low_high_popular = low_high["pattern"].tolist()

    rng = random.Random(seed)
    main_weights = {row["number"]: float(row["score"]) for _, row in main_rank.iterrows()}
    star_weights = {row["number"]: float(row["score"]) for _, row in star_rank.iterrows()}

    modes = {
        "safe": {"top_main": 18, "top_star": 8, "jitter": 0.08},
        "balanced": {"top_main": 28, "top_star": 10, "jitter": 0.18},
        "aggressive": {"top_main": 40, "top_star": 12, "jitter": 0.33},
    }

    rows: List[Dict[str, object]] = []
    used = set()

    for mode, cfg in modes.items():
        tries = 0
        made = 0
        while made < lines_per_mode and tries < 1000:
            tries += 1
            main_pool = main_rank["number"].tolist()[:cfg["top_main"]]
            star_pool = star_rank["number"].tolist()[:cfg["top_star"]]
            mw = [max(0.001, main_weights[n] * (1.0 + rng.uniform(-cfg["jitter"], cfg["jitter"]))) for n in main_pool]
            sw = [max(0.001, star_weights[s] * (1.0 + rng.uniform(-cfg["jitter"], cfg["jitter"]))) for s in star_pool]

            balls = sorted(weighted_sample_without_replacement(main_pool, mw, 5, rng))
            stars = sorted(weighted_sample_without_replacement(star_pool, sw, 2, rng))

            odd = sum(n % 2 for n in balls)
            low = sum(n <= 25 for n in balls)
            if abs(odd - 2.5) > 2 or abs(low - 2.5) > 2:
                continue

            key = tuple(balls + [-1] + stars)
            if key in used:
                continue

            score = line_score(
                balls, stars, main_rank, star_rank,
                odd_even_popular, low_high_popular,
                hist_sum_mean, hist_sum_std,
            )

            rows.append({
                "mode": mode,
                "balls": " ".join(f"{x:02d}" for x in balls),
                "stars": " ".join(f"{x:02d}" for x in stars),
                "sum_balls": sum(balls),
                "odd_even": f"{odd}-{5 - odd}",
                "low_high": f"{low}-{5 - low}",
                "score": score,
            })
            used.add(key)
            made += 1

    out = pd.DataFrame(rows).sort_values(["mode", "score"], ascending=[True, False]).reset_index(drop=True)
    mode_order = pd.CategoricalDtype(categories=["safe", "balanced", "aggressive"], ordered=True)
    out["mode"] = out["mode"].astype(mode_order)
    out = out.sort_values(["mode", "score"], ascending=[True, False]).reset_index(drop=True)
    out["mode"] = out["mode"].astype(str)
    return out


def choose_best_line(suggested: pd.DataFrame) -> Tuple[Dict[str, object], BestLineDecision]:
    # Prefer high-scoring balanced first, then safe, then anything else.
    if suggested.empty:
        raise ValueError("No suggested lines generated.")

    balanced = suggested[suggested["mode"] == "balanced"].sort_values("score", ascending=False)
    safe = suggested[suggested["mode"] == "safe"].sort_values("score", ascending=False)
    aggressive = suggested[suggested["mode"] == "aggressive"].sort_values("score", ascending=False)

    if not balanced.empty:
        row = balanced.iloc[0].to_dict()
        return row, BestLineDecision(
            mode="balanced",
            reason="Chosen because balanced lines usually give the best mix of strong numbers, realistic spread, and stable pattern profile.",
        )
    if not safe.empty:
        row = safe.iloc[0].to_dict()
        return row, BestLineDecision(
            mode="safe",
            reason="Chosen because no balanced line was available, so the model took the strongest conservative line.",
        )
    row = aggressive.iloc[0].to_dict()
    return row, BestLineDecision(
        mode="aggressive",
        reason="Chosen as fallback from the highest available score.",
    )


def build_dashboard_data(df: pd.DataFrame) -> Dict[str, object]:
    hist = enrich_history(df)
    main_rank = build_rank_table(hist, MAIN_RANGE, [f"ball_{i}" for i in range(1, 6)], "main")
    star_rank = build_rank_table(hist, STAR_RANGE, ["lucky_star_1", "lucky_star_2"], "star")
    odd_even, low_high = top_pattern_tables(hist)
    suggested = generate_suggested_lines(hist)
    best_line, decision = choose_best_line(suggested)

    latest = hist.iloc[-1]
    latest_draw = {
        "date": latest["draw_date"].date().isoformat(),
        "balls": [int(latest[f"ball_{i}"]) for i in range(1, 6)],
        "stars": [int(latest["lucky_star_1"]), int(latest["lucky_star_2"])],
        "draw_number": "" if pd.isna(latest.get("draw_number")) else str(latest.get("draw_number")),
        "jackpot": "" if pd.isna(latest.get("jackpot")) else str(latest.get("jackpot")),
        "uk_code": "" if pd.isna(latest.get("uk_millionaire_maker")) else str(latest.get("uk_millionaire_maker")),
    }

    return {
        "history_rows": len(hist),
        "latest_draw": latest_draw,
        "main_top10": main_rank.head(10).to_dict(orient="records"),
        "star_top10": star_rank.head(10).to_dict(orient="records"),
        "odd_even_top": odd_even.head(6).to_dict(orient="records"),
        "low_high_top": low_high.head(6).to_dict(orient="records"),
        "suggested": suggested.to_dict(orient="records"),
        "best_line": best_line,
        "best_line_reason": decision.reason,
        "best_line_mode": decision.mode,
        "history_start": hist["draw_date"].min().date().isoformat(),
        "history_end": hist["draw_date"].max().date().isoformat(),
        "sum_mean": round(float(hist["sum_balls"].mean()), 2),
        "sum_std": round(float(hist["sum_balls"].std(ddof=0) or 0), 2),
    }


def render_table(rows: List[Dict[str, object]], columns: Sequence[Tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body_parts = []
    for row in rows:
        tds = "".join(f"<td>{html.escape(str(row.get(key, '')))}</td>" for key, _ in columns)
        body_parts.append(f"<tr>{tds}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_parts)}</tbody></table>"


def mode_chip(mode: str) -> str:
    cls = "safe" if mode == "safe" else "balanced" if mode == "balanced" else "aggressive"
    return f'<span class="chip {cls}">{html.escape(mode.upper())}</span>'


def render_dashboard(data: Dict[str, object], refresh: RefreshResult) -> str:
    latest = data["latest_draw"]
    best = data["best_line"]

    main_table = render_table(
        data["main_top10"],
        [("rank", "#"), ("number", "Number"), ("times_seen", "Seen"), ("draws_since_seen", "Draws since"), ("score", "Score")],
    )
    star_table = render_table(
        data["star_top10"],
        [("rank", "#"), ("number", "Star"), ("times_seen", "Seen"), ("draws_since_seen", "Draws since"), ("score", "Score")],
    )
    odd_even_table = render_table(data["odd_even_top"], [("pattern", "Odd-Even"), ("count", "Count"), ("pct", "%")])
    low_high_table = render_table(data["low_high_top"], [("pattern", "Low-High"), ("count", "Count"), ("pct", "%")])
    suggested_table = render_table(
        data["suggested"],
        [("mode", "Mode"), ("balls", "Main numbers"), ("stars", "Stars"), ("sum_balls", "Sum"), ("odd_even", "Odd-Even"), ("low_high", "Low-High"), ("score", "Score")],
    )

    status_class = "status-ok" if refresh.ok else "status-warn"
    refresh_text = f"{refresh.message} Added {refresh.draws_added} new draw(s)." if refresh.ok else refresh.message
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    balls_html = "".join(f'<span class="ball">{n:02d}</span>' for n in latest["balls"])
    stars_html = "".join(f'<span class="star">{n:02d}</span>' for n in latest["stars"])
    best_balls_html = "".join(f'<span class="ball hero-ball">{n}</span>' for n in str(best["balls"]).split())
    best_stars_html = "".join(f'<span class="star hero-star">{n}</span>' for n in str(best["stars"]).split())

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EuroMillions Live Dashboard v2</title>
<meta http-equiv="refresh" content="900">
<style>
:root {{
  --bg-0:#02060c;
  --bg-1:#07131a;
  --bg-2:#0d1f23;
  --panel:#091118cc;
  --panel-2:#0d1820;
  --line:#19303a;
  --text:#dbfff5;
  --muted:#90b5ab;
  --neon:#00ff9c;
  --neon-2:#00d8ff;
  --gold:#ffd54a;
  --danger:#ff6b6b;
  --safe:#0bcf7a;
  --balanced:#00d8ff;
  --aggr:#ff6b6b;
  --shadow:0 0 0 1px rgba(0,255,156,.08), 0 0 24px rgba(0,255,156,.08), inset 0 0 0 1px rgba(255,255,255,.02);
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0;
  color:var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  background:
    radial-gradient(circle at top left, rgba(0,255,156,.08), transparent 24%),
    radial-gradient(circle at top right, rgba(0,216,255,.08), transparent 24%),
    linear-gradient(180deg, var(--bg-0), var(--bg-1) 45%, var(--bg-0));
  min-height:100vh;
}}
body::before {{
  content:"";
  position:fixed; inset:0;
  pointer-events:none;
  background-image: linear-gradient(rgba(255,255,255,.015) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.012) 1px, transparent 1px);
  background-size: 100% 3px, 3px 100%;
  opacity:.25;
}}
.wrap {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
.grid {{ display:grid; gap:18px; }}
.top {{ grid-template-columns: 1.3fr .7fr; }}
.two {{ grid-template-columns: 1fr 1fr; }}
.three {{ grid-template-columns: repeat(3, 1fr); }}
.card {{
  background: linear-gradient(180deg, rgba(9,17,24,.94), rgba(5,11,16,.94));
  border:1px solid rgba(0,255,156,.12);
  border-radius: 22px;
  padding: 18px;
  box-shadow: var(--shadow);
  position:relative;
  overflow:hidden;
}}
.card::after {{
  content:"";
  position:absolute; inset:0;
  background: linear-gradient(120deg, transparent 0%, rgba(255,255,255,.02) 30%, transparent 70%);
  pointer-events:none;
}}
.hero-title {{ font-size: 42px; line-height:1; margin: 6px 0 10px; letter-spacing:-1px; }}
.sub {{ color: var(--muted); line-height:1.55; max-width: 950px; }}
.tiny {{ color: var(--muted); font-size: 12px; }}
.badge {{
  display:inline-flex; align-items:center; gap:8px;
  padding:8px 12px; border-radius:999px; font-size:12px; font-weight:700;
  border:1px solid rgba(0,255,156,.15); background:rgba(0,255,156,.06); color:var(--neon);
  text-transform:uppercase; letter-spacing:.08em;
}}
.status-ok {{ color:var(--safe); border-color:rgba(11,207,122,.25); background:rgba(11,207,122,.08); }}
.status-warn {{ color:#ffb3b3; border-color:rgba(255,107,107,.2); background:rgba(255,107,107,.08); }}
.section-title {{ font-size: 24px; margin: 0 0 12px; }}
.panel-title {{ font-size:18px; margin:0 0 8px; color:#f0fff8; }}
.mono-line {{ color:var(--neon); opacity:.9; font-size:13px; }}
.kpi-grid {{ display:grid; grid-template-columns: repeat(4,1fr); gap:12px; margin-top:16px; }}
.kpi {{ background:rgba(0,255,156,.04); border:1px solid rgba(0,255,156,.1); border-radius:16px; padding:12px; }}
.kpi .label {{ color:var(--muted); font-size:12px; }}
.kpi .value {{ font-size:20px; margin-top:5px; font-weight:800; }}
.balls {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
.ball,.star {{
  width:46px; height:46px; display:inline-flex; align-items:center; justify-content:center;
  border-radius:999px; font-weight:900; font-size:15px;
  border:1px solid rgba(255,255,255,.08);
}}
.ball {{ background:#ecfff8; color:#06110d; box-shadow:0 0 16px rgba(255,255,255,.08); }}
.star {{ background:var(--gold); color:#342400; box-shadow:0 0 16px rgba(255,213,74,.18); }}
.hero-line {{ display:flex; flex-wrap:wrap; gap:10px; margin: 14px 0; }}
.hero-ball,.hero-star {{ width:58px; height:58px; font-size:18px; }}
.best-row {{ display:grid; grid-template-columns: 1.2fr .8fr; gap:16px; align-items:stretch; }}
.best-meta {{ display:grid; grid-template-columns: repeat(4,1fr); gap:10px; margin-top:14px; }}
.best-meta .box {{ background:rgba(0,216,255,.04); border:1px solid rgba(0,216,255,.12); border-radius:14px; padding:10px; }}
.best-meta .box .v {{ font-weight:800; font-size:18px; margin-top:4px; }}
.chip {{ display:inline-flex; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; letter-spacing:.08em; }}
.chip.safe {{ background:rgba(11,207,122,.14); color:#8dffd0; }}
.chip.balanced {{ background:rgba(0,216,255,.14); color:#9befff; }}
.chip.aggressive {{ background:rgba(255,107,107,.14); color:#ffbaba; }}
.mode-grid {{ display:grid; grid-template-columns: repeat(3,1fr); gap:12px; margin-bottom:14px; }}
.mode-box {{ background:rgba(255,255,255,.02); border:1px solid rgba(255,255,255,.06); border-radius:16px; padding:14px; }}
.mode-box h3 {{ margin:0 0 6px; }}
table {{ width:100%; border-collapse: collapse; }}
th, td {{ border-bottom:1px solid rgba(255,255,255,.06); padding:11px 10px; text-align:left; font-size:14px; }}
th {{ color:#b7ffe5; font-size:12px; letter-spacing:.08em; text-transform:uppercase; }}
tr:hover td {{ background:rgba(0,255,156,.04); }}
.inline-cmd {{ background:rgba(0,255,156,.08); border:1px solid rgba(0,255,156,.12); border-radius:12px; padding:10px 12px; color:#c8ffea; overflow-wrap:anywhere; }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
.btn {{
  cursor:pointer; border:none; text-decoration:none;
  padding:12px 16px; border-radius:14px; font-weight:800;
  background:linear-gradient(180deg, rgba(0,255,156,.18), rgba(0,255,156,.08));
  color:var(--text); border:1px solid rgba(0,255,156,.18);
  box-shadow:0 0 18px rgba(0,255,156,.08);
}}
.btn.alt {{ background:linear-gradient(180deg, rgba(0,216,255,.14), rgba(0,216,255,.08)); border-color:rgba(0,216,255,.18); }}
.small-note {{ color:var(--muted); font-size:13px; line-height:1.5; }}
.footer {{ margin-top:18px; color:var(--muted); font-size:13px; line-height:1.6; }}
@media (max-width: 980px) {{
  .top, .two, .three, .best-row, .mode-grid {{ grid-template-columns: 1fr; }}
  .kpi-grid, .best-meta {{ grid-template-columns: 1fr 1fr; }}
  .hero-title {{ font-size:32px; }}
}}
@media (max-width: 620px) {{
  .kpi-grid, .best-meta {{ grid-template-columns: 1fr; }}
}}
</style>
<script>
function copyBestLine() {{
  const text = document.getElementById('best-line-copy').innerText;
  navigator.clipboard.writeText(text).then(() => {{
    const el = document.getElementById('copy-status');
    el.textContent = 'Copied.';
    setTimeout(() => el.textContent = '', 1800);
  }});
}}
function refreshNow() {{ window.location.reload(); }}
</script>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="badge">EuroMillions live model v2</div>
    <div class="hero-title">EuroMillions weekly picks dashboard</div>
    <div class="sub">A local live page that checks the official UK EuroMillions history source whenever available, updates your stored history, re-scores numbers and stars, and shows a single <strong>best line for next draw</strong> plus backup lines. It stays statistical — not magical.</div>
    <div class="kpi-grid">
      <div class="kpi"><div class="label">Generated</div><div class="value">{html.escape(generated)}</div></div>
      <div class="kpi"><div class="label">History range</div><div class="value">{html.escape(str(data['history_start']))}<br><span class="tiny">to {html.escape(str(data['history_end']))}</span></div></div>
      <div class="kpi"><div class="label">Stored draws</div><div class="value">{data['history_rows']}</div></div>
      <div class="kpi"><div class="label">Source state</div><div class="value"><span class="badge {status_class}">{html.escape(refresh.source)}</span></div></div>
    </div>
  </div>

  <div class="grid top" style="margin-top:18px;">
    <div class="card">
      <div class="section-title">Best line for next draw</div>
      <div>{mode_chip(str(data['best_line_mode']))}</div>
      <div class="hero-line" style="margin-top:14px;">{best_balls_html}</div>
      <div class="hero-line">{best_stars_html}</div>
      <div id="best-line-copy" class="inline-cmd" style="margin-top:14px;">Main numbers: {html.escape(str(best['balls']))} | Stars: {html.escape(str(best['stars']))}</div>
      <div class="actions">
        <button class="btn" onclick="copyBestLine()">Copy best line</button>
        <button class="btn alt" onclick="refreshNow()">Refresh now</button>
        <span id="copy-status" class="small-note"></span>
      </div>
      <div class="best-meta">
        <div class="box"><div class="tiny">Score</div><div class="v">{html.escape(str(best['score']))}</div></div>
        <div class="box"><div class="tiny">Sum</div><div class="v">{html.escape(str(best['sum_balls']))}</div></div>
        <div class="box"><div class="tiny">Odd-Even</div><div class="v">{html.escape(str(best['odd_even']))}</div></div>
        <div class="box"><div class="tiny">Low-High</div><div class="v">{html.escape(str(best['low_high']))}</div></div>
      </div>
      <p class="small-note" style="margin-top:14px;">{html.escape(str(data['best_line_reason']))}</p>
    </div>

    <div class="card">
      <div class="section-title">Sync / machine status</div>
      <p class="small-note">{html.escape(refresh_text)}</p>
      <div class="tiny">Auto page refresh while open: every 15 minutes.</div>
      <div class="tiny">Every time the page loads, it tries the official feed first. If the site is unavailable, it uses your local cache and still recalculates the picks.</div>
      <div class="tiny" style="margin-top:12px;">Official source:</div>
      <div class="inline-cmd">{html.escape(OFFICIAL_XML_URL)}</div>
      <div class="tiny" style="margin-top:12px;">Local history file:</div>
      <div class="inline-cmd">{html.escape(str(LOCAL_HISTORY))}</div>
    </div>
  </div>

  <div class="grid top" style="margin-top:18px; grid-template-columns: 1fr 1fr;">
    <div class="card">
      <div class="section-title">Latest official draw in your history</div>
      <div class="tiny">Draw date: {html.escape(str(latest['date']))}</div>
      <div class="balls">{balls_html}</div>
      <div class="balls">{stars_html}</div>
      <div class="kpi-grid" style="grid-template-columns: repeat(3,1fr);">
        <div class="kpi"><div class="label">Draw number</div><div class="value">{html.escape(str(latest['draw_number'])) or '-'}</div></div>
        <div class="kpi"><div class="label">Jackpot</div><div class="value">{html.escape(str(latest['jackpot'])) or '-'}</div></div>
        <div class="kpi"><div class="label">UK MM code</div><div class="value" style="font-size:16px;">{html.escape(str(latest['uk_code'])) or '-'}</div></div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">What to play</div>
      <p class="small-note"><strong>Fast rule:</strong> when you open this page, use the big line in <strong>Best line for next draw</strong>. That is the single line the dashboard currently prefers.</p>
      <p class="small-note"><strong>Backup rule:</strong> if you want 2 or 3 plays instead of 1, use the top <strong>balanced</strong> line first, then the top <strong>safe</strong> line, then one extra balanced line.</p>
      <p class="small-note"><strong>Do not use</strong> the latest official draw as your next play. That section is only there to confirm that the history is updated.</p>
    </div>
  </div>

  <div class="card" style="margin-top:18px;">
    <div class="section-title">Suggested backup lines</div>
    <div class="mode-grid">
      <div class="mode-box"><h3>Safe</h3><div class="small-note">Leans hardest on the strongest current numbers and stars.</div></div>
      <div class="mode-box"><h3>Balanced</h3><div class="small-note">Usually the smartest default: strong numbers plus a healthier mix.</div></div>
      <div class="mode-box"><h3>Aggressive</h3><div class="small-note">Allows more delayed or less common numbers into the line.</div></div>
    </div>
    {suggested_table}
  </div>

  <div class="grid two" style="margin-top:18px;">
    <div class="card">
      <div class="section-title">Top 10 main numbers</div>
      {main_table}
    </div>
    <div class="card">
      <div class="section-title">Top 10 stars</div>
      {star_table}
    </div>
  </div>

  <div class="grid two" style="margin-top:18px;">
    <div class="card">
      <div class="section-title">Most common odd / even patterns</div>
      {odd_even_table}
    </div>
    <div class="card">
      <div class="section-title">Most common low / high patterns</div>
      {low_high_table}
    </div>
  </div>

  <div class="card footer">
    <strong>Model notes.</strong> Number rankings blend historical frequency with recency and delay. Suggested lines then get bonuses for healthier spread, common balance patterns, and realistic total sums, with penalties for clumping and repeated endings. This is a structured picker, not a promise. Ball-sum mean in your history: <strong>{html.escape(str(data['sum_mean']))}</strong> | standard deviation: <strong>{html.escape(str(data['sum_std']))}</strong>
  </div>
</div>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ["/", "/index.html"]:
            self.send_error(404, "Not found")
            return

        try:
            df, refresh = refresh_history()
            data = build_dashboard_data(df)
            page = render_dashboard(data, refresh).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        except Exception as exc:
            msg = f"<h1>Dashboard error</h1><pre>{html.escape(str(exc))}</pre>".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, fmt: str, *args) -> None:
        return


def run_server(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    ensure_base_dir()
    try:
        refresh_history()
    except Exception:
        pass

    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 72)
    print("EuroMillions Live Dashboard v2")
    print(f"URL: {url}")
    print(f"History CSV: {LOCAL_HISTORY}")
    print("=" * 72)

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the EuroMillions live local dashboard v2")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port for the local dashboard")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser")
    args = parser.parse_args()
    run_server(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
