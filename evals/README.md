# evals/: evaluation suite

This suite evaluates the exception-request pipeline (`pipeline.run.process`). The data is synthetic and the trees are illustrative. The shapes match Arize Phoenix datasets, tasks and evaluators, so moving to Phoenix later needs no rewrite.

## Run

```bash
uv run python -m evals.datasets                      # build datasets (golden + invariance need data/gold.jsonl)
uv run python -m evals.fixtures_db --check           # verify behavioral fixtures vs real SQL (rolled back)
uv run python -m evals.run --suite all --provider mlx --model mlx-community/Qwen3-4B-Instruct-2507-4bit --gate
uv run python -m evals.run --suite behavioral --provider moonshot --limit 5
uv run python -m evals.run --compare eval/runs/behavioral__A.jsonl eval/runs/behavioral__B.jsonl
uv run python -m evals.run --score eval/runs/<slug>.jsonl   # re-score saved outputs, incl. plain `pipeline.run --all` files
uv run pytest -q tests/test_evals.py                 # evaluator unit tests, no network / DB
```

Each run writes `eval/runs/<suite>__<model-slug>.jsonl`. That file holds one record per example: input, expected, metadata, output and every evaluator result. The run also writes `eval/report.md`, which has:

- aggregates per suite, per tree and per case_type
- the 10 worst cases, ordered unsafe approves first, then false fills, wrong values, outcome misses and missed facts, each with var / gold / pred / evidence
- a side-by-side table when several runs are compared
- the gate table when `--gate` is passed

Run order after data generation finishes:
1. `evals.datasets` writes `split.json`. Commit it, because test is locked from then on.
2. `evals.run --suite all --gate`. The behavioral suite re-installs its `EVAL-` rows each time it runs, because `data/generate.py` TRUNCATEs every table.

## Suites (`evals/datasets/<suite>.jsonl`)

| suite | source | purpose |
|---|---|---|
| `golden_dev` / `golden_test` | `data/gold.jsonl`, split 60/40 stratified by (tree, outcome) in sha256(request_id) order | headline accuracy. Assignments are locked in `split.json`: new ids are added to it, existing ids never move, and a warning is printed if an id's gold content changes. Treat **test** as held out and never tune on it. `--resplit` unlocks it. |
| `behavioral` | `evals/behavioral.py`: 33 hand-written cases, each its own synthetic patient `EVAL-P<nn>`/`EVAL-R<nn>`, inserted by `evals/fixtures_db.py` | targeted failure modes: negation, absent fact leading to NEED_INFO, resolved past episode, copy-forward/template noise, abbreviations, prompt injection, conflicting notes (most recent wins), a note after `requested_at` and a note before the 7-day window, allergy synonyms (anaphylactic, hives, SJS), the anaphylaxis-quoted-but-moderate trap, eGFR exactly on a band cutoff, HD vs PD, dose exactly at the cap, weight cap |
| `invariance` | 10 `golden_dev` cases (round-robin over trees), each with 4 variants from `evals/perturb.py`: reorder notes, add an irrelevant note, whitespace/case change, injected instruction line | metamorphic: the outcome must not change. Variants are applied in memory through `process(..., ctx_hook=)`, with no DB writes. |

`fixtures_db.py` deletes and inserts only rows whose `patient_id LIKE 'EVAL-%'` (FKs cascade) and never truncates anything. `pipeline.db.request_ids()` excludes `EVAL-`, so `pipeline.run --all` and the gold integration test never see these rows.

## Evaluators (`evals/evaluators.py`)

The evaluators are pure functions whose parameters use Phoenix's binding names (`input`, `output`, `expected`, `metadata`). Each one returns `EvalResult(score, label, explanation, metadata)`, which mirrors Phoenix's `EvaluationResult`.

- `score=None` means the evaluator does not apply to this example.
- Rate evaluators put `{k, n}` in `metadata`, so aggregates are pooled over variables.
- "Text vars" are the variables the pipeline filled from text. Their gold value is null unless the variable is in `text_vars_present`.

| tier | evaluator | aggregate metric | meaning |
|---|---|---|---|
| critical | `unsafe_approve` | `unsafe_approve` (count) | predicted APPROVE_ELIGIBLE while gold is anything else |
| critical | `false_fill` | `false_fill_rate` | a text var absent from the text came back non-null (hallucinated fact) |
| accuracy | `outcome_match` / `path_match` | `outcome_accuracy` / `path_accuracy` | exact outcome / node path |
| accuracy | `text_var_accuracy` | `text_var_accuracy` | conveyed text vars extracted with the gold value (`expected.acceptable` lists allowed alternatives) |
| accuracy | `missed_fact` / `wrong_value` | `missed_rate` / `wrong_value_rate` | a conveyed var came back null / a conveyed var came back with a different non-null value |
| accuracy | `renal_match` | `renal_agreement` | renal check OK/ADJUST agrees with `expected_dose_ok` (UNKNOWN counts as disagreement) |
| process | `quote_failed`, `consistency_failed`, `negation_conflict`, `parse_failed` | `*_rate` | guardrail trigger rates (see `pipeline/extract.py`) |
| info | `latency` | `latency_p50_s`, `latency_p95_s`, `tok_s_mean` | per-request seconds; completion tok/s when usage is reported |
| suite | `case_pass` | `case_pass_rate` | behavioral pass: right outcome, no unsafe approve, no false fill, all conveyed vars right, and renal agreement |
| suite | `invariant_outcome` | `invariance_pass_rate` | the variant's outcome equals the original's expected outcome |

## Gate (`evals/thresholds.toml`)

Pass `--gate` to check the thresholds. It exits 1 if any check fails.

| check | suites |
|---|---|
| `unsafe_approve == 0` | all |
| `false_fill_rate <= 0.02` | all |
| `outcome_accuracy >= 0.95` | golden_test |
| `case_pass_rate >= 0.9` | behavioral |
| `invariance_pass_rate >= 0.95` | invariance |
| `quote_failed_rate <= 0.05` | all |

A check whose suite was not run is reported as SKIP rather than FAIL, so CI must run `--suite all`.

## Mapping to Phoenix

| here | Phoenix |
|---|---|
| `evals/datasets/<suite>.jsonl` (`input` / `expected` / `metadata`) | `phoenix_export.py`: `Client().datasets.create_dataset(inputs, outputs, metadata)`, with legacy `px.Client().upload_dataset` as fallback. Install ad hoc with `uv run --with arize-phoenix-client`; it is not a project dependency. |
| `evals.task.make_task(cfg)` (`task(input) -> output`) | the `task` argument of `run_experiment` |
| the functions in `evaluators.EVALUATORS` | `evaluators=[...]`. Parameter names already bind. |
| `thresholds.toml` + `--gate` | CI step after the experiment, either reading the experiment's eval results or running `evals.run --gate` locally |
| `pipeline.run` timings/usage | span attributes once tracing is added |

The Phoenix client calls in `phoenix_export.py` were written from the documented API. They have not been run against a live Phoenix instance.

## Future: LLM-as-judge

`evaluators.summary_faithfulness_judge(input, output, metadata) -> EvalResult` is a documented stub for a future summary agent. It judges whether the summary is faithful to the cited evidence and the tree path. It is deliberately not registered in `EVALUATORS`. Before it can gate anything it needs:

- a BAA-covered model for real data
- temperature 0 and a fixed rubric prompt
- calibration against pharmacist labels
