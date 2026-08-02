"""Plugin: team-composition win rates from stored history.

Reads the `team` column on `battles` (a JSON list of the brawlers on the tracked
player's own team — see bs_client.player_team, written by history.record_battles).
Groups battles by (map, comp) and reports how each line-up actually performed.

Two entry points:
  - best_comps(): the aggregation, used directly by the /team-comp slash command.
  - the `team_comps` LLM tool, so /ask can answer comp questions in prose too.

Only battles with a real team (duo / 3v3) carry a comp; solo modes are NULL and
never appear here. A battle shared by several tracked friends is stored once per
friend, so best_comps() dedupes on (battle_time, comp, result) before tallying —
otherwise a squad game would be counted three times.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bs_client
import store

DB_PATH = Path(__file__).parent.parent / "history.db"
_TIME_FMT = "%Y%m%dT%H%M%S.000Z"  # matches history.py's stored battle_time

# Below this many games a win rate is noise (one lucky game reads as 100%).
MIN_GAMES = 3


def _norm(tag: str) -> str:
    tag = (tag or "").strip().upper()
    return tag if tag.startswith("#") else "#" + tag


def _conn():
    return sqlite3.connect(DB_PATH)


def _fuzzy_map(want: str, maps: list[str]) -> str | None:
    """Resolve a user-typed map name to a stored one. Exact (case-insensitive)
    first, then substring either direction ('hard rock' -> 'Hard Rock Mine'),
    so people don't have to type the map perfectly. None if nothing matches."""
    want = (want or "").strip().lower()
    if not want:
        return None
    for m in maps:
        if m.lower() == want:
            return m
    for m in maps:
        ml = m.lower()
        if want in ml or ml in want:
            return m
    return None


def best_comps(tags: list[str], map_name: str | None = None, days: int = 30,
               min_games: int = MIN_GAMES, ranked_only: bool = False,
               together_only: bool = False) -> dict:
    """Rank the team comps a set of players actually ran, by win rate.

    tags         : players to pool (the asker + their linked friends). Pooled so a
                   squad's shared game counts once (deduped on battle_time+comp).
    map_name     : filter to one map (fuzzy-matched). None = every map.
    days         : look-back window.
    ranked_only  : restrict to competitive Ranked (type=soloRanked) games.
    together_only: only count games where 2+ of `tags` were on the SAME team
                   (real squad games), using the stored teammate tags. False
                   counts every team game a pooled player played, randoms included.

    Returns {map|maps, days, comps:[...], note?} where each comp is
    {brawlers, games, wins, losses, winRate}, sorted by win rate then games. When
    no map is given, comps span all maps and carry their own `map`. A comp also
    carries `playedBy` ({brawler: discord_id}) for any brawler whose player tag
    is linked in our DB — never invented for unrecognized tags, so it only ever
    covers a subset of `brawlers`."""
    tags = [_norm(t) for t in tags if t]
    if not tags:
        return {"comps": [], "note": "No player tags to look at."}
    squad = set(tags)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(_TIME_FMT)
    placeholders = ",".join("?" for _ in tags)
    sql = (f"SELECT DISTINCT battle_time, map, mode, result, rank, team, team_tags, team_pairs "
           f"FROM battles WHERE tag IN ({placeholders}) "
           f"AND battle_time >= ? AND team IS NOT NULL")
    params = [*tags, cutoff]
    if ranked_only:
        sql += " AND type = 'soloRanked'"
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()

    if not rows:
        return {"map": map_name, "days": days, "comps": [],
                "note": "No team games with stored comps in this window yet — "
                        "comps build up as tracked players are snapshotted."}

    all_maps = sorted({r[1] for r in rows if r[1]})
    resolved = _fuzzy_map(map_name, all_maps) if map_name else None
    if map_name and resolved is None:
        return {"map": map_name, "days": days, "comps": [],
                "note": f"No stored team games on a map matching '{map_name}'. "
                        f"Maps seen: {', '.join(all_maps) or 'none'}."}

    # tag -> discord_id for every linked player, so a comp can name WHO played
    # WHAT whenever a teammate happens to be in our DB — never invented for
    # tags we don't recognize.
    linked = store.all_links()

    # Dedupe a squad's shared game (same battle_time + comp + result across the
    # pooled tags collapses to one). Key by map too so identical comps on
    # different maps stay separate when we're not filtering to one map.
    seen: set[tuple] = set()
    buckets: dict[tuple, dict] = {}
    for battle_time, bmap, mode, result, rank, team_json, tags_json, pairs_json in rows:
        if resolved and bmap != resolved:
            continue
        try:
            brawlers = tuple(json.loads(team_json))
        except (TypeError, ValueError):
            continue
        if together_only:
            try:
                mates = set(json.loads(tags_json)) if tags_json else set()
            except (TypeError, ValueError):
                mates = set()
            if len(mates & squad) < 2:
                continue  # not a real squad game — solo-queued with randoms
        key = (battle_time, brawlers, result, rank)
        if key in seen:
            continue
        seen.add(key)

        bucket_key = brawlers if resolved else (bmap, brawlers)
        b = buckets.setdefault(bucket_key, {"map": bmap, "mode": mode,
                                            "brawlers": list(brawlers),
                                            "games": 0, "wins": 0, "losses": 0,
                                            "_playedBy": {}})
        b["games"] += 1
        w = bs_client.outcome(mode, result, rank)
        if w == "win":
            b["wins"] += 1
        elif w == "loss":
            b["losses"] += 1

        try:
            pairs = json.loads(pairs_json) if pairs_json else []
        except (TypeError, ValueError):
            pairs = []
        for p in pairs:
            did = linked.get(_norm(p.get("tag", "")))
            brawler = p.get("brawler")
            if not did or not brawler:
                continue  # only attribute tags we actually recognize
            counts = b["_playedBy"].setdefault(brawler, {})
            counts[did] = counts.get(did, 0) + 1

    comps = [b for b in buckets.values() if b["games"] >= min_games]
    for b in comps:
        b["winRate"] = round(100 * b["wins"] / b["games"], 1) if b["games"] else 0.0
        if resolved:
            b.pop("map", None)  # redundant when every comp is on the same map
        played_by = b.pop("_playedBy")
        if played_by:
            # Most frequent linked player per brawler across this comp's games.
            b["playedBy"] = {brawler: max(counts, key=counts.get)
                             for brawler, counts in played_by.items()}
    comps.sort(key=lambda b: (b["winRate"], b["games"]), reverse=True)

    out = {"days": days, "minGames": min_games, "rankedOnly": ranked_only,
           "togetherOnly": together_only, "comps": comps}
    if resolved:
        out["map"] = resolved
        # One map = one mode; hoist it so the map filter's output names it once.
        if comps:
            out["mode"] = comps[0].get("mode")
        for b in comps:
            b.pop("mode", None)
    else:
        out["maps"] = all_maps
    if not comps:
        out["note"] = (f"No comp reached the {min_games}-game minimum in this "
                       f"window — play more games or widen the window.")
    return out


