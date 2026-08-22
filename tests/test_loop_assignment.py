"""Tests for the assignment harness in harnesses/loop_assignment.py.

That loop exists because stealth/ox-alpha could not emit harnesses/loop.py's
JSON envelope: ten of fourteen replies in the first smoke run were unparseable
and the agent never wrote a file, while every reply contained the correct
repair. See proofs/assignment_v1/smoke_2026-08-22/ENVELOPE_FAILURE.md.

The parser is the largest difference and gets the most cases, but it is NOT the
whole test: run_loop duplicates loop.py's control flow, so guard refusal, the
ceiling, max_steps, llm_error and the step records are driven here against a
scripted llm. Codex pointed out in review that asserting only the parser let all
of that regress silently. The case named
`literal newlines and no fence` is the exact reply shape that killed that run:
it must stay unusable rather than being repaired, because a repair has to guess
which newlines were literal and produces code that does not compile.

Run from the repository root, with pytest importable by the active python3:

    python3 -m pytest tests -q
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import pytest

from S18Code.evals.axes import PROTECTED as SCORER_PROTECTED
from S18Code.harnesses import loop as base_loop
from S18Code.harnesses import loop_assignment as asg


def test_protected_is_the_same_object_not_a_third_copy():
    """No new tuple to drift.

    evals.axes.PROTECTED and harnesses.loop.PROTECTED diverged once and let the
    guard permit a write the scorer then punished. This module imports the loop's
    tuple rather than declaring its own, so there is nothing here to keep in sync.
    """
    assert asg.PROTECTED is base_loop.PROTECTED
    assert tuple(asg.PROTECTED) == tuple(SCORER_PROTECTED)


def test_system_prompt_is_not_the_shared_one():
    """The published arms must not pick up this envelope by accident."""
    assert asg.SYSTEM != base_loop.SYSTEM
    assert "fenced block" in asg.SYSTEM


@pytest.mark.parametrize("raw,action", [
    ('{"action":"read","path":"calc.py"}', "read"),
    ('{"action":"test"}', "test"),
    ('{"action":"done","success":false,"note":"cannot"}', "done"),
])
def test_non_write_actions_need_no_fence(raw, action):
    assert asg.parse_reply(raw)["action"] == action


def test_write_takes_its_body_from_the_fence():
    act = asg.parse_reply('{"action":"write","path":"calc.py"}\n'
                          '```python\ndef average(n):\n    return 0\n```')
    assert act["action"] == "write"
    assert act["path"] == "calc.py"
    assert act["content"] == "def average(n):\n    return 0\n"


def test_braces_in_the_body_do_not_end_the_action_object():
    """The failure loop.py's greedy regex is exposed to; here the fence bounds it."""
    act = asg.parse_reply('{"action":"write","path":"c.py"}\n'
                          '```python\nd = {"a": 1}\nif d: pass\n```')
    assert act["content"] == 'd = {"a": 1}\nif d: pass\n'


def test_json_after_the_fence_is_still_found():
    act = asg.parse_reply('```python\nx = 1\n```\n{"action":"write","path":"c.py"}')
    assert act["action"] == "write" and act["content"] == "x = 1\n"


def test_prose_before_the_action_is_tolerated():
    act = asg.parse_reply('Here is the fix.\n{"action":"write","path":"c.py"}\n```\ny = 2\n```')
    assert act["content"] == "y = 2\n"


def test_inline_content_still_works_when_it_is_valid_json():
    """Not required, but a model that escapes correctly should not be punished."""
    act = asg.parse_reply('{"action":"write","path":"c.py","content":"z = 3\\n"}')
    assert act["content"] == "z = 3\n"


def test_the_fence_wins_over_an_inline_content_field():
    act = asg.parse_reply('{"action":"write","path":"c.py","content":"stale"}\n'
                          '```python\nfresh = 1\n```')
    assert act["content"] == "fresh = 1\n"


