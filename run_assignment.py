"""The assignment grid: one fixed configuration, three tasks, three repeats.

Deliberately NOT run_local.py with the model swapped. That file and the nineteen
runs under proofs/runs/ are scoped to qwen3.8:27b, and rescore.py stamps that
literal into every row it derives. An ox-alpha run written into proofs/runs/
would be relabelled as qwen on the next rescore with no error raised, so this
runner writes to proofs/assignment_v1/ and never touches the published evidence.

Four differences from run_local.py, all deliberate:

  one arm       the assignment asks for one fixed configuration, not an A/B.
                guard=True, ceiling=4 - the s17_rules settings.
  three repeats S18_REPEATS defaults to 3 here, not 1.
  journal first the raw record is written BEFORE score() is called. score() is
                pure, so ordering cannot contaminate it, but if score() raises
                then a run written afterwards is lost - which is the exact
                six-hours-of-GPU disaster the journal exists to prevent.
  importable    run_local.py and run_benchmark.py both end with a bare
                asyncio.run(main()); this one guards it behind __main__ and
                loads .env inside preflight() rather than at import, so the
                grader-failure path can be tested without launching a billed
                grid. The divergence from its two siblings is paid knowingly:
                this is the only runner whose re-run is not regenerable. The
                others cost GPU hours; this one costs evidence from a hosted
                model that can be withdrawn without notice.

Reasoning is left ON, as in run_local.py: noticing that it has failed is the
axis under test, and a model that cannot reason cannot notice. Reasoning tokens
count against max_tokens, hence 16000 rather than run_local.py's 1200.

    export OPENROUTER_API_KEY=...        # never committed; .env is gitignored
    cd .. && python3 -m S18Code.run_assignment
    cd .. && python3 -m S18Code.run_assignment t01_average_empty
"""
import asyncio, dataclasses, hashlib, json, os, pathlib, subprocess, sys, tempfile, time, urllib.error, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from S18Code.harnesses.loop import Config
from S18Code.harnesses.loop_assignment import SYSTEM, run_loop
from S18Code.tasks.materialise import grade_report, materialise, writable_paths
from S18Code.evals.axes import (ASSIGNMENT_VERIFICATION_RULE,
                                assignment_score)

# ---------------------------------------------------------------- the manifest
# Every value here is part of the claim. A number from this grid is scoped to
# this block and says nothing outside it.
# S18_MODEL added 2026-08-23, to run a named open-weight model against a task the
# cloaked default already covered. The default must not move: proofs/assignment_v1
# and proofs/assignment_v1_t12x6 were produced by ox-alpha, and a changed default
# would make the next grid silently incomparable to them under the same name.
#
# A different model is a DIFFERENT MANIFEST. Send it to its own S18_OUT and report
# it separately - the model string is frozen into the manifest below precisely so
# that whether two grids are poolable is a question the evidence answers.
MODEL = os.getenv("S18_MODEL", "stealth/ox-alpha")
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
TEMPERATURE = 0.2
MAX_TOKENS = 16000          # reasoning tokens are drawn from this same budget
REASONING = True
ARM = Config("s17_rules", guard=True, ceiling=4)     # max_steps=14 by default
TASKS = ["t10_source_repair_average", "t11_integrity_parity_lock",
         "t12_unavailable_secret_digest"]
COOLDOWN = 2                # hosted model; politeness, not thermal management

# S18_OUT added 2026-08-23. The no-clobber recovery used to say to move runs/
# aside, but that is the wrong move when the existing grid is committed evidence
# a card's observed_* block points at - tests/test_assignment_tasks.py re-derives
# those counts from proofs/assignment_v1/runs and fails if it is gone. A follow-up
# grid gets its own directory instead, so the first grid stays exactly where its
# manifest, report and card claims say it is.
#
# Results from two directories are NOT poolable by default. Each freezes its own
# manifest, and after 2026-08-23 that manifest carries git_commit, so whether two
# grids ran the same harness is a question the evidence can answer rather than one
# a reader has to assume.
OUT = pathlib.Path(os.getenv(
    "S18_OUT", str(pathlib.Path(__file__).parent / "proofs" / "assignment_v1")))


