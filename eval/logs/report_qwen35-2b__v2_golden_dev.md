# Eval report

_Synthetic data; trees are illustrative._


## Qwen3.5-2B-4bit

### Per suite

| metric | golden_dev |
|---|---|
| n | 47 |
| error_rate | 0.000 |
| unsafe_approve | 0 |
| false_fill_rate | 0.176 |
| outcome_accuracy | 0.660 |
| path_accuracy | 0.809 |
| text_var_accuracy | 0.805 |
| missed_rate | 0.159 |
| wrong_value_rate | 0.035 |
| renal_agreement | 1.000 |
| quote_failed_rate | 0.062 |
| consistency_failed_rate | 0.069 |
| negation_conflict_rate | 0.031 |
| parse_failed_rate | 0.000 |
| case_pass_rate | 0.532 |
| invariance_pass_rate | - |
| latency_p50_s | 6.879 |
| latency_p95_s | 12.4 |
| tok_s_mean | 18.5 |


### golden_dev - per tree

| tree | n | outcome_accuracy | unsafe_approve | false_fill_rate | text_var_accuracy | case_pass_rate | invariance_pass_rate |
|---|---|---|---|---|---|---|---|
| daptomycin_v1 | 15 | 0.600 | 0 | 0.000 | 0.680 | 0.467 | - |
| meropenem_v1 | 16 | 0.812 | 0 | 0.167 | 0.891 | 0.688 | - |
| vancomycin_v1 | 16 | 0.562 | 0 | 0.333 | 0.786 | 0.438 | - |


### golden_dev - per case_type

| case_type | n | outcome_accuracy | unsafe_approve | false_fill_rate | text_var_accuracy | case_pass_rate | invariance_pass_rate |
|---|---|---|---|---|---|---|---|
| APPROVE_ELIGIBLE | 15 | 0.667 | 0 | 0.200 | 0.743 | 0.533 | - |
| DENY_ELIGIBLE | 14 | 0.643 | 0 | 0.333 | 0.811 | 0.429 | - |
| NEED_INFO | 6 | 0.833 | 0 | 0.143 | 1.000 | 0.833 | - |
| PHARMACIST_REVIEW | 12 | 0.583 | 0 | 0.000 | 0.806 | 0.500 | - |


### 10 worst cases

- **R0030** [golden_dev/NEED_INFO] false_fill, outcome: pred `DENY_ELIGIBLE` gold `NEED_INFO`
  - `hemodynamic_instability` gold=`None` pred=`False` evidence: "No signs of shock"
- **R0016** [golden_dev/DENY_ELIGIBLE] false_fill: pred `DENY_ELIGIBLE` gold `DENY_ELIGIBLE`
  - `source_control_achieved` gold=`None` pred=`False` evidence: "Repeat blood cx drawn 3/3: no growth to date"
- **R0064** [golden_dev/APPROVE_ELIGIBLE] false_fill: pred `APPROVE_ELIGIBLE` gold `APPROVE_ELIGIBLE`
  - `hemodynamic_instability` gold=`None` pred=`False` evidence: "T 37.1, HR 92, RR 16, SpO2 98% RA"
- **R0005** [golden_dev/APPROVE_ELIGIBLE] wrong_value, outcome: pred `PHARMACIST_REVIEW` gold `APPROVE_ELIGIBLE`
  - `failed_first_line_therapy` gold=`True` pred=`False` evidence: "No cultures sent to date, remains empiric. Strict I/O, f/u lactate q4h, f/u WBC AM. Will reassess pressor wean once on appropriate coverage."
  - `septic_shock` gold=`True` pred=`False` evidence: "No cultures sent to date, remains empiric. Strict I/O, f/u lactate q4h, f/u WBC AM. Will reassess pressor wean once on appropriate coverage."
- **R0059** [golden_dev/PHARMACIST_REVIEW] wrong_value, outcome: pred `DENY_ELIGIBLE` gold `PHARMACIST_REVIEW`
  - `hemodynamic_instability` gold=`True` pred=`False` evidence: "after fluids she remains soft, BP 84/52 MAP 63 on repeat cuff, but no pressor-dependent shock this episode - no norepinephrine/vasopressin at any point and none"
- **R0060** [golden_dev/PHARMACIST_REVIEW] wrong_value, outcome: pred `DENY_ELIGIBLE` gold `PHARMACIST_REVIEW`
  - `vanc_failure_or_intolerance` gold=`True` pred=`False` evidence: "Vanc off the table d/t severe DRESS"
- **R0041** [golden_dev/APPROVE_ELIGIBLE] outcome, missed: pred `NEED_INFO` gold `APPROVE_ELIGIBLE`
  - `hemodynamic_instability` gold=`True` pred=`None` evidence: "SBP nadir 76, s/p 500 mL NS + Trendelenburg, recovered to 100s/60s in chair"
  - `septic_shock` gold=`False` pred=`None` evidence: "SBP nadir 76, s/p 500 mL NS + Trendelenburg, recovered to 100s/60s in chair"
- **R0070** [golden_dev/APPROVE_ELIGIBLE] outcome, missed: pred `NEED_INFO` gold `APPROVE_ELIGIBLE`
  - `hemodynamic_instability` gold=`True` pred=`None` evidence: "BP 86/54 (MAP ~62)"
  - `septic_shock` gold=`False` pred=`None` evidence: "BP 86/54 (MAP ~62)"
- **R0011** [golden_dev/DENY_ELIGIBLE] outcome, missed: pred `NEED_INFO` gold `DENY_ELIGIBLE`
  - `source_control_achieved` gold=`True` pred=`None` evidence: "PD catheter removed 2/23 — source control achieved, prior CT w/o undrained collection."
- **R0012** [golden_dev/PHARMACIST_REVIEW] outcome, missed: pred `NEED_INFO` gold `PHARMACIST_REVIEW`
  - `septic_shock` gold=`True` pred=`None` evidence: "Now norepi 0.1, MAP 66-70. Still pressor-dependent despite volume."