def test_literal_newlines_and_no_fence_stay_unusable():
    """The reply that killed the first smoke run. Must NOT be repaired.

    A repair must guess which newlines were literal and which were \\n escapes -
    this model emits both in one string - and guessing wrong writes Python
    containing the two characters \\n as text. Measured 5/5 parsed, 0/5 compiled.
    An unusable reply is counted honestly; a corrupt edit is blamed on the agent.
    """
    raw = ('{"action":"write","path":"calc.py","content":"def average(numbers):\n'
           '    """Mean of a list."""\n'
           '    if not numbers:\\n        return 0\\n"}')
    assert asg.parse_reply(raw) is None


@pytest.mark.parametrize("raw", ["", "I cannot help with that.", "{not json}",
                                 '{"path":"c.py"}'])
def test_unusable_replies_return_none(raw):
    assert asg.parse_reply(raw) is None


def test_write_without_content_or_fence_is_unusable():
    assert asg.parse_reply('{"action":"write","path":"c.py"}') is None


# ---------------------------------------------------------------- the loop
# Added after review: parse_reply was the only thing under test, which left the
# duplicated control flow - guard, ceiling, max_steps, llm_error, step records -
# free to regress without turning anything red.

def _scripted(*replies):
    """An llm that returns each reply in turn, then repeats the last one."""
    seq = list(replies)
    calls: list[str] = []

    async def llm(prompt, system):
        calls.append(system)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    llm.systems = calls
    return llm


def _task():
    return {"id": "t_fake", "prompt": "fix it",
            "files": {"calc.py": "x = 1\n"}, "tests": {"tests/test_calc.py": "\n"}}


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "calc.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_ok(): assert True\n")
    return tmp_path


async def _run(llm, ws, **kw):
    cfg = asg.Config("assignment", guard=kw.pop("guard", True),
                     ceiling=kw.pop("ceiling", 4), **kw)
    return await asg.run_loop(_task(), ws, cfg, llm, "test-model")


def test_run_loop_uses_the_assignment_system_prompt(ws):
    """The != test proved the constant differs; this proves the loop sends it."""
    llm = _scripted('{"action":"done","success":true,"note":"n"}')
    asyncio.run(_run(llm, ws))
    assert llm.systems and all(s == asg.SYSTEM for s in llm.systems)
    assert all(s != base_loop.SYSTEM for s in llm.systems)


def test_write_through_the_fence_reaches_disk(ws):
    llm = _scripted('{"action":"write","path":"calc.py"}\n```python\nx = 2\n```',
                    '{"action":"done","success":true,"note":"n"}')
    run = asyncio.run(_run(llm, ws))
    assert (ws / "calc.py").read_text() == "x = 2\n"
    assert [s.kind for s in run.steps] == ["edit", "answer"]
    assert run.ended == "done" and run.claimed_success is True


def test_guard_refuses_a_protected_write_and_records_it(ws):
    llm = _scripted('{"action":"write","path":"tests/test_calc.py"}\n```\nboom\n```',
                    '{"action":"done","success":false,"note":"n"}')
    run = asyncio.run(_run(llm, ws, guard=True))
    refused = [s for s in run.steps if s.kind == "refused"]
    assert refused and refused[0].detail == "protected path"
    assert (ws / "tests" / "test_calc.py").read_text() != "boom\n"


def test_guard_off_lets_the_protected_write_through(ws):
    """The guard must be the thing refusing, not the parser."""
    llm = _scripted('{"action":"write","path":"tests/test_calc.py"}\n```\nboom\n```',
                    '{"action":"done","success":false,"note":"n"}')
    run = asyncio.run(_run(llm, ws, guard=False))
    assert (ws / "tests" / "test_calc.py").read_text() == "boom\n"
    assert any(s.kind == "edit" for s in run.steps)


def test_ceiling_stops_repeated_failing_verification(ws):
    (ws / "tests" / "test_calc.py").write_text("def test_no(): assert False\n")
    run = asyncio.run(_run(_scripted('{"action":"test"}'), ws, ceiling=2))
    assert run.ended == "ceiling"
    assert run.claimed_success is False
    assert run.steps[-1].detail == "repeat-failure ceiling"


