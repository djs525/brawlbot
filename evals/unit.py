"""Unit suite: deterministic checks over the pure data layer. No LLM, no
network, no API keys — always free to run, and a failure here is always a real
bug in the data code (the layer the model's grounding depends on).

Each test is a small function; `run_unit()` collects results in the same
shape the agent runner uses.
"""

import json

import bs_client
import store
from plugins import jsonout

from .checks import CheckResult


def _case(name):
    """Decorator: register a test function."""
    def wrap(fn):
        _TESTS.append((name, fn))
        return fn
    return wrap


_TESTS: list = []


# ------------------------------------------------------------- outcome()

@_case("outcome: team-mode results")
def _(assert_):
    assert_(bs_client.outcome("gemGrab", "victory", None) == "win", "victory->win")
    assert_(bs_client.outcome("gemGrab", "defeat", None) == "loss", "defeat->loss")
    assert_(bs_client.outcome("gemGrab", "draw", None) is None, "draw->None")
    assert_(bs_client.outcome(None, None, None) is None, "unknown->None")


@_case("outcome: showdown placement cutoffs")
def _(assert_):
    assert_(bs_client.outcome("soloShowdown", None, 4) == "win", "solo rank4=win")
    assert_(bs_client.outcome("soloShowdown", None, 5) == "loss", "solo rank5=loss")
    assert_(bs_client.outcome("duoShowdown", None, 2) == "win", "duo rank2=win")
    assert_(bs_client.outcome("duoShowdown", None, 3) == "loss", "duo rank3=loss")
    # result (if present) outranks rank
    assert_(bs_client.outcome("soloShowdown", "victory", 9) == "win", "result wins")


# ------------------------------------------------------------ is_ranked()

@_case("is_ranked: type disambiguation")
def _(assert_):
    assert_(bs_client.is_ranked({"type": "soloRanked"}) is True, "soloRanked")
    assert_(bs_client.is_ranked({"type": "SOLORANKED"}) is True, "case-insensitive")
    assert_(bs_client.is_ranked({"type": "ranked"}) is False,
            "trophy ladder 'ranked' is NOT competitive Ranked")
    assert_(bs_client.is_ranked({}) is False, "missing type -> trophy")
    assert_(bs_client.is_ranked({"type": None}) is False, "None type -> trophy")


# ----------------------------------------------------- summarize_battles()

def _mini_battles():
    return [
        {"mode": "gemGrab", "brawler": "PIPER", "result": "victory",
         "rank": None, "trophyChange": 8, "type": "ranked"},
        {"mode": "gemGrab", "brawler": "PIPER", "result": "defeat",
         "rank": None, "trophyChange": -6, "type": "ranked"},
        {"mode": "soloShowdown", "brawler": "LEON", "result": None,
         "rank": 2, "trophyChange": 10, "type": "ranked"},
        {"mode": "gemGrab", "brawler": "BELLE", "result": "victory",
         "rank": None, "trophyChange": None, "type": "soloRanked"},
        {"mode": "gemGrab", "brawler": "BELLE", "result": "defeat",
         "rank": None, "trophyChange": None, "type": "soloRanked"},
    ]


@_case("summarize_battles: overall tallies")
def _(assert_):
    s = bs_client.summarize_battles(_mini_battles())
    o = s["overall"]
    assert_(o["battles"] == 5, f"battles {o['battles']}")
    assert_(o["wins"] == 3 and o["losses"] == 2, f"W/L {o['wins']}/{o['losses']}")
    assert_(o["trophyChange"] == 12, f"net {o['trophyChange']}")
    assert_(o["winRate"] == 60.0, f"WR {o['winRate']}")


@_case("summarize_battles: trophy/ranked split")
def _(assert_):
    s = bs_client.summarize_battles(_mini_battles())
    assert_(s["trophy"]["overall"]["battles"] == 3, "3 trophy games")
    assert_(s["ranked"]["overall"]["battles"] == 2, "2 ranked games")
    assert_(s["ranked"]["overall"]["trophyChange"] == 0,
            "Ranked never moves trophies")
    assert_(s["ranked"]["overall"]["winRate"] == 50.0, "ranked WR 50.0")
    assert_(s["trophy"]["overall"]["trophyChange"] == 12, "trophy net +12")


