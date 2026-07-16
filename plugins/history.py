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
  battles(tag, battle_time, mode, map, brawler, result, rank, trophy_change)
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
        rows.append((
            tag,
            item.get("battleTime"),
            b.get("mode") or ev.get("mode"),
            ev.get("map"),
            bs_client.player_brawler(b, tag),
            b.get("result"),
            b.get("rank"),
            b.get("trophyChange"),
        ))
    if not rows:
        return 0
    with _conn() as c:
        before = c.total_changes
        c.execute("INSERT OR IGNORE INTO tracked (tag, added_at) VALUES (?, ?)",
                  (tag, datetime.now(timezone.utc).strftime(_TIME_FMT)))
        c.executemany(
            "INSERT OR IGNORE INTO battles "
            "(tag, battle_time, mode, map, brawler, result, rank, trophy_change) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return c.total_changes - before


def query_history(tag: str, days: int = 30) -> dict:
    tag = _norm(tag)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(_TIME_FMT)
    with _conn() as c:
        rows = c.execute(
            "SELECT mode, brawler, result, rank, trophy_change FROM battles "
            "WHERE tag = ? AND battle_time >= ?",
            (tag, cutoff),
        ).fetchall()

    if not rows:
        return {"tag": tag, "days": days, "battles": 0,
                "note": "No stored history in this window yet — history builds "
                        "up as this player is queried over time."}

    summary = bs_client.summarize_battles([
        {"mode": mode, "brawler": brawler, "result": result,
         "rank": rank, "trophyChange": trophy}
        for mode, brawler, result, rank, trophy in rows
    ])
    return {"tag": tag, "days": days,
            "battles": summary["overall"]["battles"], **summary}


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
