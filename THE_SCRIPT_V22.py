#!/usr/bin/env python3

# THE_SCRIPT_V22.py — Overline Edge (v22.0)
#
# JsonOdds + Rich TeamRankings stats (NFL/NCAAF/NBA/NCAAB) + MoneyPuck (NHL) +
# LEAGUE-INFO (ALL SHEETS) + ESPN/OpenWeather CSV weather +
# Online elevation fallback (Open-Meteo geocoding) +
# League-specific TEAM STATS blocks + narrative-ready per-game matchup rows +
# Diagnostics: weather_debug, elev_source, detailed data_missing.

import os
import re
import logging
import smtplib
import datetime
import warnings
from difflib import get_close_matches
from urllib.parse import urljoin

import requests
import pandas as pd
import numpy as np
from email.message import EmailMessage

# ========= GLOBAL =========

warnings.simplefilter(action="ignore", category=FutureWarning)
pd.options.mode.chained_assignment = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ========= EMAIL =========

EMAIL_FROM = "treyboiter@gmail.com"
EMAIL_TO = "treyboiter@gmail.com"
EMAIL_PW = "pjgwmtptccrxoigd"  # Gmail App Password

# ========= JSONODDS =========

JSONODDS_API_KEY = "52b605b5-5e92-4952-83ef-ca7472667df0"
JSONODDS_BASES = (
    "https://jsonodds.com/api/",
    "https://api.jsonodds.com/api/",
    "https://api.jsonodds.com/",
)

SPORT_MAP = {
    "NFL": "nfl",
    "NCAAF": "ncaaf",
    "NCAAB": "ncaab",
    "NBA": "nba",
    "NHL": "nhl",
}

REQUIRED_SPORT_SLUGS = set(SPORT_MAP.values())
SELECTED_BASE = None

# ========= FILES / OUTPUT =========

LEAGUE_INFO_CANDIDATES = ["LEAGUE-INFO.xlsx", "LEAGUE_INFO.xlsx", "league-info.xlsx"]

REPORT_ROOT = "reports"
os.makedirs(REPORT_ROOT, exist_ok=True)

# ESPN/OpenWeather CSV mapping
WEATHER_CSV_MAP = {
    "NFL": "NFL_{date}.csv",
    "NCAAF": "NCAA_FB_{date}.csv",
    "NCAAB": "NCAA_MB_{date}.csv",
    "NBA": "NBA_{date}.csv",
    "NHL": "NHL_{date}.csv",
}

_weather_cache: dict[str, pd.DataFrame] = {}


def _normalize_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


# ========= TEAM ALIASES =========

TEAM_ALIASES = {
    # College aliases (expand as needed)
    _normalize_name("MIAMI OH"): "Miami (OH)",
    _normalize_name("MIAMI-OH"): "Miami (OH)",
    _normalize_name("MIAMI (OH)"): "Miami (OH)",
    _normalize_name("UMASS LOWELL"): "UMass Lowell",
    _normalize_name("UMASS-LOWELL"): "UMass Lowell",
    _normalize_name("TEXAS STATE"): "Texas State Bobcats",
    _normalize_name("UCF"): "UCF Knights",
    _normalize_name("UTSA"): "UTSA Roadrunners",
    _normalize_name("USC"): "USC Trojans",
    _normalize_name("UCLA"): "UCLA Bruins",
    # Add specific NCAAB trouble spots here, e.g. Lipscomb / SE Missouri St, etc.

    # ... extend organically as mismatches show up ...
}

# NHL: JsonOdds full names -> MoneyPuck 3-letter abbreviations
NHL_TEAM_ABBR = {
    "Anaheim Ducks": "ANA",
    "Arizona Coyotes": "ARI",
    "Boston Bruins": "BOS",
    "Buffalo Sabres": "BUF",
    "Calgary Flames": "CGY",
    "Carolina Hurricanes": "CAR",
    "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL",
    "Columbus Blue Jackets": "CBJ",
    "Dallas Stars": "DAL",
    "Detroit Red Wings": "DET",
    "Edmonton Oilers": "EDM",
    "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK",
    "Minnesota Wild": "MIN",
    "Montreal Canadiens": "MTL",
    "Nashville Predators": "NSH",
    "New Jersey Devils": "NJD",
    "New York Islanders": "NYI",
    "New York Rangers": "NYR",
    "Ottawa Senators": "OTT",
    "Philadelphia Flyers": "PHI",
    "Pittsburgh Penguins": "PIT",
    "San Jose Sharks": "SJS",
    "St. Louis Blues": "STL",
    "Tampa Bay Lightning": "TBL",
    "Toronto Maple Leafs": "TOR",
    "Vancouver Canucks": "VAN",
    "Vegas Golden Knights": "VGK",
    "Washington Capitals": "WSH",
    "Winnipeg Jets": "WPG",
    "Seattle Kraken": "SEA",
}

# ========= SLATE DATE (ET) =========

SLATE_DATE_ENV = os.getenv("SLATE_DATE", "").strip()


def _today_et_date():
    return pd.Timestamp.now(tz="America/New_York").date()


def _target_slate_date():
    if SLATE_DATE_ENV:
        try:
            return datetime.date.fromisoformat(SLATE_DATE_ENV)
        except Exception:
            logger.warning("Invalid SLATE_DATE '%s'; defaulting to today ET", SLATE_DATE_ENV)
    return _today_et_date()