@_case("summarize_battles: per-mode and per-brawler buckets")
def _(assert_):
    s = bs_client.summarize_battles(_mini_battles())
    assert_(s["perMode"]["gemGrab"]["battles"] == 4, "gemGrab bucket")
    assert_(s["perMode"]["soloShowdown"]["wins"] == 1, "showdown rank2 = win")
    assert_(s["perBrawler"]["PIPER"]["winRate"] == 50.0, "PIPER WR")
    assert_(s["perBrawler"]["LEON"]["wins"] == 1, "LEON showdown win")


@_case("summarize_battles: empty input")
def _(assert_):
    s = bs_client.summarize_battles([])
    assert_(s["overall"]["battles"] == 0, "no battles")
    assert_(s["overall"]["winRate"] == 0.0, "no div-by-zero")


# ------------------------------------------------- battle player resolution

def _team_battle():
    return {"mode": "gemGrab", "teams": [
        [{"tag": "#P0LQY8", "brawler": {"name": "PIPER"}},
         {"tag": "#2GGRVL", "brawler": {"name": "CROW"}},
         {"tag": "#8QCUPJ", "brawler": {"name": "BELLE"}}],
        [{"tag": "#9YV0CQ", "brawler": {"name": "SHELLY"}}],
    ]}


@_case("player_brawler: finds the right player, tolerates formats")
def _(assert_):
    b = _team_battle()
    assert_(bs_client.player_brawler(b, "#P0LQY8") == "PIPER", "exact tag")
    assert_(bs_client.player_brawler(b, "p0lqy8") == "PIPER", "lowercase, no #")
    assert_(bs_client.player_brawler(b, "#RUV2C9") is None, "absent -> None")


@_case("player_team: sorted comp, None for solo")
def _(assert_):
    b = _team_battle()
    assert_(bs_client.player_team(b, "#P0LQY8") == ["BELLE", "CROW", "PIPER"],
            "sorted canonical comp")
    solo = {"players": [{"tag": "#P0LQY8", "brawler": {"name": "LEON"}}]}
    assert_(bs_client.player_team(solo, "#P0LQY8") is None, "solo -> None")


@_case("player_team_tags: WHO played, sorted")
def _(assert_):
    tags = bs_client.player_team_tags(_team_battle(), "#P0LQY8")
    assert_(tags == ["#2GGRVL", "#8QCUPJ", "#P0LQY8"], f"got {tags}")


# ------------------------------------------------------------ slim_player()

@_case("slim_player: precomputed tallies agree with the roster")
def _(assert_):
    from .fixtures import RAW_PLAYER
    p = bs_client.slim_player(RAW_PLAYER)
    assert_(p["power11Count"] == 3, f"p11 {p['power11Count']}")
    assert_(p["power10Count"] == 2, f"p10 {p['power10Count']}")
    assert_(p["brawlerCount"] == len(p["brawlers"]), "count == list length")
    trophies = [b["trophies"] for b in p["brawlers"]]
    assert_(trophies == sorted(trophies, reverse=True), "sorted by trophies desc")
    assert_(p["iconUrl"].startswith("https://cdn.brawlify.com/"), "renderable icon URL")


@_case("slim_battlelog: absent player yields brawler None, never a guess")
def _(assert_):
    from .fixtures import RAW_BATTLELOG
    battles = bs_client.slim_battlelog(RAW_BATTLELOG, "#P0LQY8")
    heist = [b for b in battles if b["mode"] == "heist"]
    assert_(len(heist) == 1 and heist[0]["brawler"] is None,
            "heist battle resolves to None")
    named = [b for b in battles if b["brawler"]]
    assert_(all(b["brawler"] in {"PIPER", "MORTIS", "LEON", "BELLE", "SPIKE"}
                for b in named), "only our own brawlers resolved")


# --------------------------------------------------------------- jsonout

