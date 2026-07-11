# BrawlBot 🏆

An AI-powered Brawl Stars analyst that lives in your Discord server. Link your player tag, then ask it anything — your stats, the current meta, best picks for today's maps — and get real answers built from live battle data. No dashboards, just chat.

> Not affiliated with, endorsed, sponsored, or specifically approved by Supercell. For more information see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).

## Features

- **`/link <tag>`** — connect your Brawl Stars player tag to your Discord account
- **`/ask <question>`** — natural-language questions about your profile, brawlers, battle log, club, and the meta, answered by an LLM agent with live API data
- Trophy, win-rate, and brawler performance analysis from your recent battles
- Event rotation and map-aware pick suggestions (via Brawlify metadata)

### Scope note

The official Supercell API is public-profile only: it exposes brawlers, trophies, victories, club, and battle history. It does **not** expose account currencies (Coins, Gems, Power Points, Bling, etc.), so BrawlBot can't show those — no third-party API can, either.

## Architecture

Flat, single-folder layout:

```
brawlbot/
├── bot.py        # Discord client, slash commands (defer → followup pattern)
├── agent.py      # LLM agent: answers questions using live API data
├── store.py      # SQLite storage (linked tags), tag normalization
├── brawlbot.db   # created automatically on first run
└── .env          # secrets (never commit this)
```

Data sources:
- **Supercell Brawl Stars API** (`api.brawlstars.com`) — live player/club/battle data
- **Brawlify API** (`api.brawlify.com`) — static metadata: brawler info, event rotations, maps
- **Anthropic API** — powers the `/ask` agent

## Setup

Requires Python 3.10+.

```bash
git clone <your-repo-url>
cd brawlbot
pip install discord.py python-dotenv anthropic requests
```

Create a `.env` file:

```
DISCORD_TOKEN=your_discord_bot_token
ANTHROPIC_API_KEY=your_anthropic_key
BRAWL_API_TOKEN=your_supercell_api_token
```

Where to get them:
- **DISCORD_TOKEN** — [Discord Developer Portal](https://discord.com/developers/applications) → your app → Bot → Reset Token. Also enable the *Message Content* intent.
- **BRAWL_API_TOKEN** — [developer.brawlstars.com](https://developer.brawlstars.com) (tokens are IP-locked; create a new one if your IP changes)
- **ANTHROPIC_API_KEY** — [console.anthropic.com](https://console.anthropic.com)

Add `.env` and `brawlbot.db` to `.gitignore` before your first commit.

## Run

```bash
python bot.py
```

On startup the bot syncs its slash commands (global sync can take up to an hour to propagate the first time). Then in Discord:

```
/link #YOURTAG
/ask what should I push on today's maps?
```

## Notes for contributors

- Slash command handlers must `await interaction.response.defer()` within 3 seconds, then reply with `interaction.followup.send(...)`. The agent takes 10–20s, so this is non-negotiable.
- Replies are chunked to respect Discord's 2000-character message cap.
- `discord.NotFound` (error 10062) on defer means the interaction token expired before the ACK — the handler drops it gracefully.
- New slash commands require a bot restart (sync runs in `on_ready`).

## Roadmap

- Trophy progression tracking over time (snapshot history in the DB)
- Club activity monitoring and rank-up notifications
- Plugin system (`plugins/` subfolder) for per-feature modules
- Thread-based conversations for follow-up questions