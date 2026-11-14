"""UFC fight props priced against Kalshi: go the distance, method of victory, round of finish.

The Odds API carries only fight winner and total rounds for MMA, so prop prices come from
Kalshi's public market data (the same API the bot reads NBA/NFL win chances from):
KXUFCFIGHT (winner), KXUFCDISTANCE (goes the distance), KXUFCMOV (fighter by KO/TKO,
submission or decision) and KXUFCROUNDS (fight ends before round N). One call per series.

The model, tested on 5,461 UFC fights with method-of-victory odds (2010-26):
  win chance   the market-anchored fight model (ufc_model.anchored)
  finish rate  a fighter's (KO + sub wins + 2) / (wins + 4): pulled toward the UFC's 50%
  KO share     (KO wins + 1.9) / (finishes + 3): 64% of UFC finishes are KO/TKO
  method       P(A by KO) = P(A wins) x finish rate x KO share, and so on
  round        finishes front-loaded (sim.finish_share: 52/30/18 in a 3-rounder)
Blended PROP_WEIGHT (0.3) with the market, it beat the method odds alone on 2020-26 fights
(6-way log loss 1.5375 vs 1.5481; goes the distance 0.6661 vs 0.6743). On its own it's worse
than the market, so the market keeps most of the weight; round props only ROUND_WEIGHT.

The test used UFC-only records; ESPN's are whole pro records, where regional fights end early
more often (tonight's card read 69% finishes against the UFC's ~50%). So the card's finish
level is set by the market and the model keeps only which fights are more (or less)
finish-prone than priced (recenter_finish).

Kalshi's prices are the market; the bot's chance is for betting the same prop at FanDuel,
DraftKings or Kalshi itself: "worth it at" is the price that pays for the chance plus 2%.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from .oddsapi import norm

log = logging.getLogger("linemaker.ufc_props")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"fight": "KXUFCFIGHT", "distance": "KXUFCDISTANCE", "mov": "KXUFCMOV", "rounds": "KXUFCROUNDS"}
PROP_WEIGHT = 0.3
ROUND_WEIGHT = 0.15     # the round curve is the same for every fighter: it never saw round prices
FINISH_PRIOR, FINISH_N = 0.5, 4
KO_PRIOR, KO_N = 0.64, 3
MIN_EDGE = 0.03         # shown when the bot's chance beats the market mid by this much
MAX_SHOWN = 10
METHODS = (("KO", " by KO/TKO/DQ"), ("SUB", " by Submission"), ("DEC", " by Decision"))
_CACHE = {}             # series -> (time, markets)


def _get(series: str) -> list:
    """Open markets of one Kalshi series (cached 5 minutes; backs off when rate-limited)."""
    hit = _CACHE.get(series)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    url = f"{KALSHI}/markets?series_ticker={series}&status=open&limit=1000"
    req = urllib.request.Request(url, headers={"User-Agent": "Linemaker/1.0", "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read()).get("markets", [])
            _CACHE[series] = (time.time(), data)
            return data
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
    return []


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _quote(m: dict) -> Optional[dict]:
    """{"mid", "bid", "ask"} for YES, or None when there's no two-sided market."""
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    if bid is None or ask is None or ask <= 0 or ask < bid:
        return None
    return {"mid": (bid + ask) / 2, "bid": bid, "ask": ask}


def _code(m: dict) -> str:
    """The fight's code shared by every series: KXUFCMOV-26NOV14GANHOK -> 26NOV14GANHOK."""
    parts = (m.get("event_ticker") or "").split("-")
    return parts[1] if len(parts) > 1 else ""


def kalshi_fights(fetch=_get) -> dict:
    """{fight code: {"names": [..], "win": {name: quote}, "distance": quote,
    "mov": {(name, method): quote}, "draw": quote, "rounds": {n: quote}}}."""
    out = {}
    for kind, series in SERIES.items():
        try:
            markets = fetch(series)
        except Exception as e:
            log.warning("kalshi %s: %s", series, e)
            continue
        for m in markets:
            q, code, sub = _quote(m), _code(m), (m.get("yes_sub_title") or "").strip()
            if not q or not code:
                continue
            f = out.setdefault(code, {"names": [], "win": {}, "mov": {}, "rounds": {}})
            if kind == "fight":
                f["win"][sub] = q
                f["names"].append(sub)
            elif kind == "distance":
                f["distance"] = q
            elif kind == "rounds":
                try:
                    f["rounds"][int(m["ticker"].rsplit("-", 1)[1])] = q
                except (KeyError, ValueError):
                    pass
            elif kind == "mov":
                if sub.lower().startswith("draw"):
                    f["draw"] = q
                    continue
                for meth, suffix in METHODS:
                    if sub.endswith(suffix):
                        f["mov"][(sub[: -len(suffix)].strip(), meth)] = q
    return out


def same_fighter(a: str, b: str) -> bool:
    """ESPN vs Kalshi spellings: "Alatengheili" / "Alateng Heili", "Bia" / "Beatriz Mesquita"."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if _spellings(na) & _spellings(nb):   # "Heili Alateng" (DraftKings) = "Alatengheili" (ESPN)
        return True
    return na.split()[-1] == nb.split()[-1]


def _spellings(name: str) -> set:
    """The name with its words run together, in every order (names written family-name first)."""
    from itertools import permutations
    words = name.split()
    return {"".join(p) for p in permutations(words)} if len(words) <= 4 else {"".join(words)}


def _who(name: str, a: str, b: str) -> Optional[str]:
    return a if same_fighter(name, a) else b if same_fighter(name, b) else None


def match(kalshi: dict, a: str, b: str) -> Optional[dict]:
    """The Kalshi fight for ESPN's a vs b, with every name mapped to a or b."""
    for f in kalshi.values():
        names = f["names"] or list({n for n, _m in f["mov"]})
        if len(names) == 2 and {_who(n, a, b) for n in names} == {a, b}:
            return {"win": {_who(n, a, b): q for n, q in f["win"].items()},
                    "mov": {(_who(n, a, b), m): q for (n, m), q in f["mov"].items() if _who(n, a, b)},
                    "distance": f.get("distance"), "draw": f.get("draw"), "rounds": f["rounds"]}
    return None


