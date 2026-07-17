"""Plugin: live event rotation.

Primary source is the OFFICIAL Supercell rotation (bs_client.get_events), which
is authoritative and always populated. BrawlAPI's community feed is used only as
a fallback — it intermittently returns empty active/upcoming lists, which is what
made the bot say "no event data".
"""

import datetime

import aiohttp

import bs_client

from . import jsonout

# Stamped onto every rotation payload so the model never presents a listed map as
# a future/upcoming one. The official feed is now-only; there is no tomorrow data.
NOW_ONLY_NOTE = (
    "This rotation is LIVE-NOW ONLY. Every map here is active at 'now'. It does "
    "NOT contain upcoming, next, or tomorrow's maps — that data is not available "
    "from this source. If asked about a future map, say you can only see the "
    "current rotation and do NOT invent or guess a future map name."
)

BRAWLAPI_EVENTS = "https://api.brawlapi.com/v1/events"

TOOLS = [
    {
        "name": "get_event_rotation",
        "description": (
            "Get the CURRENT live Brawl Stars event rotation: which game modes "
            "are active right now, the map each mode is on, and when each event "
            "ends. Use for any question about what maps or modes are up right "
            "now, or map-aware brawler pick recommendations. IMPORTANT: this "
            "returns ONLY what is live right now — it does NOT include upcoming / "
            "tomorrow's / future maps. The 'now' field is the current UTC time so "
            "you can see how long each event has left; every map listed is active "
            "now, never a future one."
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

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.000Z")

    if slots:
        return jsonout.dump({"source": "official", "now": now,
                             "note": NOW_ONLY_NOTE, "rotation": slots})

    # Fallback: BrawlAPI community feed (may itself be empty).
    try:
        data = await _brawlapi_fallback()
    except Exception as e:
        return f"ERROR: no event rotation available (official empty, BrawlAPI failed: {e})"
    if not (data.get("active") or data.get("upcoming")):
        return "ERROR: event rotation is currently empty from both sources — try again shortly."
    return jsonout.dump({"source": "brawlapi", "now": now,
                         "note": NOW_ONLY_NOTE, **data})
