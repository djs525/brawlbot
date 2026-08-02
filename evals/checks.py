"""Check library for agent evals.

A check is a callable taking a CaseContext and returning a CheckResult. All
checks are pure string/trace assertions — deterministic, no LLM grading, so a
red result is always a real regression (or a fixture bug), never grader noise.
"""

import re
from dataclasses import dataclass, field


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CaseContext:
    answer: object            # agent.Answer
    trace: list = field(default_factory=list)
    served: str = ""          # concatenation of every served tool payload

    @property
    def text(self) -> str:
        """Every user-visible string in the answer: summary, title, fields,
        and all tab views. What the hallucination checks scan."""
        parts = []

        def add_view(v):
            parts.extend([v.summary or "", v.title or ""])
            for f in v.fields:
                parts.extend([f.get("name", ""), f.get("value", "")])

        add_view(self.answer)
        for tab in getattr(self.answer, "tabs", []):
            add_view(tab)
        return "\n".join(p for p in parts if p)


# Distinctive brawler names safe to police with word-boundary matching.
# Ambiguous English words (MAX, GENE, AMBER, BULL, ASH, SAM, EVE, TICK, STU,
# GALE, GRAY, BERRY, KIT, BO, LOU, MEG, HANK, PAM, BEA, CARL, JANET, OTIS,
# DOUG, CHUCK, CHARLIE, LILY, LOLA, PENNY, ROSA, FRANK, BUZZ, FANG, SURGE,
# SANDY, WILLOW, PEARL, BONNIE) are excluded — they'd false-positive on
# ordinary prose. This list only needs to CATCH hallucinations, not be
# exhaustive.
POLICED_BRAWLERS = [
    "SHELLY", "NITA", "COLT", "BROCK", "DYNAMIKE", "8-BIT", "EMZ",
    "EL PRIMO", "BARLEY", "POCO", "RICO", "DARRYL", "JACKY", "GUS",
    "PIPER", "BIBI", "NANI", "EDGAR", "GRIFF", "GROM", "COLETTE",
    "BELLE", "MANDY", "MAISIE", "ANGELO", "MORTIS", "TARA", "MR. P",
    "SPROUT", "BYRON", "SQUEAK", "RUFFS", "BUSTER", "R-T", "CHESTER",
    "CORDELIUS", "MICO", "MELODIE", "DRACO", "KENJI", "SPIKE", "CROW",
    "LEON", "JESSIE", "JANET", "MEEPLE", "OLLIE", "LUMI",
]

# Real map names that do NOT exist in any fixture — if one shows up in an
# answer, the model invented it (the exact failure mode of commit 995e05a).
POLICED_MAPS = [
    "Sneaky Fields", "Double Swoosh", "Gem Fort", "Shooting Star",
    "Snake Prairie", "Center Stage", "Pinhole Punt", "Triple Dribble",
    "Super Stadium", "Kaboom Canyon", "Crystal Arcade", "Deathcap Trap",
    "Undermine", "Minecart Madness", "Out in the Open", "Cavern Churn",
    "Island Invasion", "Hot Potato", "Rockwall Brawl", "Bridge Too Far",
    "Pit Stop", "Belle's Rock", "Ring of Fire", "Beach Ball",
    "Sunny Soccer", "Penalty Kick", "Split", "Dueling Beetles",
]


def _word_hit(needle: str, haystack_upper: str) -> bool:
    """Case-insensitive whole-word match ('LEON' must not match 'NAPOLEON')."""
    pat = r"(?<![A-Z0-9])" + re.escape(needle.upper()) + r"(?![A-Z0-9])"
    return re.search(pat, haystack_upper) is not None


# ------------------------------------------------------------- factories
# Each returns a check callable; `name` makes failures self-describing.

def tool_called(*names: str, times: int | None = None):
    """At least one of `names` was called (times: exactly-N total calls)."""

    def check(ctx: CaseContext) -> CheckResult:
        hits = [t for t in ctx.trace if t["tool"] in names]
        label = f"tool_called({'|'.join(names)}" + (f", x{times})" if times else ")")
        if not hits:
            called = sorted({t["tool"] for t in ctx.trace}) or ["<none>"]
            return CheckResult(label, False, f"never called; saw {called}")
        if times is not None and len(hits) != times:
            return CheckResult(label, False, f"called {len(hits)}x, wanted {times}x")
        return CheckResult(label, True)
    return check


def tool_not_called(*names: str):
    def check(ctx: CaseContext) -> CheckResult:
        hits = sorted({t["tool"] for t in ctx.trace if t["tool"] in names})
        label = f"tool_not_called({'|'.join(names)})"
        if hits:
            return CheckResult(label, False, f"called: {hits}")
        return CheckResult(label, True)
    return check


def tool_arg(tool: str, arg: str, expected):
    """Some call to `tool` passed arg == expected (normalized tags compare
    case-insensitively with '#' tolerated)."""

    def norm(v):
        if isinstance(v, str):
            v = v.strip().upper()
            return v.lstrip("#") if v.startswith("#") else v
        return v

    def check(ctx: CaseContext) -> CheckResult:
        label = f"tool_arg({tool}.{arg}=={expected!r})"
        calls = [t for t in ctx.trace if t["tool"] == tool]
        if not calls:
            return CheckResult(label, False, f"{tool} never called")
        got = [t["args"].get(arg) for t in calls]
        if any(norm(g) == norm(expected) for g in got):
            return CheckResult(label, True)
        return CheckResult(label, False, f"saw {arg}={got}")
    return check


