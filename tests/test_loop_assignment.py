"""Tests for the assignment harness in harnesses/loop_assignment.py.

That loop exists because stealth/ox-alpha could not emit harnesses/loop.py's
JSON envelope: ten of fourteen replies in the first smoke run were unparseable
and the agent never wrote a file, while every reply contained the correct
repair. See proofs/assignment_v1/smoke_2026-08-22/ENVELOPE_FAILURE.md.

The parser is the whole difference, so it is the whole test. The case named
`literal newlines and no fence` is the exact reply shape that killed that run:
it must stay unusable rather than being repaired, because a repair has to guess
which newlines were literal and produces code that does not compile.

Run from the repository root, with pytest importable by the active python3:

    python3 -m pytest tests -q
"""
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
