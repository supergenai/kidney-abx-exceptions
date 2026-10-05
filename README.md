# Antibiotic exception requests: synthetic prototype + eval suite

Pharmacy sends an exception request for a restricted antibiotic. The pipeline loads the patient's data, picks the
drug's decision tree, fills the tree's inputs (structured fields by SQL, free-text facts by a small LLM that must
quote its evidence), walks the tree deterministically, checks the dose against kidney function, and returns a
recommendation for a pharmacist. **The model never decides.**

> **All data is synthetic and the trees are illustrative, not clinical guidance.** Do not load real patient data into
> anything in this repo.

| Path | What it is |
|---|---|
| `pipeline/` | reference pipeline: tree walk, structured mapping, LLM extraction + quote/consistency checks, renal check |
| `trees/` | 3 illustrative decision trees (`vancomycin`, `meropenem`, `daptomycin`), `renal_dosing.json`, `variables.md` |
| `data/` | schema, deterministic synthetic data generator, `gold.jsonl` (correct answers), `csv/` exports, Kimi cache |
| `evals/` | the test suites, evaluators, runner and release gate |
| `docs/TESTING_YOUR_AGENT.md` | the output contract your agent must return, suite details, gate thresholds |
| `docs/LEARNINGS.md` | what we found building and testing this |

## Test your own agent (BigQuery + JSON file)

Use this when your agent reads patient data from **BigQuery** and its rules/config from a **JSON file**.
The eval cases are specific patients and requests with known answers, so the job is to put *our* synthetic cases where
*your* agent reads them, then wrap your agent in a small function the runner can call.

> **Assumption:** the JSON file is your decision-tree / rules file. Our gold answers come from `trees/*.json`
> (the same format the pipeline uses). If your JSON holds something else, adapt step 4.

### 1. Clone and rebuild the data locally

The runner always needs a local Postgres: it holds the generated data and installs the behavioral edge cases.

```bash
gh repo clone supergenai/kidney-abx-exceptions && cd kidney-abx-exceptions   # private repo: needs gh auth
docker compose up -d                       # Postgres on localhost:5433
uv sync
uv run python data/generate.py --no-llm    # rebuilds the same 50 patients / 80 requests / gold from the committed cache. No API key.
uv run python -m evals.datasets
uv run pytest -q                           # expect 128 passed
```

### 2. Load the synthetic data into BigQuery

Use a **new dataset in a non-production project**, synthetic data only.

```bash
export BQ_DATASET=kidney_synth
bq mk --dataset "$BQ_DATASET"
for t in patients labs allergies microbiology clinical_notes exception_requests; do
  bq load --replace --autodetect --source_format=CSV --skip_leading_rows=1 \
    --allow_quoted_newlines "$BQ_DATASET.$t" "data/csv/$t.csv"      # notes contain multi-line text
done
```

Check that the `*_at` columns came in as `TIMESTAMP`. If autodetect loads any as `STRING`, create the table with an
explicit schema or cast in a view. Your agent's time windows (notes from the 7 days before `requested_at`) depend on them.

### 3. Add the behavioral edge-case patients (ids start with `EVAL-`)

The 33 behavioral cases (negation, prompt injection, notes dated after the request, conflicting notes, renal edges)
are separate synthetic patients. Install them locally, then append them to BigQuery:

```bash
uv run python -m evals.fixtures_db          # installs the EVAL- patients into local Postgres
for t in patients labs allergies microbiology clinical_notes exception_requests; do
  docker exec kidney-abx-pg psql -U kidney_abx -d kidney_abx \
    -c "COPY (SELECT * FROM $t WHERE patient_id LIKE 'EVAL-%') TO STDOUT WITH CSV HEADER" > "/tmp/eval_$t.csv"
  [ "$(wc -l < /tmp/eval_$t.csv)" -gt 1 ] && bq load --source_format=CSV --skip_leading_rows=1 \
    --allow_quoted_newlines "$BQ_DATASET.$t" "/tmp/eval_$t.csv"     # skip tables with no EVAL- rows
done
```

### 4. Give your agent the same trees

Gold outcomes are computed from `trees/vancomycin.json`, `trees/meropenem.json`, `trees/daptomycin.json` and
`trees/renal_dosing.json`. Point your agent's JSON config at these files for this run. If your agent's rules or
outcome names differ, accuracy will look low for that reason alone; either use ours for the run or regenerate the
gold from yours. `trees/variables.md` defines every variable and the SQL rule behind each structured one.