TARGET_SLATE_DATE = _target_slate_date()
logger.info("Target slate date (ET): %s", TARGET_SLATE_DATE.isoformat())

# ========= MODEL PARAMS =========

TOTAL_BANDS = {
    "NBA": (150.0, 260.0, 0.5),
    "NCAAB": (110.0, 190.0, 0.5),
    "NFL": (25.0, 75.0, 0.5),
    "NCAAF": (35.0, 90.0, 0.5),
    "NHL": (4.0, 9.0, 0.5),
}

LEAGUE_TOT_STD = {"NBA": 12.0, "NCAAB": 14.0, "NFL": 8.0, "NCAAF": 12.0, "NHL": 1.2}
LEAGUE_EDGE_THRESH = {"NBA": 3.0, "NCAAB": 3.0, "NFL": 2.0, "NCAAF": 2.5, "NHL": 0.6}


def _snap_total(league: str, v):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    lo, hi, step = TOTAL_BANDS.get(league, (10.0, 300.0, 0.5))
    x = round(round(x / step) * step, 2)
    if not (lo <= x <= hi):
        return None
    return x


def _median_or_none(seq):
    seq = [x for x in seq if x is not None and not (isinstance(x, float) and np.isnan(x))]
    if not seq:
        return None
    return float(np.median(seq))


# ========= TEAMRANKINGS URLS =========

STATS_URLS = {
    "NBA": {
        "pts_pg": "https://www.teamrankings.com/nba/stat/points-per-game",
        "off_eff": "https://www.teamrankings.com/nba/stat/offensive-efficiency",
        "floor_pct": "https://www.teamrankings.com/nba/stat/floor-percentage",
        "paint_pts": "https://www.teamrankings.com/nba/stat/points-in-paint-per-game",
        "pct_pts_2": "https://www.teamrankings.com/nba/stat/percent-of-points-from-2-pointers",
        "pct_pts_3": "https://www.teamrankings.com/nba/stat/percent-of-points-from-3-pointers",
        "pct_pts_ft": "https://www.teamrankings.com/nba/stat/percent-of-points-from-free-throws",
        "eFG%": "https://www.teamrankings.com/nba/stat/effective-field-goal-pct",
        "ftm_per_100": "https://www.teamrankings.com/nba/stat/ftm-per-100-possessions",
        "treb%": "https://www.teamrankings.com/nba/stat/total-rebounding-percentage",
        "ast_to": "https://www.teamrankings.com/nba/stat/assist--per--turnover-ratio",
        "fouls_per_poss": "https://www.teamrankings.com/nba/stat/personal-fouls-per-possession",
        "opp_pts_pg": "https://www.teamrankings.com/nba/stat/opponent-points-per-game",
        "opp_floor_pct": "https://www.teamrankings.com/nba/stat/opponent-floor-percentage",
        "opp_pct_pts_2": "https://www.teamrankings.com/nba/stat/opponent-percent-of-points-from-2-pointers",
        "opp_pct_pts_3": "https://www.teamrankings.com/nba/stat/opponent-percent-of-points-from-3-pointers",
        "opp_pct_pts_ft": "https://www.teamrankings.com/nba/stat/opponent-percent-of-points-from-free-throws",
        "opp_eFG%": "https://www.teamrankings.com/nba/stat/opponent-effective-field-goal-pct",
        "opp_ftm_per_100": "https://www.teamrankings.com/nba/stat/opponent-ftm-per-100-possessions",
        "opp_ast_to": "https://www.teamrankings.com/nba/stat/opponent-assist--per--turnover-ratio",
        "opp_fouls_per_poss": "https://www.teamrankings.com/nba/stat/opponent-personal-fouls-per-possession",
        "pace": "https://www.teamrankings.com/nba/stat/possessions-per-game",
        "extra_chances": "https://www.teamrankings.com/nba/stat/extra-chances-per-game",
    },
    "NCAAB": {
        "pace": "https://www.teamrankings.com/ncaa-basketball/stat/possessions-per-game",
        "off_eff": "https://www.teamrankings.com/ncaa-basketball/stat/offensive-efficiency",
        "def_eff": "https://www.teamrankings.com/ncaa-basketball/stat/defensive-efficiency",
        "eFG%": "https://www.teamrankings.com/ncaa-basketball/stat/effective-field-goal-pct",
        "3P%": "https://www.teamrankings.com/ncaa-basketball/stat/three-point-pct",
        "FTr": "https://www.teamrankings.com/ncaa-basketball/stat/free-throws-per-field-goal-attempt",
    },
    "NFL": {
        "off_ppg": "https://www.teamrankings.com/nfl/stat/points-per-game",
        "def_ppg": "https://www.teamrankings.com/nfl/stat/opponent-points-per-game",
        "red_zone_scoring_pct": "https://www.teamrankings.com/nfl/stat/red-zone-scoring-pct",
        "yards_per_play": "https://www.teamrankings.com/nfl/stat/yards-per-play",
        "sec_play": "https://www.teamrankings.com/nfl/stat/seconds-per-play",
        "third_down_conversion_pct": "https://www.teamrankings.com/nfl/stat/third-down-conversion-pct",
        "rushing_play_pct": "https://www.teamrankings.com/nfl/stat/rushing-play-pct",
        "yards_per_rush_attempt": "https://www.teamrankings.com/nfl/stat/yards-per-rush-attempt",
        "completion_pct": "https://www.teamrankings.com/nfl/stat/completion-pct",
        "qb_sacked_pct": "https://www.teamrankings.com/nfl/stat/qb-sacked-pct",
        "pass%": "https://www.teamrankings.com/nfl/stat/passing-play-pct",
        "yards_per_pass_attempt": "https://www.teamrankings.com/nfl/stat/yards-per-pass-attempt",
        "opp_red_zone_scoring_pct": "https://www.teamrankings.com/nfl/stat/opponent-red-zone-scoring-pct",
        "opp_yards_per_play": "https://www.teamrankings.com/nfl/stat/opponent-yards-per-play",
        "opp_third_down_conversion_pct": "https://www.teamrankings.com/nfl/stat/opponent-third-down-conversion-pct",
        "opp_yards_per_rush_attempt": "https://www.teamrankings.com/nfl/stat/opponent-yards-per-rush-attempt",
        "opp_completion_pct": "https://www.teamrankings.com/nfl/stat/opponent-completion-pct",
        "opp_yards_per_pass_attempt": "https://www.teamrankings.com/nfl/stat/opponent-yards-per-pass-attempt",
        "penalty_yards_per_game": "https://www.teamrankings.com/nfl/stat/penalty-yards-per-game",
    },
    "NCAAF": {
        "off_ppg": "https://www.teamrankings.com/college-football/stat/points-per-game",
        "def_ppg": "https://www.teamrankings.com/college-football/stat/opponent-points-per-game",
        "red_zone_scoring_pct": "https://www.teamrankings.com/college-football/stat/red-zone-scoring-pct",
        "yards_per_play": "https://www.teamrankings.com/college-football/stat/yards-per-play",
        "sec_play": "https://www.teamrankings.com/college-football/stat/seconds-per-play",
        "third_down_conversion_pct": "https://www.teamrankings.com/college-football/stat/third-down-conversion-pct",
        "rushing_play_pct": "https://www.teamrankings.com/college-football/stat/rushing-play-pct",
        "yards_per_rush_attempt": "https://www.teamrankings.com/college-football/stat/yards-per-rush-attempt",
        "completion_pct": "https://www.teamrankings.com/college-football/stat/completion-pct",
        "qb_sacked_pct": "https://www.teamrankings.com/college-football/stat/qb-sacked-pct",
        "pass%": "https://www.teamrankings.com/college-football/stat/passing-play-pct",
        "yards_per_pass_attempt": "https://www.teamrankings.com/college-football/stat/yards-per-pass-attempt",
        "opp_red_zone_scoring_pct": "https://www.teamrankings.com/college-football/stat/opponent-red-zone-scoring-pct",
        "opp_yards_per_play": "https://www.teamrankings.com/college-football/stat/opponent-yards-per-play",
        "opp_third_down_conversion_pct": "https://www.teamrankings.com/college-football/stat/opponent-third-down-conversion-pct",
        "opp_yards_per_rush_attempt": "https://www.teamrankings.com/college-football/stat/opponent-yards-per-rush-attempt",
        "opp_completion_pct": "https://www.teamrankings.com/college-football/stat/opponent-completion-pct",
        "opp_yards_per_pass_attempt": "https://www.teamrankings.com/college-football/stat/opponent-yards-per-pass-attempt",
        "penalty_yards_per_game": "https://www.teamrankings.com/college-football/stat/penalty-yards-per-game",
    },
}


