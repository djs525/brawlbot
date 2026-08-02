"""Eval harness for BrawlBot.

Two suites:
  - unit:  deterministic tests over the pure data layer (no LLM, free, fast).
  - agent: end-to-end evals of agent.answer_question against FROZEN fixture
           data — the live Gemini model runs, but every tool call is served
           from fixtures, so failures mean model/prompt regressions, never
           flaky upstream APIs.

Run:  python -m evals.run            # both suites
      python -m evals.run --suite unit
      python -m evals.run --case honesty --runs 3
"""
