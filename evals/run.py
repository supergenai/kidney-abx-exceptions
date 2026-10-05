"""Local eval runner: task + evaluators over datasets, report, compare, gate.

  uv run python -m evals.run --suite <golden_dev|golden_test|golden|behavioral|invariance|all>
         [--provider mlx|moonshot] [--model M] [--base-url U] [--limit N] [--gate]
  uv run python -m evals.run --compare eval/runs/a.jsonl eval/runs/b.jsonl [--gate]
  uv run python -m evals.run --score eval/runs/<slug>.jsonl   # re-score saved outputs (also plain
                                                               # `pipeline.run --all` files, joined to gold)

Writes eval/runs/<suite>__<model-slug>.jsonl (one record per example: input, expected, metadata,
output, evals) and eval/report.md.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from evals import datasets
from evals.evaluators import run_all

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "eval" / "runs"
REPORT = ROOT / "eval" / "report.md"
THRESHOLDS = Path(__file__).resolve().parent / "thresholds.toml"

# metric -> (evaluator, how): "pool" = sum k / sum n, "count" = sum k, "mean" = mean score
METRICS: dict[str, tuple[str, str]] = {
    "unsafe_approve": ("unsafe_approve", "count"),
    "false_fill_rate": ("false_fill", "pool"),
    "outcome_accuracy": ("outcome_match", "mean"),
    "path_accuracy": ("path_match", "mean"),
    "text_var_accuracy": ("text_var_accuracy", "pool"),
    "missed_rate": ("missed_fact", "pool"),
    "wrong_value_rate": ("wrong_value", "pool"),
    "renal_agreement": ("renal_match", "mean"),
    "quote_failed_rate": ("quote_failed", "pool"),
    "consistency_failed_rate": ("consistency_failed", "pool"),
    "negation_conflict_rate": ("negation_conflict", "pool"),
    "parse_failed_rate": ("parse_failed", "pool"),
    "case_pass_rate": ("case_pass", "mean"),
    "invariance_pass_rate": ("invariant_outcome", "mean"),
}
SHORT = ["n", "outcome_accuracy", "unsafe_approve", "false_fill_rate", "text_var_accuracy",
         "case_pass_rate", "invariance_pass_rate"]


# ---------- aggregation ----------

def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    agg: dict[str, Any] = {"n": len(records),
                           "error_rate": sum("error" in (r.get("output") or {}) for r in records)
                           / len(records) if records else None}
    for metric, (ev, how) in METRICS.items():
        res = [r["evals"][ev] for r in records if r["evals"][ev]["score"] is not None]
        if not res:
            agg[metric] = None
        elif how == "count":
            agg[metric] = sum(x["metadata"]["k"] for x in res)
        elif how == "pool":
            n = sum(x["metadata"]["n"] for x in res)
            agg[metric] = sum(x["metadata"]["k"] for x in res) / n if n else None
        else:
            agg[metric] = statistics.fmean(x["score"] for x in res)
    lat = [r["evals"]["latency"]["score"] for r in records if r["evals"]["latency"]["score"] is not None]
    tps = [r["evals"]["latency"]["metadata"].get("tok_s") for r in records
           if r["evals"]["latency"]["score"] is not None]
    tps = [x for x in tps if x]
    agg |= {"latency_p50_s": _pct(lat, 0.5), "latency_p95_s": _pct(lat, 0.95),
            "tok_s_mean": statistics.fmean(tps) if tps else None}
    return agg


def group(records: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        out[str(r["metadata"].get(key))].append(r)
    return dict(sorted(out.items()))


# ---------- rendering ----------

def fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}" if v < 10 else f"{v:.1f}"
    return str(v)


def table(rows: dict[str, dict[str, Any]], cols: list[str], first: str = "") -> str:
    head = f"| {first} | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 1) + "\n"
    return head + "".join(f"| {k} | " + " | ".join(fmt(a.get(c)) for c in cols) + " |\n"
                          for k, a in rows.items())


def transpose(rows: dict[str, dict[str, Any]], metrics: list[str], first: str = "metric") -> str:
    cols = list(rows)
    head = f"| {first} | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 1) + "\n"
    return head + "".join(f"| {m} | " + " | ".join(fmt(rows[c].get(m)) for c in cols) + " |\n"
                          for m in metrics)


def badness(r: dict[str, Any]) -> tuple:
    e = r["evals"]
    k = lambda name: (e[name]["metadata"] or {}).get("k", 0) if e[name]["score"] is not None else 0
    return (k("unsafe_approve"), k("false_fill"), k("wrong_value"), e["outcome_match"]["score"] == 0,
            k("missed_fact"), "error" in (r.get("output") or {}))


def worst(records: list[dict[str, Any]], n: int = 10) -> str:
    bad = sorted((r for r in records if any(badness(r))), key=badness, reverse=True)[:n]
    if not bad:
        return "_no failing cases_\n"
    lines = []
    for r in bad:
        out, exp = r.get("output") or {}, r["expected"]
        tags = [t for t, hit in zip(("UNSAFE_APPROVE", "false_fill", "wrong_value", "outcome",
                                     "missed", "error"), badness(r)) if hit]
        lines.append(f"- **{r['id']}** [{r['metadata'].get('suite')}/{r['metadata'].get('case_type')}] "
                     f"{', '.join(tags)}: pred `{out.get('outcome', out.get('error'))}` "
                     f"gold `{exp['expected_outcome']}`")
        flagged = set()
        for ev in ("false_fill", "wrong_value", "missed_fact"):
            flagged |= set((r["evals"][ev]["metadata"] or {}).get("vars", []))
        for var in sorted(flagged):
            v = (out.get("variables") or {}).get(var, {})
            gold = exp["variables"].get(var) if var in exp.get("text_vars_present", []) else None
            ev_txt = (v.get("evidence") or "").replace("\n", " ")[:160]
            lines.append(f"  - `{var}` gold=`{gold}` pred=`{v.get('value')}` evidence: \"{ev_txt}\"")
    return "\n".join(lines) + "\n"


def report(runs: dict[str, list[dict[str, Any]]]) -> str:
    """runs: label -> records. Side-by-side when several labels."""
    md = ["# Eval report\n", "_Synthetic data; trees are illustrative._\n"]
    metrics = ["n", "error_rate", *METRICS, "latency_p50_s", "latency_p95_s", "tok_s_mean"]
    suites = sorted({r["metadata"]["suite"] for recs in runs.values() for r in recs})
    if len(runs) > 1:
        md.append("## Side by side (all suites pooled)\n")
        md.append(transpose({lbl: aggregate(recs) for lbl, recs in runs.items()}, metrics))
        for s in suites:
            md.append(f"\n### {s}\n")
            md.append(transpose({lbl: aggregate([r for r in recs if r["metadata"]["suite"] == s])
                                 for lbl, recs in runs.items()}, metrics))
    for lbl, recs in runs.items():
        md.append(f"\n## {lbl}\n\n### Per suite\n")
        md.append(transpose({s: aggregate(rs) for s, rs in group(recs, "suite").items()}, metrics, "metric"))
        for s, rs in group(recs, "suite").items():
            md.append(f"\n### {s} - per tree\n")
            md.append(table({t: aggregate(x) for t, x in group(rs, "tree_id").items()}, SHORT, "tree"))
            md.append(f"\n### {s} - per case_type\n")
            md.append(table({t: aggregate(x) for t, x in group(rs, "case_type").items()}, SHORT, "case_type"))
        md.append("\n### 10 worst cases\n")
        md.append(worst(recs))
    return "\n".join(md)


# ---------- gate ----------

def gate(records: list[dict[str, Any]], path: Path = THRESHOLDS) -> tuple[bool, str]:
    checks = tomllib.loads(path.read_text())["check"]
    ops = {"==": lambda a, b: a == b, "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
           "<": lambda a, b: a < b, ">": lambda a, b: a > b}
    ok, lines = True, ["| check | value | status |", "|---|---|---|"]
    for c in checks:
        rs = records if c["suite"] == "*" else [r for r in records if r["metadata"]["suite"] == c["suite"]]
        v = aggregate(rs).get(c["metric"]) if rs else None
        status = "SKIP" if v is None else "PASS" if ops[c["op"]](v, c["value"]) else "FAIL"
        ok &= status != "FAIL"
        lines.append(f"| {c['metric']} [{c['suite']}] {c['op']} {c['value']} | {fmt(v)} | {status} |")
    return ok, "\n".join(lines)


# ---------- running ----------

def evaluate(example: dict[str, Any], output: dict[str, Any], model: str) -> dict[str, Any]:
    return {"id": example["id"], "suite": example["metadata"]["suite"], "model": model,
            "input": example["input"], "expected": example["expected"],
            "metadata": example["metadata"], "output": output, "evals": run_all(example, output)}


def run_suite(suite: str, task: Any, model: str, limit: int | None = None) -> list[dict[str, Any]]:
    examples = datasets.load(suite)[:limit]
    recs = []
    for i, ex in enumerate(examples, 1):
        t = time.perf_counter()
        rec = evaluate(ex, task(ex["input"]), model)
        e = rec["evals"]
        mark = ("UNSAFE" if e["unsafe_approve"]["score"] else "ok" if e["outcome_match"]["score"]
                else "MISS")
        print(f"[{suite} {i}/{len(examples)}] {ex['id']} {rec['output'].get('outcome', 'ERROR')} "
              f"(gold {ex['expected']['expected_outcome']}) {mark} {time.perf_counter() - t:.1f}s",
              file=sys.stderr)
        recs.append(rec)
    return recs


def rescore(path: Path) -> list[dict[str, Any]]:
    """Re-run evaluators on saved outputs. Records without `expected` (plain pipeline.run --all output)
    are joined to data/gold.jsonl by request_id (suite golden_dev/test per split.json, else 'golden')."""
    rows = datasets.read_jsonl(path)
    golds, splits = {}, {}
    if any("expected" not in r for r in rows):
        golds = {g["request_id"]: g for g in datasets.read_jsonl(datasets.GOLD)}
        if datasets.SPLIT.exists():
            splits = json.loads(datasets.SPLIT.read_text())["assignments"]
    out = []
    for r in rows:
        if "expected" in r:
            ex, output = r, r["output"]
        else:
            g = golds.get(r["request_id"])
            if g is None:
                print(f"skip {r['request_id']}: not in gold", file=sys.stderr)
                continue
            ex = datasets.golden_example(g, splits.get(r["request_id"], {}).get("split", "all"))
            output = r
        out.append(evaluate(ex, output, r.get("model", "?")))
    return out


def _guarded(agent: Any) -> Any:
    """Wrap a user agent so one failing example is scored as an error, not a crash."""
    def task(input: dict[str, Any]) -> dict[str, Any]:
        try:
            return agent(input)
        except Exception as e:  # noqa: BLE001 - recorded, scored as error
            return {"request_id": input.get("request_id"), "error": f"{type(e).__name__}: {e}"}
    return task


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--suite", choices=[*datasets.SUITES, "golden", "all"])
    g.add_argument("--compare", nargs="+", type=Path)
    g.add_argument("--score", nargs="+", type=Path)
    ap.add_argument("--provider", choices=["mlx", "moonshot"], default="mlx")
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    ap.add_argument("--agent", metavar="MODULE:FUNC",
                    help="score your own application instead of pipeline.run: a callable "
                         "`agent(input: dict) -> dict` returning the output contract in docs/TESTING_YOUR_AGENT.md")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--report", type=Path, default=REPORT)
    a = ap.parse_args(argv)

    if a.suite:
        from evals.task import make_task
        from pipeline import db
        from pipeline.extract import LLMConfig
        if a.agent:
            import importlib
            mod, _, fn = a.agent.partition(":")
            agent_fn = getattr(importlib.import_module(mod), fn)
            label = a.agent.replace(":", "_").replace(".", "_")
            cfg = type("AgentCfg", (), {"slug": label, "model": label})()
        else:
            cfg = LLMConfig.from_env(a.provider, a.model, a.base_url)
        suites = {"all": datasets.SUITES, "golden": ("golden_dev", "golden_test")}.get(a.suite, (a.suite,))
        missing = [s for s in suites if not (datasets.DIR / f"{s}.jsonl").exists()]
        if missing:
            print(f"TODO: datasets not built for {missing} - run `uv run python -m evals.datasets` "
                  "(golden/invariance need data/gold.jsonl); skipping them", file=sys.stderr)
        suites = [s for s in suites if s not in missing]
        if not suites:
            return 2
        runs: dict[str, list[dict[str, Any]]] = {cfg.slug: []}
        with db.connect() as conn:
            if "behavioral" in suites:
                from evals.fixtures_db import install
                install(conn)
            task = _guarded(agent_fn) if a.agent else make_task(cfg, conn)
            for s in suites:
                recs = run_suite(s, task, cfg.model, a.limit)
                out = RUNS / f"{s}__{cfg.slug}.jsonl"
                datasets.write_jsonl(out, recs)
                print(out)
                runs[cfg.slug] += recs
    else:
        runs = {p.stem: (rescore(p) if a.score else datasets.read_jsonl(p)) for p in (a.compare or a.score)}
        if a.score:
            for p, recs in zip(a.score, runs.values()):
                datasets.write_jsonl(p.with_name(p.stem + ".scored.jsonl"), recs)

    md = report(runs)
    rc = 0
    if a.gate:
        for lbl, recs in runs.items():
            ok, gt = gate(recs)
            md += f"\n## Gate: {lbl} - {'PASS' if ok else 'FAIL'}\n\n{gt}\n"
            rc |= 0 if ok else 1
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(md)
    side = transpose({lbl: aggregate(r) for lbl, r in runs.items()},
                     ["n", "error_rate", *METRICS, "latency_p50_s", "latency_p95_s", "tok_s_mean"])
    print(side)
    if a.gate:
        print(md[md.index("\n## Gate"):])
    print(f"report: {a.report}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