def _pull_tr_table(url: str, stat_name: str) -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    dfs = pd.read_html(r.text, attrs={"class": "tr-table"})
    if not dfs:
        return pd.DataFrame(columns=["Team", stat_name])
    df = dfs[0]
    val_col = df.columns[2] if len(df.columns) > 2 else df.columns[-1]
    df = df[["Team", val_col]].copy()
    df.columns = ["Team", stat_name]
    df["Team"] = df["Team"].astype(str).apply(lambda x: re.sub(r"\s+\(.*\)", "", x).strip())
    return df


# ========= NHL STATS FROM MONEYPUCK =========

def _nhl_season_year_for_date(d: datetime.date) -> int:
    return d.year


def fetch_nhl_stats_moneypuck() -> pd.DataFrame:
    season_year = _nhl_season_year_for_date(TARGET_SLATE_DATE)
    url = f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{season_year}/regular/teams.csv"
    logger.info(f"--- Fetching NHL team stats from MoneyPuck {season_year} ---")
    try:
        df = pd.read_csv(url)
    except Exception as e:
        logger.error(f"NHL MoneyPuck teams.csv fetch failed: {e}")
        return pd.DataFrame()

    cols_lower = {c.lower(): c for c in df.columns}

    def _find_col(candidates):
        for cand in candidates:
            cl = cand.lower()
            if cl in cols_lower:
                return cols_lower[cl]
            for k, v in cols_lower.items():
                if cl == k or cl in k:
                    return v
        return None

    team_col = _find_col(["team"])
    gp_col = _find_col(["games_played", "games"])
    gf_col = _find_col(["goalsFor", "goals_for", "goalsfor", "goals"])
    ga_col = _find_col(["goalsAgainst", "goals_against", "goalsagainst"])
    if not all([team_col, gp_col, gf_col, ga_col]):
        logger.error(
            "NHL MoneyPuck teams.csv missing expected columns (team/gp/gf/ga); "
            f"got: {list(df.columns)[:10]}"
        )
        return pd.DataFrame()

    df = df[[team_col, gp_col, gf_col, ga_col]].copy()
    df.columns = ["abbr", "games", "goals_for", "goals_against"]
    df["games"] = pd.to_numeric(df["games"], errors="coerce").replace(0, np.nan)
    df["goals_for"] = pd.to_numeric(df["goals_for"], errors="coerce")
    df["goals_against"] = pd.to_numeric(df["goals_against"], errors="coerce")
    df["off_gpg"] = df["goals_for"] / df["games"]
    df["def_gpg"] = df["goals_against"] / df["games"]

    rows = []
    for full_name, abbr in NHL_TEAM_ABBR.items():
        row = df[df["abbr"].astype(str).str.upper() == abbr]
        if row.empty:
            continue
        r = row.iloc[0].to_dict()
        r["Team"] = full_name
        rows.append(r)

    if not rows:
        logger.error("NHL MoneyPuck: no rows matched NHL_TEAM_ABBR mapping.")
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    logger.info(f"NHL MoneyPuck stats loaded for {len(out)} teams.")
    return out


