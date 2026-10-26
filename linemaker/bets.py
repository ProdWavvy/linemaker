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
    from .jsonio import save_json
    save_json(path, bets, indent=1)


def leg_from_eval(ev, live: Optional[bool] = None, odds: Optional[int] = None, book: str = "") -> dict:
    """A bet leg pointing at the tracked market; live legs settle from the live files."""
    m, s = ev.market, ev.best
    if live is None:
        live = getattr(ev, "phase", "pre") == "live"
    return {"key": m.key, "side": s.side, "label": s.label, "sport": m.sport, "date": m.date,
            "bet_type": m.bet_type,
            "game": m.game, "odds": odds if odds is not None else s.best_odds,
            "book": book or s.best_book, "live": live, "model_prob": round(s.blended, 4)}


def find_open(book: str, legs: list) -> Optional[dict]:
    """An open logged bet with exactly these legs at this book (a second tap on Bet)."""
    want = sorted(f"{l['key']}|{l['side']}" for l in legs)
    return next((b for b in load_bets() if b.get("status") == "open" and b.get("book") == book
                 and sorted(f"{l['key']}|{l['side']}" for l in b.get("legs", [])) == want), None)


def update_bet(bet_id: str, **fields) -> Optional[dict]:
    bets = load_bets()
    bet = next((b for b in bets if b["id"] == bet_id), None)
    if bet is not None:
        bet.update(fields)
        save_bets(bets)
    return bet


def remove_bet(bet_id: str) -> bool:
    bets = load_bets()
    keep = [b for b in bets if b["id"] != bet_id]
    if len(keep) != len(bets):
        save_bets(keep)
    return len(keep) != len(bets)


def leg_exposure(leg: dict) -> tuple:
    """Same as parlay.exposure for a logged leg: (game, player) for props, (game, "game line")
    for moneylines / spreads / totals (a team's lines lose together)."""
    parts = (leg.get("key") or "").split("|")
    gid, subject = (parts[1], parts[2]) if len(parts) >= 3 else ("", leg.get("label", ""))
    return (gid, subject) if (leg.get("bet_type") or "prop") == "prop" else (gid, "game line")


def open_exposure() -> dict:
    """exposure -> {"n": open bets it's in, "label"} across your logged, unsettled bets."""
    out = {}
    for b in load_bets():
        if b.get("status") != "open":
            continue
        for x in {leg_exposure(l): l for l in b.get("legs", [])}.items():
            e = out.setdefault(x[0], {"n": 0, "label": x[1].get("label", "")})
            e["n"] += 1
    return out


def max_exposure() -> int:
    """Open bets one player / game may be in before new parlays leave it out (0 = off)."""
    try:
        return int(os.environ.get("LINEMAKER_MAX_EXPOSURE", "2") or 0)
    except ValueError:
        return 2


def add_bet(legs: list, odds: int, stake: float, book: str, user: str = "", **extra) -> dict:
    bet = {"id": uuid.uuid4().hex[:10], "placed": dt.datetime.now().isoformat(timespec="seconds"),
           "user": user, "kind": "parlay" if len(legs) > 1 else "single", "legs": legs,
           "odds": int(odds), "stake": round(float(stake), 2), "book": book, "status": "open"}
    bet.update(extra)
    bets = load_bets()
    bets.append(bet)
    save_bets(bets)
    return bet


def _record(leg: dict, cache: dict) -> Optional[dict]:
    folder = os.path.join(_dir(), "live") if leg.get("live") else (
        os.path.join(_dir(), leg["folder"]) if leg.get("folder") else _dir())
    from .tracking import load_day
    k = (folder, leg["date"])
    if k not in cache:
        try:
            cache[k] = load_day(folder, leg["date"])
        except (OSError, ValueError):
            cache[k] = {}
    return cache[k].get(leg["key"])


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
        if bet.get("kind") == "pp":   # PrizePicks: Power / Flex payout tables
            from .pp import settle_entry
            got = settle_entry(bet, outcomes)
            if got is None:
                continue
            bet["status"], bet["profit"] = got
            for leg, o in zip(bet["legs"], outcomes):
                leg["outcome"] = o
            bet["settled"] = dt.datetime.now().isoformat(timespec="seconds")
            n += 1
            continue
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
    by_kind, by_sport = {}, {}
    for b in done:
        sports = {l.get("sport", "?") for l in b.get("legs", [])}
        s = by_sport.setdefault(sports.pop() if len(sports) == 1 else "Mixed",
                                {"n": 0, "won": 0, "profit": 0.0, "staked": 0.0})
        s["n"] += 1
        s["won"] += b["status"] == "won"
        s["profit"] += b.get("profit", 0.0)
        s["staked"] += b["stake"]
        k = by_kind.setdefault(b["kind"], {"n": 0, "won": 0, "profit": 0.0, "staked": 0.0})
        k["n"] += 1
        k["won"] += b["status"] == "won"
        k["profit"] += b.get("profit", 0.0)
        k["staked"] += b["stake"]
    return {"open": [b for b in rows if b["status"] == "open"], "settled": done,
            "won": sum(b["status"] == "won" for b in done), "lost": sum(b["status"] == "lost" for b in done),
            "push": sum(b["status"] == "push" for b in done), "staked": staked, "profit": profit,
            "roi": profit / staked if staked else None, "by_kind": by_kind,
            "by_sport": by_sport}


def _implied(odds: int) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def leg_clv(leg: dict, cache: dict) -> Optional[float]:
    """Implied probability of the closing price at the leg's book minus the price you got
    (positive = you beat the close). None until the game has started (the line hasn't closed)
    or when the book's closing price wasn't seen."""
    rec = _record(leg, cache)
    if not rec or not rec.get("history") or leg.get("odds") is None:
        return None
    start = rec.get("commence") or ""
    if not start or dt.datetime.fromisoformat(start.replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc):
        return None
    from .pro import book_key
    want = book_key(leg.get("book") or "")
    for snap in reversed(rec["history"]):
        close = next((o for b, o in (snap.get(leg["side"]) or {}).items() if book_key(b) == want), None)
        if close is not None:
            return _implied(close) - _implied(leg["odds"])
    return None


def clv_summary(days: Optional[int] = 30, today: Optional[str] = None) -> dict:
    """Closing-line value across your logged legs (live bets left out: they close in-game)."""
    end = dt.date.fromisoformat(today) if today else dt.date.today()
    start = end - dt.timedelta(days=days) if days else None
    cache, vals = {}, []
    for b in load_bets():
        if start and dt.date.fromisoformat(b["placed"][:10]) < start:
            continue
        for leg in b.get("legs", []):
            if leg.get("live") or b.get("kind") == "pp":
                continue
            v = leg_clv(leg, cache)
            if v is not None:
                vals.append(v)
    return {"n": len(vals), "beat": sum(v > 0.0005 for v in vals) / len(vals) if vals else None,
            "avg": sum(vals) / len(vals) if vals else None}
