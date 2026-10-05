# Tree variables — definitions and derivation rules

ILLUSTRATIVE ONLY — synthetic prototype, not clinical guidance.

Every variable used by `vancomycin.json`, `meropenem.json`, `daptomycin.json`. Inputs to every rule are the
request's `patient_id` and `requested_at` (from `exception_requests`). "Window" always means
`requested_at - 14 days <= collected_at <= requested_at` (inclusive both ends).

The SQL blocks below are **executable and authoritative**: `data/generate.py` runs exactly these blocks
(psycopg named params `%(patient_id)s`, `%(requested_at)s`) against the loaded DB to verify every gold
label. Each query returns one row, one column; SQL `NULL` = variable unknown (→ `NEED_INFO` if the tree
reaches it). Organism matching uses case-insensitive regex `~*` (no `%` wildcards, so no escaping issues).

Data conventions the rules rely on:
- `microbiology` has one row per (isolate, antibiotic). Organism names carry the resistance phenotype in
  parentheses: `Staphylococcus aureus (MRSA)`, `Staphylococcus aureus (MSSA)`, `Escherichia coli (ESBL)`,
  `Klebsiella pneumoniae (ESBL)`, `Klebsiella pneumoniae (CRE)`, `Enterococcus faecium (VRE)`.
  A negative culture is a row with `organism IS NULL` (and NULL antibiotic/interpretation).
- MSSA does **not** match `MRSA`; `Enterococcus faecalis` (vanc-S) is not VRE. Both appear as distractors.
- Older MDRO isolates (> 14 days before request) exist as distractors and must be ignored by the window.
- Allergy severity order: `mild < moderate < severe < anaphylaxis`; rows with severity `unknown` are ignored.

## Structured variables

### mrsa_isolated — bool (vancomycin, daptomycin)
True iff any microbiology row in the window has organism matching `MRSA`.
<!-- sql:mrsa_isolated -->
```sql
SELECT EXISTS (
  SELECT 1 FROM microbiology
  WHERE patient_id = %(patient_id)s
    AND organism ~* 'MRSA'
    AND collected_at BETWEEN %(requested_at)s::timestamptz - interval '14 days' AND %(requested_at)s::timestamptz
)
```

### mrsa_vanc_susceptible — bool or null (vancomycin)
Among window rows with organism matching `MRSA` and `antibiotic = 'vancomycin'`: true iff all are `S`;
false if any is `I` or `R`; NULL if there are no such rows (no MRSA, or vancomycin not tested).
<!-- sql:mrsa_vanc_susceptible -->
```sql
SELECT bool_and(interpretation = 'S') FROM microbiology
WHERE patient_id = %(patient_id)s
  AND organism ~* 'MRSA'
  AND lower(antibiotic) = 'vancomycin'
  AND collected_at BETWEEN %(requested_at)s::timestamptz - interval '14 days' AND %(requested_at)s::timestamptz
```

### esbl_isolated — bool (meropenem)
True iff any window row has organism matching `ESBL`.
<!-- sql:esbl_isolated -->
```sql
SELECT EXISTS (
  SELECT 1 FROM microbiology
  WHERE patient_id = %(patient_id)s
    AND organism ~* 'ESBL'
    AND collected_at BETWEEN %(requested_at)s::timestamptz - interval '14 days' AND %(requested_at)s::timestamptz
)
```

### carbapenem_resistant — bool (meropenem)
True iff any window row (any organism) has `antibiotic` in meropenem/ertapenem/imipenem with interpretation `R`.
<!-- sql:carbapenem_resistant -->
```sql
SELECT EXISTS (
  SELECT 1 FROM microbiology
  WHERE patient_id = %(patient_id)s
    AND lower(antibiotic) IN ('meropenem', 'ertapenem', 'imipenem')
    AND interpretation = 'R'
    AND collected_at BETWEEN %(requested_at)s::timestamptz - interval '14 days' AND %(requested_at)s::timestamptz
)
```

### vre_isolated — bool (daptomycin)
True iff any window row has organism matching `VRE`.
<!-- sql:vre_isolated -->
```sql
SELECT EXISTS (
  SELECT 1 FROM microbiology
  WHERE patient_id = %(patient_id)s
    AND organism ~* 'VRE'
    AND collected_at BETWEEN %(requested_at)s::timestamptz - interval '14 days' AND %(requested_at)s::timestamptz
)
```

### dapto_susceptible — bool or null (daptomycin)
Among window rows with organism matching `VRE` and `antibiotic = 'daptomycin'`: true iff all `S`; false if
any `I`/`R`; NULL if none.
<!-- sql:dapto_susceptible -->
```sql
SELECT bool_and(interpretation = 'S') FROM microbiology
WHERE patient_id = %(patient_id)s
  AND organism ~* 'VRE'
  AND lower(antibiotic) = 'daptomycin'
  AND collected_at BETWEEN %(requested_at)s::timestamptz - interval '14 days' AND %(requested_at)s::timestamptz
```

