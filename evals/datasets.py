"""Build eval datasets as Phoenix-shaped examples, one JSON object per line:
  {"id", "input": {request_id, [perturbation]}, "expected": {tree_id, variables, expected_outcome,
   expected_path, expected_dose_ok, text_vars_present, [acceptable]}, "metadata": {suite, tree_id,
   case_type, tags, [split]}}

Suites -> evals/datasets/<suite>.jsonl:
  golden_dev / golden_test  from data/gold.jsonl, split 60/40 stratified by (tree_id, outcome);
                            assignments are locked in evals/datasets/split.json (test = held out)
  behavioral                evals/behavioral.py CASES (DB rows via evals/fixtures_db.py)
  invariance                ~10 golden_dev cases x evals/perturb.py PERTURBATIONS

  uv run python -m evals.datasets            # build all (golden only if data/gold.jsonl exists)
  uv run python -m evals.datasets --resplit  # discard split.json and re-split (unlocks test!)
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.behavioral import CASES, structured_values
from evals.perturb import PERTURBATIONS
from pipeline import renal
from pipeline.run import TREES, tree_path
from pipeline.tree import load_tree, walk

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "data" / "gold.jsonl"
DIR = Path(__file__).resolve().parent / "datasets"
SPLIT = DIR / "split.json"
DEV_FRACTION = 0.6
N_INVARIANCE = 10
EXPECTED_KEYS = ("tree_id", "variables", "expected_outcome", "expected_path", "expected_dose_ok",
                 "text_vars_present")
SUITES = ("golden_dev", "golden_test", "behavioral", "invariance")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def load(suite: str, directory: Path | None = None) -> list[dict[str, Any]]:
    path = (directory or DIR) / f"{suite}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing - run `uv run python -m evals.datasets`")
    return read_jsonl(path)


# ---------- golden ----------

def assign_splits(golds: list[dict[str, Any]], locked: dict[str, dict[str, str]]
                  ) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Keep locked assignments; place new ids per (tree_id, outcome) stratum in sha256(id) order so
    each stratum's dev share tracks DEV_FRACTION. Returns (assignments, warnings)."""
    out, warnings = dict(locked), []
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for g in golds:
        strata[(g["tree_id"], g["expected_outcome"])].append(g)
        prev = locked.get(g["request_id"])
        if prev and prev["sha"] != _h(json.dumps(g, sort_keys=True))[:12]:
            warnings.append(f"{g['request_id']} gold content changed since it was split ({prev['split']})")
    for members in strata.values():
        dev = sum(out.get(g["request_id"], {}).get("split") == "dev" for g in members)
        total = sum(g["request_id"] in out for g in members)
        for g in sorted(members, key=lambda g: _h(g["request_id"])):
            if g["request_id"] in out:
                continue
            split = "dev" if dev < round(DEV_FRACTION * (total + 1)) else "test"
            dev, total = dev + (split == "dev"), total + 1
            out[g["request_id"]] = {"split": split, "sha": _h(json.dumps(g, sort_keys=True))[:12]}
    return out, warnings


def golden_example(g: dict[str, Any], split: str) -> dict[str, Any]:
    return {"id": g["request_id"], "input": {"request_id": g["request_id"]},
            "expected": {k: g.get(k) for k in EXPECTED_KEYS},
            "metadata": {"suite": f"golden_{split}", "split": split, "tree_id": g["tree_id"],
                         "case_type": g["expected_outcome"],
                         "tags": [t for t in [g.get("renal_band")] if t]}}


