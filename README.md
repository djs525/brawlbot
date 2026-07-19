# BrawlBot 🏆

An AI-powered Brawl Stars analyst that lives in your Discord server. Link your player tag, then ask it anything — your stats, the current meta, best picks for today's maps — and get real answers built from live battle data. No dashboards, just chat.

> Not affiliated with, endorsed, sponsored, or specifically approved by Supercell. For more information see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).

## Features

- **`/link <tag>`** — connect your Brawl Stars player tag to your Discord account
- **`/ask <question>`** — natural-language questions about your profile, brawlers, battle log, history, event rotation, and rankings, answered by an LLM agent with live API data
- Trophy, win-rate, and brawler performance analysis from your recent battles
- Longitudinal history beyond the ~25-battle API window, stored locally in SQLite
- Event rotation and map-aware pick suggestions (via BrawlAPI/Brawlify metadata)
- **Proactive session recaps** — when a linked player finishes a play session, the bot posts a deterministic recap (record, net trophies, brawlers, standout) to your `#bs` channel. No `/ask` needed — the history poller detects the session and pushes.
- **`/recap [hours]`** — post your recent session recap to `#bs` on demand (default 2h window, max 7 days)
- **`/team-comp [map] [days] [ranked_only] [squad_only]`** — best duo/3v3 line-ups you and your linked friends have *actually run*, ranked by real win rate from stored history; also available to `/ask` as the `team_comps` tool
- **Plugin architecture** — new data sources are drop-in files; the agent core never changes
- **Eval harness** (`evals/`) — unit + agent suites that gate prompt/model/data-layer changes; see [evals/README.md](evals/README.md)

## Why this works

The official Supercell API exposes per-player data: full profile, brawler progression (power level, rank, gadgets, star powers, gears), last ~25 battles, club data, event rotation, and regional rankings. That's enough for an LLM agent to answer *personal* questions ("what should I push this season?", "why did I tilt last night?") — not just static meta pages like existing stats sites.

### Scope note

The official Supercell API is public-profile only. It does **not** expose account currencies (Coins, Gems, Power Points, Bling, Credits, Star Points), because that's account-private economy data Supercell has never exposed for any of their games. No third-party API can show these either — BrawlBot is upfront about this constraint.

## Architecture

```
Discord slash command (/ask)
        │  defer → followup pattern (beat Discord's 3-second timer)
        ▼
  Agent loop (agent.py — Gemini with tool use)
        │  builds the tool menu from the plugin registry, runs the loop itself
        ▼
  plugins/
  ├── official_bs.py   # Supercell API: get_player, get_battlelog
  ├── events.py        # event rotation: official first, BrawlAPI fallback
  ├── brawlapi.py      # BrawlAPI: get_map_details (map images, no key)
  ├── brawlers.py      # Supercell catalog: get_brawlers
  ├── rankings.py      # Supercell leaderboards: get_rankings
  ├── history.py       # local longitudinal history: query_history (SQLite)
  ├── teamcomp.py      # team-comp win rates from stored history: team_comps
  └── jsonout.py       # shared JSON serializer (not a plugin — no TOOLS)
        ▼
  SQLite
  ├── brawlbot.db (store.py)   # linked tags, tag normalization
  └── history.db (history.py)  # tracked players + persisted battles
```

Project layout:

```
brawlbot/
├── bot.py        # Discord client, slash commands (defer → followup)
├── agent.py      # LLM agent loop: source-agnostic, never changes when adding data
├── plugins/      # each file = one data source exposing TOOLS + async execute()
├── bs_client.py  # async Supercell API wrapper + slim_* payload trimmers
├── recap.py      # proactive session recaps: session detection + embed (rides the poller)
├── store.py      # SQLite storage (linked tags), tag normalization
├── evals/        # eval harness: unit suite (pure data layer) + agent suite (live Gemini, fixture tools)
├── brawlbot.db   # links DB, created automatically on first run
├── history.db    # tracked players + battle history
└── .env          # secrets (never commit this)
```

### How the agent loop works

1. Build the tool menu by aggregating `TOOLS` from every plugin (`plugins.all_tools()`), translating each plugin's `input_schema` into a Gemini function declaration.
2. Send Gemini the question plus that menu.
3. Gemini replies with either **words** (done — return them) or one-or-more **tool calls** (`resp.function_calls`).
4. Run the calls concurrently via `plugins.execute(...)`, append the results, and call Gemini again.
5. Repeat until Gemini answers, capped at **8** tool-loop turns to bound cost.

The loop also walks a **model fallback chain** (`agent.MODELS`): on a retryable error (overload, rate-limit, 5xx, quota) it drops to the next, cheaper model instead of failing the request.

`agent.py` contains no URLs, tokens, or source names — adding a data source is just dropping a new file into `plugins/` that exposes `TOOLS` (a list of tool defs) and `async def execute(name, tool_input)`.

Data sources:
- **Supercell Brawl Stars API** (`api.brawlstars.com/v1`) — live player/club/battle/rankings/brawler data
- **BrawlAPI / Brawlify** (`api.brawlapi.com/v1`) — static metadata: event rotations, maps (no key required)
- **Google Gemini API** — powers the `/ask` agent

## Setup

Requires Python 3.10+.

```bash
git clone <your-repo-url>
cd brawlbot
pip install discord.py python-dotenv google-genai aiohttp
```

Create a `.env` file:

