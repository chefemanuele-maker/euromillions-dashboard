#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import secrets
import sys
import html
import hashlib
import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

OFFICIAL_XML_URL = "https://www.national-lottery.co.uk/results/euromillions/draw-history/xml"
OFFICIAL_RESULTS_URL = "https://www.national-lottery.co.uk/results/euromillions"
BACKFILL_RESULTS_URL = "https://www.national-lottery.com/euromillions/results/{slug}"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LOTTERY_DATA_DIR", APP_DIR))
LOCAL_HISTORY = BASE_DIR / "euromillions_history_live.csv"
BASELINE_HISTORY = BASE_DIR / "euromillions_export_2026-06-02.csv"
USER_ORIGINAL = BASE_DIR / "euromillions_export_2026-03-16.csv"
REFRESH_STATE_FILE = BASE_DIR / "euromillions_refresh_state.json"
DASHBOARD_CACHE = BASE_DIR / "euromillions_dashboard_payload.json"
BACKFILL_FETCH_TIMEOUT = int(os.environ.get("EUROMILLIONS_BACKFILL_TIMEOUT", "15"))
BACKFILL_MAX_DRAWS = int(os.environ.get("EUROMILLIONS_BACKFILL_MAX_DRAWS", "60"))
QUICK_BACKFILL_MAX_DRAWS = int(os.environ.get("EUROMILLIONS_QUICK_BACKFILL_MAX_DRAWS", "3"))
QUICK_BACKFILL_LOOKBACK_DAYS = int(os.environ.get("EUROMILLIONS_QUICK_BACKFILL_LOOKBACK_DAYS", "21"))
QUICK_BACKFILL_FETCH_TIMEOUT = int(os.environ.get("EUROMILLIONS_QUICK_BACKFILL_TIMEOUT", "6"))
CACHE_MAX_AGE_SECONDS = int(os.environ.get("EUROMILLIONS_CACHE_MAX_AGE_SECONDS", str(6 * 60 * 60)))
PUBLIC_AUTO_REFRESH_MIN_INTERVAL_SECONDS = int(os.environ.get("EUROMILLIONS_PUBLIC_AUTO_REFRESH_MIN_INTERVAL_SECONDS", "1200"))

MAIN_RANGE = list(range(1, 51))
STAR_RANGE = list(range(1, 13))
UK_TICKET_COST_GBP = 2.50

# Exact EuroMillions combinatorics: choose 5 from 50 main numbers and 2 from 12 Lucky Stars.
TOTAL_COMBINATIONS = math.comb(50, 5) * math.comb(12, 2)
EUROMILLIONS_PRIZE_TIERS = [
    {"match": "5 + 2", "odds": 139_838_160, "avg_prize_gbp": 61_611_320.84},
    {"match": "5 + 1", "odds": 6_991_908, "avg_prize_gbp": 268_551.81},
    {"match": "5", "odds": 3_107_515, "avg_prize_gbp": 25_413.56},
    {"match": "4 + 2", "odds": 621_503, "avg_prize_gbp": 1_363.07},
    {"match": "4 + 1", "odds": 31_076, "avg_prize_gbp": 92.88},
    {"match": "3 + 2", "odds": 14_126, "avg_prize_gbp": 49.05},
    {"match": "4", "odds": 13_812, "avg_prize_gbp": 30.99},
    {"match": "2 + 2", "odds": 986, "avg_prize_gbp": 10.66},
    {"match": "3 + 1", "odds": 707, "avg_prize_gbp": 8.24},
    {"match": "3", "odds": 314, "avg_prize_gbp": 6.80},
    {"match": "1 + 2", "odds": 188, "avg_prize_gbp": 5.23},
    {"match": "2 + 1", "odds": 50, "avg_prize_gbp": 4.15},
    {"match": "2", "odds": 22, "avg_prize_gbp": 2.72},
]


