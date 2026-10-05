# Antibiotic Exception Requests — Synthetic Prototype

Pharmacy sends an exception request for a restricted antibiotic. The system loads the patient's
clinical data, picks the drug's decision tree, fills the tree's inputs (structured fields by code,
free-text facts by a small LLM with verbatim evidence quotes), walks the tree deterministically,
runs a renal dose check, and returns a recommendation for a pharmacist to approve/override.
The LLM never decides. Synthetic data only — no PHI. Trees are ILLUSTRATIVE, not clinical guidance.

## Completed
- [x] Decisions: generate 5-table synthetic dataset (50 patients); draft 3 sample trees; baseline
      off-the-shelf small Qwen before any fine-tuning; Kimi K3 writes synthetic notes/requests.

## Current Work
- [-] Synthetic data + trees (`trees/`, `data/`, local Postgres in Docker)
- [x] Pipeline: loader → tree select → structured mapping (SQL rules from `trees/variables.md`) → LLM extraction → quote check → traversal → renal dose check — 68 tests passing
   - Smoke test Qwen2.5-1.5B (MLX 4-bit): 1–2.4 s/call warm; ignores `{value, evidence}` format → 1/5 correct
   - GAP: quote check proves the quote exists, not that the value matches it (got `moderate` with quote saying "ANAPHYLAXIS")

## Next Steps
- [ ] Baseline eval (`eval/`): Qwen3-4B-Instruct-2507, Qwen3.5-2B/4B/9B (MLX 4-bit, local) vs Kimi K3 reference
- [x] Value↔quote consistency check (`consistency_failed`; synonym table `ENUM_SYNONYMS` in extract.py; conservative)
- [x] Eval suite `evals/` (Phoenix-compatible: datasets {input, expected, metadata}; evaluators bound on output/expected/input/metadata; task wrapper; local runner; thresholds gate). Suites: golden (dev/test locked), behavioral (~30 hand-written EVAL- cases incl. prompt injection, temporal leakage, negation), invariance (metamorphic variants). See evals/README.md. TODO once data generation finishes: `uv run python -m evals.datasets` (locks split.json), then `evals.run --suite all --gate` per model
- [ ] Arize Phoenix wiring (tracing + upload + run_experiment) — deferred by user
- [ ] Decide whether fine-tuning is needed based on baseline gaps
- [ ] SPIKE: production on GCP under BAA (VPC-SC, Cloud SQL, Cloud Run GPU, audit logs)

## Design contract (all components must follow)

### Database: Postgres 16 in Docker, db `kidney_abx`
5 clinical tables + 1 workflow table:
- `patients` (patient_id PK, mrn, age, sex, weight_kg, ckd_stage, dialysis_modality [none|HD|PD], dialysis_days text, clinic)
- `labs` (id, patient_id FK, test_code, test_name, value numeric, unit, collected_at)  — e.g. EGFR, CREAT, WBC, CRP, LACTATE, VANC_TROUGH
- `allergies` (id, patient_id FK, agent, reaction, severity [mild|moderate|severe|anaphylaxis|unknown], recorded_at)
- `microbiology` (id, patient_id FK, specimen, collected_at, organism nullable, antibiotic, interpretation [S|I|R], mic text)
- `clinical_notes` (id, patient_id FK, note_type, author_role, written_at, body text)  — free text; prior antibiotic history and clinical status live HERE on purpose
- `exception_requests` (request_id PK, patient_id FK, drug, dose_mg, frequency, route, indication text, justification text, requested_at, prescriber)

### Tree JSON (`trees/<drug>.json`)
```json
{
  "tree_id": "meropenem_v1", "drug": "meropenem", "illustrative": true,
  "variables": {
    "esbl_isolated":        {"type": "bool", "source": "structured", "description": "..."},
    "septic_shock":         {"type": "bool", "source": "text", "description": "..."},
    "pcn_allergy_severity": {"type": "enum", "values": ["none","mild","moderate","severe","anaphylaxis"], "source": "structured_or_text"}
  },
  "root": "n1",
  "nodes": {
    "n1": {"var": "esbl_isolated", "op": "==", "value": true, "yes": "APPROVE", "no": "n2"},
    "n2": {"var": "septic_shock",  "op": "==", "value": true, "yes": "n3", "no": "DENY"}
  },
  "outcomes": {"APPROVE": "...", "DENY": "...", "PHARMACIST_REVIEW": "..."}
}
```
Outcomes: APPROVE_ELIGIBLE | DENY_ELIGIBLE | PHARMACIST_REVIEW | NEED_INFO (auto when a needed var is null).
Ops: `== != < <= > >= in`. Variable `source`: `structured` (code-mapped from tables), `text` (LLM extraction from notes + justification), `structured_or_text` (structured first, fall back to text).

### Extraction output (LLM)
`{"<var>": {"value": <typed or null>, "evidence": "<verbatim quote or null>"}}` — a value with an evidence quote not found verbatim (whitespace/case-normalised) in the source text is nulled.

### Gold labels
Built scenario-first: sample a target path → fix variable values → generate rows + text consistent with them. Stored in `data/gold.jsonl` (request_id, tree_id, variables, expected_outcome, expected_path, text_vars_present).

## Implementation Notes
- Kimi (api.moonshot.ai) is acceptable ONLY because data is synthetic. Real PHI → BAA-covered provider in-network.
- Local small models via `mlx-lm` (pip, OpenAI-compatible server) — no system install needed.
- Never auto-deny in production design; DENY_ELIGIBLE routes to a pharmacist.
