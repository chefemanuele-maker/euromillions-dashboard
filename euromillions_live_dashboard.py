#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import random
import re
import sys
import html
import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

BASE_DIR = Path.home() / "Data" / "Euro"
LOCAL_HISTORY = BASE_DIR / "euromillions_history_live.csv"
USER_ORIGINAL = BASE_DIR / "euromillions_export_2026-03-16.csv"
REFRESH_STATE_FILE = BASE_DIR / "euromillions_refresh_state.json"

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
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
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

    return df.dropna(subset=["draw_date"] + num_cols).copy()


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
    persist_history(df)
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

    def unique_preserve(seq: List[int]) -> List[int]:
        out: List[int] = []
        seen = set()
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def first_value(values_map: Dict[str, List[str]], keys: Sequence[str], default=pd.NA):
        for key in keys:
            vals = values_map.get(key)
            if vals:
                return vals[0]
        return default

    allowed_draw_tags = {
        "draw",
        "game_draw",
        "draw_item",
        "draw_row",
        "drawdetails",
        "draw_detail",
        "result",
        "results",
    }

    for elem in root.iter():
        tag = local_name(elem.tag)

        if tag not in allowed_draw_tags and "draw" not in tag:
            continue

        values_map: Dict[str, List[str]] = {}

        for child in elem.iter():
            child_name = local_name(child.tag)
            child_value = clean_text(child.text)
            if not child_value:
                continue
            values_map.setdefault(child_name, []).append(child_value)

        draw_date = None
        for key in ["draw_date", "date", "drawdate", "draw_date_uk", "draw_date_iso"]:
            vals = values_map.get(key, [])
            for v in vals:
                m = re.search(r"\d{4}-\d{2}-\d{2}", v)
                if m:
                    draw_date = m.group(0)
                    break
            if draw_date:
                break

        if not draw_date:
            for vals in values_map.values():
                for v in vals:
                    m = re.search(r"\d{4}-\d{2}-\d{2}", v)
                    if m:
                        draw_date = m.group(0)
                        break
                if draw_date:
                    break

        if not draw_date:
            continue

        main_candidates: List[int] = []
        star_candidates: List[int] = []

        explicit_main_tags = {
            "ball_1", "ball_2", "ball_3", "ball_4", "ball_5",
            "main_ball_1", "main_ball_2", "main_ball_3", "main_ball_4", "main_ball_5",
            "number_1", "number_2", "number_3", "number_4", "number_5",
            "main_number_1", "main_number_2", "main_number_3", "main_number_4", "main_number_5",
        }

        explicit_star_tags = {
            "lucky_star_1", "lucky_star_2",
            "star_1", "star_2",
            "star_number_1", "star_number_2",
            "lucky_stars_1", "lucky_stars_2",
        }

        for key, vals in values_map.items():
            for v in vals:
                if not re.fullmatch(r"\d{1,2}", v):
                    continue
                n = int(v)

                if key in explicit_main_tags:
                    if 1 <= n <= 50:
                        main_candidates.append(n)
                elif key in explicit_star_tags:
                    if 1 <= n <= 12:
                        star_candidates.append(n)

        if len(main_candidates) < 5 or len(star_candidates) < 2:
            for key, vals in values_map.items():
                key_l = key.lower()

                if any(bad in key_l for bad in [
                    "draw_number", "drawno", "machine", "set", "jackpot",
                    "millionaire", "ukmm", "prize", "winner", "raffle",
                    "amount", "count", "game", "id"
                ]):
                    continue

                for v in vals:
                    if not re.fullmatch(r"\d{1,2}", v):
                        continue
                    n = int(v)

                    if "star" in key_l:
                        if 1 <= n <= 12:
                            star_candidates.append(n)
                    elif "ball" in key_l or "number" in key_l:
                        if 1 <= n <= 50:
                            main_candidates.append(n)

        main_candidates = unique_preserve(main_candidates)
        star_candidates = unique_preserve(star_candidates)

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
            "draw_number": first_value(
                values_map,
                ["draw_number", "draw_no", "drawno", "id"],
                default=pd.NA,
            ),
            "jackpot": first_value(
                values_map,
                ["jackpot_amount", "jackpot", "jackpot_value"],
                default=pd.NA,
            ),
            "uk_millionaire_maker": first_value(
                values_map,
                ["uk_millionaire_maker", "ukmm_code", "millionaire_maker_code"],
                default=pd.NA,
            ),
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