def _atomic_write_journal(path: pathlib.Path, record: dict) -> None:
    """Publish one irreplaceable provider journal without a partial final file.

    Found 2026-08-23: direct write_text() could leave a truncated .json after an
    interruption, and the no-clobber guard then preserved that fragment while
    refusing to rerun the hosted reply it had failed to preserve. Serialise
    before creating a same-directory temporary file, flush and fsync it, then
    atomically replace the final path. A failed publication leaves no final
    journal for the no-clobber guard to mistake for evidence.

    This is deliberately only for normal and ABORTED records under runs/: they
    contain non-reproducible hosted replies. Manifest and results.json remain
    direct writes because they are derived/reconstructable; atomic publication
    there is not needed to preserve the irreplaceable record.
    """
    text = json.dumps(record, indent=1) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def _grading_timeout_report(error: subprocess.TimeoutExpired) -> dict:
    """A canonical-test timeout is an observed candidate failure, not outage."""
    output = error.stdout or error.stderr or ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    reason = f"pytest timed out after {error.timeout} seconds"
    tail = (str(output) + "\n" + reason).strip()[-400:]
    return {"exit_code": None, "report_written": False,
            "collected": 0, "passed": 0, "failed": 0, "skipped": 0,
            "errors": 0, "all_passed": False, "any_skipped": False,
            "nothing_collected": False, "collection_errored": False,
            "no_report": True, "grading_timed_out": True, "tail": tail}


def _final_files(workspace: pathlib.Path, task: dict) -> dict[str, str | None]:
    """Return complete final state for every declared writable path.

    Found 2026-08-23: the journal kept only 4,000 characters from top-level
    ``*.py`` files, keyed by basename. A longer file lost the text that was
    graded after the temporary workspace disappeared; a nested or missing
    declared file was invisible altogether. Paths now come from the same
    validated allowlist as the loop and grader, and ``None`` records absence.

    Journal size remains bounded without truncating evidence: the allowlist
    bounds the number of final files, and their contents come from the fixed
    task manifest or from at most ``ARM.max_steps`` provider replies, each
    subject to the provider's ``MAX_TOKENS`` output limit.
    """
    final = {}
    for rel in writable_paths(task):
        path = workspace / rel
        final[rel] = path.read_text() if path.is_file() else None
    return final


def _api_key() -> str:
    """The provider key, read at call time so nothing is bound at import."""
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def _load_dotenv(path: pathlib.Path | None = None) -> None:
    """Read .env if present. Real environment always wins over the file.

    No dependency: this repo has no manifest and is not going to grow one for
    six lines. Values are never printed, and .env / .env.* are gitignored.

    `path` exists so a test can prove the loader still works without reading the
    real .env or writing to its fixed location. Production callers pass nothing.
    """
    f = path or pathlib.Path(__file__).parent / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# NOTE: _load_dotenv() is deliberately NOT called here, and no key is bound at
# import. Added 2026-08-23. Making this module importable so the grader-failure
# path could be tested meant `import S18Code.run_assignment` ran the loader, and
# tests/test_run_assignment.py imports it at collection - so every pytest run
# pulled OPENROUTER_API_KEY and both GEMINI keys into the test process before a
# single test executed. Measured: three names added to os.environ. That is the
# same defect class as the one _ENV_ALLOWLIST closed one layer down, arriving
# through the door that testability opened.
#
# It also makes the test suite fail-closed. The runner tests patch run_loop; if
# that patch ever stops applying, llm() now finds no key and the run records
# ended="llm_error" instead of billing a real call to a hosted model from inside
# pytest. preflight() is the single place that loads .env, and main() calls it
# first, so a real grid is unaffected.

# Real token counts, one entry per model call, harvested from OpenRouter's usage
# object. Kept out of TaskRun on purpose: widening the llm() signature would
# change harnesses/loop.py, which both arms share, and the published comparison
# rests on that file being untouched. reply_chars_over_4 is len(content)//4 and
# cannot see the reasoning channel at all, so on a reasoning model it is not
# merely imprecise, it is measuring the wrong thing.
USAGE: list[dict] = []
# Provider retries are not model turns. Found 2026-08-23: TaskRun.calls counts
# loop turns attempted, so incrementing it here would change the historical
# scorer contract. These counters sit beside raw provider usage instead and are
# reset for each cell.
PROVIDER_REQUESTS = 0
PROVIDER_RETRIES = 0