def finish_rate(f: dict) -> float:
    return ((f.get("finishes") or 0) + FINISH_PRIOR * FINISH_N) / ((f.get("wins") or 0) + FINISH_N)


def ko_share(f: dict) -> float:
    kos, subs = f.get("kos"), f.get("subs")
    if kos is None or subs is None:
        return KO_PRIOR
    return (kos + KO_PRIOR * KO_N) / (kos + subs + KO_N)


def model_outcomes(fa: dict, fb: dict, p_a: float) -> dict:
    """{(fighter key "a"/"b", method): chance} for the six ways the fight can be won."""
    out = {}
    for key, f, p in (("a", fa, p_a), ("b", fb, 1 - p_a)):
        fin, ko = finish_rate(f), ko_share(f)
        out[(key, "KO")] = p * fin * ko
        out[(key, "SUB")] = p * fin * (1 - ko)
        out[(key, "DEC")] = p * (1 - fin)
    return out


def american(p: float) -> str:
    p = min(max(p, 0.01), 0.99)
    return f"{-round(100 * p / (1 - p)):+d}" if p >= 0.5 else f"+{round(100 * (1 - p) / p)}"


def worth_at(p: float, edge: float = 0.02) -> str:
    """The longest price that still isn't worth it, so anything better is: pays chance + edge."""
    dec = (1 + edge) / max(p, 0.01)
    return f"{-round(100 / (dec - 1)):+d}" if dec < 2 else f"+{round(100 * (dec - 1))}"


def _logit(p: float) -> float:
    import math
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def recenter_finish(models: list, markets: list) -> float:
    """Logit shift that puts the card's average decision chance on the market's."""
    pairs = [(m, k) for m, k in zip(models, markets) if m is not None and k is not None]
    if not pairs:
        return 0.0
    return sum(_logit(k) - _logit(m) for m, k in pairs) / len(pairs)


def _apply_shift(model: dict, shift: float) -> dict:
    """Decision outcomes scaled to the shifted decision chance, finishes to the rest
    (each fighter's win chance unchanged)."""
    import math
    dec = model[("a", "DEC")] + model[("b", "DEC")]
    if not 0 < dec < 1 or not shift:
        return model
    new = 1 / (1 + math.exp(-(_logit(dec) + shift)))
    return {k: v * (new / dec if k[1] == "DEC" else (1 - new) / (1 - dec)) for k, v in model.items()}


def fight_props(card: list, info: dict, kalshi: dict) -> list:
    """Per fight on the card: [{"fight", "props": [{label, chance, market, model, side_yes}]}].
    card: [(name a, id a, name b, id b, competition)] (ufc_model.card); info: id -> fighter."""
    from .sim import finish_share
    from .ufc_model import anchor_shift, anchored
    from .ufc_model import anchor_shift as _sh, anchored as _an
    staged = []
    for a, ida, b, idb, comp in card:
        k = match(kalshi, a, b)
        if not k:
            continue
        staged.append((a, ida, b, idb, comp, k))
    raw = []
    for a, ida, b, idb, comp, k in staged:   # first pass: the card's finish level vs the market
        fa, fb = info.get(ida) or {}, info.get(idb) or {}
        m = model_outcomes(fa, fb, 0.5)
        raw.append((m[("a", "DEC")] + m[("b", "DEC")], k["distance"]["mid"] if k.get("distance") else None))
    shift = recenter_finish([r[0] for r in raw], [r[1] for r in raw])
    out = []
    for a, ida, b, idb, comp, k in staged:
        fa, fb = info.get(ida) or {}, info.get(idb) or {}
        rounds = ((comp.get("format") or {}).get("regulation") or {}).get("periods") or 3
        wa, wb = k["win"].get(a), k["win"].get(b)
        if wa and wb:
            p_mkt = wa["mid"] / (wa["mid"] + wb["mid"])
        else:   # no winner market: the method prices imply it
            sa = sum(q["mid"] for (n, _m), q in k["mov"].items() if n == a)
            sb = sum(q["mid"] for (n, _m), q in k["mov"].items() if n == b)
            p_mkt = sa / (sa + sb) if sa + sb else 0.5
        p_a = anchored(p_mkt, anchor_shift(fa, fb)[0])
        model = _apply_shift(model_outcomes(fa, fb, p_a), shift)
        mov_total = sum(q["mid"] for q in k["mov"].values()) + (k["draw"]["mid"] if k.get("draw") else 0)
        props = []
        names = {"a": a, "b": b}
        for (key, meth), pm in model.items():
            q = k["mov"].get((names[key], meth))
            if not q or not mov_total:
                continue
            mkt = q["mid"] / mov_total
            props.append(_prop(f"{names[key]} by {dict(KO='KO/TKO', SUB='submission', DEC='decision')[meth]}",
                               pm, mkt, q, spec={"kind": "mov", "fighter": names[key], "method": meth}))
        dist_model = model[("a", "DEC")] + model[("b", "DEC")]
        if k.get("distance"):
            props.append(_prop("Goes the distance", dist_model, k["distance"]["mid"], k["distance"],
                               spec={"kind": "distance"}))
        for n, q in sorted(k["rounds"].items()):
            if n > rounds:
                continue
            pm = (1 - dist_model) * finish_share(n - 1, rounds)
            props.append(_prop(f"Ends before round {n}", pm, q["mid"], q, ROUND_WEIGHT,
                               spec={"kind": "rounds", "before": n}))
        out.append({"fight": f"{a} vs {b}", "a": a, "b": b, "rounds": rounds, "win_a": p_a,
                    "finish": 1 - dist_model, "props": [p for p in props if p], "when": comp.get("date", "")})
    return out


LIVE_EDGE = 0.03   # a Kalshi price under 3% or over 97% is a fight under way or a dead market: skipped


