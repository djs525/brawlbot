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


def _norm_tag(tag: str) -> str:
    """Uppercase, ensure leading #, URL-encode (# -> %23) for the path."""
    tag = tag.strip().upper()
    if not tag.startswith("#"):
        tag = "#" + tag
    return quote(tag, safe="")


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

# How many top-trophy brawlers get full loadout detail. Set to cover the whole
# roster so upgrade/resource-allocation advice sees every brawler's gadgets,
# star powers and gears — not just the high-trophy ones. Heavier per tool-loop
# turn, but the full payload still fits under the caller's _fit limit.
# NOTE: this is a count, not "all" — if Supercell ships more than this many
# brawlers, the excess would drop to core-stats-only. Bump it when that happens.
DETAILED_BRAWLERS = 104

def slim_player(data: dict) -> dict:
    """Profile + the player's COMPLETE brawler roster, sorted by trophies desc.

    Every brawler is included, so "brawlers below 1000 trophies" or "which
    Power 11s haven't I pushed" answer accurately. To keep the payload light,
    only the top DETAILED_BRAWLERS carry gadgets/starpowers/gears; the tail
    carries core stats (name/power/trophies) — visible, just not detailed.
    """
    brawlers = sorted(data.get("brawlers", []),
                      key=lambda b: b.get("trophies", 0), reverse=True)

    def entry(b, detailed):
        e = {"name": b.get("name"), "power": b.get("power"),
             "trophies": b.get("trophies"),
             "highestTrophies": b.get("highestTrophies")}
        if detailed:
            e["gadgets"] = [g["name"] for g in b.get("gadgets", [])]
            e["starPowers"] = [s["name"] for s in b.get("starPowers", [])]
            e["gears"] = [g["name"] for g in b.get("gears", [])]
        return e

    return {
        "name": data.get("name"),
        "tag": data.get("tag"),
        "trophies": data.get("trophies"),
        "highestTrophies": data.get("highestTrophies"),
        "expLevel": data.get("expLevel"),
        "victories3v3": data.get("3vs3Victories"),
        "soloVictories": data.get("soloVictories"),
        "duoVictories": data.get("duoVictories"),
        "club": (data.get("club") or {}).get("name"),
        "brawlerCount": len(data.get("brawlers", [])),
        # Detail note so the model knows the tail isn't missing gear data by
        # accident — it can ask for a specific brawler if a user needs it.
        "loadoutDetailFor": f"top {DETAILED_BRAWLERS} by trophies; rest show core stats only",
        "brawlers": [entry(b, i < DETAILED_BRAWLERS)
                     for i, b in enumerate(brawlers)],
    }


def slim_battlelog(data: dict) -> list[dict]:
    out = []
    for item in data.get("items", []):
        b = item.get("battle", {})
        ev = item.get("event", {})
        out.append({
            "time": item.get("battleTime"),
            "mode": b.get("mode") or ev.get("mode"),
            "map": ev.get("map"),
            "type": b.get("type"),
            "result": b.get("result"),        # victory/defeat/draw (3v3 etc.)
            "rank": b.get("rank"),            # showdown placement
            "trophyChange": b.get("trophyChange"),
            "starPlayer": (b.get("starPlayer") or {}).get("name"),
        })
    return out
