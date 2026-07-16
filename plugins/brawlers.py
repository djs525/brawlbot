"""Plugin: brawler catalog (official Supercell API, via bs_client)."""

import bs_client

from . import jsonout

# The catalog is a lookup table — the model maps a brawler NAME to its numeric
# id here before calling get_rankings. Truncating it drops real brawlers off
# the end (the list is trophy-ordered, so the newest ones go first) and makes
# them unlookupable. ~14k today; leave headroom for future releases.
CATALOG_LIMIT = 25000

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
        return jsonout.dump(await bs_client.get_brawlers_slim(), limit=CATALOG_LIMIT)
    return f"ERROR: brawlers got unknown tool '{name}'"
