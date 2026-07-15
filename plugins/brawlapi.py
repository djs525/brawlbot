"""Plugin: BrawlAPI (brawlapi.com) — event rotation, maps, images. No key needed."""

import json
import time

import aiohttp

BASE = "https://api.brawlapi.com/v1"

TOOLS = [
    {
        "name": "get_map_details",
        "description": (
            "Get details for one Brawl Stars map — game mode, environment, and "
            "image URL. Identify the map by NAME (e.g. 'Hard Rock Mine', as it "
            "appears in the event rotation) or by numeric map ID. Prefer the "
            "name: the rotation gives map names, not IDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "map_name": {
                    "type": "string",
                    "description": "Map name, e.g. 'Hard Rock Mine' (case-insensitive).",
                },
                "map_id": {"type": "integer", "description": "Numeric map ID."},
            },
            # Neither is required by schema, but exactly one must be given —
            # enforced in execute() so we can return a helpful error instead.
            "required": [],
        },
    },
    {
        "name": "get_brawler_info",
        "description": (
            "Static ROSTER reference for a brawler, NOT a player's owned "
            "brawler. Use ONLY for a brawler's archetype/class (Damage Dealer, "
            "Tank, Assassin, Marksman, Artillery, Controller, Support), rarity, "
            "or lore — the official player API does NOT carry these. It also "
            "returns star power / gadget DESCRIPTIONS (what each ability does), "
            "which the player API lacks. Do NOT use this to check which star "
            "powers/gadgets a PLAYER owns or their power level — get_player "
            "carries the player's actual loadout. Identify by NAME (e.g. "
            "'Shelly', case-insensitive) or numeric ID. Omit both to get the "
            "whole roster as a compact name→archetype list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brawler_name": {
                    "type": "string",
                    "description": "Brawler name, e.g. 'Shelly' (case-insensitive).",
                },
                "brawler_id": {"type": "integer", "description": "Numeric brawler ID."},
            },
            "required": [],
        },
    },
]

# The full /maps catalog is ~880KB, so cache it to avoid refetching on every
# name lookup. Maps change rarely; a 1-hour TTL is plenty fresh.
_MAPS_TTL = 3600
_maps_cache: tuple[float, list[dict]] | None = None

# Same treatment for the /brawlers catalog. Roster changes only on updates.
_BRAWLERS_TTL = 3600
_brawlers_cache: tuple[float, list[dict]] | None = None


async def _get_json(path: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE}{path}") as resp:
            resp.raise_for_status()
            return await resp.json()


async def _all_maps() -> list[dict]:
    global _maps_cache
    now = time.monotonic()
    if _maps_cache and now - _maps_cache[0] < _MAPS_TTL:
        return _maps_cache[1]
    maps = (await _get_json("/maps")).get("list", [])
    _maps_cache = (now, maps)
    return maps


async def _resolve_id(map_name: str) -> int | None:
    """Map name -> numeric id. Case-insensitive. Prefers an active (non-disabled)
    map when the same name exists across versions."""
    key = map_name.strip().lower()
    hits = [m for m in await _all_maps()
            if (m.get("name") or "").strip().lower() == key]
    if not hits:
        return None
    active = [m for m in hits if not m.get("disabled")]
    return (active or hits)[0].get("id")


async def _all_brawlers() -> list[dict]:
    global _brawlers_cache
    now = time.monotonic()
    if _brawlers_cache and now - _brawlers_cache[0] < _BRAWLERS_TTL:
        return _brawlers_cache[1]
    brawlers = (await _get_json("/brawlers")).get("list", [])
    _brawlers_cache = (now, brawlers)
    return brawlers


def _slim_brawler(b: dict) -> dict:
    """Keep archetype/rarity/lore + loadout names. Drop CDN image cruft."""
    def loadout(items):
        return [{"name": it.get("name"), "description": it.get("description")}
                for it in items or []]
    return {
        "id": b.get("id"),
        "name": b.get("name"),
        # BrawlAPI tags the newest brawlers 'Unknown' until they classify them.
        "archetype": (b.get("class") or {}).get("name"),
        "rarity": (b.get("rarity") or {}).get("name"),
        "description": b.get("description"),
        "starPowers": loadout(b.get("starPowers")),
        "gadgets": loadout(b.get("gadgets")),
    }


def _slim(m: dict) -> dict:
    """Keep the fields the model can actually use; drop CDN cruft and the
    stats/teamStats arrays (BrawlAPI always returns these empty)."""
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "gameMode": (m.get("gameMode") or {}).get("name"),
        "environment": (m.get("environment") or {}).get("name"),
        "disabled": m.get("disabled"),
        "new": m.get("new"),
        "imageUrl": m.get("imageUrl"),
        "link": m.get("link"),
    }


async def execute(name: str, tool_input: dict) -> str:
    if name == "get_brawler_info":
        return await _execute_brawler(tool_input)
    if name != "get_map_details":
        return f"ERROR: brawlapi got unknown tool '{name}'"

    map_id = tool_input.get("map_id")
    map_name = tool_input.get("map_name")

    if map_id is None and map_name:
        map_id = await _resolve_id(map_name)
        if map_id is None:
            return f"ERROR: no map named '{map_name}' found in the BrawlAPI catalog."
    if map_id is None:
        return "ERROR: get_map_details needs a map_name or map_id."

    data = await _get_json(f"/maps/{map_id}")
    return json.dumps(_slim(data))[:12000]


async def _execute_brawler(tool_input: dict) -> str:
    brawler_id = tool_input.get("brawler_id")
    brawler_name = tool_input.get("brawler_name")
    roster = await _all_brawlers()

    # No selector -> compact roster of name -> archetype so the model can scan
    # every brawler's role in one shot without pulling full loadouts.
    if brawler_id is None and not brawler_name:
        return json.dumps([{"name": b.get("name"),
                            "archetype": (b.get("class") or {}).get("name")}
                           for b in roster])

    if brawler_id is not None:
        hit = next((b for b in roster if b.get("id") == brawler_id), None)
    else:
        key = brawler_name.strip().lower()
        hit = next((b for b in roster
                    if (b.get("name") or "").strip().lower() == key), None)

    if hit is None:
        who = brawler_name if brawler_name else brawler_id
        return f"ERROR: no brawler '{who}' found in the BrawlAPI catalog."
    return json.dumps(_slim_brawler(hit))[:12000]
