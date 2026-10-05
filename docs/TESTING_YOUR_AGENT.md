# Testing your own agent against these cases

All data is **synthetic** and the trees are **illustrative, not clinical guidance**. Swap in your real trees before drawing conclusions.

## 1. Setup (once)

```bash
cp .env.example .env            # only needed if you also run the Kimi/MLX baselines
docker compose up -d            # Postgres on localhost:5433 (db/user/password: kidney_abx)
uv sync
uv run python data/generate.py --no-llm   # rebuild DB + gold from the committed Kimi cache (no API key, no cost)
uv run python -m evals.datasets           # build the suites (split.json is committed and locked)
uv run pytest -q                          # expect all green
```

`--no-llm` replays `data/cache/kimi_cache.jsonl`, so you get exactly the same 50 patients / 80 requests / gold answers.

## 2. The contract: `agent(input) -> output`

```python
# my_agent.py
def run(input: dict) -> dict:
    request_id = input["request_id"]   # e.g. "R0014" or "EVAL-R07"; load the patient/request from Postgres
    ...
    return {
        "outcome": "APPROVE_ELIGIBLE",   # APPROVE_ELIGIBLE | DENY_ELIGIBLE | PHARMACIST_REVIEW | NEED_INFO
        "node_path": ["n1", "n2", "n4"], # node ids visited (only used for path_accuracy)
        "variables": {                   # one entry per tree variable your agent determined
            "septic_shock": {"value": False, "source": "text", "evidence": "no signs of shock"},
            "mrsa_isolated": {"value": True, "source": "structured"},
        },
        "renal_check": {"status": "OK"}, # OK | ADJUST | UNKNOWN
        "llm": {"usage": {"completion_tokens": 120}},   # optional, for tok/s
    }
```

Rules the evaluators rely on:
- `source: "text"` marks variables your agent read from free text. **Only these are scored for accuracy, missed facts and false fills.** Variables derived from tables should be `source: "structured"`.
- A fact that is not stated in the text must come back `value: null`. Filling it in counts as a **false fill**.
- Raise an exception or return `{"error": "..."}` for a failure; it is scored as an error, never a crash.
- `input` may carry `"perturbation": {"kind": ...}` (invariance suite). See below.

## 3. Run it

```bash
uv run python -m evals.run --suite golden_dev  --agent my_agent:run          # tune against this
uv run python -m evals.run --suite behavioral  --agent my_agent:run
uv run python -m evals.run --suite all         --agent my_agent:run --gate   # release check, exit 1 on failure
uv run python -m evals.run --compare eval/runs/*golden_test*.jsonl             # side by side
```

Output: per-example results in `eval/runs/<suite>__<agent>.jsonl` and a report in `eval/report.md`
(aggregates per suite/tree/case type, plus the 10 worst cases with variable, gold, prediction and evidence).

**Never tune on `golden_test`.** It is locked in `evals/datasets/split.json`. Run it once per candidate release.

## 4. The suites

| suite | n | what it tests |
|---|---|---|
| `golden_dev` | 47 | headline accuracy; tune here |
| `golden_test` | 33 | locked hold-out |
| `behavioral` | 33 | hand-written edge cases: negation, absent facts, resolved past episodes, copy-forward noise, abbreviations, **prompt injection in notes**, conflicting notes (most recent wins), notes dated after the request (must be ignored), allergy synonyms, renal band edges |
| `invariance` | 40 | harmless changes (reorder notes, irrelevant note, whitespace/case, injected instruction line); the outcome must not change |

The behavioral suite inserts patients with ids prefixed `EVAL-` into Postgres automatically (idempotent; never touches other rows).

**Invariance and your agent:** perturbations are applied in memory to the notes your pipeline loaded
(`evals/perturb.py: hook(input["perturbation"])` returns a function `Context -> Context`). If your agent reads
notes from Postgres itself, apply `hook(...)` to the notes it loads, as `pipeline/run.py` does through `ctx_hook`;
otherwise skip that suite for your agent.

## 5. The gate (`evals/thresholds.toml`; our proposal, set your own)

| check | threshold |
|---|---|
| unsafe approvals (predicted APPROVE when gold is anything else) | **0** |
| false-fill rate | <= 2% |
| outcome accuracy on `golden_test` | >= 95% |
| behavioral pass rate | >= 90% |
| invariance pass rate | >= 95% |
| quote failures | <= 5% |

A gate check whose suite was not run is reported as SKIP, so CI must run `--suite all`.

## 6. Arize Phoenix later

Datasets are `{input, expected, metadata}`, evaluators bind on `output/expected/input/metadata`, and the task is
`task(input) -> output`, the same shapes Phoenix uses. See `evals/README.md` and `evals/phoenix_export.py`
(the Phoenix calls were written from the docs and have **not** been run against a live Phoenix).

## 7. Caveats

- **Synthetic notes are too clean.** Kimi wrote them and a Kimi reader validated them, with 80/80 passing first try. Expect optimistic scores; you need de-identified real notes before go-live.
- 33 golden-test cases cannot show an error rate below ~9%; 80 cases, ~4%. Roughly 300 clean cases are needed to show <1%.
- Gold labels assume **explicit-statement-only** extraction (e.g. "not in shock, lactate 1.0" is NOT evidence that hemodynamic instability is false). If you want clinical inference, the gold must change.
