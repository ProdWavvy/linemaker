"""Every UFC prop the bot prices, logged and graded, so the fight-props model can be checked.

tracking/ufc_props/<card date>.json, one entry per (fight, prop, where): the side the bot leaned
to, its chance, the market's chance and the price (Kalshi's cost of that side, or a book's odds;
pick'em apps have no price), whether it was shown as a play (3+ points better than the price),
first sighting kept (a later sighting as a play updates it, the price you'd have seen).

Graded from ESPN after the fight: the winner and method ("Unofficial Winner Decision" /
"Submission" / KO-TKO in the fight's details), the round and clock of the finish, and each
fighter's significant strikes and takedowns. Draws and no contests void method props.
"""
import datetime as dt
import hashlib
import json
import logging
import os
from typing import Optional

log = logging.getLogger("linemaker.ufc_props_track")


def _path(date: str) -> str:
    return os.path.join(os.environ.get("LINEMAKER_TRACKING_DIR", "tracking"), "ufc_props", f"{date}.json")


def load(date: str) -> dict:
    try:
        from .jsonio import load_json
        return load_json(_path(date)) or {}
    except (OSError, ValueError):
        return {}


def _on() -> bool:
    return os.environ.get("LINEMAKER_TRACKING", "on").lower() not in ("off", "0", "false", "no")


def log_props(date: str, entries: list) -> int:
    """Remember priced props. entries: dicts with fight, label, kind, spec, chance, market, price,
    odds, book, shown. Returns how many were new."""
    if not _on() or not entries:
        return 0
    from .jsonio import path_lock, save_json
    path = _path(date)
    new = 0
    with path_lock(path):
        data = load(date)
        now = dt.datetime.now().isoformat(timespec="seconds")
        for e in entries:
            pid = hashlib.sha1(json.dumps([e["fight"], e["label"], e.get("book", "")]).encode()).hexdigest()[:12]
            old = data.get(pid)
            if old and (old.get("shown") or not e.get("shown") or old.get("result")):
                continue
            data[pid] = dict(e, logged=now, result=None)
            new += old is None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        save_json(path, data)
    return new


# ---- results from ESPN --------------------------------------------------------------------
def fight_outcome(comp: dict) -> Optional[dict]:
    """{"winner", "method": KO|SUB|DEC|None, "void", "round", "minutes", "rounds"} for a
    finished fight, None if it isn't final."""
    st = comp.get("status") or {}
    if not (st.get("type") or {}).get("completed"):
        return None
    from .ufc_model import fight_length
    texts = " ".join(((d.get("type") or {}).get("text") or "") for d in comp.get("details") or [])
    winner_txt = next((((d.get("type") or {}).get("text") or "") for d in comp.get("details") or []
                       if "winner" in ((d.get("type") or {}).get("text") or "").lower()), "")
    w = winner_txt.lower()
    method = ("DEC" if "decision" in w else "SUB" if "submission" in w
              else "KO" if any(k in w for k in ("ko", "knockout", "doctor", "disqualif", "dq")) else None)
    rounds = ((comp.get("format") or {}).get("regulation") or {}).get("periods") or 3
    length = fight_length(st)
    if method is None and length is not None and st.get("displayClock") == "5:00" and st.get("period") == rounds:
        method = "DEC"   # went the full scheduled time
    winners = [c["athlete"]["displayName"] for c in comp.get("competitors", []) if c.get("winner")]
    void = not winners or any(k in texts.lower() for k in ("no contest", "draw"))
    return {"winner": winners[0] if winners else None, "method": method, "void": void,
            "round": st.get("period"), "minutes": None if length is None else 5 * length, "rounds": rounds}


def yes_happened(spec: dict, out: dict, stat_value=None) -> Optional[bool]:
    """Did the prop's YES side (the event it names) happen? None = can't tell / void."""
    from .ufc_props import same_fighter
    kind = spec.get("kind")
    if kind == "distance":
        return None if out["method"] is None else out["method"] == "DEC"
    if kind == "rounds":
        if out["method"] is None:
            return None
        return out["method"] != "DEC" and (out["round"] or 99) < spec["before"]
    if kind == "mov":
        if out["void"] or out["method"] is None:
            return None
        return bool(out["winner"]) and same_fighter(out["winner"], spec["fighter"]) and out["method"] == spec["method"]
    if kind == "stat":
        if stat_value is None:
            return None
        if stat_value == spec["line"]:
            return None   # push
        return stat_value > spec["line"]   # YES = over
    return None