# ========= STATS FETCH DISPATCH =========

def fetch_live_stats(league: str) -> pd.DataFrame:
    if league == "NHL":
        return fetch_nhl_stats_moneypuck()
    urls = STATS_URLS.get(league, {})
    if not urls:
        return pd.DataFrame()
    logger.info(f"--- Fetching Live Stats for {league} ---")
    merged = pd.DataFrame()
    for stat_name, url in urls.items():
        try:
            df = _pull_tr_table(url, stat_name)
            merged = df if merged.empty else pd.merge(merged, df, on="Team", how="outer")
        except Exception as e:
            logger.warning(f"{league} stat {stat_name} failed: {e}")
            continue
    logger.info(f"Stats loaded for {len(merged)} teams.")
    return merged


def get_team_stats(team_name: str, stats_df: pd.DataFrame) -> dict:
    if stats_df.empty:
        return {}
    row = stats_df[stats_df["Team"].str.lower() == str(team_name).lower()]
    if row.empty:
        matches = get_close_matches(str(team_name), stats_df["Team"].tolist(), n=1, cutoff=0.6)
        if matches:
            row = stats_df[stats_df["Team"] == matches[0]]
    return row.iloc[0].to_dict() if not row.empty else {}


# ========= JSONODDS HELPERS =========

def _jo_headers():
    return {
        "x-api-key": JSONODDS_API_KEY,
        "Accept": "application/json",
        "User-Agent": "OverlineEdge/1.0",
    }


def jo_request(path: str, params: dict | None = None, allow_probe: bool = False):
    global SELECTED_BASE
    params = params or {}
    bases = (SELECTED_BASE,) if SELECTED_BASE and not allow_probe else JSONODDS_BASES
    last_err = None
    for base in bases:
        url = urljoin(base, path)
        try:
            r = requests.get(url, headers=_jo_headers(), params=params, timeout=20)
            if r.status_code == 403:
                last_err = "403 Forbidden"
                logger.warning(f"JsonOdds 403 @ {url}")
                continue
            r.raise_for_status()
            if allow_probe and not SELECTED_BASE:
                SELECTED_BASE = base
                logger.info(f"Preflight selected base: {SELECTED_BASE}")
            return r.json()
        except Exception as e:
            last_err = str(e)
            logger.warning(f"JsonOdds request failed for {url}: {e}")
    logger.error(f"JsonOdds request failed for path '{path}': {last_err or 'no response'}")
    return None


def jsonodds_preflight():
    logger.info("=== JSONODDS PREFLIGHT ===")
    usage = jo_request("usage", allow_probe=True)
    if usage is None:
        logger.error("Preflight: /usage failed — check key/plan or connectivity.")
        return False, set()
    logger.info("Preflight OK: /usage responded (key active)")
    sports = jo_request("sports")
    available = set()
    if isinstance(sports, dict):
        available = set(str(v).lower() for v in sports.values())
    elif isinstance(sports, list):
        available = set(str(x).lower() for x in sports)
    for slug in sorted(REQUIRED_SPORT_SLUGS):
        logger.info(f"Preflight sport '{slug}': {'OK' if slug in available else 'MISSING'}")
    return True, available


# ========= ODDS PARSING =========

def _parse_start_times(raw):
    if not raw:
        return (None, None)
    try:
        dt_utc = pd.to_datetime(raw, utc=True)
        dt_et = dt_utc.tz_convert("America/New_York")
        return (dt_utc.isoformat(), dt_et.isoformat())
    except Exception:
        try:
            any_dt = pd.to_datetime(raw)
            if any_dt.tzinfo is None:
                any_dt = any_dt.tz_localize("UTC")
            return (
                any_dt.tz_convert("UTC").isoformat(),
                any_dt.tz_convert("America/New_York").isoformat(),
            )
        except Exception:
            return (None, None)


