"""Plugin: long-window battle history (SQLite, history.db).

The official battlelog only returns ~25 recent battles. This plugin keeps a
rolling local record so we can answer questions over longer windows.

On every query_history call we:
  1. mark the tag as tracked (so it keeps accumulating),
  2. snapshot the player's current battlelog into the `battles` table
     (PRIMARY KEY (tag, battle_time) dedupes re-seen battles),
  3. aggregate everything stored within the last N days.

Schema (pre-existing history.db):
  tracked(tag, added_at)
  battles(tag, battle_time, mode, map, brawler, result, rank, trophy_change, type)

`type` (added by _migrate) distinguishes trophy-ladder games from competitive
Ranked — see bs_client.is_ranked. Rows stored before the migration have
type=NULL and count as trophy games.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bs_client

DB_PATH = Path(__file__).parent.parent / "history.db"

# API battleTime / our stored battle_time format: "20240115T120000.000Z".
# Fixed width, so lexicographic string compare == chronological compare.
_TIME_FMT = "%Y%m%dT%H%M%S.000Z"

TOOLS = [
    {
        "name": "query_history",
        "description": (
            "Aggregate a player's battle history over the last N days (default "
            "30) from locally stored records: overall win rate, net trophy "
            "change, and per-mode and per-brawler breakdowns. Use for "
            "longer-window trends than the ~25-battle battlelog can show."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Player tag, e.g. #ABC123"},
                "days": {
                    "type": "integer",
                    "description": "Look-back window in days (default 30).",
                },
            },
            "required": ["tag"],
        },
    },
]


def _norm(tag: str) -> str:
    tag = (tag or "").strip().upper()
    return tag if tag.startswith("#") else "#" + tag


def _conn():
    return sqlite3.connect(DB_PATH)


def _migrate() -> None:
    """Bring an old battles table up to the current schema by adding any columns
    it predates. Each ALTER is idempotent — a second run hits 'duplicate column
    name' and is ignored. Safe to call every import.

    `team` (JSON brawler list on THIS player's team) and `team_tags` (JSON of
    those teammates' player tags) power the team-comp feature — the first says
    WHAT was played, the second WHO played it, so squad games (2+ linked friends
    on one team) are distinguishable from solo-queue-with-randoms. Rows stored
    before this migration have team/team_tags=NULL and are invisible to comp
    queries until the player is re-snapshotted.

    `team_pairs` (JSON list of {tag, brawler}) pairs WHO with WHAT directly —
    team/team_tags are each sorted independently, so their indices don't line
    up. Powers per-brawler attribution ("who played what") for linked players."""
    for col in ("type TEXT", "team TEXT", "team_tags TEXT", "team_pairs TEXT"):
        try:
            with _conn() as c:
                c.execute(f"ALTER TABLE battles ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # column already exists, or table not created yet (first run)


def track(tag: str) -> None:
    """Add a tag to the tracked set so the background poller snapshots it.
    Idempotent — re-tracking an existing tag is a no-op."""
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO tracked (tag, added_at) VALUES (?, ?)",
                  (_norm(tag), datetime.now(timezone.utc).strftime(_TIME_FMT)))


def tracked_tags() -> list[str]:
    """Every tag the poller should snapshot."""
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT tag FROM tracked")]


def record_battles(tag: str, battlelog: dict) -> int:
    """Upsert a raw battlelog (bs_client.get_battlelog output) into history.
    Returns the number of newly stored battles."""
    tag = _norm(tag)
    rows = []
    for item in battlelog.get("items", []):
        b = item.get("battle", {})
        ev = item.get("event", {})
        team = bs_client.player_team(b, tag)          # brawlers (WHAT), None for solo
        team_tags = bs_client.player_team_tags(b, tag)  # teammate tags (WHO)
        team_pairs = bs_client.player_team_pairs(b, tag)  # WHO played WHAT, paired
        rows.append((
            tag,
            item.get("battleTime"),
            b.get("mode") or ev.get("mode"),
            ev.get("map"),
            bs_client.player_brawler(b, tag),
            b.get("result"),
            b.get("rank"),
            b.get("trophyChange"),
            b.get("type"),
            json.dumps(team) if team else None,
            json.dumps(team_tags) if team_tags else None,
            json.dumps(team_pairs) if team_pairs else None,
        ))
    if not rows:
        return 0
    with _conn() as c:
        before = c.total_changes
        c.execute("INSERT OR IGNORE INTO tracked (tag, added_at) VALUES (?, ?)",
                  (tag, datetime.now(timezone.utc).strftime(_TIME_FMT)))
        c.executemany(
            "INSERT OR IGNORE INTO battles "
            "(tag, battle_time, mode, map, brawler, result, rank, trophy_change, type, team, team_tags, team_pairs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        new = c.total_changes - before
        # Backfill rows stored before the team/team_tags/team_pairs (or type)
        # columns existed: INSERT OR IGNORE skips battles already seen, so
        # without this a battle first recorded by older code would never gain
        # its comp even while the battlelog still carries the data.
        c.executemany(
            "UPDATE battles SET team = ?, team_tags = ?, team_pairs = ?, type = COALESCE(type, ?) "
            "WHERE tag = ? AND battle_time = ? AND team IS NULL",
            [(r[9], r[10], r[11], r[8], r[0], r[1]) for r in rows if r[9]],
        )
        return new


def query_history(tag: str, days: int = 30) -> dict:
    tag = _norm(tag)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(_TIME_FMT)
    with _conn() as c:
        rows = c.execute(
            "SELECT mode, brawler, result, rank, trophy_change, type FROM battles "
            "WHERE tag = ? AND battle_time >= ?",
            (tag, cutoff),
        ).fetchall()

    if not rows:
        return {"tag": tag, "days": days, "battles": 0,
                "note": "No stored history in this window yet — history builds "
                        "up as this player is queried over time."}

    summary = bs_client.summarize_battles([
        {"mode": mode, "brawler": brawler, "result": result,
         "rank": rank, "trophyChange": trophy, "type": btype}
        for mode, brawler, result, rank, trophy, btype in rows
    ])
    return {"tag": tag, "days": days,
            "battles": summary["overall"]["battles"], **summary}


_migrate()  # bring an old battles table up to the current schema on import


async def execute(name: str, tool_input: dict) -> str:
    if name != "query_history":
        return f"ERROR: history got unknown tool '{name}'"
    tag = tool_input["tag"]
    days = tool_input.get("days") or 30
    # Refresh from the live battlelog so history stays current, then aggregate.
    # A fetch failure shouldn't sink the query — we can still report stored rows.
    try:
        record_battles(tag, await bs_client.get_battlelog(tag))
    except Exception as e:
        print(f"[history] battlelog refresh failed for {tag}: {e}")
    return json.dumps(query_history(tag, days))
