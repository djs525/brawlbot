"""Async wrapper around the official Brawl Stars API (api.brawlstars.com/v1).

Get a token at https://developer.brawlstars.com and put it in .env as
BRAWL_STARS_TOKEN. The token is IP-locked, so it must be created for the
public IP the bot runs from.

The `slim_*` helpers trim the fat API payloads down to what the LLM actually
needs, keeping us inside free-tier token limits.
"""
import os
from urllib.parse import quote

import aiohttp

BASE = "https://api.brawlstars.com/v1"
TOKEN = os.getenv("BRAWL_STARS_TOKEN")


class BrawlStarsError(RuntimeError):
    """Raised when the API returns a non-200 response."""


def _plain_tag(tag: str) -> str:
    """Uppercase, ensure leading #. For comparing tags, not for URLs."""
    tag = (tag or "").strip().upper()
    return tag if tag.startswith("#") else "#" + tag


def _norm_tag(tag: str) -> str:
    """Uppercase, ensure leading #, URL-encode (# -> %23) for the path."""
    return quote(_plain_tag(tag), safe="")


async def _get(path: str) -> dict:
    if not TOKEN:
        raise BrawlStarsError("BRAWL_STARS_TOKEN is not set in the environment.")
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE}{path}", headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                reason = body.get("reason") or body.get("message") or resp.reason
                raise BrawlStarsError(f"{resp.status}: {reason}")
            return body


# ---------------------------------------------------------------- raw fetches

async def get_player(tag: str) -> dict:
    return await _get(f"/players/{_norm_tag(tag)}")


async def get_battlelog(tag: str) -> dict:
    return await _get(f"/players/{_norm_tag(tag)}/battlelog")


async def get_events() -> list[dict]:
    data = await _get("/events/rotation")
    out = []
    for slot in data:
        ev = slot.get("event", {})
        out.append({"mode": ev.get("mode"), "map": ev.get("map"),
                    "startTime": slot.get("startTime"),
                    "endTime": slot.get("endTime")})
    return out


async def get_brawlers_slim() -> list[dict]:
    data = await _get("/brawlers")
    return [{"id": b["id"], "name": b["name"],
             "starPowers": [sp["name"] for sp in b.get("starPowers", [])],
             "gadgets": [g["name"] for g in b.get("gadgets", [])]}
            for b in data.get("items", [])]


async def get_rankings(country: str = "global", brawler_id: int | None = None) -> list[dict]:
    country = (country or "global").lower()
    if brawler_id is not None:
        data = await _get(f"/rankings/{country}/brawlers/{brawler_id}")
    else:
        data = await _get(f"/rankings/{country}/players")
    return [{"rank": p.get("rank"), "name": p.get("name"),
             "tag": p.get("tag"), "trophies": p.get("trophies")}
            for p in data.get("items", [])]


# ------------------------------------------------------------------ slimmers

def slim_player(data: dict) -> dict:
    """Profile + the player's COMPLETE brawler roster, sorted by trophies desc.

    Every brawler is included with its full loadout, so "brawlers below 1000
    trophies", "which Power 11s haven't I pushed", and gear/upgrade advice all
    see the real roster. A full 106-brawler profile is ~20k chars, well inside
    the 40k ceiling get_player serializes under.

    This used to detail only the top N by trophies (N=104), which silently
    demoted the tail to core-stats-only as Supercell shipped new brawlers —
    a constant that had to be hand-bumped every update or quietly go stale.
    """
    brawlers = sorted(data.get("brawlers", []),
                      key=lambda b: b.get("trophies", 0), reverse=True)

    # Pre-computed tallies so the model reports exact numbers instead of
    # hand-counting a ~90-item list (LLMs undercount long lists).
    power11_count = sum(1 for b in brawlers if b.get("power") == 11)
    power10_count = sum(1 for b in brawlers if b.get("power") == 10)

    def entry(b):
        return {"name": b.get("name"), "power": b.get("power"),
                "trophies": b.get("trophies"),
                "highestTrophies": b.get("highestTrophies"),
                "gadgets": [g["name"] for g in b.get("gadgets", [])],
                "starPowers": [s["name"] for s in b.get("starPowers", [])],
                "gears": [g["name"] for g in b.get("gears", [])]}

    # Profile icon as a renderable URL (Brawlify CDN). The API only gives the
    # numeric icon id; the model can hand this straight to present_answer's
    # thumbnail_url for profile answers.
    icon_id = (data.get("icon") or {}).get("id")
    icon_url = (f"https://cdn.brawlify.com/profile-icons/regular/{icon_id}.png"
                if icon_id else None)

    return {
        "name": data.get("name"),
        "tag": data.get("tag"),
        "iconUrl": icon_url,
        "trophies": data.get("trophies"),
        "highestTrophies": data.get("highestTrophies"),
        "expLevel": data.get("expLevel"),
        "victories3v3": data.get("3vs3Victories"),
        "soloVictories": data.get("soloVictories"),
        "duoVictories": data.get("duoVictories"),
        "club": (data.get("club") or {}).get("name"),
        "brawlerCount": len(data.get("brawlers", [])),
        "power11Count": power11_count,
        "power10Count": power10_count,
        "brawlers": [entry(b) for b in brawlers],
    }


