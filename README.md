# Linemaker

Betting-card generator for NBA, WNBA, NHL, NFL, NCAAF, MLB, UCL, La Liga and more.
Python 3.10+. The engine has no dependencies; the Discord bot needs `pip install -r requirements.txt`.
`python -m unittest` runs the tests.

## Discord bot

1. https://discord.com/developers/applications → New Application → **Bot** → Reset Token, copy it.
2. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions
   Send Messages, Embed Links, Attach Files. Open the URL and add the bot to your server.
3. Run it:

       pip install -r requirements.txt
       export DISCORD_TOKEN=...            # see .env.example for the other settings
       export LINEMAKER_GUILD_ID=...       # your server ID: commands show up instantly
       python -m linemaker.bot

Slash commands:

| Command | Does |
|---|---|
| `/pro slate:<csv> [date] [odds_min] [odds_max] [min_prob] [confirmed_only] [devig]` | Pro card: summary embed, one embed per pick, full card attached as .md |
| `/lite slate:<csv> [date] [odds_min] [odds_max] [min_prob]` | Lite card |
| `/grade [results:<csv>] [date] [auto]` | Grade that day's frozen snapshot; game lines auto-scored from final scores |
| `/devig side_a:-115 side_b:-105 [side_c] [method]` | Fair probabilities, overround, hold, fair odds (2- or 3-way, American or decimal) |
| `/ev probability:38 odds:+200 [stake]` | Expected value at a price, break-even, fair odds |
| `/fair probability:58` | Fair American odds |
| `/odds sport [date] [props]` | Live no-vig board + projections template CSV |
| `/live sport projections:<csv> [date] [props] [devig]` | Live prices + your projections → pro card |
| `/quota` | Odds API credits remaining |
| `/calibrate [days]` | Best blend weight from graded tracked markets |
| `/picks sport [date]` | FantasyPros projections vs live odds → pro card |

Daily auto-post: set `LINEMAKER_CHANNEL_ID`, `LINEMAKER_SLATE_DIR` and `LINEMAKER_POST_TIME`;
at that time the bot posts the pro card for `<SLATE_DIR>/<today>.csv` if it exists.
Frozen projections persist in `LINEMAKER_SNAPSHOTS`, so keep that folder on durable disk.

## Talking to the bot in plain English

Set `GEMINI_API_KEY` (free tier, from aistudio.google.com) or `ANTHROPIC_API_KEY`, and turn
on **Message Content Intent** (Developer Portal → your app →
Bot → Privileged Gateway Intents). Then mention the bot, DM it, or talk in a channel listed in
`LINEMAKER_CHAT_CHANNELS` (comma-separated channel IDs; every message there gets an answer):

    @Linemaker what's the NBA board tonight?
    @Linemaker I have Brunson at 29.5 points and the Knicks winning by 6, any edge?
    @Linemaker is 58% at +150 worth it?
    @Linemaker devig -115 / -105
    @Linemaker how did yesterday's picks do?

Gemini (`gemini-3.8-flash` by default) or Claude (`claude-opus-5-5`) reads the message and calls the same engine the slash
commands use; every number in a reply comes from a tool result. Full cards are attached as
files. Attach a projections CSV to a message to price it. The last 6 exchanges per channel are
remembered. Slash commands keep working.

## Automatic projections (FantasyPros)

Set `FANTASYPROS_API_KEY`. `/picks sport:NBA` (or "@Linemaker any plays tonight?", or
`python -m linemaker auto --sport NBA`) pulls FantasyPros expert-consensus projections,
attaches injury statuses, prices every projected player prop that FanDuel/DraftKings post,
and returns the card. Daily for NBA and MLB, weekly for NFL; NHL and soccer have no
projections. Markets: NBA points, rebounds, assists, threes, PRA; NFL passing/rushing/receiving
yards, receptions, completions, attempts, passing TDs, anytime TD (rush + rec TDs); MLB hits,
home runs, total bases, pitcher strikeouts. Players listed Out are skipped; questionable and
doubtful are flagged with a haircut.

Projections without a confirmed-lineup flag are locked for grading
`LINEMAKER_FREEZE_MINUTES` (default 30) before their game starts. A free FantasyPros key
returns sample data only; production data needs Premium (bundled with FantasyPros HOF).
Daily auto-post from FantasyPros: set `LINEMAKER_SPORTS` and leave
`LINEMAKER_PROJECTIONS_DIR` unset.

## Value-scan replies

