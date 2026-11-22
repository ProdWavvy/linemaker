"""What hasn't graded: every tracked day before today, per phase (pre-game, live, PrizePicks),
with the bot's picks still waiting for a result, plus open bets whose games are all past.

A market in a game where nothing graded is waiting (the game isn't final, or the results feed
missed it): grading that day again usually picks it up. A market in a game where other markets
did grade won't ever grade (a player who didn't play, a stat the box score doesn't list).
"""
import datetime as dt
import glob
import os
import re
from typing import Optional

PHASES = (("pre-game", ""), ("live", "live"), ("PrizePicks", "pp"))
DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def tracked_days(base: str) -> list:
    return sorted({m.group(1) for p in glob.glob(os.path.join(base, "*", "*.json"))
                   if (m := DATE_FILE.match(os.path.basename(p)))})


def day_status(recs: dict) -> dict:
    graded_games = {r.get("game") for r in recs.values() if r.get("result")}
    out = {"markets": len(recs), "graded": 0, "waiting": 0, "never": 0, "picks": 0, "pick_waiting": 0,
           "pick_never": 0, "games": {}}
    for r in recs.values():
        picked = bool(r.get("picked"))
        out["picks"] += picked
        if r.get("result"):
            out["graded"] += 1
            continue
        if r.get("game") in graded_games:
            out["never"] += 1
            out["pick_never"] += picked
        else:
            out["waiting"] += 1
            out["pick_waiting"] += picked
            g = r.get("game") or "?"
            out["games"][g] = out["games"].get(g, 0) + 1
    return out


def report(today: str, tracking: Optional[str] = None, back: int = 60) -> dict:
    """{"days": [(day, phase, status)], "fix": [days worth grading again], "bets": [open past bets]}."""
    from .tracking import load_day
    tracking = tracking or os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=back)).isoformat()
    rows, fix = [], set()
    for phase, sub in PHASES:
        base = os.path.join(tracking, sub) if sub else tracking
        for d in tracked_days(base):
            if not (start <= d < today):
                continue
            st = day_status(load_day(base, d, lean=True))
            if st["waiting"] or st["pick_never"]:
                rows.append((d, phase, st))
            if st["waiting"]:
                fix.add(d)
    bets = []
    try:
        from .bets import load_bets
        for b in load_bets():
            dates = [l.get("date") or "" for l in b.get("legs") or []]
            if b.get("status") == "open" and dates and all(x and x < today for x in dates):
                bets.append(b)
    except Exception:
        pass
    return {"days": sorted(rows), "fix": sorted(fix), "bets": bets}


def lines(rep: dict) -> list:
    out = []
    for d, phase, st in rep["days"]:
        bits = []
        if st["pick_waiting"]:
            bits.append(f"**{st['pick_waiting']} pick{'s' if st['pick_waiting'] != 1 else ''} not graded**")
        if st["waiting"]:
            games = sorted(st["games"], key=lambda g: -st["games"][g])
            bits.append(f"{st['waiting']} markets in {len(games)} game{'s' if len(games) != 1 else ''} with no "
                        f"results ({', '.join(games[:3])}{'…' if len(games) > 3 else ''})")
        if st["pick_never"]:
            bits.append(f"{st['pick_never']} pick{'s' if st['pick_never'] != 1 else ''} void (didn't play / no stat)")
        out.append(f"• **{d[5:].replace('-', '/')}** {phase}: " + " · ".join(bits)
                   + f" · {st['graded']}/{st['markets']} graded")
    for b in rep["bets"][:10]:
        legs = b.get("legs") or []
        what = legs[0].get("label", "") if len(legs) == 1 else f"{len(legs)}-leg parlay"
        out.append(f"• 🎟️ Open bet past its games: {what} · {b.get('book', '')} · {b.get('user', '')}")
    return out
