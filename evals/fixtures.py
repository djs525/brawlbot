"""Frozen fixture data + the fake tool router for agent evals.

Fixtures are built by running fake RAW API payloads through the REAL production
slimmers (bs_client.slim_player / slim_battlelog / summarize_battles and
jsonout.dump), so the shapes the model sees in an eval can never drift from
what production serves. Honesty notes are imported from plugins.events for the
same reason.

The router replaces plugins.execute for the duration of one eval case. It
records every tool call (name, args, served payload) so checks can assert on
the trace — which tools ran, with what arguments, and whether every URL in the
final answer actually appeared in a served payload.
"""

import json
from datetime import datetime, timedelta, timezone

import bs_client
from plugins import jsonout
from plugins.events import HAS_UPCOMING_NOTE, NOW_ONLY_NOTE
from plugins.official_bs import PLAYER_LIMIT

# ---------------------------------------------------------------- identities

ASKER_TAG = "#P0LQY8"    # linked eval player (valid Supercell tag alphabet)
FRIEND_TAG = "#RUV2C9"   # second linked player, for compare questions

_TIME_FMT = "%Y%m%dT%H%M%S.000Z"


def _t(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)).strftime(_TIME_FMT)


# ---------------------------------------------------------------- raw player

def _brawler(name, power, trophies, highest, gadgets, star_powers, gears=()):
    return {"name": name, "power": power, "trophies": trophies,
            "highestTrophies": highest,
            "gadgets": [{"name": g} for g in gadgets],
            "starPowers": [{"name": s} for s in star_powers],
            "gears": [{"name": g} for g in gears]}


RAW_PLAYER = {
    "name": "Ripley", "tag": ASKER_TAG,
    "icon": {"id": 28000042},
    "trophies": 41237, "highestTrophies": 42876, "expLevel": 210,
    "3vs3Victories": 9214, "soloVictories": 812, "duoVictories": 655,
    "club": {"name": "Eval Club"},
    "brawlers": [
        _brawler("PIPER", 11, 1180, 1245, ["Auto Aimer"], ["Ambush"], ["Shield Gear"]),
        _brawler("LEON", 11, 1104, 1150, ["Clone Projector"], ["Smoke Trails"]),
        _brawler("SPIKE", 11, 986, 1010, ["Popping Pincushion"], ["Fertilize"]),
        _brawler("MORTIS", 10, 903, 940, ["Combo Spinner"], ["Coiled Snake"]),
        _brawler("BELLE", 10, 871, 905, ["Nest Egg"], ["Positive Feedback"]),
        _brawler("CROW", 9, 764, 800, ["Defense Booster"], ["Extra Toxic"]),
        _brawler("SHELLY", 8, 640, 702, ["Fast Forward"], ["Shell Shock"]),
        _brawler("NITA", 7, 518, 560, ["Bear Paws"], ["Bear With Me"]),
    ],
}
# -> power11Count=3 (PIPER, LEON, SPIKE), power10Count=2, brawlerCount=8

RAW_FRIEND = {
    "name": "Nova", "tag": FRIEND_TAG,
    "icon": {"id": 28000007},
    "trophies": 38854, "highestTrophies": 39102, "expLevel": 188,
    "3vs3Victories": 7810, "soloVictories": 601, "duoVictories": 512,
    "club": {"name": "Eval Club"},
    "brawlers": [
        _brawler("MORTIS", 11, 1051, 1090, ["Combo Spinner"], ["Coiled Snake"]),
        _brawler("CROW", 10, 899, 930, ["Defense Booster"], ["Extra Toxic"]),
        _brawler("SHELLY", 9, 701, 740, ["Fast Forward"], ["Shell Shock"]),
        _brawler("PIPER", 9, 688, 700, ["Auto Aimer"], ["Ambush"]),
        _brawler("NITA", 8, 555, 580, ["Bear Paws"], ["Bear With Me"]),
    ],
}