Picks come back as a value scan: header and gates, a board ranked by probability gap, a
write-up per pick (projection, FanDuel vs DraftKings, no-vig consensus, line movement, ESPN
hit rates by season / last 5-20 / venue / opponent for NBA, WNBA, NFL, NCAAF, NHL and MLB,
Kalshi win probability, estimated pace and defensive rating, injury and correlation flags),
long-term notes and scoping notes. Each pick has buttons: Odds Comparison, Hit Rate Chart,
Line Movement and Key Stats (answered privately; they stop working after a restart). Picks
whose season hit rate (15+ games) sits below the price's implied probability are moved to the
notes. Alternate-line ladders are off by default (`LINEMAKER_ALT_LINES=on` adds them, roughly
doubling prop credits). Hit rates, Kalshi and team context come from free, unofficial feeds.

## Tracking and calibration

Every market the engine prices (all game lines and props, not just picks) is logged to
`tracking/<date>.json` with the projection and how it was built, the simulation, model and
no-vig market probabilities, blend weight, and every book's price. Records update until the
game starts, then lock. `/grade` (or `python -m linemaker grade ... --auto`) attaches final
scores for game lines and any results CSV for props. `/calibrate` (or
`python -m linemaker calibrate`) scores blend weights 0.00–1.00 by log loss and Brier score,
by market, and reports pick ROI; set the suggested `LINEMAKER_MODEL_WEIGHT` in `.env`.

## Free projections (ESPN game-log model)

`LINEMAKER_PROJECTIONS=auto` (default) uses FantasyPros and switches to the ESPN model when
FantasyPros looks like sample data or no key is set; `espn` or `fantasypros` forces one.
The ESPN model builds projections only for players with a posted prop: basketball is
minutes × per-minute rate adjusted for estimated pace and opponent defense; football is
opportunity × efficiency (targets, carries, attempts); hockey and baseball are per-game
averages with recent games blended in. Spread comes from each player's own game-to-game
variation; ESPN's injury report removes players listed Out.

## Live (in-game) model

Ask "any live edges right now", "live nba moneylines" or use `/picks bet:Live in-game lines`.
Only games in progress are priced (one /odds call per league, ~3 credits). The prior is the
pre-game market line saved by a game-lines scan before tip-off (else the ESPN-ratings model);
basketball and football treat the rest of the game as a random walk scaled to the time left,
hockey and baseball count remaining goals/runs as Poisson. The market gets most of the weight
(`LINEMAKER_LIVE_MODEL_WEIGHT`, default 0.35), odds older than `LINEMAKER_LIVE_MAX_ODDS_AGE`
seconds are dropped, the last few minutes are skipped, and every live read is tracked in
`tracking/live/` apart from the pre-game calibration. Not modelled: possession, fouls, power
plays, pitcher changes, in-game injuries. No live player props.

## Automatic weight tuning

Every morning (`LINEMAKER_AUTOGRADE_TIME`, default 09:30) the bot grades yesterday and re-fits
the model-vs-market blend on the last 60 days of graded markets; `/grade` does the same. The
result goes to `tracking/weights.json`, which new cards read immediately. Guardrails: nothing
moves before 200 graded outcomes (alt lines on one stat count once), a market gets its own weight only at 75+ (pulled toward the
overall weight), a weight moves only when it clearly fits better and by at most 0.10 per run,
and weights stay within 0.05-0.95. `LINEMAKER_AUTOTUNE=off` freezes the `.env` values;
delete `weights.json` to start over.

## Running 24/7 with automatic updates

On a Linux server (Ubuntu or Oracle Linux), clone this repo and run
`bash deploy/setup_server.sh` once. It installs the bot as the `linemaker` service (starts on
boot, restarts on crashes) and, for a git clone, the `linemaker-update` timer: every 5 minutes
it pulls new commits, installs new requirements, checks the bot still loads (rolling back if
not) and restarts it. `.env`, `tracking/`, `cache/` and `snapshots/` are git-ignored, so
updates never touch them. Logs: `sudo journalctl -u linemaker -f` and
`sudo journalctl -u linemaker-update`.

## Alerts, stakes, bet log and reports

- **Pre-tip scans** (`LINEMAKER_PRETIP_MINUTES`, default 45): props for each game shortly before
  tip, posted only when something clears the gates. Asking for a league's props ("best props
  nhl", `/picks`) turns its pre-tip scans on for the rest of the day;
  `LINEMAKER_PRETIP_SPORTS=all` runs them for every league daily.
- **Injury news** (`LINEMAKER_NEWS_MINUTES`, default 12): a newly ruled-Out rotation player
  triggers a re-price of his game with the teammate usage bump (basketball).