def gbp(value: float) -> str:
    return f"£{value:,.2f}"


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


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def save_json_atomic(path: Path, payload: Dict[str, object]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_refresh_state() -> Dict[str, object]:
    ensure_base_dir()
    if not REFRESH_STATE_FILE.exists():
        return {}
    try:
        return json.loads(REFRESH_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_refresh_state(
    *,
    ok: bool,
    source: str,
    message: str,
    draws_added: int,
    latest_date: Optional[str],
) -> None:
    ensure_base_dir()
    now = utc_now_iso()
    state = load_refresh_state()

    state["last_attempt_at"] = now
    state["last_attempt_ok"] = ok
    state["last_attempt_source"] = source
    state["last_attempt_message"] = message
    state["last_attempt_draws_added"] = draws_added
    state["latest_date"] = latest_date

    if ok:
        state["last_success_at"] = now
        state["last_success_source"] = source
        state["last_success_message"] = message

    save_json_atomic(REFRESH_STATE_FILE, state)


def normalize_draw_numbers(df: pd.DataFrame) -> pd.DataFrame:
    """Sort main balls and Lucky Stars into canonical order for validation/deduping."""
    out = df.copy()
    ball_cols = [f"ball_{i}" for i in range(1, 6)]
    star_cols = ["lucky_star_1", "lucky_star_2"]

    for idx, row in out.iterrows():
        balls = sorted(int(row[c]) for c in ball_cols)
        stars = sorted(int(row[c]) for c in star_cols)
        for col, value in zip(ball_cols, balls):
            out.at[idx, col] = value
        for col, value in zip(star_cols, stars):
            out.at[idx, col] = value
    return out


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {c: c.strip().lower() for c in df.columns}
    df = df.rename(columns=rename_map).copy()

    required = [
        "draw_date",
        "ball_1",
        "ball_2",
        "ball_3",
        "ball_4",
        "ball_5",
        "lucky_star_1",
        "lucky_star_2",
    ]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")

    for optional_col in ["draw_number", "uk_millionaire_maker", "jackpot", "source"]:
        if optional_col not in df.columns:
            df[optional_col] = pd.NA if optional_col != "source" else "local"

    df["draw_date"] = pd.to_datetime(df["draw_date"], errors="coerce").dt.date

    num_cols = [
        "ball_1",
        "ball_2",
        "ball_3",
        "ball_4",
        "ball_5",
        "lucky_star_1",
        "lucky_star_2",
    ]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    df = df.dropna(subset=["draw_date"] + num_cols).copy()
    return normalize_draw_numbers(df)


def dedupe_history(df: pd.DataFrame) -> pd.DataFrame:
    keys = [
        "draw_date",
        "ball_1",
        "ball_2",
        "ball_3",
        "ball_4",
        "ball_5",
        "lucky_star_1",
        "lucky_star_2",
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
    logger.info("Saved local history CSV: %s | rows=%s", LOCAL_HISTORY, len(out))


def load_local_history() -> pd.DataFrame:
    ensure_base_dir()

    candidates = []
    if LOCAL_HISTORY.exists():
        candidates.append(LOCAL_HISTORY)
    if BASELINE_HISTORY.exists():
        candidates.append(BASELINE_HISTORY)
    if USER_ORIGINAL.exists():
        candidates.append(USER_ORIGINAL)

    frames: List[pd.DataFrame] = []
    for path in candidates:
        try:
            logger.info("Trying CSV source: %s", path)
            frames.append(standardize_columns(pd.read_csv(path)))
        except Exception as exc:
            logger.warning("Skipping invalid CSV source %s | reason=%s", path, exc)
            continue

    if not frames:
        raise FileNotFoundError("No usable EuroMillions CSV found in the project folder.")

    df = dedupe_history(pd.concat(frames, ignore_index=True))
    logger.info("Loaded local history | rows=%s | latest=%s", len(df), df["draw_date"].max())
    return df


def validate_draw_row(row: Dict[str, object]) -> bool:
    try:
        balls = [int(row[f"ball_{i}"]) for i in range(1, 6)]
        stars = [int(row["lucky_star_1"]), int(row["lucky_star_2"])]
    except Exception:
        return False

    if len(balls) != 5 or len(stars) != 2:
        return False
    if len(set(balls)) != 5:
        return False
    if len(set(stars)) != 2:
        return False
    if not all(1 <= x <= 50 for x in balls):
        return False
    if not all(1 <= x <= 12 for x in stars):
        return False

    return True


def parse_official_xml(text: str) -> pd.DataFrame:
    root = ET.fromstring(text)
    rows: List[Dict[str, object]] = []

    def local_name(tag_name: str) -> str:
        return tag_name.split("}")[-1].lower().replace("-", "_")

    def clean_text(value: Optional[str]) -> str:
        return value.strip() if value else ""

    # ogni <game> contiene un blocco draw + balls
    for game in root.iter():
        if local_name(game.tag) != "game":
            continue

        draw_elem = None
        balls_elem = None

        for child in list(game):
            child_tag = local_name(child.tag)
            if child_tag == "draw":
                draw_elem = child
            elif child_tag == "balls":
                balls_elem = child

        if draw_elem is None or balls_elem is None:
            continue

        draw_number = pd.NA
        draw_date = None
        jackpot = pd.NA
        uk_millionaire_maker = pd.NA

        for child in draw_elem:
            tag = local_name(child.tag)
            value = clean_text(child.text)

            if not value:
                continue

            if tag == "draw_number":
                draw_number = value
            elif tag == "draw_date":
                m = re.search(r"\d{4}-\d{2}-\d{2}", value)
                if m:
                    draw_date = m.group(0)
            elif tag in {"jackpot", "jackpot_amount", "jackpot_value"}:
                jackpot = value
            elif tag in {"uk_millionaire_maker", "ukmm_code", "millionaire_maker_code"}:
                uk_millionaire_maker = value

              # fallback jackpot scan across the whole <game> block
        if pd.isna(jackpot):
            for node in game.iter():
                node_tag = local_name(node.tag)
                node_value = clean_text(node.text)

                if not node_value:
                    continue

                if node_tag in {
                    "jackpot",
                    "jackpot_amount",
                    "jackpot_value",
                    "estimated_jackpot",
                    "prize_pool",
                }:
                    jackpot = node_value
                    break  

        if not draw_date:
            continue

        main_candidates: List[int] = []
        star_candidates: List[int] = []
        raffle_codes: List[str] = []

        for child in balls_elem:
            tag = local_name(child.tag)
            value = clean_text(child.text)

            if tag == "ball":
                if re.fullmatch(r"\d{1,2}", value):
                    n = int(value)
                    if 1 <= n <= 50:
                        main_candidates.append(n)

            elif tag == "bonus_ball":
                ball_type = (child.attrib.get("type") or "").strip().lower()
                if ball_type == "luckystar" and re.fullmatch(r"\d{1,2}", value):
                    n = int(value)
                    if 1 <= n <= 12:
                        star_candidates.append(n)

            elif tag == "raffles":
                for raffle_child in child:
                    raffle_tag = local_name(raffle_child.tag)
                    raffle_value = clean_text(raffle_child.text)
                    if raffle_tag == "raffle" and raffle_value:
                        raffle_codes.append(raffle_value)

        if len(main_candidates) != 5 or len(star_candidates) != 2:
            logger.warning(
                "Skipping XML draw: invalid parsed candidates | date=%s | mains=%s | stars=%s",
                draw_date, main_candidates, star_candidates
            )
            continue

        balls = sorted(main_candidates)
        stars = sorted(star_candidates)

        row: Dict[str, object] = {
            "draw_date": draw_date,
            "draw_number": draw_number,
            "jackpot": jackpot,
            "uk_millionaire_maker": ", ".join(raffle_codes) if raffle_codes else uk_millionaire_maker,
            "source": "official_xml",
        }

        for i, v in enumerate(balls, 1):
            row[f"ball_{i}"] = v
        row["lucky_star_1"] = stars[0]
        row["lucky_star_2"] = stars[1]

        if not validate_draw_row(row):
            logger.warning("Skipping XML draw: failed validation | row=%s", row)
            continue

        rows.append(row)

    if not rows:
        raise ValueError("No draw rows parsed from official XML.")

    parsed = standardize_columns(pd.DataFrame(rows))
    logger.info("Parsed official XML | rows=%s | latest=%s", len(parsed), parsed["draw_date"].max())
    return parsed


def fetch_official_xml(timeout: int = 20) -> pd.DataFrame:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml,text/xml,text/plain,*/*",
        "Referer": OFFICIAL_RESULTS_URL,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    logger.info("Fetching official XML: %s", OFFICIAL_XML_URL)
    resp = requests.get(OFFICIAL_XML_URL, headers=headers, timeout=timeout)
    logger.info("Official XML response status: %s", resp.status_code)
    resp.raise_for_status()

    text = resp.text.strip()
    if not text:
        raise ValueError("Official XML response is empty.")

    logger.info("Official XML first 1500 chars:\n%s", text[:1500])

    return parse_official_xml(text)


def _extract_json_array(script_text: str, key_patterns: List[str]) -> Optional[List[int]]:
    for key in key_patterns:
        pattern = rf'"{key}"\s*:\s*\[(.*?)\]'
        match = re.search(pattern, script_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            nums = [int(x) for x in re.findall(r"\d{1,2}", match.group(1))]
            if nums:
                return nums
    return None


def parse_official_html_backup(text: str) -> pd.DataFrame:
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", text, flags=re.IGNORECASE | re.DOTALL)
    candidates = scripts + [text]

    for chunk in candidates:
        date_match = (
            re.search(r'"drawDate"\s*:\s*"([^"]+)"', chunk, flags=re.IGNORECASE)
            or re.search(r'"date"\s*:\s*"([^"]+)"', chunk, flags=re.IGNORECASE)
            or re.search(r"(\d{4}-\d{2}-\d{2})", chunk)
        )

        main_nums = _extract_json_array(
            chunk,
            ["mainNumbers", "main_numbers", "drawnNumbers", "numbers", "balls"],
        )
        star_nums = _extract_json_array(
            chunk,
            ["luckyStars", "lucky_stars", "starNumbers", "stars"],
        )

        if not date_match or not main_nums or not star_nums:
            continue

        draw_date_raw = date_match.group(1)
        parsed_date = pd.to_datetime(draw_date_raw, errors="coerce")
        if pd.isna(parsed_date):
            continue

        main_nums = [int(x) for x in main_nums if 1 <= int(x) <= 50]
        star_nums = [int(x) for x in star_nums if 1 <= int(x) <= 12]

        dedup_main = []
        seen_main = set()
        for x in main_nums:
            if x not in seen_main:
                seen_main.add(x)
                dedup_main.append(x)

        dedup_stars = []
        seen_stars = set()
        for x in star_nums:
            if x not in seen_stars:
                seen_stars.add(x)
                dedup_stars.append(x)

        if len(dedup_main) < 5 or len(dedup_stars) < 2:
            continue

        balls = sorted(dedup_main[:5])
        stars = sorted(dedup_stars[:2])

        row = {
            "draw_date": parsed_date.date().isoformat(),
            "ball_1": balls[0],
            "ball_2": balls[1],
            "ball_3": balls[2],
            "ball_4": balls[3],
            "ball_5": balls[4],
            "lucky_star_1": stars[0],
            "lucky_star_2": stars[1],
            "source": "official_html_backup",
        }

        if not validate_draw_row(row):
            logger.warning("Skipping HTML backup row: failed validation | row=%s", row)
            continue

        parsed = standardize_columns(pd.DataFrame([row]))
        logger.info("Parsed HTML backup | latest=%s", parsed["draw_date"].max())
        return parsed

    raise ValueError("HTML backup parser could not confidently extract the latest draw.")


def fetch_official_html_backup(timeout: int = 20) -> pd.DataFrame:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": OFFICIAL_RESULTS_URL,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    logger.info("Fetching official HTML backup: %s", OFFICIAL_RESULTS_URL)
    resp = requests.get(OFFICIAL_RESULTS_URL, headers=headers, timeout=timeout)
    logger.info("Official HTML backup response status: %s", resp.status_code)
    resp.raise_for_status()

    text = resp.text.strip()
    if not text:
        raise ValueError("Official HTML backup response is empty.")

    return parse_official_html_backup(text)



def backfill_slug(draw_date: dt.date) -> str:
    return (
        f"{draw_date.strftime('%A').lower()}-"
        f"{draw_date.day:02d}-"
        f"{draw_date.strftime('%B').lower()}-"
        f"{draw_date.year}"
    )


def parse_backfill_html(draw_date: dt.date, text: str) -> Dict[str, object]:
    match = re.search(r'<ul class="balls" id="ballsCell">(.*?)</ul>', text, flags=re.S | re.I)
    if not match:
        raise ValueError(f"No backfill ball list found for {draw_date}.")

    balls_block = match.group(1)
    balls = [
        int(v)
        for v in re.findall(
            r'<li[^>]*class="[^"]*\beuromillions\b[^"]*\bball\b[^"]*\bball\b[^"]*"[^>]*>\s*(\d{1,2})\s*</li>',
            balls_block,
            flags=re.I,
        )
    ]
    stars = [
        int(v)
        for v in re.findall(
            r'<li[^>]*class="[^"]*\blucky-star\b[^"]*"[^>]*>\s*(\d{1,2})\s*</li>',
            balls_block,
            flags=re.I,
        )
    ]
    if len(balls) != 5 or len(stars) != 2:
        raise ValueError(f"Incomplete backfill draw parsed for {draw_date}.")

    def clean_text(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return html.unescape(re.sub(r"\s+", " ", value)).strip()

    draw_number = None
    draw_match = re.search(r"Draw Number:\s*<br>\s*<strong>([^<]+)</strong>", text, flags=re.S | re.I)
    if draw_match:
        draw_number = re.sub(r"\D", "", draw_match.group(1)) or None

    jackpot = None
    jackpot_match = re.search(r'Jackpot for this draw:\s*<span[^>]*>([^<]+)</span>', text, flags=re.S | re.I)
    if jackpot_match:
        jackpot = clean_text(jackpot_match.group(1))

    raffle = None
    raffle_match = re.search(r'<span class="raffleCode larger">([^<]+)</span>', text, flags=re.S | re.I)
    if raffle_match:
        raffle = clean_text(raffle_match.group(1))

    row: Dict[str, object] = {
        "source": "national_lottery_com_backfill",
        "draw_date": draw_date.isoformat(),
        "draw_number": draw_number,
        "jackpot": jackpot,
        "uk_millionaire_maker": raffle,
        "ball_1": balls[0],
        "ball_2": balls[1],
        "ball_3": balls[2],
        "ball_4": balls[3],
        "ball_5": balls[4],
        "lucky_star_1": stars[0],
        "lucky_star_2": stars[1],
    }
    if not validate_draw_row(row):
        raise ValueError(f"Invalid backfill draw parsed for {draw_date}.")
    return row


def fetch_backfill_draw(draw_date: dt.date, timeout: int = BACKFILL_FETCH_TIMEOUT) -> Dict[str, object]:
    url = BACKFILL_RESULTS_URL.format(slug=backfill_slug(draw_date))
    logger.info("Fetching National Lottery backfill draw: %s", url)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    logger.info("Backfill response status for %s: %s", draw_date, resp.status_code)
    resp.raise_for_status()
    return parse_backfill_html(draw_date, resp.text)


def fetch_missing_backfill(df: pd.DataFrame, latest_date: dt.date) -> Tuple[pd.DataFrame, int, List[str]]:
    if df.empty:
        return pd.DataFrame(), 0, []

    existing = set(pd.to_datetime(df["draw_date"], errors="coerce").dt.date.dropna())
    start_date = max(min(existing), latest_date - dt.timedelta(days=180))
    missing = [d for d in expected_draw_dates(start_date, latest_date) if d not in existing]
    if not missing:
        return pd.DataFrame(), 0, []

    rows: List[Dict[str, object]] = []
    errors: List[str] = []
    for draw_date in missing[:BACKFILL_MAX_DRAWS]:
        try:
            rows.append(fetch_backfill_draw(draw_date))
        except Exception as exc:
            logger.warning("Backfill skipped for %s | reason=%s", draw_date, exc)
            errors.append(f"{draw_date}: {exc}")

    if not rows:
        return pd.DataFrame(), 0, errors
    return standardize_columns(pd.DataFrame(rows)), len(rows), errors


def fetch_recent_quick_backfill(df: pd.DataFrame, latest_date: dt.date) -> Tuple[pd.DataFrame, int, List[str]]:
    if QUICK_BACKFILL_MAX_DRAWS <= 0 or df.empty:
        return pd.DataFrame(), 0, []

    existing = set(pd.to_datetime(df["draw_date"], errors="coerce").dt.date.dropna())
    start_date = max(existing) if existing else latest_date
    if latest_date - start_date > dt.timedelta(days=QUICK_BACKFILL_LOOKBACK_DAYS):
        logger.info(
            "Skipping quick backfill: gap from local history to official latest is too large | local=%s official=%s",
            start_date,
            latest_date,
        )
        return pd.DataFrame(), 0, []
    missing = [d for d in expected_draw_dates(start_date, latest_date) if d not in existing and d != latest_date]
    if not missing:
        return pd.DataFrame(), 0, []

    rows: List[Dict[str, object]] = []
    errors: List[str] = []
    for draw_date in missing[:QUICK_BACKFILL_MAX_DRAWS]:
        try:
            rows.append(fetch_backfill_draw(draw_date, timeout=QUICK_BACKFILL_FETCH_TIMEOUT))
        except Exception as exc:
            logger.warning("Quick backfill skipped for %s | reason=%s", draw_date, exc)
            errors.append(f"{draw_date}: {exc}")

    if not rows:
        return pd.DataFrame(), 0, errors
    return standardize_columns(pd.DataFrame(rows)), len(rows), errors


def expected_draw_dates(start: dt.date, end: dt.date) -> List[dt.date]:
    # EuroMillions started weekly on Fridays, then added Tuesdays in May 2011.
    tuesday_start = dt.date(2011, 5, 10)
    out: List[dt.date] = []
    cur = start
    while cur <= end:
        if cur.weekday() == 4 or (cur >= tuesday_start and cur.weekday() == 1):
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def history_quality_report(df: pd.DataFrame) -> Dict[str, object]:
    if df.empty:
        return {"ok": False, "missing_recent_count": 0, "missing_recent_dates": [], "notes": ["History is empty."]}

    hist_dates = set(pd.to_datetime(df["draw_date"], errors="coerce").dt.date.dropna().tolist())
    start = max(min(hist_dates), dt.date.today() - dt.timedelta(days=180))
    end = max(hist_dates)
    expected = expected_draw_dates(start, end)
    missing = [d.isoformat() for d in expected if d not in hist_dates]

    duplicate_dates = int(pd.to_datetime(df["draw_date"], errors="coerce").dt.date.duplicated().sum())
    invalid_rows = 0
    for _, row in df.iterrows():
        if not validate_draw_row(row.to_dict()):
            invalid_rows += 1

    notes = []
    if missing:
        notes.append(f"Missing {len(missing)} expected Tue/Fri draw date(s) in the last 180 days.")
    if duplicate_dates:
        notes.append(f"Found {duplicate_dates} duplicate draw date(s).")
    if invalid_rows:
        notes.append(f"Found {invalid_rows} invalid draw row(s).")
    if not notes:
        notes.append("Recent history completeness check passed.")

    return {
        "ok": not missing and duplicate_dates == 0 and invalid_rows == 0,
        "missing_recent_count": len(missing),
        "missing_recent_dates": missing[:20],
        "duplicate_dates": duplicate_dates,
        "invalid_rows": invalid_rows,
        "notes": notes,
    }

def refresh_history(allow_backfill: bool = True, persist: bool = True) -> Tuple[pd.DataFrame, RefreshResult]:
    df = load_local_history()
    before = len(df)
    sources: List[str] = []
    warnings: List[str] = []
    backfilled = 0
    quick_backfilled = 0

    try:
        official = fetch_official_xml()
        if official.empty:
            raise ValueError("Official XML returned no valid draws.")
        latest_official = pd.to_datetime(official["draw_date"], errors="coerce").dt.date.max()
        if latest_official and allow_backfill:
            backfill_df, backfilled, backfill_errors = fetch_missing_backfill(df, latest_official)
            if not backfill_df.empty:
                df = dedupe_history(pd.concat([df, backfill_df], ignore_index=True))
                sources.append("national_lottery_com_backfill")
            if backfill_errors and not backfilled:
                warnings.append(
                    "Official XML currently exposes only the latest draw; historical backfill was unavailable."
                )
        elif latest_official:
            quick_df, quick_backfilled, quick_errors = fetch_recent_quick_backfill(df, latest_official)
            if not quick_df.empty:
                df = dedupe_history(pd.concat([df, quick_df], ignore_index=True))
                sources.append("national_lottery_com_quick_backfill")
            if quick_errors and not quick_backfilled:
                warnings.append("Recent quick backfill was unavailable.")
        df = dedupe_history(pd.concat([df, official], ignore_index=True))
        sources.append("official_xml" if allow_backfill else "official_xml_quick")
    except Exception as xml_exc:
        logger.exception("Official XML refresh failed")
        warnings.append(f"Official XML failed: {xml_exc}")

        if allow_backfill:
            try:
                html_backup = fetch_official_html_backup()
                if html_backup.empty:
                    raise ValueError("Official HTML backup returned no valid draws.")
                df = dedupe_history(pd.concat([df, html_backup], ignore_index=True))
                sources.append("official_html_backup")
            except Exception as html_exc:
                logger.exception("Official HTML backup refresh failed")
                warnings.append(f"HTML backup failed: {html_exc}")

    if persist:
        persist_history(df)
    added = max(0, len(df) - before)
    latest_date = str(df["draw_date"].max()) if not df.empty else None
    quality = history_quality_report(df)

    ok = bool(sources) and bool(quality.get("ok", False))
    if sources:
        source = "+".join(sources)
        message = "Refresh complete."
        if backfilled:
            message += f" Backfilled {backfilled} missing draw(s) between local cache and latest official XML draw."
        if quick_backfilled:
            message += f" Quick-backfilled {quick_backfilled} recent missing draw(s)."
    else:
        source = "local_cache"
        message = "Official sources unavailable. Using local cache."

    if warnings:
        message += " Warnings: " + " | ".join(warnings[:2])
    if not quality.get("ok", False):
        message += " Data quality warning: " + " ".join(str(x) for x in quality.get("notes", []))

    result = RefreshResult(
        source=source,
        ok=ok,
        message=message,
        draws_added=added,
        latest_date=latest_date,
    )
    if persist:
        save_refresh_state(
            ok=result.ok,
            source=result.source,
            message=result.message,
            draws_added=result.draws_added,
            latest_date=result.latest_date,
        )
    return df, result


def refresh_to_dict(refresh: RefreshResult) -> Dict[str, object]:
    return asdict(refresh)


def refresh_from_dict(raw: Dict[str, object]) -> RefreshResult:
    return RefreshResult(
        source=str(raw.get("source", "local_cache")),
        ok=bool(raw.get("ok", False)),
        message=str(raw.get("message", "Loaded cached dashboard data.")),
        draws_added=int(raw.get("draws_added", 0) or 0),
        latest_date=raw.get("latest_date") if raw.get("latest_date") is not None else None,
    )


def local_refresh_result(df: pd.DataFrame, message: str = "Loaded local EuroMillions history without online refresh.") -> RefreshResult:
    latest = str(df["draw_date"].max()) if not df.empty and "draw_date" in df.columns else None
    return RefreshResult(
        source="local_cache",
        ok=True,
        message=message,
        draws_added=0,
        latest_date=latest,
    )


def save_dashboard_cache(data: Dict[str, object], refresh: RefreshResult, premium_line_count: int = 5) -> None:
    ensure_base_dir()
    payload: Dict[str, object] = {
        "generated_at": utc_now_iso(),
        "premium_line_count": int(premium_line_count),
        "data": data,
        "refresh": refresh_to_dict(refresh),
    }
    save_json_atomic(DASHBOARD_CACHE, payload)


def parse_utc_timestamp(value: object) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def latest_local_history_end() -> Optional[str]:
    try:
        df = load_local_history()
        if df.empty or "draw_date" not in df.columns:
            return None
        latest = pd.to_datetime(df["draw_date"], errors="coerce").max()
        if pd.isna(latest):
            return None
        return latest.date().isoformat()
    except Exception:
        logger.exception("Local history check failed while validating dashboard cache")
        return None


def local_history_newer_than_cache(generated_at: object) -> bool:
    generated = parse_utc_timestamp(generated_at)
    if generated is None:
        return True

    local_paths = [path for path in [LOCAL_HISTORY, USER_ORIGINAL] if path.exists()]
    if not local_paths:
        return False

    newest_mtime = max(dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc) for path in local_paths)
    return newest_mtime > generated


def dashboard_cache_expired(generated_at: object) -> bool:
    if CACHE_MAX_AGE_SECONDS <= 0:
        return False
    generated = parse_utc_timestamp(generated_at)
    if generated is None:
        return True
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - generated
    return age.total_seconds() > CACHE_MAX_AGE_SECONDS


def load_dashboard_cache(
    premium_line_count: int = 5,
    allow_stale: bool = False,
) -> Optional[Tuple[Dict[str, object], RefreshResult, str]]:
    if not DASHBOARD_CACHE.exists():
        return None
    try:
        payload = json.loads(DASHBOARD_CACHE.read_text(encoding="utf-8"))
        data = payload.get("data")
        refresh = payload.get("refresh")
        generated_at = str(payload.get("generated_at", ""))
        cached_lines = int(payload.get("premium_line_count", premium_line_count) or premium_line_count)
        if cached_lines != int(premium_line_count):
            return None
        if not isinstance(data, dict) or not isinstance(refresh, dict):
            return None
        if not data.get("target_seed"):
            logger.info("Ignoring stale dashboard cache: missing deterministic target seed")
            return None
        if not allow_stale and dashboard_cache_expired(generated_at):
            logger.info("Ignoring stale dashboard cache: generated_at=%s exceeded max age", generated_at)
            return None

        if local_history_newer_than_cache(generated_at):
            logger.info("Ignoring stale dashboard cache: local history file is newer than cache")
            return None

        return data, refresh_from_dict(refresh), generated_at
    except Exception:
        logger.exception("Dashboard cache load failed")
        return None


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


def get_hot_numbers_last_n(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    recent = df.tail(n)
    counts = {num: 0 for num in MAIN_RANGE}
    for _, row in recent.iterrows():
        for i in range(1, 6):
            counts[int(row[f"ball_{i}"])] += 1

    rows = [{"number": k, "seen_last_n": v} for k, v in counts.items()]
    out = pd.DataFrame(rows).sort_values(["seen_last_n", "number"], ascending=[False, True]).reset_index(drop=True)
    return out.head(10)


def get_overdue_numbers(df: pd.DataFrame) -> pd.DataFrame:
    rank = build_rank_table(df, MAIN_RANGE, [f"ball_{i}" for i in range(1, 6)], "main")
    return rank.sort_values(["draws_since_seen", "number"], ascending=[False, True]).head(10)[
        ["number", "draws_since_seen", "times_seen"]
    ]


def get_top_pairs(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    pair_counts: Dict[Tuple[int, int], int] = {}
    for _, row in df.iterrows():
        balls = sorted(int(row[f"ball_{i}"]) for i in range(1, 6))
        for pair in combinations(balls, 2):
            pair_counts[pair] = pair_counts.get(pair, 0) + 1

    rows = [{"pair": f"{a:02d} {b:02d}", "count": count} for (a, b), count in pair_counts.items()]
    out = pd.DataFrame(rows).sort_values(["count", "pair"], ascending=[False, True]).reset_index(drop=True)
    return out.head(top_n)


def simple_bar_chart_html(
    rows: List[Dict[str, object]],
    label_key: str,
    value_key: str,
    title: str,
    unit: str = "",
) -> str:
    if not rows:
        return "<div>No data</div>"

    max_value = max(float(row[value_key]) for row in rows) or 1.0
    parts = [f'<div class="mini-chart-title">{html.escape(title)}</div>']
    for row in rows:
        label = str(row[label_key])
        value = float(row[value_key])
        width = max(6, int((value / max_value) * 100))
        parts.append(
            f'''
            <div class="bar-row">
              <div class="bar-label">{html.escape(label)}</div>
              <div class="bar-track">
                <div class="bar-fill" style="width:{width}%"></div>
              </div>
              <div class="bar-value">{html.escape(str(int(value) if value.is_integer() else round(value, 2)))}{html.escape(unit)}</div>
            </div>
            '''
        )
    return "".join(parts)


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


def prize_tier_ways(main_matches: int, star_matches: int) -> int:
    return (
        math.comb(5, main_matches)
        * math.comb(45, 5 - main_matches)
        * math.comb(2, star_matches)
        * math.comb(10, 2 - star_matches)
    )


def exact_any_prize_probability() -> float:
    winning_match_pairs = [
        (5, 2), (5, 1), (5, 0), (4, 2), (4, 1), (3, 2), (4, 0),
        (2, 2), (3, 1), (3, 0), (1, 2), (2, 1), (2, 0),
    ]
    return sum(prize_tier_ways(main, stars) for main, stars in winning_match_pairs) / TOTAL_COMBINATIONS


def pack_jackpot_probability(line_count: int) -> Dict[str, object]:
    line_count = max(1, int(line_count))
    jackpot_p = 1.0 - ((TOTAL_COMBINATIONS - 1) / TOTAL_COMBINATIONS) ** line_count
    any_prize_single_p = exact_any_prize_probability()
    any_prize_pack_p = 1.0 - ((1.0 - any_prize_single_p) ** line_count)
    gross_expected_prize = sum(float(t["avg_prize_gbp"]) / float(t["odds"]) for t in EUROMILLIONS_PRIZE_TIERS) * line_count
    estimated_cost = UK_TICKET_COST_GBP * line_count
    return {
        "lines": line_count,
        "jackpot_odds_text": f"1 in {TOTAL_COMBINATIONS:,}" if line_count == 1 else f"about 1 in {round(1 / jackpot_p):,}",
        "jackpot_probability_pct": round(jackpot_p * 100, 9),
        "any_prize_odds_text": f"about 1 in {round(1 / any_prize_pack_p, 1)} for this pack",
        "any_prize_probability_pct": round(any_prize_pack_p * 100, 6),
        "any_prize_single_line_odds_text": f"about 1 in {round(1 / any_prize_single_p, 2)} per line",
        "estimated_cost_gbp": round(estimated_cost, 2),
        "estimated_cost_text": gbp(estimated_cost),
        "gross_expected_prize_gbp": round(gross_expected_prize, 2),
        "gross_expected_prize_text": gbp(gross_expected_prize),
        "expected_loss_warning": "Lottery expected value is normally negative after ticket cost; treat play as entertainment, not income.",
        "truth": "Every valid EuroMillions line has the same jackpot probability; the engine can only improve data quality, diversification, and payout-sharing risk.",
    }


def budget_strategy(line_count: int) -> Dict[str, object]:
    line_count = max(1, int(line_count))
    cost = line_count * UK_TICKET_COST_GBP
    return {
        "selected_lines": line_count,
        "cost_per_draw_gbp": round(cost, 2),
        "cost_per_draw_text": gbp(cost),
        "monthly_if_two_draws_per_week_gbp": round(cost * 8.7, 2),
        "monthly_if_two_draws_per_week_text": gbp(cost * 8.7),
        "best_practice": [
            "Set a fixed monthly cap before playing.",
            "Prefer 3-5 diversified lines over chasing many similar lines.",
            "Never increase spend after losses; each draw is independent.",
            "If playing with family/friends, write the split agreement before buying.",
        ],
    }


def line_pack_diversity_report(suggested: pd.DataFrame) -> Dict[str, object]:
    if suggested.empty:
        return {"ok": False, "message": "No suggested lines available."}

    parsed = [set(parse_line_numbers(row["balls"])) for _, row in suggested.iterrows()]
    overlaps = []
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            overlaps.append(len(parsed[i] & parsed[j]))

    max_overlap = max(overlaps) if overlaps else 0
    avg_overlap = round(sum(overlaps) / len(overlaps), 2) if overlaps else 0.0
    all_numbers = sorted(set().union(*parsed)) if parsed else []
    return {
        "ok": max_overlap <= 3,
        "unique_main_numbers": len(all_numbers),
        "max_pair_overlap": max_overlap,
        "average_pair_overlap": avg_overlap,
        "message": "Good diversification" if max_overlap <= 3 else "Some lines overlap heavily; consider fewer/more diversified lines.",
    }


def parse_line_numbers(text_value: object) -> List[int]:
    return [int(x) for x in str(text_value).replace(",", " ").split() if str(x).strip()]


def popularity_risk_score(balls: Sequence[int], stars: Sequence[int]) -> float:
    """Estimate how likely a line is to be shared with many human players.

    It does not change draw probability. It favours lines that avoid birthdays,
    obvious sequences, rows/columns, and very low sums because those are common
    human choices and can split prizes if they land.
    """
    balls = sorted(int(x) for x in balls)
    stars = sorted(int(x) for x in stars)
    risk = 0.0

    birthday_count = sum(1 for n in balls if n <= 31)
    if birthday_count >= 4:
        risk += 22 + (birthday_count - 4) * 10
    if max(balls) <= 31:
        risk += 28
    if sum(balls) < 95:
        risk += 18
    if sum(balls) > 185:
        risk += 8

    consecutive_pairs = sum(1 for a, b in zip(balls, balls[1:]) if b == a + 1)
    risk += consecutive_pairs * 13

    gaps = [b - a for a, b in zip(balls, balls[1:])]
    if len(set(gaps)) == 1:
        risk += 36
    if balls in ([1, 2, 3, 4, 5], [5, 10, 15, 20, 25], [10, 20, 30, 40, 50]):
        risk += 50

    same_decade_max = max(sum(1 for n in balls if lo <= n <= lo + 9) for lo in [1, 11, 21, 31, 41])
    if same_decade_max >= 4:
        risk += 16

    if stars == [1, 2]:
        risk += 22
    elif all(s <= 6 for s in stars):
        risk += 8

    return round(min(100.0, risk), 3)


def statistical_shape_score(balls: Sequence[int], hist_sum_mean: float, hist_sum_std: float) -> float:
    balls = sorted(int(x) for x in balls)
    odd = sum(n % 2 for n in balls)
    low = sum(n <= 25 for n in balls)
    total_sum = sum(balls)
    z = abs((total_sum - hist_sum_mean) / hist_sum_std) if hist_sum_std else 0.0
    sum_score = max(0.0, 30.0 - (z * 10.0))
    balance_score = 22.0 - (abs(odd - 2.5) * 4.0) - (abs(low - 2.5) * 4.0)
    spread = max(balls) - min(balls)
    spread_score = 18.0 if 24 <= spread <= 45 else 10.0 if spread >= 18 else 3.0
    return round(max(0.0, sum_score + balance_score + spread_score), 3)


def historical_signal_score(
    balls: Sequence[int],
    stars: Sequence[int],
    main_rank: object,
    star_rank: object,
) -> float:
    # This function is called thousands of times while building the dashboard.
    # Avoid rebuilding pandas indexes inside the hot loop; callers may pass
    # precomputed dict lookups. Keep DataFrame support for compatibility.
    main_lookup = main_rank if isinstance(main_rank, dict) else main_rank.set_index("number")["score"].to_dict()
    star_lookup = star_rank if isinstance(star_rank, dict) else star_rank.set_index("number")["score"].to_dict()
    raw = sum(main_lookup.get(int(n), 0.0) for n in balls) + sum(star_lookup.get(int(s), 0.0) for s in stars)
    # History is a weak signal in a fair lottery, so cap its influence.
    return round(min(45.0, raw / 2.6), 3)


def ticket_quality_score(
    balls: Sequence[int],
    stars: Sequence[int],
    main_rank: object,
    star_rank: object,
    hist_sum_mean: float,
    hist_sum_std: float,
    mode: str,
) -> Tuple[float, float, float, float]:
    shape = statistical_shape_score(balls, hist_sum_mean, hist_sum_std)
    history = historical_signal_score(balls, stars, main_rank, star_rank)
    popularity_risk = popularity_risk_score(balls, stars)
    value_score = max(0.0, 100.0 - popularity_risk)

    if mode == "value":
        total = (shape * 0.42) + (value_score * 0.48) + (history * 0.10)
    elif mode == "balanced":
        total = (shape * 0.45) + (value_score * 0.30) + (history * 0.25)
    elif mode == "anti_last_draw":
        total = (shape * 0.50) + (value_score * 0.38) + (history * 0.12)
    else:  # coverage/random-professional line
        total = (shape * 0.55) + (value_score * 0.35) + (history * 0.10)

    return round(total, 3), round(value_score, 3), round(shape, 3), round(history, 3)


def line_score(
    balls: Sequence[int],
    stars: Sequence[int],
    main_rank: object,
    star_rank: object,
    hist_sum_mean: float,
    hist_sum_std: float,
) -> float:
    main_lookup = main_rank if isinstance(main_rank, dict) else main_rank.set_index("number")["score"].to_dict()
    star_lookup = star_rank if isinstance(star_rank, dict) else star_rank.set_index("number")["score"].to_dict()
    base = sum(main_lookup.get(n, 0.0) for n in balls) + sum(star_lookup.get(s, 0.0) for s in stars)

    total_sum = sum(balls)
    z = abs((total_sum - hist_sum_mean) / hist_sum_std) if hist_sum_std else 0.0
    sum_bonus = max(0.0, 18.0 - (z * 8.0))

    spread = max(balls) - min(balls)
    spread_bonus = 10.0 if spread >= 18 else 3.0

    consecutive_pairs = sum(1 for a, b in zip(sorted(balls), sorted(balls)[1:]) if b == a + 1)
    consecutive_penalty = consecutive_pairs * 4.5

    overlap_bonus = len(set(balls)) * 0.0
    return round(base + sum_bonus + spread_bonus + overlap_bonus - consecutive_penalty, 3)


def generate_suggested_lines(df: pd.DataFrame, lines_per_mode: int = 4, seed: Optional[int] = 42) -> pd.DataFrame:
    main_rank = build_rank_table(df, MAIN_RANGE, [f"ball_{i}" for i in range(1, 6)], "main")
    star_rank = build_rank_table(df, STAR_RANGE, ["lucky_star_1", "lucky_star_2"], "star")

    hist_sum_mean = float(df["sum_balls"].mean())
    hist_sum_std = float(df["sum_balls"].std(ddof=0) or 1.0)

    rng = random.Random(seed) if seed is not None else secrets.SystemRandom()
    main_weights = {row["number"]: float(row["score"]) for _, row in main_rank.iterrows()}
    star_weights = {row["number"]: float(row["score"]) for _, row in star_rank.iterrows()}
    main_score_lookup = main_rank.set_index("number")["score"].to_dict()
    star_score_lookup = star_rank.set_index("number")["score"].to_dict()

    modes = {
        "value": {"top_main": 50, "top_star": 12, "jitter": 0.80, "history_weight": 0.18},
        "balanced": {"top_main": 50, "top_star": 12, "jitter": 0.45, "history_weight": 0.45},
        "coverage": {"top_main": 50, "top_star": 12, "jitter": 1.00, "history_weight": 0.05},
        "anti_last_draw": {"top_main": 50, "top_star": 12, "jitter": 0.55, "history_weight": 0.20},
    }

    last_row = df.iloc[-1]
    last_balls = {int(last_row[f"ball_{i}"]) for i in range(1, 6)}
    last_stars = {int(last_row["lucky_star_1"]), int(last_row["lucky_star_2"])}

    rows: List[Dict[str, object]] = []
    used = set()

    for mode, cfg in modes.items():
        tries = 0
        made = 0

        candidates: List[Dict[str, object]] = []
        main_pool = main_rank["number"].tolist()[:cfg["top_main"]]
        star_pool = star_rank["number"].tolist()[:cfg["top_star"]]

        while tries < 4500:
            tries += 1

            if mode in {"value", "coverage"}:
                mw = [max(0.001, (1.0 + main_weights[n] * cfg["history_weight"]) * (1.0 + rng.uniform(-cfg["jitter"], cfg["jitter"]))) for n in main_pool]
                sw = [max(0.001, (1.0 + star_weights[s] * cfg["history_weight"]) * (1.0 + rng.uniform(-cfg["jitter"], cfg["jitter"]))) for s in star_pool]
            else:
                mw = [max(0.001, main_weights[n] * (1.0 + rng.uniform(-cfg["jitter"], cfg["jitter"]))) for n in main_pool]
                sw = [max(0.001, star_weights[s] * (1.0 + rng.uniform(-cfg["jitter"], cfg["jitter"]))) for s in star_pool]

            balls = sorted(weighted_sample_without_replacement(main_pool, mw, 5, rng))
            stars = sorted(weighted_sample_without_replacement(star_pool, sw, 2, rng))

            if mode == "anti_last_draw":
                overlap_balls = len(set(balls) & last_balls)
                overlap_stars = len(set(stars) & last_stars)
                if overlap_balls > 1 or overlap_stars > 0:
                    continue

            odd = sum(n % 2 for n in balls)
            low = sum(n <= 25 for n in balls)
            if abs(odd - 2.5) > 2 or abs(low - 2.5) > 2:
                continue

            key = tuple([mode] + balls + [-1] + stars)
            if key in used:
                continue

            score, value_score, shape_score, history_score = ticket_quality_score(
                balls,
                stars,
                main_score_lookup,
                star_score_lookup,
                hist_sum_mean,
                hist_sum_std,
                mode,
            )

            candidates.append({
                "mode": mode,
                "balls": " ".join(f"{x:02d}" for x in balls),
                "stars": " ".join(f"{x:02d}" for x in stars),
                "sum_balls": sum(balls),
                "odd_even": f"{odd}-{5 - odd}",
                "low_high": f"{low}-{5 - low}",
                "score": score,
                "value_score": value_score,
                "shape_score": shape_score,
                "history_signal": history_score,
                "popularity_risk": popularity_risk_score(balls, stars),
            })
            used.add(key)

        for candidate in sorted(candidates, key=lambda x: float(x["score"]), reverse=True):
            rows.append(candidate)
            made += 1
            if made >= lines_per_mode:
                break

    out = pd.DataFrame(rows).sort_values(["mode", "score"], ascending=[True, False]).reset_index(drop=True)
    mode_order = pd.CategoricalDtype(
        categories=["value", "balanced", "coverage", "anti_last_draw"],
        ordered=True
    )
    out["mode"] = out["mode"].astype(mode_order)
    out = out.sort_values(["mode", "score"], ascending=[True, False]).reset_index(drop=True)
    out["mode"] = out["mode"].astype(str)
    return out


def target_line_seed(df: pd.DataFrame, total_lines: int) -> int:
    latest = df.iloc[-1]
    latest_date = latest["draw_date"].date().isoformat() if hasattr(latest["draw_date"], "date") else str(latest["draw_date"])
    draw_number = "" if pd.isna(latest.get("draw_number")) else str(latest.get("draw_number"))
    balls = "-".join(str(int(latest[f"ball_{i}"])) for i in range(1, 6))
    stars = f"{int(latest['lucky_star_1'])}-{int(latest['lucky_star_2'])}"
    seed_material = f"{latest_date}|{draw_number}|{balls}|{stars}|{int(total_lines)}"
    digest = hashlib.sha256(seed_material.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def generate_premium_line_pack(df: pd.DataFrame, total_lines: int = 5) -> pd.DataFrame:
    hist = enrich_history(df)
    base = generate_suggested_lines(
        hist,
        lines_per_mode=max(3, total_lines),
        seed=target_line_seed(hist, total_lines),
    )

    target_order = ["value", "balanced", "coverage", "anti_last_draw"]
    selected_rows: List[Dict[str, object]] = []
    used_balls_sets = []

    for mode in target_order:
        mode_rows = base[base["mode"] == mode].sort_values("score", ascending=False)
        for _, row in mode_rows.iterrows():
            balls_tuple = tuple(row["balls"].split())
            similarity_ok = True
            for prev in used_balls_sets:
                overlap = len(set(balls_tuple) & set(prev))
                if overlap >= 3:
                    similarity_ok = False
                    break
            if not similarity_ok:
                continue

            selected_rows.append(row.to_dict())
            used_balls_sets.append(balls_tuple)

            if len(selected_rows) >= total_lines:
                return pd.DataFrame(selected_rows)

    return pd.DataFrame(selected_rows).head(total_lines)


def choose_best_line(suggested: pd.DataFrame) -> Tuple[Dict[str, object], BestLineDecision]:
    if suggested.empty:
        raise ValueError("No suggested lines generated.")

    value = suggested[suggested["mode"] == "value"].sort_values("score", ascending=False)
    balanced = suggested[suggested["mode"] == "balanced"].sort_values("score", ascending=False)
    coverage = suggested[suggested["mode"] == "coverage"].sort_values("score", ascending=False)
    anti = suggested[suggested["mode"] == "anti_last_draw"].sort_values("score", ascending=False)

    if not value.empty:
        row = value.iloc[0].to_dict()
        return row, BestLineDecision(
            mode="value",
            reason="Chosen as the best realistic-value line: normal statistical shape, diversified numbers, and lower risk of sharing a prize with common human picks.",
        )

    if not balanced.empty:
        row = balanced.iloc[0].to_dict()
        return row, BestLineDecision(
            mode="balanced",
            reason="Chosen because balanced lines mix data signals, realistic spread, and lower popularity risk without pretending to predict randomness.",
        )

    if not coverage.empty:
        row = coverage.iloc[0].to_dict()
        return row, BestLineDecision(
            mode="coverage",
            reason="Chosen because no value/balanced line was available, so the model selected a broad-coverage line.",
        )

    if not anti.empty:
        row = anti.iloc[0].to_dict()
        return row, BestLineDecision(
            mode="anti_last_draw",
            reason="Chosen to reduce overlap with the most recent draw while keeping a strong score.",
        )

    row = suggested.sort_values("score", ascending=False).iloc[0].to_dict()
    return row, BestLineDecision(
        mode=str(row.get("mode", "fallback")),
        reason="Chosen as fallback from the highest available score.",
    )


def suggested_to_dataframe(suggested_rows: List[Dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(suggested_rows)


def build_dashboard_data(df: pd.DataFrame, premium_line_count: int = 5) -> Dict[str, object]:
    hist = enrich_history(df)
    main_rank = build_rank_table(hist, MAIN_RANGE, [f"ball_{i}" for i in range(1, 6)], "main")
    star_rank = build_rank_table(hist, STAR_RANGE, ["lucky_star_1", "lucky_star_2"], "star")
    seed = target_line_seed(hist, premium_line_count)
    suggested = generate_premium_line_pack(hist, total_lines=premium_line_count)
    best_line, decision = choose_best_line(suggested)
    state = load_refresh_state()

    latest = hist.iloc[-1]
    latest_draw = {
        "date": latest["draw_date"].date().isoformat(),
        "balls": [int(latest[f"ball_{i}"]) for i in range(1, 6)],
        "stars": [int(latest["lucky_star_1"]), int(latest["lucky_star_2"])],
        "draw_number": "" if pd.isna(latest.get("draw_number")) else str(latest.get("draw_number")),
        "jackpot": "" if pd.isna(latest.get("jackpot")) else str(latest.get("jackpot")),
        "uk_code": "" if pd.isna(latest.get("uk_millionaire_maker")) else str(latest.get("uk_millionaire_maker")),
    }

    recent = hist.tail(10).sort_values("draw_date", ascending=False).copy()
    recent_rows = []
    for _, row in recent.iterrows():
        recent_rows.append({
            "draw_date": row["draw_date"].date().isoformat(),
            "balls": " ".join(f"{int(row[f'ball_{i}']):02d}" for i in range(1, 6)),
            "stars": f"{int(row['lucky_star_1']):02d} {int(row['lucky_star_2']):02d}",
        })

    hot_last_10_df = get_hot_numbers_last_n(hist, 10)
    overdue_df = get_overdue_numbers(hist)
    top_pairs_df = get_top_pairs(hist, 10)

    hot_last_10 = hot_last_10_df.to_dict(orient="records")
    overdue = overdue_df.to_dict(orient="records")
    top_pairs = top_pairs_df.to_dict(orient="records")

    hot_last_10_chart = simple_bar_chart_html(hot_last_10, "number", "seen_last_n", "Hot numbers")
    overdue_chart = simple_bar_chart_html(overdue, "number", "draws_since_seen", "Overdue numbers")
    top_pairs_chart = simple_bar_chart_html(top_pairs, "pair", "count", "Top pairs")
    quality = history_quality_report(hist)
    odds = pack_jackpot_probability(premium_line_count)
    odds["total_combinations"] = f"{TOTAL_COMBINATIONS:,}"
    odds["tiers"] = EUROMILLIONS_PRIZE_TIERS
    strategy = budget_strategy(premium_line_count)
    diversity = line_pack_diversity_report(suggested)

    return {
        "history_rows": len(hist),
        "latest_draw": latest_draw,
        "target_seed": seed,
        "main_top10": main_rank.head(10).to_dict(orient="records"),
        "star_top10": star_rank.head(10).to_dict(orient="records"),
        "suggested": suggested.to_dict(orient="records"),
        "best_line": best_line,
        "best_line_reason": decision.reason,
        "best_line_mode": decision.mode,
        "history_start": hist["draw_date"].min().date().isoformat(),
        "history_end": hist["draw_date"].max().date().isoformat(),
        "sum_mean": round(float(hist["sum_balls"].mean()), 2),
        "sum_std": round(float(hist["sum_balls"].std(ddof=0) or 0), 2),
        "recent_draws": recent_rows,
        "refresh_state": state,
        "hot_last_10": hot_last_10,
        "overdue_numbers": overdue,
        "top_pairs": top_pairs,
        "hot_last_10_chart": hot_last_10_chart,
        "overdue_chart": overdue_chart,
        "top_pairs_chart": top_pairs_chart,
        "premium_line_count": premium_line_count,
        "quality": quality,
        "odds": odds,
        "strategy": strategy,
        "diversity": diversity,
    }


def build_and_store_dashboard_cache(premium_line_count: int = 5) -> Tuple[Dict[str, object], RefreshResult]:
    df, refresh = refresh_history(allow_backfill=True, persist=True)
    data = build_dashboard_data(df, premium_line_count=premium_line_count)
    save_dashboard_cache(data, refresh, premium_line_count=premium_line_count)
    return data, refresh


def build_and_store_latest_official_cache(premium_line_count: int = 5) -> Tuple[Dict[str, object], RefreshResult]:
    df = load_local_history()
    before = len(df)
    sources: List[str] = []
    warnings: List[str] = []
    quick_backfilled = 0
    try:
        official = fetch_official_xml(timeout=12)
        if official.empty:
            raise ValueError("Official XML returned no valid draws.")
        latest_official = pd.to_datetime(official["draw_date"], errors="coerce").dt.date.max()
        if latest_official:
            quick_df, quick_backfilled, quick_errors = fetch_recent_quick_backfill(df, latest_official)
            if not quick_df.empty:
                df = dedupe_history(pd.concat([df, quick_df], ignore_index=True))
                sources.append("national_lottery_com_quick_backfill")
            if quick_errors and not quick_backfilled:
                warnings.append("Recent quick backfill was unavailable.")
        df = dedupe_history(pd.concat([df, official], ignore_index=True))
        sources.append("official_xml_latest")
        persist_history(df)
        latest_date = str(df["draw_date"].max()) if not df.empty else None
        quality = history_quality_report(df)
        message = "Latest official draw refresh complete."
        if quick_backfilled:
            message += f" Quick-backfilled {quick_backfilled} recent missing draw(s)."
        if warnings:
            message += " Warnings: " + " | ".join(warnings[:2])
        if not quality.get("ok", False):
            message += " Data quality warning: " + " ".join(str(x) for x in quality.get("notes", []))
        refresh = RefreshResult(
            source="+".join(sources),
            ok=bool(quality.get("ok", False)),
            message=message,
            draws_added=max(0, len(df) - before),
            latest_date=latest_date,
        )
    except Exception as exc:
        logger.exception("Latest official refresh failed; keeping local history")
        refresh = local_refresh_result(df, f"Latest official refresh failed. Using local history. Reason: {exc}")

    save_refresh_state(
        ok=refresh.ok,
        source=refresh.source,
        message=refresh.message,
        draws_added=refresh.draws_added,
        latest_date=refresh.latest_date,
    )
    data = build_dashboard_data(df, premium_line_count=premium_line_count)
    save_dashboard_cache(data, refresh, premium_line_count=premium_line_count)
    return data, refresh


def build_quick_dashboard_payload(premium_line_count: int = 5) -> Tuple[Dict[str, object], RefreshResult]:
    df, refresh = refresh_history(allow_backfill=False, persist=False)
    data = build_dashboard_data(df, premium_line_count=premium_line_count)
    return data, refresh


def public_auto_refresh_enabled() -> bool:
    return os.environ.get("EUROMILLIONS_PUBLIC_AUTO_REFRESH", "1").strip().lower() not in {"0", "false", "no"}


def public_auto_refresh_too_soon() -> bool:
    if PUBLIC_AUTO_REFRESH_MIN_INTERVAL_SECONDS <= 0:
        return False
    state = load_refresh_state()
    last_attempt = parse_utc_timestamp(state.get("last_attempt_at"))
    if last_attempt is None:
        return False
    if last_attempt.tzinfo is None:
        last_attempt = last_attempt.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - last_attempt
    return age.total_seconds() < PUBLIC_AUTO_REFRESH_MIN_INTERVAL_SECONDS


def load_public_history_snapshot() -> Tuple[pd.DataFrame, RefreshResult]:
    try:
        df = load_local_history()
        return df, local_refresh_result(
            df,
            "Loaded local CSV history. Online refresh runs outside public requests.",
        )
    except Exception:
        logger.exception("Public history snapshot load failed; using local CSV history")
        df = load_local_history()
        return df, local_refresh_result(df, "Loaded local CSV history after public snapshot failed.")


def build_dashboard_payload(premium_line_count: int = 5, allow_refresh: bool = False) -> Dict[str, object]:
    if not allow_refresh:
        cached = load_dashboard_cache(premium_line_count=premium_line_count)
        if cached is not None:
            data, refresh, generated_at = cached
            return {
                "data": data,
                "refresh": refresh_to_dict(refresh),
                "generated_at": generated_at,
                "cache_used": True,
            }
        if public_auto_refresh_enabled() and not public_auto_refresh_too_soon():
            try:
                data, refresh = build_and_store_latest_official_cache(premium_line_count=premium_line_count)
                return {
                    "data": data,
                    "refresh": refresh_to_dict(refresh),
                    "generated_at": utc_now_iso(),
                    "cache_used": False,
                }
            except Exception:
                logger.exception("Public auto-refresh failed; trying stale dashboard cache")
        cached = load_dashboard_cache(premium_line_count=premium_line_count, allow_stale=True)
        if cached is not None:
            data, refresh, generated_at = cached
            return {
                "data": data,
                "refresh": refresh_to_dict(refresh),
                "generated_at": generated_at,
                "cache_used": True,
            }

    if allow_refresh:
        data, refresh = build_and_store_dashboard_cache(premium_line_count=premium_line_count)
        cache_used = False
        generated_at = utc_now_iso()
    else:
        try:
            df, refresh = load_public_history_snapshot()
            data = build_dashboard_data(df, premium_line_count=premium_line_count)
            save_dashboard_cache(data, refresh, premium_line_count=premium_line_count)
        except Exception:
            logger.exception("Quick dashboard refresh failed; using local CSV history")
            df = load_local_history()
            refresh = local_refresh_result(df, "Loaded local CSV history after quick refresh was unavailable.")
            data = build_dashboard_data(df, premium_line_count=premium_line_count)
            save_dashboard_cache(data, refresh, premium_line_count=premium_line_count)
        cache_used = False
        generated_at = utc_now_iso()

    return {
        "data": data,
        "refresh": refresh_to_dict(refresh),
        "generated_at": generated_at,
        "cache_used": cache_used,
    }


def render_table(rows: List[Dict[str, object]], columns: Sequence[Tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body_parts = []
    for row in rows:
        tds = "".join(f"<td>{html.escape(str(row.get(key, '')))}</td>" for key, _ in columns)
        body_parts.append(f"<tr>{tds}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_parts)}</tbody></table>"


def mode_chip(mode: str) -> str:
    classes = {
        "value": "safe",
        "balanced": "balanced",
        "coverage": "aggressive",
        "anti_last_draw": "anti",
    }
    labels = {
        "value": "VALUE",
        "balanced": "BALANCED",
        "coverage": "COVERAGE",
        "anti_last_draw": "ANTI LAST DRAW",
    }
    cls = classes.get(mode, "balanced")
    label = labels.get(mode, mode.upper())
    return f'<span class="chip {cls}">{html.escape(label)}</span>'


def render_dashboard(data: Dict[str, object], refresh: RefreshResult) -> str:
    latest = data["latest_draw"]
    best = data["best_line"]
    runtime = data.get("runtime_status", {}) if isinstance(data.get("runtime_status", {}), dict) else {}
    state = data.get("refresh_state", {})
    quality = data.get("quality", {})
    odds = data.get("odds", {})
    strategy = data.get("strategy", {})
    diversity = data.get("diversity", {})
    quality_notes = quality.get("notes", []) if isinstance(quality, dict) else []
    quality_class = "ok" if isinstance(quality, dict) and quality.get("ok") else "warn"
    quality_title = "DATA QUALITY OK" if quality_class == "ok" else "DATA QUALITY WARNING"
    quality_html = "".join(f"<li>{html.escape(str(note))}</li>" for note in quality_notes)
    if isinstance(quality, dict) and quality.get("missing_recent_dates"):
        missing = ", ".join(str(x) for x in quality.get("missing_recent_dates", [])[:12])
        quality_html += f"<li>Missing recent dates: {html.escape(missing)}</li>"

    main_table = render_table(
        data["main_top10"],
        [("rank", "#"), ("number", "Number"), ("times_seen", "Seen"), ("draws_since_seen", "Draws since"), ("score", "Score")],
    )
    star_table = render_table(
        data["star_top10"],
        [("rank", "#"), ("number", "Star"), ("times_seen", "Seen"), ("draws_since_seen", "Draws since"), ("score", "Score")],
    )
    recent_draws_table = render_table(
        data["recent_draws"],
        [("draw_date", "Date"), ("balls", "Main numbers"), ("stars", "Stars")],
    )
    suggested_table = render_table(
        data["suggested"],
        [("mode", "Mode"), ("balls", "Main numbers"), ("stars", "Stars"), ("sum_balls", "Sum"), ("odd_even", "Odd-Even"), ("low_high", "Low-High"), ("score", "Score"), ("value_score", "Value"), ("popularity_risk", "Share risk")],
    )
    odds_table = render_table(
        odds.get("tiers", [])[:8] if isinstance(odds, dict) else [],
        [("match", "Match"), ("odds", "Odds 1 in"), ("avg_prize_gbp", "Avg prize £")],
    )
    strategy_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in strategy.get("best_practice", [])) if isinstance(strategy, dict) else ""

    refresh_text = f"{refresh.message} Added {refresh.draws_added} new draw(s)." if refresh.ok else refresh.message
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    balls_html = "".join(f'<span class="ball">{n:02d}</span>' for n in latest["balls"])
    stars_html = "".join(f'<span class="star">{n:02d}</span>' for n in latest["stars"])
    best_balls_html = "".join(f'<span class="ball hero-ball">{n}</span>' for n in str(best["balls"]).split())
    best_stars_html = "".join(f'<span class="star hero-star">{n}</span>' for n in str(best["stars"]).split())

    last_success_at = state.get("last_success_at", "-")
    last_attempt_at = state.get("last_attempt_at", "-")
    last_success_source = state.get("last_success_source", "-")
    cache_used_text = "yes" if runtime.get("cache_used") else "no"
    payload_generated_at = runtime.get("generated_at", "-")

    selector_links = """
    <div class="actions">
      <a class="btn alt" href="/euromillions?lines=1">1 line</a>
      <a class="btn alt" href="/euromillions?lines=3">3 lines</a>
      <a class="btn alt" href="/euromillions?lines=5">5 lines</a>
      <a class="btn alt" href="/euromillions?lines=10">10 lines</a>
    </div>
    """

    return f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>EuroMillions Live Dashboard</title>
<meta http-equiv=\"refresh\" content=\"900\">
<style>
:root {{
  --bg-0:#020204;
  --bg-1:#050b10;
  --panel:#07100d;
  --panel-2:#020806;
  --text:#d7fff2;
  --muted:#73a99a;
  --neon:#00ff9c;
  --cyan:#00d8ff;
  --pink:#ff2bd6;
  --gold:#ffd54a;
  --red:#ff3864;
  --safe:#0bcf7a;
  --balanced:#00d8ff;
  --aggr:#ff3864;
  --anti:#bc6cff;
  --shadow:0 0 0 1px rgba(0,255,156,.16), 0 0 34px rgba(0,255,156,.16), inset 0 0 0 1px rgba(255,255,255,.035);
  --shadow-hot:0 0 14px rgba(0,255,156,.46), 0 0 42px rgba(0,216,255,.18);
}}
* {{ box-sizing:border-box; }}
html {{ scroll-behavior:smooth; }}
body {{
  margin:0;
  color:var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  background:
    radial-gradient(circle at 18% 8%, rgba(0,255,156,.16), transparent 23%),
    radial-gradient(circle at 82% 2%, rgba(255,43,214,.12), transparent 22%),
    radial-gradient(circle at 55% 90%, rgba(0,216,255,.10), transparent 25%),
    linear-gradient(180deg, var(--bg-0), var(--bg-1) 48%, #010302);
  min-height:100vh;
  overflow-x:hidden;
}}
body::before {{
  content:"";
  position:fixed;
  inset:0;
  pointer-events:none;
  z-index:0;
  background:
    linear-gradient(rgba(0,255,156,.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,216,255,.025) 1px, transparent 1px);
  background-size:36px 36px;
  mask-image: radial-gradient(circle at center, black 0 58%, transparent 88%);
}}
body::after {{
  content:"";
  position:fixed;
  inset:0;
  pointer-events:none;
  z-index:2;
  background:repeating-linear-gradient(0deg, rgba(255,255,255,.035) 0 1px, transparent 1px 4px);
  mix-blend-mode:overlay;
  opacity:.18;
}}
.wrap {{ max-width: 1480px; margin: 0 auto; padding: 26px; position:relative; z-index:1; }}
.grid {{ display:grid; gap:18px; }}
.top {{ grid-template-columns: 1.32fr .68fr; }}
.two {{ grid-template-columns: 1fr 1fr; }}
.three {{ grid-template-columns: 1fr 1fr 1fr; }}
.card {{
  position:relative;
  overflow:hidden;
  background:
    linear-gradient(135deg, rgba(0,255,156,.075), transparent 22%),
    linear-gradient(180deg, rgba(7,16,13,.92), rgba(1,6,5,.96));
  border:1px solid rgba(0,255,156,.20);
  border-radius: 24px;
  padding: 19px;
  box-shadow: var(--shadow);
  backdrop-filter: blur(10px);
}}
.card::before {{
  content:"";
  position:absolute;
  inset:0;
  pointer-events:none;
  background:linear-gradient(90deg, transparent, rgba(0,255,156,.11), transparent);
  transform:translateX(-120%);
  animation: sweep 7s linear infinite;
}}
.card::after {{
  content:"";
  position:absolute;
  left:18px; right:18px; top:0;
  height:1px;
  background:linear-gradient(90deg, transparent, rgba(0,255,156,.85), rgba(0,216,255,.75), transparent);
}}
@keyframes sweep {{ 0%,55% {{ transform:translateX(-120%); }} 70%,100% {{ transform:translateX(120%); }} }}
.hero-title {{
  font-size: clamp(34px, 5vw, 64px);
  line-height:.92;
  margin: 10px 0 12px;
  letter-spacing:-2px;
  color:#eafff8;
  text-shadow: 0 0 8px rgba(0,255,156,.72), 0 0 28px rgba(0,216,255,.28);
}}
.hero-title::before {{ content:"> "; color:var(--neon); }}
.sub {{ color: var(--muted); line-height:1.6; max-width: 990px; }}
.tiny {{ color: var(--muted); font-size: 12px; }}
.badge {{
  display:inline-flex; align-items:center; gap:8px;
  padding:8px 12px; border-radius:999px; font-size:12px; font-weight:900;
  border:1px solid rgba(0,255,156,.34); background:rgba(0,255,156,.075); color:var(--neon);
  text-transform:uppercase; letter-spacing:.12em;
  box-shadow:0 0 18px rgba(0,255,156,.18);
}}
.badge::before {{ content:"●"; color:var(--neon); text-shadow:0 0 9px var(--neon); animation:pulse 1.4s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:.35; }} 50% {{ opacity:1; }} }}
.section-title {{ font-size: 22px; margin: 0 0 13px; letter-spacing:-.5px; text-shadow:0 0 12px rgba(0,255,156,.25); }}
.section-title::before {{ content:"// "; color:var(--cyan); }}
.kpi-grid {{ display:grid; grid-template-columns: repeat(4,1fr); gap:12px; margin-top:17px; }}
.kpi {{ background:rgba(0,255,156,.045); border:1px solid rgba(0,255,156,.16); border-radius:17px; padding:13px; box-shadow: inset 0 0 24px rgba(0,255,156,.025); }}
.kpi .label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.11em; }}
.kpi .value {{ font-size:19px; margin-top:6px; font-weight:900; color:#f3fffb; }}
.balls {{ display:flex; flex-wrap:wrap; gap:9px; margin-top:12px; }}
.ball,.star {{
  width:46px; height:46px; display:inline-flex; align-items:center; justify-content:center;
  border-radius:14px; font-weight:1000; font-size:15px;
  border:1px solid rgba(255,255,255,.12);
  transform:skewX(-4deg);
}}
.ball {{ background:linear-gradient(180deg,#eafff8,#8fffd2); color:#04100b; box-shadow:0 0 18px rgba(0,255,156,.25); }}
.star {{ background:linear-gradient(180deg,#fff0a0,var(--gold)); color:#2d2100; box-shadow:0 0 18px rgba(255,213,74,.28); }}
.hero-line {{ display:flex; flex-wrap:wrap; gap:11px; margin: 14px 0; }}
.hero-ball,.hero-star {{ width:60px; height:60px; font-size:19px; border-radius:18px; box-shadow:var(--shadow-hot); }}
.best-meta {{ display:grid; grid-template-columns: repeat(4,1fr); gap:10px; margin-top:14px; }}
.best-meta .box {{ background:rgba(0,216,255,.055); border:1px solid rgba(0,216,255,.18); border-radius:15px; padding:11px; }}
.best-meta .box .v {{ font-weight:900; font-size:18px; margin-top:4px; color:#e9fbff; }}
.chip {{ display:inline-flex; padding:7px 11px; border-radius:999px; font-size:12px; font-weight:900; letter-spacing:.10em; border:1px solid currentColor; }}
.chip.safe {{ background:rgba(11,207,122,.14); color:#8dffd0; }}
.chip.balanced {{ background:rgba(0,216,255,.14); color:#9befff; }}
.chip.aggressive {{ background:rgba(255,56,100,.14); color:#ff9ab0; }}
.chip.anti {{ background:rgba(188,108,255,.16); color:#e7c7ff; }}
table {{ width:100%; border-collapse: collapse; overflow:hidden; }}
th, td {{ border-bottom:1px solid rgba(0,255,156,.09); padding:12px 10px; text-align:left; font-size:14px; }}
th {{ color:#9dffe1; font-size:11px; letter-spacing:.12em; text-transform:uppercase; background:rgba(0,255,156,.04); }}
tr:hover td {{ background:rgba(0,255,156,.045); color:#ffffff; }}
.inline-cmd {{ background:#010604; border:1px solid rgba(0,255,156,.28); border-radius:14px; padding:12px 13px; color:#c8ffea; overflow-wrap:anywhere; box-shadow:inset 0 0 24px rgba(0,255,156,.05); }}
.inline-cmd::before {{ content:"$ "; color:var(--neon); }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
.btn {{
  cursor:pointer; text-decoration:none;
  padding:12px 16px; border-radius:14px; font-weight:900;
  background:linear-gradient(180deg, rgba(0,255,156,.20), rgba(0,255,156,.075));
  color:var(--text); border:1px solid rgba(0,255,156,.32);
  display:inline-block; box-shadow:0 0 18px rgba(0,255,156,.11);
  transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
}}
button.btn {{ font-family:inherit; }}
.btn:hover {{ transform:translateY(-2px); border-color:rgba(0,255,156,.75); box-shadow:0 0 26px rgba(0,255,156,.24); }}
.btn.alt {{ background:linear-gradient(180deg, rgba(0,216,255,.18), rgba(0,216,255,.07)); border-color:rgba(0,216,255,.30); }}
.small-note {{ color:var(--muted); font-size:13px; line-height:1.55; }}
.alert {{ margin-top:12px; border-radius:17px; padding:12px 14px; line-height:1.45; }}
.alert.ok {{ border:1px solid rgba(11,207,122,.32); background:rgba(11,207,122,.09); color:#c8ffea; }}
.alert.warn {{ border:1px solid rgba(255,213,74,.42); background:rgba(255,213,74,.11); color:#ffeaa0; }}
.alert ul {{ margin:8px 0 0 18px; padding:0; }}
.footer {{ margin-top:18px; color:var(--muted); font-size:13px; line-height:1.7; }}
.mini-chart-title {{ font-size:14px; font-weight:900; margin-bottom:10px; color:#c8ffea; }}
.bar-row {{ display:grid; grid-template-columns: 58px 1fr 54px; gap:8px; align-items:center; margin-bottom:8px; }}
.bar-label {{ font-size:12px; color:#d9fff0; }}
.bar-track {{ height:11px; background:rgba(255,255,255,.06); border:1px solid rgba(0,255,156,.08); border-radius:999px; overflow:hidden; }}
.bar-fill {{ height:100%; background:linear-gradient(90deg, rgba(0,255,156,.95), rgba(0,216,255,.95), rgba(255,43,214,.72)); border-radius:999px; box-shadow:0 0 13px rgba(0,255,156,.32); }}
.bar-value {{ font-size:12px; color:#b7ffe5; text-align:right; }}
@media (max-width: 1100px) {{
  .top, .two, .three {{ grid-template-columns: 1fr; }}
  .kpi-grid, .best-meta {{ grid-template-columns: 1fr 1fr; }}
}}
@media (max-width: 620px) {{
  .wrap {{ padding:14px; }}
  .kpi-grid, .best-meta {{ grid-template-columns: 1fr; }}
  th, td {{ font-size:12px; padding:9px 7px; }}
  .hero-ball,.hero-star {{ width:52px; height:52px; }}
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
<div class=\"wrap\">

  <div class=\"card\">
    <div class=\"badge\">live hacker model</div>
    <div class=\"hero-title\">EUROMILLIONS // ODDS ENGINE</div>
    <div class=\"sub\">Neon command center for EuroMillions analytics: live refresh, archive repair, data-quality checks, hot/overdue signals, frequent pairs, anti-last-draw logic and premium line packs.</div>

    <div class=\"kpi-grid\">
      <div class=\"kpi\"><div class=\"label\">Generated</div><div class=\"value\">{html.escape(generated)}</div></div>
      <div class=\"kpi\"><div class=\"label\">History range</div><div class=\"value\">{html.escape(str(data['history_start']))}<br><span class=\"tiny\">to {html.escape(str(data['history_end']))}</span></div></div>
      <div class=\"kpi\"><div class=\"label\">Stored draws</div><div class=\"value\">{data['history_rows']}</div></div>
      <div class=\"kpi\"><div class=\"label\">Premium lines shown</div><div class=\"value\">{html.escape(str(data['premium_line_count']))}</div></div>
    </div>
  </div>

  <div class=\"grid top\" style=\"margin-top:18px;\">
    <div class=\"card\">
      <div class=\"section-title\">Target line for next draw</div>
      <div>{mode_chip(str(data['best_line_mode']))}</div>
      <p class=\"small-note\">Based on latest draw in history: {html.escape(str(latest['date']))}.</p>
      <div class=\"hero-line\" style=\"margin-top:14px;\">{best_balls_html}</div>
      <div class=\"hero-line\">{best_stars_html}</div>
      <div id=\"best-line-copy\" class=\"inline-cmd\" style=\"margin-top:14px;\">Main numbers: {html.escape(str(best['balls']))} | Stars: {html.escape(str(best['stars']))}</div>
      {selector_links}
      <div class=\"actions\">
        <button class=\"btn\" onclick=\"copyBestLine()\">Copy best line</button>
        <button class=\"btn alt\" onclick=\"refreshNow()\">Refresh now</button>
        <a class=\"btn alt\" href=\"/download/suggested\">Download suggested CSV</a>
        <span id=\"copy-status\" class=\"small-note\"></span>
      </div>
      <div class=\"best-meta\">
        <div class=\"box\"><div class=\"tiny\">Score</div><div class=\"v\">{html.escape(str(best['score']))}</div></div>
        <div class=\"box\"><div class=\"tiny\">Value</div><div class=\"v\">{html.escape(str(best.get('value_score', '-')))}</div></div>
        <div class=\"box\"><div class=\"tiny\">Share risk</div><div class=\"v\">{html.escape(str(best.get('popularity_risk', '-')))}</div></div>
        <div class=\"box\"><div class=\"tiny\">Sum</div><div class=\"v\">{html.escape(str(best['sum_balls']))}</div></div>
      </div>
      <p class=\"small-note\" style=\"margin-top:14px;\">{html.escape(str(data['best_line_reason']))}</p>
    </div>

    <div class=\"card\">
      <div class=\"section-title\">Sync / machine status</div>
      <p class=\"small-note\">{html.escape(refresh_text)}</p>
      <div class=\"tiny\">Latest draw date: {html.escape(str(latest['date']))}</div>
      <div class=\"tiny\">Payload generated: {html.escape(str(payload_generated_at))}</div>
      <div class=\"tiny\">Cache used: {html.escape(cache_used_text)}</div>
      <div class=\"tiny\">Last attempt: {html.escape(str(last_attempt_at))}</div>
      <div class=\"tiny\">Last success: {html.escape(str(last_success_at))}</div>
      <div class=\"tiny\">Last success source: {html.escape(str(last_success_source))}</div>
      <div class=\"alert {quality_class}\">
        <b>{quality_title}</b>
        <ul>{quality_html}</ul>
      </div>
      <div class=\"actions\">
        <a class=\"btn alt\" href=\"/download/history\">Download history CSV</a>
      </div>
    </div>
  </div>

  <div class=\"grid top\" style=\"margin-top:18px; grid-template-columns: 1fr 1fr;\">
    <div class=\"card\">
      <div class=\"section-title\">Latest official draw in your history</div>
      <div class=\"tiny\">Draw date: {html.escape(str(latest['date']))}</div>
      <div class=\"balls\">{balls_html}</div>
      <div class=\"balls\">{stars_html}</div>
      <div class=\"kpi-grid\" style=\"grid-template-columns: repeat(3,1fr);\">
        <div class=\"kpi\"><div class=\"label\">Draw number</div><div class=\"value\">{html.escape(str(latest['draw_number'])) or '-'}</div></div>
        <div class=\"kpi\"><div class=\"label\">Jackpot</div><div class=\"value\">{html.escape(str(latest['jackpot'])) or '-'}</div></div>
        <div class=\"kpi\"><div class=\"label\">UK MM code</div><div class=\"value\" style=\"font-size:16px;\">{html.escape(str(latest['uk_code'])) or '-'}</div></div>
      </div>
    </div>

    <div class=\"card\">
      <div class=\"section-title\">What to play</div>
      <p class=\"small-note\"><strong>Fast rule:</strong> use the big line in <strong>Best line for next draw</strong>.</p>
      <p class=\"small-note\"><strong>Line pack:</strong> 1, 3, 5 or 10 diversified lines using the selector buttons.</p>
      <p class=\"small-note\"><strong>Best realistic edge:</strong> buy only what you can afford, use more independent lines if your budget allows, and avoid common human patterns so a prize is less likely to be shared.</p>
      <p class=\"small-note\"><strong>Current pack odds:</strong> jackpot {html.escape(str(odds.get('jackpot_odds_text', '1 in 139,838,160')))}; any prize {html.escape(str(odds.get('any_prize_odds_text', 'about 1 in 13 per line')))}.</p>
      <p class=\"small-note\"><strong>Cost:</strong> {html.escape(str(strategy.get('cost_per_draw_text', '£0.00')))} per draw; approx {html.escape(str(strategy.get('monthly_if_two_draws_per_week_text', '£0.00')))} / month if playing Tue+Fri.</p>
      <p class=\"small-note\"><strong>Diversity:</strong> {html.escape(str(diversity.get('message', '-')))} · unique main numbers {html.escape(str(diversity.get('unique_main_numbers', '-')))} · max overlap {html.escape(str(diversity.get('max_pair_overlap', '-')))}.</p>
    </div>
  </div>

  <div class=\"card\" style=\"margin-top:18px;\">
    <div class=\"section-title\">Probability truth engine</div>
    <p class=\"small-note\"><strong>Total valid combinations:</strong> {html.escape(str(odds.get('total_combinations', '139,838,160')))}. {html.escape(str(odds.get('truth', 'Every valid line has the same jackpot chance.')))}</p>
    <p class=\"small-note\"><strong>What this app optimises:</strong> clean live data, diversified line packs, statistically normal shapes, and lower shared-prize risk — not magic prediction.</p>
    <p class=\"small-note\"><strong>Expected value check:</strong> estimated ticket cost {html.escape(str(odds.get('estimated_cost_text', '-')))}; gross expected prize before cost {html.escape(str(odds.get('gross_expected_prize_text', '-')))}. {html.escape(str(odds.get('expected_loss_warning', '')))}</p>
    <ul class=\"small-note\">{strategy_items}</ul>
    {odds_table}
  </div>

  <div class=\"card\" style=\"margin-top:18px;\">
    <div class=\"section-title\">Latest 10 draws</div>
    {recent_draws_table}
  </div>

  <div class=\"card\" style=\"margin-top:18px;\">
    <div class=\"section-title\">Suggested premium lines</div>
    {suggested_table}
  </div>

  <div class=\"grid three\" style=\"margin-top:18px;\">
    <div class=\"card\">
      <div class=\"section-title\">Hot numbers (last 10 draws)</div>
      {data['hot_last_10_chart']}
    </div>
    <div class=\"card\">
      <div class=\"section-title\">Most overdue numbers</div>
      {data['overdue_chart']}
    </div>
    <div class=\"card\">
      <div class=\"section-title\">Top frequent pairs</div>
      {data['top_pairs_chart']}
    </div>
  </div>

  <div class=\"grid two\" style=\"margin-top:18px;\">
    <div class=\"card\">
      <div class=\"section-title\">Top 10 main numbers</div>
      {main_table}
    </div>
    <div class=\"card\">
      <div class=\"section-title\">Top 10 stars</div>
      {star_table}
    </div>
  </div>

  <div class=\"card footer\">
    <strong>Model notes.</strong> Ball-sum mean in your history: <strong>{html.escape(str(data['sum_mean']))}</strong> | standard deviation: <strong>{html.escape(str(data['sum_std']))}</strong><br>
    <strong>Responsible play:</strong> this dashboard can organise choices and avoid weak patterns, but lottery draws are random. It cannot predict or improve the jackpot odds of any valid line.
  </div>

</div>
</body>
</html>"""


try:
    from flask import Flask, Response, jsonify, request

    app = Flask(__name__)

    @app.route("/")
    def flask_home():
        return """
        <html>
        <head><title>EuroMillions Dashboard</title></head>
        <body style="background:#0b0f19;color:white;font-family:Arial;padding:40px;">
            <h1>EuroMillions Dashboard</h1>
            <p>Server running on Render</p>
            <a style="color:#4dd0ff;font-size:22px;" href="/euromillions">Open EuroMillions Dashboard</a>
        </body>
        </html>
        """

    @app.route("/euromillions")
    def flask_euromillions():
        try:
            lines_count = request.args.get("lines", default=5, type=int)
            if lines_count not in [1, 3, 5, 10]:
                lines_count = 5
            payload = build_dashboard_payload(premium_line_count=lines_count)
            refresh = refresh_from_dict(payload["refresh"])
            page = render_dashboard(payload["data"], refresh)
            return Response(page, mimetype="text/html")
        except Exception:
            logger.exception("Legacy Render entrypoint failed")
            return Response(
                "<h1>EuroMillions error</h1><p>The dashboard could not load cached data.</p>",
                status=500,
                mimetype="text/html",
            )

    @app.route("/api/suggested")
    def flask_api_suggested():
        try:
            lines_count = request.args.get("lines", default=5, type=int)
            if lines_count not in [1, 3, 5, 10]:
                lines_count = 5
            payload = build_dashboard_payload(premium_line_count=lines_count)
            data = payload["data"]
            return jsonify({
                "ok": True,
                "refresh": payload["refresh"],
                "quality": data.get("quality"),
                "best_line": data.get("best_line"),
                "suggested": data.get("suggested"),
            })
        except Exception:
            logger.exception("Legacy Render API failed")
            return jsonify({"ok": False, "error": "dashboard_unavailable"}), 500

    @app.route("/cron/refresh", methods=["GET", "POST"])
    def flask_cron_refresh():
        try:
            min_interval = int(os.environ.get("CRON_REFRESH_MIN_INTERVAL_SECONDS", "1200"))
            if min_interval > 0:
                quality = history_quality_report(load_local_history())
                if quality.get("ok", False):
                    state = load_refresh_state()
                    last_attempt = parse_utc_timestamp(state.get("last_attempt_at"))
                    if last_attempt is not None:
                        if last_attempt.tzinfo is None:
                            last_attempt = last_attempt.replace(tzinfo=dt.timezone.utc)
                        age = dt.datetime.now(dt.timezone.utc) - last_attempt
                        if age.total_seconds() < min_interval:
                            return jsonify({
                                "ok": True,
                                "skipped": True,
                                "message": "Refresh skipped because the previous attempt was recent.",
                                "last_attempt_at": state.get("last_attempt_at"),
                                "latest_date": state.get("latest_date"),
                            })

            data, refresh = build_and_store_latest_official_cache()
            df = load_local_history()
            return jsonify({
                "ok": refresh.ok,
                "skipped": False,
                "source": refresh.source,
                "message": refresh.message,
                "draws_added": refresh.draws_added,
                "latest_date": refresh.latest_date,
                "rows": len(df),
                "cache_history_rows": data.get("history_rows"),
                "quality": history_quality_report(df),
            })
        except Exception:
            logger.exception("Legacy Render cron refresh failed")
            return jsonify({
                "ok": False,
                "error": "cron_refresh_failed",
                "message": "The cron refresh failed. Check server logs for details.",
            }), 500
except Exception:
    app = None
