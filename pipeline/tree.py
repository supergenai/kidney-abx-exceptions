"""Decision-tree loading, validation and deterministic traversal."""
from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NEED_INFO = "NEED_INFO"
OPS = {"==", "!=", "<", "<=", ">", ">=", "in"}
VAR_TYPES = {"bool", "enum", "number", "int", "float", "string"}
SOURCES = {"structured", "text", "structured_or_text"}
_CMP = {"==": operator.eq, "!=": operator.ne, "<": operator.lt,
        "<=": operator.le, ">": operator.gt, ">=": operator.ge}

Tree = dict[str, Any]


class TreeError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class Step:
    node_id: str
    var: str
    value: Any
    branch: str | None  # "yes" | "no"; None on the node where NEED_INFO stopped

    def as_tuple(self) -> tuple[str, str, Any, str | None]:
        return (self.node_id, self.var, self.value, self.branch)


@dataclass
class Result:
    outcome: str
    path: list[Step] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "path": [list(s.as_tuple()) for s in self.path],
                "node_path": [s.node_id for s in self.path], "missing": self.missing}


def load_tree(path: str | Path) -> Tree:
    tree = json.loads(Path(path).read_text())
    validate_tree(tree)
    return tree


def validate_tree(tree: Tree) -> None:
    """Raise TreeError listing every structural problem found."""
    errs: list[str] = []
    for key in ("tree_id", "drug", "variables", "root", "nodes", "outcomes"):
        if key not in tree:
            errs.append(f"missing key '{key}'")
    if errs:
        raise TreeError(errs)
    variables, nodes, outcomes = tree["variables"], tree["nodes"], tree["outcomes"]

    for name, spec in variables.items():
        if spec.get("type") not in VAR_TYPES:
            errs.append(f"variable '{name}': bad type {spec.get('type')!r}")
        if spec.get("source") not in SOURCES:
            errs.append(f"variable '{name}': bad source {spec.get('source')!r}")
        if spec.get("type") == "enum" and not spec.get("values"):
            errs.append(f"variable '{name}': enum without values")
    # NEED_INFO may appear in `outcomes` as a description, but no branch may target it
    targets = {n.get(b) for n in nodes.values() for b in ("yes", "no")}
    if NEED_INFO in targets:
        errs.append(f"'{NEED_INFO}' is automatic and cannot be a branch target")
    if tree["root"] not in nodes:
        errs.append(f"root '{tree['root']}' is not a node")

    for nid, node in nodes.items():
        if nid in outcomes:
            errs.append(f"id '{nid}' is both a node and an outcome")
        var = node.get("var")
        if var not in variables:
            errs.append(f"node '{nid}': unknown variable {var!r}")
        op = node.get("op")
        if op not in OPS:
            errs.append(f"node '{nid}': bad op {op!r}")
        if op == "in" and not isinstance(node.get("value"), list):
            errs.append(f"node '{nid}': 'in' needs a list value")
        spec = variables.get(var, {})
        if spec.get("type") == "enum" and "value" in node:
            vals = node["value"] if isinstance(node["value"], list) else [node["value"]]
            bad = [v for v in vals if v not in (spec.get("values") or [])]
            if bad:
                errs.append(f"node '{nid}': values {bad} not allowed for enum '{var}'")
        for br in ("yes", "no"):
            tgt = node.get(br)
            if tgt not in nodes and tgt not in outcomes:
                errs.append(f"node '{nid}': '{br}' -> dangling id {tgt!r}")

    if not errs:  # graph checks only make sense on a well-formed graph
        errs += _graph_errors(tree["root"], nodes)
    if errs:
        raise TreeError(errs)


def _graph_errors(root: str, nodes: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    state: dict[str, int] = {}  # 1 = on stack, 2 = done

    def dfs(nid: str) -> None:
        state[nid] = 1
        for br in ("yes", "no"):
            tgt = nodes[nid][br]
            if tgt not in nodes:
                continue
            if state.get(tgt) == 1:
                errs.append(f"cycle: '{nid}' -> '{tgt}'")
            elif tgt not in state:
                dfs(tgt)
        state[nid] = 2

    dfs(root)
    unreachable = sorted(set(nodes) - set(state))
    if unreachable:
        errs.append(f"unreachable nodes: {unreachable}")
    return errs


def evaluate(op: str, actual: Any, expected: Any) -> bool:
    if op == "in":
        return actual in expected
    return bool(_CMP[op](actual, expected))


def walk(tree: Tree, values: dict[str, Any]) -> Result:
    """Traverse from root. A null (or absent) variable stops with NEED_INFO; the stopping node
    is recorded in the path with value/branch None (same node-id path as data/walker.py)."""
    nodes, path = tree["nodes"], []
    nid = tree["root"]
    while nid in nodes:
        node = nodes[nid]
        var = node["var"]
        val = values.get(var)
        if val is None:
            path.append(Step(nid, var, None, None))
            return Result(NEED_INFO, path, [var])
        branch = "yes" if evaluate(node["op"], val, node["value"]) else "no"
        path.append(Step(nid, var, val, branch))
        nid = node[branch]
    return Result(nid, path, [])
