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


def _player_brawler(battle: dict, tag: str) -> str | None:
    """Find which brawler `tag` played in one battle. Players live either in a
    flat `players` list (showdown) or nested in `teams` (3v3/duo)."""
    tag = _norm(tag)
    groups = battle.get("teams") or [battle.get("players", [])]
    for group in groups:
        for player in group:
            if _norm(player.get("tag", "")) == tag:
                return (player.get("brawler") or {}).get("name")
    return None


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
            _player_brawler(b, tag),
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


def _rate(wins: int, total: int) -> float:
    return round(100 * wins / total, 1) if total else 0.0


def _outcome(mode: str | None, result: str | None, rank: int | None) -> str | None:
    """Normalize a battle to "win"/"loss"/None(draw/unknown).

    Team modes report result directly. Showdown reports placement instead:
    solo (10 players) top 4 is a win; duo/trio (5 teams) top 2 is a win."""
    if result == "victory":
        return "win"
    if result == "defeat":
        return "loss"
    if result == "draw":
        return None
    if rank is not None:  # showdown placement
        win_cutoff = 4 if "solo" in (mode or "").lower() else 2
        return "win" if rank <= win_cutoff else "loss"
    return None


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

    def bucket():
        return {"battles": 0, "wins": 0, "losses": 0, "trophyChange": 0}

    overall = bucket()
    per_mode: dict[str, dict] = {}
    per_brawler: dict[str, dict] = {}

    for mode, brawler, result, rank, trophy in rows:
        outcome = _outcome(mode, result, rank)  # "win" / "loss" / None (draw)
        for b in (overall,
                  per_mode.setdefault(mode or "unknown", bucket()),
                  per_brawler.setdefault(brawler or "unknown", bucket())):
            b["battles"] += 1
            b["trophyChange"] += trophy or 0
            if outcome == "win":
                b["wins"] += 1
            elif outcome == "loss":
                b["losses"] += 1

    def finalize(b):
        b["winRate"] = _rate(b["wins"], b["battles"])
        return b

    return {
        "tag": tag,
        "days": days,
        "battles": overall["battles"],
        "overall": finalize(overall),
        "perMode": {k: finalize(v) for k, v in per_mode.items()},
        "perBrawler": {k: finalize(v) for k, v in per_brawler.items()},
    }


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
