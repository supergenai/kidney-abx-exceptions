"""Synthetic kidney-care antibiotic exception dataset generator. NO PHI — every record is fake.

Scenario-first: for each request pick a target (path, outcome) in the drug's tree, fix every variable,
write structured rows that reproduce the structured variables, then have Kimi write notes + justification
that convey the text variables. A second Kimi call (independent reader) validates every text.

Usage:  uv run python data/generate.py [--reset-schema] [--workers 8] [--no-llm]
Kimi outputs are cached in data/cache/kimi_cache.jsonl, so reruns are deterministic and free.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from walker import enumerate_paths, walk  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TREES_DIR = ROOT / "trees"
CACHE_PATH = DATA / "cache" / "kimi_cache.jsonl"
SEED = 20261005
MODEL = "kimi-k3"
N_PATIENTS = 50
N_TWO_REQUESTS = 30  # -> 80 requests
SYNTH_HEADER = "*** SYNTHETIC RECORD — FAKE PATIENT — NOT FOR CLINICAL USE ***"
DRUGS = ["vancomycin", "meropenem", "daptomycin"]
OUTCOMES = ["APPROVE_ELIGIBLE", "DENY_ELIGIBLE", "PHARMACIST_REVIEW"]
SEV_ORDER = ["mild", "moderate", "severe", "anaphylaxis"]
UTC = timezone.utc

TREES = {d: json.loads((TREES_DIR / f"{d}.json").read_text()) for d in DRUGS}
RENAL = json.loads((TREES_DIR / "renal_dosing.json").read_text())

# ---------------------------------------------------------------------------------------------
# Text variable guidance (shared by writer + reader prompts)
# ---------------------------------------------------------------------------------------------
TEXT_DEFS = {
    "septic_shock": "Current episode: sepsis requiring vasopressors to keep MAP >= 65 despite fluids. "
    "A past, resolved shock episode does not count.",
    "hemodynamic_instability": "Currently hypotensive (SBP < 90 or MAP < 65, including intradialytic hypotension "
    "forcing HD to stop early) or on vasopressors at the time of the request.",
    "prior_mdro_colonization": "History BEFORE this episode of colonization or infection with MRSA, VRE, ESBL or CRE.",
    "failed_first_line_therapy": "Current episode: >= 48h of a first-line agent (pip-tazo, cefepime, ceftriaxone, etc.) "
    "with clinical worsening or no improvement.",
    "source_control_achieved": "Current episode: infection source has been controlled (abscess drained, infected "
    "catheter/PD catheter removed, obstruction relieved). false = a source needing control is still pending or declined.",
    "pulmonary_source": "The infection being treated now is pneumonia / a lung source.",
    "vanc_failure_or_intolerance": "Failure on vancomycin (persistent bacteremia >= 72h on therapeutic levels, or clinical "
    "worsening on vanc) or vancomycin intolerance (vanc-attributed AKI, rash, infusion reaction NOT controllable by "
    "slowing the infusion). Red-man reaction managed by slower infusion does NOT count.",
    "pcn_allergy_severity": "Penicillin-class allergy severity: one of none|mild|moderate|severe|anaphylaxis. "
    "'NKDA' or 'no penicillin allergy' = none.",
}
ABSENT_HINTS = {
    "septic_shock": "do not mention shock, sepsis severity, vasopressors/pressors, blood pressure, MAP, perfusion or lactate interpretation",
    "hemodynamic_instability": "do not mention blood pressure, MAP, hypotension, pressors, or how HD sessions went hemodynamically",
    "prior_mdro_colonization": "do not mention any past or current history of MRSA/VRE/ESBL/CRE, screening swabs, isolation or contact precautions",
    "failed_first_line_therapy": "do not mention any antibiotic given earlier in this episode, nor the response to any treatment so far",
    "source_control_achieved": "do not mention drainage, procedures, catheter removal/exchange, imaging for collections, or anything about controlling the source",
    "pulmonary_source": "do not state or hint at the anatomic source/site of infection (no lungs, CXR, cough, sputum, skin, urine, line, bone, etc.)",
    "vanc_failure_or_intolerance": "do not mention any prior or current vancomycin course, levels, response, or tolerance",
    "pcn_allergy_severity": "do not mention allergies at all (no 'NKDA', no allergy list)",
}
NOTE_TYPES = [
    ("Nephrology Progress Note", "Nephrology attending"),
    ("Hospitalist Progress Note", "Hospitalist"),
    ("ID Consult", "Infectious diseases fellow"),
    ("Dialysis Treatment Note", "Dialysis RN"),
    ("Pharmacy Note", "Clinical pharmacist"),
    ("H&P", "Admitting resident"),
    ("Progress Note", "Physician assistant"),
]
DISTRACTORS = {
    "template_other_patient": "include a leftover template/copy-paste fragment that clearly belongs to a DIFFERENT patient "
    "(e.g. '[template - bed 14, not this pt]: ...on linezolid...'), unmistakably marked as not this patient",
    "resolved_past_episode": "mention a remote, fully resolved past problem (> 1 year ago, clearly dated) that is unrelated to "
    "every fact listed below (e.g. a remote fracture, an old C. diff colitis, resolved, a prior AV "
    "fistula revision)",
    "copy_forward": "include a copy-forwarded block from an older note (marked e.g. '(copied fwd from 3/2)') with stale "
    "vitals or plan that a later line in the same note explicitly supersedes — never let stale text contradict a fact",
    "irrelevant_heavy": "include lots of irrelevant detail (phosphate binder, PTH, diet, transport issues, fistula bruit, "
    "family meeting)",
}
PRESCRIBERS = [
    "Dr. Testa Fakeworth (SYNTHETIC)", "Dr. Mock Sampleton (SYNTHETIC)", "Dr. Ima Placeholder (SYNTHETIC)",
    "Dr. Dummy Rowe (SYNTHETIC)", "Dr. Faux Kidneyman (SYNTHETIC)", "Dr. Synth Ettica (SYNTHETIC)",
]
CLINICS = ["Synthetic Kidney Clinic North", "Synthetic Dialysis Unit East", "Synthetic Renal Ward 4B",
           "Synthetic Home Dialysis Program"]


# ---------------------------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------------------------
EGFR_RANGE = {"3a": (46, 58), "3b": (32, 43), "4": (17, 28), "5": (8, 13), "5D": (4, 9)}


@dataclass
class Patient:
    patient_id: str
    mrn: str
    age: int
    sex: str
    weight_kg: float
    ckd_stage: str
    dialysis_modality: str
    dialysis_days: str | None
    clinic: str
    egfr_base: float
    allergies: list[dict] = field(default_factory=list)
    pcn_text_value: str | None = None  # used only when no structured PCN row exists
    mdro_history: list[datetime] = field(default_factory=list)

    def pcn_struct(self):
        return max_severity(a for a in self.allergies if re.search(
            r"(penicillin|amoxicillin|ampicillin|piperacillin|nafcillin|oxacillin|dicloxacillin)", a["agent"], re.I))

    def vanc_struct(self):
        rows = [a for a in self.allergies if re.search("vancomycin", a["agent"], re.I)]
        return "none" if not rows else max_severity(rows)


def max_severity(rows):
    sev = [r["severity"] for r in rows if r["severity"] != "unknown"]
    return max(sev, key=SEV_ORDER.index) if sev else None


def make_patients(rng: random.Random) -> list[Patient]:
    mix = ["HD"] * 20 + ["PD"] * 5 + ["3a"] * 5 + ["3b"] * 6 + ["4"] * 8 + ["5"] * 6
    rng.shuffle(mix)
    pts = []
    for i, m in enumerate(mix, start=1):
        modality = m if m in ("HD", "PD") else "none"
        stage = "5D" if modality != "none" else m
        days = {"HD": rng.choice(["MWF", "TTS"]), "PD": rng.choice(["CAPD 4 exchanges/day", "APD nightly"])}.get(modality)
        lo, hi = EGFR_RANGE[stage]
        p = Patient(
            patient_id=f"P{i:03d}", mrn=f"SYN-FAKE-{100000 + i * 37}", age=rng.randint(28, 89),
            sex=rng.choice("MF"), weight_kg=round(rng.uniform(48, 118), 1), ckd_stage=stage,
            dialysis_modality=modality, dialysis_days=days,
            clinic="Synthetic Home Dialysis Program" if modality == "PD" else rng.choice(CLINICS[:3]),
            egfr_base=rng.uniform(lo + 1, hi - 1),
        )
        rec = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=rng.randint(0, 360))
        pcn = rng.choices(["none", "mild", "moderate", "severe", "anaphylaxis"], [55, 8, 12, 10, 15])[0]
        if pcn != "none":
            agent, reaction = {
                "mild": ("Amoxicillin", "itching"), "moderate": (rng.choice(["Penicillin", "Amoxicillin"]), "hives/rash"),
                "severe": ("Penicillin", "angioedema"), "anaphylaxis": (rng.choice(["Penicillin", "Ampicillin"]), "anaphylaxis"),
            }[pcn]
            p.allergies.append(dict(agent=agent, reaction=reaction, severity=pcn, recorded_at=rec))
        else:
            p.pcn_text_value = rng.choices(["none", "anaphylaxis", "moderate"], [70, 15, 15])[0]
        vanc = rng.choices(["none", "mild", "moderate", "severe", "anaphylaxis"], [72, 6, 14, 5, 3])[0]
        if vanc != "none":
            reaction = {"mild": "flushing", "moderate": "red man syndrome", "severe": "DRESS",
                        "anaphylaxis": "anaphylaxis"}[vanc]
            p.allergies.append(dict(agent="Vancomycin", reaction=reaction, severity=vanc, recorded_at=rec + timedelta(days=3)))
        if rng.random() < 0.45:
            agent, reaction, sev = rng.choice([
                ("Sulfamethoxazole-trimethoprim", "rash", "moderate"), ("Codeine", "nausea", "mild"),
                ("Latex", "contact dermatitis", "mild"), ("Iodinated contrast", "unknown", "unknown"),
                ("Cephalexin", "rash", "mild"), ("Heparin", "HIT", "severe"),
            ])
            p.allergies.append(dict(agent=agent, reaction=reaction, severity=sev, recorded_at=rec + timedelta(days=10)))
        pts.append(p)
    return pts


# ---------------------------------------------------------------------------------------------
# Scenario sampling
# ---------------------------------------------------------------------------------------------
def text_vars(tree):
    return [v for v, s in tree["variables"].items() if s["source"] in ("text", "structured_or_text")]


def fixed_values(drug: str, p: Patient, egfr_latest: float) -> dict:
    fx = {"vanc_allergy_severity": p.vanc_struct(), "on_dialysis": p.dialysis_modality != "none",
          "egfr_latest": egfr_latest}
    if p.pcn_struct() is not None:
        fx["pcn_allergy_severity"] = p.pcn_struct()
    else:
        fx["pcn_allergy_severity"] = p.pcn_text_value
    if p.mdro_history:
        fx["prior_mdro_colonization"] = True
    return {k: v for k, v in fx.items() if k in TREES[drug]["variables"]}


def sample_values(drug: str, fx: dict, rng: random.Random) -> dict:
    b = lambda pr: rng.random() < pr  # noqa: E731
    if drug == "vancomycin":
        v = {"mrsa_isolated": b(0.5), "septic_shock": b(0.35), "prior_mdro_colonization": b(0.45)}
        v["mrsa_vanc_susceptible"] = b(0.7) if v["mrsa_isolated"] else None
        v["hemodynamic_instability"] = True if v["septic_shock"] else b(0.4)
    elif drug == "meropenem":
        v = {"carbapenem_resistant": b(0.15), "esbl_isolated": b(0.5), "septic_shock": b(0.4),
             "failed_first_line_therapy": b(0.5), "source_control_achieved": b(0.5)}
    else:
        v = {"pulmonary_source": b(0.2), "vre_isolated": b(0.4), "vanc_failure_or_intolerance": b(0.5)}
        v["dapto_susceptible"] = b(0.7) if v["vre_isolated"] else None
        v["mrsa_isolated"] = b(0.1 if v["vre_isolated"] else 0.55)
    v.update(fx)
    return v


def feasible_assignment(drug, fx, target_path, target_outcome, rng, tries=600):
    for _ in range(tries):
        v = sample_values(drug, fx, rng)
        out, path = walk(TREES[drug], v)
        if out == target_outcome and path == target_path:
            return v
    return None


@dataclass
class Request:
    request_id: str
    patient: Patient
    drug: str
    requested_at: datetime
    target: str
    values: dict = field(default_factory=dict)  # full intended values (None = absent/unknown)
    present_text: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    expected_path: list[str] = field(default_factory=list)
    micro: list[dict] = field(default_factory=list)
    labs: list[dict] = field(default_factory=list)
    dose_mg: float = 0
    frequency: str = ""
    route: str = "IV"
    indication: str = ""
    prescriber: str = ""
    renal_band: str = ""
    dose_ok: bool = True
    notes: list[dict] = field(default_factory=list)
    justification: str = ""
    distractor: str = ""
    n_notes: int = 1


PATH_USE: Counter = Counter()


def plan_scenario(req: Request, egfr_latest: float, rng: random.Random) -> None:
    drug, p, tree = req.drug, req.patient, TREES[req.drug]
    fx = fixed_values(drug, p, egfr_latest)
    pcn_from_text = drug == "meropenem" and p.pcn_struct() is None
    tvars = [t for t in text_vars(tree) if t != "pcn_allergy_severity" or pcn_from_text]
    # variables that may be nulled (absent from text): text vars, but never prior_mdro when structured history exists
    nullable = [t for t in tvars if not (t == "prior_mdro_colonization" and p.mdro_history)]

    paths = enumerate_paths(tree)
    feasible = {}
    for path, outcome in paths:
        v = feasible_assignment(drug, fx, path, outcome, rng)
        if v is not None:
            feasible[(tuple(path), outcome)] = v

    target = req.target
    if target == "NEED_INFO":
        cands = [k for k in feasible if any(tree["nodes"][n]["var"] in nullable for n in k[0])]
    else:
        cands = [k for k in feasible if k[1] == target]
    if not cands:  # infeasible for this patient: fall back to any feasible decided outcome
        cands = [k for k in feasible if k[1] in OUTCOMES]
        req.target = "FALLBACK"
    # prefer the least-used feasible path so far, for path diversity
    least = min(PATH_USE[(drug, k)] for k in cands)
    key = rng.choice(sorted(k for k in cands if PATH_USE[(drug, k)] == least))
    PATH_USE[(drug, key)] += 1
    values = dict(feasible[key])
    on_path = {tree["nodes"][n]["var"] for n in key[0]}

    nulled = None
    if target == "NEED_INFO" and req.target != "FALLBACK":
        nulled = rng.choice([tree["nodes"][n]["var"] for n in key[0] if tree["nodes"][n]["var"] in nullable])
        values[nulled] = None
    # off-path text vars: present 70%
    for t in tvars:
        if t not in on_path and t in nullable and rng.random() < 0.3:
            values[t] = None
    # consistency constraints (vancomycin tree)
    if drug == "vancomycin":
        if values["septic_shock"] is None:
            values["hemodynamic_instability"] = None
        if values["septic_shock"] is True:
            values["hemodynamic_instability"] = True
    if nulled == "hemodynamic_instability" and values.get("septic_shock") is True:
        raise AssertionError("constraint violation")

    req.values = values
    req.present_text = [t for t in tvars if values[t] is not None]
    req.expected_outcome, req.expected_path = walk(tree, values)
    if target == "NEED_INFO" and req.target != "FALLBACK":
        assert req.expected_outcome == "NEED_INFO", (req.request_id, values)


# ---------------------------------------------------------------------------------------------
# Structured rows
# ---------------------------------------------------------------------------------------------
def iso(organism, specimen, when, panel):
    return [dict(specimen=specimen, collected_at=when, organism=organism, antibiotic=a, interpretation=i, mic=m)
            for a, i, m in panel]


def make_micro(req: Request, rng: random.Random, prev_req_at: datetime | None) -> None:
    v, p, t = req.values, req.patient, req.requested_at
    rows: list[dict] = []
    in_win = lambda: t - timedelta(days=rng.uniform(1, 10), hours=rng.uniform(0, 12))  # noqa: E731
    hd, pd = p.dialysis_modality == "HD", p.dialysis_modality == "PD"
    bc = "PD effluent" if pd and rng.random() < 0.5 else ("Blood culture" if rng.random() < 0.7 else "Wound swab")
    if v.get("pulmonary_source"):
        bc = "Sputum"
    mdro_now = False
    if v.get("mrsa_isolated"):
        vs = v.get("mrsa_vanc_susceptible")
        vanc = ("S", rng.choice(["0.5", "1"])) if vs in (True, None) else (rng.choice(["I", "R"]), None)
        if vanc[1] is None:
            vanc = (vanc[0], "4" if vanc[0] == "I" else "16")
        rows += iso("Staphylococcus aureus (MRSA)", bc, in_win(), [
            ("oxacillin", "R", ">=4"), ("vancomycin", vanc[0], vanc[1]), ("daptomycin", "S", "0.5"),
            ("linezolid", "S", "2"), ("trimethoprim-sulfamethoxazole", "S", "<=0.5/9.5")])
        mdro_now = True
    if v.get("vre_isolated"):
        ds = v.get("dapto_susceptible")
        dap = ("S", "2") if ds in (True, None) else ("R", "8")
        rows += iso("Enterococcus faecium (VRE)", "Urine culture" if bc == "Wound swab" else bc, in_win(), [
            ("vancomycin", "R", ">=32"), ("ampicillin", "R", ">=32"), ("linezolid", "S", "2"),
            ("daptomycin", dap[0], dap[1])])
        mdro_now = True
    cr = v.get("carbapenem_resistant")
    if v.get("esbl_isolated"):
        org = rng.choice(["Escherichia coli (ESBL)", "Klebsiella pneumoniae (ESBL)"])
        carb = ("R", ">=16") if cr else ("S", "<=0.25")
        rows += iso(org, rng.choice(["Urine culture", "Blood culture"]), in_win(), [
            ("ceftriaxone", "R", ">=64"), ("cefepime", "R", ">=32"), ("piperacillin-tazobactam", rng.choice(["S", "I"]), "16"),
            ("ertapenem", carb[0], ">=8" if cr else "<=0.5"), ("meropenem", carb[0], carb[1]), ("ciprofloxacin", "R", ">=4")])
        mdro_now = True
    elif cr:
        if rng.random() < 0.5:
            rows += iso("Klebsiella pneumoniae (CRE)", "Blood culture", in_win(), [
                ("ceftriaxone", "R", ">=64"), ("meropenem", "R", ">=16"), ("ertapenem", "R", ">=8"),
                ("ceftazidime-avibactam", "S", "2")])
            mdro_now = True
        else:
            rows += iso("Pseudomonas aeruginosa", rng.choice(["Sputum", "Urine culture"]), in_win(), [
                ("meropenem", "R", ">=16"), ("imipenem", "R", ">=16"), ("cefepime", "S", "4"),
                ("piperacillin-tazobactam", "S", "16")])
    # in-window distractors that do not change any variable
    if not v.get("mrsa_isolated") and rng.random() < 0.35:
        rows += iso("Staphylococcus aureus (MSSA)", "Blood culture", in_win(), [
            ("oxacillin", "S", "0.5"), ("cefazolin", "S", "<=2"), ("vancomycin", "S", "1")])
    if not v.get("esbl_isolated") and not cr and rng.random() < 0.3:
        rows += iso("Escherichia coli", "Urine culture", in_win(), [
            ("ceftriaxone", "S", "<=1"), ("piperacillin-tazobactam", "S", "<=4"), ("meropenem", "S", "<=0.25")])
    if not v.get("vre_isolated") and rng.random() < 0.25:
        rows += iso("Enterococcus faecalis", "Urine culture", in_win(), [
            ("ampicillin", "S", "<=2"), ("vancomycin", "S", "1")])
    if rng.random() < 0.4:
        rows.append(dict(specimen="Blood culture", collected_at=in_win(), organism=None, antibiotic=None,
                         interpretation=None, mic=None))
    # out-of-window MDRO distractor: only before the first request and only when consistent with prior-MDRO facts
    allow_old_mdro = prev_req_at is None and req.values.get("prior_mdro_colonization", True) is not False \
        and not (req.drug == "vancomycin" and req.values.get("prior_mdro_colonization") is None)
    if allow_old_mdro and rng.random() < 0.3:
        when = t - timedelta(days=rng.uniform(30, 90))
        org, panel = rng.choice([
            ("Staphylococcus aureus (MRSA)", [("oxacillin", "R", ">=4"), ("vancomycin", "S", "1")]),
            ("Escherichia coli (ESBL)", [("ceftriaxone", "R", ">=64"), ("meropenem", "S", "<=0.25")]),
            ("Enterococcus faecium (VRE)", [("vancomycin", "R", ">=32"), ("daptomycin", "S", "2")]),
        ])
        rows += iso(org, rng.choice(["Blood culture", "Urine culture", "Nares swab"]), when, panel)
        p.mdro_history.append(when)
    if mdro_now:
        p.mdro_history.append(t)
    req.micro = rows


def make_labs(req: Request, rng: random.Random) -> float:
    p, t, v = req.patient, req.requested_at, req.values
    lo, hi = EGFR_RANGE[p.ckd_stage]
    labs = []
    days = sorted(rng.sample(range(1, 22), k=rng.randint(3, 5)), reverse=True) + [0]
    egfr = None
    for d in days:
        when = t - timedelta(days=d, hours=rng.uniform(2, 10))
        egfr = round(min(hi, max(lo, p.egfr_base + rng.uniform(-2, 2))), 0)
        creat = round(min(12.5, 90 / egfr) * rng.uniform(0.95, 1.05), 2)
        labs.append(dict(test_code="EGFR", test_name="eGFR (CKD-EPI 2021)", value=egfr, unit="mL/min/1.73m2", collected_at=when))
        labs.append(dict(test_code="CREAT", test_name="Creatinine", value=creat, unit="mg/dL", collected_at=when))
    shock = v.get("septic_shock") is True or v.get("hemodynamic_instability") is True
    for d in sorted(rng.sample(range(0, 5), k=2), reverse=True):
        when = t - timedelta(days=d, hours=rng.uniform(1, 6))
        labs.append(dict(test_code="WBC", test_name="White blood cell count",
                         value=round(rng.uniform(14, 24) if shock else rng.uniform(7, 15), 1), unit="10^9/L", collected_at=when))
        labs.append(dict(test_code="CRP", test_name="C-reactive protein", value=round(rng.uniform(40, 260)), unit="mg/L", collected_at=when))
    labs.append(dict(test_code="LACTATE", test_name="Lactate",
                     value=round(rng.uniform(3.2, 7.5) if v.get("septic_shock") else rng.uniform(0.8, 1.9), 1),
                     unit="mmol/L", collected_at=t - timedelta(hours=rng.uniform(2, 20))))
    labs.append(dict(test_code="K", test_name="Potassium", value=round(rng.uniform(3.6, 5.8), 1), unit="mmol/L",
                     collected_at=t - timedelta(days=1, hours=rng.uniform(0, 8))))
    labs.append(dict(test_code="HGB", test_name="Hemoglobin", value=round(rng.uniform(8.2, 11.8), 1), unit="g/dL",
                     collected_at=t - timedelta(days=1, hours=rng.uniform(0, 8))))
    if req.drug == "daptomycin":
        labs.append(dict(test_code="CK", test_name="Creatine kinase", value=round(rng.uniform(40, 260)), unit="U/L",
                         collected_at=t - timedelta(hours=rng.uniform(2, 30))))
    if req.drug == "vancomycin" and rng.random() < 0.3:
        labs.append(dict(test_code="VANC_TROUGH", test_name="Vancomycin trough", value=round(rng.uniform(8, 22), 1),
                         unit="mg/L", collected_at=t - timedelta(days=rng.uniform(25, 60))))
    req.labs = labs
    return egfr


# ---------------------------------------------------------------------------------------------
# Renal dose check (mirror of trees/renal_dosing.json semantics)
# ---------------------------------------------------------------------------------------------
def renal_band(drug, p: Patient, egfr):
    bands = RENAL["drugs"][drug]["bands"]
    if p.dialysis_modality in ("HD", "PD"):
        return next(b for b in bands if b["band"] == p.dialysis_modality)
    for b in bands:
        if "egfr_min" in b and b["egfr_min"] <= egfr and (b["egfr_max"] is None or egfr < b["egfr_max"]):
            return b
    raise ValueError((drug, egfr))


def dose_ok(drug, p, band, dose, freq):
    cap = band["max_dose_mg"]
    if "max_mg_per_kg" in band:
        cap = min(cap, band["max_mg_per_kg"] * p.weight_kg)
    return dose <= cap and RENAL["frequency_hours"][freq] >= band["min_interval_hours"]


def make_order(req: Request, egfr: float, rng: random.Random) -> None:
    p, v, drug = req.patient, req.values, req.drug
    band = renal_band(drug, p, egfr)
    compliant = rng.random() < 0.75
    freq = band["frequency"]
    if drug == "daptomycin":
        dose = min(1000, round(rng.uniform(6, 8) * p.weight_kg / 50) * 50)
        dose = max(300, min(dose, int(band["max_mg_per_kg"] * p.weight_kg) // 50 * 50))
    elif drug == "vancomycin":
        dose = rng.choice([x for x in (750, 1000, 1250, 1500, 2000) if x <= band["max_dose_mg"]])
    else:
        dose = rng.choice([x for x in (500, 1000) if x <= band["max_dose_mg"]])
    if not compliant:
        if rng.random() < 0.5:
            dose = {"daptomycin": round(11 * p.weight_kg / 50) * 50, "vancomycin": band["max_dose_mg"] + 500,
                    "meropenem": band["max_dose_mg"] * 2}[drug]
        else:
            freq = {"q8h": "q6h", "q12h": "q8h", "q24h": "q12h", "q48h": "q24h", "post-HD": "q24h", "q5d": "q48h"}[freq]
    req.dose_mg, req.frequency = float(dose), freq
    req.renal_band = band["band"]
    req.dose_ok = dose_ok(drug, p, band, dose, freq)
    pd = p.dialysis_modality == "PD"
    peritonitis = pd and any(m["specimen"] == "PD effluent" for m in req.micro)
    req.route = "IP" if drug == "vancomycin" and peritonitis and freq == "q5d" else "IV"
    req.prescriber = rng.choice(PRESCRIBERS)
    # indication: consistent with structured data; never names a text variable unless that var is present
    micro_orgs = {m["organism"] for m in req.micro if m["organism"]}
    has = lambda s: any(s in o for o in micro_orgs)  # noqa: E731
    if drug == "vancomycin":
        if v["mrsa_isolated"]:
            req.indication = "MRSA PD peritonitis" if peritonitis else rng.choice(["MRSA bacteremia", "MRSA infection, see cultures"])
        else:
            req.indication = "PD peritonitis, empiric gram-positive coverage" if peritonitis else \
                rng.choice(["Empiric gram-positive coverage", "Fever, empiric MRSA coverage pending cultures"])
    elif drug == "meropenem":
        if v["esbl_isolated"]:
            req.indication = "ESBL " + ("E. coli" if has("Escherichia coli (ESBL)") else "Klebsiella") + " infection"
        elif v["carbapenem_resistant"]:
            req.indication = "Gram-negative infection, resistant isolate"
        else:
            req.indication = rng.choice(["Gram-negative infection, empiric", "Broad-spectrum gram-negative coverage"])
    else:
        pulm = v.get("pulmonary_source")
        bug = "VRE" if v["vre_isolated"] else ("MRSA" if v["mrsa_isolated"] else "Gram-positive")
        if pulm is True:
            req.indication = f"{bug} pneumonia"
        elif pulm is False:
            req.indication = rng.choice([f"{bug} bacteremia, line source", f"{bug} infection, non-pulmonary source"])
        else:
            req.indication = f"{bug} infection"


# ---------------------------------------------------------------------------------------------
# Kimi
# ---------------------------------------------------------------------------------------------
class Kimi:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.lock = threading.Lock()
        self.cache: dict[str, dict] = {}
        self.usage = Counter()
        if CACHE_PATH.exists():
            for line in CACHE_PATH.read_text().splitlines():
                rec = json.loads(line)
                self.cache[rec["key"]] = rec
        if enabled:
            from openai import OpenAI
            load_dotenv(ROOT / ".env")
            self.client = OpenAI(api_key=os.environ["MOONSHOT_API_KEY"],
                                 base_url=os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1"),
                                 timeout=300, max_retries=1)

    def chat_json(self, system: str, user: str) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = hashlib.sha256(json.dumps([MODEL, messages]).encode()).hexdigest()
        with self.lock:
            hit = self.cache.get(key)
        if hit:
            with self.lock:
                self.usage["cached_calls"] += 1
                self.usage["prompt_tokens_orig"] += hit["usage"]["prompt_tokens"]
                self.usage["completion_tokens_orig"] += hit["usage"]["completion_tokens"]
            return json.loads(hit["content"])
        if not self.enabled:
            raise RuntimeError("cache miss with --no-llm")
        import openai
        for wait in [2, 4, 8, 15, 30, 30, 60, 60, 60, 60]:  # transport/rate-limit errors are not validation attempts
            try:
                resp = self.client.chat.completions.create(model=MODEL, messages=messages,
                                                           response_format={"type": "json_object"})
                break
            except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError):
                with self.lock:
                    self.usage["api_retries"] += 1
                time.sleep(wait + random.random() * 2)
        else:
            raise RuntimeError("Kimi API unavailable after retries")
        content = resp.choices[0].message.content
        usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        parsed = json.loads(content)  # raises on malformed -> caller counts as failed attempt
        with self.lock:
            self.usage["live_calls"] += 1
            self.usage["prompt_tokens"] += usage["prompt_tokens"]
            self.usage["completion_tokens"] += usage["completion_tokens"]
            self.usage["prompt_tokens_orig"] += usage["prompt_tokens"]
            self.usage["completion_tokens_orig"] += usage["completion_tokens"]
            rec = {"key": key, "content": content, "usage": usage}
            self.cache[key] = rec
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with CACHE_PATH.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        return parsed


WRITER_SYSTEM = """You write SYNTHETIC clinical documentation for a test dataset (fake patients, no real people).
Output strict JSON only: {"notes": [{"note_type": str, "author_role": str, "days_before_request": int 0-6, "body": str}], "justification": str}
Style: realistic inpatient/nephrology charting. Terse abbreviations (pt, hx, abx, s/p, HD MWF, c/o, w/, d/t, f/u, NKDA where true),
negations ("no signs of shock", "not septic", "denies"), copy-forward noise, irrelevant details, vitals/plan fragments.
Vary length (40-250 words per note). The justification is the prescriber's free-text reason on the restricted-antibiotic
exception form (1-4 sentences, may be terse). Never use the variable names themselves (e.g. never write "septic_shock").
Prefer natural clinical phrasing over checklist-style statements (e.g. "+MRSA nares swab 2024" or "on levo 0.1, MAP 58"
rather than "prior MDRO colonization: yes"), but each required fact must still be unambiguous. Never invent real names; refer to the patient as "pt" or "the patient". Do not invent culture results beyond those given."""

READER_SYSTEM = """You are a meticulous clinical pharmacist auditing documentation (synthetic test data).
Given ONLY the provided text, determine each requested variable for the CURRENT episode at the time of the exception
request, using the exact definitions given. Ignore anything explicitly about a different patient. Do not infer a value
from lab numbers or general likelihood: if the text does not address the variable, return null. A value of false
requires the text to actually state it (e.g. an explicit negation).
Output strict JSON only: {"<variable>": {"value": <true|false|enum string|null>, "evidence": "<short verbatim quote or null>"}}"""


def fact_lines(req: Request) -> tuple[list[str], list[str]]:
    must, absent = [], []
    for var in text_vars(TREES[req.drug]):
        if var == "pcn_allergy_severity" and req.patient.pcn_struct() is not None:
            continue  # structured value; handled via allergy list
        val = req.values.get(var)
        if val is None:
            absent.append(f"- {ABSENT_HINTS[var]}  [topic: {TEXT_DEFS[var]}]")
        else:
            shown = json.dumps(val)
            if var == "pcn_allergy_severity":
                shown = {"none": "NO penicillin/beta-lactam allergy (state this explicitly)",
                         "moderate": "penicillin allergy with hives/rash (moderate) — NOT listed in the EHR allergy table, documented only in notes",
                         "anaphylaxis": "penicillin allergy with ANAPHYLAXIS — NOT listed in the EHR allergy table, documented only in notes"}[val]
            must.append(f"- {TEXT_DEFS[var]}  => VALUE: {shown}")
    return must, absent


def writer_prompt(req: Request, feedback: str | None) -> str:
    p = req.patient
    micro = sorted({(m["specimen"], m["organism"] or "no growth", m["collected_at"].date().isoformat())
                    for m in req.micro if m["collected_at"] >= req.requested_at - timedelta(days=14)})
    sus = defaultdict(list)
    for m in req.micro:
        if m["organism"] and m["collected_at"] >= req.requested_at - timedelta(days=14):
            sus[m["organism"]].append(f"{m['antibiotic']} {m['interpretation']}")
    allergies = [f"{a['agent']} ({a['reaction']}, {a['severity']})" for a in p.allergies] or ["none recorded in EHR allergy table"]
    latest = {}
    for lab in sorted(req.labs, key=lambda x: x["collected_at"]):
        latest[lab["test_code"]] = f"{lab['value']} {lab['unit']}"
    must, absent = fact_lines(req)
    pcn_struct = req.drug == "meropenem" and p.pcn_struct() is not None
    lines = [
        f"Patient: {p.age}{p.sex}, {p.weight_kg} kg, CKD stage {p.ckd_stage}, dialysis: {p.dialysis_modality}"
        + (f" ({p.dialysis_days})" if p.dialysis_days else "") + f", clinic: {p.clinic}.",
        f"Exception request: {req.drug} {int(req.dose_mg)} mg {req.route} {req.frequency}; indication on form: \"{req.indication}\"; "
        f"requested {req.requested_at.date().isoformat()}.",
        "Cultures (last 14 days): " + "; ".join(f"{s}: {o} ({d})" for s, o, d in micro) if micro else "Cultures (last 14 days): none sent.",
        "Susceptibilities: " + "; ".join(f"{o}: {', '.join(v)}" for o, v in sus.items()) if sus else "",
        "EHR allergy table: " + "; ".join(allergies)
        + (" (if allergies are mentioned at all, be consistent with this table)" if pcn_struct or req.drug != "meropenem" else ""),
        "Latest labs: " + ", ".join(f"{k} {v}" for k, v in latest.items()),
        "",
        f"Write {req.n_notes} note(s) dated within the 6 days before the request (day 0 = request day) plus the justification.",
        "FACTS THAT MUST BE CONVEYED (somewhere across the notes/justification, unambiguous to a careful reader, in natural "
        "clinical language — vary phrasing, may be spread across notes):",
        *(must or ["- (none)"]),
        "TOPICS THAT MUST BE COMPLETELY ABSENT from every note and the justification (not mentioned, not hinted, not negated):",
        *(absent or ["- (none)"]),
        f"Realism twist to include: {DISTRACTORS[req.distractor]}. The twist must not touch any absent topic and must not "
        "make any required fact ambiguous.",
    ]
    if feedback:
        lines += ["", "A previous draft FAILED an independent audit: " + feedback +
                  " Rewrite from scratch so every required fact is explicit and every absent topic is truly absent."]
    return "\n".join(x for x in lines if x is not None)


def reader_prompt(req: Request, notes: list[dict], justification: str) -> tuple[str, list[str]]:
    vars_ = [v for v in text_vars(TREES[req.drug])]
    defs = "\n".join(f"- {v}: {TEXT_DEFS[v]}" for v in vars_)
    text = "\n\n".join(f"[{n['note_type']} — {n['author_role']}]\n{n['body']}" for n in notes)
    return (f"Variables:\n{defs}\n\n=== CLINICAL NOTES ===\n{text}\n\n=== EXCEPTION REQUEST JUSTIFICATION ===\n{justification}",
            vars_)


def check_reader(req: Request, read: dict, vars_: list[str]) -> list[str]:
    problems = []
    for var in vars_:
        got = read.get(var, {})
        got = got.get("value") if isinstance(got, dict) else got
        if isinstance(got, str) and got.lower() in ("null", "none") and var != "pcn_allergy_severity":
            got = None
        if isinstance(got, str) and var != "pcn_allergy_severity":
            got = {"true": True, "false": False}.get(got.lower(), got)
        if var == "pcn_allergy_severity" and req.patient.pcn_struct() is not None:
            exp = req.patient.pcn_struct()
            if got not in (None, exp):
                problems.append(f"{var}: text says {got!r} but allergy table says {exp!r}")
            continue
        exp = req.values.get(var)
        if got != exp:
            what = "should be completely absent" if exp is None else f"should read as {json.dumps(exp)}"
            problems.append(f"{var} was read as {json.dumps(got)} but {what}")
    return problems


def generate_text(req: Request, kimi: Kimi) -> dict:
    feedback, history = None, []
    for attempt in range(3):  # 1 try + max 2 retries
        try:
            out = kimi.chat_json(WRITER_SYSTEM, writer_prompt(req, feedback))
            notes = [n for n in out["notes"] if n.get("body")][: req.n_notes]
            justification = out["justification"].strip()
            assert notes and justification
            prompt, vars_ = reader_prompt(req, notes, justification)
            read = kimi.chat_json(READER_SYSTEM, prompt)
            problems = check_reader(req, read, vars_)
        except Exception as e:  # malformed output counts as a failed attempt
            problems, notes, justification = [f"writer/reader error: {type(e).__name__}: {str(e)[:120]}"], [], ""
        history.append(problems)
        if not problems:
            req.notes, req.justification = notes, justification
            return {"status": "ok", "attempts": attempt + 1, "history": history}
        feedback = "; ".join(problems)
    return {"status": "dropped", "attempts": 3, "history": history}


# ---------------------------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------------------------
def load_sql_rules() -> dict[str, str]:
    md = (TREES_DIR / "variables.md").read_text()
    return {m.group(1): m.group(2).strip() for m in
            re.finditer(r"<!-- sql:(\w+) -->\s*```sql\n(.*?)```", md, re.S)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-schema", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-llm", action="store_true", help="only use cached Kimi outputs")
    ap.add_argument("--limit", type=int, default=0, help="debug: only generate text for the first N requests")
    ap.add_argument("--plan-only", action="store_true", help="stop after structured load + verification")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    t0 = time.time()

    rng = random.Random(SEED)
    patients = make_patients(rng)

    # --- request plan: balanced targets per drug
    two = set(rng.sample(range(N_PATIENTS), N_TWO_REQUESTS))
    slots = []
    for i, p in enumerate(patients):
        r1 = datetime(2026, 2, 1, tzinfo=UTC) + timedelta(days=rng.randint(0, 120), hours=rng.randint(7, 20), minutes=rng.randint(0, 59))
        slots.append((p, r1, None))
        if i in two:
            slots.append((p, r1 + timedelta(days=rng.randint(35, 100), hours=rng.randint(-5, 5)), r1))
    n = len(slots)
    targets = []
    for k, drug in enumerate(DRUGS):
        cnt = n // 3 + (1 if k < n % 3 else 0)
        n_need = round(cnt * 0.15)
        rest = [OUTCOMES[j % 3] for j in range(cnt - n_need)]
        targets += [(drug, "NEED_INFO")] * n_need + [(drug, o) for o in rest]
    rng.shuffle(targets)

    slots.sort(key=lambda s: (s[1]))
    requests: list[Request] = []
    for idx, ((p, at, prev), (drug, tgt)) in enumerate(zip(slots, targets), start=1):
        req = Request(request_id=f"R{idx:04d}", patient=p, drug=drug, requested_at=at, target=tgt)
        r_rng = random.Random(SEED * 1000 + idx)
        # eGFR is patient-fixed: draw the latest value first, plan against it, then pin the last EGFR lab to it
        lo, hi = EGFR_RANGE[p.ckd_stage]
        egfr_latest = round(min(hi, max(lo, p.egfr_base + r_rng.uniform(-2, 2))), 0)
        plan_scenario(req, egfr_latest, r_rng)
        make_micro(req, r_rng, prev)
        make_labs(req, r_rng)
        max((x for x in req.labs if x["test_code"] == "EGFR"), key=lambda x: x["collected_at"])["value"] = egfr_latest
        make_order(req, egfr_latest, r_rng)
        req.n_notes = r_rng.choices([1, 2, 3], [30, 45, 25])[0]
        req.distractor = r_rng.choice(sorted(DISTRACTORS))
        requests.append(req)

    # --- load structured tables
    rules = load_sql_rules()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.reset_schema:
            conn.execute("DROP TABLE IF EXISTS exception_requests, clinical_notes, microbiology, allergies, labs, patients CASCADE")
        conn.execute((DATA / "schema.sql").read_text())
        conn.execute("TRUNCATE exception_requests, clinical_notes, microbiology, allergies, labs, patients RESTART IDENTITY CASCADE")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO patients VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", [
                (p.patient_id, p.mrn, p.age, p.sex, p.weight_kg, p.ckd_stage, p.dialysis_modality, p.dialysis_days, p.clinic)
                for p in patients])
            cur.executemany("INSERT INTO allergies (patient_id, agent, reaction, severity, recorded_at) VALUES (%s,%s,%s,%s,%s)", [
                (p.patient_id, a["agent"], a["reaction"], a["severity"], a["recorded_at"]) for p in patients for a in p.allergies])
            cur.executemany("INSERT INTO labs (patient_id, test_code, test_name, value, unit, collected_at) VALUES (%s,%s,%s,%s,%s,%s)", [
                (r.patient.patient_id, x["test_code"], x["test_name"], x["value"], x["unit"], x["collected_at"])
                for r in requests for x in sorted(r.labs, key=lambda y: y["collected_at"])])
            cur.executemany("INSERT INTO microbiology (patient_id, specimen, collected_at, organism, antibiotic, interpretation, mic) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)", [
                (r.patient.patient_id, m["specimen"], m["collected_at"], m["organism"], m["antibiotic"], m["interpretation"], m["mic"])
                for r in requests for m in sorted(r.micro, key=lambda y: y["collected_at"])])
        conn.commit()

        # --- verify structured vars via the SQL in trees/variables.md
        mismatches = []
        for r in requests:
            for var, spec in TREES[r.drug]["variables"].items():
                if spec["source"] == "text":
                    continue
                got = conn.execute(rules[var], {"patient_id": r.patient.patient_id, "requested_at": r.requested_at}).fetchone()[0]
                if spec["source"] == "structured_or_text":
                    exp = r.patient.pcn_struct()
                else:
                    exp = r.values.get(var)
                if got != exp:
                    mismatches.append((r.request_id, var, exp, got))
        print(f"structured verification: {len(mismatches)} mismatches")
        for m in mismatches:
            print("  MISMATCH", m)
        if mismatches:
            sys.exit(1)
    if args.plan_only:
        plan = Counter((r.drug, r.expected_outcome) for r in requests)
        print(sorted(plan.items()), "fallbacks:", sum(r.target == "FALLBACK" for r in requests))
        print("paths:", sorted(Counter((r.drug, tuple(r.expected_path), r.expected_outcome) for r in requests).items()))
        print("dose flagged:", sum(not r.dose_ok for r in requests))
        return

    # --- Kimi text generation + validation
    kimi = Kimi(enabled=not args.no_llm)
    if args.limit:
        requests = requests[: args.limit]
    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(generate_text, r, kimi): r for r in requests}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            results[r.request_id] = f.result()
            print(f"[{i}/{len(requests)}] {r.request_id} {r.drug} {r.expected_outcome}: {results[r.request_id]['status']} "
                  f"after {results[r.request_id]['attempts']} attempt(s)", flush=True)
    kept = [r for r in requests if results[r.request_id]["status"] == "ok"]
    dropped = [r for r in requests if results[r.request_id]["status"] != "ok"]

    # --- load text + requests, write gold + CSVs
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            for r in kept:
                for k, n in enumerate(r.notes):
                    d = max(0, min(6, int(n.get("days_before_request", 1))))
                    when = r.requested_at - timedelta(days=d, hours=2 + 3 * k)
                    when = max(when, r.requested_at - timedelta(days=6, hours=20))
                    cur.execute("INSERT INTO clinical_notes (patient_id, note_type, author_role, written_at, body) VALUES (%s,%s,%s,%s,%s)",
                                (r.patient.patient_id, n.get("note_type", "Progress Note"), n.get("author_role", "Physician"),
                                 when, f"{SYNTH_HEADER}\n{n['body'].strip()}"))
                cur.execute("INSERT INTO exception_requests VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (r.request_id, r.patient.patient_id, r.drug, r.dose_mg, r.frequency, r.route, r.indication,
                             r.justification, r.requested_at, r.prescriber))
        conn.commit()
        csv_dir = DATA / "csv"
        csv_dir.mkdir(exist_ok=True)
        counts = {}
        for t in ["patients", "labs", "allergies", "microbiology", "clinical_notes", "exception_requests"]:
            counts[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            with (csv_dir / f"{t}.csv").open("wb") as f, conn.cursor().copy(f"COPY (SELECT * FROM {t} ORDER BY 1) TO STDOUT WITH CSV HEADER") as cp:
                for chunk in cp:
                    f.write(chunk)

    with (DATA / "gold.jsonl").open("w") as f:
        for r in kept:
            f.write(json.dumps({
                "request_id": r.request_id, "tree_id": TREES[r.drug]["tree_id"],
                "variables": {k: r.values.get(k) for k in TREES[r.drug]["variables"]},
                "expected_outcome": r.expected_outcome, "expected_path": r.expected_path,
                "text_vars_present": r.present_text,
                "renal_band": r.renal_band, "expected_dose_ok": r.dose_ok,
            }) + "\n")

    # --- stats + README
    dist = defaultdict(Counter)
    for r in kept:
        dist[r.drug][r.expected_outcome] += 1
    attempts = Counter(results[r.request_id]["attempts"] for r in kept)
    fallbacks = sum(1 for r in requests if r.target == "FALLBACK")
    stats = {
        "seed": SEED, "model": MODEL, "planned_requests": len(requests), "kept": len(kept),
        "dropped": [{"request_id": r.request_id, "drug": r.drug, "expected_outcome": r.expected_outcome,
                     "problems": results[r.request_id]["history"]} for r in dropped],
        "first_pass_ok": attempts.get(1, 0), "ok_after_1_retry": attempts.get(2, 0), "ok_after_2_retries": attempts.get(3, 0),
        "target_fallbacks": fallbacks, "row_counts": counts, "distribution": {d: dict(c) for d, c in dist.items()},
        "dose_flagged": sum(1 for r in kept if not r.dose_ok), "kimi_usage": dict(kimi.usage),
        "retry_reasons": [p for r in requests for h in results[r.request_id]["history"] for p in h],
        "elapsed_s": round(time.time() - t0, 1),
    }
    (DATA / "generation_stats.json").write_text(json.dumps(stats, indent=2, default=str))
    write_readme(stats)
    print(json.dumps({k: v for k, v in stats.items() if k not in ("retry_reasons",)}, indent=2, default=str))


def write_readme(s: dict) -> None:
    rows = []
    for d in DRUGS:
        c = s["distribution"].get(d, {})
        rows.append(f"| {d} | " + " | ".join(str(c.get(o, 0)) for o in OUTCOMES + ["NEED_INFO"]) + f" | {sum(c.values())} |")
    tot = Counter()
    for c in s["distribution"].values():
        tot.update(c)
    rows.append("| **total** | " + " | ".join(str(tot.get(o, 0)) for o in OUTCOMES + ["NEED_INFO"]) + f" | {sum(tot.values())} |")
    u = s["kimi_usage"]
    drops = "\n".join(f"- {d['request_id']} ({d['drug']}, {d['expected_outcome']}): {d['problems'][-1]}" for d in s["dropped"]) or "- none"
    rc = "\n".join(f"| {t} | {n} |" for t, n in s["row_counts"].items())
    (DATA / "README.md").write_text(f"""# Synthetic data

