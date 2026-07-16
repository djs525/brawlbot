"""Shared JSON serializer for plugin tool results.

Not a plugin — no TOOLS, so the registry skips it.

Every plugin used to return `json.dumps(payload)[:12000]`. A character slice
cuts mid-object and hands the model malformed JSON, which it then answers from
anyway (inventing whatever the truncated tail should have said). Both the
brawler catalog and the rankings board had already outgrown 12000 and were
being corrupted on every call.

dump() keeps the same size ceiling but only ever emits parseable JSON: lists
lose whole trailing items and say so, so the model knows it's seeing a subset
instead of guessing at one.
"""

import json

LIMIT = 12000


def _wrap(items: list, total: int) -> dict:
    return {"items": items,
            "truncated": f"showing {len(items)} of {total} — payload capped at "
                         f"{LIMIT} chars; narrow the query for the rest"}


def dump(payload, limit: int = LIMIT) -> str:
    """Serialize `payload` to JSON that is always under `limit` and always valid.

    Fits as much as possible: a list is trimmed to the most whole trailing
    items that fit, wrapped with a truncation note. A non-list too big to fit
    returns an explicit error rather than a silently mangled object.
    """
    s = json.dumps(payload)
    if len(s) <= limit:
        return s

    if isinstance(payload, list):
        # Binary search the largest prefix whose wrapped form still fits.
        lo, hi = 0, len(payload)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(_wrap(payload[:mid], len(payload)))) <= limit:
                lo = mid
            else:
                hi = mid - 1
        return json.dumps(_wrap(payload[:lo], len(payload)))

    return json.dumps({"error": f"payload is {len(s)} chars, over the {limit} "
                                f"limit, and cannot be trimmed safely — "
                                f"request a narrower slice of this data"})
