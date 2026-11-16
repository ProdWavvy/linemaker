"""Linked Polymarket US accounts (one per Discord member) and the sync that keeps their bets.

Each member links their own API key with /polymarket link (a private form). Keys are kept in
~/.linemaker-secrets/polymarket.json (LINEMAKER_SECRETS_DIR): outside the code folder, the
GitHub backups and the logs, readable by the bot's user only (0600). A member can unlink at any
time; each has their own max per order (LINEMAKER_PM_MAX_STAKE caps everyone, default $25).

Sync (every 10 minutes, and on /polymarket status): every open position on the member's
account becomes a logged bet in their name, so /record and the reports count what they really
bet there. A bet they placed through the bot, or logged by hand, is matched to its position
instead of being added twice. Positions the bot didn't track are settled from Polymarket's own
result (the market's settlement, or what a sale before the end realized).
"""
import datetime as dt
import json
import logging
import os
import threading
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.pm_accounts")
_LOCK = threading.Lock()
LEAGUE_TOKENS = {"nba": "NBA", "nhl": "NHL", "nfl": "NFL", "cbb": "NCAAB", "cfb": "NCAAF", "mlb": "MLB",
                 "wnba": "WNBA", "ufc": "UFC"}


def _path() -> str:
    d = os.path.expanduser(os.environ.get("LINEMAKER_SECRETS_DIR", "~/.linemaker-secrets"))
    return os.path.join(d, "polymarket.json")


