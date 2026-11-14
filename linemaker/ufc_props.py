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
    if na.replace(" ", "") == nb.replace(" ", ""):
        return True
    return na.split()[-1] == nb.split()[-1]


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
                               pm, mkt, q))
        dist_model = model[("a", "DEC")] + model[("b", "DEC")]
        if k.get("distance"):
            props.append(_prop("Goes the distance", dist_model, k["distance"]["mid"], k["distance"]))
        for n, q in sorted(k["rounds"].items()):
            if n > rounds:
                continue
            pm = (1 - dist_model) * finish_share(n - 1, rounds)
            props.append(_prop(f"Ends before round {n}", pm, q["mid"], q, ROUND_WEIGHT))
        out.append({"fight": f"{a} vs {b}", "a": a, "b": b, "rounds": rounds, "win_a": p_a,
                    "finish": 1 - dist_model, "props": [p for p in props if p], "when": comp.get("date", "")})
    return out


LIVE_EDGE = 0.03   # a Kalshi price under 3% or over 97% is a fight under way or a dead market: skipped


def _prop(label: str, model: float, market: float, quote: dict, weight: float = PROP_WEIGHT) -> Optional[dict]:
    if not LIVE_EDGE <= market <= 1 - LIVE_EDGE:
        return None
    chance = weight * model + (1 - weight) * market
    yes = chance >= market
    return {"label": label if yes else _negate(label), "chance": chance if yes else 1 - chance,
            "market": market if yes else 1 - market, "model": model if yes else 1 - model,
            "kalshi_price": quote["ask"] if yes else 1 - quote["bid"], "yes": yes}


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
    slines, snote = sgo_lines(day)
    stats = ("\n\n🥊 **Strike & takedown lines** (SportsGameOdds)\n" + "\n".join(slines)) if slines else ""
    return head + "\n\n" + body + stats + "\n\n" + FOOTER + (f"\n{snote}" if snote else "")


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


# ---- sportsbook strike / takedown lines (SportsGameOdds, needs a key) ---------------------
def _book_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _imp(odds: int) -> float:
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def sgo_edges(date: str, min_edge: float = MIN_EDGE) -> tuple:
    """([(prop, side, chance, best odds, book, flags)], props seen) for the card's fighter stat
    props at your betting books: chance = STAT_WEIGHT x model + the rest the de-vigged consensus."""
    from . import sgo
    from .pro import parlay_books
    from .ufc_model import fighter_props, next_card_date
    events = sgo.fetch_ufc_events()
    props = sgo.fighter_props(events)
    if not props:
        return [], 0
    rows = fighter_props(next_card_date(date))
    names = {_book_key(b): b for b in parlay_books()}
    out = []
    for p in props:
        row = next((r for r in rows if same_fighter(r["fighter"], p["fighter"])), None)
        if row is None:
            continue
        model_over = price_stat_prop(row, p["stat"], p["line"], True)
        if model_over is None:
            continue
        fair = p["fair_over"] if p["fair_over"] is not None else 0.5
        over = STAT_WEIGHT * model_over + (1 - STAT_WEIGHT) * fair
        proj = {"significant strikes": row.get("sig_strikes"), "takedowns": row.get("takedowns"),
                "fight time": row.get("minutes")}[p["stat"]]
        flags = []
        if (row.get("stat_fights") or 0) < 5:
            flags.append("few fights on record")
        if p["stat"] != "fight time" and p["line"] > 0 and proj and not 0.6 <= proj / p["line"] <= 1.6:
            flags.append("projection far from the line")
        for side, chance in (("over", over), ("under", 1 - over)):
            prices = [(o[side], names[k]) for k, o in p["books"].items() if k in names and side in o]
            if not prices:
                continue
            odds, book = min(prices, key=lambda x: _imp(x[0]))   # the best price pays the most
            if chance - _imp(odds) >= min_edge:
                out.append((p, side, chance, odds, book, flags, model_over if side == "over" else 1 - model_over, proj))
    out.sort(key=lambda x: x[2] - _imp(x[3]), reverse=True)
    return out, len(props)


def sgo_lines(date: str) -> tuple:
    """(lines, note) for the fighter stat props section; note explains an empty section."""
    from . import sgo
    if not sgo.api_key():
        return [], ("Strike / takedown lines: add a free SportsGameOdds key with "
                    "`/set SPORTSGAMEODDS_API_KEY` and FanDuel / DraftKings lines get priced here.")
    try:
        edges, seen = sgo_edges(date)
    except sgo.PlanError:
        return [], ("Strike / takedown lines: your SportsGameOdds plan doesn't include UFC (the free plan "
                    "doesn't; Rookie does). Typed lines still work: `hokit over 45.5 sig strikes -115`.")
    except Exception as e:
        log.warning("sportsgameodds: %s", e)
        return [], f"Strike / takedown lines unavailable right now ({str(e)[:150]})."
    if not seen:
        return [], "SportsGameOdds has no strike / takedown lines for this card yet."
    lines = []
    for p, side, chance, odds, book, flags, model, proj in edges[:MAX_SHOWN]:
        stat = {"significant strikes": "sig strikes"}.get(p["stat"], p["stat"])
        lines.append(f"**{p['fighter']} {side.title()} {p['line']:g} {stat}** {odds:+d} {book}\n"
                     f" our {chance:.0%} (model {model:.0%}, projects {proj:.1f}) · fair {american(chance)}"
                     + (f" · ⚠️ {', '.join(flags)}" if flags else ""))
    return lines, ("" if lines else f"{seen} strike / takedown lines checked: none 3+ points better than its price.")
