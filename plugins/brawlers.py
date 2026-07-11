"""Plugin: brawler catalog (official Supercell API, via bs_client)."""

import json

import bs_client

TOOLS = [
    {
        "name": "get_brawlers",
        "description": (
            "Get the full catalog of every Brawl Stars brawler with its id, "
            "name, star powers, and gadgets. Use to look up what a brawler's "
            "star powers/gadgets are called, or to map a brawler name to its "
            "numeric id (needed by get_rankings for a per-brawler leaderboard)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


async def execute(name: str, tool_input: dict) -> str:
    if name == "get_brawlers":
        return json.dumps(await bs_client.get_brawlers_slim())[:12000]
    return f"ERROR: brawlers got unknown tool '{name}'"
