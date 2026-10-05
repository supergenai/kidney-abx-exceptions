"""Pure evaluator functions, Phoenix-compatible.

Parameter names follow Phoenix's experiment binding (`input`, `output`, `expected`, `metadata`), so
each function can go straight into `phoenix.client.experiments.run_experiment(evaluators=[...])`.
No phoenix import: `EvalResult` mirrors Phoenix's EvaluationResult (score, label, explanation,
metadata).

Conventions
- `output` is a `pipeline.run.process` record (or {"error": ...} when the task raised).
- `expected` is a dataset example's expected dict (tree_id, variables, expected_outcome,
  expected_path, expected_dose_ok, text_vars_present; optional `acceptable`: {var: [values]}).
- `score` is the per-example metric in its natural direction (see DIRECTION); None = not applicable.
- Rate metrics put pooled counts in metadata {"k": hits, "n": denominator} so aggregates are pooled
  over variables rather than averaged over examples.
- "Text variables" are the ones the pipeline filled from text (output source == "text").
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

APPROVE = "APPROVE_ELIGIBLE"


@dataclass
class EvalResult:
    score: float | None
    label: str
    explanation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _na(why: str) -> EvalResult:
    return EvalResult(None, "n/a", why)


def _err(output: dict[str, Any]) -> str | None:
    return output.get("error") if isinstance(output, dict) else "no output"


def _text_vars(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n: v for n, v in (output.get("variables") or {}).items() if v.get("source") == "text"}


def _gold(name: str, expected: dict[str, Any]) -> Any:
    """Gold value of a text var: None when the text does not convey it."""
    if name not in expected.get("text_vars_present", []):
        return None
    return expected["variables"].get(name)


def _ok(name: str, pred: Any, expected: dict[str, Any]) -> bool:
    return pred in expected.get("acceptable", {}).get(name, [_gold(name, expected)])


def _rate(name: str, hits: list[str], n: int, what: str) -> EvalResult:
    if n == 0:
        return _na(f"no {what}")
    return EvalResult(len(hits) / n, "fail" if hits else "pass",
                      f"{len(hits)}/{n} {what}" + (f": {', '.join(hits)}" if hits else ""),
                      {"k": len(hits), "n": n, "vars": hits})


# ---------- critical ----------

def unsafe_approve(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    """1.0 when the pipeline says APPROVE_ELIGIBLE and gold says anything else."""
    pred, gold = (output or {}).get("outcome"), expected["expected_outcome"]
    bad = pred == APPROVE and gold != APPROVE
    return EvalResult(float(bad), "unsafe" if bad else "safe", f"pred={pred} gold={gold}",
                      {"k": int(bad), "n": 1})


def false_fill(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    """Rate of text vars the text does NOT convey that came back non-null (hallucinated facts)."""
    if e := _err(output):
        return _na(f"task error: {e}")
    tv = _text_vars(output)
    absent = [n for n in tv if _gold(n, expected) is None]
    return _rate("false_fill", [n for n in absent if tv[n]["value"] is not None], len(absent),
                 "absent text vars filled")


# ---------- accuracy ----------

def outcome_match(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    pred, gold = (output or {}).get("outcome"), expected["expected_outcome"]
    return EvalResult(float(pred == gold), "match" if pred == gold else "mismatch",
                      f"pred={pred} gold={gold}")


def path_match(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    pred = (output or {}).get("node_path")
    gold = expected["expected_path"]
    return EvalResult(float(pred == gold), "match" if pred == gold else "mismatch",
                      f"pred={pred} gold={gold}")


def text_var_accuracy(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    """Share of text vars the text DOES convey that were extracted with the gold value."""
    if e := _err(output):
        return _na(f"task error: {e}")
    tv = _text_vars(output)
    present = [n for n in tv if _gold(n, expected) is not None]
    wrong = [n for n in present if not _ok(n, tv[n]["value"], expected)]
    r = _rate("text_var_accuracy", wrong, len(present), "present text vars wrong or missing")
    if r.score is not None:
        r.score, r.metadata["k"] = 1 - r.score, len(present) - len(wrong)
    return r


def missed_fact(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    """Rate of conveyed text vars returned null."""
    if e := _err(output):
        return _na(f"task error: {e}")
    tv = _text_vars(output)
    present = [n for n in tv if _gold(n, expected) is not None]
    return _rate("missed_fact", [n for n in present if tv[n]["value"] is None], len(present),
                 "present text vars missed")


def wrong_value(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    """Rate of conveyed text vars returned non-null but different from gold."""
    if e := _err(output):
        return _na(f"task error: {e}")
    tv = _text_vars(output)
    present = [n for n in tv if _gold(n, expected) is not None]
    hits = [n for n in present
            if tv[n]["value"] is not None and not _ok(n, tv[n]["value"], expected)]
    return _rate("wrong_value", hits, len(present), "present text vars with wrong value")


def renal_match(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    """Renal check OK/not-OK agrees with gold expected_dose_ok (UNKNOWN counts as disagreement)."""
    gold = expected.get("expected_dose_ok")
    if gold is None:
        return _na("no expected_dose_ok")
    status = ((output or {}).get("renal_check") or {}).get("status")
    ok = (status == "OK") == gold and status in ("OK", "ADJUST")
    return EvalResult(float(ok), "match" if ok else "mismatch",
                      f"status={status} gold_dose_ok={gold}")


# ---------- process ----------

def _flag_rate(flag: str, output: dict[str, Any]) -> EvalResult:
    if e := _err(output):
        return _na(f"task error: {e}")
    tv = _text_vars(output)
    return _rate(flag, [n for n, v in tv.items() if v.get(flag)], len(tv), f"text vars {flag}")


def quote_failed(output: dict[str, Any]) -> EvalResult:
    return _flag_rate("quote_failed", output)


def consistency_failed(output: dict[str, Any]) -> EvalResult:
    return _flag_rate("consistency_failed", output)


def negation_conflict(output: dict[str, Any]) -> EvalResult:
    return _flag_rate("negation_conflict", output)


def parse_failed(output: dict[str, Any]) -> EvalResult:
    if e := _err(output):
        return _na(f"task error: {e}")
    llm = output.get("llm") or {}
    if not llm.get("n_vars"):
        return _na("no LLM call")
    bad = bool(llm.get("parse_failed"))
    return EvalResult(float(bad), "fail" if bad else "pass", f"attempts={llm.get('attempts')}",
                      {"k": int(bad), "n": 1})


# ---------- info ----------

def latency(output: dict[str, Any]) -> EvalResult:
    """score = total seconds per request; metadata carries llm_s and completion tok/s."""
    t = (output or {}).get("timings") or {}
    if "total_s" not in t:
        return _na("no timings")
    toks = ((output.get("llm") or {}).get("usage") or {}).get("completion_tokens")
    tps = toks / t["llm_s"] if toks and t.get("llm_s") else None
    return EvalResult(float(t["total_s"]), "info",
                      f"total={t['total_s']}s llm={t.get('llm_s')}s tok/s={tps and round(tps, 1)}",
                      {"llm_s": t.get("llm_s"), "tok_s": tps})


# ---------- suite-specific ----------

def invariant_outcome(input: dict[str, Any], output: dict[str, Any],
                      expected: dict[str, Any]) -> EvalResult:
    """Metamorphic: a perturbed variant must keep the original's expected outcome."""
    if not (input or {}).get("perturbation"):
        return _na("not a metamorphic variant")
    r = outcome_match(output, expected)
    return EvalResult(r.score, "invariant" if r.score else "changed",
                      f"{input['perturbation']['kind']}: {r.explanation}")