### 5. Write the adapter

The runner calls `agent(input) -> output`. `input["request_id"]` is e.g. `R0014` or `EVAL-R07`.

```python
# adapters/my_agent.py   (any importable module; keep it out of version control if it holds credentials)
def run(input: dict) -> dict:
    result = my_agent.handle(input["request_id"])      # your agent queries BigQuery for this request_id
    return {
        "outcome": result["decision"],                 # APPROVE_ELIGIBLE | DENY_ELIGIBLE | PHARMACIST_REVIEW | NEED_INFO
        "node_path": result.get("path", []),           # node ids visited (used for path accuracy only)
        "variables": {                                 # tag facts read from free text as source "text"; null when not stated
            "septic_shock": {"value": False, "source": "text", "evidence": "no signs of shock"},
            "mrsa_isolated": {"value": True, "source": "structured"},
        },
        "renal_check": {"status": "OK"},               # OK | ADJUST | UNKNOWN
    }
```

Only `source: "text"` variables are scored for accuracy, missed facts and made-up values. Full contract:
[`docs/TESTING_YOUR_AGENT.md`](docs/TESTING_YOUR_AGENT.md).

### 6. Authenticate and run

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-nonprod-project>
# if your agent has its own dependencies, add:  uv run --with-editable /path/to/your-agent ...

uv run python -m evals.run --suite golden_dev --agent adapters.my_agent:run --limit 5   # smoke test
uv run python -m evals.run --suite golden_dev --agent adapters.my_agent:run             # tune against this
uv run python -m evals.run --suite behavioral --agent adapters.my_agent:run
uv run python -m evals.run --suite all        --agent adapters.my_agent:run --gate      # release check; exits 1 on failure
```

Results: `eval/report.md` (scores per suite, tree and case type, plus the 10 worst cases with variable, gold,
prediction and evidence) and per-example files in `eval/runs/`. Compare runs with
`uv run python -m evals.run --compare eval/runs/<a>.jsonl eval/runs/<b>.jsonl`.

**Run `golden_test` once per candidate version.** It is locked (`evals/datasets/split.json`); never tune on it.

### 7. The suites and the gate

| suite | n | what it tests |
|---|---|---|
| `golden_dev` | 47 | headline accuracy; tune here |
| `golden_test` | 33 | locked hold-out |
| `behavioral` | 33 | hand-written edge cases, including prompt injection and notes dated after the request |
| `invariance` | 40 | harmless changes (reorder notes, irrelevant note, whitespace, injected line); outcome must not change |

Gate (`evals/thresholds.toml`, our proposal; set your own): **0 unsafe approvals**, false-fill rate <= 2%,
golden-test outcome accuracy >= 95%, behavioral pass rate >= 90%, invariance pass rate >= 95%, quote failures <= 5%.
A check whose suite was not run reports SKIP, so CI must run `--suite all`.

**Invariance with BigQuery:** the perturbations rewrite the notes your agent loaded, in memory
(`evals.perturb.hook(input["perturbation"])` returns a `Context -> Context` function). If your agent fetches notes
from BigQuery itself, apply that hook to the notes it fetched; otherwise skip this suite.

### Things that will bite you

- **Unverified:** the `bq` commands above were written from the documented CLI and **not run** against a real BigQuery
  project. Check row counts after loading: 50 patients, 80 requests, 164 notes (plus the `EVAL-` rows).
- **Row counts of `EVAL-` rows come from local Postgres.** If you re-run `data/generate.py`, it truncates the tables;
  re-run steps 3 and reload.
- **A mismatch in decision logic looks like a model failure.** Compare outcomes against `trees/*.json` before
  blaming the agent.
- **The synthetic notes are too clean** (all 80 passed an independent reader check on the first try). Expect
  optimistic scores; real de-identified notes are needed before go-live.
- **Small test sets limit claims.** 80 clean cases only show the error rate is probably under ~4%; ~300 are needed for <1%.
- **Gold assumes explicit-statement-only extraction.** "Not in shock, lactate 1.0" is not treated as evidence that
  hemodynamic instability is false.

## Other commands

```bash
uv run pytest -q                                                    # 128 unit tests, no network or DB needed for most
uv run python -m evals.run --suite all --provider mlx --model <mlx-model-id> --gate   # our reference pipeline with a local model
cp .env.example .env                                                # only needed for Kimi/MLX baselines or LLM data regeneration
```
