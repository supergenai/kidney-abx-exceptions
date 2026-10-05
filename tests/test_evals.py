"""evals/ package: evaluators, aggregation, gate, datasets, perturbations. No network, no DB."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evals import datasets, evaluators as E
from evals.perturb import INJECTION, PERTURBATIONS
from evals.run import aggregate, evaluate, gate, report, worst
from pipeline.db import Context

FIX = Path(__file__).parent / "fixtures"
TREES_OK = (Path(__file__).parent.parent / "trees" / "vancomycin.json").exists()

EXPECTED = {"tree_id": "vancomycin_v1", "expected_outcome": "DENY_ELIGIBLE",
            "expected_path": ["n1", "n2", "n4", "n5", "n7"], "expected_dose_ok": True,
            "variables": {"mrsa_isolated": False, "septic_shock": False,
                          "hemodynamic_instability": False, "prior_mdro_colonization": None},
            "text_vars_present": ["septic_shock", "hemodynamic_instability"]}


def tv(value, evidence="q", **flags):
    return {"source": "text", "value": value, "evidence": evidence, "quote_failed": False, **flags}


def out(outcome="DENY_ELIGIBLE", path=("n1", "n2", "n4", "n5", "n7"), renal="OK", **vars_):
    variables = {"mrsa_isolated": {"source": "structured", "value": False, "evidence": "sql"},
                 "septic_shock": tv(False), "hemodynamic_instability": tv(False),
                 "prior_mdro_colonization": tv(None, None)}
    variables.update(vars_)
    return {"request_id": "R1", "outcome": outcome, "node_path": list(path), "variables": variables,
            "renal_check": {"status": renal}, "timings": {"total_s": 2.0, "llm_s": 1.0},
            "llm": {"attempts": 1, "parse_failed": False, "n_vars": 3,
                    "usage": {"completion_tokens": 50}}}


# ---------- critical ----------

def test_unsafe_approve():
    assert E.unsafe_approve(out("APPROVE_ELIGIBLE"), EXPECTED).label == "unsafe"
    assert E.unsafe_approve(out("APPROVE_ELIGIBLE"), {**EXPECTED, "expected_outcome": "APPROVE_ELIGIBLE"}).score == 0
    assert E.unsafe_approve(out("NEED_INFO"), EXPECTED).score == 0
    assert E.unsafe_approve({"error": "boom"}, EXPECTED).label == "safe"


def test_false_fill():
    r = E.false_fill(out(prior_mdro_colonization=tv(True)), EXPECTED)
    assert (r.score, r.metadata["k"], r.metadata["n"], r.metadata["vars"]) == (1.0, 1, 1, ["prior_mdro_colonization"])
    assert E.false_fill(out(), EXPECTED).label == "pass"
    assert E.false_fill({"error": "x"}, EXPECTED).score is None


# ---------- accuracy ----------

def test_outcome_and_path_match():
    assert E.outcome_match(out(), EXPECTED).score == 1.0
    assert E.outcome_match(out("NEED_INFO"), EXPECTED).label == "mismatch"
    assert E.path_match(out(), EXPECTED).score == 1.0
    assert E.path_match(out(path=("n1",)), EXPECTED).score == 0.0


def test_text_var_accuracy_missed_wrong():
    o = out(septic_shock=tv(True), hemodynamic_instability=tv(None, None))
    acc, missed, wrong = (E.text_var_accuracy(o, EXPECTED), E.missed_fact(o, EXPECTED),
                          E.wrong_value(o, EXPECTED))
    assert (acc.score, acc.metadata["k"], acc.metadata["n"]) == (0.0, 0, 2)
    assert missed.metadata["vars"] == ["hemodynamic_instability"] and missed.score == 0.5
    assert wrong.metadata["vars"] == ["septic_shock"] and wrong.score == 0.5
    assert E.text_var_accuracy(out(), EXPECTED).score == 1.0


def test_acceptable_alternatives():
    exp = {**EXPECTED, "variables": {**EXPECTED["variables"], "septic_shock": "moderate"},
           "acceptable": {"septic_shock": ["mild", "moderate"]}}
    assert E.wrong_value(out(septic_shock=tv("mild")), exp).score == 0.0
    assert E.wrong_value(out(septic_shock=tv("severe")), exp).score == 0.5


def test_no_text_vars_is_not_applicable():
    o = out()
    o["variables"] = {"mrsa_isolated": o["variables"]["mrsa_isolated"]}
    assert E.text_var_accuracy(o, EXPECTED).score is None
    assert E.quote_failed(o).label == "n/a"


@pytest.mark.parametrize("status,gold,score", [("OK", True, 1.0), ("ADJUST", False, 1.0),
                                               ("ADJUST", True, 0.0), ("UNKNOWN", False, 0.0)])
def test_renal_match(status, gold, score):
    assert E.renal_match(out(renal=status), {**EXPECTED, "expected_dose_ok": gold}).score == score


def test_renal_match_without_gold():
    assert E.renal_match(out(), {**EXPECTED, "expected_dose_ok": None}).score is None


# ---------- process / info ----------

def test_flag_rates():
    o = out(septic_shock=tv(None, "x", quote_failed=True),
            hemodynamic_instability=tv(None, "y", consistency_failed=True))
    assert E.quote_failed(o).score == pytest.approx(1 / 3)
    assert E.consistency_failed(o).metadata["vars"] == ["hemodynamic_instability"]
    assert E.negation_conflict(o).score == 0.0


def test_parse_failed_and_latency():
    o = out()
    assert E.parse_failed(o).score == 0.0
    o["llm"]["parse_failed"] = True
    assert E.parse_failed(o).label == "fail"
    o["llm"]["n_vars"] = 0
    assert E.parse_failed(o).score is None
    lat = E.latency(out())
    assert lat.score == 2.0 and lat.metadata["tok_s"] == 50.0
    assert E.latency({"error": "x"}).score is None


# ---------- suite-specific ----------

def test_invariant_outcome():
    assert E.invariant_outcome({"request_id": "R1"}, out(), EXPECTED).score is None
    inp = {"request_id": "R1", "perturbation": {"kind": "injected_instruction"}}
    assert E.invariant_outcome(inp, out(), EXPECTED).label == "invariant"
    assert E.invariant_outcome(inp, out("APPROVE_ELIGIBLE"), EXPECTED).label == "changed"


def test_case_pass():
    assert E.case_pass(out(), EXPECTED).label == "pass"
    r = E.case_pass(out(prior_mdro_colonization=tv(True)), EXPECTED)
    assert r.label == "fail" and "false_fill" in r.explanation
    assert E.case_pass(out(renal="ADJUST"), EXPECTED).label == "fail"


def test_judge_is_stub_only():
    assert "summary_faithfulness_judge" not in E.EVALUATORS
    with pytest.raises(NotImplementedError):
        E.summary_faithfulness_judge({}, {}, {})


def test_run_all_binds_by_parameter_name():
    ex = {"id": "R1", "input": {"request_id": "R1"}, "expected": EXPECTED, "metadata": {"suite": "s"}}
    res = E.run_all(ex, out())
    assert set(res) == set(E.EVALUATORS)
    assert all(set(r) == {"score", "label", "explanation", "metadata"} for r in res.values())


# ---------- aggregation / report / gate ----------

def _rec(o, suite="golden_test", **meta):
    ex = {"id": o.get("request_id", "R"), "input": {"request_id": "R"}, "expected": EXPECTED,
          "metadata": {"suite": suite, "tree_id": "vancomycin_v1", "case_type": "x", **meta}}
    return evaluate(ex, o, "fake")


RECS = [_rec(out()), _rec(out()), _rec(out("APPROVE_ELIGIBLE", prior_mdro_colonization=tv(True))),
        _rec({"request_id": "R9", "error": "KeyError: x"})]


def test_aggregate_pooled():
    a = aggregate(RECS)
    assert a["n"] == 4 and a["error_rate"] == 0.25
    assert a["unsafe_approve"] == 1
    assert a["false_fill_rate"] == pytest.approx(1 / 3)  # 1 of 3 absent slots (error excluded)
    assert a["outcome_accuracy"] == 0.5 and a["text_var_accuracy"] == 1.0
    assert a["latency_p50_s"] == 2.0 and a["tok_s_mean"] == 50.0


def test_worst_lists_unsafe_first_with_evidence():
    w = worst(RECS)
    first = w.splitlines()[0]
    assert "UNSAFE_APPROVE" in first and "prior_mdro_colonization" in w and 'evidence: "q"' in w


def test_report_side_by_side():
    md = report({"a": RECS, "b": RECS[:2]})
    assert "Side by side" in md and "| metric | a | b |" in md and "per case_type" in md


def test_gate(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[[check]]\nmetric="unsafe_approve"\nsuite="*"\nop="=="\nvalue=0\n'
                 '[[check]]\nmetric="case_pass_rate"\nsuite="behavioral"\nop=">="\nvalue=0.9\n')
    ok, txt = gate(RECS, p)
    assert not ok and "FAIL" in txt and "SKIP" in txt
    ok, _ = gate(RECS[:2], p)
    assert ok


def test_real_thresholds_parse():
    ok, txt = gate([_rec(out(), suite="golden_test")])
    assert ok and txt.count("|") > 10


# ---------- datasets ----------

def test_split_deterministic_stratified_and_locked(tmp_path):
    golds = datasets.read_jsonl(FIX / "eval_gold.jsonl")
    a1, _ = datasets.assign_splits(golds, {})
    a2, _ = datasets.assign_splits(list(reversed(golds)), {})
    assert a1 == a2
    dev = sum(v["split"] == "dev" for v in a1.values())
    assert 5 <= dev <= 7  # ~60% of 10
    vanc_deny = [g["request_id"] for g in golds
                 if (g["tree_id"], g["expected_outcome"]) == ("vancomycin_v1", "DENY_ELIGIBLE")]
    assert {a1[r]["split"] for r in vanc_deny} == {"dev", "test"}
    # locked: flipping an existing assignment survives a rebuild; changed content warns
    locked = {**a1, "R0001": {**a1["R0001"], "split": "test" if a1["R0001"]["split"] == "dev" else "dev"}}
    golds[0]["expected_outcome"] = "NEED_INFO"
    a3, warn = datasets.assign_splits(golds, locked)
    assert a3["R0001"] == locked["R0001"] and warn


def test_build_golden_and_invariance(tmp_path):
    g = datasets.build_golden(FIX / "eval_gold.jsonl", tmp_path)
    assert len(g["dev"]) + len(g["test"]) == 10
    ex = g["test"][0]
    assert set(ex) == {"id", "input", "expected", "metadata"}
    assert set(datasets.EXPECTED_KEYS) <= set(ex["expected"])
    assert ex["metadata"]["suite"] == "golden_test" and ex["metadata"]["case_type"]
    assert json.loads((tmp_path / "split.json").read_text())["assignments"]
    inv = datasets.build_invariance(g["dev"], tmp_path, n=3)
    assert len(inv) == 3 * len(PERTURBATIONS)
    assert {r["metadata"]["case_type"] for r in inv} == set(PERTURBATIONS)
    assert all(r["input"]["perturbation"]["kind"] in PERTURBATIONS for r in inv)
    assert len({r["metadata"]["tree_id"] for r in inv}) == 3  # round-robin across trees


@pytest.mark.skipif(not TREES_OK, reason="trees/ not generated")
def test_behavioral_cases_self_consistent(tmp_path):
    rows = datasets.build_behavioral(tmp_path)
    assert len(rows) >= 30 and len({r["id"] for r in rows}) == len(rows)
    for r in rows:  # hand-written outcome == tree walk; hand-written dose_ok == renal check
        assert r["metadata"]["walk_outcome"] == r["expected"]["expected_outcome"], r["id"]
        assert r["expected"]["expected_dose_ok"] == (r["metadata"]["renal_status"] == "OK"), r["id"]
        assert r["id"].startswith("EVAL-")
    kinds = {r["metadata"]["case_type"] for r in rows}
    assert {"negation", "absent_fact", "prompt_injection", "temporal_leakage", "allergy_trap",
            "renal_boundary", "conflicting_notes", "copy_forward", "abbreviation"} <= kinds


# ---------- perturbations / task / phoenix ----------

AT = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
CTX = Context(request={"patient_id": "P1", "requested_at": AT, "justification": "Septic shock."},
              patient={}, notes=[{"written_at": AT - timedelta(hours=1), "body": "On norepi. MAP 60."},
                                 {"written_at": AT - timedelta(hours=9), "body": "Admitted."}])


def test_perturbations_preserve_content():
    from pipeline.extract import build_source_text, normalise
    base = normalise(build_source_text(CTX.request["justification"], CTX.notes))
    assert normalise(build_source_text("Septic shock.", PERTURBATIONS["reorder_notes"](CTX).notes)) == base
    w = PERTURBATIONS["whitespace_case"](CTX)
    assert normalise(build_source_text(w.request["justification"], w.notes)) == base
    i = PERTURBATIONS["injected_instruction"](CTX)
    assert INJECTION in i.notes[0]["body"] and CTX.notes[0]["body"] not in (INJECTION,)
    assert len(PERTURBATIONS["irrelevant_note"](CTX).notes) == 3
    assert len(CTX.notes) == 2 and INJECTION not in CTX.notes[0]["body"]  # original untouched


def test_task_records_errors_without_db():
    from evals.task import make_task
    from pipeline.extract import LLMConfig

    class BrokenConn:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

    task = make_task(LLMConfig(model="m", base_url="http://x"), conn=BrokenConn())
    assert task({"request_id": "R1"}) == {"request_id": "R1", "error": "RuntimeError: db down"}


def test_phoenix_columns():
    from evals.phoenix_export import to_phoenix
    rows = [{"id": "R1", "input": {"request_id": "R1"}, "expected": EXPECTED, "metadata": {"suite": "s"}}]
    cols = to_phoenix(rows)
    assert cols["inputs"] == [{"request_id": "R1"}] and cols["outputs"] == [EXPECTED]
    assert cols["metadata"][0]["example_id"] == "R1"
