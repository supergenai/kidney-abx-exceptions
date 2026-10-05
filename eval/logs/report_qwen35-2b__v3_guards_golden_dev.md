# Eval report

_Synthetic data; trees are illustrative._


## Qwen3.5-2B-4bit

### Per suite

| metric | golden_dev |
|---|---|
| n | 47 |
| error_rate | 0.000 |
| unsafe_approve | 0 |
| false_fill_rate | 0.000 |
| outcome_accuracy | 0.660 |
| path_accuracy | 0.766 |
| text_var_accuracy | 0.770 |
| missed_rate | 0.195 |
| wrong_value_rate | 0.035 |
| renal_agreement | 1.000 |
| quote_failed_rate | 0.092 |
| consistency_failed_rate | 0.108 |
| negation_conflict_rate | 0.008 |
| parse_failed_rate | 0.000 |
| case_pass_rate | 0.574 |
| invariance_pass_rate | - |
| latency_p50_s | 7.281 |
| latency_p95_s | 9.765 |
| tok_s_mean | 18.6 |


### golden_dev - per tree

| tree | n | outcome_accuracy | unsafe_approve | false_fill_rate | text_var_accuracy | case_pass_rate | invariance_pass_rate |
|---|---|---|---|---|---|---|---|
| daptomycin_v1 | 15 | 0.533 | 0 | 0.000 | 0.600 | 0.467 | - |
| meropenem_v1 | 16 | 0.875 | 0 | 0.000 | 0.891 | 0.812 | - |
| vancomycin_v1 | 16 | 0.562 | 0 | 0.000 | 0.738 | 0.438 | - |


### golden_dev - per case_type

| case_type | n | outcome_accuracy | unsafe_approve | false_fill_rate | text_var_accuracy | case_pass_rate | invariance_pass_rate |
|---|---|---|---|---|---|---|---|
| APPROVE_ELIGIBLE | 15 | 0.667 | 0 | 0.000 | 0.743 | 0.600 | - |
| DENY_ELIGIBLE | 14 | 0.643 | 0 | 0.000 | 0.811 | 0.571 | - |
| NEED_INFO | 6 | 1.000 | 0 | 0.000 | 0.900 | 0.833 | - |
| PHARMACIST_REVIEW | 12 | 0.500 | 0 | 0.000 | 0.710 | 0.417 | - |


### 10 worst cases

- **R0005** [golden_dev/APPROVE_ELIGIBLE] wrong_value, outcome: pred `PHARMACIST_REVIEW` gold `APPROVE_ELIGIBLE`
  - `failed_first_line_therapy` gold=`True` pred=`False` evidence: "No cultures sent to date, remains empiric. Strict I/O, f/u lactate q4h, f/u WBC AM. Will reassess pressor wean once on appropriate coverage."
  - `septic_shock` gold=`True` pred=`False` evidence: "No cultures sent to date, remains empiric. Strict I/O, f/u lactate q4h, f/u WBC AM. Will reassess pressor wean once on appropriate coverage."
- **R0059** [golden_dev/PHARMACIST_REVIEW] wrong_value, outcome: pred `DENY_ELIGIBLE` gold `PHARMACIST_REVIEW`
  - `hemodynamic_instability` gold=`True` pred=`False` evidence: "after fluids she remains soft, BP 84/52 MAP 63 on repeat cuff, but no pressor-dependent shock this episode - no norepinephrine/vasopressin at any point and none"
- **R0060** [golden_dev/PHARMACIST_REVIEW] wrong_value, outcome: pred `DENY_ELIGIBLE` gold `PHARMACIST_REVIEW`
  - `vanc_failure_or_intolerance` gold=`True` pred=`False` evidence: "Vanc off the table d/t severe DRESS"
- **R0007** [golden_dev/DENY_ELIGIBLE] outcome, missed: pred `NEED_INFO` gold `DENY_ELIGIBLE`
  - `pulmonary_source` gold=`True` pred=`None` evidence: ""
  - `vanc_failure_or_intolerance` gold=`True` pred=`None` evidence: ""
- **R0012** [golden_dev/PHARMACIST_REVIEW] outcome, missed: pred `NEED_INFO` gold `PHARMACIST_REVIEW`
  - `failed_first_line_therapy` gold=`False` pred=`None` evidence: "vanc + pip-tazo started ~18h ago in ED — only a few doses in, too early to judge response."
  - `septic_shock` gold=`True` pred=`None` evidence: "Now norepi 0.1, MAP 66-70. Still pressor-dependent despite volume."
- **R0041** [golden_dev/APPROVE_ELIGIBLE] outcome, missed: pred `NEED_INFO` gold `APPROVE_ELIGIBLE`
  - `hemodynamic_instability` gold=`True` pred=`None` evidence: "SBP nadir 76, s/p 500 mL NS + Trendelenburg, recovered to 100s/60s in chair"
  - `septic_shock` gold=`False` pred=`None` evidence: "SBP nadir 76, s/p 500 mL NS + Trendelenburg, recovered to 100s/60s in chair"
- **R0062** [golden_dev/PHARMACIST_REVIEW] outcome, missed: pred `NEED_INFO` gold `PHARMACIST_REVIEW`
  - `pulmonary_source` gold=`False` pred=`None` evidence: "CXR 5/28 no infiltrate, no lung source identified"
  - `vanc_failure_or_intolerance` gold=`False` pred=`None` evidence: "no rash, no infusion reaction, Cr 4.11 = baseline, i.e., no vanc-attributed kidney injury"
- **R0070** [golden_dev/APPROVE_ELIGIBLE] outcome, missed: pred `NEED_INFO` gold `APPROVE_ELIGIBLE`
  - `hemodynamic_instability` gold=`True` pred=`None` evidence: "BP 86/54 (MAP ~62)"
  - `septic_shock` gold=`False` pred=`None` evidence: "BP 86/54 (MAP ~62)"
- **R0006** [golden_dev/APPROVE_ELIGIBLE] outcome, missed: pred `NEED_INFO` gold `APPROVE_ELIGIBLE`
  - `pulmonary_source` gold=`False` pred=`None` evidence: "CXR 2/12 w/o infiltrate or consolidation, lungs CTA bilat."
- **R0029** [golden_dev/PHARMACIST_REVIEW] outcome, missed: pred `NEED_INFO` gold `PHARMACIST_REVIEW`
  - `prior_mdro_colonization` gold=`True` pred=`None` evidence: "+MRSA nares swab last yr"