def refresh_history() -> Tuple[pd.DataFrame, RefreshResult]:
    df = load_local_history()

    try:
        official = fetch_official_xml()
        if official.empty:
            raise ValueError("Official XML returned no valid draws.")

        before = len(df)
        merged = dedupe_history(pd.concat([df, official], ignore_index=True))
        persist_history(merged)
        added = len(merged) - before
        latest_date = str(merged["draw_date"].max())

        result = RefreshResult(
            source="official_xml",
            ok=True,
            message="Official refresh complete.",
            draws_added=max(0, added),
            latest_date=latest_date,
        )
        save_refresh_state(
            ok=result.ok,
            source=result.source,
            message=result.message,
            draws_added=result.draws_added,
            latest_date=result.latest_date,
        )
        return merged, result

    except Exception as xml_exc:
        logger.exception("Official XML refresh failed")

        try:
            html_backup = fetch_official_html_backup()
            if html_backup.empty:
                raise ValueError("Official HTML backup returned no valid draws.")

            before = len(df)
            merged = dedupe_history(pd.concat([df, html_backup], ignore_index=True))
            persist_history(merged)
            added = len(merged) - before
            latest_date = str(merged["draw_date"].max())

            result = RefreshResult(
                source="official_html_backup",
                ok=True,
                message=f"XML failed, HTML backup refresh complete. ({xml_exc})",
                draws_added=max(0, added),
                latest_date=latest_date,
            )
            save_refresh_state(
                ok=result.ok,
                source=result.source,
                message=result.message,
                draws_added=result.draws_added,
                latest_date=result.latest_date,
            )
            return merged, result

        except Exception as html_exc:
            logger.exception("Official HTML backup refresh failed")
            latest_date = str(df["draw_date"].max()) if not df.empty else None
            result = RefreshResult(
                source="local_cache",
                ok=False,
                message=f"Official source unavailable. Using local cache. (XML: {xml_exc}) (HTML: {html_exc})",
                draws_added=0,
                latest_date=latest_date,
            )
            save_refresh_state(
                ok=result.ok,
                source=result.source,
                message=result.message,
                draws_added=result.draws_added,
                latest_date=result.latest_date,
            )
            return df, result


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


def line_score(
    balls: Sequence[int],
    stars: Sequence[int],
    main_rank: pd.DataFrame,
    star_rank: pd.DataFrame,
    hist_sum_mean: float,
    hist_sum_std: float,
) -> float:
    main_lookup = main_rank.set_index("number")["score"].to_dict()
    star_lookup = star_rank.set_index("number")["score"].to_dict()
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