def _prop(label: str, model: float, market: float, quote: dict, weight: float = PROP_WEIGHT,
          spec: Optional[dict] = None) -> Optional[dict]:
    if not LIVE_EDGE <= market <= 1 - LIVE_EDGE:
        return None
    chance = weight * model + (1 - weight) * market
    yes = chance >= market
    return {"label": label if yes else _negate(label), "chance": chance if yes else 1 - chance,
            "market": market if yes else 1 - market, "model": model if yes else 1 - model,
            "kalshi_price": quote["ask"] if yes else 1 - quote["bid"], "yes": yes, "spec": spec or {}}


def _negate(label: str) -> str:
    if label == "Goes the distance":
        return "Doesn't go the distance"
    if label.startswith("Ends before round"):
        return "Lasts into round " + label.rsplit(" ", 1)[1]
    return f"NOT {label}"


def best_props(fights: list, min_edge: float = MIN_EDGE, per_fight: int = 2) -> list:
    """[(fight, prop)] where the bot's chance beats the market by min_edge, biggest first."""
    out = []
    for f in fights:
        good = sorted((p for p in f["props"] if p["chance"] - p["market"] >= min_edge),
                      key=lambda p: p["chance"] - p["market"], reverse=True)[:per_fight]
        out += [(f, p) for p in good]
    return sorted(out, key=lambda x: x[1]["chance"] - x[1]["market"], reverse=True)


def props_lines(date: str) -> tuple:
    """(lines for the props embed, fights) for the card on `date`."""
    from concurrent.futures import ThreadPoolExecutor
    from .ufc_model import card, fighter, not_started
    fights = [f for f in card(date) if not_started(f[4])]   # under way: Kalshi is pricing the live fight
    ids = sorted({i for f in fights for i in (f[1], f[3])})
    with ThreadPoolExecutor(max_workers=8) as pool:
        info = dict(zip(ids, pool.map(fighter, ids)))
    priced = fight_props(fights, info, kalshi_fights())
    lines = []
    for f, p in best_props(priced)[:MAX_SHOWN]:
        lines.append(f"**{p['label']}** · {f['fight']}\n"
                     f" our {p['chance']:.0%} vs Kalshi {p['market']:.0%} · fair {american(p['chance'])} · "
                     f"worth it at **{worth_at(p['chance'])}** or better (Kalshi {p['kalshi_price'] * 100:.0f}¢)")
    return lines, priced


TYPED_TIP = ("Strike / takedown lines: post a screenshot of the DraftKings / FanDuel props page, or type "
             "the line, e.g. `hokit over 45.5 sig strikes -115`.")
STAT_FOOTER = ("Strike / takedown lines: our chance = 30% model + 70% the books' consensus (a pick'em line "
               "counts as the 50/50 point); untested model, heed the ⚠️ flags.")
FOOTER = ("Our chance = 30% model + 70% Kalshi (round props 15% model). Worth it at = the price at "
          "FanDuel / DraftKings (or Kalshi) that's +2% EV: take anything better. Fights under way are "
          "left out.")


def props_reply(date: str) -> str:
    """The typed-ask answer ("ufc props"): the card's best fight props, phone-sized."""
    from .ufc_model import next_card_date
    try:
        day = next_card_date(date)
        lines, priced = props_lines(day)
    except Exception as e:
        log.warning("ufc props %s: %s", date, e)
        return f"⚠️ Couldn't load UFC fight props right now ({type(e).__name__}). Try again in a minute."
    if not priced:
        return ("No UFC fight props to price: Kalshi has no open markets for the next card yet, or every "
                "fight has started. Ask again closer to fight night.")
    head = f"🥊 **UFC fight props vs Kalshi** · {day} · {len(priced)} fights priced"
    body = "\n".join(lines) if lines else "No prop is 3+ points better than Kalshi's price right now."
    slines, snote = propline_lines(day)
    stats = ("\n\n🥊 **Strike & takedown lines**\n" + "\n".join(slines)) if slines else ""
    return head + "\n\n" + body + stats + "\n\n" + FOOTER + "\n" + (snote or STAT_FOOTER)


# ---- fighter stat props you type in: "hokit over 45.5 sig strikes -130" -------------------
# No allowed feed carries strike / takedown lines (the Odds API has none for MMA; ufc.com
# blocks automated requests), so these are priced on a line you give. The model: the fighter's
# ESPN rate (pulled toward the card average by sample size) times the expected fight length,
# simulated so a quick finish drags every over down together (ufc_model.fighter_props, sim
# "ufccount"). It has never been tested and doesn't know the opponent's defense, so the line you
# typed is treated as the market: STAT_WEIGHT model, the rest the line's own chance (50/50, or
# your price less half a normal 4.5% margin), like every other UFC market here.
STAT_WEIGHT = 0.3
import re as _re

STAT_RE = _re.compile(r"\b(?P<side>over|under|o|u)\s*(?P<line>\d+(?:\.\d+)?)\s*"
                      r"(?P<stat>sig(?:nificant)?\.?\s*str(?:ikes?)?|ss|takedowns?|fight\s*time)\b"   # not "tds" / "minutes": NFL / NBA
                      r"(?:\s*(?:at|@)?\s*(?P<odds>[+-]\d{3,4}))?",
                      _re.I)


def stat_prop_ask(text: str) -> Optional[dict]:
    m = STAT_RE.search(text or "")
    if not m:
        return None
    stat = m.group("stat").lower()
    kind = ("takedowns" if stat.startswith("t") and not stat.startswith("time")
            else "fight time" if stat.startswith("fight") else "significant strikes")
    return {"over": m.group("side").lower().startswith("o"), "line": float(m.group("line")), "stat": kind,
            "odds": int(m.group("odds")) if m.group("odds") else None}


def line_chance(odds: Optional[int]) -> float:
    """The market's chance for the side you typed: 50/50 without a price, else the price's
    implied chance less half of a normal 4.5% two-way margin."""
    if odds is None:
        return 0.5
    imp = 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)
    return min(max(imp - 0.0225, 0.02), 0.98)