def build_golden(gold_path: Path = GOLD, directory: Path = DIR, resplit: bool = False
                 ) -> dict[str, list[dict[str, Any]]]:
    golds = read_jsonl(gold_path)
    split_path = directory / "split.json"
    locked = {} if resplit or not split_path.exists() else json.loads(split_path.read_text())["assignments"]
    assignments, warnings = assign_splits(golds, locked)
    for w in warnings:
        print(f"WARNING: {w}")
    split_path.parent.mkdir(parents=True, exist_ok=True)
    split_path.write_text(json.dumps({
        "rule": f"stratified by (tree_id, expected_outcome), sha256(request_id) order, "
                f"dev fraction {DEV_FRACTION}; existing assignments are never changed",
        "gold_sha256": _h(gold_path.read_text()), "assignments": dict(sorted(assignments.items()))},
        indent=1) + "\n")
    out = {s: [golden_example(g, s) for g in golds if assignments[g["request_id"]]["split"] == s]
           for s in ("dev", "test")}
    for s, rows in out.items():
        write_jsonl(directory / f"golden_{s}.jsonl", rows)
    return out


# ---------- behavioral ----------

def behavioral_example(case: dict[str, Any]) -> dict[str, Any]:
    tree = load_tree(tree_path(case["drug"]))
    specs, sv = tree["variables"], structured_values(case)
    variables, present = {}, []
    for name, spec in specs.items():
        value = sv[name] if spec["source"] != "text" else None
        if value is None and spec["source"] != "structured":
            value = case["text"].get(name)
            if value is not None:
                present.append(name)
        variables[name] = value
    result = walk(tree, variables)
    rc = renal.check(renal.load_renal(TREES / "renal_dosing.json"), case["drug"], case["egfr"],
                     case["dialysis"], case["dose"], case["freq"], case["weight"])
    expected = {"tree_id": tree["tree_id"], "variables": variables,
                "expected_outcome": case["outcome"], "expected_path": [s.node_id for s in result.path],
                "expected_dose_ok": case["dose_ok"] if case["dose_ok"] is not None else rc.status == "OK",
                "text_vars_present": present}
    if case["acceptable"]:
        expected["acceptable"] = case["acceptable"]
    return {"id": f"EVAL-R{case['id']}", "input": {"request_id": f"EVAL-R{case['id']}"},
            "expected": expected,
            "metadata": {"suite": "behavioral", "tree_id": tree["tree_id"],
                         "case_type": case["case_type"], "tags": case["tags"],
                         "walk_outcome": result.outcome, "renal_status": rc.status}}


def build_behavioral(directory: Path = DIR) -> list[dict[str, Any]]:
    rows = [behavioral_example(c) for c in CASES]
    write_jsonl(directory / "behavioral.jsonl", rows)
    return rows


# ---------- invariance ----------

def build_invariance(dev: list[dict[str, Any]], directory: Path = DIR, n: int = N_INVARIANCE
                     ) -> list[dict[str, Any]]:
    """Pick n dev cases round-robin across trees (sha256 order), one variant per perturbation."""
    by_tree: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ex in sorted(dev, key=lambda e: _h(e["id"])):
        by_tree[ex["metadata"]["tree_id"]].append(ex)
    picked: list[dict[str, Any]] = []
    while len(picked) < n and any(by_tree.values()):
        for t in sorted(by_tree):
            if by_tree[t] and len(picked) < n:
                picked.append(by_tree[t].pop(0))
    rows = [{"id": f"{ex['id']}~{kind}",
             "input": {**ex["input"], "perturbation": {"kind": kind}},
             "expected": ex["expected"],
             "metadata": {**ex["metadata"], "suite": "invariance", "case_type": kind,
                          "tags": [*ex["metadata"]["tags"], f"source:{ex['id']}"]}}
            for ex in picked for kind in PERTURBATIONS]
    write_jsonl(directory / "invariance.jsonl", rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--resplit", action="store_true")
    ap.add_argument("--gold", type=Path, default=GOLD)
    ap.add_argument("--out", type=Path, default=DIR)
    a = ap.parse_args(argv)
    print(f"behavioral: {len(build_behavioral(a.out))}")
    if not a.gold.exists():
        print(f"TODO: {a.gold} not found - golden_dev/golden_test/invariance not built")
        return 0
    g = build_golden(a.gold, a.out, a.resplit)
    print(f"golden_dev: {len(g['dev'])}  golden_test: {len(g['test'])}")
    print(f"invariance: {len(build_invariance(g['dev'], a.out))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