def player_brawler(battle: dict, tag: str) -> str | None:
    """Find which brawler `tag` played in one battle. Players live either in a
    flat `players` list (showdown) or nested in `teams` (3v3/duo)."""
    want = _plain_tag(tag)
    groups = battle.get("teams") or [battle.get("players", [])]
    for group in groups:
        for player in group:
            if _plain_tag(player.get("tag", "")) == want:
                return (player.get("brawler") or {}).get("name")
    return None


def _rate(wins: int, total: int) -> float:
    return round(100 * wins / total, 1) if total else 0.0


def outcome(mode: str | None, result: str | None, rank: int | None) -> str | None:
    """Normalize a battle to "win"/"loss"/None(draw/unknown).

    Team modes report result directly. Showdown reports placement instead:
    solo (10 players) top 4 is a win; duo/trio (5 teams) top 2 is a win."""
    if result == "victory":
        return "win"
    if result == "defeat":
        return "loss"
    if result == "draw":
        return None
    if rank is not None:  # showdown placement
        win_cutoff = 4 if "solo" in (mode or "").lower() else 2
        return "win" if rank <= win_cutoff else "loss"
    return None


def summarize_battles(battles: list[dict]) -> dict:
    """Pre-computed overall / per-mode / per-brawler tallies over a battle list.

    Same reason slim_player ships power11Count: the model mis-aggregates a
    25-row list by hand (it merged soloShowdown into duoShowdown on a shared
    map). Each battle is a dict with mode/brawler/result/rank/trophyChange.
    """
    def bucket():
        return {"battles": 0, "wins": 0, "losses": 0, "trophyChange": 0}

    overall = bucket()
    per_mode: dict[str, dict] = {}
    per_brawler: dict[str, dict] = {}

    for b in battles:
        mode = b.get("mode")
        result = outcome(mode, b.get("result"), b.get("rank"))
        for acc in (overall,
                    per_mode.setdefault(mode or "unknown", bucket()),
                    per_brawler.setdefault(b.get("brawler") or "unknown", bucket())):
            acc["battles"] += 1
            acc["trophyChange"] += b.get("trophyChange") or 0
            if result == "win":
                acc["wins"] += 1
            elif result == "loss":
                acc["losses"] += 1

    def finalize(b):
        b["winRate"] = _rate(b["wins"], b["battles"])
        return b

    return {"overall": finalize(overall),
            "perMode": {k: finalize(v) for k, v in per_mode.items()},
            "perBrawler": {k: finalize(v) for k, v in per_brawler.items()}}


def slim_battlelog(data: dict, tag: str) -> list[dict]:
    """Trim a battlelog to what the model needs. `tag` identifies which player
    in each battle is 'us' — without it the brawler column can't be resolved
    and per-brawler questions get answered from thin air."""
    out = []
    for item in data.get("items", []):
        b = item.get("battle", {})
        ev = item.get("event", {})
        out.append({
            "time": item.get("battleTime"),
            "mode": b.get("mode") or ev.get("mode"),
            "map": ev.get("map"),
            "type": b.get("type"),
            "brawler": player_brawler(b, tag),  # brawler THIS player used
            "result": b.get("result"),        # victory/defeat/draw (3v3 etc.)
            "rank": b.get("rank"),            # showdown placement
            "trophyChange": b.get("trophyChange"),
            "starPlayer": (b.get("starPlayer") or {}).get("name"),
        })
    return out
