"""Market records and CSV loading."""
import csv
from dataclasses import dataclass, field
from typing import Optional

INJURY_EXCLUDE = {"out", "doubtful", "ir", "suspended", "inactive"}
INJURY_FLAG = {"questionable", "gtd", "day-to-day", "probable"}


@dataclass
class AltLine:
    line: float
    odds: int


@dataclass
class Market:
    date: str
    sport: str
    game_id: str
    game: str
    start_confirmed: bool
    bet_type: str          # "prop", "moneyline", "spread", "total"
    subject: str           # player name, or team for game lines
    team: str
    market: str            # "points", "PRA", "shots on goal", "total", ...
    side: str              # "over", "under", "yes", or team name
    line: Optional[float]
    odds: int
    book: str
    season_hits: int
    season_games: int
    split_hits: Optional[int] = None
    split_games: Optional[int] = None
    l5: Optional[float] = None
    l10: Optional[float] = None
    l15: Optional[float] = None
    l20: Optional[float] = None
    projection: Optional[float] = None
    matchup_adj: float = 0.0
    matchup_note: str = ""
    is_combo: bool = False
    injury_status: str = "active"
    alt_lines: list = field(default_factory=list)
    notes: str = ""

    @property
    def label(self) -> str:
        line = "" if self.line is None else f" {self.line:g}"
        if self.bet_type == "prop":
            return f"{self.subject} {self.side.title()}{line} {self.market}"
        if self.bet_type == "moneyline":
            return f"{self.side} ML"
        if self.bet_type == "spread":
            return f"{self.side}{line}"
        return f"{self.game} {self.side.title()}{line}"

    @property
    def recent_windows(self) -> dict:
        return {k: v for k, v in
                (("L5", self.l5), ("L10", self.l10), ("L15", self.l15), ("L20", self.l20))
                if v is not None}


def _opt_float(v: str) -> Optional[float]:
    v = (v or "").strip()
    if not v:
        return None
    v = float(v.rstrip("%"))
    return v / 100 if v > 1 else v


def _opt_num(v: str, cast=float):
    v = (v or "").strip()
    return cast(v) if v else None


def _bool(v: str) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_alt_lines(v: str) -> list:
    """'24.5:-450|22.5:-800' -> [AltLine(24.5, -450), AltLine(22.5, -800)]"""
    out = []
    for part in (v or "").split("|"):
        part = part.strip()
        if part:
            line, odds = part.split(":")
            out.append(AltLine(float(line), int(odds)))
    return out


def load_markets(path: str) -> list:
    markets = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            markets.append(Market(
                date=row["date"].strip(),
                sport=row["sport"].strip(),
                game_id=row["game_id"].strip(),
                game=row["game"].strip(),
                start_confirmed=_bool(row.get("start_confirmed", "true")),
                bet_type=row["bet_type"].strip().lower(),
                subject=row["subject"].strip(),
                team=row.get("team", "").strip(),
                market=row["market"].strip(),
                side=row["side"].strip(),
                line=_opt_num(row.get("line")),
                odds=int(row["odds"]),
                book=row.get("book", "").strip(),
                season_hits=int(row["season_hits"]),
                season_games=int(row["season_games"]),
                split_hits=_opt_num(row.get("split_hits"), int),
                split_games=_opt_num(row.get("split_games"), int),
                l5=_opt_float(row.get("l5")),
                l10=_opt_float(row.get("l10")),
                l15=_opt_float(row.get("l15")),
                l20=_opt_float(row.get("l20")),
                projection=_opt_num(row.get("projection")),
                matchup_adj=_opt_num(row.get("matchup_adj")) or 0.0,
                matchup_note=row.get("matchup_note", "").strip(),
                is_combo=_bool(row.get("is_combo", "")),
                injury_status=(row.get("injury_status") or "active").strip().lower(),
                alt_lines=parse_alt_lines(row.get("alt_lines", "")),
                notes=row.get("notes", "").strip(),
            ))
    return markets
