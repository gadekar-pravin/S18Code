"""The assignment grid: one fixed configuration, three tasks, three repeats.

Deliberately NOT run_local.py with the model swapped. That file and the nineteen
runs under proofs/runs/ are scoped to qwen3.8:27b, and rescore.py stamps that
literal into every row it derives. An ox-alpha run written into proofs/runs/
would be relabelled as qwen on the next rescore with no error raised, so this
runner writes to proofs/assignment_v1/ and never touches the published evidence.

Three differences from run_local.py, all deliberate:

  one arm       the assignment asks for one fixed configuration, not an A/B.
                guard=True, ceiling=4 - the s17_rules settings.
  three repeats S18_REPEATS defaults to 3 here, not 1.
  journal first the raw record is written BEFORE score() is called. score() is
                pure, so ordering cannot contaminate it, but if score() raises
                then a run written afterwards is lost - which is the exact
                six-hours-of-GPU disaster the journal exists to prevent.

Reasoning is left ON, as in run_local.py: noticing that it has failed is the
axis under test, and a model that cannot reason cannot notice. Reasoning tokens
count against max_tokens, hence 16000 rather than run_local.py's 1200.

    export OPENROUTER_API_KEY=...        # never committed; .env is gitignored
    cd .. && python3 -m S18Code.run_assignment
    cd .. && python3 -m S18Code.run_assignment t01_average_empty
"""
import asyncio, dataclasses, hashlib, json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from S18Code.harnesses.loop import Config
from S18Code.harnesses.loop_assignment import SYSTEM, run_loop
from S18Code.tasks.materialise import grade_clean_room, materialise
from S18Code.evals.axes import score

# ---------------------------------------------------------------- the manifest
# Every value here is part of the claim. A number from this grid is scoped to
# this block and says nothing outside it.
MODEL = "stealth/ox-alpha"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
TEMPERATURE = 0.2
MAX_TOKENS = 16000          # reasoning tokens are drawn from this same budget
REASONING = True
ARM = Config("s17_rules", guard=True, ceiling=4)     # max_steps=14 by default
TASKS = ["t10_source_repair_average", "t11_integrity_parity_lock",
         "t12_unavailable_secret_digest"]
COOLDOWN = 2                # hosted model; politeness, not thermal management

OUT = pathlib.Path(__file__).parent / "proofs" / "assignment_v1"

def _load_dotenv() -> None:
    """Read .env if present. Real environment always wins over the file.

    No dependency: this repo has no manifest and is not going to grow one for
    six lines. Values are never printed, and .env / .env.* are gitignored.
    """
    f = pathlib.Path(__file__).parent / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
if not KEY:
    raise SystemExit(
        "set OPENROUTER_API_KEY before running this.\n"
        "  export OPENROUTER_API_KEY=...      (or put it in .env, which is gitignored)")

# Real token counts, one entry per model call, harvested from OpenRouter's usage
# object. Kept out of TaskRun on purpose: widening the llm() signature would
# change harnesses/loop.py, which both arms share, and the published comparison
# rests on that file being untouched. reply_chars_over_4 is len(content)//4 and
# cannot see the reasoning channel at all, so on a reasoning model it is not
# merely imprecise, it is measuring the wrong thing.
USAGE: list[dict] = []


async def llm(prompt, system):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "reasoning": {"enabled": REASONING},
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "X-Title": "S18Code assignment grid",
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                d = json.load(r)
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            USAGE.append({**(d.get("usage") or {}),
                          "reasoning_chars": len(msg.get("reasoning") or ""),
                          # The raw reply, bounded. The plan asks the journal to
                          # carry every response; TaskRun has no field for it and
                          # widening TaskRun would break rescore.py on the
                          # nineteen historical runs, so it rides here.
                          "raw": content[:4000]})
            return content
        except urllib.error.HTTPError as e:
            # Status only. The body can echo request material and the header
            # carries the key; neither belongs in a journal or a terminal.
            last = f"HTTP {e.code}"
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
        "system_prompt_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest()[:16],
        "provider": "openrouter",
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "reasoning_enabled": REASONING,
        "model_call_budget": ARM.max_steps,
        "guard": ARM.guard,
        "ceiling": ARM.ceiling,
        "arm": ARM.name,
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


