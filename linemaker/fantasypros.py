"""FantasyPros API: consensus player projections and injuries -> projection rows.

Projections are expert-consensus means (ESPN, Yahoo, FantasyPros and others). They become
the `mean` the pro engine simulates; the engine's default spread per market supplies the
rest. Daily for NBA and MLB, weekly for NFL. NHL has injuries but no projections.

  FANTASYPROS_API_KEY   sent as the x-api-key header. A free key returns *sample* data
                        (fine for testing, never for betting); production data needs the
                        Premium tier (bundled with FantasyPros HOF).
"""
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

BASE = "https://api.fantasypros.com/public/v2/json"
log = logging.getLogger("linemaker.fantasypros")
NFL_POSITIONS = ("QB", "RB", "WR", "TE")

# FantasyPros stat key -> engine market name. Tuples are summed.
NBA_MARKETS = {
    "points": "points", "rebounds": "rebounds", "assists": "assists",
    "three_points_made": "threes", ("points", "rebounds", "assists"): "PRA",
}
NFL_MARKETS = {
    "pass_yds": "passing yards", "pass_tds": "passing tds", "pass_cmp": "pass completions",
    "pass_att": "pass attempts", "rush_yds": "rushing yards", "rush_att": "rush attempts",
    "rec_yds": "receiving yards", "rec_rec": "receptions", ("rush_tds", "rec_tds"): "anytime td",
}
MLB_HITTER = {"hits": "hits", "hrs": "home runs"}
MLB_PITCHER = {"k": "strikeouts"}

# Injury status (long or short form, any sport) -> engine status.
INJURY_MAP = {
    "o": "out", "out": "out", "ir": "out", "il10": "out", "il15": "out", "il60": "out",
    "ltir": "out", "susp": "out", "suspended": "out", "pup": "out", "nfi": "out",
    "d": "doubtful", "doubtful": "doubtful",
    "q": "questionable", "questionable": "questionable", "gtd": "questionable",
    "dtd": "questionable", "day-to-day": "questionable", "udp": "questionable",
    "p": "probable", "probable": "probable",
}


class FantasyProsError(RuntimeError):
    pass