def price_stat_prop(row: dict, stat: str, line: float, over: bool) -> Optional[float]:
    """Chance the side hits, from one fighter row of ufc_model.fighter_props (pushes removed)."""
    from .sim import simulate_ladder
    rounds = row.get("rounds") or 3
    if stat == "fight time":
        res = simulate_ladder(row["early"], line, "ufcminutes", rounds, 20000, f"ufcmin|{row['fighter']}")
    else:
        mean = row.get("sig_strikes" if stat == "significant strikes" else "takedowns")
        if mean is None:
            return None
        res = simulate_ladder(mean, line, f"ufccount|{row['early']:.4f}|{rounds}", 0.0, 20000,
                              f"ufc|{row['fighter']}|{stat}")
    decided = res.p_over + res.p_under
    if not decided:
        return None
    return (res.p_over if over else res.p_under) / decided


def stat_prop_reply(date: str, text: str) -> Optional[str]:
    """The answer to a typed fighter stat line, or None when the text isn't one."""
    ask = stat_prop_ask(text)
    if ask is None:
        return None
    from .ufc_model import fighter_props, next_card_date
    try:
        rows = fighter_props(next_card_date(date))
    except Exception as e:
        log.warning("ufc stat prop: %s", e)
        return f"⚠️ Couldn't load the UFC card right now ({type(e).__name__}). Try again in a minute."
    words = set(norm(text).split())
    hits = [r for r in rows if norm(r["fighter"]) in norm(text)
            or (norm(r["fighter"]).split() and norm(r["fighter"]).split()[-1] in words)]
    if len(hits) != 1:
        names = ", ".join(r["fighter"] for r in rows[:30])
        return ("Name one fighter on the card with the line, e.g. `hokit over 45.5 sig strikes`."
                + (f" On the card: {names}." if names else ""))
    row = hits[0]
    p = price_stat_prop(row, ask["stat"], ask["line"], ask["over"])
    if p is None:
        return f"ESPN has no {ask['stat']} numbers for {row['fighter']}, so I can't price that line."
    proj = {"significant strikes": row.get("sig_strikes"), "takedowns": row.get("takedowns"),
            "fight time": row.get("minutes")}[ask["stat"]]
    side = "Over" if ask["over"] else "Under"
    mkt = line_chance(ask.get("odds"))
    chance = STAT_WEIGHT * p + (1 - STAT_WEIGHT) * mkt
    price = f" {ask['odds']:+d}" if ask.get("odds") else ""
    lines = [f"🥊 **{row['fighter']} {side} {ask['line']:g} {ask['stat']}{price}** vs {row['opponent']}",
             f"Our chance **{chance:.0%}** · fair {american(chance)} · "
             + (f"worth it at **{worth_at(chance)}** or better" if chance > mkt + 0.01
                else "no edge on this side")]
    lines.append(f"Model alone {p:.0%} (projects {proj:.1f}; fight lasts {row['minutes']:.1f} min on average, "
                 f"{row['early']:.0%} chance it ends early) · the line counts 70%"
                 + ("" if ask.get("odds") else " as a 50/50 point (add the price, e.g. `-130`, to use it)"))
    small = (row.get("stat_fights") or 0) < 5
    far = ask["stat"] != "fight time" and ask["line"] > 0 and proj and not 0.6 <= proj / ask["line"] <= 1.6
    if small or far:
        lines.append("⚠️ " + " and ".join(x for x in (
            "few fights on record" if small else "",
            "the projection is far from the book's line, which usually means the model is missing something "
            "(opponent's defense, level of competition)" if far else "") if x) + ": go small or skip.")
    lines.append("Untested model (no strike/takedown line history to check it on).")
    return "\n".join(lines)


# ---- sportsbook screenshots: post a DraftKings / FanDuel prop page, the bot prices it -------
# The AI model reads the lines off your screenshot (chat.read_screenshot, structured JSON);
# each UFC strike / takedown / fight-time / total-rounds line is then priced like a typed
# line. Milestone ladders ("50+ +215") have only one side, so their margin can't be removed
# from the other side: LADDER_HOLD is assumed instead (those ladders carry far more than a
# two-way line's 4-5%).
LADDER_HOLD = 0.15
SCREENSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "sport": {"type": "string", "enum": ["UFC", "other"]},
        "lines": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "fighter": {"type": "string"},
                "stat": {"type": "string", "enum": ["significant strikes", "takedowns", "fight time",
                                                    "total rounds", "other"]},
                "side": {"type": "string", "enum": ["over", "under", "at_least"]},
                "line": {"type": "number"},
                "odds": {"type": "integer"},
            },
            "required": ["fighter", "stat", "side", "line", "odds"],
            "additionalProperties": False,
        }},
    },
    "required": ["sport", "lines"],
    "additionalProperties": False,
}
SCREENSHOT_PROMPT = (
    "This is a screenshot of a sportsbook app (DraftKings, FanDuel or similar). List every betting "
    "line you can read.\n"
    "- sport: \"UFC\" for UFC / MMA fights, otherwise \"other\".\n"
    "- One entry per price shown. fighter: the fighter the line is about (for a fight-level line such as "
    "total rounds, either fighter's name in that fight).\n"
    "- stat: significant strikes, takedowns, fight time (minutes) or total rounds. Anything else "
    "(moneyline, method, spread, parlays) is \"other\".\n"
    "- side: \"over\" / \"under\" for over-under lines; \"at_least\" for milestone ladders shown as "
    "\"50+\" (line 50).\n"
    "- line: the number (3.5, 50, 1.5). odds: American odds as an integer (+215 -> 215, -166 -> -166); "
    "0 if not visible.\n"
    "- A price that's cut off at the screen edge: include it only if both the number and the odds are "
    "fully readable. Never guess.")


def _implied(odds: int) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def price_rounds(row: dict, line: float, over: bool) -> Optional[float]:
    """Chance a total-rounds side hits (rounds completed, a stoppage mid-round counting part)."""
    from .sim import simulate_ladder
    res = simulate_ladder(row["early"], line, "ufcrounds", row.get("rounds") or 3, 20000,
                          f"ufcrounds|{row['fighter']}")
    decided = res.p_over + res.p_under
    return (res.p_over if over else res.p_under) / decided if decided else None