def _parse_matches_to_games(league: str, matches):
    games = []
    if not isinstance(matches, list):
        return games
    for m in matches:
        try:
            away = m.get("AwayTeam") or m.get("awayTeam")
            home = m.get("HomeTeam") or m.get("homeTeam")
            if not away or not home:
                continue
            start_utc, start_et = _parse_start_times(
                m.get("MatchTime")
                or m.get("matchTime")
                or m.get("GameTime")
                or m.get("StartTime")
            )

            odds_list = m.get("Odds") or m.get("odds") or []
            if not isinstance(odds_list, list):
                odds_list = []

            totals, over_prices, under_prices = [], [], []
            spr_a, spr_a_price, spr_h, spr_h_price = [], [], [], []
            ml_a, ml_h = [], []

            for o in odds_list:
                if not isinstance(o, dict):
                    continue
                tv = _snap_total(
                    league,
                    o.get("TotalNumber")
                    or o.get("OverUnder")
                    or o.get("Total")
                    or o.get("TotalPoints"),
                )
                if tv is not None:
                    totals.append(tv)

                for key, bucket in (("OverLine", over_prices), ("UnderLine", under_prices)):
                    try:
                        v = o.get(key)
                        if v is not None:
                            bucket.append(float(v))
                    except Exception:
                        pass

                for key, bucket in (
                    ("PointSpreadAway", spr_a),
                    ("PointSpreadHome", spr_h),
                    ("PointSpreadAwayLine", spr_a_price),
                    ("PointSpreadHomeLine", spr_h_price),
                ):
                    try:
                        v = o.get(key)
                        if v is not None:
                            bucket.append(float(v))
                    except Exception:
                        pass

                for key, bucket in (("MoneyLineAway", ml_a), ("MoneyLineHome", ml_h)):
                    try:
                        v = o.get(key)
                        if v is not None:
                            bucket.append(float(v))
                    except Exception:
                        pass

            game = {
                "away": away,
                "home": home,
                "start_time_utc": start_utc,
                "start_time_et": start_et,
                "spread_away_line": _median_or_none(spr_a),
                "spread_away_price": _median_or_none(spr_a_price),
                "spread_home_line": _median_or_none(spr_h),
                "spread_home_price": _median_or_none(spr_h_price),
                "total_line": _median_or_none(totals),
                "over_price": _median_or_none(over_prices),
                "under_price": _median_or_none(under_prices),
                "ml_away": _median_or_none(ml_a),
                "ml_home": _median_or_none(ml_h),
                "book_count": len(odds_list),
            }
            games.append(game)
        except Exception:
            continue
    return games


def _filter_to_slate_date_et(games: list[dict], slate_date: datetime.date, league: str):
    kept, dropped = [], 0
    for g in games:
        et_raw = g.get("start_time_et")
        if not et_raw:
            dropped += 1
            continue
        try:
            dt_et = pd.to_datetime(et_raw)
            if dt_et.tzinfo is None:
                dt_et = dt_et.tz_localize("America/New_York")
            if dt_et.tz_convert("America/New_York").date() == slate_date:
                kept.append(g)
            else:
                dropped += 1
        except Exception:
            dropped += 1
    logger.info("%s ET-date kept %d on %s (dropped %d)", league, len(kept), slate_date, dropped)
    return kept


def _fetch_with_params(league: str, params: dict, label: str):
    sport_key = SPORT_MAP.get(league)
    if not sport_key:
        return []
    payload = jo_request(f"odds/{sport_key}", params=params)
    matches = payload.get("matches") if isinstance(payload, dict) else payload
    games = _parse_matches_to_games(league, matches)
    logger.info("%s fetch (%s): %d events", league, label, len(games))
    return games


def fetch_jsonodds_games(league: str):
    games = _fetch_with_params(league, {"oddType": "Game"}, "oddType=Game")
    games = _filter_to_slate_date_et(games, TARGET_SLATE_DATE, league)
    strict = [g for g in games if g.get("total_line") is not None]
    if len(strict) < len(games):
        logger.info("%s STRICT drop missing totals: %d", league, len(games) - len(strict))
    games = strict
    if games:
        return games

    logger.info("%s: empty after Attempt#1; trying date=%s", league, TARGET_SLATE_DATE)
    games2 = _fetch_with_params(
        league, {"oddType": "Game", "date": TARGET_SLATE_DATE.isoformat()}, "date & oddType"
    )
    games2 = _filter_to_slate_date_et(games2, TARGET_SLATE_DATE, league)
    games2 = [g for g in games2 if g.get("total_line") is not None]
    if games2:
        return games2

    logger.info("%s: still empty; trying window daysFrom=-1, daysTo=1", league)
    games3 = _fetch_with_params(
        league, {"oddType": "Game", "daysFrom": -1, "daysTo": 1}, "window"
    )
    games3 = _filter_to_slate_date_et(games3, TARGET_SLATE_DATE, league)
    games3 = [g for g in games3 if g.get("total_line") is not None]
    return games3


# ========= LEAGUE-INFO / VENUE =========

def load_league_info() -> pd.DataFrame:
    all_rows = []
    used_files = []
    for fname in LEAGUE_INFO_CANDIDATES:
        path = os.path.join(os.path.dirname(__file__), fname)
        if not os.path.exists(path):
            continue
        try:
            sheets = pd.read_excel(path, sheet_name=None)
            for sheet_name, tmp in sheets.items():
                if tmp is None or tmp.empty:
                    continue
                tmp.columns = [str(c).strip().upper() for c in tmp.columns]
                if "TEAM" not in tmp.columns:
                    team_col = next((c for c in tmp.columns if "TEAM" in c), tmp.columns[0])
                    tmp = tmp.rename(columns={team_col: "TEAM"})
                elev_col = next((c for c in tmp.columns if "ELEV" in c), None)
                if elev_col:
                    tmp["ELEVATION"] = pd.to_numeric(tmp[elev_col], errors="coerce")
                lat_col = next((c for c in tmp.columns if "Y AXIS" in c or "LAT" in c), None)
                lon_col = next(
                    (c for c in tmp.columns if "X AXIS" in c or "LON" in c or "LONG" in c), None
                )
                if lat_col:
                    tmp["LAT"] = pd.to_numeric(tmp[lat_col], errors="coerce")
                if lon_col:
                    tmp["LON"] = -pd.to_numeric(tmp[lon_col], errors="coerce")
                tmp["TEAM_KEY"] = tmp["TEAM"].apply(_normalize_name)
                tmp["SOURCE_SHEET"] = sheet_name
                all_rows.append(tmp)
            used_files.append(fname)
            break
        except Exception as e:
            logger.info(f"Failed loading {fname}: {e}")
    if not all_rows:
        logger.warning("No LEAGUE-INFO sheets loaded; physics/weather will be limited.")
        return pd.DataFrame()
    league_df = pd.concat(all_rows, ignore_index=True)
    logger.info(
        f"Loaded league info from {used_files[0]}: {len(league_df)} rows across {len(all_rows)} sheets"
    )
    return league_df


