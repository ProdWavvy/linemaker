"""UFC fight model: win probability for each fighter from ESPN's fighter data (free).

ufc.com blocks automated requests, so the numbers come from ESPN, which carries the same
UFC stats feed (record, significant strikes, takedowns, submissions, reach, age, every fight
result). Per fighter:

  record     shrunk win rate (W + 3) / (W + L + 6), so a 3-0 prospect isn't treated as
             unbeatable
  form       last 5 results, newest weighted most (1, .8, .6, .4, .2): +1 a win, -1 a loss
  striking   significant strikes landed per minute, significant strike accuracy
  grappling  takedowns per 15 min, takedown accuracy, submission attempts per 15 min
  finishing  share of wins by KO/TKO or submission
  body       reach (inches); age past 32 counts against a fighter
  experience log of pro fights

  z = 2.5 Δrecord + 0.6 Δform + 0.15 Δstrikes/min + 0.01 Δstrike acc (pts) + 0.08 Δtakedowns
      + 0.005 Δtakedown acc (pts) + 0.10 Δsub attempts + 0.4 Δfinish rate + 0.03 Δreach
      - 0.07 Δ(years past 32) + 0.2 Δlog(fights)
  P(fighter A wins) = 1 / (1 + e^-z), with z capped at ±2.2 (about 90%)

The weights are a reasonable first guess, not fitted. That's why UFC fights blend at only
LINEMAKER_UFC_MODEL_WEIGHT (default 0.35) with the de-vigged FanDuel/DraftKings price, and
the auto-tuner adjusts "UFC moneyline" once enough fights are graded.

Not modelled: injuries, weight cuts, short-notice replacements, camp changes, opponent
strength. The market knows those; the model is a second opinion.
"""
import datetime as dt
import json
import logging
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from statistics import NormalDist
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.ufc")
SITE = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/mma"
CORE = "https://sports.core.api.espn.com/v2/sports/mma"
FORM_WEIGHTS = (1.0, 0.8, 0.6, 0.4, 0.2)
Z_CAP = 2.2
COEF = {"record": 2.5, "form": 0.6, "slpm": 0.15, "sacc": 0.01, "td": 0.08, "tdacc": 0.005,
        "sub": 0.10, "finish": 0.4, "reach": 0.03, "age": -0.07, "exp": 0.2}


def _cache_dir() -> str:
    return os.path.join(os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "espn_mma")