def case_pass(output: dict[str, Any], expected: dict[str, Any]) -> EvalResult:
    """Behavioral pass = right outcome, no unsafe approve, no false fill, every conveyed text var
    right, and renal agreement when gold has it."""
    checks = {"outcome": outcome_match(output, expected), "unsafe": unsafe_approve(output, expected),
              "false_fill": false_fill(output, expected),
              "text_vars": text_var_accuracy(output, expected),
              "renal": renal_match(output, expected)}
    good = {"outcome": 1.0, "unsafe": 0.0, "false_fill": 0.0, "text_vars": 1.0, "renal": 1.0}
    failed = [k for k, r in checks.items() if r.score is not None and r.score != good[k]]
    return EvalResult(float(not failed), "fail" if failed else "pass",
                      "; ".join(f"{k}: {checks[k].explanation}" for k in failed) or "all checks pass")


# ---------- future: LLM-as-judge (stub only) ----------

def summary_faithfulness_judge(input: dict[str, Any], output: dict[str, Any],
                               metadata: dict[str, Any]) -> EvalResult:
    """STUB for a future summary agent: an LLM judge scores whether `output["summary"]` is
    faithful to the cited evidence and the tree path (score 0-1, label faithful|unfaithful,
    explanation = judge rationale). Must use a BAA-covered model for real data, temperature 0, a
    fixed rubric prompt, and be calibrated against pharmacist labels before it may gate releases.
    Not implemented; not registered in EVALUATORS."""
    raise NotImplementedError


# name -> (fn, tier, direction). direction: "lower" | "higher" is better, "info" = no direction.
EVALUATORS: dict[str, tuple[Callable[..., EvalResult], str, str]] = {
    "unsafe_approve": (unsafe_approve, "critical", "lower"),
    "false_fill": (false_fill, "critical", "lower"),
    "outcome_match": (outcome_match, "accuracy", "higher"),
    "path_match": (path_match, "accuracy", "higher"),
    "text_var_accuracy": (text_var_accuracy, "accuracy", "higher"),
    "missed_fact": (missed_fact, "accuracy", "lower"),
    "wrong_value": (wrong_value, "accuracy", "lower"),
    "renal_match": (renal_match, "accuracy", "higher"),
    "quote_failed": (quote_failed, "process", "lower"),
    "consistency_failed": (consistency_failed, "process", "lower"),
    "negation_conflict": (negation_conflict, "process", "lower"),
    "parse_failed": (parse_failed, "process", "lower"),
    "latency": (latency, "info", "info"),
    "invariant_outcome": (invariant_outcome, "suite", "higher"),
    "case_pass": (case_pass, "suite", "higher"),
}


def run_all(example: dict[str, Any], output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Apply every evaluator, binding arguments by parameter name like Phoenix does."""
    import inspect
    bind = {"input": example.get("input", {}), "output": output,
            "expected": example.get("expected", {}), "metadata": example.get("metadata", {})}
    out = {}
    for name, (fn, _, _) in EVALUATORS.items():
        params = inspect.signature(fn).parameters
        out[name] = fn(**{p: bind[p] for p in params}).to_dict()
    return out
