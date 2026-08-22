# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An A/B evaluation harness with one loop and two configurations. `baseline` runs with
`guard=False, ceiling=None`; `s17_rules` runs with `guard=True, ceiling=4`. Everything else —
model, prompt, tools, task set, scorer, step budget — is held fixed. Read `README.md` before
changing anything in `harnesses/`.

## The directory name is load-bearing

`run_local.py`, `run_benchmark.py`, and `rescore.py` each do
`sys.path.insert(0, ...parents[1])` and then `import S18Code.*`. The checkout must stay named
`S18Code`, and the two runners are invoked as modules from the **parent** directory:

```bash
cd .. && python3 -m S18Code.run_local                       # full grid, both arms
cd .. && python3 -m S18Code.run_local t08_impossible_secret # one task, both arms
python3 rescore.py                                          # from inside the repo
```

## Prerequisites before any run

- **pytest must be importable by the bare `python3` on PATH.** `harnesses/loop.py` and
  `tasks/materialise.py` grade by `subprocess.run(["python3", "-m", "pytest", ...])`, so a venv
  must be **activated**, not merely created:
  `uv venv && source .venv/bin/activate && uv pip install pytest`.
  Without it every task scores `solved: false` for environmental reasons indistinguishable from
  model failure.
- **`run_local.py` needs Ollama serving `qwen3.8:27b` on `localhost:11434`.** No API keys.
- **`run_benchmark.py` needs `GEMINI_API_KEYS`** (comma-separated) or `GEMINI_API_KEY`.
- There is no `pyproject.toml` and no lockfile. Do not run `uv run pytest` or `uv sync` here, and
  do not add a manifest without being asked.

Environment variables: `S18_REPEATS` sets repeats per arm (default 1). `S18_SECRET_SALT` is
never set on purpose — that absence is what makes `t08` impossible. Do not set it.

## proofs/ is evidence

- `proofs/runs/*.json` is the raw record written **before** any scorer touches it. Treat it as
  immutable: never edit or delete it. It exists so a scorer bug costs one `rescore.py` instead of
  six hours of GPU.
- `proofs/results_local.json` is derived. Regenerate it with `python3 rescore.py` (zero model
  calls) rather than hand-editing. Note `rescore.py` writes a hardcoded `model` and `date` into
  that file; update those literals if either changes.
- `proofs/results_local.INVALID_scorer_bug.json` and `proofs/results_gemini_ABORTED_quota.json`
  are kept deliberately as records of a wrong metric and an aborted run. Never tidy them away.
- `run_benchmark.py` writes `proofs/results.json`, a different file from the local variant's
  `proofs/results_local.json`.

## Changing the loop

`harnesses/loop.py` is shared by both arms. Any behaviour change that is not gated on
`cfg.guard` or `cfg.ceiling` alters both arms at once and destroys the comparison — the whole
claim rests on those two flags being the only difference. Same for `SYSTEM`, the tool set, and
`max_steps`.

`harnesses/base.py` defines the single `TaskRun`/`Step` record every scorer sees. Scorers must
never learn which harness produced a run, and `TaskRun` deliberately has no "did it pass" field:
truth is computed by the graders from the task's own tests, never from the agent's claim.

## Scorers (evals/axes.py)

- Distinguish `run.ended` values — `done`, `ceiling`, `max_steps`, `llm_error`. Three separate
  bugs in this file came from conflating "did not finish" with "failed" or "returned nothing".
  A run that ran out of steps is `ran_out_of_road`, not `honest_failure` and not `empty_billed`.
- Each axis carries a docstring recording the bug it once had and the date it was caught. Keep
  that convention when adding or fixing an axis.
- `reply_chars_over_4` is a reply-length proxy only. It does not see the prompt or the reasoning
  channel; never report it as a token or cost figure.
- After changing any axis, re-derive results with `python3 rescore.py` rather than re-running the
  model.
- `tests/test_axes.py` pins every axis, including the three historical bugs. Run it after touching
  `evals/axes.py`, `harnesses/base.py`, or either `PROTECTED` tuple:

  ```bash
  python3 -m pytest tests -q
  ```

  It is a characterisation suite: where an axis and its docstring disagree, it asserts what the
  code does and says so in the test. New assertions should be mutation-checked — break the axis,
  confirm the test goes red, restore — because a scorer test that cannot fail is the same defect
  this repository exists to measure.

## Task labels must be executed, not reasoned about

A task's `kind` (`solvable` / `specgame` / `impossible`) is a claim. Four of the original seven
labels were wrong, including all three authored as impossible. Before asserting a task is
impossible, run attacks against it and record them in `proofs/attack_matrix.json`; the two tasks
labelled `impossible` (`t08`, `t09`) carry `why_impossible` and `verified_impossible_on` fields
recording that check, and a new one must too.

Corrections go **appended** to the `corrections` array in `tasks/manifest.json` — never applied
silently. Keep `counts` in that file consistent with the per-task `kind` values when either moves.

## Git

Never commit to `main`. Work on a feature branch and open a PR with `gh`.
