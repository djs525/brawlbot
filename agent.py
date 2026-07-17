"""Gemini tool-use agent over the Brawl Stars data layer.

Tools are not defined here — they live in the `plugins/` package and are
discovered at runtime via the plugin registry (plugins.load_plugins() must
run before the first question, which bot.py does at startup).
"""
import asyncio
import json
import os
from dataclasses import dataclass, field

from google import genai
from google.genai import types

import plugins


@dataclass
class Answer:
    """One embed view. The bot renders it as a Discord embed.

    Only `summary` is required; everything else is optional decoration. When the
    model finishes with plain prose instead of calling present_answer, we wrap
    that text as `summary` and leave the rest empty — the embed degrades to a
    plain description, so nothing downstream has to special-case it.

    `tabs` holds alternate views of the SAME answer (e.g. Overview / By Mode /
    By Brawler). When present, the bot renders one button per view and swaps the
    embed on click — no re-query, the model already had all the data. Each tab is
    itself an Answer (its own summary/fields/images), distinguished by `label`
    (the button text). Tabs never nest — a tab's own `tabs` is ignored.
    """
    summary: str
    title: str | None = None
    fields: list[dict] = field(default_factory=list)
    image_url: str | None = None      # big banner image (maps)
    thumbnail_url: str | None = None  # small corner image (player icon / brawler)
    color: str | None = None          # hex like "#00b0f4"
    label: str | None = None          # button text when this Answer is a tab
    tabs: list["Answer"] = field(default_factory=list)

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
- get_battlelog and query_history both return a pre-computed summary with
  overall/perMode/perBrawler tallies (battles, wins, losses, trophyChange,
  winRate). For "how did I do on X" / "breakdown by brawler or mode" questions,
  report those numbers directly — do NOT re-aggregate the battle list by hand.
- Each battle's "brawler" field is the brawler THIS player used. Never infer or
  guess a brawler; if the field is absent, say so rather than naming one.
- The event rotation is LIVE-NOW ONLY — there is no data source for upcoming,
  next, or tomorrow's maps. If asked about a future map ("next Basket Brawl map",
  "tomorrow's rotation"), say plainly that you can only see the CURRENT rotation
  and cannot see future maps — NEVER invent a map name. You may still give
  mode-generic advice (e.g. good pushers for Basket Brawl as a mode) and apply
  roster filters (P11, under 1000 trophies), just without tying it to an unseen
  future map.

The first message contains "asker_tag" — the questioner's own player tag.
When they say "me"/"my", use that tag.

Be concrete: cite numbers, win rates, trophy deltas. Give actionable advice.
Keep answers under 1800 characters — this is Discord, not a report.

OUTPUT FORMAT — always finish by calling the `present_answer` tool (call it
alone, as your final step, after you have gathered all the data you need). This
renders a rich Discord embed. Guidelines:
- `summary`: your prose answer (<1800 chars). Numbers and advice go here.
- `title`: a short headline for the card, e.g. "Your Showdown — Last 30 Days".
- `fields`: use for stat breakdowns — one field per mode/brawler/metric with a
  short `name` and a compact `value`. Set `inline: true` on short stat fields so
  they sit side by side. Prefer fields over a wall of bullet points in summary.
- `image_url`: a large banner image. Set it to a map's `imageUrl` when the answer
  is about a specific map or the event rotation.
- `thumbnail_url`: a small corner image. Use the player's `iconUrl` for profile
  answers, or a brawler's `imageUrl` when the answer is about one brawler.
- Only ever use image URLs that appeared in tool results — never invent one.
- `color`: optional hex accent, e.g. "#f5c518" for trophies, "#e84d4d" for
  warnings/tilt, "#4da3ff" for neutral info.