def generate_suggested_lines(df: pd.DataFrame, lines_per_mode: int = 4, seed: int = 42) -> pd.DataFrame:
    main_rank = build_rank_table(df, MAIN_RANGE, [f"ball_{i}" for i in range(1, 6)], "main")
    star_rank = build_rank_table(df, STAR_RANGE, ["lucky_star_1", "lucky_star_2"], "star")

    hist_sum_mean = float(df["sum_balls"].mean())
    hist_sum_std = float(df["sum_balls"].std(ddof=0) or 1.0)

    rng = random.Random(seed)
    main_weights = {row["number"]: float(row["score"]) for _, row in main_rank.iterrows()}
    star_weights = {row["number"]: float(row["score"]) for _, row in star_rank.iterrows()}

    modes = {
        "safe": {"top_main": 18, "top_star": 8, "jitter": 0.08},
        "balanced": {"top_main": 28, "top_star": 10, "jitter": 0.18},
        "aggressive": {"top_main": 40, "top_star": 12, "jitter": 0.33},
        "anti_last_draw": {"top_main": 32, "top_star": 12, "jitter": 0.20},
    }

    last_row = df.iloc[-1]
    last_balls = {int(last_row[f"ball_{i}"]) for i in range(1, 6)}
    last_stars = {int(last_row["lucky_star_1"]), int(last_row["lucky_star_2"])}

    rows: List[Dict[str, object]] = []
    used = set()

    for mode, cfg in modes.items():
        tries = 0
        made = 0

        while made < lines_per_mode and tries < 1500:
            tries += 1
            main_pool = main_rank["number"].tolist()[:cfg["top_main"]]
            star_pool = star_rank["number"].tolist()[:cfg["top_star"]]

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

            score = line_score(
                balls,
                stars,
                main_rank,
                star_rank,
                hist_sum_mean,
                hist_sum_std,
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
    mode_order = pd.CategoricalDtype(
        categories=["safe", "balanced", "aggressive", "anti_last_draw"],
        ordered=True
    )
    out["mode"] = out["mode"].astype(mode_order)
    out = out.sort_values(["mode", "score"], ascending=[True, False]).reset_index(drop=True)
    out["mode"] = out["mode"].astype(str)
    return out


def generate_premium_line_pack(df: pd.DataFrame, total_lines: int = 5) -> pd.DataFrame:
    hist = enrich_history(df)
    base = generate_suggested_lines(hist, lines_per_mode=max(3, total_lines), seed=42)

    target_order = ["balanced", "safe", "anti_last_draw", "aggressive"]
    selected_rows: List[Dict[str, object]] = []
    used_balls_sets = []

    for mode in target_order:
        mode_rows = base[base["mode"] == mode].sort_values("score", ascending=False)
        for _, row in mode_rows.iterrows():
            balls_tuple = tuple(row["balls"].split())
            similarity_ok = True
            for prev in used_balls_sets:
                overlap = len(set(balls_tuple) & set(prev))
                if overlap >= 4:
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

    balanced = suggested[suggested["mode"] == "balanced"].sort_values("score", ascending=False)
    safe = suggested[suggested["mode"] == "safe"].sort_values("score", ascending=False)
    anti = suggested[suggested["mode"] == "anti_last_draw"].sort_values("score", ascending=False)
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

    if not anti.empty:
        row = anti.iloc[0].to_dict()
        return row, BestLineDecision(
            mode="anti_last_draw",
            reason="Chosen to reduce overlap with the most recent draw while keeping a strong score.",
        )

    row = aggressive.iloc[0].to_dict()
    return row, BestLineDecision(
        mode="aggressive",
        reason="Chosen as fallback from the highest available score.",
    )


def suggested_to_dataframe(suggested_rows: List[Dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(suggested_rows)


def build_dashboard_data(df: pd.DataFrame, premium_line_count: int = 5) -> Dict[str, object]:
    hist = enrich_history(df)
    main_rank = build_rank_table(hist, MAIN_RANGE, [f"ball_{i}" for i in range(1, 6)], "main")
    star_rank = build_rank_table(hist, STAR_RANGE, ["lucky_star_1", "lucky_star_2"], "star")
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

    return {
        "history_rows": len(hist),
        "latest_draw": latest_draw,
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
        "safe": "safe",
        "balanced": "balanced",
        "aggressive": "aggressive",
        "anti_last_draw": "anti",
    }
    labels = {
        "safe": "SAFE",
        "balanced": "BALANCED",
        "aggressive": "AGGRESSIVE",
        "anti_last_draw": "ANTI LAST DRAW",
    }
    cls = classes.get(mode, "balanced")
    label = labels.get(mode, mode.upper())
    return f'<span class="chip {cls}">{html.escape(label)}</span>'


def render_dashboard(data: Dict[str, object], refresh: RefreshResult) -> str:
    latest = data["latest_draw"]
    best = data["best_line"]
    state = data.get("refresh_state", {})

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
        [("mode", "Mode"), ("balls", "Main numbers"), ("stars", "Stars"), ("sum_balls", "Sum"), ("odd_even", "Odd-Even"), ("low_high", "Low-High"), ("score", "Score")],
    )

    refresh_text = f"{refresh.message} Added {refresh.draws_added} new draw(s)." if refresh.ok else refresh.message
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    balls_html = "".join(f'<span class="ball">{n:02d}</span>' for n in latest["balls"])
    stars_html = "".join(f'<span class="star">{n:02d}</span>' for n in latest["stars"])
    best_balls_html = "".join(f'<span class="ball hero-ball">{n}</span>' for n in str(best["balls"]).split())
    best_stars_html = "".join(f'<span class="star hero-star">{n}</span>' for n in str(best["stars"]).split())

    last_success_at = state.get("last_success_at", "-")
    last_attempt_at = state.get("last_attempt_at", "-")
    last_success_source = state.get("last_success_source", "-")

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
  --bg-0:#02060c;
  --bg-1:#07131a;
  --text:#dbfff5;
  --muted:#90b5ab;
  --neon:#00ff9c;
  --gold:#ffd54a;
  --safe:#0bcf7a;
  --balanced:#00d8ff;
  --aggr:#ff6b6b;
  --anti:#d18cff;
  --shadow:0 0 0 1px rgba(0,255,156,.08), 0 0 24px rgba(0,255,156,.08), inset 0 0 0 1px rgba(255,255,255,.02);
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0;
  color:var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  background:
    radial-gradient(circle at top left, rgba(0,255,156,.08), transparent 24%),
    radial-gradient(circle at top right, rgba(0,216,255,.08), transparent 24%),
    linear-gradient(180deg, var(--bg-0), var(--bg-1) 45%, var(--bg-0));
  min-height:100vh;
}}
.wrap {{ max-width: 1450px; margin: 0 auto; padding: 24px; }}
.grid {{ display:grid; gap:18px; }}
.top {{ grid-template-columns: 1.3fr .7fr; }}
.two {{ grid-template-columns: 1fr 1fr; }}
.three {{ grid-template-columns: 1fr 1fr 1fr; }}
.card {{
  background: linear-gradient(180deg, rgba(9,17,24,.94), rgba(5,11,16,.94));
  border:1px solid rgba(0,255,156,.12);
  border-radius: 22px;
  padding: 18px;
  box-shadow: var(--shadow);
}}
.hero-title {{ font-size: 40px; line-height:1; margin: 6px 0 10px; letter-spacing:-1px; }}
.sub {{ color: var(--muted); line-height:1.55; max-width: 950px; }}
.tiny {{ color: var(--muted); font-size: 12px; }}
.badge {{
  display:inline-flex; align-items:center; gap:8px;
  padding:8px 12px; border-radius:999px; font-size:12px; font-weight:700;
  border:1px solid rgba(0,255,156,.15); background:rgba(0,255,156,.06); color:var(--neon);
  text-transform:uppercase; letter-spacing:.08em;
}}
.section-title {{ font-size: 24px; margin: 0 0 12px; }}
.kpi-grid {{ display:grid; grid-template-columns: repeat(4,1fr); gap:12px; margin-top:16px; }}
.kpi {{ background:rgba(0,255,156,.04); border:1px solid rgba(0,255,156,.1); border-radius:16px; padding:12px; }}
.kpi .label {{ color:var(--muted); font-size:12px; }}
.kpi .value {{ font-size:19px; margin-top:5px; font-weight:800; }}
.balls {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
.ball,.star {{
  width:46px; height:46px; display:inline-flex; align-items:center; justify-content:center;
  border-radius:999px; font-weight:900; font-size:15px;
  border:1px solid rgba(255,255,255,.08);
}}
.ball {{ background:#ecfff8; color:#06110d; }}
.star {{ background:var(--gold); color:#342400; }}
.hero-line {{ display:flex; flex-wrap:wrap; gap:10px; margin: 14px 0; }}
.hero-ball,.hero-star {{ width:58px; height:58px; font-size:18px; }}
.best-meta {{ display:grid; grid-template-columns: repeat(4,1fr); gap:10px; margin-top:14px; }}
.best-meta .box {{ background:rgba(0,216,255,.04); border:1px solid rgba(0,216,255,.12); border-radius:14px; padding:10px; }}
.best-meta .box .v {{ font-weight:800; font-size:18px; margin-top:4px; }}
.chip {{ display:inline-flex; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; letter-spacing:.08em; }}
.chip.safe {{ background:rgba(11,207,122,.14); color:#8dffd0; }}
.chip.balanced {{ background:rgba(0,216,255,.14); color:#9befff; }}
.chip.aggressive {{ background:rgba(255,107,107,.14); color:#ffbaba; }}
.chip.anti {{ background:rgba(209,140,255,.16); color:#e7c7ff; }}
table {{ width:100%; border-collapse: collapse; }}
th, td {{ border-bottom:1px solid rgba(255,255,255,.06); padding:11px 10px; text-align:left; font-size:14px; }}
th {{ color:#b7ffe5; font-size:12px; letter-spacing:.08em; text-transform:uppercase; }}
.inline-cmd {{ background:rgba(0,255,156,.08); border:1px solid rgba(0,255,156,.12); border-radius:12px; padding:10px 12px; color:#c8ffea; overflow-wrap:anywhere; }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
.btn {{
  cursor:pointer; border:none; text-decoration:none;
  padding:12px 16px; border-radius:14px; font-weight:800;
  background:linear-gradient(180deg, rgba(0,255,156,.18), rgba(0,255,156,.08));
  color:var(--text); border:1px solid rgba(0,255,156,.18);
  display:inline-block;
}}
.btn.alt {{ background:linear-gradient(180deg, rgba(0,216,255,.14), rgba(0,216,255,.08)); border-color:rgba(0,216,255,.18); }}
.small-note {{ color:var(--muted); font-size:13px; line-height:1.5; }}
.footer {{ margin-top:18px; color:var(--muted); font-size:13px; line-height:1.6; }}
.mini-chart-title {{ font-size:14px; font-weight:800; margin-bottom:10px; color:#c8ffea; }}
.bar-row {{ display:grid; grid-template-columns: 58px 1fr 54px; gap:8px; align-items:center; margin-bottom:8px; }}
.bar-label {{ font-size:12px; color:#d9fff0; }}
.bar-track {{ height:10px; background:rgba(255,255,255,.06); border-radius:999px; overflow:hidden; }}
.bar-fill {{ height:100%; background:linear-gradient(90deg, rgba(0,255,156,.85), rgba(0,216,255,.85)); border-radius:999px; }}
.bar-value {{ font-size:12px; color:#b7ffe5; text-align:right; }}
@media (max-width: 1100px) {{
  .top, .two, .three {{ grid-template-columns: 1fr; }}
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
<div class=\"wrap\">

  <div class=\"card\">
    <div class=\"badge\">EuroMillions live premium model</div>
    <div class=\"hero-title\">EuroMillions premium analytics dashboard</div>
    <div class=\"sub\">Live refresh, XML primary source, HTML fallback, local cache safety, hot numbers, overdue numbers, frequent pairs, anti-last-draw lines, premium line pack, and lightweight charts.</div>

    <div class=\"kpi-grid\">
      <div class=\"kpi\"><div class=\"label\">Generated</div><div class=\"value\">{html.escape(generated)}</div></div>
      <div class=\"kpi\"><div class=\"label\">History range</div><div class=\"value\">{html.escape(str(data['history_start']))}<br><span class=\"tiny\">to {html.escape(str(data['history_end']))}</span></div></div>
      <div class=\"kpi\"><div class=\"label\">Stored draws</div><div class=\"value\">{data['history_rows']}</div></div>
      <div class=\"kpi\"><div class=\"label\">Premium lines shown</div><div class=\"value\">{html.escape(str(data['premium_line_count']))}</div></div>
    </div>
  </div>

  <div class=\"grid top\" style=\"margin-top:18px;\">
    <div class=\"card\">
      <div class=\"section-title\">Best line for next draw</div>
      <div>{mode_chip(str(data['best_line_mode']))}</div>
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
        <div class=\"box\"><div class=\"tiny\">Sum</div><div class=\"v\">{html.escape(str(best['sum_balls']))}</div></div>
        <div class=\"box\"><div class=\"tiny\">Odd-Even</div><div class=\"v\">{html.escape(str(best['odd_even']))}</div></div>
        <div class=\"box\"><div class=\"tiny\">Low-High</div><div class=\"v\">{html.escape(str(best['low_high']))}</div></div>
      </div>
      <p class=\"small-note\" style=\"margin-top:14px;\">{html.escape(str(data['best_line_reason']))}</p>
    </div>

    <div class=\"card\">
      <div class=\"section-title\">Sync / machine status</div>
      <p class=\"small-note\">{html.escape(refresh_text)}</p>
      <div class=\"tiny\">Last attempt: {html.escape(str(last_attempt_at))}</div>
      <div class=\"tiny\">Last success: {html.escape(str(last_success_at))}</div>
      <div class=\"tiny\">Last success source: {html.escape(str(last_success_source))}</div>
      <div class=\"actions\">
        <a class=\"btn\" href=\"/admin/refresh\">Open refresh JSON</a>
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
      <p class=\"small-note\"><strong>Avoid:</strong> copying the latest official draw into your next play.</p>
    </div>
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
    <strong>Model notes.</strong> Ball-sum mean in your history: <strong>{html.escape(str(data['sum_mean']))}</strong> | standard deviation: <strong>{html.escape(str(data['sum_std']))}</strong>
  </div>

</div>
</body>
</html>"""