# The set of brawler names an answer about the asker is ALLOWED to mention.
ROSTER = {b["name"] for b in RAW_PLAYER["brawlers"]}
FRIEND_ROSTER = {b["name"] for b in RAW_FRIEND["brawlers"]}


# -------------------------------------------------------------- raw battlelog

def _team_battle(mode, map_, type_, our_brawler, result, trophy_change,
                 minutes_ago, our_tag=ASKER_TAG, star=None):
    """One 3v3 battle where OUR player is on the first team."""
    us = {"tag": our_tag, "name": "Ripley",
          "brawler": {"name": our_brawler, "trophies": 1000}}
    mates = [{"tag": "#2GGRVL", "name": "Ally1", "brawler": {"name": "CROW"}},
             {"tag": "#8QCUPJ", "name": "Ally2", "brawler": {"name": "BELLE"}}]
    foes = [{"tag": f"#9YV{i}CQ", "name": f"Foe{i}",
             "brawler": {"name": "SHELLY"}} for i in range(3)]
    battle = {"mode": mode, "type": type_, "result": result,
              "teams": [[us, *mates], foes]}
    if trophy_change is not None:
        battle["trophyChange"] = trophy_change
    if star:
        battle["starPlayer"] = {"tag": our_tag, "name": "Ripley"}
    return {"battleTime": _t(minutes_ago),
            "event": {"mode": mode, "map": map_},
            "battle": battle}


def _showdown_battle(map_, rank, trophy_change, minutes_ago, brawler="LEON"):
    players = [{"tag": ASKER_TAG, "name": "Ripley",
                "brawler": {"name": brawler, "trophies": 1100}}]
    players += [{"tag": f"#PP{i}YLQ", "name": f"Rando{i}",
                 "brawler": {"name": "NITA"}} for i in range(9)]
    return {"battleTime": _t(minutes_ago),
            "event": {"mode": "soloShowdown", "map": map_},
            "battle": {"mode": "soloShowdown", "type": "ranked",
                       "rank": rank, "trophyChange": trophy_change,
                       "players": players}}


def _absent_battle(minutes_ago):
    """A Heist battle where OUR tag never appears -> brawler resolves to None.
    Exercises the 'never infer a brawler' rule."""
    team = [{"tag": f"#C{i}UVGR", "name": f"Other{i}",
             "brawler": {"name": "COLT"}} for i in range(3)]
    foes = [{"tag": f"#J{i}RLQG", "name": f"Foe{i}",
             "brawler": {"name": "BULL"}} for i in range(3)]
    return {"battleTime": _t(minutes_ago),
            "event": {"mode": "heist", "map": "Safe Zone"},
            "battle": {"mode": "heist", "type": "ranked", "result": "victory",
                       "trophyChange": 8, "teams": [team, foes]}}


RAW_BATTLELOG = {"items": [
    # trophy ladder: gem grab on Hard Rock Mine, PIPER (2W 1L, +10)
    _team_battle("gemGrab", "Hard Rock Mine", "ranked", "PIPER", "victory", 8, 30, star=True),
    _team_battle("gemGrab", "Hard Rock Mine", "ranked", "PIPER", "defeat", -6, 55),
    _team_battle("gemGrab", "Hard Rock Mine", "ranked", "PIPER", "victory", 8, 80),
    # trophy ladder: brawl ball on Backyard Bowl, MORTIS (2W 1L, +11)
    _team_battle("brawlBall", "Backyard Bowl", "ranked", "MORTIS", "victory", 9, 110),
    _team_battle("brawlBall", "Backyard Bowl", "ranked", "MORTIS", "defeat", -7, 135),
    _team_battle("brawlBall", "Backyard Bowl", "ranked", "MORTIS", "victory", 9, 160),
    # solo showdown, LEON: rank 2 (win, +10), rank 8 (loss, -4)
    _showdown_battle("Feast or Famine", 2, 10, 190),
    _showdown_battle("Feast or Famine", 8, -4, 215),
    # competitive Ranked (type=soloRanked, no trophyChange): 3W 1L -> 75.0%
    _team_battle("gemGrab", "Hard Rock Mine", "soloRanked", "BELLE", "victory", None, 245),
    _team_battle("gemGrab", "Hard Rock Mine", "soloRanked", "BELLE", "defeat", None, 270),
    _team_battle("brawlBall", "Backyard Bowl", "soloRanked", "SPIKE", "victory", None, 300),
    _team_battle("brawlBall", "Backyard Bowl", "soloRanked", "SPIKE", "victory", None, 325),
    # heist game our player is absent from -> brawler None
    _absent_battle(350),
]}
# Trophy split: 9 battles, 6W 3L, net +35.  Ranked split: 4 battles, 3W 1L, 75.0%.

