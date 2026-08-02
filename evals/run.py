"""Eval runner.

  python -m evals.run                    # unit + agent suites
  python -m evals.run --suite unit       # free, no API keys needed
  python -m evals.run --suite agent      # needs GEMINI_API_KEY (from .env)
  python -m evals.run --case honesty     # substring filter on case names
  python -m evals.run --runs 3           # repeat each agent case (flakiness)
  python -m evals.run --list             # show cases without running

Agent cases patch plugins.execute with the fixture router, so NO Brawl Stars /
BrawlAPI network calls happen — only Gemini runs live. Cases run sequentially
(rate-limit friendly; also keeps the router patch race-free).

Exit code: 0 all green, 1 any failure, 2 harness/config error.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # agent.py reads GEMINI_API_KEY at import time

RESULTS_DIR = Path(__file__).parent / "results"

GREEN, RED, DIM, RESET = "\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[0m"


def _mark(ok: bool) -> str:
    return f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"


# Gemini free tier allows ~5 requests/min; an eval case burns 2-4. These
# signatures mean "the model never got to answer" — infra, not a regression —
# so the runner cools down and retries instead of failing the case.
_QUOTA_SIGS = ("429", "resource_exhausted", "quota", "rate limit")


def _is_quota_error(err: str | None) -> bool:
    return bool(err) and any(s in err.lower() for s in _QUOTA_SIGS)


# ----------------------------------------------------------------- agent

async def _run_case(case, timeout: float) -> dict:
    """One run of one agent case: patch the router in, ask, check, restore."""
    import agent
    import plugins
    from .checks import CaseContext, not_error_answer, summary_fits
    from .fixtures import FixtureRouter

    router = FixtureRouter(events_upcoming=case.events_upcoming)
    original = plugins.execute
    plugins.execute = router.route
    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(
            agent.answer_question(case.question, asker_tag=case.asker_tag,
                                  mentioned=case.mentioned),
            timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timed out after {timeout:.0f}s",
                "checks": [], "trace": [t["tool"] for t in router.trace],
                "seconds": round(time.monotonic() - started, 1)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "checks": [], "trace": [t["tool"] for t in router.trace],
                "seconds": round(time.monotonic() - started, 1)}
    finally:
        plugins.execute = original

    ctx = CaseContext(answer=answer, trace=router.trace,
                      served=router.served_text())
    results = [c(ctx) for c in [not_error_answer(), summary_fits(),
                                *case.checks]]
    return {
        "ok": all(r.ok for r in results),
        "error": None,
        "checks": [r.__dict__ for r in results],
        "trace": [t["tool"] for t in router.trace],
        "answer": {"title": answer.title, "summary": answer.summary,
                   "tabs": [t.label for t in answer.tabs]},
        "seconds": round(time.monotonic() - started, 1),
    }


async def _run_case_with_retry(case, timeout: float, retries: int,
                               cooldown: float) -> dict:
    """Run once; on a quota error (model never answered) cool down and retry
    up to `retries` times. Model/prompt failures are NEVER retried — only
    infra errors are, so a red case stays meaningful."""
    r = await _run_case(case, timeout)
    for attempt in range(retries):
        if not _is_quota_error(r.get("error")):
            break
        print(f"        {DIM}quota exhausted — cooling down {cooldown:.0f}s "
              f"(retry {attempt + 1}/{retries}){RESET}")
        await asyncio.sleep(cooldown)
        r = await _run_case(case, timeout)
    r["infra_error"] = _is_quota_error(r.get("error"))
    return r


async def _run_agent_suite(cases, runs: int, timeout: float, retries: int,
                           cooldown: float, pace: float) -> list[dict]:
    import plugins
    if not plugins.all_tools():
        plugins.load_plugins()  # real tool schemas; execute() gets patched per case

    out = []
    first = True
    for case in cases:
        case_runs = []
        for i in range(runs):
            if not first and pace > 0:
                await asyncio.sleep(pace)  # stay under requests/min limits
            first = False
            r = await _run_case_with_retry(case, timeout, retries, cooldown)
            case_runs.append(r)
            status = _mark(r["ok"])
            extra = f" {DIM}({r['seconds']}s, tools: {r['trace']}){RESET}"
            run_tag = f" run {i + 1}/{runs}" if runs > 1 else ""
            print(f"  {status}  {case.name}{run_tag}{extra}")
            for c in r["checks"]:
                if not c["ok"]:
                    print(f"        {RED}✗ {c['name']}{RESET}: {c['detail']}")
            if r["error"]:
                kind = "infra error (not a regression)" if r["infra_error"] else "error"
                print(f"        {RED}✗ {kind}{RESET}: {r['error'][:200]}")
        passed = sum(1 for r in case_runs if r["ok"])
        out.append({"name": case.name, "question": case.question,
                    "notes": case.notes, "runs": case_runs,
                    "pass_rate": passed / len(case_runs),
                    "ok": passed == len(case_runs),
                    "infra_only": all(r.get("infra_error") for r in case_runs
                                      if not r["ok"]) and passed < len(case_runs)})
    return out


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="BrawlBot eval harness")
    ap.add_argument("--suite", choices=["unit", "agent", "all"], default="all")
    ap.add_argument("--case", default="", help="substring filter on case names")
    ap.add_argument("--runs", type=int, default=1,
                    help="repetitions per agent case (default 1)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds per agent run (default 120)")
    ap.add_argument("--retries", type=int, default=2,
                    help="retries per run on quota/429 errors (default 2)")
    ap.add_argument("--cooldown", type=float, default=60.0,
                    help="seconds to wait before a quota retry (default 60)")
    ap.add_argument("--pace", type=float, default=15.0,
                    help="seconds between agent runs, for free-tier rate "
                         "limits (default 15; 0 to disable)")
    ap.add_argument("--list", action="store_true", help="list cases and exit")
    args = ap.parse_args()

    from .cases import CASES
    wants = [w.strip().lower() for w in args.case.split(",") if w.strip()]
    cases = [c for c in CASES
             if not wants or any(w in c.name.lower() for w in wants)]

    if args.list:
        for c in cases:
            print(f"{c.name}\n  Q: {c.question}\n  {DIM}{c.notes}{RESET}")
        return 0

    report = {"started": datetime.now(timezone.utc).isoformat(),
              "suite": args.suite, "unit": [], "agent": []}
    failed = False

    if args.suite in ("unit", "all"):
        from .unit import run_unit
        print("── unit suite ──")
        unit_results = run_unit()
        for r in unit_results:
            print(f"  {_mark(r['ok'])}  {r['name']}")
            if not r["ok"]:
                print(f"        {RED}✗{RESET} {r['checks'][0]['detail']}")
        report["unit"] = unit_results
        n_ok = sum(r["ok"] for r in unit_results)
        print(f"  {n_ok}/{len(unit_results)} unit tests passed\n")
        failed |= n_ok < len(unit_results)

    if args.suite in ("agent", "all"):
        import os
        if not os.getenv("GEMINI_API_KEY"):
            print(f"{RED}GEMINI_API_KEY missing — agent suite needs it "
                  f"(.env). Unit suite runs without it.{RESET}")
            return 2
        if not cases:
            print(f"no agent cases match filter {args.case!r}")
            return 2
        print(f"── agent suite ({len(cases)} case(s), {args.runs} run(s) each) ──")
        agent_results = asyncio.run(
            _run_agent_suite(cases, args.runs, args.timeout,
                             args.retries, args.cooldown, args.pace))
        report["agent"] = agent_results
        n_ok = sum(r["ok"] for r in agent_results)
        n_infra = sum(1 for r in agent_results if r.get("infra_only"))
        print(f"  {n_ok}/{len(agent_results)} agent cases passed"
              + (f" ({n_infra} failed on quota only — rerun those)" if n_infra else ""))
        for r in agent_results:
            if 0 < r["pass_rate"] < 1:
                print(f"  {DIM}flaky: {r['name']} "
                      f"({r['pass_rate']:.0%} pass rate){RESET}")
        failed |= n_ok < len(agent_results)

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n{DIM}report: {out_path}{RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
