"""One look for every parlay the bot shows (payout targets, volume cards, live slip):

  header (embed field name)   🟦 FanDuel · +222 · 36% hit
  body                        • Logan Henderson o14.5 pitcher outs (-132) ✅
                              • Richie Palacios o0.5 hits (-120)
                              💵 $10 → $32.22 · EV +17%

The numbers that decide a bet sit in the header, each leg gets its own line, and the money
fits on one line, so a phone screen shows one parlay per block.
"""
import re

from .odds import fmt_odds

BOOK_EMOJI = {"FanDuel": "🟦", "DraftKings": "🟩"}


def short_label(label: str) -> str:
    """'Logan Henderson Over 14.5 pitcher outs' -> 'Logan Henderson o14.5 pitcher outs'."""
    return re.sub(r" (Over|Under) (\d)", lambda m: f" {'o' if m.group(1) == 'Over' else 'u'}{m.group(2)}", label)


def odds_text(p) -> str:
    """'+230', or '≈+230' when it's an estimate of the book's same-game price."""
    return ("≈" if getattr(p, "sgp_estimated", False) else "") + fmt_odds(p.american)


def sgp_line(p) -> str:
    """Why a same-game price is an estimate, with the multiplied odds for comparison."""
    if not getattr(p, "sgp_estimated", False):
        return ""
    from .odds import decimal_to_american
    listed = getattr(p, "listed_decimal", p.decimal)
    return (f"🔗 {p.book} prices same-game legs together: ≈{fmt_odds(p.american)} estimated "
            f"(multiplied odds {fmt_odds(decimal_to_american(listed))}); check the slip's price")


def header(p, extra: str = "") -> str:
    kind = f" · {p.kind}" if getattr(p, "kind", "") else ""
    return (f"{BOOK_EMOJI.get(p.book, '🎟️')} {p.book} · {odds_text(p)} · {p.prob:.0%} hit"
            f"{kind}{extra}")


def legs_lines(p) -> list:
    from .parlay_math import leg_value
    return [f"• {short_label(ev.best.label)} ({fmt_odds(o)}){' ✅' if leg_value(ev, o) > 1 else ''}"
            for ev, o in p.legs]


def money_line(p) -> str:
    """'💵 $10 → $32.22 · EV +17%' at your suggested stake ($10 → when no tiers are set)."""
    from .staking import _money, suggested, suggested_dollars
    amount = suggested_dollars(p.prob, p.american, parlay=True) or 10.0
    fun = " · fun money" if suggested(p.prob, p.american) <= 0 else ""
    boost = f" · 🚀 +{p.boost:.0%}" if getattr(p, "boost", 0) else ""
    return f"💵 **{_money(amount)} → {_money(round(amount * p.decimal, 2))}** · EV {p.ev:+.0f}%{boost}{fun}"


def block(p, extra_lines=(), header_extra: str = "") -> tuple:
    """(field name, field value) for one parlay."""
    value = "\n".join(legs_lines(p) + [money_line(p), sgp_line(p)] + [x for x in extra_lines if x])
    return header(p, header_extra)[:256], value[:1024]
