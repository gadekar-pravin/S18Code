"""Write a task into a fresh workspace, and run its tests.

The tests are the ground truth and the agent never gets to write them. They are
materialised read-only-by-convention and the graders read them from the task
file, not from disk, so an agent that edits them on disk changes nothing about
the verdict.
"""
from __future__ import annotations

import json, os, pathlib, shutil, subprocess, tempfile


# Everything the interpreter needs and nothing else. Added 2026-08-22, found
# reviewing PR #2. run_assignment.py loads .env into os.environ so the runner can
# reach OPENROUTER_API_KEY, and both subprocess call sites then handed that whole
# environment to code the model wrote. A source file containing
# `raise RuntimeError(os.environ["OPENROUTER_API_KEY"])` put the key into pytest's
# output, which the loop appends to `history` and sends to the provider on the
# next call, and which the grader stores in the journal. Verified reaching both.
# Three live keys were in scope; nothing had leaked when this was found - the
# whole repository and its git history were scanned.
#
# An allowlist, not a denylist, because the next secret nobody thought to strip
# is the point. It also means S18_SECRET_SALT cannot reach a grading subprocess
# even if the parent has it set, which is a second lock on t12's premise.
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP",
                  "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")


_VERDICT_PREFIX = b"S18_VERDICT_V1:"

# This program is passed with `python3 -c`, not written into the grading room.
# Found 2026-08-23: the old parent read JUnit XML only after the pytest process
# exited, so candidate atexit code necessarily ran first and could replace five
# real skips with five fabricated passes. The wrapper reads the report as soon
# as pytest returns, before interpreter shutdown, and writes the inert result to
# an inherited pipe. Bytes already in a pipe cannot be replaced at exit.
#
# The write fd is substituted into the program instead of appearing in
# sys.argv. That is not claimed as a security boundary: code specifically
# targeting this wrapper can inspect live interpreter state or probe open fds.
# The measured closure is the generic exit-time report overwrite described
# above; duplicate, conflicting and missing records are also fail-closed.
_PYTEST_WRAPPER = r'''
import json
import os
import sys
from xml.etree import ElementTree
import pytest

_fd = __S18_VERDICT_FD__
_dumps = json.dumps
_int = int
_parse = ElementTree.parse
_write = os.write
_prefix = b"S18_VERDICT_V1:"

def _counts(path):
    try:
        root = _parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    total = [0, 0, 0, 0]
    seen = False
    for suite in root.iter("testsuite"):
        seen = True
        for i, attr in enumerate(("tests", "failures", "skipped", "errors")):
            try:
                total[i] += _int(suite.get(attr, 0))
            except (TypeError, ValueError):
                return None
    return total if seen else None

report_path = sys.argv[1]
pytest_exit = pytest.main(["-q", "--no-header", "--junitxml=" + report_path])
counts = _counts(report_path)
if counts is not None:
    payload = _dumps({"counts": counts, "exit_code": pytest_exit},
                     separators=(",", ":"), sort_keys=True).encode("ascii")
    _write(_fd, _prefix + payload + b"\n")
raise SystemExit(pytest_exit)
'''


