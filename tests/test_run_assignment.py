"""Runner-level regression tests for assignment journal durability and cost."""
import asyncio
import importlib
import json
import os
import pathlib
import subprocess
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import pytest

from S18Code.harnesses.base import Step, TaskRun
from S18Code import run_assignment as runner


def test_atomic_journal_uses_flushed_same_directory_temp_before_replace(
        tmp_path, monkeypatch):
    """Found 2026-08-23: an interrupted direct write poisoned no-clobber."""
    path = tmp_path / "runs" / "cell.json"
    path.parent.mkdir()
    path.write_text("old complete journal\n")
    observed = {"flushes": 0}
    real_named_temporary_file = runner.tempfile.NamedTemporaryFile

    class TrackedStream:
        def __init__(self, stream):
            self.stream = stream
            self.name = stream.name

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, value):
            return self.stream.write(value)

        def flush(self):
            observed["flushes"] += 1
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    def tracked_temporary(*args, **kwargs):
        observed["temp_dir"] = pathlib.Path(kwargs["dir"])
        return TrackedStream(real_named_temporary_file(*args, **kwargs))

    def interrupted_replace(source, destination):
        temporary = pathlib.Path(source)
        observed["replace"] = (
            temporary.parent, pathlib.Path(destination), temporary.read_text())
        raise OSError("replace interrupted")

    monkeypatch.setattr(runner.tempfile, "NamedTemporaryFile", tracked_temporary)
    monkeypatch.setattr(runner.os, "replace", interrupted_replace)

    with pytest.raises(OSError, match="replace interrupted"):
        runner._atomic_write_journal(path, {"reply": "irreplaceable"})

    assert (observed, path.read_text(), sorted(path.parent.glob("*.tmp"))) == (
        {"flushes": 1, "temp_dir": path.parent,
         "replace": (path.parent, path,
                     '{\n "reply": "irreplaceable"\n}\n')},
        "old complete journal\n", [])


def test_atomic_journal_serialises_before_creating_a_temp_file(
        tmp_path, monkeypatch):
    path = tmp_path / "runs" / "cell.json"
    path.parent.mkdir()
    opened = []

    def unexpected_temp(*args, **kwargs):
        opened.append(kwargs.get("dir"))
        raise AssertionError("temp created before serialisation succeeded")

    monkeypatch.setattr(runner.tempfile, "NamedTemporaryFile", unexpected_temp)
    with pytest.raises(TypeError, match="not JSON serializable"):
        runner._atomic_write_journal(path, {"bad": object()})
    assert (opened, path.exists()) == ([], False)


