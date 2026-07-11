"""Gemini tool-use agent over the Brawl Stars data layer."""
import asyncio
import json
import os

from google import genai
from google.genai import types

import bs_client
import store

SYSTEM = """You are a Brawl Stars analytics assistant embedded in a Discord bot.
You answer questions about a player's profile, recent battles, long-term history,
the current event rotation, and rankings — using tools, never guessing at data.

Domain notes:
- Trophies per brawler; 'trophyChange' is per-battle delta. type=soloRanked is Ranked mode.
- Showdown results come as rank (1-10 solo, 1-5 duo) instead of victory/defeat.
- Battlelog covers only ~25 recent battles; use query_history for longer windows
  (only works if the player is tracked).

The first message contains "asker_tag" — the questioner's own player tag.
When they say "me"/"my", use that tag.

Be concrete: cite numbers, win rates, trophy deltas. Give actionable advice.
Keep answers under 1800 characters — this is Discord, not a report."""

TOOLS = types.Tool(function_declarations=[
    {"name": "get_player",
     "description": "Full player profile: trophies, brawlers with power/gadgets/star powers/gears, victories, club.",
     "parameters": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]}},
    {"name": "get_battlelog",
     "description": "Last ~25 battles with mode, map, result, trophy change, teams.",
     "parameters": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]}},
    {"name": "query_history",
     "description": "Aggregated stats over N days from stored history: per-brawler/mode/map win rates and trophy deltas. Tracked players only.",
     "parameters": {"type": "object",
                    "properties": {"tag": {"type": "string"}, "days": {"type": "integer"}},
                    "required": ["tag"]}},
    {"name": "get_events",
     "description": "Current event rotation: active modes and maps.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_brawlers",
     "description": "Catalog of all brawlers with their star powers and gadgets.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_rankings",
     "description": "Top player or brawler rankings for a country code (or 'global').",
     "parameters": {"type": "object",
                    "properties": {"country": {"type": "string"}, "brawler_id": {"type": "integer"}},
                    "required": []}},
])

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Tried in order; each fallback is cheaper/lighter than the one before it.
# When a model is unavailable (overloaded / rate-limited / 5xx) we drop to the
# next one instead of failing the whole request.
MODELS = [
    "gemini-flash-latest",       # alias: always the current flash
    "gemini-flash-lite-latest",  # alias: always the current flash-lite
    "gemini-3.1-flash-lite",     # concrete lite pin as a backstop
    "gemini-2.0-flash-lite",     # oldest cheap fallback (free-tier quota)
]

# Error signatures that mean "try another model" rather than "give up".
# 404/not_found included: a model that's dead for this key should drop to the
# next one, not kill the whole request.
_RETRYABLE = ("503", "502", "500", "429", "404", "not_found", "unavailable",
              "overloaded", "rate limit", "resource_exhausted", "quota")


def _is_retryable(err: Exception) -> bool:
    msg = str(err).lower()
    return any(sig in msg for sig in _RETRYABLE)


async def _generate(contents, config):
    """generate_content with model fallback. Walk the MODELS chain; on a
    retryable error drop to the next (cheaper) model. Re-raise the last error
    if every model fails, or immediately on a non-retryable error."""
    last_err = None
    for model in MODELS:
        try:
            return await client.aio.models.generate_content(
                model=model, contents=contents, config=config)
        except Exception as e:
            if not _is_retryable(e):
                raise
            last_err = e
            print(f"[agent] model {model} unavailable ({e}); falling back")
    raise last_err


def _fit(obj, limit: int = 40000):
    """Shrink a tool result so its JSON stays under `limit` chars WITHOUT
    breaking JSON validity (the old code sliced the serialized string mid-token).
    Lists get their tail dropped; oversized dicts trim list/dict fields; long
    strings get clipped."""
    if len(json.dumps(obj)) <= limit:
        return obj
    if isinstance(obj, list):
        kept = []
        for item in obj:
            if len(json.dumps(kept + [item])) > limit:
                break
            kept.append(item)
        kept.append({"_truncated": f"{len(obj) - len(kept)} more items omitted"})
        return kept
    if isinstance(obj, dict):
        d = {k: (_fit(v, limit // 2) if isinstance(v, (list, dict)) else v)
             for k, v in obj.items()}
        if len(json.dumps(d)) <= limit:
            return d
        return {"_truncated": "payload too large", "keys": list(obj)[:20]}
    if isinstance(obj, str):
        return obj[:limit - 20] + "…"
    return obj


async def _execute(name: str, args: dict) -> dict:
    if name == "get_player":
        return bs_client.slim_player(await bs_client.get_player(args["tag"]))
    if name == "get_battlelog":
        raw = await bs_client.get_battlelog(args["tag"])
        store.record_battles(args["tag"], raw)  # snapshot for long-window history
        return bs_client.slim_battlelog(raw)
    if name == "query_history":
        return store.query_history(args["tag"], args.get("days", 30))
    if name == "get_events":
        return await bs_client.get_events()
    if name == "get_brawlers":
        return await bs_client.get_brawlers_slim()
    if name == "get_rankings":
        return await bs_client.get_rankings(args.get("country", "global"),
                                            args.get("brawler_id"))
    return {"error": f"unknown tool {name}"}


async def answer_question(question: str, asker_tag: str | None,
                          mentioned: dict[str, str] | None = None) -> str:
    context = {"asker_tag": asker_tag or "NOT LINKED",
               "mentioned_players": mentioned or {}}
    contents = [types.Content(role="user", parts=[
        types.Part(text=f"{json.dumps(context)}\n\nQuestion: {question}")])]

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        tools=[TOOLS],
        max_output_tokens=3000,
        # we run the loop ourselves for control + async tools:
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    for _ in range(8):  # capped tool loop
        resp = await _generate(contents, config)

        calls = resp.function_calls or []
        if not calls:
            return resp.text or "I couldn't produce an answer — try rephrasing."

        contents.append(resp.candidates[0].content)  # the model's tool-call turn

        # Run all tool calls in this turn concurrently — a multi-call turn
        # (e.g. get_player + get_battlelog) no longer pays each tool's latency
        # in series. gather preserves order, so parts still line up with calls.
        async def _run(call):
            try:
                return await _execute(call.name, dict(call.args or {}))
            except Exception as e:
                return {"error": str(e)}

        results = await asyncio.gather(*(_run(c) for c in calls))
        parts = [
            types.Part.from_function_response(
                name=call.name,
                # truncate huge payloads so we stay inside free-tier token limits
                response={"result": _fit(out)})
            for call, out in zip(calls, results)
        ]
        contents.append(types.Content(role="user", parts=parts))

    return "I got lost in the data — try a more specific question."