def _find_venue_row(league_df: pd.DataFrame, team: str):
    if league_df.empty or "TEAM" not in league_df.columns:
        return None
    key = _normalize_name(team)
    if key in TEAM_ALIASES:
        key = _normalize_name(TEAM_ALIASES[key])
    if "TEAM_KEY" in league_df.columns:
        row = league_df[league_df["TEAM_KEY"] == key]
        if not row.empty:
            return row.iloc[0]
    mask = league_df["TEAM"].astype(str).str.contains(
        re.escape(str(team)), case=False, na=False, regex=True
    )
    row = league_df[mask]
    if not row.empty:
        return row.iloc[0]
    return None


def _extract_lat_lon_from_row(row):
    if row is None:
        return (None, None)
    if "LAT" not in row or "LON" not in row:
        return (None, None)
    try:
        lat = float(row["LAT"])
        lon = float(row["LON"])
        if np.isnan(lat) or np.isnan(lon):
            return (None, None)
        return (lat, lon)
    except Exception:
        return (None, None)


# ========= ONLINE ELEVATION (Open-Meteo Geocoding) =========

_elev_cache: dict[str, float] = {}


def fetch_elevation_online(place_name: str) -> float | None:
    key = place_name.strip().lower()
    if key in _elev_cache:
        return _elev_cache[key]
    if not place_name:
        _elev_cache[key] = None
        return None
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": place_name, "count": 1, "language": "en", "format": "json"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            _elev_cache[key] = None
            return None
        elev = results[0].get("elevation")
        if elev is None:
            _elev_cache[key] = None
            return None
        _elev_cache[key] = float(elev)
        return _elev_cache[key]
    except Exception as e:
        logger.warning(f"Online elevation lookup failed for '{place_name}': {e}")
        _elev_cache[key] = None
        return None


# ========= WEATHER LOOKUP FROM CSV (WITH DIAGNOSTICS) =========

def _load_weather_df_for_league(league: str) -> pd.DataFrame:
    if league in _weather_cache:
        return _weather_cache[league]
    template = WEATHER_CSV_MAP.get(league)
    if not template:
        _weather_cache[league] = pd.DataFrame()
        return _weather_cache[league]
    fname = template.format(date=TARGET_SLATE_DATE.isoformat())
    path = os.path.join(os.path.dirname(__file__), fname)
    if not os.path.exists(path):
        logger.warning(f"Weather CSV missing for {league}: {fname}")
        _weather_cache[league] = pd.DataFrame()
        return _weather_cache[league]
    try:
        df = pd.read_csv(path)
        for col in ("teams", "kickoff_est"):
            if col not in df.columns:
                df[col] = ""
        _weather_cache[league] = df
        logger.info(f"Loaded weather CSV for {league}: {fname} ({len(df)} rows)")
        return df
    except Exception as e:
        logger.error(f"Error loading weather CSV for {league}: {e}")
        _weather_cache[league] = pd.DataFrame()
        return _weather_cache[league]


