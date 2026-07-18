"""Proactive session recaps.

The history poller (bot.py) already snapshots every tracked player's battlelog
every POLL_MINUTES. This module rides that loop: when a LINKED player has just
finished a play session, it builds a recap embed and the bot posts it to the
configured channel (default #bs).

"Finished a session" = the player has un-recapped battles inside the last
RECAP_WINDOW_HOURS, and the most recent poll tick added no new battles (they've
gone quiet). That fires exactly one recap per burst of play, ~POLL_MINUTES after
the last game. A per-tag watermark (recaps table) dedupes so a session is never
recapped twice, and the 2-hour window keeps first-run from recapping ancient
stored battles.

Recap content is fully deterministic — computed from stored rows via
bs_client.summarize_battles — so it never hallucinates.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

import bs_client

DB_PATH = Path(__file__).parent / "history.db"

# Same fixed-width format as stored battle_time, so string compare == time compare.
_TIME_FMT = "%Y%m%dT%H%M%S.000Z"

# Only battles newer than this count toward a "current session". Also the guard
# that stops first-run from recapping the whole back-catalogue of stored games.
RECAP_WINDOW_HOURS = 2


def _conn():
    return sqlite3.connect(DB_PATH)


def init() -> None:
    """Create the recap watermark table if missing. Safe to call every startup."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS recaps (
                tag              TEXT PRIMARY KEY,
                last_battle_time TEXT NOT NULL
            )
        """)


def _watermark(tag: str) -> str:
    """Newest battle_time already recapped for this tag, or '' if never. Empty
    string sorts before any real timestamp, so a first recap sees everything."""
    with _conn() as c:
        row = c.execute(
            "SELECT last_battle_time FROM recaps WHERE tag = ?", (tag,)
        ).fetchone()
    return row[0] if row else ""


def _advance(tag: str, newest: str) -> None:
    with _conn() as c:
        c.execute("""
            INSERT INTO recaps (tag, last_battle_time) VALUES (?, ?)
            ON CONFLICT(tag) DO UPDATE SET last_battle_time = excluded.last_battle_time
        """, (tag, newest))


def _rows(tag: str, cutoff: str, watermark: str) -> list[dict]:
    """Stored battles for `tag` with battle_time in (watermark, ∞) and ≥ cutoff,
    oldest→newest. Pass watermark='' to ignore the recap watermark."""
    with _conn() as c:
        rows = c.execute(
            "SELECT battle_time, mode, brawler, result, rank, trophy_change, type "
            "FROM battles WHERE tag = ? AND battle_time >= ? AND battle_time > ? "
            "ORDER BY battle_time",
            (tag, cutoff, watermark),
        ).fetchall()
    return [
        {"battle_time": bt, "mode": mode, "brawler": brawler,
         "result": result, "rank": rank, "trophyChange": trophy, "type": btype}
        for bt, mode, brawler, result, rank, trophy, btype in rows
    ]


def pending_session(tag: str) -> list[dict]:
    """Un-recapped battles for `tag` inside the recent window, oldest→newest.
    Respects the watermark, so already-recapped games are excluded. Empty when
    there's nothing new to recap. Used by the auto poller."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=RECAP_WINDOW_HOURS)).strftime(_TIME_FMT)
    return _rows(tag, cutoff, _watermark(tag))


