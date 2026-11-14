"""UFC fighter stat props (significant strikes, takedowns, fight time) from SportsGameOdds.

The Odds API carries no MMA props, so these come from SportsGameOdds (sportsgameodds.com), a
documented odds API with API keys. Set the key with /set SPORTSGAMEODDS_API_KEY (stored on the
server only, never in GitHub). Without a key this module does nothing.

API (v2): GET https://api.sportsgameodds.com/v2/events?leagueID=UFC&oddsAvailable=true, header
x-api-key. Each fight is one event; its `odds` are keyed by oddID
"{statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}". UFC is a single-participant league:
the two fighters sit in the event's home / away slots, so a fighter prop's statEntityID is
"home" / "away" (or a playerID, handled too). Each odd has the consensus line and price
(bookOverUnder / bookOdds), the de-vigged fair price (fairOdds / fairOverUnder) and byBookmaker
{bookmakerID: {odds, overUnder, available}}.

Cost: the free Amateur plan allows 2,500 event objects a month (a 13-fight card = 13 objects
per request) and refreshes every 10 minutes, so a card is fetched at most once every
SGO_CACHE_MINUTES (15) and the month's objects are counted (cache/sgo_usage.json); it stops
at LINEMAKER_SGO_MONTHLY_CAP (2,300) so the bot never runs the key dry.
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

log = logging.getLogger("linemaker.sgo")
BASE = "https://api.sportsgameodds.com/v2/events"
SGO_CACHE_MINUTES = 15
_CACHE = {}   # "ufc" -> (time, events)
SKIPPED = set()   # statIDs seen that the model can't price (logged once each)


def api_key() -> str:
    return os.environ.get("SPORTSGAMEODDS_API_KEY", "").strip()


def _usage_path() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "sgo_usage.json")


def _usage() -> dict:
    try:
        with open(_usage_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    month = dt.date.today().strftime("%Y-%m")
    return data if data.get("month") == month else {"month": month, "objects": 0, "requests": 0}


def _record(objects: int) -> None:
    u = _usage()
    u["objects"] += objects
    u["requests"] += 1
    try:
        os.makedirs(os.path.dirname(_usage_path()) or ".", exist_ok=True)
        with open(_usage_path(), "w") as f:
            json.dump(u, f)
    except OSError:
        pass


def monthly_cap() -> int:
    try:
        return int(os.environ.get("LINEMAKER_SGO_MONTHLY_CAP", "") or 2300)
    except ValueError:
        return 2300


def usage_line() -> str:
    u = _usage()
    return f"SportsGameOdds: {u['objects']} of {monthly_cap()} event objects used this month ({u['requests']} requests)"


def fetch_ufc_events(days: int = 3, opener=urllib.request.urlopen) -> list:
    """Upcoming UFC fights with odds (cached; [] without a key or past the monthly cap)."""
    key = api_key()
    if not key:
        return []
    hit = _CACHE.get("ufc")
    if hit and time.time() - hit[0] < SGO_CACHE_MINUTES * 60:
        return hit[1]
    if _usage()["objects"] >= monthly_cap():
        log.warning("sportsgameodds: monthly cap reached (%s)", usage_line())
        return hit[1] if hit else []
    now = dt.datetime.now(dt.timezone.utc)
    events, cursor = [], None
    for _page in range(5):
        params = {"leagueID": "UFC", "oddsAvailable": "true", "limit": "50",
                  "startsAfter": (now - dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "startsBefore": (now + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if cursor:
            params["cursor"] = cursor
        req = urllib.request.Request(f"{BASE}?{urllib.parse.urlencode(params)}",
                                     headers={"x-api-key": key, "Accept": "application/json",
                                              "User-Agent": "Linemaker/1.0"})
        try:
            with opener(req, timeout=25) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            raise RuntimeError(f"SportsGameOdds said {e.code}: {body or e.reason}") from None
        page = payload.get("data") or []
        events += page
        _record(len(page))
        cursor = payload.get("nextCursor")
        if not cursor or not page:
            break
    _CACHE["ufc"] = (time.time(), events)
    return events


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _american(v) -> Optional[int]:
    try:
        return int(round(float(str(v).replace("+", ""))))
    except (TypeError, ValueError):
        return None


def stat_kind(stat_id: str, market: str = "") -> Optional[str]:
    """Our model's stat for an SGO statID, or None for ones it can't price (strikes by target or
    position, attempted strikes, knockdowns, ground / clinch time)."""
    s = (stat_id or "").lower()
    if any(k in s for k in ("head", "body", "leg", "distance", "clinch", "ground", "attempt", "knockdown")):
        return None
    if "takedown" in s:
        return "takedowns"
    if "significant" in s and "strike" in s:
        return "significant strikes"
    if "fighttime" in s.replace("_", "") or s in ("fighttime", "timeinfight"):
        return "fight time seconds" if "second" in s else "fight time"
    return None


def _name(event: dict, entity: str) -> str:
    teams = event.get("teams") or {}
    if entity in ("home", "away"):
        t = teams.get(entity) or {}
        names = t.get("names") or {}
        return names.get("long") or names.get("medium") or t.get("name") or ""
    p = (event.get("players") or {}).get(entity) or {}
    full = p.get("name") or " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x)
    return full or entity.split("_1_")[0].replace("_", " ").title()


def fighter_props(events: list) -> list:
    """[{"fighter", "opponent", "stat", "line", "fair_over", "books": {book: {"over", "under"}},
    "start"}] for the props our model can price, at each market's consensus line."""
    out = []
    for ev in events:
        home, away = _name(ev, "home"), _name(ev, "away")
        grouped = {}
        for odd in (ev.get("odds") or {}).values():
            if odd.get("betTypeID") != "ou" or odd.get("periodID", "game") != "game":
                continue
            entity, side = odd.get("statEntityID") or "", odd.get("sideID")
            if entity in ("all", "") or side not in ("over", "under"):
                continue
            kind = stat_kind(odd.get("statID", ""), odd.get("marketName", ""))
            if kind:
                grouped.setdefault((entity, kind), {})[side] = odd
            elif odd.get("statID") and odd["statID"] not in SKIPPED:
                SKIPPED.add(odd["statID"])
                log.info("sportsgameodds: fighter stat %s not priced (no model for it)", odd["statID"])
        for (entity, kind), sides in grouped.items():
            over, under = sides.get("over") or {}, sides.get("under") or {}
            line = _f(over.get("bookOverUnder") or under.get("bookOverUnder"))
            if line is None:
                continue
            fair = _fair_over(over, under, line)
            books = {}
            for side, odd in (("over", over), ("under", under)):
                for bk, q in (odd.get("byBookmaker") or {}).items():
                    if q.get("available") is False or _f(q.get("overUnder")) != line:
                        continue
                    price = _american(q.get("odds"))
                    if price is not None:
                        books.setdefault(bk, {})[side] = price
            name = _name(ev, entity)
            opp = away if name == home else home if name == away else ""
            scale = 1 / 60 if kind == "fight time seconds" else 1.0
            out.append({"fighter": name, "opponent": opp, "stat": "fight time" if kind.startswith("fight time") else kind,
                        "line": line * scale, "fair_over": fair, "books": books,
                        "start": (ev.get("status") or {}).get("startsAt", "")})
    return out


def _implied(odds: Optional[int]) -> Optional[float]:
    if odds is None:
        return None
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def _fair_over(over: dict, under: dict, line: float) -> Optional[float]:
    """De-vigged chance of the over at the line: SGO's fair price when it's at the same line,
    else the consensus prices with the margin removed."""
    fo, fu = _implied(_american(over.get("fairOdds"))), _implied(_american(under.get("fairOdds")))
    same = _f(over.get("fairOverUnder")) in (None, line)
    if fo is not None and fu is not None and same and fo + fu > 0:
        return fo / (fo + fu)
    bo, bu = _implied(_american(over.get("bookOdds"))), _implied(_american(under.get("bookOdds")))
    if bo is not None and bu is not None and bo + bu > 0:
        return bo / (bo + bu)
    return None