def _normalize_team_for_match(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def fetch_weather_from_csv(league: str, game: dict):
    """
    Returns:
        temp_c, wind_mps, extra, debug_reason
    debug_reason:
        '' on success
        'NO_CSV_FILE', 'NO_TEAM_MATCH', 'MISSING_TEMP_OR_WIND'
    """
    df = _load_weather_df_for_league(league)
    if df.empty:
        return None, None, {}, "NO_CSV_FILE"

    raw_away = str(game["away"])
    raw_home = str(game["home"])

    norm_away_key = _normalize_team_for_match(raw_away)
    norm_home_key = _normalize_team_for_match(raw_home)

    if norm_away_key in TEAM_ALIASES:
        away = TEAM_ALIASES[norm_away_key]
    else:
        away = raw_away

    if norm_home_key in TEAM_ALIASES:
        home = TEAM_ALIASES[norm_home_key]
    else:
        home = raw_home

    away_key = _normalize_team_for_match(away)
    home_key = _normalize_team_for_match(home)

    try:
        game_dt = pd.to_datetime(game.get("start_time_et")).tz_convert("America/New_York")
    except Exception:
        game_dt = None

    candidates = []
    for _, row in df.iterrows():
        teams_str = str(row.get("teams", ""))
        t_norm = _normalize_team_for_match(teams_str)
        if away_key not in t_norm or home_key not in t_norm:
            continue
        try:
            ke = str(row.get("kickoff_est", ""))
            ke_dt = pd.to_datetime(ke)
            if ke_dt.tzinfo is None:
                ke_dt = ke_dt.tz_localize("America/New_York")
            same_day = ke_dt.date() == TARGET_SLATE_DATE
        except Exception:
            same_day = False
        if not same_day:
            continue
        if game_dt is not None:
            diff = abs((ke_dt - game_dt).total_seconds())
        else:
            diff = 999999
        candidates.append((diff, row))

    if not candidates:
        return None, None, {}, "NO_TEAM_MATCH"

    candidates.sort(key=lambda x: x[0])
    best = candidates[0][1]

    temp_f = best.get("temp_f")
    wind_mph = best.get("wind_mph")
    pressure_mb = best.get("pressure_mb")
    humidity_pct = best.get("humidity_pct")
    cloud_pct = best.get("cloud_cover_pct")
    lat = best.get("latitude")
    lon = best.get("longitude")

    if pd.isna(temp_f) or pd.isna(wind_mph):
        return None, None, {}, "MISSING_TEMP_OR_WIND"

    temp_f_val = float(temp_f)
    temp_c = (temp_f_val - 32.0) * 5.0 / 9.0
    wind_mps = float(wind_mph) * 0.44704

    extra = {
        "temp_f": temp_f_val,
        "wind_mph": float(wind_mph),
        "pressure_mb": float(pressure_mb) if pressure_mb == pressure_mb else None,
        "humidity_pct": float(humidity_pct) if humidity_pct == humidity_pct else None,
        "cloud_cover_pct": float(cloud_pct) if cloud_pct == cloud_pct else None,
        "lat": float(lat) if lat == lat else None,
        "lon": float(lon) if lon == lon else None,
    }

    return temp_c, wind_mps, extra, ""


# ========= MODEL =========

def run_model(league: str, game: dict, stats_df: pd.DataFrame, league_df: pd.DataFrame) -> dict:
    home = game["home"]
    away = game["away"]

    h_stats = get_team_stats(home, stats_df)
    a_stats = get_team_stats(away, stats_df)

    missing: list[str] = []
    stats_ok = True

    if league in ("NBA", "NCAAB"):
        if not h_stats or "pace" not in h_stats or "off_eff" not in h_stats:
            missing.append("STATS_HOME")
            stats_ok = False
        if not a_stats or "pace" not in a_stats or "off_eff" not in a_stats:
            missing.append("STATS_AWAY")
            stats_ok = False
    elif league in ("NFL", "NCAAF"):
        if not h_stats or "off_ppg" not in h_stats or "def_ppg" not in h_stats:
            missing.append("STATS_HOME")
            stats_ok = False
        if not a_stats or "off_ppg" not in a_stats or "def_ppg" not in a_stats:
            missing.append("STATS_AWAY")
            stats_ok = False
    elif league == "NHL":
        if not h_stats or "off_gpg" not in h_stats or "def_gpg" not in h_stats:
            missing.append("STATS_HOME")
            stats_ok = False
        if not a_stats or "off_gpg" not in a_stats or "def_gpg" not in a_stats:
            missing.append("STATS_AWAY")
            stats_ok = False

    if league in ("NBA", "NCAAB", "NFL", "NCAAF"):
        for k, v in h_stats.items() if h_stats else []:
            game[f"h_{k}"] = v
        for k, v in a_stats.items() if a_stats else []:
            game[f"a_{k}"] = v
    elif league == "NHL":
        if h_stats:
            game["h_off_gpg"] = h_stats.get("off_gpg")
            game["h_def_gpg"] = h_stats.get("def_gpg")
        if a_stats:
            game["a_off_gpg"] = a_stats.get("off_gpg")
            game["a_def_gpg"] = a_stats.get("def_gpg")

    # Venue / elevation with source diagnostics
    row = _find_venue_row(league_df, home)
    elev_ft = None
    elev_source = "MISSING"
    if row is None:
        missing.append("VENUE_ROW_MISSING")
    else:
        elev_val = row.get("ELEVATION")
        if elev_val is None or (isinstance(elev_val, float) and np.isnan(elev_val)):
            city_name = str(row.get("CITY", "")).strip()
            elev_ft = fetch_elevation_online(city_name) if city_name else None
            elev_source = "ONLINE" if elev_ft is not None else "MISSING"
        else:
            try:
                elev_ft = float(elev_val)
                elev_source = "LEAGUE_INFO"
            except Exception:
                elev_ft = None
                elev_source = "BAD_VALUE"

    # Weather from CSV (with diagnostics)
    temp_c, wind_mps, w_extra, wx_reason = fetch_weather_from_csv(league, game)
    weather_ok = temp_c is not None and wind_mps is not None
    if not weather_ok:
        missing.append("WEATHER")

    game["weather_debug"] = wx_reason
    game["temp_c"] = temp_c
    game["temp_f"] = w_extra.get("temp_f")
    game["wind_mps"] = wind_mps
    game["wind_mph"] = w_extra.get("wind_mph")
    game["pressure_mb"] = w_extra.get("pressure_mb")
    game["humidity_pct"] = w_extra.get("humidity_pct")
    game["cloud_cover_pct"] = w_extra.get("cloud_cover_pct")
    game["venue_elev_ft"] = elev_ft
    game["elev_source"] = elev_source
    game["weather_ok"] = weather_ok

    if not stats_ok:
        game["predicted_total"] = np.nan
        game["edge"] = np.nan
        game["conf"] = np.nan
        game["pick"] = "NO PICK - MISSING: " + ",".join(sorted(set(missing)))
        game["nes"] = np.nan
        game["data_missing"] = missing
        return game

    # Baseline prediction
    if league in ("NBA", "NCAAB"):
        h_pace = float(h_stats["pace"])
        a_pace = float(a_stats["pace"])
        pace = (h_pace + a_pace) / 2.0
        h_off = float(h_stats["off_eff"])
        a_off = float(a_stats["off_eff"])
        if h_off < 10 and a_off < 10:
            pred = pace * (h_off + a_off)
        else:
            pred = pace * (h_off + a_off) / 100.0
    elif league in ("NFL", "NCAAF"):
        h_off = float(h_stats["off_ppg"])
        a_off = float(a_stats["off_ppg"])
        pred = h_off + a_off
    elif league == "NHL":
        h_off = float(h_stats["off_gpg"])
        a_off = float(a_stats["off_gpg"])
        pred = h_off + a_off
    else:
        pred = 0.0

    total_market = game.get("total_line")
    if total_market is None or (isinstance(total_market, float) and np.isnan(total_market)):
        game["predicted_total"] = pred
        game["edge"] = np.nan
        game["conf"] = np.nan
        game["pick"] = "NO PICK - MISSING: TOTAL_MARKET"
        game["nes"] = np.nan
        game["data_missing"] = missing
        return game

    edge = float(pred) - float(total_market)
    std = LEAGUE_TOT_STD.get(league, 10.0)
    z = edge / std if std else 0.0
    conf = abs(z)
    game["predicted_total"] = pred
    game["edge"] = edge
    game["conf"] = conf
    game["nes"] = edge / std if std else np.nan

    if not weather_ok:
        game["pick"] = "NO PICK - MISSING: WEATHER"
    else:
        thresh = LEAGUE_EDGE_THRESH.get(league, 2.0)
        if abs(edge) < thresh:
            game["pick"] = "NO PICK - EDGE TOO SMALL"
        else:
            game["pick"] = "OVER" if edge > 0 else "UNDER"

    game["data_missing"] = missing
    return game


# ========= OUTPUT: MATCHUP-FIRST FORMAT =========

def format_games_for_output(league: str, games: list[dict]) -> pd.DataFrame:
    if not games:
        return pd.DataFrame()

    base_cols = [
        "away",
        "home",
        "start_time_utc",
        "start_time_et",
        "spread_away_line",
        "spread_away_price",
        "spread_home_line",
        "spread_home_price",
        "total_line",
        "over_price",
        "under_price",
        "ml_away",
        "ml_home",
        "book_count",
    ]

    weather_cols = [
        "temp_f",
        "temp_c",
        "wind_mph",
        "wind_mps",
        "pressure_mb",
        "humidity_pct",
        "cloud_cover_pct",
        "venue_elev_ft",
        "elev_source",
        "weather_ok",
        "weather_debug",
    ]

    model_cols = ["predicted_total", "edge", "conf", "nes", "pick", "data_missing"]

    all_keys = set().union(*[g.keys() for g in games])
    home_cols = sorted([k for k in all_keys if k.startswith("h_")])
    away_cols = sorted([k for k in all_keys if k.startswith("a_")])

    col_order = base_cols + weather_cols + model_cols + home_cols + away_cols

    rows = []
    for g in games:
        row = {c: g.get(c) for c in col_order}
        rows.append(row)

    df = pd.DataFrame(rows, columns=col_order)
    return df


def save_league_report(league: str, games: list[dict]):
    if not games:
        logger.info(f"No games for {league} on {TARGET_SLATE_DATE}")
        return
    df = format_games_for_output(league, games)
    out_dir = os.path.join(REPORT_ROOT, TARGET_SLATE_DATE.isoformat())
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{league}_{TARGET_SLATE_DATE.isoformat()}.xlsx")
    df.to_excel(out_path, index=False)
    logger.info(f"Saved {league} report: {out_path}")


# ========= EMAIL SUMMARY =========

def send_email_summary(subject: str, body: str, attachment_paths: list[str]):
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachment_paths:
        try:
            with open(path, "rb") as f:
                data = f.read()
            filename = os.path.basename(path)
            msg.add_attachment(
                data, maintype="application", subtype="octet-stream", filename=filename
            )
        except Exception as e:
            logger.warning(f"Failed attaching {path}: {e}")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PW)
            smtp.send_message(msg)
        logger.info("Email summary sent.")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")


# ========= MAIN =========

def main():
    ok, _avail = jsonodds_preflight()
    if not ok:
        logger.error("Stopping: JsonOdds preflight failed.")
        return

    league_df = load_league_info()
    all_attachments = []

    for league in ("NFL", "NCAAF", "NBA", "NCAAB", "NHL"):
        logger.info(f"=== {league} ===")
        stats_df = fetch_live_stats(league)
        games = fetch_jsonodds_games(league)
        modeled_games = [run_model(league, g, stats_df, league_df) for g in games]
        save_league_report(league, modeled_games)
        if modeled_games:
            out_dir = os.path.join(REPORT_ROOT, TARGET_SLATE_DATE.isoformat())
            all_attachments.append(
                os.path.join(out_dir, f"{league}_{TARGET_SLATE_DATE.isoformat()}.xlsx")
            )

    if all_attachments:
        send_email_summary(
            subject=f"Overline Edge — Slate {TARGET_SLATE_DATE.isoformat()}",
            body=f"Attached: reports for {TARGET_SLATE_DATE.isoformat()}",
            attachment_paths=all_attachments,
        )


if __name__ == "__main__":
    main()