class FantasyPros:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 20):
        self.api_key = api_key or os.environ.get("FANTASYPROS_API_KEY")
        if not self.api_key:
            raise FantasyProsError("Set FANTASYPROS_API_KEY to your FantasyPros API key.")
        self.timeout = timeout
        self.retry_wait = 2.0

    def _get(self, path: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(url, headers={"x-api-key": self.api_key,
                                                   "Accept": "application/json"})
        started = time.time()
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                log.info("fantasypros %s: %.1fs", path, time.time() - started)
                return data
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    log.warning("fantasypros %s: rate limited, retrying", path)
                    time.sleep(self.retry_wait * 2 ** attempt)
                    continue
                log.warning("fantasypros %s: HTTP %s", path, e.code)
                detail = e.read().decode(errors="replace")[:300]
                raise FantasyProsError(f"HTTP {e.code} from FantasyPros: {detail}") from None

    def nba_projections(self, date: str) -> dict:
        return self._get(f"/nba/{season_for('NBA', date)}/projections", type="daily",
                         date=date, stat_values="precise")

    def mlb_projections(self, date: str, position: str) -> dict:
        return self._get(f"/mlb/{season_for('MLB', date)}/projections", type="daily",
                         date=date, position=position)

    def nfl_projections(self, date: str, position: str, week: Optional[int] = None) -> dict:
        return self._get(f"/nfl/{season_for('NFL', date)}/projections", position=position,
                         week=week)

    def injuries(self, sport: str) -> dict:
        return self._get(f"/{sport.lower()}/injuries")


def season_for(sport: str, date: str) -> int:
    """FantasyPros season year: the year a season starts (NBA/NHL in Oct, NFL in Sep)."""
    d = dt.date.fromisoformat(date)
    if sport.upper() in ("NBA", "NHL"):
        return d.year if d.month >= 8 else d.year - 1
    if sport.upper() == "NFL":
        return d.year if d.month >= 3 else d.year - 1
    return d.year


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(sport: str, name: str, team: str, position: str, stats: dict, mapping: dict,
          source: str) -> list:
    out = []
    for keys, market in mapping.items():
        keys = keys if isinstance(keys, tuple) else (keys,)
        vals = [_num(stats.get(k)) for k in keys]
        if any(v is None for v in vals):
            continue
        mean = sum(vals)
        if mean <= 0:
            continue
        out.append({"sport": sport, "subject": name, "market": market, "position": position,
                    "mean": f"{mean:.3f}", "notes": f"FantasyPros consensus ({source}); {team}"})
    return out


def parse_nba(payload: dict) -> list:
    rows = []
    for p in payload.get("player", []):
        rows += _rows("NBA", p["name"], p.get("team_id", ""), p.get("position_id", ""), p,
                      NBA_MARKETS, f"daily {payload.get('date', '')}".strip())
    return rows


def parse_mlb(payload: dict, pitchers: bool) -> list:
    rows = []
    for p in payload.get("player", []):
        if pitchers:
            rows += _rows("MLB", p["name"], p.get("team_id", ""), "SP", p, MLB_PITCHER, "daily")
            continue
        stats = dict(p)
        parts = [_num(p.get(k)) for k in ("1b", "2b", "3b", "hrs")]
        if all(v is not None for v in parts):
            stats["total_bases"] = parts[0] + 2 * parts[1] + 3 * parts[2] + 4 * parts[3]
        rows += _rows("MLB", p["name"], p.get("team_id", ""), "H", stats,
                      {**MLB_HITTER, "total_bases": "total bases"}, "daily")
    return rows


def parse_nfl(payload: dict) -> list:
    rows = []
    for p in payload.get("players", []):
        stats = p.get("stats") or {}
        if isinstance(stats, list):   # schema allows either shape
            stats = stats[0] if stats else {}
        pos = p.get("position_id", "")
        rows += _rows("NFL", p["name"], p.get("team_id", ""), pos, stats, NFL_MARKETS,
                      f"week {payload.get('week', '')}".strip())
    return rows


def injury_status(payload: dict) -> dict:
    """normalised player name -> engine injury status."""
    from .oddsapi import norm
    out = {}
    for inj in payload.get("injuries", []):
        raw = (inj.get("status_short") or inj.get("status") or "").strip().lower()
        status = INJURY_MAP.get(raw) or INJURY_MAP.get((inj.get("status") or "").strip().lower())
        if status and inj.get("name"):
            out[norm(inj["name"])] = status
    return out


def fetch_projections(api: FantasyPros, sport: str, date: str,
                      nfl_week: Optional[int] = None) -> list:
    """Projection rows (pro-engine format) for one sport and date, injuries attached."""
    s = sport.upper()
    api.warning = None
    if s == "NBA":
        payload = api.nba_projections(date)
        got = str(payload.get("date") or "")[:10]
        players = len(payload.get("player", []))
        log.info("fantasypros NBA daily: %d players for %s (asked for %s)", players, got or "?", date)
        if got and got != date:
            api.warning = (f"FantasyPros returned projections for {got}, not {date}. A free API key "
                           "serves sample data; real daily projections need the Premium tier.")
        elif players < 60:
            api.warning = (f"FantasyPros returned only {players} NBA player projections for a full "
                           "slate. If this is a free API key, it's serving sample data.")
        rows = parse_nba(payload)
    elif s == "MLB":
        rows = (parse_mlb(api.mlb_projections(date, "H"), pitchers=False)
                + parse_mlb(api.mlb_projections(date, "SP"), pitchers=True))
    elif s == "NFL":
        rows = []
        for pos in NFL_POSITIONS:
            rows += parse_nfl(api.nfl_projections(date, pos, nfl_week))
    else:
        raise FantasyProsError(f"FantasyPros has no {sport} projections (NBA, NFL, MLB only)")
    if api.warning:
        log.warning(api.warning)
    try:
        statuses = injury_status(api.injuries(s))
    except FantasyProsError:
        statuses = {}
    from .oddsapi import norm
    for r in rows:
        status = statuses.get(norm(r["subject"]))
        if status:
            r["injury_status"] = status
    return rows


SUPPORTED = ("NBA", "NFL", "MLB")