def _stat_value(spec: dict, comp: dict, out: dict, ids: dict):
    from .ufc_model import WEB, _get, _stat
    from .oddsapi import norm
    if spec["stat"] == "fight time":
        return out["minutes"]
    aid = ids.get(norm(spec["fighter"]))
    if not aid:
        return None
    try:
        stats = _get(f"{WEB}/athletes/{aid}/stats", max_age=3600)
    except Exception:
        return None
    return (_stat(stats, comp.get("uid", ""), "striking", "SSL") if spec["stat"] == "significant strikes"
            else _stat(stats, comp.get("uid", ""), "clinch", "TDL"))


def grade(date: str) -> int:
    """Settle the card's logged props that are final. Returns how many settled."""
    data = load(date)
    todo = {k: v for k, v in data.items() if not v.get("result")}
    if not todo:
        return 0
    from .oddsapi import norm
    from .ufc_model import card
    from .ufc_props import same_fighter
    try:
        fights = card(date)
    except Exception as e:
        log.warning("ufc props grading %s: %s", date, e)
        return 0
    n = 0
    for e in todo.values():
        a, b = (e.get("fight") or " vs ").split(" vs ", 1)
        hit = next((f for f in fights if {same_fighter(a, f[0]) or same_fighter(a, f[2]),
                                          same_fighter(b, f[0]) or same_fighter(b, f[2])} == {True}), None)
        if not hit:
            continue
        fa, ida, fb, idb, comp = hit
        out = fight_outcome(comp)
        if out is None:
            continue
        spec = e.get("spec") or {}
        value = _stat_value(spec, comp, out, {norm(fa): ida, norm(fb): idb}) if spec.get("kind") == "stat" else None
        if spec.get("kind") == "stat" and value is None and not _stale(date):
            continue   # ESPN posts a fight's stats a little after it: try again at the next grade
        yes = yes_happened(spec, out, value)
        e["actual"] = value if value is not None else out.get("method")
        if yes is None:
            e["result"], e["profit"] = "void", 0.0
        else:
            won = yes == bool(e.get("yes", True))
            e["result"] = "won" if won else "lost"
            dec = (1 / e["price"]) if e.get("price") else (_decimal(e["odds"]) if e.get("odds") else None)
            e["profit"] = (round(dec - 1, 4) if won else -1.0) if dec else None
        n += 1
    if n:
        from .jsonio import path_lock, save_json
        with path_lock(_path(date)):
            fresh = load(date)
            fresh.update({k: data[k] for k in todo if data[k].get("result")})
            save_json(_path(date), fresh)
    return n


def _stale(date: str, days: int = 3) -> bool:
    """Old enough that stats which still aren't posted never will be (the prop is voided)."""
    try:
        return (dt.date.today() - dt.date.fromisoformat(date)).days >= days
    except ValueError:
        return True


def _decimal(odds: int) -> float:
    return 1 + (odds / 100 if odds > 0 else 100 / -odds)


def summary_lines(date: str) -> list:
    """Record of the shown plays (units at the prices shown) and of every priced prop."""
    data = [v for v in load(date).values() if v.get("result") in ("won", "lost")]
    if not data:
        return []
    out = []
    for name, rows in (("Plays shown", [v for v in data if v.get("shown")]), ("Every prop priced", data)):
        if not rows:
            continue
        won = sum(v["result"] == "won" for v in rows)
        priced = [v for v in rows if v.get("profit") is not None]
        units = sum(v["profit"] for v in priced)
        chance = sum(v.get("chance", 0) for v in rows) / len(rows)
        out.append(f"{name}: **{won}-{len(rows) - won}**" + (f" · {units:+.1f}u at 1u each" if priced else "")
                   + f" · hit {won / len(rows):.0%} vs our {chance:.0%}")
    return out
