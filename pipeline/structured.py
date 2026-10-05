"""Structured variable derivation.

The rules are the `<!-- sql:<var> -->` SQL blocks in trees/variables.md, which that file declares
executable and authoritative (the gold generator runs the same blocks). We execute them verbatim with
params patient_id / requested_at, so pipeline and gold cannot drift. Every block is bounded by
`<= requested_at` (no future data).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pipeline.db import Context

VARIABLES_MD = Path(__file__).resolve().parent.parent / "trees" / "variables.md"
_BLOCK = re.compile(r"<!--\s*sql:(\w+)\s*-->\s*```sql\s*\n(.*?)```", re.S)


@lru_cache(maxsize=4)
def load_rules(path: Path = VARIABLES_MD) -> dict[str, str]:
    """var name -> SQL text."""
    return {m.group(1): m.group(2).strip() for m in _BLOCK.finditer(path.read_text())}


def _normalise(value: Any, spec: dict[str, Any]) -> Any:
    if value is None:
        return None
    if spec["type"] in {"number", "float"}:
        return float(value)
    if spec["type"] == "int":
        return int(value)
    return value


def derive(specs: dict[str, dict[str, Any]], ctx: Context, conn: Any,
           rules: dict[str, str] | None = None) -> dict[str, tuple[Any, str | None]]:
    """Return {var: (value, evidence)} for every structured / structured_or_text variable.
    Raises KeyError if a structured variable has no SQL rule (contract violation)."""
    rules = rules if rules is not None else load_rules()
    params = {"patient_id": ctx.request["patient_id"], "requested_at": ctx.request["requested_at"]}
    out: dict[str, tuple[Any, str | None]] = {}
    for name, spec in specs.items():
        if spec["source"] not in ("structured", "structured_or_text"):
            continue
        if name not in rules:
            raise KeyError(f"no sql rule for structured variable '{name}' in variables.md")
        row = conn.execute(rules[name], params).fetchone()
        value = _normalise(next(iter(row.values())) if row else None, spec)
        out[name] = (value, f"sql:{name} -> {value!r}")
    return out


def egfr_latest(ctx: Context, conn: Any) -> float | None:
    """Same rule as the egfr_latest tree variable; used for renal band selection."""
    spec = {"egfr_latest": {"type": "number", "source": "structured"}}
    return derive(spec, ctx, conn)["egfr_latest"][0]