def recent_session(tag: str, hours: int = RECAP_WINDOW_HOURS) -> list[dict]:
    """All stored battles for `tag` in the last `hours`, oldest→newest —
    IGNORING the watermark. For the on-demand /recap command, so a player can
    re-view a session the auto poller already recapped. No side effects."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=hours)).strftime(_TIME_FMT)
    return _rows(tag, cutoff, "")


def _pretty_mode(mode: str | None) -> str:
    """'gemGrab' / 'soloShowdown' -> 'Gem Grab' / 'Solo Showdown'."""
    if not mode:
        return "Unknown"
    import re
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", mode)
    return spaced[:1].upper() + spaced[1:]


def _record(o: dict) -> str:
    """'5W · 2L · 1D  (62.5%)' from an overall bucket."""
    draws = o["battles"] - o["wins"] - o["losses"]
    rec = f"{o['wins']}W · {o['losses']}L" + (f" · {draws}D" if draws else "")
    return f"{rec}  ({o['winRate']}%)"


def _brawler_lines(sub: dict, show_trophies: bool, limit: int = 6) -> list[str]:
    """Per-brawler lines, most-played first, with W/L (and trophy swing for
    trophy games — Ranked has no trophyChange so we omit it there)."""
    brawlers = sorted(sub["perBrawler"].items(), key=lambda kv: -kv[1]["battles"])
    lines = []
    for name, v in brawlers[:limit]:
        line = f"**{name or 'Unknown'}** — {v['wins']}W/{v['losses']}L ({v['winRate']}%)"
        if show_trophies:
            line += f", {v['trophyChange']:+d}🏆"
        lines.append(line)
    return lines


def _standout(sub: dict) -> tuple[str, dict] | None:
    """Best win rate among brawlers with ≥2 games, or None."""
    contenders = [(n, v) for n, v in sub["perBrawler"].items() if v["battles"] >= 2]
    if not contenders:
        return None
    return max(contenders, key=lambda kv: (kv[1]["winRate"], kv[1]["battles"]))


def _add_section(embed: discord.Embed, title: str, sub: dict,
                 show_trophies: bool) -> None:
    """Append one game-type block (Trophy or Ranked) to the recap embed."""
    o = sub["overall"]
    header = _record(o)
    if show_trophies:
        net = o["trophyChange"]
        arrow = "📈" if net > 0 else "📉" if net < 0 else "➖"
        header += f"  ·  {arrow} {net:+d}🏆"
    embed.add_field(name=f"{title} — {o['battles']} game"
                         f"{'s' if o['battles'] != 1 else ''}",
                    value=header, inline=False)

    modes = sorted(sub["perMode"].items(), key=lambda kv: -kv[1]["battles"])
    mode_line = " · ".join(f"{_pretty_mode(m)} ×{v['battles']}" for m, v in modes[:5])
    if mode_line:
        embed.add_field(name="Modes", value=mode_line, inline=False)

    b_lines = _brawler_lines(sub, show_trophies)
    if b_lines:
        embed.add_field(name="Brawlers", value="\n".join(b_lines), inline=False)


def build_embed(tag: str, rows: list[dict], player_name: str | None,
                discord_id: str | None) -> discord.Embed:
    """Deterministic session recap embed from the session's battle rows.

    Trophy-ladder and competitive-Ranked games get their own sections — their
    win rates don't belong in the same bucket, and Ranked has no trophy swing."""
    s = bs_client.summarize_battles(rows)
    total = s["overall"]["battles"]
    trophy, ranked = s["trophy"], s["ranked"]
    tn, rn = trophy["overall"]["battles"], ranked["overall"]["battles"]

    # Session colour tracks the trophy swing (Ranked contributes none). Neutral
    # blue when there were no trophy games to move a number.
    net = trophy["overall"]["trophyChange"]
    if tn and net > 0:
        color = 0x57F287
    elif tn and net < 0:
        color = 0xED4245
    else:
        color = 0x4DA3FF

    who = player_name or tag
    if tn and rn:
        breakdown = f" — **{tn}** trophy · **{rn}** ranked"
    else:
        breakdown = ""
    embed = discord.Embed(
        title=f"🎮 Session recap — {who}",
        description=f"`{tag}` just wrapped a session of **{total}** "
                    f"battle{'s' if total != 1 else ''}{breakdown}.",
        color=color,
    )

    if tn:
        _add_section(embed, "🏆 Trophy", trophy, show_trophies=True)
    if rn:
        _add_section(embed, "⚔️ Ranked", ranked, show_trophies=False)

    # Standout brawler, drawn from whichever pool the player mostly played.
    star = _standout(trophy if tn >= rn else ranked)
    if star:
        embed.add_field(
            name="⭐ Standout",
            value=f"{star[0]} carried at {star[1]['winRate']}% "
                  f"over {star[1]['battles']} games.",
            inline=False)

    embed.set_footer(text="BrawlBot • session recap • not affiliated with Supercell")
    return embed


async def render(tag: str, rows: list[dict],
                 discord_id: str | None) -> discord.Embed:
    """Fetch the player's display name (best-effort) and build the recap embed.
    Pure — no watermark side effects. Shared by the auto poller and /recap."""
    player_name = None
    try:
        player_name = (await bs_client.get_player(tag)).get("name")
    except Exception as e:
        print(f"[recap] get_player failed for {tag}: {e}")
    return build_embed(tag, rows, player_name, discord_id)


async def maybe_recap(tag: str, new_this_tick: int,
                      discord_id: str | None) -> discord.Embed | None:
    """Decide whether `tag` just finished a session and, if so, build the recap
    embed and advance the watermark. Returns the embed to post, or None.

    Caller passes new_this_tick (battles the latest snapshot added) so we only
    fire on a quiet tick — i.e. once the player has stopped playing."""
    if new_this_tick > 0:
        return None  # still playing; wait for a quiet tick before recapping
    rows = pending_session(tag)
    if not rows:
        return None
    embed = await render(tag, rows, discord_id)
    _advance(tag, rows[-1]["battle_time"])  # newest included battle
    return embed