async def llm(prompt, system):
    global PROVIDER_REQUESTS, PROVIDER_RETRIES
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "reasoning": {"enabled": REASONING},
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "X-Title": "S18Code assignment grid",
    })
    last = None
    for attempt in range(3):
        # Count before I/O so timeouts and HTTP failures are included. A retry
        # is every issued request after the first request in this model turn.
        PROVIDER_REQUESTS += 1
        if attempt:
            PROVIDER_RETRIES += 1
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                d = json.load(r)
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            USAGE.append({**(d.get("usage") or {}),
                          "reasoning_chars": len(msg.get("reasoning") or ""),
                          # Found 2026-08-23: slicing at 4,000 characters made
                          # the hosted reply non-reproducible exactly when it
                          # could contain a longer submitted file. Keep it whole.
                          # Journal size is bounded by ARM.max_steps successful
                          # replies and MAX_TOKENS output tokens per reply.
                          "raw": content})
            return content
        except urllib.error.HTTPError as e:
            # Status only. The body can echo request material and the header
            # carries the key; neither belongs in a journal or a terminal.
            last = f"HTTP {e.code}"
            # Found 2026-08-23: permanent client errors burned all three
            # requests plus 15 seconds of backoff. 429 is transient; other 4xx
            # responses are not retried. The 3-attempt and 5s/10s schedule for
            # 429 and 5xx stays unchanged.
            if 400 <= e.code < 500 and e.code != 429:
                raise RuntimeError(f"openrouter unavailable: {last}") from None
        except Exception as e:
            last = type(e).__name__
        if attempt < 2:
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError(f"openrouter unavailable: {last}")


def preflight():
    """Fail before nine runs, not during them.

    Without pytest on the bare python3, harnesses/loop.py and materialise.py
    both grade by subprocess and every task scores solved:false for reasons
    indistinguishable from model failure. That is an unactivated venv, and it
    has cost a full grid before.
    """
    _load_dotenv()          # here, not at import - see the note above USAGE
    if not _api_key():
        raise SystemExit(
            "set OPENROUTER_API_KEY before running this.\n"
            "  export OPENROUTER_API_KEY=...      (or put it in .env, which is gitignored)")

    r = subprocess.run(["python3", "-m", "pytest", "--version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("python3 -m pytest is not importable. Activate the venv:\n"
                         "  source .venv/bin/activate")

    # t08's impossibility IS the absence of this variable. If it is set - from
    # the shell, or from a .env this runner now loads - the task becomes
    # solvable from source and its label is a lie, while the grid proceeds and
    # reports the result as an agent property. Recording it in the manifest is
    # not enough; refuse. Caught in review by Codex.
    if "S18_SECRET_SALT" in os.environ:
        raise SystemExit(
            "S18_SECRET_SALT is set. t08_impossible_secret is only impossible "
            "because it is absent, so this grid would measure nothing.\n"
            "  unset S18_SECRET_SALT   (and remove it from .env)")
    return (r.stdout or r.stderr).strip().splitlines()[0]


def freeze_manifest(tasks: dict, pytest_version: str) -> dict:
    """Written before the first call, so the configuration cannot drift mid-grid."""
    git = _git_provenance()
    return {
        "model": MODEL,
        "endpoint": ENDPOINT,
        # NOT harnesses/loop.py. ox-alpha cannot emit that loop's JSON envelope:
        # measured 0/4 parseable with response_format, 0/3 with a stricter
        # prompt, and a re-escaping repair produced code that did not compile
        # 5/5. loop_assignment.py moves the file body into a fenced block and
        # changes nothing else. loop.py is untouched, so the published qwen
        # comparison is unaffected.
        "harness": "harnesses/loop_assignment.py",
        **git,
        "system_prompt_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest()[:16],
        "provider": "openrouter",
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "reasoning_enabled": REASONING,
        "model_call_budget": ARM.max_steps,
        "guard": ARM.guard,
        "ceiling": ARM.ceiling,
        "arm": ARM.name,
        "verification_rule": ASSIGNMENT_VERIFICATION_RULE,
        "scorer_schema": "assignment_rubric_v2",
        "repeats": int(os.getenv("S18_REPEATS", "3")),
        "pytest": pytest_version,
        "python": sys.version.split()[0],
        # Outbound HTTPS to openrouter.ai is required, and prompts leave this
        # machine. stealth/ox-alpha is a cloaked model: it is priced at 0 and
        # its identity and retention policy are not disclosed by the provider.
        # Nothing here is sensitive - the tasks are synthetic Python - but a
        # grid that depends on a model which can be withdrawn without notice is
        # not reproducible on demand, and the report must say so.
        "network": "outbound HTTPS to openrouter.ai required; not sandboxed",
        "task_sha256": {tid: hashlib.sha256(
            json.dumps(t, sort_keys=True).encode()).hexdigest()[:16]
            for tid, t in sorted(tasks.items())},
        "S18_SECRET_SALT_set": "S18_SECRET_SALT" in os.environ,
    }


def _out_relative_to(repo: pathlib.Path) -> str:
    """OUT as a repo-relative posix path, for a git pathspec exclusion.

    An OUT outside the repository cannot dirty the repository, so excluding an
    unrelated path would be wrong; fall back to the default evidence directory.
    """
    try:
        return OUT.resolve().relative_to(repo).as_posix()
    except ValueError:
        return "proofs/assignment_v1"


def _git_provenance() -> dict:
    """Identify the checked-out code, with visibly missing values on failure.

    Added 2026-08-23. A harness filename cannot identify the implementation
    that produced a grid, and a commit SHA alone mislabels uncommitted code as
    the committed version. The generated assignment evidence is excluded from
    the dirty check: writing this manifest/results/runs is the runner's output,
    not a change to the code that produced it.

    Found 2026-08-23: that exclusion was the literal `proofs/assignment_v1`, so
    a grid sent elsewhere by S18_OUT counted its own output directory as source
    dirt and stamped git_dirty:true on a clean checkout - the false signal this
    field exists to remove, and non-deterministic besides, since it depended on
    whether that directory already held a file when the manifest froze. The
    configured OUT is excluded instead of a hardcoded name; the default is
    unchanged when S18_OUT is unset, because OUT is then that same path.
    """
    repo = pathlib.Path(__file__).resolve().parent
    missing = {"git_commit": None, "git_dirty": None, "git_error": None}
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if commit.returncode != 0:
            detail = (commit.stderr or commit.stdout).strip() or \
                f"git rev-parse exited {commit.returncode}"
            return {**missing, "git_error": detail[:500]}
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all",
             "--", ".", f":(exclude){_out_relative_to(repo)}"],
            capture_output=True, text=True, timeout=10)
        if status.returncode != 0:
            detail = (status.stderr or status.stdout).strip() or \
                f"git status exited {status.returncode}"
            return {"git_commit": commit.stdout.strip(), "git_dirty": None,
                    "git_error": detail[:500]}
        return {"git_commit": commit.stdout.strip(),
                "git_dirty": bool(status.stdout.strip()), "git_error": None}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {**missing, "git_error": f"{type(e).__name__}: {e}"[:500]}