def test_grader_timeout_is_counted_failure_and_continues_grid(tmp_path, monkeypatch, capsys):
    out = tmp_path / "assignment"
    monkeypatch.setattr(runner, "OUT", out)
    monkeypatch.setattr(runner, "COOLDOWN", 0)
    monkeypatch.setattr(runner, "preflight", lambda: "pytest test-version")
    monkeypatch.setenv("S18_REPEATS", "1")
    monkeypatch.setattr(sys, "argv", ["run_assignment.py",
                                      "t10_source_repair_average",
                                      "t11_integrity_parity_lock"])

    calls = 0
    counts_at_cell_start = []
    long_final_file = "# complete journal evidence\n" + "x = 1\n" * 900

    async def fake_run_loop(task, ws, cfg, llm, model):
        nonlocal calls
        counts_at_cell_start.append((runner.PROVIDER_REQUESTS, runner.PROVIDER_RETRIES))
        calls += 1
        runner.PROVIDER_REQUESTS += 2
        runner.PROVIDER_RETRIES += 1
        runner.USAGE.append({"total_tokens": calls, "raw": f"reply-{calls}"})
        if task["id"] == "t10_source_repair_average":
            (ws / "calc.py").write_text(long_final_file)
        return TaskRun(task_id=task["id"], harness=cfg.name, model=model,
                       steps=[Step("edit", next(iter(task["files"])), True),
                              Step("answer", detail="done")],
                       claimed_success=True, calls=1, ended="done")

    def fake_grade(ws, task):
        if task["id"] == "t10_source_repair_average":
            raise subprocess.TimeoutExpired(["python3", "-m", "pytest"], 120)
        return {"exit_code": 1, "report_written": True, "collected": 1,
                "passed": 0, "failed": 1, "skipped": 0, "errors": 0,
                "all_passed": False, "any_skipped": False,
                "nothing_collected": False, "collection_errored": False,
                "no_report": False, "grading_timed_out": False,
                "tail": "assertion failed"}

    class _ReachedTheProvider(BaseException):
        """Deliberately not an Exception.

        run_loop wraps its llm call in `except Exception`, which converts any
        failure into ended="llm_error" and swallows the message - so an
        AssertionError here produced an unrelated dict mismatch instead of
        saying what went wrong. Verified 2026-08-23 by dropping the run_loop
        patch. BaseException escapes that handler.
        """

    def _explode(prompt, system):
        # The test's own safety property, made mutation-checkable. main() is
        # patched at run_loop, so the real llm must never be reached; if a later
        # refactor makes that patch stop applying, this goes red instead of
        # billing a call to a hosted model from inside pytest.
        raise _ReachedTheProvider("test reached the real model path")

    monkeypatch.setattr(runner, "llm", _explode)
    monkeypatch.setattr(runner, "run_loop", fake_run_loop)
    monkeypatch.setattr(runner, "grade_report", fake_grade)

    asyncio.run(runner.main())

    journal = json.loads((out / "runs" /
        "t10_source_repair_average__s17_rules__r0.json").read_text())
    assert {
        "actually_passed": journal["actually_passed"],
        "pytest_tail": journal["pytest_tail"],
        "steps": journal["steps"],
        "usage": journal["usage"],
        "provider_counts": (journal["provider_requests"], journal["provider_retries"]),
        "final_files": journal["final_files"],
        "timed_out": journal["grading_report"]["grading_timed_out"],
        "no_report": journal["grading_report"]["no_report"],
    } == {
        "actually_passed": False,
        "pytest_tail": "pytest timed out after 120 seconds",
        "steps": [{"kind": "edit", "target": "calc.py", "ok": True, "detail": ""},
                  {"kind": "answer", "target": "", "ok": True, "detail": "done"}],
        "usage": [{"total_tokens": 1, "raw": "reply-1"}],
        "provider_counts": (2, 1),
        "final_files": {"calc.py": long_final_file},
        "timed_out": True,
        "no_report": True,
    }

    results = json.loads((out / "results.json").read_text())["rows"]
    assert [(row["task"], row.get("not_a_result"), row["solved"],
             row["grading_timed_out"])
            for row in results] == [
        ("t10_source_repair_average", None, False, True),
        ("t11_integrity_parity_lock", None, False, False),
    ]
    output = capsys.readouterr().out
    assert "solved=False" in output and "grading_timed_out" in output
    assert counts_at_cell_start == [(0, 0), (0, 0)]


def test_non_timeout_grader_exception_remains_not_a_result(
        tmp_path, monkeypatch, capsys):
    """Found 2026-08-23: only candidate-caused timeouts move categories."""
    out = tmp_path / "assignment"
    monkeypatch.setattr(runner, "OUT", out)
    monkeypatch.setattr(runner, "COOLDOWN", 0)
    monkeypatch.setattr(runner, "preflight", lambda: "pytest test-version")
    monkeypatch.setenv("S18_REPEATS", "1")
    monkeypatch.setattr(sys, "argv", ["run_assignment.py",
                                      "t10_source_repair_average"])

    async def fake_run_loop(task, ws, cfg, llm, model):
        runner.USAGE.append({"total_tokens": 8, "raw": "reply"})
        return TaskRun(task_id=task["id"], harness=cfg.name, model=model,
                       steps=[Step("answer", detail="done")],
                       claimed_success=True, calls=1, ended="done")

    def broken_grader(ws, task):
        raise OSError("grader implementation broke")

    monkeypatch.setattr(runner, "run_loop", fake_run_loop)
    monkeypatch.setattr(runner, "grade_report", broken_grader)
    asyncio.run(runner.main())

    journal = json.loads((out / "runs" /
        "t10_source_repair_average__s17_rules__r0.json").read_text())
    row = json.loads((out / "results.json").read_text())["rows"][0]
    assert (journal["actually_passed"], journal["usage"][0]["total_tokens"],
            journal["grading_error"]["exception"]) == (None, 8, "OSError")
    assert (row["not_a_result"], row["result_status"], row["solved"],
            row["grader_exception"]) == (True, "grader_error", None, "OSError")
    assert "GRADER_ERROR OSError (run journalled; not a result)" in capsys.readouterr().out


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_provider_reply_is_kept_complete_for_the_journal(monkeypatch):
    """Found 2026-08-23: 4,000 characters silently replaced the paid reply."""
    reply = '{"action":"done","success":false,"note":"' + "x" * 5000 + '"}'
    monkeypatch.setattr(runner.urllib.request, "urlopen",
                        lambda req, timeout: _Response())
    monkeypatch.setattr(runner.json, "load", lambda response: {
        "choices": [{"message": {"content": reply}}],
        "usage": {"total_tokens": 123},
    })
    runner.USAGE.clear()
    runner.PROVIDER_REQUESTS = 0
    runner.PROVIDER_RETRIES = 0

    returned = asyncio.run(runner.llm("prompt", "system"))

    assert (returned, runner.USAGE) == (
        reply, [{"total_tokens": 123, "reasoning_chars": 0, "raw": reply}])