def screenshot_reply(date: str, data: dict) -> str:
    """The reply to a screenshot the AI read: each UFC line priced, best edge first."""
    lines = (data or {}).get("lines") or []
    if (data or {}).get("sport") != "UFC":
        return ("📸 I can price **UFC** prop screenshots for now (significant strikes, takedowns, fight "
                "time, total rounds). For other sports, ask for picks as usual.")
    if not lines:
        return "📸 I couldn't read any lines in that screenshot. Try one where the odds are fully on screen."
    from .ufc_model import fighter_props, next_card_date
    try:
        rows = fighter_props(next_card_date(date))
    except Exception as e:
        log.warning("ufc screenshot: %s", e)
        return f"⚠️ Couldn't load the UFC card right now ({type(e).__name__}). Try again in a minute."
    priced, skipped = [], []
    for l in lines:
        stat, odds, side = l.get("stat"), l.get("odds") or 0, l.get("side")
        label = l.get("fighter") or "?"
        if stat == "other":
            skipped.append(f"{label} (not a strike / takedown / time / rounds line)")
            continue
        if not odds or abs(odds) < 100:
            skipped.append(f"{label} {l.get('line')} (odds not readable)")
            continue
        row = next((r for r in rows if same_fighter(r["fighter"], label)), None)
        if row is None:
            skipped.append(f"{label} (not on the card, or the fight has started)")
            continue
        at_least = side == "at_least"
        line, over = (float(l["line"]) - 0.5, True) if at_least else (float(l["line"]), side == "over")
        if stat == "total rounds":
            model, weight = price_rounds(row, line, over), ROUND_WEIGHT
        else:
            model, weight = price_stat_prop(row, stat, line, over), STAT_WEIGHT
        if model is None:
            skipped.append(f"{label} {stat} (no ESPN numbers to price it)")
            continue
        imp = _implied(odds)
        market = imp / (1 + LADDER_HOLD) if at_least else line_chance(odds)
        chance = weight * model + (1 - weight) * market
        priced.append((chance - imp, l, row, chance, model, imp, at_least, over))
    priced.sort(key=lambda x: x[0], reverse=True)
    out = [f"📸 **Read {len(lines)} line{'s' if len(lines) != 1 else ''} from your screenshot** · "
           f"{sum(1 for p in priced if p[0] >= 0.02)} worth a look"]
    for edge, l, row, chance, model, imp, at_least, over in priced[:15]:
        stat = {"significant strikes": "sig strikes"}.get(l["stat"], l["stat"])
        who = "" if l["stat"] == "total rounds" else f"{row['fighter']} "
        what = f"{l['line']:g}+" if at_least else f"{'Over' if over else 'Under'} {l['line']:g}"
        tail = (f"✅ worth it at **{worth_at(chance)}** or better" if edge >= 0.02 else "no edge")
        out.append(f"**{who}{what} {stat}** {l['odds']:+d}\n our {chance:.0%} vs price {imp:.0%} "
                   f"(model {model:.0%}) · {tail}")
    if skipped:
        out.append("Skipped: " + "; ".join(skipped[:6]) + (f" (+{len(skipped) - 6} more)" if len(skipped) > 6 else ""))
    out.append("Read by AI from your screenshot: check the numbers match before betting. Our chance = 30% "
               "model + 70% the book's price with its margin removed (rounds 15% model; \"50+\" ladders "
               "assume a 15% margin). The strike model is untested: small bets.")
    return "\n".join(out)


# ---- strike / takedown / fight-time lines from PropLine (needs PROPLINE_API_KEY) ------------
PROPLINE_UFC = {"player_significant_strikes": "significant strikes", "player_takedowns": "takedowns",
                "fight_time_minutes": "fight time"}
DFS_BOOKS = {"prizepicks", "underdog", "underdogfantasy", "sleeper", "dabble", "parlayplay", "pick6",
             "draftkingspick6"}


