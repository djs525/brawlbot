"""Gemini tool-use agent over the Brawl Stars data layer.

Tools are not defined here — they live in the `plugins/` package and are
discovered at runtime via the plugin registry (plugins.load_plugins() must
run before the first question, which bot.py does at startup).
"""
import asyncio
import json
import os

from google import genai
from google.genai import types

import plugins

SYSTEM = """You are a Brawl Stars analytics assistant embedded in a Discord bot.
You answer questions about a player's profile, recent battles, long-term history,
the current event rotation, maps, the brawler catalog, and rankings — using
tools, never guessing at data.

Domain notes:
- Trophies per brawler; 'trophyChange' is per-battle delta. type=soloRanked is Ranked mode.
- Showdown results come as rank (1-10 solo, 1-5 duo) instead of victory/defeat.
- Battlelog covers only ~25 recent battles; use query_history for longer windows.
- get_player returns pre-computed tallies (power11Count, power10Count, brawlerCount).
  For "how many" questions, report these numbers directly — do NOT hand-count the
  brawler list, you will miscount it.

The first message contains "asker_tag" — the questioner's own player tag.
When they say "me"/"my", use that tag.

Be concrete: cite numbers, win rates, trophy deltas. Give actionable advice.
Keep answers under 1800 characters — this is Discord, not a report."""

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
    if last_err is None:
        raise RuntimeError("No models configured or all models failed without raising an exception.")
    raise last_err


def _gemini_tool() -> types.Tool:
    """Build the Gemini tool menu from the plugin registry. Plugins declare
    their schema under the Anthropic-style `input_schema` key; Gemini wants it
    under `parameters`, so translate on the way through."""
    return types.Tool(function_declarations=[
        {"name": t["name"],
         "description": t["description"],
         "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}
        for t in plugins.all_tools()
    ])


async def answer_question(question: str, asker_tag: str | None,
                          mentioned: dict[str, str] | None = None) -> str:
    context = {"asker_tag": asker_tag or "NOT LINKED",
               "mentioned_players": mentioned or {}}
    contents = [types.Content(role="user", parts=[
        types.Part(text=f"{json.dumps(context)}\n\nQuestion: {question}")])]

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        tools=[_gemini_tool()],
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
        # plugins.execute already truncates payloads and wraps its own errors.
        results = await asyncio.gather(
            *(plugins.execute(c.name, dict(c.args or {})) for c in calls))
        parts = [
            types.Part.from_function_response(
                name=call.name, response={"result": out})
            for call, out in zip(calls, results)
        ]
        contents.append(types.Content(role="user", parts=parts))

    return "I got lost in the data — try a more specific question."