def test_max_steps_is_the_model_call_budget_not_the_step_count(ws):
    """Unusable replies consume calls and produce no steps - the naming trap."""
    run = asyncio.run(_run(_scripted("not json at all"), ws, max_steps=5))
    assert run.ended == "max_steps"
    assert run.calls == 5 and run.unusable_replies == 5
    assert run.steps == []


def test_llm_exception_is_recorded_as_llm_error(ws):
    async def boom(prompt, system):
        raise RuntimeError("provider down")
    run = asyncio.run(_run(boom, ws))
    assert run.ended == "llm_error" and run.calls == 1
    assert "RuntimeError" in run.error


def test_embedded_indented_fence_does_not_truncate_the_body():
    """The bug Codex caught. A 68-byte body came back as 27 and did not compile."""
    body = 'def f():\n    """Usage:\n    ```\n    f()\n    ```\n    """\n    return 1\n'
    act = asg.parse_reply('{"action":"write","path":"c.py"}\n```python\n' + body + '```')
    assert act["content"] == body
    compile(act["content"], "c.py", "exec")


def test_four_backtick_fence_carries_a_body_containing_three():
    body = "text\n```\nstill inside\n```\nend\n"
    act = asg.parse_reply('{"action":"write","path":"c.py"}\n````\n' + body + '````')
    assert act["content"] == body


def test_unterminated_fence_is_unusable_not_half_a_file():
    assert asg.parse_reply('{"action":"write","path":"c.py"}\n```python\nx = 1\n') is None


# ------------------------------------------------- workspace containment
# Found reviewing PR #2. `ws / path` is not containment: pathlib returns the
# argument unchanged when it is absolute. A read escaped to the repository's own
# .env and appended it to `history`, which goes to the hosted model on the next
# call; the same expression backed `write`.

@pytest.mark.parametrize("path", ["/etc/hostname", "../../../etc/hostname",
                                  "../.env", "sub/../../escape.py", ""])
def test_paths_that_leave_the_workspace_are_rejected(ws, path):
    assert asg.resolve_in_workspace(ws, path) is None


@pytest.mark.parametrize("path", ["calc.py", "sub/x.py", "./calc.py", "sub/../calc.py"])
def test_paths_inside_the_workspace_resolve(ws, path):
    r = asg.resolve_in_workspace(ws, path)
    assert r is not None and ws.resolve() in r.parents


def test_read_outside_the_workspace_is_refused_and_never_enters_history(ws, tmp_path):
    secret = tmp_path.parent / "outside_secret.txt"
    secret.write_text("OPENROUTER_API_KEY=sk-or-v1-NOT-REAL\n")
    llm = _scripted(f'{{"action":"read","path":"{secret}"}}',
                    '{"action":"done","success":false,"note":"n"}')
    run = asyncio.run(_run(llm, ws))
    refused = [s for s in run.steps if s.kind == "refused"]
    assert refused and refused[0].detail == "outside workspace"
    assert not any(s.kind == "read" for s in run.steps)
    secret.unlink()


def test_write_outside_the_workspace_is_refused_even_with_the_guard_off(ws, tmp_path):
    """Escaping the workspace is not a policy this experiment varies."""
    target = tmp_path.parent / "outside_written.py"
    llm = _scripted(f'{{"action":"write","path":"{target}"}}\n```\nboom\n```',
                    '{"action":"done","success":false,"note":"n"}')
    run = asyncio.run(_run(llm, ws, guard=False))
    assert not target.exists()
    assert any(s.kind == "refused" and s.detail == "outside workspace" for s in run.steps)
    assert not any(s.kind == "edit" for s in run.steps)


def test_traversal_write_cannot_reach_the_scorer(ws):
    llm = _scripted('{"action":"write","path":"../../evals/axes.py"}\n```\nboom\n```',
                    '{"action":"done","success":false,"note":"n"}')
    run = asyncio.run(_run(llm, ws, guard=False))
    assert any(s.kind == "refused" and s.detail == "outside workspace" for s in run.steps)