def _load() -> dict:
    try:
        with open(_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def global_max() -> float:
    try:
        return float(os.environ.get("LINEMAKER_PM_MAX_STAKE", "25"))
    except ValueError:
        return 25.0


def link(user_id: str, name: str, key_id: str, secret: str, check=None) -> dict:
    """Verify the key (it must read the account's balances) and keep it. Returns the balances."""
    from .polymarket_us import balances
    key_id, secret = key_id.strip(), secret.strip().replace(" ", "")
    got = (check or balances)((key_id, secret))
    with _LOCK:
        data = _load()
        old = data.get(str(user_id)) or {}
        data[str(user_id)] = {"key_id": key_id, "secret": secret, "name": name,
                              "linked": dt.datetime.now().isoformat(timespec="seconds"),
                              "max_stake": old.get("max_stake")}
        _save(data)
    return got


def unlink(user_id: str) -> bool:
    with _LOCK:
        data = _load()
        if data.pop(str(user_id), None) is None:
            return False
        _save(data)
    return True


def account(user_id: str) -> Optional[dict]:
    return _load().get(str(user_id))


def creds(user_id: str) -> Optional[tuple]:
    a = account(user_id)
    return (a["key_id"], a["secret"]) if a else None


def max_stake(user_id: str) -> float:
    a = account(user_id) or {}
    own = a.get("max_stake")
    return min(global_max(), float(own)) if own else global_max()


def set_max(user_id: str, amount: float) -> bool:
    with _LOCK:
        data = _load()
        if str(user_id) not in data:
            return False
        data[str(user_id)]["max_stake"] = max(0.0, float(amount))
        _save(data)
    return True


def linked() -> dict:
    """user id -> display name for every linked member."""
    return {uid: a.get("name", "") for uid, a in _load().items()}


# ---- sync --------------------------------------------------------------------------------------
def _num(x) -> float:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def league_of(slug: str) -> str:
    parts = (slug or "").lower().split("-")
    return next((LEAGUE_TOKENS[p] for p in parts if p in LEAGUE_TOKENS), "Other")


def position_label(p: dict) -> str:
    meta = p.get("marketMetadata") or {}
    title = meta.get("title") or p.get("slug", "")
    long = _num(p.get("netPosition")) > 0
    side = meta.get("outcome") or ""
    return f"{title} · {'Yes' if long else 'No'}" if not side else f"{title} · {side if long else 'No ' + side}"


def _words(text: str) -> set:
    return {w for w in norm(text).split() if len(w) > 2 and not w.isdigit()}


def sync_user(user_id: str, name: str, cr: tuple, positions=None, settlement=None, today: Optional[str] = None) -> dict:
    """Bring one member's Polymarket positions into the bet log. Returns counts."""
    from . import bets as B
    from .polymarket_us import _call, positions as fetch_positions
    from .odds import american_to_decimal
    today = today or dt.date.today().isoformat()
    open_pos = [p for p in (positions if positions is not None else fetch_positions(cr)) if _num(p.get("netPosition"))]
    out = {"added": 0, "matched": 0, "settled": 0}
    all_bets = B.load_bets()
    mine = [b for b in all_bets if b.get("user") == name and b.get("book", "").lower().startswith("polymarket")]
    seen = set()
    for p in open_pos:
        slug, net = p["slug"], _num(p.get("netPosition"))
        long, qty = net > 0, abs(net)
        cost = _num(p.get("cost")) or _num(p.get("baseCost")) or qty * _num(p.get("avgPx"))
        if not qty or cost <= 0:
            continue
        price = min(max(cost / qty, 0.01), 0.99)
        dec = 1 / price
        odds = int(round((dec - 1) * 100)) if dec >= 2 else int(round(-100 / (dec - 1)))
        seen.add((slug, long))
        bet = next((b for b in mine if b.get("pm_slug") == slug and b.get("pm_long") == long), None)
        if bet is None:   # logged by hand (or before the link): same member, Polymarket, same names
            want = _words(position_label(p))
            bet = next((b for b in mine if not b.get("pm_slug") and b.get("status") == "open"
                        and len(b.get("legs") or []) == 1
                        and len(_words(b["legs"][0].get("label", "")) & want) >= 2), None)
            if bet is not None:
                out["matched"] += 1
        if bet is not None:
            bet.update(pm_slug=slug, pm_long=long, pm_qty=qty)
            if bet.get("pm_external") or bet.get("source") == "polymarket-bot":
                bet.update(stake=round(cost, 2), odds=odds)
            continue
        all_bets.append({"id": __import__("uuid").uuid4().hex[:10],
                         "placed": dt.datetime.now().isoformat(timespec="seconds"), "user": name,
                         "kind": "single", "book": "Polymarket", "status": "open", "stake": round(cost, 2),
                         "odds": odds, "source": "polymarket", "pm_external": True, "pm_slug": slug,
                         "pm_long": long, "pm_qty": qty,
                         "legs": [{"label": position_label(p), "sport": league_of(slug), "date": today,
                                   "bet_type": "external", "odds": odds, "book": "Polymarket"}]})
        out["added"] += 1
    # positions gone: settle the ones the bot can't grade itself
    for b in all_bets:
        if b.get("user") != name or b.get("status") != "open" or not b.get("pm_external"):
            continue
        if (b["pm_slug"], b["pm_long"]) in seen:
            continue
        try:
            s = settlement(b["pm_slug"]) if settlement else _call("GET", f"/v1/markets/{b['pm_slug']}/settlement")
        except Exception as e:
            log.info("polymarket settlement %s: %s", b["pm_slug"], e)
            continue
        val = s.get("settlement") if isinstance(s, dict) else None
        if val is None:
            continue
        won = (float(val) >= 0.5) == bool(b["pm_long"])
        qty = b.get("pm_qty") or b["stake"] * american_to_decimal(b["odds"])
        b["status"] = "won" if won else "lost"
        b["profit"] = round(qty - b["stake"], 2) if won else -b["stake"]
        b["settled"] = dt.datetime.now().isoformat(timespec="seconds")
        out["settled"] += 1
    if any(out.values()):
        B.save_bets(all_bets)
    return out


def sync_all() -> dict:
    """Every linked member (errors for one never stop the others)."""
    out = {}
    for uid, a in _load().items():
        try:
            out[a.get("name", uid)] = sync_user(uid, a.get("name", ""), (a["key_id"], a["secret"]))
        except Exception as e:
            log.warning("polymarket sync for a linked member failed: %s", type(e).__name__)
    return out