def mentions(*variants: str, where: str = "answer"):
    """Answer contains at least ONE of the variants (case-insensitive)."""

    def check(ctx: CaseContext) -> CheckResult:
        hay = ctx.text.lower()
        label = f"mentions({variants[0]!r}…)" if len(variants) > 1 else f"mentions({variants[0]!r})"
        if any(v.lower() in hay for v in variants):
            return CheckResult(label, True)
        return CheckResult(label, False, f"none of {list(variants)} in answer")
    return check


def lacks(*needles: str):
    """Answer contains NONE of the needles (case-insensitive)."""

    def check(ctx: CaseContext) -> CheckResult:
        hay = ctx.text.lower()
        found = [n for n in needles if n.lower() in hay]
        label = f"lacks({needles[0]!r}…)" if len(needles) > 1 else f"lacks({needles[0]!r})"
        if found:
            return CheckResult(label, False, f"found forbidden {found}")
        return CheckResult(label, True)
    return check


def number_present(value, label_hint: str = ""):
    """A distinctive number from the fixtures appears in the answer.
    Accepts int (with thousands-separator variant) or float (with the
    0-decimal rounding variant, for win rates)."""

    def variants(v) -> list[str]:
        if isinstance(v, float) and not v.is_integer():
            return [f"{v}", f"{round(v)}"]
        v = int(v)
        out = [str(v)]
        if v >= 1000:
            out.append(f"{v:,}")
        return out

    vs = variants(value)

    def check(ctx: CaseContext) -> CheckResult:
        hay = ctx.text
        label = f"number({value}{' ' + label_hint if label_hint else ''})"
        for v in vs:
            pat = r"(?<![\d.,])" + re.escape(v) + r"(?![\d])"
            if re.search(pat, hay):
                return CheckResult(label, True)
        return CheckResult(label, False, f"none of {vs} in answer")
    return check


def number_present_any(values, label_hint: str = ""):
    """Like number_present, but passes if ANY of the values appears — for
    questions the model may legitimately answer from more than one source
    (e.g. battlelog vs query_history, whose tallies differ)."""
    subchecks = [number_present(v, label_hint) for v in values]

    def check(ctx: CaseContext) -> CheckResult:
        results = [c(ctx) for c in subchecks]
        label = f"number_any({list(values)}{' ' + label_hint if label_hint else ''})"
        if any(r.ok for r in results):
            return CheckResult(label, True)
        return CheckResult(label, False, f"none of {list(values)} in answer")
    return check


def no_foreign_brawlers(allowed: set[str]):
    """No policed brawler name outside `allowed` appears anywhere in the
    answer. THE core hallucination guard (commit e6eb70f's failure mode)."""

    def check(ctx: CaseContext) -> CheckResult:
        hay = ctx.text.upper()
        allowed_up = {a.upper() for a in allowed}
        bad = [b for b in POLICED_BRAWLERS
               if b not in allowed_up and _word_hit(b, hay)]
        if bad:
            return CheckResult("no_foreign_brawlers", False,
                               f"invented/foreign brawlers: {bad}")
        return CheckResult("no_foreign_brawlers", True)
    return check


def no_foreign_maps(allowed: set[str] = frozenset()):
    """No policed real-world map name outside `allowed` appears in the answer.
    Guards the event-rotation honesty rule (commit 995e05a)."""

    def check(ctx: CaseContext) -> CheckResult:
        hay = ctx.text.upper()
        allowed_up = {a.upper() for a in allowed}
        bad = [m for m in POLICED_MAPS
               if m.upper() not in allowed_up and _word_hit(m, hay)]
        if bad:
            return CheckResult("no_foreign_maps", False,
                               f"invented/foreign maps: {bad}")
        return CheckResult("no_foreign_maps", True)
    return check


_URL_RE = re.compile(r"https?://[^\s\"')\]>*]+")


def urls_grounded():
    """Every URL in the answer (text, image, thumbnail, all tabs) appeared
    verbatim in some served tool payload — the 'never invent an image URL'
    rule from the system prompt, enforced."""

    def check(ctx: CaseContext) -> CheckResult:
        urls = set(_URL_RE.findall(ctx.text))
        views = [ctx.answer, *getattr(ctx.answer, "tabs", [])]
        for v in views:
            for u in (v.image_url, v.thumbnail_url):
                if u:
                    urls.add(u)
        bad = [u for u in urls if u.rstrip(".,") not in ctx.served]
        if bad:
            return CheckResult("urls_grounded", False, f"invented URLs: {bad}")
        return CheckResult("urls_grounded", True)
    return check


def summary_fits(limit: int = 4096):
    """Discord's embed description hard cap — anything over gets truncated
    into spillover messages, which reads broken."""

    def check(ctx: CaseContext) -> CheckResult:
        n = len(ctx.answer.summary or "")
        if n > limit:
            return CheckResult(f"summary_fits({limit})", False, f"{n} chars")
        return CheckResult(f"summary_fits({limit})", True)
    return check


def not_error_answer():
    """The agent produced a real answer, not its own failure fallbacks."""

    FALLBACKS = ("i couldn't produce an answer",
                 "i got lost in the data")

    def check(ctx: CaseContext) -> CheckResult:
        s = (ctx.answer.summary or "").lower()
        if any(f in s for f in FALLBACKS):
            return CheckResult("not_error_answer", False,
                               f"fallback answer: {s[:80]!r}")
        if not s.strip():
            return CheckResult("not_error_answer", False, "empty summary")
        return CheckResult("not_error_answer", True)
    return check
