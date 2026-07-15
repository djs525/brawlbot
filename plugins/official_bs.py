"""Plugin: official Supercell Brawl Stars API — player profile + battle log.

Thin adapter over bs_client, which owns the HTTP/auth (single BRAWL_STARS_TOKEN)
and the slim_* payload trimmers. This file used to duplicate bs_client with its
own BRAWL_API_TOKEN and no slimming — that token is gone now.
"""

import json

import bs_client

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
]


async def execute(name: str, tool_input: dict) -> str:
    if name == "get_player":
        data = await bs_client.get_player(tool_input["tag"])
        # slim_player is already size-bounded (full roster ~20k chars); a 12k
        # slice here would chop the tail brawlers and undercount the roster.
        return json.dumps(bs_client.slim_player(data))[:40000]
    if name == "get_battlelog":
        raw = await bs_client.get_battlelog(tool_input["tag"])
        return json.dumps(bs_client.slim_battlelog(raw))[:12000]
    return f"ERROR: official_bs got unknown tool '{name}'"
