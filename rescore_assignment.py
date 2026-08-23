"""Re-derive an assignment grid's results from its saved journals. No model calls.

`rescore.py` cannot do this. It globs proofs/runs/ and stamps the literal
`"model": "qwen3.8:27b"` into every row, so an assignment journal dropped there
is silently relabelled as qwen with no error raised (CLAUDE.md says so, and it
is the reason this file exists rather than a flag on that one). Here the model,
the date and every other manifest field come from the grid's own manifest.json.

Two things it does:

  1. **Identity control.** Under the published rule the recomputed rows must
     equal the committed results.json. If they do not, either the scorer moved
     or the journals did, and the run says which rows differ. "All attacks
     closed" and "the harness is broken" look identical without a case that
     passes for the right reason; the same is true of a rescore that agrees
     with everything because it is computing nothing.

  2. **A rule change, applied to saved observations.** `--rule` selects which
     reading drives the verification axis (evals/axes.py VERIFICATION_RULES).
     The journals are read-only inputs; nothing here re-runs a model, and the
     provider module is never imported.

    python3 rescore_assignment.py                          # control, both grids
    python3 rescore_assignment.py --rule v2_command_after_last_edit --write
    python3 rescore_assignment.py --grid proofs/assignment_v1 --list-rules

Journals are immutable evidence. This file only ever reads them, and it writes
derived output to `results_rescored_{rule}.json` rather than over results.json,
so the committed record of what the grid actually reported stays put.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from S18Code.evals.axes import (DEFAULT_VERIFICATION_RULE, VERIFICATION_RULES,
                                score)
from S18Code.harnesses.base import Step, TaskRun

HERE = pathlib.Path(__file__).parent
GRIDS = (HERE / "proofs" / "assignment_v1",
         HERE / "proofs" / "assignment_v1_t12x6")

# The five ways a graded run failed to pass, carried from the grading report
# into the row. Kept apart rather than collapsed into solved:false - a reader is
# entitled to know whether the suite failed, skipped, collected nothing, never
# reported, or timed out.
STATUS_FLAGS = ("any_skipped", "nothing_collected", "collection_errored",
                "no_report", "grading_timed_out")

# Journal fields that are evidence but not TaskRun constructor arguments.
NOT_RUN_FIELDS = ("actually_passed", "kind", "pytest_tail", "final_files",
                  "rep", "usage", "provider_requests", "provider_retries",
                  "grading_report", "grading_error")


def row_from_journal(d: dict, rule: str) -> tuple[dict, list[str]]:
    """Recompute one results row from one journal, exactly as run_assignment.py
    builds it. Returns the row and the names of any status flags that were
    absent from the journal.

    Absent is not the same as false. proofs/assignment_v1 ran before
    `grading_timed_out` existed, so its journals carry no such key; filling it
    with False is correct for those runs (nothing timed out - they completed and
    were graded) but the fill is reported rather than performed quietly. A
    rescore that silently invents a field is the same defect as a grid that
    silently averages its own outage.
    """
    d = dict(d)
    report = d.get("grading_report")
    grading_error = d.get("grading_error")
    passed, kind, rep = d["actually_passed"], d["kind"], d["rep"]
    usage = d.get("usage") or []
    total_tokens = sum(u.get("total_tokens", 0) for u in usage)

    run = TaskRun(**{k: v for k, v in d.items() if k not in NOT_RUN_FIELDS}
                  | {"steps": [Step(**s) for s in d["steps"]]})

    if grading_error is not None:
        # An infrastructure non-result. run_assignment.py emits a deliberately
        # different row shape here - no axes, solved:null - so that a complete
        # N-cell manifest does not look like an N-cell result set. Reproduced
        # rather than scored, because scoring it would be the lie.
        return {"task": run.task_id, "harness": run.harness, "kind": kind,
                "rep": rep, "not_a_result": True, "result_status": "grader_error",
                "solved": None, "claimed": run.claimed_success,
                "ended": run.ended, "steps": len(run.steps), "calls": run.calls,
                "provider_requests": d.get("provider_requests"),
                "provider_retries": d.get("provider_retries"),
                "grader_exception": grading_error["exception"],
                "grader_detail": grading_error["detail"],
                "usage_total_tokens": total_tokens}, []

    row = score(run, actually_passed=passed, verification_rule=rule)
    row["kind"], row["claimed"], row["rep"] = kind, run.claimed_success, rep
    row["usage_total_tokens"] = total_tokens
    row["provider_requests"] = d.get("provider_requests")
    row["provider_retries"] = d.get("provider_retries")

    backfilled = [f for f in STATUS_FLAGS if f not in (report or {})]
    for flag in STATUS_FLAGS:
        row[flag] = (report or {}).get(flag, False)
    return row, backfilled


def rescore_grid(grid: pathlib.Path, rule: str) -> dict:
    """Every journal in one grid, recomputed under one rule."""
    manifest = json.loads((grid / "manifest.json").read_text())
    rows, backfilled = [], {}
    for f in sorted((grid / "runs").glob("*.json")):
        row, missing = row_from_journal(json.loads(f.read_text()), rule)
        rows.append(row)
        for flag in missing:
            backfilled[flag] = backfilled.get(flag, 0) + 1

    # The grid's own manifest, not a literal. Provenance travels with the rows
    # it describes; that is the whole reason rescore.py could not be reused.
    return {"manifest": {**manifest,
                         "verification_rule": rule,
                         "derived_from": f"{grid.name}/runs",
                         "derivation": "rescore_assignment.py, 0 model calls",
                         "status_flags_backfilled_false": backfilled or None},
            "rows": rows}


def diff_rows(published: list[dict], derived: list[dict]) -> list[str]:
    """Field-level differences, keyed by task+rep so ordering cannot mask one."""
    def key(r):
        return (r["task"], r["rep"])

    out, pub = [], {key(r): r for r in published}
    for r in derived:
        p = pub.get(key(r))
        if p is None:
            out.append(f"{r['task']} r{r['rep']}: no published row")
            continue
        for field in sorted(set(p) | set(r)):
            a, b = p.get(field, "<absent>"), r.get(field, "<absent>")
            if a != b:
                out.append(f"{r['task']} r{r['rep']}: {field}: {a!r} -> {b!r}")
    for k in set(pub) - {key(r) for r in derived}:
        out.append(f"{k[0]} r{k[1]}: published row has no journal")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grid", action="append", type=pathlib.Path,
                    help="grid directory (repeatable). Default: both assignment grids.")
    ap.add_argument("--rule", default=DEFAULT_VERIFICATION_RULE,
                    choices=sorted(VERIFICATION_RULES),
                    help="which reading drives the verification axis")
    ap.add_argument("--write", action="store_true",
                    help="write results_rescored_{rule}.json into each grid")
    ap.add_argument("--list-rules", action="store_true")
    a = ap.parse_args(argv)

    if a.list_rules:
        for name, fn in sorted(VERIFICATION_RULES.items()):
            mark = "  (published default)" if name == DEFAULT_VERIFICATION_RULE else ""
            print(f"{name}{mark}\n    {fn.__doc__.splitlines()[0]}")
        return 0

    grids = a.grid or list(GRIDS)
    is_control = a.rule == DEFAULT_VERIFICATION_RULE
    failures = 0

    for grid in grids:
        if not (grid / "runs").is_dir():
            print(f"{grid}: no runs/ directory", file=sys.stderr)
            return 2
        derived = rescore_grid(grid, a.rule)
        published = json.loads((grid / "results.json").read_text())["rows"]
        diffs = diff_rows(published, derived["rows"])

        print(f"\n{grid.name}  rule={a.rule}  "
              f"{len(derived['rows'])} rows from journals, 0 model calls")
        back = derived["manifest"]["status_flags_backfilled_false"]
        if back:
            print(f"  absent in these journals, filled False: {back} "
                  f"(this grid predates the flag)")

        if is_control:
            # The control has to be able to fail, or it is decoration. Only the
            # backfilled flags may differ, and only by being absent upstream.
            real = [d for d in diffs if "'<absent>' ->" not in d]
            if real:
                failures += 1
                print(f"  CONTROL FAILED: {len(real)} field(s) differ from the "
                      f"committed results.json")
                for d in real:
                    print(f"    {d}")
            else:
                print(f"  control: recomputed rows match results.json"
                      + (f" ({len(diffs)} absent-flag backfills)" if diffs else ""))
        else:
            changed = sorted({d.split(":")[0] for d in diffs
                              if "'<absent>' ->" not in d})
            print(f"  vs published rule {DEFAULT_VERIFICATION_RULE}: "
                  f"{len(changed)} row(s) change")
            for d in diffs:
                if "'<absent>' ->" not in d:
                    print(f"    {d}")

        if a.write:
            out = grid / f"results_rescored_{a.rule}.json"
            out.write_text(json.dumps(derived, indent=1) + "\n")
            print(f"  wrote {out}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