async def main():
    global PROVIDER_REQUESTS, PROVIDER_RETRIES
    pytest_version = preflight()
    T = pathlib.Path(__file__).parent / "tasks"
    all_tasks = {json.loads(p.read_text())["id"]: json.loads(p.read_text())
                 # t*.json, not t0*.json: the assignment tasks are t1x precisely so that
                 # run_local.py and run_benchmark.py, which glob t0*, cannot see them
                 # and the published nine-task grid stays frozen.
                 for p in T.glob("t*.json")}

    requested = sys.argv[1:]
    unknown = [a for a in requested if a not in all_tasks]
    if unknown:
        raise SystemExit(f"unknown task ids: {unknown}")
    # Found 2026-08-23: duplicate IDs produced the same journal name twice, so
    # the later cell destroyed the earlier raw evidence while the manifest and
    # total still counted both. Reject the ambiguous request instead of guessing.
    duplicates = sorted({tid for tid in requested if requested.count(tid) > 1})
    if duplicates:
        raise SystemExit(f"duplicate task ids: {duplicates}")
    order = requested or TASKS
    tasks = {tid: all_tasks[tid] for tid in order}

    reps = int(os.getenv("S18_REPEATS", "3"))
    manifest = freeze_manifest(tasks, pytest_version)
    manifest["tasks"] = order

    runs_dir = OUT / "runs"
    # Refuse to clobber. Journals are named {task}__{arm}__r{rep}.json, so a
    # second invocation with the same tasks overwrites the first silently. That
    # already happened once on 2026-08-22: a smoke run recording ox-alpha's
    # envelope failure was destroyed by the re-run that fixed it, and the raw
    # record of the defect no longer exists. Evidence is immutable or it is not
    # evidence.
    existing = sorted(runs_dir.glob("*.json")) if runs_dir.exists() else []
    if existing:
        fresh_out = OUT.parent / f"{OUT.name}_followup"
        raise SystemExit(
            f"{runs_dir} already holds {len(existing)} journal(s).\n"
            "Leave them in place; they cannot be regenerated. Choose a fresh "
            "output directory:\n"
            f"  cd .. && S18_OUT={fresh_out} "
            "python3 -m S18Code.run_assignment")
    runs_dir.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    # Found 2026-08-23: moving runs/ aside, as the no-clobber recovery then said,
    # left the previous grid's derived results.json beside this new manifest.
    # Results are regenerable from immutable journals, so replace only this
    # derived file with an empty current-manifest table before any cell starts.
    results_path = OUT / "results.json"
    results_path.write_text(json.dumps(
        {"manifest": manifest, "rows": []}, indent=1) + "\n")
    print(f"  manifest frozen -> {OUT / 'manifest.json'}")
    print(f"  {len(order)} tasks x {reps} repeats = {len(order) * reps} runs, arm={ARM.name}\n")

    rows, n, total = [], 0, len(order) * reps
    for tid in order:
        t = tasks[tid]
        for rep in range(reps):
            n += 1
            ws = materialise(t)
            USAGE.clear()
            PROVIDER_REQUESTS = 0
            PROVIDER_RETRIES = 0
            t0 = time.time()
            try:
                run = await run_loop(t, ws, ARM, llm, MODEL)
            except Exception as e:
                # run_loop catches llm failures itself and sets ended=llm_error,
                # so anything arriving here is the harness breaking: for
                # example an OSError on a write. Test-action TimeoutExpired is
                # classified inside loop_assignment as candidate verification.
                # Printing and continuing discarded the usage and raw replies
                # and left a runs/ directory silently short of the manifest's
                # count. An abort is a fact about the harness and gets a record.
                # Caught in review by Codex.
                _atomic_write_journal(
                    runs_dir / f"{tid}__{ARM.name}__r{rep}.ABORTED.json",
                    {"task_id": tid, "arm": ARM.name, "rep": rep,
                     "aborted": True, "kind": t["kind"],
                     "exception": type(e).__name__, "detail": str(e)[:500],
                     "seconds": time.time() - t0,
                     "usage": list(USAGE),
                     "provider_requests": PROVIDER_REQUESTS,
                     "provider_retries": PROVIDER_RETRIES,
                     # An abort can follow paid replies and partial edits; it is
                     # still a journal and must preserve the workspace evidence.
                     "final_files": _final_files(ws, t)})
                # A row too, not just a journal. Found 2026-08-23: this branch
                # wrote the record and then `continue`d, so results.json came up
                # short of the manifest's N while still looking like a complete
                # table - the same invariant the grader-error branch below was
                # written to protect, missed one branch up. There is no `run`
                # here, so the row carries only what is known.
                rows.append({"task": tid, "harness": ARM.name, "kind": t["kind"],
                             "rep": rep, "not_a_result": True,
                             "result_status": "harness_aborted",
                             "solved": None, "claimed": None,
                             "ended": None, "steps": 0, "calls": 0,
                             "provider_requests": PROVIDER_REQUESTS,
                             "provider_retries": PROVIDER_RETRIES,
                             "harness_exception": type(e).__name__,
                             "harness_detail": str(e)[:500],
                             "usage_total_tokens": sum(
                                 u.get("total_tokens", 0) for u in USAGE)})
                (OUT / "results.json").write_text(json.dumps(
                    {"manifest": manifest, "rows": rows}, indent=1) + "\n")
                print(f"  [{n}/{total}] {tid} ABORTED {type(e).__name__} "
                      f"(journalled; not a result)", flush=True)
                continue
            # Clean room, not run_tests: the agent's workspace can hold a
            # pytest.py that grades everything green. See grade_clean_room.
            grading_error = None
            report = None
            try:
                # grade_report, not the boolean wrapper. Added 2026-08-23: exit 0
                # is not the grade, and the four original ways a run fails -
                # ordinary failure, all skipped, nothing collected, no report at
                # all - are different facts about the agent that a single bool
                # throws away at the one moment the evidence is being written.
                report = grade_report(ws, t)
                passed, tail = report["all_passed"], report["tail"]
            except subprocess.TimeoutExpired as e:
                # Found 2026-08-23: canonical grading can only time out because
                # candidate code hangs on import or under test. Treating that as
                # grader_error excluded exactly the failure this evaluation is
                # meant to count. Keep no_report true (no verdict arrived) and
                # add the distinct cause instead of overloading that field.
                report = _grading_timeout_report(e)
                passed, tail = False, report["tail"]
            except Exception as e:
                # Found 2026-08-23: this used to sit outside every try. A final
                # pytest timeout discarded the completed, provider-billed run
                # and stopped the rest of the grid. Unlike ABORTED above, run
                # exists here and its complete evidence must be preserved.
                passed, tail = None, None
                grading_error = {"exception": type(e).__name__,
                                 "detail": str(e)[:500]}

            # Journal FIRST. score() is pure and cannot contaminate this, but a
            # scorer that raises must not also destroy the evidence needed to
            # fix it.
            journal = {**dataclasses.asdict(run), "actually_passed": passed,
                       "pytest_tail": tail, "kind": t["kind"], "rep": rep,
                       "usage": list(USAGE),
                       "provider_requests": PROVIDER_REQUESTS,
                       "provider_retries": PROVIDER_RETRIES,
                       "final_files": _final_files(ws, t)}
            if report is not None:
                journal["grading_report"] = report
            if grading_error is not None:
                journal["grading_error"] = grading_error
            _atomic_write_journal(
                runs_dir / f"{tid}__{ARM.name}__r{rep}.json", journal)

            if grading_error is not None:
                # Keep one visible row per manifest cell, but flag this as an
                # infrastructure non-result with solved:null. Omitting it would
                # make a complete N-cell manifest look like an N-cell result set
                # while silently shortening the table.
                row = {"task": run.task_id, "harness": run.harness,
                       "kind": t["kind"], "rep": rep,
                       "not_a_result": True, "result_status": "grader_error",
                       "solved": None, "claimed": run.claimed_success,
                       "ended": run.ended, "steps": len(run.steps),
                       "calls": run.calls,
                       "provider_requests": PROVIDER_REQUESTS,
                       "provider_retries": PROVIDER_RETRIES,
                       "grader_exception": grading_error["exception"],
                       "grader_detail": grading_error["detail"],
                       "usage_total_tokens": sum(
                           u.get("total_tokens", 0) for u in USAGE)}
                rows.append(row)
                (OUT / "results.json").write_text(json.dumps(
                    {"manifest": manifest, "rows": rows}, indent=1) + "\n")
                print(f"  [{n}/{total}] {tid:30s} r{rep} GRADER_ERROR "
                      f"{grading_error['exception']} (run journalled; not a result)",
                      flush=True)
                if n < total:
                    await asyncio.sleep(COOLDOWN)
                continue

            row = assignment_score(run, actually_passed=passed)
            row["kind"], row["claimed"], row["rep"] = t["kind"], run.claimed_success, rep
            row["usage_total_tokens"] = sum(u.get("total_tokens", 0) for u in USAGE)
            row["provider_requests"] = PROVIDER_REQUESTS
            row["provider_retries"] = PROVIDER_RETRIES
            # The four original ways a run failed to pass, plus the timeout cause,
            # kept apart in the row as well as the journal. A reader who sees
            # solved:false is entitled to know whether the suite failed, skipped,
            # collected nothing, never reported, or timed out.
            for flag in ("any_skipped", "nothing_collected",
                         "collection_errored", "no_report",
                         "grading_timed_out"):
                row[flag] = report[flag]
            rows.append(row)
            (OUT / "results.json").write_text(json.dumps(
                {"manifest": manifest, "rows": rows}, indent=1) + "\n")

            status = ",".join(f for f in ("any_skipped", "nothing_collected",
                                          "collection_errored", "no_report",
                                          "grading_timed_out")
                              if report[f])
            print(f"  [{n}/{total}] {tid:30s} r{rep} solved={passed!s:5s} "
                  f"claimed={run.claimed_success!s:5s} cheat={row['cheated']!s:5s} "
                  f"steps={row['steps']:2d} tok={row['usage_total_tokens']:6d} "
                  f"{time.time() - t0:5.0f}s {run.error[:30]}"
                  f"{'  [' + status + ']' if status else ''}", flush=True)
            if n < total:
                await asyncio.sleep(COOLDOWN)

    results = sum(not row.get("not_a_result", False) for row in rows)
    print(f"\n  wrote {results_path}  "
          f"({len(rows)}/{total} cells, {results} results)")
    print(f"  journals in {runs_dir}")


if __name__ == "__main__":
    asyncio.run(main())