@_case("jsonout.dump: small payloads pass through untouched")
def _(assert_):
    payload = {"a": 1, "b": [1, 2, 3]}
    assert_(json.loads(jsonout.dump(payload)) == payload, "round-trips")


@_case("jsonout.dump: oversized list -> valid JSON subset + truncation note")
def _(assert_):
    big = [{"i": i, "pad": "x" * 100} for i in range(500)]
    out = jsonout.dump(big, limit=5000)
    assert_(len(out) <= 5000, f"respects limit ({len(out)})")
    parsed = json.loads(out)  # must not raise — the whole point of jsonout
    assert_("truncated" in parsed, "carries a truncation note")
    assert_(0 < len(parsed["items"]) < 500, "kept a whole-item prefix")
    assert_(parsed["items"][0] == big[0], "items intact, not sliced mid-object")


@_case("jsonout.dump: oversized dict -> explicit error, still valid JSON")
def _(assert_):
    big = {"blob": "x" * 20000}
    parsed = json.loads(jsonout.dump(big, limit=5000))
    assert_("error" in parsed, "explicit error object")


# ------------------------------------------------------------- store tags

@_case("normalize_tag: canonicalization and rejection")
def _(assert_):
    assert_(store.normalize_tag(" p0lqy8 ") == "#P0LQY8", "trim/upper/#")
    assert_(store.normalize_tag("#P0LQY8") == "#P0LQY8", "idempotent")
    for bad in ("AB", "X" * 16, "P0LQY!"):
        try:
            store.normalize_tag(bad)
            assert_(False, f"{bad!r} should raise")
        except ValueError:
            assert_(True, f"{bad!r} rejected")
    try:
        store.normalize_tag("POLQY8")  # 'O' not in Supercell's alphabet
        assert_(False, "'O' should raise")
    except ValueError as e:
        assert_("O" in str(e) and "0" in str(e), "hints O->0 confusion")


# ------------------------------------------- agent answer validation layer

@_case("agent._build_answer: validation and caps")
def _(assert_):
    import os
    os.environ.setdefault("GEMINI_API_KEY", "unit-test-dummy")
    from agent import _build_answer

    ans = _build_answer({
        "summary": "hello",
        "title": "T" * 300,
        "fields": ([{"name": "n", "value": "v"}] * 30
                   + [{"name": "", "value": "dropme"}, "notadict"]),
        "image_url": "not-a-url.png",
        "thumbnail_url": "https://cdn.brawlify.com/x.png",
        "color": "#f5c518",
        "tabs": [{"label": "By Mode", "summary": "s"},
                 {"label": "", "summary": "no label -> skipped"},
                 {"summary": "no label either"},
                 {"label": "B", "summary": "b"},
                 {"label": "C", "summary": "c"},
                 {"label": "D", "summary": "over the cap"}],
    })
    assert_(len(ans.fields) == 25, f"fields capped at 25 ({len(ans.fields)})")
    assert_(len(ans.title) == 256, "title capped at 256")
    assert_(ans.image_url is None, "non-http image URL dropped")
    assert_(ans.thumbnail_url is not None, "real URL kept")
    assert_(len(ans.tabs) <= 3, f"tabs capped at 3 ({len(ans.tabs)})")
    assert_(all(t.label for t in ans.tabs), "label-less tabs skipped")

    empty = _build_answer({"summary": "   "})
    assert_("couldn't produce" in empty.summary, "blank summary -> fallback text")


# ---------------------------------------------------------------- runner

def run_unit() -> list[dict]:
    """Run every unit test; return [{name, ok, failures}]."""
    results = []
    for name, fn in _TESTS:
        failures: list[str] = []

        def assert_(cond, msg=""):
            if not cond:
                failures.append(msg or "assertion failed")

        try:
            fn(assert_)
        except Exception as e:
            failures.append(f"raised {type(e).__name__}: {e}")
        results.append({"name": name, "ok": not failures,
                        "checks": [CheckResult(name, not failures,
                                               "; ".join(failures)).__dict__]})
    return results
