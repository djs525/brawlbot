"""Plugin: live event rotation.

Primary source is the OFFICIAL Supercell rotation (bs_client.get_events), which
is authoritative and always populated. BrawlAPI's community feed is used only as
a fallback — it intermittently returns empty active/upcoming lists, which is what
made the bot say "no event data".
"""

import aiohttp

import bs_client

from . import jsonout

BRAWLAPI_EVENTS = "https://api.brawlapi.com/v1/events"

TOOLS = [
    {
        "name": "get_event_rotation",
        "description": (
            "Get the CURRENT live Brawl Stars event rotation: which game modes "
            "are active right now, the map each mode is on, and when each event "
            "ends. Use for any question about what maps or modes are up right "
            "now, or map-aware brawler pick recommendations."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def _brawlapi_fallback() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(BRAWLAPI_EVENTS) as resp:
            resp.raise_for_status()
            return await resp.json()


async def execute(name: str, tool_input: dict) -> str:
    if name != "get_event_rotation":
        return f"ERROR: events got unknown tool '{name}'"

    # Official rotation first — it's the game's real rotation, never empty.
    try:
        slots = await bs_client.get_events()
    except Exception as e:
        slots = []
        print(f"[events] official rotation failed, trying BrawlAPI: {e}")

    if slots:
        return jsonout.dump({"source": "official", "rotation": slots})

    # Fallback: BrawlAPI community feed (may itself be empty).
    try:
        data = await _brawlapi_fallback()
    except Exception as e:
        return f"ERROR: no event rotation available (official empty, BrawlAPI failed: {e})"
    if not (data.get("active") or data.get("upcoming")):
        return "ERROR: event rotation is currently empty from both sources — try again shortly."
    return jsonout.dump({"source": "brawlapi", **data})
