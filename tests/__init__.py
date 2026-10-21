import os

# Sample slates use many books; tests opt out of the FanDuel/DraftKings default.
os.environ.setdefault("LINEMAKER_BET_BOOKS", "all")
os.environ.setdefault("LINEMAKER_TRACKING", "off")   # tests that need it turn it on
os.environ.setdefault("LINEMAKER_HIT_RATES", "off")   # no live ESPN calls in tests
os.environ.setdefault("LINEMAKER_PROJECTIONS", "fantasypros")   # ESPN fallback tested on its own
os.environ.setdefault("LINEMAKER_SKIP_STARTED", "off")   # fixture games have fixed start times
os.environ.setdefault("LINEMAKER_PRETIP_MINUTES", "")   # no pre-tip arming unless a test opts in