### vanc_allergy_severity — enum none|mild|moderate|severe|anaphylaxis (vancomycin, daptomycin)
Allergy rows with `agent ~* 'vancomycin'` and `recorded_at <= requested_at`. No rows → `'none'`.
Otherwise the highest severity, ignoring `unknown`; if only `unknown` rows exist → NULL.
(Red-man syndrome is recorded as mild/moderate, so it does not hit the `severe|anaphylaxis` nodes.)
<!-- sql:vanc_allergy_severity -->
```sql
SELECT CASE
  WHEN count(*) = 0 THEN 'none'
  ELSE (array_agg(severity ORDER BY array_position(ARRAY['mild','moderate','severe','anaphylaxis'], severity) DESC)
        FILTER (WHERE severity <> 'unknown'))[1]
END
FROM allergies
WHERE patient_id = %(patient_id)s
  AND agent ~* 'vancomycin'
  AND recorded_at <= %(requested_at)s::timestamptz
```

### pcn_allergy_severity — enum none|mild|moderate|severe|anaphylaxis, source `structured_or_text` (meropenem)
**Structured step**: allergy rows with `agent ~* '(penicillin|amoxicillin|ampicillin|piperacillin|nafcillin|oxacillin|dicloxacillin)'`
and `recorded_at <= requested_at`, highest severity ignoring `unknown`. **No matching row → NULL → fall back
to text**. (Cephalosporin rows such as cephalexin do not count.)
**Text step** (only when structured is NULL): the notes/justification may say e.g. "NKDA" or "no PCN
allergy" → `none`; "PCN → anaphylaxis" → `anaphylaxis`; "amoxicillin rash" → `moderate`; etc. If the
text never addresses allergies → NULL.
Note: an empty allergy table is deliberately *not* read as `none` — "no allergy recorded" is not "no allergy".
<!-- sql:pcn_allergy_severity -->
```sql
SELECT (array_agg(severity ORDER BY array_position(ARRAY['mild','moderate','severe','anaphylaxis'], severity) DESC)
        FILTER (WHERE severity <> 'unknown'))[1]
FROM allergies
WHERE patient_id = %(patient_id)s
  AND agent ~* '(penicillin|amoxicillin|ampicillin|piperacillin|nafcillin|oxacillin|dicloxacillin)'
  AND recorded_at <= %(requested_at)s::timestamptz
```

### on_dialysis — bool (daptomycin)
True iff `patients.dialysis_modality IN ('HD','PD')`.
<!-- sql:on_dialysis -->
```sql
SELECT dialysis_modality IN ('HD', 'PD') FROM patients WHERE patient_id = %(patient_id)s
```

### egfr_latest — number (daptomycin; also renal dose band selection)
Value of the most recent `labs` row with `test_code = 'EGFR'` and `collected_at <= requested_at`. NULL if none.
<!-- sql:egfr_latest -->
```sql
SELECT value::float FROM labs
WHERE patient_id = %(patient_id)s
  AND test_code = 'EGFR'
  AND collected_at <= %(requested_at)s::timestamptz
ORDER BY collected_at DESC
LIMIT 1
```

## Text variables

**Text source for every text variable** = the request's `justification` + the bodies of all
`clinical_notes` for the patient with `requested_at - 7 days <= written_at <= requested_at`.
Notes outside that window belong to earlier episodes (a patient can have an earlier request ≥ 35 days
before) and must be excluded. `indication` is consistent with the facts but is *not* guaranteed to carry
them — do not depend on it. All text variables describe the **current episode at the time of the
request** unless the definition says "history". A value of `false` is only correct when the text says so
(explicit negation, e.g. "not septic", "no pressors"); silence → NULL.

| variable | type | trees | definition |
|---|---|---|---|
| `septic_shock` | bool | vanc, mero | Sepsis requiring vasopressors to keep MAP ≥ 65 despite fluids, now. A resolved shock episode in the past is `false`/irrelevant. |
| `hemodynamic_instability` | bool | vanc | Currently hypotensive (SBP < 90 or MAP < 65, incl. intradialytic hypotension forcing HD to stop early) or on vasopressors. `septic_shock = true` implies this is `true`. |
| `prior_mdro_colonization` | bool | vanc | History, before this episode, of colonization or infection with MRSA, VRE, ESBL or CRE (e.g. "hx MRSA bacteremia 2024", "+MRSA nares swab last yr"). |
| `failed_first_line_therapy` | bool | mero | This episode: ≥ 48h of a first-line agent (pip-tazo, cefepime, ceftriaxone, …) with worsening or no improvement. |
| `source_control_achieved` | bool | mero | This episode: source controlled (abscess drained, infected catheter/PD catheter removed, obstruction relieved). `false` = source needing control is pending/declined. |
| `pulmonary_source` | bool | dapto | The infection being treated is pneumonia / lung source. |
| `vanc_failure_or_intolerance` | bool | dapto | Failure on vancomycin (persistent bacteremia ≥ 72h on therapeutic levels, clinical worsening) or vancomycin intolerance (vanc-attributed AKI, rash, infusion reaction NOT controllable by slowing the infusion). Red-man managed by slower infusion → `false`. |
| `pcn_allergy_severity` | enum | mero | Text fallback for the structured rule above. |

## Generation constraints that hold in the gold set (useful for tests)
- `septic_shock = true` ⇒ `hemodynamic_instability = true` (present in text).
- In the vancomycin tree, if `septic_shock` is absent from the text then `hemodynamic_instability` is absent too.
- If the patient has an MDRO isolate in microbiology before the window, `prior_mdro_colonization` is `true` (never false/absent).
- Absent text variables are genuinely not mentioned (validated by an independent LLM reader).