RAW_FRIEND_BATTLELOG = {"items": [
    _team_battle("brawlBall", "Backyard Bowl", "ranked", "MORTIS", "victory", 9, 40,
                 our_tag=FRIEND_TAG),
    _team_battle("brawlBall", "Backyard Bowl", "ranked", "MORTIS", "defeat", -7, 70,
                 our_tag=FRIEND_TAG),
    _team_battle("gemGrab", "Hard Rock Mine", "ranked", "CROW", "victory", 8, 100,
                 our_tag=FRIEND_TAG),
]}


# ----------------------------------------------------- long-window history

def _hist(mode, brawler, result=None, rank=None, trophy=0, type_="ranked"):
    return {"mode": mode, "brawler": brawler, "result": result,
            "rank": rank, "trophyChange": trophy, "type": type_}


HISTORY_BATTLES = (
    # LEON solo showdown: 20 games, 14 wins (rank<=4)
    [_hist("soloShowdown", "LEON", rank=2, trophy=8) for _ in range(14)]
    + [_hist("soloShowdown", "LEON", rank=7, trophy=-4) for _ in range(6)]
    # PIPER gem grab: 20 games, 8 wins
    + [_hist("gemGrab", "PIPER", result="victory", trophy=8) for _ in range(8)]
    + [_hist("gemGrab", "PIPER", result="defeat", trophy=-6) for _ in range(12)]
    # MORTIS brawl ball: 10 games, 6 wins
    + [_hist("brawlBall", "MORTIS", result="victory", trophy=9) for _ in range(6)]
    + [_hist("brawlBall", "MORTIS", result="defeat", trophy=-7) for _ in range(4)]
    # BELLE competitive Ranked: 10 games, 6 wins, no trophies
    + [_hist("gemGrab", "BELLE", result="victory", type_="soloRanked") for _ in range(6)]
    + [_hist("gemGrab", "BELLE", result="defeat", type_="soloRanked") for _ in range(4)]
)
# 60 battles, 34W 26L -> overall winRate 56.7

HISTORY_ROSTER = {"LEON", "PIPER", "MORTIS", "BELLE"}


# ---------------------------------------------------------------- events

ROTATION_MAPS = ["Hard Rock Mine", "Backyard Bowl", "Feast or Famine"]
UPCOMING_MAP = "Sneaky Fields"

_ROTATION = [
    {"mode": "gemGrab", "map": "Hard Rock Mine",
     "startTime": _t(12 * 60), "endTime": _t(-12 * 60)},
    {"mode": "brawlBall", "map": "Backyard Bowl",
     "startTime": _t(12 * 60), "endTime": _t(-12 * 60)},
    {"mode": "soloShowdown", "map": "Feast or Famine",
     "startTime": _t(12 * 60), "endTime": _t(-12 * 60)},
]

_UPCOMING = [
    {"mode": "Brawl Ball", "map": UPCOMING_MAP,
     "startTime": _t(-12 * 60), "endTime": _t(-36 * 60),
     "imageUrl": "https://cdn.brawlify.com/maps/regular/15000126.png"},
]