def test_final_files_include_complete_nested_and_missing_declared_paths(tmp_path):
    """Found 2026-08-23: glob-plus-slice lost three kinds of file evidence."""
    task = {"id": "t_journal", "files": {"nested/calc.py": "initial\n"},
            "tests": {},
            "writable": ["nested/calc.py", "created/later.py"]}
    workspace = runner.materialise(task, root=str(tmp_path / "workspace"))
    complete = "# longer than the old cap\n" + "value = 1\n" * 500
    (workspace / "nested" / "calc.py").write_text(complete)

    assert runner._final_files(workspace, task) == {
        "nested/calc.py": complete, "created/later.py": None}


def test_provider_retries_are_counted_without_changing_model_turns(tmp_path, monkeypatch):
    task = {"id": "t_retry", "prompt": "stop",
            "files": {"calc.py": "x = 1\n"},
            "tests": {"tests/test_calc.py": "def test_ok(): assert True\n"}}
    ws = runner.materialise(task, root=str(tmp_path / "ws"))
    attempts = 0

    def fake_urlopen(req, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(runner.ENDPOINT, 500, "server", {}, None)
        return _Response()

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(runner.json, "load", lambda response: {
        "choices": [{"message": {"content":
            '{"action":"done","success":false,"note":"n"}'}}],
        "usage": {"total_tokens": 9},
    })
    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    runner.USAGE.clear()
    runner.PROVIDER_REQUESTS = 0
    runner.PROVIDER_RETRIES = 0

    run = asyncio.run(runner.run_loop(task, ws, runner.ARM, runner.llm, runner.MODEL))

    assert {
        "model_turns": run.calls,
        "requests": runner.PROVIDER_REQUESTS,
        "retries": runner.PROVIDER_RETRIES,
        "attempts": attempts,
        "usage_entries": len(runner.USAGE),
        "ended": run.ended,
    } == {"model_turns": 1, "requests": 2, "retries": 1,
          "attempts": 2, "usage_entries": 1, "ended": "done"}


def test_permanent_http_error_is_not_retried(monkeypatch):
    attempts = 0
    sleeps = []

    def unauthorized(req, timeout):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(runner.ENDPOINT, 401, "unauthorized", {}, None)

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(runner.urllib.request, "urlopen", unauthorized)
    monkeypatch.setattr(runner.asyncio, "sleep", record_sleep)
    runner.USAGE.clear()
    runner.PROVIDER_REQUESTS = 0
    runner.PROVIDER_RETRIES = 0

    try:
        asyncio.run(runner.llm("prompt", "system"))
    except RuntimeError as e:
        error = str(e)
    else:
        error = "no error"

    assert (attempts, runner.PROVIDER_REQUESTS, runner.PROVIDER_RETRIES,
            sleeps, error) == (1, 1, 0, [], "openrouter unavailable: HTTP 401")


def test_importing_the_runner_binds_no_key_and_touches_no_environment():
    """Importing this module must have no effect on os.environ.

    Added 2026-08-23. Making the module importable - which is what let the
    grader-failure path above be tested at all - meant `import
    S18Code.run_assignment` ran _load_dotenv(), and this file imports it at
    collection. Measured before the fix: three names (OPENROUTER_API_KEY,
    GEMINI_API_KEY_1, GEMINI_API_KEY_2) were added to os.environ of the pytest
    process before a single test ran, conditional on a gitignored file that
    exists only on the author's machine. That is the defect class
    _ENV_ALLOWLIST closed one layer down, re-entering through the door
    testability opened.

    The `KEY` half is what makes the suite fail-closed: with no key bound at
    import, an llm() that is reached by mistake finds nothing and the run
    records ended="llm_error" rather than billing a real call.
    """
    assert not hasattr(runner, "KEY"), (
        "a module-level key binding is back; llm() must read it at call time")

    before = set(os.environ)
    importlib.reload(runner)
    added = sorted(set(os.environ) - before)
    assert added == [], f"import added {added} to os.environ"


def test_the_dotenv_loader_still_works_where_it_is_supposed_to(tmp_path, monkeypatch):
    """The other half: proving the load moved, not that it disappeared.

    Without this, the test above is satisfied by deleting _load_dotenv outright,
    which would break every real run.
    """
    monkeypatch.delenv("S18_FAKE_DOTENV_NAME", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('S18_FAKE_DOTENV_NAME="from-the-file"\n')

    runner._load_dotenv(env_file)
    assert os.environ["S18_FAKE_DOTENV_NAME"] == "from-the-file"
    monkeypatch.delenv("S18_FAKE_DOTENV_NAME", raising=False)


def test_preflight_is_the_thing_that_loads_dotenv(monkeypatch):
    """preflight() must call the loader, or a real grid finds no key."""
    called = []
    monkeypatch.setattr(runner, "_load_dotenv", lambda *a: called.append(True))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-NOT-REAL")
    monkeypatch.delenv("S18_SECRET_SALT", raising=False)
    runner.preflight()
    assert called == [True], "preflight() no longer loads .env"


def test_a_harness_abort_still_produces_a_row(tmp_path, monkeypatch, capsys):
    """Found 2026-08-23: the abort branch journalled the record and continued
    without appending a row, so results.json came up short of the manifest's N
    while still reading as a complete table. Same invariant the grader-error
    branch protects, missed one branch up.
    """
    out = tmp_path / "assignment"
    monkeypatch.setattr(runner, "OUT", out)
    monkeypatch.setattr(runner, "COOLDOWN", 0)
    monkeypatch.setattr(runner, "preflight", lambda: "pytest test-version")
    monkeypatch.setenv("S18_REPEATS", "1")
    monkeypatch.setattr(sys, "argv", ["run_assignment.py",
                                      "t10_source_repair_average",
                                      "t11_integrity_parity_lock"])
    journal_writes = []
    real_atomic_write = runner._atomic_write_journal

    def record_atomic_write(path, record):
        journal_writes.append(path.name)
        real_atomic_write(path, record)

    monkeypatch.setattr(runner, "_atomic_write_journal", record_atomic_write)

    async def fake_run_loop(task, ws, cfg, llm, model):
        runner.USAGE.append({"total_tokens": 7, "raw": "r"})
        if task["id"] == "t10_source_repair_average":
            raise OSError("workspace write broke")
        return TaskRun(task_id=task["id"], harness=cfg.name, model=model,
                       steps=[Step("answer", detail="done")],
                       claimed_success=False, calls=1, ended="done")

    def fake_grade(ws, task):
        return {"exit_code": 1, "report_written": True, "collected": 1,
                "passed": 0, "failed": 1, "skipped": 0, "errors": 0,
                "all_passed": False, "any_skipped": False,
                "nothing_collected": False, "collection_errored": False,
                "no_report": False, "grading_timed_out": False, "tail": "nope"}

    monkeypatch.setattr(runner, "run_loop", fake_run_loop)
    monkeypatch.setattr(runner, "grade_report", fake_grade)
    asyncio.run(runner.main())

    rows = json.loads((out / "results.json").read_text())["rows"]
    aborted_journal = json.loads((out / "runs" /
        "t10_source_repair_average__s17_rules__r0.ABORTED.json").read_text())
    assert len(rows) == 2, "one row per manifest cell, abort included"
    aborted = rows[0]
    assert (aborted["task"], aborted["not_a_result"], aborted["result_status"],
            aborted["solved"], aborted["harness_exception"]) == (
        "t10_source_repair_average", True, "harness_aborted", None,
        "OSError")
    # The billed reply and partial workspace are still evidence on an abort.
    assert (aborted["usage_total_tokens"], aborted_journal["final_files"]) == (
        7, {"calc.py":
            "def average(numbers):\n"
            '    """Arithmetic mean. Returns 0 for an empty list."""\n'
            "    return sum(numbers) / len(numbers)\n"})
    assert (out / "runs" /
            "t10_source_repair_average__s17_rules__r0.ABORTED.json").is_file()
    assert "ABORTED OSError (journalled; not a result)" in capsys.readouterr().out
    assert journal_writes == [
        "t10_source_repair_average__s17_rules__r0.ABORTED.json",
        "t11_integrity_parity_lock__s17_rules__r0.json"]


def test_startup_replaces_stale_derived_results_before_any_cell(
        tmp_path, monkeypatch, capsys):
    """Found 2026-08-23: runs/ recovery left the old results table behind."""
    out = tmp_path / "assignment"
    (out / "runs").mkdir(parents=True)
    (out / "results.json").write_text(json.dumps(
        {"manifest": {"old": True}, "rows": [{"task": "stale"}]}) + "\n")
    monkeypatch.setattr(runner, "OUT", out)
    monkeypatch.setattr(runner, "preflight", lambda: "pytest test-version")
    monkeypatch.setattr(runner, "_git_provenance", lambda: {
        "git_commit": "abc123", "git_dirty": False, "git_error": None})
    monkeypatch.setenv("S18_REPEATS", "0")
    monkeypatch.setattr(sys, "argv", ["run_assignment.py",
                                      "t10_source_repair_average"])

    asyncio.run(runner.main())

    written = json.loads((out / "results.json").read_text())
    output = capsys.readouterr().out
    assert (written["rows"], written["manifest"]["tasks"],
            "wrote" in output, "(0/0 cells, 0 results)" in output) == (
                [], ["t10_source_repair_average"], True, True)


def test_duplicate_task_ids_are_rejected_before_the_grid_starts(
        tmp_path, monkeypatch):
    """Found 2026-08-23: duplicate cells overwrite the same journal name."""
    monkeypatch.setattr(runner, "OUT", tmp_path / "assignment")
    monkeypatch.setattr(runner, "preflight", lambda: "pytest test-version")
    monkeypatch.setenv("S18_REPEATS", "0")
    monkeypatch.setattr(sys, "argv", ["run_assignment.py",
                                      "t10_source_repair_average",
                                      "t10_source_repair_average"])

    with pytest.raises(
            SystemExit,
            match=r"duplicate task ids: \['t10_source_repair_average'\]"):
        asyncio.run(runner.main())


def test_git_provenance_is_explicit_when_present_or_unavailable(monkeypatch):
    """Found 2026-08-23: a harness filename did not identify its code."""
    replies = iter([
        subprocess.CompletedProcess([], 0, stdout="d8a5c90\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=" M run_assignment.py\n", stderr=""),
    ])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: next(replies))
    present = runner._git_provenance()
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git missing")))
    missing = runner._git_provenance()
    monkeypatch.setattr(runner, "_git_provenance", lambda: present)
    manifest = runner.freeze_manifest({}, "pytest test-version")

    assert (present, missing,
            {key: manifest[key] for key in present}) == (
        {"git_commit": "d8a5c90", "git_dirty": True, "git_error": None},
        {"git_commit": None, "git_dirty": None,
         "git_error": "FileNotFoundError: git missing"},
        {"git_commit": "d8a5c90", "git_dirty": True, "git_error": None})


def test_out_honours_s18_out_so_a_follow_up_grid_leaves_the_first_alone(
        tmp_path, monkeypatch):
    """Added 2026-08-23. The no-clobber recovery says move runs/ aside, but the
    first grid is committed evidence that cards point at: moving it turns
    test_the_card_matches_the_grid_it_claims_to_describe red. A follow-up grid
    takes its own directory instead."""
    default = importlib.reload(runner).OUT
    assert default.name == "assignment_v1", "the default must not move"

    monkeypatch.setenv("S18_OUT", str(tmp_path / "followup"))
    try:
        assert importlib.reload(runner).OUT == tmp_path / "followup"
    finally:
        monkeypatch.delenv("S18_OUT", raising=False)
        importlib.reload(runner)
    assert runner.OUT == default, "the override must not leak into later runs"


def test_model_honours_s18_model_so_a_named_model_gets_its_own_manifest(
        monkeypatch):
    """Added 2026-08-23, to run a named open-weight model against a task the
    cloaked default already covered.

    The default must not move. proofs/assignment_v1 and
    proofs/assignment_v1_t12x6 were produced by stealth/ox-alpha, and a changed
    default would make the next grid silently incomparable to them under the
    same name - the same reasoning that pins DEFAULT_VERIFICATION_RULE. The
    model string is frozen into the manifest, so whether two grids are poolable
    stays a question the evidence answers rather than one a reader assumes.
    """
    default = importlib.reload(runner).MODEL
    assert default == "stealth/ox-alpha", "the default must not move"

    monkeypatch.setenv("S18_MODEL", "qwen/qwen3.8-27b")
    try:
        reloaded = importlib.reload(runner)
        assert reloaded.MODEL == "qwen/qwen3.8-27b"
        # and it reaches the frozen manifest, not just the request body
        manifest = reloaded.freeze_manifest({}, "pytest 9.1.1")
        assert manifest["model"] == "qwen/qwen3.8-27b"
    finally:
        monkeypatch.delenv("S18_MODEL", raising=False)
        importlib.reload(runner)
    assert runner.MODEL == default, "the override must not leak into later runs"
    assert runner.freeze_manifest({}, "pytest 9.1.1")["model"] == default
