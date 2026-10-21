"""Your real bets: logged from the "I bet this" buttons, settled from the tracked results.

Stored in <tracking>/bets.json. Each bet has one leg (a single) or several (a parlay); a leg
points at a tracked market (its key and side), so grading settles it automatically. A
parlay with a pushed leg is paid on the remaining legs.
"""
import datetime as dt
import json
import os
import uuid
from typing import Optional

from .odds import american_to_decimal


def _dir() -> str:
    return os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")


def bets_path() -> str:
    return os.path.join(_dir(), "bets.json")


def load_bets() -> list:
    try:
        with open(bets_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_bets(bets: list):
    path = bets_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(bets, f, indent=1)
    os.replace(path + ".tmp", path)


def leg_from_eval(ev, live: bool = False, odds: Optional[int] = None, book: str = "") -> dict:
    m, s = ev.market, ev.best
    return {"key": m.key, "side": s.side, "label": s.label, "sport": m.sport, "date": m.date,
            "game": m.game, "odds": odds if odds is not None else s.best_odds,
            "book": book or s.best_book, "live": live, "model_prob": round(s.blended, 4)}


def add_bet(legs: list, odds: int, stake: float, book: str, user: str = "") -> dict:
    bet = {"id": uuid.uuid4().hex[:10], "placed": dt.datetime.now().isoformat(timespec="seconds"),
           "user": user, "kind": "parlay" if len(legs) > 1 else "single", "legs": legs,
           "odds": int(odds), "stake": round(float(stake), 2), "book": book, "status": "open"}
    bets = load_bets()
    bets.append(bet)
    save_bets(bets)
    return bet


def _record(leg: dict, cache: dict) -> Optional[dict]:
    folder = os.path.join(_dir(), "live") if leg.get("live") else _dir()
    path = os.path.join(folder, f"{leg['date']}.json")
    if path not in cache:
        try:
            with open(path) as f:
                cache[path] = json.load(f)
        except (OSError, ValueError):
            cache[path] = {}
    return cache[path].get(leg["key"])


def leg_outcome(leg: dict, cache: dict) -> Optional[str]:
    """won / lost / push, or None while the game isn't graded."""
    rec = _record(leg, cache)
    if not rec or not rec.get("result"):
        return None
    winner = rec["result"].get("winner")
    if winner is None:
        return "push"
    return "won" if winner == leg["side"] else "lost"


def settle() -> int:
    """Settle every open bet whose legs are all graded. Returns how many were settled."""
    bets, cache, n = load_bets(), {}, 0
    for bet in bets:
        if bet["status"] != "open":
            continue
        outcomes = [leg_outcome(l, cache) for l in bet["legs"]]
        if "lost" in outcomes:
            bet["status"], bet["profit"] = "lost", -bet["stake"]
        elif None in outcomes:
            continue
        elif all(o == "push" for o in outcomes):
            bet["status"], bet["profit"] = "push", 0.0
        else:
            if "push" in outcomes:   # paid on the legs that stood
                dec = 1.0
                for leg, o in zip(bet["legs"], outcomes):
                    if o == "won":
                        dec *= american_to_decimal(leg["odds"])
            else:
                dec = american_to_decimal(bet["odds"])
            bet["status"], bet["profit"] = "won", round(bet["stake"] * (dec - 1), 2)
        for leg, o in zip(bet["legs"], outcomes):
            leg["outcome"] = o
        bet["settled"] = dt.datetime.now().isoformat(timespec="seconds")
        n += 1
    if n:
        save_bets(bets)
    return n


def summary(days: Optional[int] = 30, today: Optional[str] = None) -> dict:
    end = dt.date.fromisoformat(today) if today else dt.date.today()
    start = end - dt.timedelta(days=days) if days else None
    rows = [b for b in load_bets()
            if not start or dt.date.fromisoformat(b["placed"][:10]) >= start]
    done = [b for b in rows if b["status"] != "open"]
    staked = sum(b["stake"] for b in done)
    profit = sum(b.get("profit", 0.0) for b in done)
    by_kind = {}
    for b in done:
        k = by_kind.setdefault(b["kind"], {"n": 0, "won": 0, "profit": 0.0, "staked": 0.0})
        k["n"] += 1
        k["won"] += b["status"] == "won"
        k["profit"] += b.get("profit", 0.0)
        k["staked"] += b["stake"]
    return {"open": [b for b in rows if b["status"] == "open"], "settled": done,
            "won": sum(b["status"] == "won" for b in done), "lost": sum(b["status"] == "lost" for b in done),
            "push": sum(b["status"] == "push" for b in done), "staked": staked, "profit": profit,
            "roi": profit / staked if staked else None, "by_kind": by_kind}
