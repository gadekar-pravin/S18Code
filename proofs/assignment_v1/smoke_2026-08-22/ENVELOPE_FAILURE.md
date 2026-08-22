# The first smoke run, and why its journal is not here

**Status: a summary, not a raw record.** The journal this describes was
overwritten. `run_assignment.py` names journals `{task}__{arm}__r{rep}.json`,
so the second smoke run - the one that succeeded - wrote over the first at the
same path. The numbers below were read off the destroyed journal before it was
lost and are reproduced from the session that observed them. They are evidence
of a weaker kind than `proofs/runs/*.json`, and are labelled as such.

A clobber guard was added to `run_assignment.py` the same day. It now refuses to
start when the runs directory already holds journals.

## What was observed

`stealth/ox-alpha`, `harnesses/loop.py` envelope, task `t01_average_empty`,
one repeat, guard on, ceiling 4.

| | |
|---|---|
| model calls | 14 (the whole budget) |
| unusable replies | 10 |
| tool steps | 4, all `read` - it never wrote a file |
| `ended` | `max_steps` |
| `solved` / `claimed` | false / false |
| wall clock | 187 s |
| `reasoning_tokens` | 0 on every call |

The agent was not failing to solve the task. Every reply contained the correct
repair. It failed to serialise it: the JSON `content` field carried literal
newlines and unescaped `"""`, which `json.loads` rejects at column 68.

Representative reply, verbatim:

```
{"action":"write","path":"calc.py","content":"def average(numbers):
    """Mean of a list. Returns 0 for an empty list."""
    if not numbers:\n        return 0\n    return sum(numbers) / len(numbers)\n"}
```

Note the mixture: literal newlines on the first two lines, `\n` escapes on the
third. That mixture is what makes the reply unrepairable rather than merely
malformed.

## Interventions tried against the live model

Cache-busting confirmed by varying reply length (203-208 bytes), temperature 1.0.

| Intervention | Parseable |
|---|---|
| `SYSTEM` unchanged | 0/4 |
| `response_format` strict `json_schema` | 0/4 - advertised in the model's `supported_parameters` and silently ignored |
| `SYSTEM` plus an explicit instruction to escape newlines and quotes | 0/3 |
| Re-escape the `content` field before parsing | 5/5 parsed, **0/5 compiled** |

The last row is the one that decided the design. A repair function must guess
which newlines were literal and which were `\n` escapes, and guessing wrong
yields Python containing the two characters `\n` as text. That converts a
visible failure - `unusable_replies`, honestly counted - into an invisible one:
a successful-looking `edit` step writing corrupt source, which the scorer would
attribute to the agent. Different causes must not share a column.

## What was done instead

`harnesses/loop_assignment.py`: same guard, same ceiling, same budget, same
`Step` and `TaskRun` records, with the file body moved out of the JSON string
and into a fenced block. `harnesses/loop.py` is untouched, so the published
qwen comparison is unaffected.

## The second smoke run

Same task and settings, fenced envelope. `results.json` and the journal beside
this file are that run.

| | |
|---|---|
| model calls | 5 |
| unusable replies | 0 |
| steps | read, read, edit, `pytest`, answer |
| `solved` / `claimed` | true / true |
| wall clock | 128 s |
| total tokens | 2226 |

It ran its verification after its final edit, so it satisfies the stricter
"verified after last edit" rule as well as the current one.

## Still unexplained

`reasoning_tokens` is 0 on every call in both runs, under `reasoning:
{"enabled": true}` and under `reasoning: {"effort": "medium"}`. The manifest
records `reasoning_enabled: true` because that is what was requested. What was
observed is zero. Do not report this grid as measuring a reasoning
configuration that the provider never confirmed applying.
