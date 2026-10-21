"""American-odds conversions."""


def implied_probability(odds: int) -> float:
    """Implied probability of American odds (vig included)."""
    if odds == 0 or -100 < odds < 100:
        raise ValueError(f"invalid American odds: {odds}")
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def fair_odds(prob: float) -> int:
    """American odds that break even at the given probability."""
    if not 0 < prob < 1:
        raise ValueError(f"probability must be between 0 and 1: {prob}")
    if prob >= 0.5:
        return -round(prob / (1 - prob) * 100)
    return round((1 - prob) / prob * 100)


def fmt_odds(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def decimal_to_american(dec: float) -> int:
    if dec <= 1:
        raise ValueError(f"invalid decimal odds: {dec}")
    return round((dec - 1) * 100) if dec >= 2 else -round(100 / (dec - 1))


def american_to_decimal(odds: int) -> float:
    return 1 + (odds / 100 if odds > 0 else 100 / -odds)


def parse_odds(text: str) -> int:
    """'-110', '+150', '150' -> American; '2.00', '3.5', '1.82' -> decimal, converted."""
    t = text.strip()
    if t.startswith(("+", "-")):
        return int(float(t))
    v = float(t)
    if "." in t or v < 100:
        return decimal_to_american(v)
    return int(v)


def net_profit(odds: int, stake: float = 100.0) -> float:
    return stake * (odds / 100 if odds > 0 else 100 / -odds)


def expected_value(prob: float, odds: int, stake: float = 100.0, push: float = 0.0) -> float:
    """EV = P(win) x net profit - P(loss) x stake. `prob` excludes pushes; `push` is P(push)."""
    decided = 1 - push
    return prob * decided * net_profit(odds, stake) - (1 - prob) * decided * stake


def fmt_money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):.2f}"
