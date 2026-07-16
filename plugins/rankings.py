"""Plugin: leaderboards (official Supercell API, via bs_client)."""

import bs_client

from . import jsonout

TOOLS = [
    {
        "name": "get_rankings",
        "description": (
            "Get top-player or top-brawler rankings for a country. Pass a "
            "two-letter country code (e.g. 'us') or 'global'. If brawler_id is "
            "given, returns that brawler's leaderboard; otherwise the overall "
            "trophy leaderboard. Look up a brawler_id with get_brawlers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": {
                    "type": "string",
                    "description": "Two-letter country code or 'global'.",
                },
                "brawler_id": {
                    "type": "integer",
                    "description": "Numeric brawler id for a per-brawler leaderboard.",
                },
            },
            "required": [],
        },
    },
]


async def execute(name: str, tool_input: dict) -> str:
    if name == "get_rankings":
        data = await bs_client.get_rankings(
            tool_input.get("country", "global"),
            tool_input.get("brawler_id"),
        )
        return jsonout.dump(data)
    return f"ERROR: rankings got unknown tool '{name}'"
