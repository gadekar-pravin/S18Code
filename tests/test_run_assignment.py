"""Runner-level regression tests for assignment journal durability and cost."""
import asyncio
import json
import pathlib
import subprocess
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from S18Code.harnesses.base import Step, TaskRun
from S18Code import run_assignment as runner


def test_grader_timeout_preserves_run_and_continues_grid(tmp_path, monkeypatch, capsys):
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

    async def fake_run_loop(task, ws, cfg, llm, model):
        nonlocal calls
        counts_at_cell_start.append((runner.PROVIDER_REQUESTS, runner.PROVIDER_RETRIES))
        calls += 1
        runner.PROVIDER_REQUESTS += 2
        runner.PROVIDER_RETRIES += 1
        runner.USAGE.append({"total_tokens": calls, "raw": f"reply-{calls}"})
        return TaskRun(task_id=task["id"], harness=cfg.name, model=model,
                       steps=[Step("edit", next(iter(task["files"])), True),
                              Step("answer", detail="done")],
                       claimed_success=True, calls=1, ended="done")

    def fake_grade(ws, task):
        if task["id"] == "t10_source_repair_average":
            raise subprocess.TimeoutExpired(["python3", "-m", "pytest"], 120)
        return False, "assertion failed"

    monkeypatch.setattr(runner, "run_loop", fake_run_loop)
    monkeypatch.setattr(runner, "grade_clean_room", fake_grade)

    asyncio.run(runner.main())

    journal = json.loads((out / "runs" /
        "t10_source_repair_average__s17_rules__r0.json").read_text())
    assert {
        "actually_passed": journal["actually_passed"],
        "pytest_tail": journal["pytest_tail"],
        "steps": journal["steps"],
        "usage": journal["usage"],
        "provider_counts": (journal["provider_requests"], journal["provider_retries"]),
        "final_files": sorted(journal["final_files"]),
        "error_type": journal["grading_error"]["exception"],
        "bounded_detail": len(journal["grading_error"]["detail"]) <= 500,
    } == {
        "actually_passed": None,
        "pytest_tail": None,
        "steps": [{"kind": "edit", "target": "calc.py", "ok": True, "detail": ""},
                  {"kind": "answer", "target": "", "ok": True, "detail": "done"}],
        "usage": [{"total_tokens": 1, "raw": "reply-1"}],
        "provider_counts": (2, 1),
        "final_files": ["calc.py"],
        "error_type": "TimeoutExpired",
        "bounded_detail": True,
    }

    results = json.loads((out / "results.json").read_text())["rows"]
    assert [(row["task"], row.get("not_a_result"), row["solved"])
            for row in results] == [
        ("t10_source_repair_average", True, None),
        ("t11_integrity_parity_lock", None, False),
    ]
    assert "GRADER_ERROR TimeoutExpired (run journalled; not a result)" in capsys.readouterr().out
    assert counts_at_cell_start == [(0, 0), (0, 0)]


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


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
