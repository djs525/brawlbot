"""Plugin: official Supercell Brawl Stars API — player profile + battle log.

Thin adapter over bs_client, which owns the HTTP/auth (single BRAWL_STARS_TOKEN)
and the slim_* payload trimmers. This file used to duplicate bs_client with its
own BRAWL_API_TOKEN and no slimming — that token is gone now.
"""

import bs_client

from . import jsonout

# The full roster with loadouts runs ~20k chars, over jsonout's 12k default, and
# it's a profile we want whole — brawlerCount and the power tallies have to
# agree with the list under them. Give this one payload its own ceiling.
PLAYER_LIMIT = 40000

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
        return jsonout.dump(bs_client.slim_player(data), limit=PLAYER_LIMIT)
    if name == "get_battlelog":
        raw = await bs_client.get_battlelog(tool_input["tag"])
        battles = bs_client.slim_battlelog(raw, tool_input["tag"])
        # Tallies go over the full list, before any trimming, so a capped
        # battle list can't skew the per-brawler/per-mode numbers.
        return jsonout.dump({
            "summary": bs_client.summarize_battles(battles),
            "battles": battles,
        })
    return f"ERROR: official_bs got unknown tool '{name}'"