- **Projection adjustments** (basketball): teammate usage when someone is out, starter minutes
  trimmed for blowout risk (spread > 7) and back-to-backs, and defense vs position (what the
  opponent allows to guards / forwards / centers, from ESPN box scores, shrunk by sample size).
- **Stakes**: quarter-Kelly suggestion on every pick and parlay (`LINEMAKER_BANKROLL` for dollars).
- **Bet log**: "📝 I bet this" / "Log parlay" buttons record what you actually bet; grading
  settles them; `/record` shows your profit and ROI.
- **Closing-line value**: `/clv` and the `/calibrate` report show whether the market moved
  toward the picks after they were made.
- **Live grading**: in-game reads are graded from final scores, and the live weight is tuned
  separately with the same guardrails.
- **Weekly summary**: Mondays (`LINEMAKER_WEEKLY_TIME`), model record, CLV, your bets, weight
  changes.

## Credit savers

- One odds pull per league, date and bet kind per `LINEMAKER_PLAYS_CACHE_MINUTES` (default
  30), shared by chat, `/picks`, `/parlay` and the daily post, and kept on disk across restarts.
- Props are never bought for games that have already started (`LINEMAKER_SKIP_STARTED`).
- Always-plus-money markets (home runs, anytime goals) aren't bought while `LINEMAKER_ODDS_MIN`
  is negative, since they can't clear the gates (`LINEMAKER_SKIP_LONGSHOTS=off` to override).
- Props scans use the free /events list; props nobody has posted yet cost nothing.
- The daily post only runs when `LINEMAKER_POST_TIME` is set.

## Bet buttons (betslip links)

Every pick gets **Bet on FanDuel** / **Bet on DraftKings** buttons, and every volume-mode
parlay gets a button that opens that book with all of its legs loaded. Links come from The
Odds API (`includeLinks`, `includeSids`); parlay links are built by combining the legs' ids.
`LINEMAKER_STATE` fills in links that need a state; `LINEMAKER_BET_LINKS=off` turns it off.
Always check the slip in the app before placing it.

## Game-line model (moneylines, spreads, totals)

Ask "best NBA moneylines tonight" (or spreads / totals / "who wins"), or use
`/picks bet:Moneylines, spreads, totals`. Each team's points scored and allowed per game come
from ESPN standings, blended with last season early on (regressed a third toward average).
Each side's expected score is its offense plus the other side's defense relative to league
average; home edge and back-to-back penalties (NBA, WNBA, NHL) are added, then Kalshi's win
probability is blended in at `LINEMAKER_KALSHI_WEIGHT` (default 0.4). The engine blends the
result with the FanDuel/DraftKings no-vig price as usual. One /odds call per league (~3
credits). Not modelled: injuries, starting pitchers and goalies; treat MLB and NHL
sides with extra caution.

## Live odds (The Odds API)

Set `ODDS_API_KEY` (never commit it). The feed supplies prices; you supply projections.

1. `/odds sport:NBA` (or `python -m linemaker fetch --sport NBA -o odds.csv`) posts the
   no-vig board and a CSV of every priced market with blank projection columns.
2. Fill `mean` (or the projection inputs) on the rows you have a number for. Any CSV with
   `sport, subject, market` + a projection works; `line` is optional and pins a row to one line.
   Game lines use the **home team** as `subject`: `moneyline`, `spread` (mean = home margin),
   `game total` / `total goals`, `match result` (soccer: `mean` home goals, `mean_b` away).
   Player names must match the book's (accents, periods and Jr./III are ignored).
3. `/live sport:NBA projections:<csv>` (or `python -m linemaker live --sport NBA
   --projections proj.csv`) pulls fresh prices, joins, and posts the pro card. Projections
   with no odds are listed under Passes.

Credits (v4 guide): `/odds` and per-game prop calls cost markets × regions, counting only
markets that return data; empty responses are free; up to 10 `bookmakers` = 1 region.
Game lines cost 3 per sport. Props cost up to 5 per game for NBA defaults. `/live` only
buys what your projections need: game lines only if you projected one, props only if you
projected one; with props only, the game list comes from the free `/events` endpoint.
`--max-events` caps prop calls. Rate-limit responses (429) are retried with backoff.

Grading: `/grade` (or `python -m linemaker grade snapshots/<date>.json --auto`) scores
moneylines, spreads, totals and 1X2 from `/scores` (2 credits per sport, last 3 days).
Props still need a results CSV, which can be combined with `--auto`.
`/quota` (free) shows what's left.

