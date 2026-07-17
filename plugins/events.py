"""Plugin: live event rotation (+ best-effort upcoming maps).

Primary source is the OFFICIAL Supercell rotation (bs_client.get_events), which
is authoritative, live-now, and always populated — but it has NO future data.

Upcoming/next maps come only from BrawlAPI's community feed (`/v1/events`, its
`upcoming` list). That feed is flaky: it intermittently returns empty active AND
upcoming lists. So it's used two ways here:
  - as an enrichment (upcoming maps) layered on top of the official rotation, and
  - as a fallback for the now-rotation when the official call fails.
When BrawlAPI gives no upcoming, we say so honestly rather than let the model
invent a future map — see NOW_ONLY_NOTE / HAS_UPCOMING_NOTE.
"""

import datetime

import aiohttp

import bs_client

from . import jsonout

# Stamped on the payload when we have NO upcoming data. Keeps the model from
# presenting a live map as a future one, or inventing tomorrow's map wholesale.
NOW_ONLY_NOTE = (
    "The 'rotation' list is LIVE-NOW ONLY — every map in it is active at 'now'. "
    "No upcoming/next/tomorrow data is available right now (the upcoming feed is "
    "empty). If asked about a future map, say you can only see the CURRENT "
    "rotation and do NOT invent or guess a future map name."
)

# Stamped when BrawlAPI DID return upcoming maps. Now the model may answer
# next-map questions — but only from the 'upcoming' list, never invented.
HAS_UPCOMING_NOTE = (
    "'rotation' is LIVE-NOW (active at 'now'). 'upcoming' holds FUTURE maps with "
    "their startTime — use these for 'next map' / 'tomorrow' questions. Only ever "
    "name a future map that actually appears in 'upcoming'; if a mode the user "
    "asked about is not in 'upcoming', say its next map isn't available yet rather "
    "than guessing."
)

BRAWLAPI_EVENTS = "https://api.brawlapi.com/v1/events"


TOOLS = [
    {
        "name": "get_event_rotation",
        "description": (
            "Get the Brawl Stars event rotation: which game modes are active "
            "right now, the map each is on, and when each ends ('rotation', "
            "live-now), PLUS any known upcoming/next maps with their start times "
            "('upcoming', when available). Use for any question about what maps or "
            "modes are up now, what's coming next, or map-aware brawler picks. The "
            "'now' field is the current UTC time. Upcoming data is best-effort and "
            "often empty — when it is, only current maps are known; never invent a "
            "future map that isn't in the 'upcoming' list."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def _brawlapi_events() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(BRAWLAPI_EVENTS) as resp:
            resp.raise_for_status()
            return await resp.json()


def _slim_upcoming(e: dict) -> dict:
    """Trim one BrawlAPI upcoming entry to what the model needs. Field names vary
    across the feed's shape, so pull defensively with fallbacks."""
    m = e.get("map") or {}
    gm = m.get("gameMode") or {}
    return {
        "mode": gm.get("name") or e.get("mode"),
        "map": m.get("name") or e.get("map"),
        "startTime": e.get("startTime") or (e.get("slot") or {}).get("startTime"),
        "endTime": e.get("endTime") or (e.get("slot") or {}).get("endTime"),
        "imageUrl": m.get("imageUrl"),
    }


async def _fetch_upcoming() -> tuple[list[dict], dict | None]:
    """Best-effort upcoming maps from BrawlAPI. Returns (upcoming, raw_or_None);
    never raises — a failure just yields an empty list so the official rotation
    still gets returned."""
    try:
        data = await _brawlapi_events()
    except Exception as e:
        print(f"[events] BrawlAPI upcoming fetch failed: {e}")
        return [], None
    upcoming = [_slim_upcoming(x) for x in (data.get("upcoming") or [])]
    return upcoming, data


async def execute(name: str, tool_input: dict) -> str:
    if name != "get_event_rotation":
        return f"ERROR: events got unknown tool '{name}'"

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.000Z")

    # Official rotation first — the game's real live-now rotation, never empty.
    try:
        slots = await bs_client.get_events()
    except Exception as e:
        slots = []
        print(f"[events] official rotation failed, trying BrawlAPI: {e}")

    # Enrichment: upcoming maps (best-effort, often empty). Also reused below as
    # the now-rotation fallback if the official call failed.
    upcoming, brawlapi_raw = await _fetch_upcoming()
    note = HAS_UPCOMING_NOTE if upcoming else NOW_ONLY_NOTE

    if slots:
        payload = {"source": "official", "now": now, "note": note,
                   "rotation": slots}
        if upcoming:
            payload["upcoming"] = upcoming
        return jsonout.dump(payload)

    # Official failed — fall back to BrawlAPI's active list for the now-rotation.
    active = (brawlapi_raw or {}).get("active") or []
    if not (active or upcoming):
        return ("ERROR: event rotation is currently empty from both sources — "
                "try again shortly.")
    payload = {"source": "brawlapi", "now": now, "note": note, "active": active}
    if upcoming:
        payload["upcoming"] = upcoming
    return jsonout.dump(payload)