# ---------------------------------------------------------------- LLM tool face

TOOLS = [
    {
        "name": "team_comps",
        "description": (
            "Rank the team compositions (duo/3v3 line-ups) a player and their "
            "linked friends actually ran, by real win rate, from stored history. "
            "Optionally filter to one map. Use for 'best comp on <map>', 'what "
            "team should we run', or 'our best 3v3 line-ups' questions. Only "
            "reflects games actually played by tracked players — never invents a "
            "comp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Player tag, e.g. #ABC123."},
                "map": {"type": "string",
                        "description": "Map name to filter to (fuzzy-matched). "
                                       "Omit for best comps across every map."},
                "days": {"type": "integer",
                         "description": "Look-back window in days (default 30)."},
                "ranked_only": {"type": "boolean",
                                "description": "Only competitive Ranked games."},
                "together_only": {"type": "boolean",
                                  "description": "Only games where 2+ linked "
                                                 "friends were on the same team "
                                                 "(real squad games)."},
            },
            "required": ["tag"],
        },
    },
]


async def execute(name: str, tool_input: dict) -> str:
    if name != "team_comps":
        return f"ERROR: teamcomp got unknown tool '{name}'"
    tag = tool_input.get("tag")
    if not tag:
        return json.dumps({"comps": [], "note": "No tag provided."})
    # Refresh from the live battlelog so the newest games count, then aggregate.
    try:
        from plugins import history
        history.record_battles(tag, await bs_client.get_battlelog(tag))
    except Exception as e:
        print(f"[teamcomp] battlelog refresh failed for {tag}: {e}")
    ranked_only = bool(tool_input.get("ranked_only"))
    return json.dumps(best_comps(
        [tag],
        map_name=tool_input.get("map"),
        days=tool_input.get("days") or 30,
        min_games=1 if ranked_only else MIN_GAMES,
        ranked_only=ranked_only,
        together_only=bool(tool_input.get("together_only")),
    ))