```
DISCORD_TOKEN=your_discord_bot_token
GEMINI_API_KEY=your_gemini_key
BRAWL_STARS_TOKEN=your_supercell_api_token
```

Where to get them:
- **DISCORD_TOKEN** — [Discord Developer Portal](https://discord.com/developers/applications) → your app → Bot → Reset Token. Also enable the *Message Content* intent.
- **BRAWL_STARS_TOKEN** — [developer.brawlstars.com](https://developer.brawlstars.com). Tokens are **IP-locked** — create a new one if your IP changes. On serverless/dynamic-IP hosts, use a static-egress proxy (e.g. `bsproxy.royaleapi.dev`).
- **GEMINI_API_KEY** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

Add `.env`, `brawlbot.db`, and `history.db` to `.gitignore` before your first commit.

## Run

```bash
python bot.py
```

On startup you should see the plugins load, e.g. `plugins: loaded 'official_bs' with 2 tool(s)`. New slash commands sync in `on_ready`, so adding a command requires a restart.

Then in Discord:

```
/link #YOURTAG
/ask what are my best brawlers?
/ask what should I push on today's maps?
/ask how's my win rate in showdown this month?
/team-comp map:Hard Rock Mine
```

## Evals

Run before shipping any prompt, model, or data-layer change:

```bash
python -m evals.run                  # both suites
python -m evals.run --suite unit     # free, no keys, <1s — data layer only
python -m evals.run --suite agent    # live Gemini, fixture data tools
```

Details in [evals/README.md](evals/README.md).

## Implementation notes & gotchas

- **Discord's 3-second rule** — slash commands must be acknowledged within 3 seconds or Discord shows "The application did not respond." The agent takes 10–20s, so every command uses `await interaction.response.defer()` then `await interaction.followup.send(...)` (15-minute window).
- **Always send a followup** — after a `defer()`, a crash with no followup leaves the user staring at "thinking…" forever; agent calls are wrapped in try/except.
- **2000-character cap** — Discord hard-rejects longer messages; `bot._chunk` splits answers on line boundaries into ≤2000-char messages.
- **Env-load order** — `load_dotenv()` must run before `plugins.load_plugins()` in `bot.py`, since plugins read `os.environ` at import time. Tools are also built at *call* time, not import time, because `agent` is imported before the registry is loaded.
- **Payload trimming** — `bs_client.slim_*` helpers cut fat API payloads down to what the LLM needs. Size capping goes through `plugins/jsonout.dump` (12000-char ceiling): it drops whole trailing list items and says so, instead of a raw character slice that would hand the model malformed JSON to hallucinate from.
- **Showdown scoring** — showdown reports placement, not victory/defeat. `history.py` scores solo (top 4) and duo/trio (top 2) as wins when aggregating win rates.
- **Battle log retention** — the API only keeps ~25 battles. A background poller (`bot.poll_history`, every 20 min) snapshots every tracked player's battlelog into `history.db`, so longer windows can be queried. Players are enrolled by `/link` (`history.track`); `query_history` also records the caller's battles on demand. `record_battles` dedupes on `(tag, battle_time)`, so overlapping snapshots are harmless.
- **Team-comp bookkeeping** — each stored battle also records `team` (the brawler line-up on the player's own team, sorted so a comp is order-independent) and `team_tags` (who played it). A squad game shows up once per tracked friend, so `teamcomp.best_comps` dedupes on `(battle_time, comp, result)` before tallying; comps under 3 games are hidden as noise. Solo modes have no team and never become comps.

## Roadmap

- [x] **Phase 0** — bot skeleton, `/ping`
- [x] **Phase 1** — `/ask` wired to the agent (defer → followup)
- [x] **Phase 2** — plugin registry; BrawlAPI plugin (event rotation, maps)
- [x] **Phase 3** — `/link` tag storage (SQLite); brawler/rankings/history plugins; long-answer splitting
- [x] **Phase 4** — longitudinal history
  - [x] persist battles to SQLite + `query_history` aggregation
  - [x] background poller (`bot.poll_history`, every 20 min) snapshots **all tracked** tags
  - [x] auto-track on `/link` (`history.track`) to seed the tracked set
  - _rate limiting / cost caps intentionally skipped — small trusted server on the owner's key_
- [x] **Cleanup** — `official_bs.py` folded onto `bs_client` (single Supercell token, shared slimming)
- [x] **Eval harness** — `evals/` unit + agent suites (`python -m evals.run`); agent cases hit live Gemini with fixture data tools, so failures are prompt/model regressions, never upstream flake
- [x] **Team comps** — `/team-comp` command + `team_comps` LLM tool: per-map duo/3v3 line-up win rates pooled across all linked players, with ranked-only and squad-only filters
- [ ] **Phase 5** — conversational threads & rich output
  - [x] rich embeds for structured output — the agent finishes by calling a
    terminal `present_answer` tool (defined in `agent.py`, intercepted by the
    loop) that returns a structured `Answer` (title, summary, stat fields, image,
    thumbnail, color); `bot._build_embed` renders it as a Discord embed. Images
    flow from tool data: map `imageUrl`, player `iconUrl` (`slim_player`), and
    brawler `imageUrl` (BrawlAPI) — the model only ever uses URLs it saw in tool
    results, never invented ones. Falls back to plain-text chunks if the model
    skips the tool or Discord rejects the embed.
  - [ ] multi-turn follow-ups in a Discord thread (agent is stateless today)

## License

MIT (or your choice). Fan-made project; all Brawl Stars content and materials are trademarks and copyrights of Supercell.
