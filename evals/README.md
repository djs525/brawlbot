# BrawlBot eval harness

Two suites. Run before shipping any prompt / model / data-layer change.

```bash
python -m evals.run                  # both suites
python -m evals.run --suite unit     # free, no keys, <1s — data layer only
python -m evals.run --suite agent    # live Gemini, fixture data tools
python -m evals.run --case honesty   # substring filter (comma-separated OK)
python -m evals.run --runs 3         # flakiness measurement
python -m evals.run --list           # show cases without running
```

Exit code 0 = green, 1 = failures, 2 = config error. JSON report per run in
`evals/results/` (gitignored).

## How it works

- **Unit suite** (`unit.py`): deterministic asserts over the pure data layer —
  `outcome`, `is_ranked`, `summarize_battles`, `slim_*`, `jsonout.dump`,
  `normalize_tag`, `agent._build_answer`. No network, no LLM.
- **Agent suite** (`cases.py` + `fixtures.py`): each case calls the real
  `agent.answer_question` (live Gemini), but `plugins.execute` is swapped for
  a `FixtureRouter` serving frozen data — so no Brawl Stars / BrawlAPI calls,
  and a failure is a model/prompt regression, never upstream flake.
- Fixtures are built through the **real production slimmers**
  (`bs_client.slim_player`, `summarize_battles`, `jsonout.dump`) so payload
  shapes can never drift from prod. Honesty notes import from `plugins.events`.
- Checks (`checks.py`) are pure string/trace assertions — no LLM grading:
  - `no_foreign_brawlers` / `no_foreign_maps` — hallucination guards
    (the e6eb70f and 995e05a failure modes, pinned forever)
  - `urls_grounded` — every URL in the answer appeared in a tool payload
  - `number_present` / `number_present_any` — distinctive fixture numbers
  - `tool_called` / `tool_not_called` / `tool_arg` — routing correctness
- Expected numbers are **computed through production tally code** at import,
  never hand-written, so changing `summarize_battles` updates expectations.

## Rate limits

Gemini free tier ≈ 5 requests/min; a case burns 2–4. The runner paces runs
(`--pace 15` default), and retries quota/429 errors after a cooldown
(`--retries 2 --cooldown 60`). Quota failures are labeled
"infra error (not a regression)" and never mask a real red.

## Adding a case

Add a `Case` to `cases.py`: question + checks (+ fixture variant flags).
Prefer distinctive fixture numbers (41,237 trophies, 56.7% WR) so
`number_present` can't false-positive. Names are grep-able via `--case`.
