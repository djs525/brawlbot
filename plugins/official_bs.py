"""Plugin: official Supercell Brawl Stars API (live player data)."""

import os
import json
import urllib.parse
import aiohttp

BASE = "https://api.brawlstars.com/v1"
TOKEN = os.environ["BRAWL_API_TOKEN"]

TOOLS = [
    {
        "name": "get_player",
        "description": (
            "Get a Brawl Stars player's full profile by player tag: "
            "name, trophies, highest trophies, 3v3/solo/duo victories, "
            "club, and every brawler they own with power level, rank, "
            "trophies, gadgets, star powers, and gears."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Player tag, e.g. #ABC123"}
            },
            "required": ["tag"],
        },
    },
    {
        "name": "get_battlelog",
        "description": (
            "Get a player's ~25 most recent battles: mode, map, result, "
            "trophy change, teammates, and brawlers used. Use for questions "
            "about recent performance, win rates, or what someone has been playing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Player tag, e.g. #ABC123"}
            },
            "required": ["tag"],
        },
    },
    # ...add entries for any other Supercell tools you already had
]


async def _api_get(path: str) -> str:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE}{path}", headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return json.dumps(data)[:12000]


def _encode_tag(tag: str) -> str:
    # '#' is special in URLs, so #ABC123 must become %23ABC123
    return urllib.parse.quote(tag, safe="")


async def execute(name: str, tool_input: dict) -> str:
    if name == "get_player":
        return await _api_get(f"/players/{_encode_tag(tool_input['tag'])}")
    if name == "get_battlelog":
        return await _api_get(f"/players/{_encode_tag(tool_input['tag'])}/battlelog")
    return f"ERROR: official_bs got unknown tool '{name}'"