- `tabs`: optional. When an answer has natural alternate cuts of the SAME data,
  add tabs and the bot shows a button per view. The top-level summary/fields is
  the default "Overview" view; each tab is an additional view with its own
  `label` (button text, 1-2 words), `summary`, and optional `fields`/images.
  Good uses: battlelog/history → tabs "By Mode" and "By Brawler" (one field per
  mode / per brawler); profile → tabs "Top Brawlers" and "Victories". Keep it to
  at most 3 tabs. Skip tabs entirely for simple, single-cut answers (one map,
  one brawler, a yes/no)."""

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


# Properties shared by the top-level answer and each tab (one embed view).
_VIEW_PROPS = {
    "summary": {"type": "string", "description": "The prose answer (<1800 chars)."},
    "title": {"type": "string", "description": "Short headline for the card."},
    "fields": {
        "type": "array",
        "description": "Stat rows. Prefer these over bullet lists.",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short label."},
                "value": {"type": "string", "description": "Compact value."},
                "inline": {"type": "boolean",
                           "description": "true = sit side by side."},
            },
            "required": ["name", "value"],
        },
    },
    "image_url": {"type": "string",
                  "description": "Large banner image (a map's imageUrl)."},
    "thumbnail_url": {"type": "string",
                      "description": "Small corner image (player iconUrl or "
                                     "brawler imageUrl)."},
    "color": {"type": "string", "description": "Hex accent, e.g. '#f5c518'."},
}

# Terminal presentation tool. It is NOT a plugin — it fetches nothing. When the
# model calls it, the loop intercepts the args and turns them into an Answer /
# Discord embed, then stops. Keeping it here (not in plugins/) means it never
# hits plugins.execute and the plugin layer stays purely about data sources.
PRESENT_TOOL = {
    "name": "present_answer",
    "description": (
        "Render the FINAL answer as a rich Discord embed. Call this once, alone, "
        "as your last step. This ends the turn — do not call any data tool in the "
        "same step."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            **_VIEW_PROPS,
            "tabs": {
                "type": "array",
                "description": (
                    "Optional alternate views of the SAME answer, shown as "
                    "clickable buttons (e.g. 'By Mode', 'By Brawler'). The "
                    "top-level view above is the default 'Overview'. At most 3 tabs."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string",
                                  "description": "Button text, 1-2 words."},
                        **_VIEW_PROPS,
                    },
                    "required": ["label", "summary"],
                },
            },
        },
        "required": ["summary"],
    },
}


def _valid_url(u) -> str | None:
    """Only pass through real http(s) URLs — Discord rejects anything else, and
    the model occasionally hallucinates a bare filename or empty string."""
    return u if isinstance(u, str) and u.startswith(("http://", "https://")) else None


def _view_from_args(args: dict, label: str | None = None) -> Answer:
    """Coerce one view's raw args (top-level or a tab) into a validated Answer.
    Does NOT read `tabs` — nesting is handled by the caller so tabs stay flat."""
    raw_fields = args.get("fields") or []
    fields = [
        {"name": str(f["name"])[:256],
         "value": str(f["value"])[:1024],
         "inline": bool(f.get("inline", False))}
        for f in raw_fields
        if isinstance(f, dict) and f.get("name") and f.get("value")
    ][:25]  # Discord caps an embed at 25 fields
    return Answer(
        summary=str(args.get("summary") or "").strip()
        or "I couldn't produce an answer — try rephrasing.",
        title=(str(args["title"])[:256] if args.get("title") else None),
        fields=fields,
        image_url=_valid_url(args.get("image_url")),
        thumbnail_url=_valid_url(args.get("thumbnail_url")),
        color=(str(args["color"]) if args.get("color") else None),
        label=(str(label)[:80] if label else None),
    )


def _build_answer(args: dict) -> Answer:
    """Coerce present_answer's raw tool args into a validated Answer, including
    up to 3 alternate tab views. A tab needs both a label and a summary; malformed
    tabs are skipped. Tabs never nest — a tab's own `tabs`, if any, is ignored."""
    answer = _view_from_args(args)
    for t in (args.get("tabs") or [])[:3]:
        if isinstance(t, dict) and t.get("label") and t.get("summary"):
            answer.tabs.append(_view_from_args(t, label=t["label"]))
    return answer


def _gemini_tool() -> types.Tool:
    """Build the Gemini tool menu from the plugin registry plus the terminal
    present_answer tool. Plugins declare their schema under the Anthropic-style
    `input_schema` key; Gemini wants it under `parameters`, so translate on the
    way through."""
    return types.Tool(function_declarations=[
        {"name": t["name"],
         "description": t["description"],
         "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}
        for t in [*plugins.all_tools(), PRESENT_TOOL]
    ])


async def answer_question(question: str, asker_tag: str | None,
                          mentioned: dict[str, str] | None = None) -> Answer:
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

        # Terminal presentation tool: if the model asked to present, build the
        # embed from its args and stop. It fetches no data, so it never routes
        # to plugins.execute; any sibling calls in the same turn are ignored
        # (the prompt tells the model to call present_answer alone).
        present = next((c for c in calls if c.name == "present_answer"), None)
        if present is not None:
            return _build_answer(dict(present.args or {}))

        if not calls:
            # Model finished with plain prose instead of present_answer — wrap it.
            return Answer(summary=resp.text
                          or "I couldn't produce an answer — try rephrasing.")

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

    return Answer(summary="I got lost in the data — try a more specific question.")