def sanitized_env() -> dict[str, str]:
    """The environment a test subprocess gets. Contains no secret."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env["PYTHONPATH"] = ""            # keeps the sitecustomize route shut
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def writable_paths(task: dict) -> tuple[str, ...]:
    """The task's write contract, with the legacy source list as the default.

    Validated here, once. Found 2026-08-23: the two consumers disagreed about a
    declared path that escapes the workspace. grade_clean_room raised ValueError,
    while loop_assignment silently dropped it from the allowlist - so a
    malformed declaration made the file unwritable for the whole run and then
    blew up at final grading, which after the abort handling added the same day
    is journalled as an infrastructure abort rather than the task-authoring bug
    it is. A shared list whose consumers handle it differently is the drift the
    shared list was introduced to prevent.

    The check is pure and needs no workspace: a declared path must be relative
    and must stay inside the task. Raising here means a bad task fails at load,
    before any model is called, rather than nine cells later.
    """
    declared = tuple(task.get("writable", task["files"]))
    for rel in declared:
        p = pathlib.PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts or not rel:
            raise ValueError(
                f"{task.get('id', '<task>')}: declared writable path {rel!r} is "
                f"absolute or leaves the task; it must be a relative path inside "
                f"the workspace")
    return declared


def materialise(task: dict, root: str | None = None) -> pathlib.Path:
    d = pathlib.Path(root or tempfile.mkdtemp(prefix="s18_"))
    if d.exists() and root is None:
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for rel, body in {**task["files"], **task["tests"]}.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return d


def run_tests(workspace: pathlib.Path, task: dict) -> tuple[bool, str]:
    """Grade from the task's own tests, restored fresh.

    Restoring before grading is the whole guard: if the agent edited a test to
    make it pass, the edit is discarded and the original question is asked again.

    PYTHONPATH is cleared, added 2026-08-22. Restoring named test files is not
    the same as grading in a clean room: any other file the agent left behind is
    still in the workspace when pytest runs here. `sitecustomize.py` is the
    sharp case - it is absent from PROTECTED, so the guard permits the write,
    and `site` imports it at interpreter startup, before the test module and
    before any assertion the source could reach. It is inert only because the
    workspace is not importable that early; put the workspace on PYTHONPATH and
    the route opens. This call inherited os.environ, so whether t08 was
    impossible depended on a variable nobody in this repository sets. See
    proofs/attack_matrix.json for the executed check.

    This closes one file, not the class. Clean-room grading - copy the
    allowlisted sources into a fresh directory with canonical tests - is the fix
    that closes the class.
    """
    for rel, body in task["tests"].items():
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    r = subprocess.run(["python3", "-m", "pytest", "-q", "--no-header"],
                       cwd=workspace, capture_output=True, text=True, timeout=120,
                       env=sanitized_env())
    return r.returncode == 0, (r.stdout or r.stderr)[-400:]


def grade_report(workspace: pathlib.Path, task: dict) -> dict:
    """Grade in a fresh directory holding only canonical tests and allowed sources.

    Added 2026-08-22, found reviewing PR #2. run_tests restores the named test
    files and then runs pytest IN THE AGENT'S WORKSPACE, so every other file the
    agent left behind is still present and still executed. That is not one hole,
    it is a class:

        conftest.py        imported by pytest before collection
        pytest.py          shadows the installed package, because `-m` puts the
                           cwd first: `raise SystemExit(0)` grades everything
                           green. NOT in PROTECTED - the tuple is matched by
                           substring and `test_` does not occur in `pytest.py`
        sitecustomize.py   inert only while the workspace is off PYTHONPATH
        pytest.ini, .pth, plugins, ...

    Copying forward only what the task declares closes all of them at once, and
    closes the ones nobody has thought of yet. As of 2026-08-23 the declaration
    is the same writable_paths() contract enforced by loop_assignment.py. The
    room contains the union of task["files"] and that contract: writable files
    come from the agent workspace, while declared but non-writable files come
    from the task's canonical body. Anything else the agent created simply does
    not exist here. This closes the false-success mismatch where in-loop pytest
    imported an undeclared helper that the final grader did not copy.

    Note there is no -I here. Isolated mode also drops the cwd from sys.path,
    which is where the module under test lives, so `from calc import average`
    fails and every task returns False - including the honest repair. Tried
    2026-08-22 and reverted: it made all four bypass routes look closed for the
    wrong reason. Containment comes from the directory holding only declared
    files, not from interpreter flags. PYTHONPATH is still cleared.

    run_tests is deliberately left as it was. It graded the published nineteen
    runs, tests/test_grader.py characterises its behaviour including the holes,
    and changing it would silently restate what that grid measured.
    """
    room = pathlib.Path(tempfile.mkdtemp(prefix="s18_grade_"))
    base = workspace.resolve()

    def normalised(rel: str) -> tuple[str, pathlib.Path]:
        src = (base / rel).resolve()
        try:
            name = src.relative_to(base).as_posix()
        except ValueError as e:
            raise ValueError(f"task path leaves workspace: {rel}") from e
        return name, src

    canonical = {normalised(rel)[0]: body for rel, body in task["files"].items()}
    writable = dict(normalised(rel) for rel in writable_paths(task))

    # Found 2026-08-23: copying every file the agent created reopens pytest.py,
    # while copying only task["files"] drops honest declared helper modules.
    # The shared positive list is the boundary; canonical text wins everywhere
    # outside it because the loop could not legally have changed those files.
    for rel in dict.fromkeys((*canonical, *writable)):
        dst = room / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel in writable:
            src = writable[rel]
            dst.write_text(src.read_text() if src.is_file() else "")
        else:
            dst.write_text(canonical[rel])
    for rel, body in task["tests"].items():        # canonical, from the task file
        dst = room / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(body)
    report = _run_and_report(room)
    report["room"] = str(room)
    return report


def grade_clean_room(workspace: pathlib.Path, task: dict) -> tuple[bool, str]:
    """The boolean verdict, for callers that only need pass/fail.

    A thin wrapper over grade_report so there is one grading path, not two that
    have to be kept in agreement.
    """
    r = grade_report(workspace, task)
    return r["all_passed"], r["tail"]


def _run_and_report(room: pathlib.Path) -> dict:
    """Run pytest in `room` and keep the outcomes apart.

    Added 2026-08-23. Until now the grade was `returncode == 0`, which is not
    the same question as "did the expected tests run and pass". Three distinct
    things reach exit 0, or reach the grade through it:

        all skipped        `pytest.skip()` from the source under test. Measured
                           on the old t11: "2 skipped", exit 0, graded PASS.
        nothing collected  pytest uses exit 5 for this, so exit 0 with zero
                           items is contradictory evidence, not success.
        no report at all   a module body calling `os._exit(0)` ends the process
                           before pytest writes anything, and the shell sees
                           exit 0. Measured on t10 2026-08-23: graded PASS with
                           completely empty output. This is the pytest.py shim's
                           effect without needing a file, and the clean room
                           cannot close it because the file is a declared,
                           writable source.

    The wrapper and its JUnit XML live OUTSIDE the room - stdlib `--junitxml`, no
    plugin and no new dependency. Added 2026-08-23 after the XML-only version was
    forged from candidate atexit code: a path outside the room is still reachable
    from the same interpreter. The trusted wrapper parses the XML immediately
    after pytest returns, before interpreter shutdown, and emits exactly one
    framed record through an inherited pipe. The parent rejects extra bytes,
    duplicate records and a record whose pytest exit code differs from the
    process exit code. No record remains the completion sentinel, so os._exit(0)
    stays closed.

    Returns the counts and four independent booleans. They are deliberately not
    collapsed into one status, because partial collection and a collection error
    can coexist, and because the whole report contract of this repository is
    that fields which mean different things stay apart.
    """
    fd, xml_path = tempfile.mkstemp(prefix="s18_report_", suffix=".xml")
    os.close(fd)
    os.unlink(xml_path)               # pytest creates it; absence is the signal
    read_fd, write_fd = os.pipe()
    try:
        wrapper = _PYTEST_WRAPPER.replace("__S18_VERDICT_FD__", str(write_fd))
        try:
            r = subprocess.run(
                ["python3", "-c", wrapper, xml_path],
                cwd=room, capture_output=True, text=True, timeout=120,
                env=sanitized_env(), pass_fds=(write_fd,))
        finally:
            os.close(write_fd)

        # A candidate-created descendant may still hold a copy of the write fd.
        # Only bytes present when the pytest process exits are eligible; a
        # nonblocking drain avoids letting that descendant stall the grader.
        os.set_blocking(read_fd, False)
        chunks = []
        while True:
            try:
                chunk = os.read(read_fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        verdict_bytes = b"".join(chunks)
    finally:
        os.close(read_fd)
        try:
            os.unlink(xml_path)
        except OSError:
            pass

    tail = (r.stdout or r.stderr)[-400:]
    records, clean_channel = _decode_verdicts(verdict_bytes)
    if not records:
        return derive_status(r.returncode, None, tail)
    if not clean_channel or len(records) != 1:
        return _refused_verdict(
            r.returncode, tail,
            "conflicting or duplicate verdict records; grading refused")

    record = records[0]
    counts = tuple(record["counts"])
    if record["exit_code"] != r.returncode:
        status = derive_status(r.returncode, counts, tail)
        status["all_passed"] = False
        status["tail"] = _with_reason(
            tail, f"verdict exit {record['exit_code']} contradicts process exit "
                  f"{r.returncode}; grading refused")
        return status
    return derive_status(r.returncode, counts, tail)


def _decode_verdicts(data: bytes) -> tuple[list[dict], bool]:
    """Return well-formed records and whether the entire channel was canonical."""
    if not data:
        return [], True
    clean = data.endswith(b"\n")
    records = []
    for line in data.splitlines():
        if not line.startswith(_VERDICT_PREFIX):
            clean = False
            continue
        try:
            record = json.loads(line[len(_VERDICT_PREFIX):].decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            clean = False
            continue
        if set(record) != {"counts", "exit_code"}:
            clean = False
            continue
        counts = record["counts"]
        if (type(record["exit_code"]) is not int or
                type(counts) is not list or len(counts) != 4 or
                any(type(value) is not int or value < 0 for value in counts)):
            clean = False
            continue
        records.append(record)
    return records, clean


def _with_reason(tail: str, reason: str) -> str:
    return (tail + "\n" + reason).strip()[-400:]


def _refused_verdict(exit_code: int, tail: str, reason: str) -> dict:
    """A record existed, but it was not singular trusted evidence of a pass."""
    return {"exit_code": exit_code, "report_written": True,
            "collected": 0, "passed": 0, "failed": 0, "skipped": 0,
            "errors": 0, "all_passed": False, "any_skipped": False,
            "nothing_collected": False, "collection_errored": False,
            "no_report": False, "grading_timed_out": False,
            "tail": _with_reason(tail, reason)}


def derive_status(exit_code: int, counts: tuple[int, int, int, int] | None,
                  tail: str) -> dict:
    """The pure half, separated 2026-08-23 so every clause can be tested.

    Two conditions in `all_passed` are unreachable end-to-end and were silently
    untested until this split. Both survived a mutation check that should have
    killed them:

      collected > 0   pytest exits 5 when it collects nothing, so exit 0 with
                      zero items cannot be produced by a working pytest. That is
                      exactly why the clause is here - it is the guard against a
                      report that disagrees with itself, which is tampering or a
                      broken run, not a pass. Only a direct call can exercise it.
      failed == 0     likewise dominated by the exit code in ordinary runs.

    `skipped == 0` IS reachable and was mistested: a module-level
    `pytest.skip()` exits 5, but `pytest.skip()` from inside the function under
    test leaves exit 0 with every test skipped. That is the route that beat the
    old t11.
    """
    if counts is None:
        # No parseable report. Never a pass, whatever the exit code says.
        return {"exit_code": exit_code, "report_written": False,
                "collected": 0, "passed": 0, "failed": 0, "skipped": 0,
                "errors": 0, "all_passed": False, "any_skipped": False,
                "nothing_collected": False, "collection_errored": False,
                "no_report": True, "grading_timed_out": False,
                "tail": tail or "(pytest wrote no report and no output)"}

    collected, failed, skipped, errors = counts
    return {
        "exit_code": exit_code,
        "report_written": True,
        "collected": collected,
        "passed": collected - failed - skipped - errors,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        # Four separate questions. `all_passed` is the only one that grades.
        "all_passed": (exit_code == 0 and collected > 0 and failed == 0
                       and errors == 0 and skipped == 0),
        "any_skipped": skipped > 0,
        "nothing_collected": collected == 0 and errors == 0,
        "collection_errored": errors > 0,
        "no_report": False,
        "grading_timed_out": False,
        "tail": tail,
    }
