"""Plugin: BrawlAPI (brawlapi.com) — event rotation, maps, images. No key needed."""

import json
import aiohttp

BASE = "https://api.brawlapi.com/v1"

TOOLS = [
    {
        "name": "get_event_rotation",
        "description": (
            "Get the CURRENT and UPCOMING Brawl Stars event rotation: active "
            "game modes, which map each mode is on, when events end, and a "
            "map image URL for each. Use for any question about what maps or "
            "modes are up right now or coming next."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_map_details",
        "description": (
            "Get details for one specific map by its numeric map ID (found in "
            "the event rotation data): game mode, environment, and image URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "map_id": {"type": "integer", "description": "Numeric map ID"}
            },
            "required": ["map_id"],
        },
    },
]


async def _get(path: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE}{path}") as resp:
            resp.raise_for_status()
            data = await resp.json()
    return json.dumps(data)[:12000]


async def execute(name: str, tool_input: dict) -> str:
    if name == "get_event_rotation":
        return await _get("/events")
    if name == "get_map_details":
        return await _get(f"/maps/{tool_input['map_id']}")
    return f"ERROR: brawlapi got unknown tool '{name}'"