Books: bets are only recommended at **FanDuel and DraftKings** (`LINEMAKER_BET_BOOKS`,
`all` to allow any book). The no-vig consensus still averages every US book the feed
returns, which costs the same credits and gives a sharper market read. To build the
consensus from FanDuel and DraftKings only, set `ODDS_API_BOOKMAKERS=fanduel,draftkings`.

Daily auto-post from live odds: set `LINEMAKER_CHANNEL_ID`, `LINEMAKER_SPORTS=NBA,NHL`,
`LINEMAKER_PROJECTIONS_DIR` (holding `<YYYY-MM-DD>.csv`) and `LINEMAKER_POST_TIME`.

## Engine (also usable from the command line)

**lite**: hit-rate probability gap (season-weighted blend, conservatism haircuts,
70% floor, odds window). Config in `linemaker/config.py`.

    python -m linemaker lite data/sample_slate.csv --date 2026-10-21 -o card.md

**pro**: simulation-first engine (`linemaker/pro.py`, `sim.py`, `devig.py`):

1. **Projection** (`projection.py`), unless you pass `mean` directly:
   `opportunity × efficiency × matchup × pace × venue × rest`. Opportunity leans on recent
   games (role changes show there first); efficiency leans on the season and regresses toward
   `rate_expected` when given (xG, expected yards/target, whiff-based K%). Environment factors
   are multipliers vs league average, shrunk 40% toward 1.00 and clamped to ±20%.
2. Monte Carlo distribution of the stat (Poisson for goals/SOG/TDs, negative binomial for
   points/rebounds/Ks, gamma for yardage, normal for margins/totals). Median is shown next to
   the mean so right skew is visible.
3. Market de-vigged per book and averaged into a consensus; best price shopped across books.
   `--devig proportional` (default) or `--devig shin` (corrects favorite-longshot bias).
   Odds can be American (`-110`, `+150`) or decimal (`2.10`).
4. Blend model with no-vig consensus: 50/50 by default, 80/20 when `news_fresh=true`.
5. Bet only when **both** hold: model probability > no-vig market ("market caught up"
   otherwise), and blended probability > the best price's implied probability (the gap).
   EV is reported per $100: `P(win) × net profit − P(loss) × stake`.
6. Soft markets from the write-up (QB rushing, WR/TE receiving, longest reception, anytime TD)
   are flagged on the card.
7. Projections freeze to `snapshots/<date>.json` the first time a market is priced with
   `lineup_confirmed=true` and are never overwritten.

    python -m linemaker pro data/sample_pro_slate.csv --date 2026-10-21 -o pro_card.md
    python -m linemaker pro ... --odds-min -200 --odds-max -1000 --min-prob 0.70   # optional filters
    python -m linemaker grade snapshots/2026-10-21.json data/sample_results.csv  # P/L, ROI, CLV, Brier

### Pro CSV columns

`date, sport, game_id, game, lineup_confirmed, bet_type (prop|total|spread|moneyline), subject,
opponent, position, market, line, news_fresh, injury_status, notes`, plus:

- Projection: either `mean`, or `season_games, opp_season, opp_recent, opp_recent_games,
  opp_override, rate_season, rate_recent, rate_recent_games, rate_expected, matchup_factor,
  pace_factor, venue_factor, rest_factor`. Optional `stdev`, `dist`.
- Soccer 1X2: `bet_type=1x2`, `mean` = subject's projected goals, `mean_b` = opponent's;
  books list three prices, subject / draw / opponent (`Bet365:2.10/3.40/3.60`). Results
  `actual` = goal difference; closing prices in `close_a_odds, close_d_odds, close_b_odds`.
- Prices: either `books` (`DraftKings:-115/-105|FanDuel:-110/-112|Caesars:+130/`), or
  `mkt_a_odds, mkt_b_odds` (consensus) with `best_a_odds, best_a_book, best_b_odds, best_b_book`.

Side A = Over / subject team; side B = Under / opponent. For spreads, `mean` is the subject
team's projected margin and `line` its spread (e.g. -3.5).

## Lite CSV columns

`date, sport, game_id, game, start_confirmed, bet_type, subject, team, market, side, line, odds,
book, season_hits, season_games, split_hits, split_games, l5, l10, l15, l20, projection,
matchup_adj, matchup_note, is_combo, injury_status, alt_lines ("24.5:-450|22.5:-800"), notes`

Blank fields are treated as "data unavailable" and never filled in.