def _get(url: str, max_age: Optional[float] = 20 * 3600) -> dict:
    """ESPN JSON, cached on disk (max_age None = forever, for finished fights)."""
    url = url.replace("http://", "https://")
    name = "".join(c if c.isalnum() else "_" for c in url.split("//", 1)[1])[-180:]
    path = os.path.join(_cache_dir(), name + ".json")
    try:
        if max_age is None or time.time() - os.path.getmtime(path) < max_age:
            with open(path) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    req = urllib.request.Request(url, headers={"User-Agent": "Linemaker/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return data


def card(date: str) -> list:
    """[(fighter A, id, fighter B, id, competition)] on ESPN's UFC schedule for the date
    (a day either side too: cards run past midnight and across time zones)."""
    d = dt.date.fromisoformat(date)
    span = f"{(d - dt.timedelta(days=1)):%Y%m%d}-{(d + dt.timedelta(days=1)):%Y%m%d}"
    data = _get(f"{SITE}/scoreboard?dates={span}", max_age=1800)
    out = []
    for ev in data.get("events", []):
        for comp in ev.get("competitions", []):
            cs = comp.get("competitors", [])
            if len(cs) != 2:
                continue
            a, b = cs
            out.append((a["athlete"]["displayName"], a["id"], b["athlete"]["displayName"], b["id"], comp))
    return out


def _inches(text: str) -> Optional[float]:
    try:
        return float(str(text).replace('"', "").strip())
    except ValueError:
        return None


def fighter(aid: str) -> dict:
    """Everything the model uses for one fighter (missing pieces are None)."""
    f = {"id": aid}
    try:
        a = _get(f"{WEB}/athletes/{aid}").get("athlete", {})
        f.update(name=a.get("displayName"), age=a.get("age"), reach=_inches(a.get("displayReach", "")),
                 stance=(a.get("stance") or {}).get("text"))
    except Exception as e:
        log.warning("ufc athlete %s: %s", aid, e)
    try:
        rec = _get(f"{CORE}/athletes/{aid}/records").get("items", [])
        stats = {s["name"]: s.get("value") or 0 for s in (rec[0]["stats"] if rec else [])}
        f.update(wins=stats.get("wins", 0), losses=stats.get("losses", 0),
                 finishes=stats.get("tkos", 0) + stats.get("submissions", 0),
                 finished=stats.get("tkoLosses", 0) + stats.get("submissionLosses", 0))
    except Exception as e:
        log.warning("ufc record %s: %s", aid, e)
    try:
        st = _get(f"{CORE}/athletes/{aid}/statistics").get("splits", {})
        g = {s["name"]: s.get("value") for c in st.get("categories", []) for s in c.get("stats", [])}
        f.update(slpm=g.get("strikeLPM"), sacc=g.get("strikeAccuracy"), td=g.get("takedownAvg"),
                 tdacc=g.get("takedownAccuracy"), sub=g.get("submissionAvg"))
    except Exception as e:
        log.warning("ufc stats %s: %s", aid, e)
    f["form"] = recent_form(aid)
    return f


def recent_form(aid: str, n: int = 5) -> list:
    """Last n results, newest first: 1 win, -1 loss, 0 draw / no contest."""
    try:
        items = _get(f"{CORE}/athletes/{aid}/eventlog").get("events", {}).get("items", [])
    except Exception as e:
        log.warning("ufc eventlog %s: %s", aid, e)
        return []
    out = []
    for it in items:
        if not it.get("played") or not it.get("competitor"):
            continue
        try:
            c = _get(it["competitor"]["$ref"], max_age=None)   # finished fights never change
        except Exception:
            continue
        w = c.get("winner")
        out.append(1 if w is True else -1 if w is False else 0)
        if len(out) == n:
            break
    return out


def _form_score(form: list) -> float:
    if not form:
        return 0.0
    ws = FORM_WEIGHTS[:len(form)]
    return sum(w * r for w, r in zip(ws, form)) / sum(ws)


def features(f: dict) -> dict:
    w, l = f.get("wins") or 0, f.get("losses") or 0
    age = f.get("age")
    return {"record": (w + 3) / (w + l + 6), "form": _form_score(f.get("form") or []),
            "slpm": f.get("slpm"), "sacc": f.get("sacc"), "td": f.get("td"), "tdacc": f.get("tdacc"),
            "sub": f.get("sub"), "finish": (f.get("finishes") or 0) / w if w else 0.0,
            "reach": f.get("reach"), "age": max(0, age - 32) if age else None,
            "exp": math.log(1 + w + l)}


def win_prob(a: dict, b: dict) -> tuple:
    """(P(a wins), [(step, value)]) from two fighter dicts."""
    fa, fb = features(a), features(b)
    z, steps = 0.0, []
    for k, c in COEF.items():
        if fa.get(k) is None or fb.get(k) is None:
            continue           # missing for either fighter: that factor sits out
        part = c * (fa[k] - fb[k])
        z += part
        if abs(part) >= 0.01:
            steps.append((k, f"{fa[k]:.2f} vs {fb[k]:.2f} → {part:+.2f}"))
    z = max(-Z_CAP, min(Z_CAP, z))
    return 1 / (1 + math.exp(-z)), steps


FINISH_PRIOR, PRIOR_N = 0.5, 4   # about half of UFC fights end early


def finish_chance(a: dict, b: dict, p_a: float) -> float:
    """P(the fight ends before the final bell): each way the fight can be won, the winner's
    finishing rate averaged with how often the loser gets finished, both pulled toward the
    UFC norm (about half of fights end early) so short records don't swing it."""
    def rate(k, n):
        return ((k or 0) + FINISH_PRIOR * PRIOR_N) / ((n or 0) + PRIOR_N)
    a_fin, b_fin = rate(a.get("finishes"), a.get("wins")), rate(b.get("finishes"), b.get("wins"))
    a_got, b_got = rate(a.get("finished"), a.get("losses")), rate(b.get("finished"), b.get("losses"))
    return p_a * (a_fin + b_got) / 2 + (1 - p_a) * (b_fin + a_got) / 2


def _match(name: str, fights: list) -> Optional[tuple]:
    n = norm(name)
    for fa, ida, fb, idb, c in fights:
        if n in (norm(fa), norm(fb)):
            return (fa, ida, fb, idb, c)
    last = n.split()[-1] if n else ""
    hits = [f for f in fights if last and last in (norm(f[0]).split()[-1], norm(f[2]).split()[-1])]
    return hits[0] if len(hits) == 1 else None


def ufc_projections(date: str, games: list) -> list:
    """games: [(away, home)] fighter names from the odds feed -> moneyline rows for the engine.
    The engine reads a moneyline as P(margin > 0) on a normal curve, so the win chance is
    passed as mean = z(p) with stdev 1."""
    try:
        fights = card(date)
    except Exception as e:
        log.warning("ufc card %s: %s", date, e)
        return []
    pairs = []
    for away, home in games:
        m = _match(home, fights) or _match(away, fights)
        if not m:
            log.info("ufc: %s vs %s not on ESPN's card", away, home)
            continue
        fa, ida, fb, idb, comp = m
        home_id, away_id = (ida, idb) if norm(home) == norm(fa) or (
            norm(home).split()[-1:] == norm(fa).split()[-1:]) else (idb, ida)
        rounds = ((comp.get("format") or {}).get("regulation") or {}).get("periods") or 3
        pairs.append((away, home, away_id, home_id, rounds))
    ids = sorted({i for p in pairs for i in p[2:4]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        info = dict(zip(ids, pool.map(fighter, ids)))
    rows = []
    for away, home, away_id, home_id, rounds in pairs:
        p, steps = win_prob(info[home_id], info[away_id])
        p = min(max(p, 0.02), 0.98)
        rows.append({"sport": "UFC", "subject": home, "market": "moneyline",
                     "mean": f"{NormalDist().inv_cdf(p):.4f}", "stdev": "1",
                     "notes": "UFC model: ESPN fighter stats (" + "; ".join(f"{k} {v}" for k, v in steps)[:300] + ")"})
        early = finish_chance(info[home_id], info[away_id], p)
        rows.append({"sport": "UFC", "subject": home, "market": "total rounds", "dist": "ufcrounds",
                     "mean": f"{early:.4f}", "stdev": str(rounds),
                     "notes": f"UFC model: {early:.0%} chance it ends early, {rounds} rounds scheduled"})
    log.info("ufc model %s: %d fights asked, %d projected", date, len(games), len(rows))
    return rows


def ufc_results(records: dict, date: str) -> list:
    """Results rows for graded UFC moneylines from ESPN's final results: actual = +1 when the
    subject won, -1 when he lost, 0 for a draw or no contest."""
    recs = [r for r in records.values() if r.get("sport") == "UFC" and r.get("result") is None]
    if not recs:
        return []
    try:
        fights = card(date)
    except Exception as e:
        log.warning("ufc results %s: %s", date, e)
        return []
    winners, length = {}, {}
    for fa, _ia, fb, _ib, comp in fights:
        status = comp.get("status", {})
        if not status.get("type", {}).get("completed"):
            continue
        flags = {norm(c["athlete"]["displayName"]): c.get("winner") for c in comp.get("competitors", [])}
        winners.update({k: (v, flags) for k, v in flags.items()})
        rounds = fight_length(status)
        if rounds is not None:
            length.update({k: rounds for k in flags})
    rows = fighter_prop_results(records, date)
    for r in recs:
        if r["market"] in UFC_PROP_MARKETS:
            continue
        if r["market"] == "total rounds":
            if norm(r["subject"]) in length:
                rows.append({"game_id": r["game_id"], "subject": r["subject"], "market": r["market"],
                             "line": str(r["inputs"]["line"]), "actual": f"{length[norm(r['subject'])]:.3f}"})
            continue
        hit = winners.get(norm(r["subject"]))
        if hit is None:
            continue
        won, flags = hit
        actual = 0 if not any(flags.values()) else (1 if won else -1)
        rows.append({"game_id": r["game_id"], "subject": r["subject"], "market": r["market"],
                     "line": str(r["inputs"]["line"]), "actual": str(actual)})
    return rows


def fight_length(status: dict) -> Optional[float]:
    """Rounds fought, e.g. 1:44 of round 2 -> 1.347 (ESPN's clock is the time of the finish)."""
    try:
        mins, secs = str(status.get("displayClock", "")).split(":")
        return int(status["period"]) - 1 + (int(mins) * 60 + int(secs)) / 300
    except (KeyError, ValueError, TypeError):
        return None


def next_card_note(date: str) -> str:
    """Why a UFC ask came back empty, and when the next card is."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        data = _get(f"{SITE}/scoreboard", max_age=1800)
    except Exception:
        data = {}
    started, upcoming = [], []
    for ev in data.get("events", []):
        for comp in ev.get("competitions", []):
            when = dt.datetime.fromisoformat(comp.get("date", ev["date"]).replace("Z", "+00:00"))
            (started if when <= now else upcoming).append((when, ev.get("name", "UFC")))
    if upcoming:
        when, name = min(upcoming)
        return (f"No UFC fight odds to price right now. Next: **{name}**, first fight still to come "
                f"starts {when:%a %b %d %H:%M} UTC. Odds usually post several days before.")
    if started:
        return ("Every fight on today's UFC card has already started or finished, and started "
                "fights aren't priced (the model is pre-fight only). Ask again before the next card.")
    return "No UFC card found on ESPN's schedule for the next few days."


# ---- fighter props: significant strikes, takedowns, fight time ---------------------------
# A fighter's count is his career rate (ESPN's UFC stats: significant strikes landed per
# minute, takedowns per 15 minutes) times how long the fight should last:
#   expected minutes = 5 x [P(early) x mean finish round + (1 - P(early)) x scheduled rounds]
# with early finishes front-loaded as in the total-rounds model. Strikes are trimmed a little
# when the opponent is a heavy wrestler (time on the mat is time not striking at range).
# For pricing, the sim draws the fight length first, then the count around rate x length, so
# a quick finish drags every over down together (that's the real risk in these props).
UFC_PROP_MARKETS = ("significant strikes", "takedowns", "fight time")


def expected_rounds(p_early: float, rounds: int) -> float:
    lam = 0.35   # same front-loaded finish curve as sim.FINISH_HAZARD
    e_early = 1 / lam - rounds * math.exp(-lam * rounds) / (1 - math.exp(-lam * rounds))
    return p_early * e_early + (1 - p_early) * rounds


def fighter_props(date: str) -> list:
    """Per fighter on the card: win chance, chance the fight ends early, expected minutes and
    projected significant strikes / takedowns. Free (ESPN only)."""
    fights = [f for f in card(date)
              if not (f[4].get("status", {}).get("type", {}).get("completed"))]
    ids = sorted({i for f in fights for i in (f[1], f[3])})
    with ThreadPoolExecutor(max_workers=8) as pool:
        info = dict(zip(ids, pool.map(fighter, ids)))
    out = []
    for fa, ida, fb, idb, comp in fights:
        a, b = info[ida], info[idb]
        p, _s = win_prob(a, b)
        p = min(max(p, 0.02), 0.98)
        rounds = ((comp.get("format") or {}).get("regulation") or {}).get("periods") or 3
        early = finish_chance(a, b, p)
        minutes = 5 * expected_rounds(early, rounds)
        for me, opp, name, opp_name, win in ((a, b, fa, fb, p), (b, a, fb, fa, 1 - p)):
            wrestle = 1 - min(0.25, 0.03 * (opp.get("td") or 0))
            out.append({"fighter": name, "opponent": opp_name, "rounds": rounds, "win": win,
                        "early": early, "minutes": minutes, "when": comp.get("date", ""),
                        "sig_strikes": (me.get("slpm") or 0) * minutes * wrestle if me.get("slpm") is not None else None,
                        "takedowns": (me.get("td") or 0) / 15 * minutes if me.get("td") is not None else None})
    return out


def fighter_prop_rows(date: str) -> list:
    """Projection rows for the engine (joined to a fighter-props feed when one is connected)."""
    rows = []
    for f in fighter_props(date):
        dist = f"ufccount|{f['early']:.4f}|{f['rounds']}"
        for market, key in (("significant strikes", "sig_strikes"), ("takedowns", "takedowns")):
            if f[key] is not None:
                rows.append({"sport": "UFC", "subject": f["fighter"], "market": market, "dist": dist,
                             "mean": f"{f[key]:.3f}", "stdev": "1",
                             "notes": f"UFC model: {f['minutes']:.1f} expected minutes"})
        rows.append({"sport": "UFC", "subject": f["fighter"], "market": "fight time", "dist": "ufcminutes",
                     "mean": f"{f['early']:.4f}", "stdev": str(f["rounds"]),
                     "notes": f"UFC model: {f['early']:.0%} chance it ends early"})
    return rows


def _stat(stats: dict, uid: str, category: str, label: str) -> Optional[float]:
    for c in stats.get("categories", []):
        if c.get("displayName", "").lower() != category:
            continue
        labels = [l.strip() for l in c.get("labels", [])]
        if label not in labels:
            return None
        for s in c.get("statistics", []):
            if s.get("uid") == uid:
                try:
                    return float(str(s["stats"][labels.index(label)]).split("/")[0].rstrip("%"))
                except (ValueError, IndexError):
                    return None
    return None


def fighter_prop_results(records: dict, date: str) -> list:
    """Results rows for graded fighter props, from ESPN's per-fight stats."""
    recs = [r for r in records.values() if r.get("sport") == "UFC" and r.get("result") is None
            and r.get("market") in UFC_PROP_MARKETS]
    if not recs:
        return []
    fights = card(date)
    who = {}
    for fa, ida, fb, idb, comp in fights:
        if comp.get("status", {}).get("type", {}).get("completed"):
            who[norm(fa)] = (ida, comp)
            who[norm(fb)] = (idb, comp)
    rows = []
    for r in recs:
        hit = who.get(norm(r["subject"]))
        if not hit:
            continue
        aid, comp = hit
        if r["market"] == "fight time":
            length = fight_length(comp.get("status", {}))
            actual = None if length is None else 5 * length
        else:
            stats = _get(f"{WEB}/athletes/{aid}/stats", max_age=3600)
            actual = (_stat(stats, comp.get("uid", ""), "striking", "SSL") if r["market"] == "significant strikes"
                      else _stat(stats, comp.get("uid", ""), "clinch", "TDL"))
        if actual is not None:
            rows.append({"game_id": r["game_id"], "subject": r["subject"], "market": r["market"],
                         "line": str(r["inputs"]["line"]), "actual": f"{actual:g}"})
    return rows


def next_card_date(date: str) -> str:
    """Today if a fight is still to come, otherwise the date of ESPN's next UFC event."""
    if fighter_props_cached(date):
        return date
    data = _get(f"{SITE}/scoreboard", max_age=1800)
    for ev in data.get("events", []):
        return ev["date"][:10]
    return date


def fighter_props_cached(date: str) -> bool:
    return any(not f[4].get("status", {}).get("type", {}).get("completed") for f in card(date))


def props_table(date: str) -> tuple:
    """(title, code-block text) for /ufc: the model's numbers for every fighter on the card."""
    rows = fighter_props(date)
    lines = [f"{'Fighter':<20} {'Win':>4} {'SigStr':>6} {'TD':>4} {'Mins':>5}"]
    last_pair = None
    for f in rows:
        pair = tuple(sorted((f["fighter"], f["opponent"])))
        if last_pair and pair != last_pair:
            lines.append("")
        last_pair = pair
        ss = f"{f['sig_strikes']:.1f}" if f["sig_strikes"] is not None else "—"
        td = f"{f['takedowns']:.1f}" if f["takedowns"] is not None else "—"
        name = f["fighter"] if len(f["fighter"]) <= 20 else f["fighter"][:19] + "…"
        lines.append(f"{name:<20} {f['win']:>4.0%} {ss:>6} {td:>4} {f['minutes']:>5.1f}")
    return lines, rows