**Everything here is SYNTHETIC** — fake patients (`SYN-FAKE-*` MRNs), fake prescribers, LLM-written notes.
Trees and dose bands in `trees/` are illustrative, not clinical guidance. This file is regenerated by `generate.py`.

## Regenerate
```bash
docker compose up -d                       # postgres:16 on localhost:5433 (db/user/pw kidney_abx)
uv run python data/generate.py             # truncate + reload, writes gold.jsonl, csv/, README.md
uv run python data/generate.py --no-llm    # replay from data/cache/kimi_cache.jsonl only (no API calls)
uv run python data/generate.py --reset-schema   # drop + recreate tables first
```
Code-side randomness is seeded (`SEED={s['seed']}`); Kimi outputs are cached by prompt hash, so a rerun reproduces
the same dataset. Structured variables are verified against the SQL rules embedded in `trees/variables.md`
(the script executes those exact blocks) before any text is generated.

## Gold labels (`gold.jsonl`)
One line per kept request: `request_id, tree_id, variables, expected_outcome, expected_path, text_vars_present`
(contract) plus two extra fields: `renal_band`, `expected_dose_ok`. `expected_path` is the list of visited node
ids (for NEED_INFO it ends at the node whose variable is null). A `null` text variable means it is genuinely
absent from the text; `text_vars_present` lists text-sourced vars conveyed in the text (`pcn_allergy_severity` only
when no structured penicillin row exists).

## Outcome distribution (kept requests)
| tree | APPROVE_ELIGIBLE | DENY_ELIGIBLE | PHARMACIST_REVIEW | NEED_INFO | total |
|---|---|---|---|---|---|
{chr(10).join(rows)}

Requests with renal dose flag (`expected_dose_ok=false`): {s['dose_flagged']}. Target fallbacks (target outcome
infeasible for the patient's fixed facts, another outcome used): {s['target_fallbacks']}.

## Row counts
| table | rows |
|---|---|
{rc}

## Text validation (Kimi `{s['model']}` writer + independent Kimi reader)
Planned {s['planned_requests']}, kept {s['kept']}: {s['first_pass_ok']} passed first time, {s['ok_after_1_retry']} after 1 retry,
{s['ok_after_2_retries']} after 2 retries; dropped {len(s['dropped'])}.

Dropped:
{drops}

## Kimi token usage (original generation; cached replays cost 0)
prompt {u.get('prompt_tokens_orig', 0):,} / completion {u.get('completion_tokens_orig', 0):,} tokens
(this run: {u.get('live_calls', 0)} live calls, {u.get('cached_calls', 0)} cached).
""")


if __name__ == "__main__":
    main()