def _events_payload(with_upcoming: bool) -> str:
    now = datetime.now(timezone.utc).strftime(_TIME_FMT)
    payload = {"source": "official", "now": now,
               "note": HAS_UPCOMING_NOTE if with_upcoming else NOW_ONLY_NOTE,
               "rotation": _ROTATION}
    if with_upcoming:
        payload["upcoming"] = _UPCOMING
    return jsonout.dump(payload)


# --------------------------------------------------- catalogs / lookups

CATALOG = [
    {"id": 16000000, "name": "SHELLY", "starPowers": ["Shell Shock", "Band-Aid"],
     "gadgets": ["Fast Forward", "Clay Pigeons"]},
    {"id": 16000008, "name": "NITA", "starPowers": ["Bear With Me", "Hyper Bear"],
     "gadgets": ["Bear Paws", "Faux Fur"]},
    {"id": 16000010, "name": "PIPER", "starPowers": ["Ambush", "Snappy Sniping"],
     "gadgets": ["Auto Aimer", "Homemade Recipe"]},
    {"id": 16000011, "name": "MORTIS", "starPowers": ["Creepy Harvest", "Coiled Snake"],
     "gadgets": ["Combo Spinner", "Survival Shovel"]},
    {"id": 16000023, "name": "LEON", "starPowers": ["Smoke Trails", "Invisiheal"],
     "gadgets": ["Clone Projector", "Lollipop Drop"]},
    {"id": 16000025, "name": "SPIKE", "starPowers": ["Fertilize", "Curveball"],
     "gadgets": ["Popping Pincushion", "Life Plant"]},
    {"id": 16000032, "name": "CROW", "starPowers": ["Extra Toxic", "Carrion Crow"],
     "gadgets": ["Defense Booster", "Slowing Toxin"]},
    {"id": 16000041, "name": "BELLE", "starPowers": ["Positive Feedback", "Grounded"],
     "gadgets": ["Nest Egg", "Reverse Polarity"]},
]

PIPER_ID = 16000010

RANKINGS = [{"rank": i + 1, "name": n, "tag": f"#T0P{i}LQ", "trophies": 1450 - i * 12}
            for i, n in enumerate(
                ["Vortexa", "Blizzard", "Kestrel", "Falcon", "Nimbus",
                 "Onyx", "Zephyr", "Talon", "Ember", "Frost"])]

MAP_DETAILS = {
    "hard rock mine": {
        "id": 15000007, "name": "Hard Rock Mine", "gameMode": "Gem Grab",
        "environment": "Mine",
        "imageUrl": "https://cdn.brawlify.com/maps/regular/15000007.png",
    },
}

TEAM_COMPS = {
    "map": "Hard Rock Mine",
    "comps": [
        {"brawlers": ["MORTIS", "PIPER", "SPIKE"], "map": "Hard Rock Mine",
         "games": 10, "wins": 7, "losses": 3, "winRate": 70.0},
        {"brawlers": ["BELLE", "CROW", "PIPER"], "map": "Hard Rock Mine",
         "games": 6, "wins": 3, "losses": 3, "winRate": 50.0},
    ],
}


# ------------------------------------------------------- expected numbers
# Computed through the REAL production tally code, never by hand, so a change
# to summarize_battles updates the expectations automatically.

def expected_battlelog_summary() -> dict:
    battles = bs_client.slim_battlelog(RAW_BATTLELOG, ASKER_TAG)
    return bs_client.summarize_battles(battles)


def expected_history_summary() -> dict:
    return bs_client.summarize_battles(HISTORY_BATTLES)


# ---------------------------------------------------------------- router

def _norm(tag: str) -> str:
    tag = (tag or "").strip().upper()
    return tag if tag.startswith("#") else "#" + tag


