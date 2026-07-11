"""SQLite backed link store: Discord User ID -> Brawl Stars Tag"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "brawlbot.db"

def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON") #enforce foreign key constraints
    return conn

def init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS links (
                discord_id TEXT PRIMARY KEY,
                player_tag TEXT NOT NULL,
                linked_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

def set_link(discord_id:str, player_tag:str)-> None:
    #Upsert - re-linking overwrites. Fine for friend group
    #meaning? If user already linked, this will update their link to the new player tag.
    with _conn() as c:
        c.execute("""
            INSERT INTO links (discord_id, player_tag) VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                player_tag = excluded.player_tag,
                linked_at  = datetime('now')
        """, (discord_id, player_tag))

def get_tag(discord_id:str) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT player_tag FROM links WHERE discord_id = ?", (discord_id,)
        ).fetchone()
    return row[0] if row else None


VALID_TAG_CHARS = set("0289PYLQGRJCUV")  # Supercell's alphabet
def normalize_tag(raw: str) -> str:
    """Return a canonical '#ABC123' tag, or raise ValueError."""
    t = raw.strip().upper().lstrip("#")
    if not (3 <= len(t) <= 15):
        raise ValueError("Tag length looks wrong — should be 3–15 characters.")
    bad = [ch for ch in t if ch not in VALID_TAG_CHARS]
    if bad:
        hint = " (did you type 'O' instead of '0'?)" if "O" in bad else ""
        raise ValueError(f"Invalid characters in tag: {''.join(bad)}{hint}")
    return f"#{t}"