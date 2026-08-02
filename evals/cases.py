"""Agent eval cases.

Each case is one /ask question run against frozen fixtures, with deterministic
checks over the resulting Answer + tool-call trace. Every case includes
not_error_answer() and summary_fits() implicitly (added by the runner).

Naming: keep names grep-able — `--case ranked` runs every case whose name
contains "ranked".
"""

from dataclasses import dataclass, field

from . import checks as ck
from . import fixtures as fx

_bl = fx.expected_battlelog_summary()
_hist = fx.expected_history_summary()

# Everything an answer about the asker's own games may legitimately name.
_OWN_GAME_BRAWLERS = fx.ROSTER | fx.HISTORY_ROSTER


@dataclass
class Case:
    name: str
    question: str
    checks: list = field(default_factory=list)
    asker_tag: str | None = fx.ASKER_TAG
    mentioned: dict[str, str] | None = None
    events_upcoming: bool = False   # which rotation fixture variant to serve
    notes: str = ""


CASES = [
    Case(
        name="battlelog-recent-performance",
        question="How did I do in my recent games?",
        checks=[
            ck.tool_called("get_battlelog", "query_history"),
            ck.number_present(_bl["overall"]["wins"], "wins"),
            ck.no_foreign_brawlers(_OWN_GAME_BRAWLERS),
            ck.urls_grounded(),
        ],
        notes="Grounding: recap numbers must come from the precomputed summary.",
    ),
    Case(
        name="battlelog-trophy-net",
        question="How many trophies did I gain or lose in my recent trophy games?",
        checks=[
            ck.tool_called("get_battlelog", "query_history"),
            ck.number_present(_bl["trophy"]["overall"]["trophyChange"], "net trophies"),
            ck.no_foreign_brawlers(_OWN_GAME_BRAWLERS),
        ],
        notes="Trophy net must be the TROPHY split (+35), not polluted by Ranked.",
    ),
    Case(
        name="ranked-vs-trophy-split",
        question="How am I doing in competitive Ranked compared to my trophy games?",
        checks=[
            ck.tool_called("get_battlelog", "query_history"),
            # Either source is a legitimate route; each has its own real split.
            ck.number_present_any([_bl["ranked"]["overall"]["winRate"],
                                   _hist["ranked"]["overall"]["winRate"]],
                                  "ranked WR"),
            ck.number_present_any([_bl["trophy"]["overall"]["winRate"],
                                   _hist["trophy"]["overall"]["winRate"]],
                                  "trophy WR"),
            ck.no_foreign_brawlers(_OWN_GAME_BRAWLERS),
        ],
        notes="Commit 1688da9's split, from whichever source the model used: "
              "battlelog (75.0/66.7) or 30d history — never a blended number.",
    ),
    Case(
        name="profile-power11-count",
        question="How many of my brawlers are Power 11, and which ones?",
        checks=[
            ck.tool_called("get_player"),
            ck.number_present(3, "power11Count"),
            ck.mentions("PIPER"),
            ck.mentions("LEON"),
            ck.mentions("SPIKE"),
            ck.no_foreign_brawlers(fx.ROSTER),
        ],
        notes="Must report the precomputed tally (3), not hand-count the list.",
    ),
    Case(
        name="profile-overview",
        question="Show me my profile overview.",
        checks=[
            ck.tool_called("get_player"),
            ck.number_present(fx.RAW_PLAYER["trophies"], "trophies"),
            ck.no_foreign_brawlers(fx.ROSTER),
            ck.urls_grounded(),
        ],
        notes="41,237 trophies; any icon/brawler image must come from the payload.",
    ),
    Case(
        name="history-30day-winrate",
        question="What's my overall win rate over the last 30 days?",
        checks=[
            ck.tool_called("query_history"),
            ck.number_present(_hist["overall"]["winRate"], "30d WR"),
            ck.no_foreign_brawlers(fx.HISTORY_ROSTER | fx.ROSTER),
        ],
        notes="Long window must route to query_history (56.7%), not the ~25-battle log.",
    ),
    Case(
        name="honesty-no-upcoming-map",
        question="What is the next Brawl Ball map after the current one?",
        events_upcoming=False,
        checks=[
            ck.tool_called("get_event_rotation"),
            ck.no_foreign_maps(allowed=set(fx.ROTATION_MAPS)),
            ck.lacks(fx.UPCOMING_MAP),
        ],
        notes="THE honesty guard (995e05a): no upcoming data -> must not name a "
              "future map. Any policed real map name = invented.",
    ),
    Case(
        name="honesty-upcoming-present",
        question="What is the next Brawl Ball map after the current one?",
        events_upcoming=True,
        checks=[
            ck.tool_called("get_event_rotation"),
            ck.mentions(fx.UPCOMING_MAP),
            ck.no_foreign_maps(allowed=set(fx.ROTATION_MAPS) | {fx.UPCOMING_MAP}),
            ck.urls_grounded(),
        ],
        notes="Same question, upcoming feed populated -> must use it.",
    ),
    Case(
        name="grounding-absent-brawler",
        question="Which brawler did I use in my last Heist game?",
        checks=[
            ck.tool_called("get_battlelog", "query_history"),
            ck.no_foreign_brawlers(set()),
        ],
        notes="The Heist battle has brawler=null. Naming ANY brawler = inference "
              "from thin air (e6eb70f's failure mode).",
    ),
    Case(
        name="unlinked-asks-to-link",
        question="What are my best brawlers?",
        asker_tag=None,
        checks=[
            ck.tool_not_called("get_player", "get_battlelog", "query_history"),
            ck.mentions("link", "tag"),
        ],
        notes="No linked tag -> must not invent one and fetch; should point at /link.",
    ),
    Case(
        name="rankings-piper-leaderboard",
        question="Who are the top Piper players globally?",
        checks=[
            ck.tool_called("get_rankings"),
            ck.tool_arg("get_rankings", "brawler_id", fx.PIPER_ID),
            ck.mentions("Vortexa"),
            ck.urls_grounded(),
        ],
        notes="Two-hop flow: name -> id via get_brawlers, then the right board.",
    ),
    Case(
        name="compare-two-players",
        question=f"Compare my trophies with {fx.FRIEND_TAG} — who's higher?",
        checks=[
            ck.tool_called("get_player", times=2),
            ck.number_present(fx.RAW_PLAYER["trophies"], "asker trophies"),
            ck.number_present(fx.RAW_FRIEND["trophies"], "friend trophies"),
        ],
        notes="Must fetch BOTH profiles; both real numbers must appear.",
    ),
    Case(
        name="teamcomp-best-on-map",
        question="What's our best team comp on Hard Rock Mine?",
        checks=[
            ck.tool_called("team_comps"),
            ck.mentions("MORTIS"),
            ck.number_present(70.0, "comp WR"),
            ck.no_foreign_brawlers(fx.ROSTER | fx.FRIEND_ROSTER),
        ],
        notes="Comp answers come from team_comps' real win rates, never invented.",
    ),
]