class FixtureRouter:
    """Drop-in replacement for plugins.execute that serves frozen fixtures and
    records the full call trace. Build one per case; `events_upcoming` flips
    the rotation payload between the two honesty variants."""

    def __init__(self, events_upcoming: bool = False):
        self.events_upcoming = events_upcoming
        self.trace: list[dict] = []  # {"tool", "args", "payload"}

    # -- payload builders (run REAL slimmers, mirror each plugin's execute) --

    def _player(self, args: dict) -> str:
        tag = _norm(args.get("tag", ""))
        raw = {ASKER_TAG: RAW_PLAYER, FRIEND_TAG: RAW_FRIEND}.get(tag)
        if raw is None:
            return ("ERROR while running 'get_player': "
                    "BrawlStarsError: 404: notFound")
        return jsonout.dump(bs_client.slim_player(raw), limit=PLAYER_LIMIT)

    def _battlelog(self, args: dict) -> str:
        tag = _norm(args.get("tag", ""))
        raw = {ASKER_TAG: RAW_BATTLELOG,
               FRIEND_TAG: RAW_FRIEND_BATTLELOG}.get(tag)
        if raw is None:
            return ("ERROR while running 'get_battlelog': "
                    "BrawlStarsError: 404: notFound")
        battles = bs_client.slim_battlelog(raw, tag)
        return jsonout.dump({"summary": bs_client.summarize_battles(battles),
                             "battles": battles})

    def _history(self, args: dict) -> str:
        tag = _norm(args.get("tag", ""))
        if tag not in (ASKER_TAG, FRIEND_TAG):
            return json.dumps({"tag": tag, "days": args.get("days") or 30,
                               "battles": 0, "note": "No stored history."})
        battles = HISTORY_BATTLES if tag == ASKER_TAG else HISTORY_BATTLES[:10]
        summary = bs_client.summarize_battles(battles)
        return json.dumps({"tag": tag, "days": args.get("days") or 30,
                           "battles": summary["overall"]["battles"], **summary})

    def _rankings(self, args: dict) -> str:
        return jsonout.dump(RANKINGS)

    def _brawlers_catalog(self, args: dict) -> str:
        return jsonout.dump(CATALOG, limit=25000)

    def _map_details(self, args: dict) -> str:
        key = (args.get("map_name") or "").strip().lower()
        detail = MAP_DETAILS.get(key)
        if detail is None:
            return json.dumps({"error": f"No map found matching {key!r}."})
        return jsonout.dump(detail)

    def _brawler_info(self, args: dict) -> str:
        key = (args.get("brawler_name") or "").strip().upper()
        hit = next((b for b in CATALOG if b["name"] == key), None)
        if hit is None:
            return json.dumps({"error": f"No brawler matching {key!r}."})
        return jsonout.dump({**hit, "archetype": "Marksman", "rarity": "Epic"})

    def _events(self, args: dict) -> str:
        return _events_payload(self.events_upcoming)

    def _team_comps(self, args: dict) -> str:
        return json.dumps(TEAM_COMPS)

    # ------------------------------------------------------------- routing

    async def route(self, name: str, tool_input: dict) -> str:
        handler = {
            "get_player": self._player,
            "get_battlelog": self._battlelog,
            "query_history": self._history,
            "get_event_rotation": self._events,
            "get_rankings": self._rankings,
            "get_brawlers": self._brawlers_catalog,
            "get_map_details": self._map_details,
            "get_brawler_info": self._brawler_info,
            "team_comps": self._team_comps,
        }.get(name)
        if handler is None:
            payload = f"ERROR: no plugin provides a tool named '{name}'"
        else:
            try:
                payload = handler(dict(tool_input or {}))
            except Exception as e:  # a fixture bug must read as such, loudly
                payload = (f"ERROR while running '{name}' (FIXTURE BUG): "
                           f"{type(e).__name__}: {e}")
        self.trace.append({"tool": name, "args": dict(tool_input or {}),
                           "payload": payload})
        return payload

    def served_text(self) -> str:
        """Everything the model was shown, concatenated — ground truth for
        'was this URL/number actually in a tool result?' checks."""
        return "\n".join(t["payload"] for t in self.trace)
