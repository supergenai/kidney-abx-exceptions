# Learnings: 2026-10-05 session

## What we built
- **Kidney antibiotic-exception prototype** (synthetic data only): Postgres (6 tables, 50 patients, 80 requests, 164 notes), 3 illustrative decision trees, renal dosing table, and a pipeline: tree selection (code) -> structured variables (SQL) -> LLM extraction of text variables with verbatim evidence quotes -> quote + value-consistency + negation checks -> deterministic tree walk -> renal dose check.
- **Eval suite** (`evals/`): golden dev/test (locked split), 33 behavioral edge cases, invariance (metamorphic) variants, code-only evaluators, a gate, Phoenix-shaped datasets/evaluators/task.
- **Geelong text-to-SQL** (paused, local only): 10 spatial tables in DuckDB, SQL guard + FastAPI serving (40 tests), 46 seeds + 45 Kimi pairs. The QLoRA/Colab/Cloud Run steps were not reached.

## Results so far (partial; run was stopped early)

| golden_test (n=33) | Kimi K3 | Qwen3.5-2B baseline |
|---|---|---|
| outcome accuracy | 93.9% | 39.4% |
| case pass rate | 84.8% | 15.2% |
| behavioral pass | 84.8% | 30.3% |
| invariance pass | 80.0% | 57.5% |
| unsafe approvals (behavioral) | 1 | 1 |
| quote failures | ~0% | 50-60% |
| gate | FAIL | FAIL |

Qwen3.5-2B after prompt v3 + guards, **golden_dev only**: outcome 44.7% -> 66.0%, case pass 21% -> 57%, quote failures 58% -> 9%, false fills 11.8% -> 0%, 0 unsafe. Latency p50 rose 4.6 s -> 7.3 s. Behavioral/invariance for v3 and the Qwen3-4B / 3.5-4B / 3.5-9B baselines were **not completed**. Nothing here was run on `golden_test` after optimisation.

## Learnings

**Architecture**
1. A small model should not walk a decision tree. Code walks it (microseconds, exact, auditable); the model only reads text and returns `{value, evidence}`. This is also what makes it testable.
2. Fine-tuning teaches format and behaviour, not facts. Facts belong in the database / retrieval. For "give agents context", use RAG, not training.
3. Fail toward a human: any null, timeout or check failure routes to NEED_INFO / PHARMACIST_REVIEW. Never auto-deny.
4. Prompt-injection safety falls out of the design: the model only extracts, the quote check needs a verbatim match, the tree decides. This is tested, not assumed.

**What the evals found**
5. The first smoke test showed the 1.5B model ignores the output format (1/5) and that the quote check had a hole: it returned "moderate" with a real quote saying "ANAPHYLAXIS". A quote check proves the quote exists, not that the value agrees with it. We added a value/quote consistency check.
6. **Even the frontier model failed the gate**: Kimi K3 made an unsafe approval on a conflicting-notes case by trusting an older note ("on norepinephrine, septic shock") over a newer one. A frontier model is not a safety argument; deterministic structure plus evals is.
7. Kimi's "false fills" are partly a labelling decision: it inferred "not in shock, lactate 1.0" => instability = false; gold says absent. Decide explicit-only vs clinical inference and encode it in the gold.
8. For the 2B model the dominant failure was output discipline (unquoted/paraphrased evidence), fixed mostly by prompt and guard changes, not fine-tuning. The remaining problem is missed facts (~20%), which fail safe (NEED_INFO) but cost throughput.
9. The consistency guard trades recall for safety: a correct "false" backed by a purely descriptive quote can be nulled. That is the safe direction; measure the cost.

**Data and evals**
10. Scenario-first generation (choose the path and facts first, then write rows and text) gives gold labels for free. Validate generated text with an independent reader.
11. **The synthetic data is probably too easy**: 80/80 passed the reader check first try. Scores are optimistic. Real de-identified notes are required before any go-live claim.
12. Small eval sets bound what you can claim: 0 errors in 80 cases still allows ~4% true error (95%); ~300 clean cases to show <1%. `golden_test` has 33.
13. Lock the test split and never tune on it; log every optimisation experiment; only keep a change if unsafe approvals and false fills do not get worse.
14. Behavioral edge cases (injection, post-dated notes, conflicting notes, renal edges) caught more than aggregate accuracy did.

**Compliance and infra**
15. Real PHI needs: BAA-covered services only, no free Colab, no third-party LLMs without a BAA (Kimi/Moonshot is synthetic-only), VPC-SC, audit logs, de-identified views, human approval. Postgres (+ Cloud Storage for documents) beats Firestore here.
16. Free Cloud Run is CPU-only: prompt reading, not generation, dominates latency for a 1.5B model; cache the schema/prompt or train it in.
17. Operational gotchas: Kimi allows 15 concurrent requests (16 workers dropped 25 requests; use backoff, 8 workers); `data/generate.py` TRUNCATEs, so behavioral `EVAL-` rows are re-installed per run; the key was pasted in chat, so **rotate it**.

## Decisions still open
- Explicit-only vs inference-allowed extraction (changes gold).
- Tree policy questions flagged by the data agent: severe vanc allergy => DENY at root even with MRSA; meropenem ESBL "stable, source controlled => DENY" assumes ertapenem step-down; daptomycin eGFR<30 => REVIEW is weakly justified.
- Whether any local model clears the gate after optimisation; fine-tuning is only justified if the remaining gaps (missed facts, conflicting-note recency, injection) persist.

## Next
1. Finish baselines for Qwen3-4B-2507 / 3.5-4B / 3.5-9B; re-run v3 on behavioral + invariance; one locked golden_test run per candidate.
2. Fix the conflicting-notes recency failure (applies to every model, including Kimi).
3. Arize Phoenix wiring (deferred by you). 4. Real de-identified notes for eval. 5. Real trees. 6. Resume Geelong or drop it.