def _bkey(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def propline_props(events: list, odds_for) -> list:
    """[{"fighter", "fight": (a, b), "stat", "line", "books": {title: {"over", "under"}},
    "dfs": {title: {...}}, "fair_over"}] from PropLine UFC events; odds_for(event_id) -> odds."""
    out = []
    for ev in events:
        try:
            data = odds_for(ev["id"])
        except Exception as e:
            log.warning("propline ufc odds %s: %s", ev.get("id"), e)
            continue
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        groups = {}
        for bk in data.get("bookmakers", []):
            title = bk.get("title") or bk.get("key", "")
            dfs = _bkey(bk.get("key") or title) in DFS_BOOKS or _bkey(title) in DFS_BOOKS
            for mk in bk.get("markets", []):
                stat = PROPLINE_UFC.get(mk.get("key"))
                if not stat:
                    continue
                for o in mk.get("outcomes", []):
                    side = (o.get("name") or "").lower()
                    if side not in ("over", "under") or o.get("point") is None or o.get("price") is None:
                        continue
                    if (o.get("dfs_odds_type") or "standard") not in ("standard", ""):
                        continue   # PrizePicks goblin / demon lines pay differently from the break-even
                    who = o.get("description") or home   # fight time is fight-level
                    g = groups.setdefault((who, stat, float(o["point"])), {"books": {}, "dfs": {}, "mult": {}, "ids": {}})
                    g["dfs" if dfs else "books"].setdefault(title, {})[side] = int(o["price"])
                    if o.get("book_outcome_id"):
                        g["ids"].setdefault(title, {})[side] = str(o["book_outcome_id"])
                    if o.get("payout_multiplier") is not None:
                        g["mult"].setdefault(title, {})[side] = float(o["payout_multiplier"])
        for (who, stat, line), g in groups.items():
            pairs = [(_implied(q["over"]), _implied(q["under"])) for q in g["books"].values()
                     if "over" in q and "under" in q]
            if not pairs:   # no sportsbook: a pick'em app's own two-way price (Underdog, Sleeper) still leans
                pairs = [(_implied(q["over"]), _implied(q["under"])) for q in g["dfs"].values()
                         if "over" in q and "under" in q and not (q["over"] == q["under"] == 100)]
            fair = sum(o / (o + u) for o, u in pairs) / len(pairs) if pairs else None
            out.append({"fighter": who, "fight": (home, away), "stat": stat, "line": line,
                        "books": g["books"], "dfs": g["dfs"], "mult": g["mult"], "ids": g["ids"], "fair_over": fair})
    return out


def propline_edges(date: str, min_edge: float = MIN_EDGE) -> tuple:
    """([row], props seen): the card's priced strike / takedown / time lines, best first.
    Sportsbooks: our chance vs your books' best price. Pick'em apps (PrizePicks, Underdog): our
    chance vs the app's per-leg break-even."""
    from . import propline
    from .pp import break_even, use_app
    from .pro import parlay_books
    from .ufc_model import fighter_props, next_card_date, not_started
    day = next_card_date(date)
    rows = fighter_props(day)   # the card's fights not under way
    evs = [e for e in propline.events("UFC")   # only those: PropLine lists every upcoming card
           if any(same_fighter(r["fighter"], e.get("home_team", "")) or same_fighter(r["fighter"], e.get("away_team", ""))
                  for r in rows)]
    props = propline_props(evs, lambda eid: propline.event_odds("UFC", eid, ",".join(PROPLINE_UFC), ids=True))
    if not props:
        return [], 0
    mine = {_bkey(b): b for b in parlay_books()}
    apps = {}   # the pick'em apps the bot plays: their own per-leg break-even
    for app in ("prizepicks", "underdog"):
        with use_app(app):
            apps[app] = break_even()
    out = []
    for p in props:
        row = next((r for r in rows if same_fighter(r["fighter"], p["fighter"])), None)
        if row is None and p["stat"] == "fight time":   # fight-level: either fighter's row
            row = next((r for r in rows if any(same_fighter(r["fighter"], x) for x in p["fight"])), None)
        if row is None:
            continue
        model_over = price_stat_prop(row, p["stat"], p["line"], True)
        if model_over is None:
            continue
        market_over = p["fair_over"] if p["fair_over"] is not None else 0.5   # a pick'em line is the 50/50 point
        over = STAT_WEIGHT * model_over + (1 - STAT_WEIGHT) * market_over
        proj = {"significant strikes": row.get("sig_strikes"), "takedowns": row.get("takedowns"),
                "fight time": row.get("minutes")}[p["stat"]]
        flags = []
        if (row.get("stat_fights") or 0) < 5:
            flags.append("few fights on record")
        if p["stat"] != "fight time" and p["line"] > 0 and proj and not 0.6 <= proj / p["line"] <= 1.6:
            flags.append("projection far from the line")
        for side, chance, model in (("over", over, model_over), ("under", 1 - over, 1 - model_over)):
            prices = [(q[side], title) for title, q in p["books"].items() if side in q and _bkey(title) in mine]
            if prices:
                odds, book = min(prices, key=lambda x: _implied(x[0]))   # the best price pays the most
                edge = chance - _implied(odds)
                if edge >= min_edge:
                    out.append({"prop": p, "side": side, "chance": chance, "model": model, "odds": odds,
                                "book": book, "edge": edge, "flags": flags, "proj": proj, "row": row})
            for title, q in p["dfs"].items():
                app = "underdog" if _bkey(title).startswith("underdog") else _bkey(title)
                if app not in apps or side not in q:
                    continue   # Sleeper, Dabble ...: their prices inform the market, not a pick here
                if (p.get("mult", {}).get(title, {}).get(side) or 1.0) != 1.0:
                    continue   # an Underdog leg with a payout tweak has its own break-even
                be = apps[app]
                if chance - be >= min_edge:
                    out.append({"prop": p, "side": side, "chance": chance, "model": model, "odds": None,
                                "book": title, "edge": chance - be, "flags": flags, "proj": proj, "row": row})
    out.sort(key=lambda x: x["edge"], reverse=True)
    seen, best = set(), []
    for r in out:   # the best way to play each fighter-stat (one line, one book)
        k = (r["row"]["fighter"], r["prop"]["stat"], r["side"])
        if k not in seen:
            seen.add(k)
            best.append(r)
    return best, len(props)


def propline_lines(date: str) -> tuple:
    """(lines, note) for the strike / takedown section; note explains an empty one."""
    from . import propline
    if not propline.on():
        return [], TYPED_TIP
    try:
        edges, seen = propline_edges(date)
    except propline.PropLineError as e:
        log.warning("propline ufc: %s", e)
        return [], f"Strike / takedown lines unavailable from PropLine right now ({e.code}). {TYPED_TIP}"
    except Exception as e:
        log.warning("propline ufc: %s", e)
        return [], f"Strike / takedown lines unavailable right now ({type(e).__name__}). {TYPED_TIP}"
    if not seen:
        return [], "No strike / takedown lines posted for this card yet (PropLine). " + TYPED_TIP
    lines = []
    for r in edges[:MAX_SHOWN]:
        p = r["prop"]
        stat = {"significant strikes": "sig strikes"}.get(p["stat"], p["stat"])
        who = "" if p["stat"] == "fight time" else f"{r['row']['fighter']} "
        if p["stat"] == "fight time":
            stat += f" ({' vs '.join(p['fight'])})"
        if r["odds"] is None:   # pick'em app
            word = "More" if r["side"] == "over" else "Less"
            lines.append(f"**{who}{word} {p['line']:g} {stat}** · {r['book']}\n"
                         f" our {r['chance']:.0%} vs {r['chance'] - r['edge']:.1%} break-even "
                         f"(model {r['model']:.0%}, projects {r['proj']:.1f})"
                         + (f" · ⚠️ {', '.join(r['flags'])}" if r["flags"] else ""))
        else:
            lines.append(f"**{who}{r['side'].title()} {p['line']:g} {stat}** {r['odds']:+d} {r['book']}\n"
                         f" our {r['chance']:.0%} vs price {_implied(r['odds']):.0%} (model {r['model']:.0%}, "
                         f"projects {r['proj']:.1f}) · fair {american(r['chance'])}"
                         + (f" · ⚠️ {', '.join(r['flags'])}" if r["flags"] else ""))
    note = "" if lines else f"{seen} strike / takedown lines checked: none 3+ points better than its price."
    return lines, note



# ---- the card (formatted like every other ask) and the log the grader reads -------------------
def props_card(date: str) -> tuple:
    """([discord.Embed], short text) for a UFC props ask: header, fight props vs Kalshi, strike /
    takedown lines, how they're priced. Every priced prop is logged for grading."""
    import discord
    from .scan_format import GOLD, GREY, _day
    from .ufc_model import next_card_date
    from . import propline
    day = next_card_date(date)
    _lines, priced = props_lines(day)
    plays = best_props(priced)[:MAX_SHOWN]
    shown_ids = {id(p) for _f, p in plays}
    strike_all, seen = [], 0
    strike_note = ""
    if propline.on():
        try:
            strike_all, seen = propline_edges(day, min_edge=0.0)
        except Exception as e:
            log.warning("ufc strike lines: %s", e)
            strike_note = f"Strike / takedown lines unavailable right now ({type(e).__name__})."
    strike_plays = [r for r in strike_all if r["edge"] >= MIN_EDGE][:MAX_SHOWN]
    _log_card(day, priced, shown_ids, strike_all)
    red = 0xE74C3C
    n_plays = len(plays) + len(strike_plays)
    head = [f"**{n_plays} play{'s' if n_plays != 1 else ''}** · {len(priced)} fights priced"
            + (f" · {seen} strike / takedown lines checked" if seen else ""),
            "Fight props vs Kalshi · strike & takedown lines from the books PropLine carries"]
    if not priced:
        head.append("No fight props to price: Kalshi has no open markets for the next card yet, or every fight "
                    "has started.")
    header = discord.Embed(title=f"🥊 UFC Props · {_day(day)}", color=GOLD if n_plays else GREY,
                           description="\n".join(head))
    header.set_footer(text="Prices read just now · fights under way are left out")
    out = [header]
    buttons = []
    if plays:
        try:
            offers = fight_book_offers(sorted({f["fight"] for f, _p in plays}))
        except Exception as e:
            log.warning("ufc fight offers: %s", e)
            offers = {}
        rows = []
        for i, (f, p) in enumerate(plays, 1):
            quotes = offers.get((f["fight"], offer_key(p)), {}) if offer_key(p) else {}
            books = []
            for book, (odds, url) in sorted(quotes.items()):
                good = _implied(odds) * 1.02 <= p["chance"]   # +2% EV or better at this price
                books.append(f"{'FD' if book == 'FanDuel' else 'DK'} {odds:+d}{' ✅' if good else ''}")
                if url:
                    buttons.append((f"#{i} {'FD' if book == 'FanDuel' else 'DK'} {odds:+d}", url))
            where = (" · " + " · ".join(books)) if books else ""
            rows.append(f"**{i}.** {p['label']} · **{p['chance']:.0%}**\n"
                        f"\u2003{f['fight']} · Kalshi {p['market']:.0%} · fair {american(p['chance'])} · "
                        f"bet at **{worth_at(p['chance'])}**+ (Kalshi {p['kalshi_price'] * 100:.0f}¢){where}")
        e = discord.Embed(title="🎯 Fight Props", color=red, description="\n".join(rows)[:4096])
        e.set_footer(text="Bold % = our chance · bet at = the price that's +2% EV · FD / DK = FanDuel's and "
                          "DraftKings' price for the same prop, ✅ = worth it there · buttons load the slip")
        out.append(e)
    elif priced:
        out.append(discord.Embed(title="🎯 Fight Props", color=GREY,
                                 description="No fight prop is 3+ points better than Kalshi's price right now."))
    if strike_plays:
        rows = []
        for i, r in enumerate(strike_plays, len(plays) + 1):
            p = r["prop"]
            stat = {"significant strikes": "sig strikes"}.get(p["stat"], p["stat"])
            who = " vs ".join(p["fight"]) if p["stat"] == "fight time" else r["row"]["fighter"]
            link = pickem_link(r["book"], (p.get("ids", {}).get(r["book"]) or {}).get(r["side"]), r["side"], p["line"])
            if link:
                buttons.append((f"#{i} {r['book']}", link))
            if r["odds"] is None:
                word = "More" if r["side"] == "over" else "Less"
                price = f"{r['book']} · break-even {r['chance'] - r['edge']:.0%}"
            else:
                word = r["side"].title()
                price = f"{r['book']} {r['odds']:+d} · fair {american(r['chance'])}"
            rows.append(f"**{i}.** {who} {word} {p['line']:g} {stat} · **{r['chance']:.0%}**\n"
                        f"\u2003{price} · projects {r['proj']:.1f}"
                        + (f" · ⚠️ {', '.join(r['flags'])}" if r["flags"] else ""))
        e = discord.Embed(title="🥊 Strike & Takedown Lines", color=red, description="\n".join(rows)[:4096])
        e.set_footer(text=STAT_FOOTER)
        out.append(e)
    else:
        note = strike_note or (f"{seen} strike / takedown lines checked: none 3+ points better than its price."
                               if seen else TYPED_TIP if not propline.on() else
                               "No strike / takedown lines posted for this card yet.")
        out.append(discord.Embed(title="🥊 Strike & Takedown Lines", color=GREY, description=note))
    how = discord.Embed(title="📖 How These Are Priced", color=GREY, description=(
        "Fight props: our chance = 30% model + 70% Kalshi (round props 15% model). The model: the market-anchored "
        "win chance x each fighter's finish rate and KO share, tested on 5,461 UFC fights with method odds.\n"
        "Strike / takedown lines: 30% model + 70% the books' consensus (a pick'em line is the 50/50 point); "
        "untested model, heed the ⚠️ flags.\nEvery prop shown is logged and graded in the morning report. "
        "Typed lines and screenshots work too: `hokit over 45.5 sig strikes -115`."))
    out.append(how)
    text = (f"UFC props {day}: {n_plays} plays ("
            + "; ".join(f"{p['label']} {p['chance']:.0%}" for _f, p in plays[:5]) + ")")
    return out, text, buttons[:25]


def _log_card(day: str, priced: list, shown_ids: set, strike_all: list) -> None:
    from .ufc_props_track import log_props
    entries = []
    for f in priced:
        for p in f["props"]:
            entries.append({"fight": f["fight"], "label": p["label"], "kind": p["spec"].get("kind", ""),
                            "spec": p["spec"], "yes": p["yes"], "chance": round(p["chance"], 4),
                            "market": round(p["market"], 4), "model": round(p["model"], 4),
                            "price": round(p["kalshi_price"], 4), "book": "Kalshi", "shown": id(p) in shown_ids})
    for r in strike_all:
        p = r["prop"]
        entries.append({"fight": " vs ".join(p["fight"]),
                        "label": f"{r['row']['fighter']} {r['side']} {p['line']:g} {p['stat']}",
                        "kind": "stat", "spec": {"kind": "stat", "stat": p["stat"], "line": p["line"],
                                                 "fighter": r["row"]["fighter"]},
                        "yes": r["side"] == "over", "chance": round(r["chance"], 4), "model": round(r["model"], 4),
                        "odds": r["odds"], "book": r["book"], "shown": r["edge"] >= MIN_EDGE})
    try:
        log_props(day, entries)
    except Exception as e:
        log.warning("ufc props log: %s", e)



# ---- FanDuel / DraftKings prices and betslip links for fight props (via PropLine) -----------
BET_BOOKS = {"fanduel": "FanDuel", "draftkings": "DraftKings"}


def _slip(book: str, bk: dict, outcome: dict) -> Optional[str]:
    import urllib.parse
    bid = str(outcome.get("book_outcome_id") or "")
    if book == "fanduel" and ":" in bid:
        mid, sel = bid.split(":", 1)
        return (f"https://sportsbook.fanduel.com/addToBetslip?marketId[0]={urllib.parse.quote(mid)}"
                f"&selectionId[0]={urllib.parse.quote(sel)}")
    if book == "draftkings" and bid and bk.get("link"):
        return f"{bk['link']}?outcomes={urllib.parse.quote(bid)}"
    return None


METHOD_WORDS = (("ko/tko", "KO"), ("submission", "SUB"), ("decision", "DEC"))


def _fd_methods(sport_key: str, event_id, a: str, b: str) -> dict:
    """FanDuel's method-of-victory board (KO/TKO, submission, decision per fighter) from its
    same-game builder's leg list, which carries markets the regular feed doesn't."""
    from . import propline
    data = propline._cached(f"sgplegs|{event_id}", propline.EVENTS_TTL, lambda: propline._call(
        f"/sports/{sport_key}/events/{event_id}/sgp/legs", {"bookmakers": "fanduel", "markets": "fight_outcome"}))
    out = {}
    for bk in (data or {}).get("bookmakers", []):
        for r in bk.get("legs") or []:
            name, bid = (r.get("name") or ""), str(r.get("book_outcome_id") or "")
            if r.get("market") != "fight_outcome" or r.get("price") is None or ":" not in bid:
                continue
            who, _, how = name.rpartition(" by ")
            meth = next((m for w, m in METHOD_WORDS if w == how.strip().lower()), None)
            fighter = _who(who, a, b)
            if meth and fighter:
                out[("mov", norm(fighter), meth)] = (int(r["price"]), _slip("fanduel", {}, r))
    return out


def fight_book_offers(fight_names: list) -> dict:
    """{(fight label, key): {book title: (odds, slip url)}} for the fight props FanDuel and
    DraftKings post: key ("distance", "yes"|"no") or ("mov", norm(fighter), KO|SUB|DEC)."""
    from . import propline
    if not propline.on():
        return {}
    try:
        evs = propline.events("UFC")
    except Exception as e:
        log.warning("propline ufc events: %s", e)
        return {}
    out = {}
    for label in fight_names:
        a, _, b = label.partition(" vs ")
        ev = next((x for x in evs if {_who(x.get("home_team", ""), a, b), _who(x.get("away_team", ""), a, b)} == {a, b}), None)
        if not ev:
            continue
        try:
            data = propline.event_odds("UFC", ev["id"], "fight_distance,round_betting", "fanduel,draftkings", ids=True)
        except Exception as e:
            log.warning("propline ufc fight props %s: %s", label, e)
            continue
        for bk in data.get("bookmakers", []):
            book = BET_BOOKS.get((bk.get("key") or "").lower())
            if not book:
                continue
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    if o.get("price") is None:
                        continue
                    name = o.get("name") or ""
                    if m.get("key") == "fight_distance" and name.lower() in ("yes", "no"):
                        key = ("distance", name.lower())
                    elif m.get("key") == "round_betting" and "decision" in name.lower():
                        who = _who(name.lower().split(" to win")[0].split(" by ")[0], a, b)
                        if not who:
                            continue
                        key = ("mov", norm(who), "DEC")
                    else:
                        continue
                    out.setdefault((label, key), {})[book] = (int(o["price"]), _slip(bk["key"].lower(), bk, o))
        try:   # KO/TKO and submission (and decision) at FanDuel
            for key, quote in _fd_methods(propline.sport_key("UFC"), ev["id"], a, b).items():
                out.setdefault((label, key), {}).setdefault("FanDuel", quote)
        except Exception as e:
            log.warning("fanduel ufc methods %s: %s", label, e)
    return out


def offer_key(prop: dict) -> Optional[tuple]:
    """Which FanDuel / DraftKings market a fight prop is (None: they don't post it)."""
    spec = prop.get("spec") or {}
    if spec.get("kind") == "distance":
        return ("distance", "yes" if prop["yes"] else "no")
    if spec.get("kind") == "mov" and prop["yes"]:   # the books sell the YES side only
        return ("mov", norm(spec["fighter"]), spec["method"])
    return None


def pickem_link(book: str, entry_id: Optional[str], side: str, line: float) -> Optional[str]:
    k = _bkey(book)
    if k == "prizepicks" and entry_id:
        return f"https://app.prizepicks.com/?projections={entry_id}-{'o' if side == 'over' else 'u'}-{line:g}"
    if k.startswith("underdog"):
        return "https://underdogfantasy.com/pick-em"
    return None