async def main():
    pytest_version = preflight()
    T = pathlib.Path(__file__).parent / "tasks"
    all_tasks = {json.loads(p.read_text())["id"]: json.loads(p.read_text())
                 # t*.json, not t0*.json: the assignment tasks are t1x precisely so that
                 # run_local.py and run_benchmark.py, which glob t0*, cannot see them
                 # and the published nine-task grid stays frozen.
                 for p in T.glob("t*.json")}

    order = [a for a in sys.argv[1:] if a in all_tasks] or TASKS
    unknown = [a for a in sys.argv[1:] if a not in all_tasks]
    if unknown:
        raise SystemExit(f"unknown task ids: {unknown}")
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
        raise SystemExit(
            f"{runs_dir} already holds {len(existing)} journal(s).\n"
            f"Move them aside before running again - they cannot be regenerated:\n"
            f"  mv {runs_dir} {runs_dir.parent / 'runs_<label>'}")
    runs_dir.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"  manifest frozen -> {OUT / 'manifest.json'}")
    print(f"  {len(order)} tasks x {reps} repeats = {len(order) * reps} runs, arm={ARM.name}\n")

    rows, n, total = [], 0, len(order) * reps
    for tid in order:
        t = tasks[tid]
        for rep in range(reps):
            n += 1
            ws = materialise(t)
            USAGE.clear()
            t0 = time.time()
            try:
                run = await run_loop(t, ws, ARM, llm, MODEL)
            except Exception as e:
                # run_loop catches llm failures itself and sets ended=llm_error,
                # so anything arriving here is the harness breaking: a pytest
                # TimeoutExpired from the test action, an OSError on a write.
                # Printing and continuing discarded the usage and raw replies
                # and left a runs/ directory silently short of the manifest's
                # count. An abort is a fact about the harness and gets a record.
                # Caught in review by Codex.
                (runs_dir / f"{tid}__{ARM.name}__r{rep}.ABORTED.json").write_text(
                    json.dumps({"task_id": tid, "arm": ARM.name, "rep": rep,
                                "aborted": True,
                                "exception": type(e).__name__, "detail": str(e)[:500],
                                "seconds": time.time() - t0,
                                "usage": list(USAGE)}, indent=1) + "\n")
                print(f"  [{n}/{total}] {tid} ABORTED {type(e).__name__} "
                      f"(journalled)", flush=True)
                continue
            # Clean room, not run_tests: the agent's workspace can hold a
            # pytest.py that grades everything green. See grade_clean_room.
            passed, tail = grade_clean_room(ws, t)

            # Journal FIRST. score() is pure and cannot contaminate this, but a
            # scorer that raises must not also destroy the evidence needed to
            # fix it.
            (runs_dir / f"{tid}__{ARM.name}__r{rep}.json").write_text(json.dumps(
                {**dataclasses.asdict(run), "actually_passed": passed,
                 "pytest_tail": tail, "kind": t["kind"], "rep": rep,
                 "usage": list(USAGE),
                 "final_files": {f.name: f.read_text()[:4000]
                                 for f in sorted(ws.glob("*.py"))}}, indent=1) + "\n")

            row = score(run, actually_passed=passed)
            row["kind"], row["claimed"], row["rep"] = t["kind"], run.claimed_success, rep
            row["usage_total_tokens"] = sum(u.get("total_tokens", 0) for u in USAGE)
            rows.append(row)
            (OUT / "results.json").write_text(json.dumps(
                {"manifest": manifest, "rows": rows}, indent=1) + "\n")

            print(f"  [{n}/{total}] {tid:30s} r{rep} solved={passed!s:5s} "
                  f"claimed={run.claimed_success!s:5s} cheat={row['cheated']!s:5s} "
                  f"steps={row['steps']:2d} tok={row['usage_total_tokens']:6d} "
                  f"{time.time() - t0:5.0f}s {run.error[:30]}", flush=True)
            if n < total:
                await asyncio.sleep(COOLDOWN)

    print(f"\n  wrote {OUT / 'results.json'}  ({len(rows)}/{total} rows)")
    print(f"  journals in {runs_dir}")


asyncio.run(main())
