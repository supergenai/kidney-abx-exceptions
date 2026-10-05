"""End-to-end processing of one exception request.

CLI:
  uv run python -m pipeline.run --request-id R001 [--provider mlx|moonshot] [--model M] [--base-url U]
  uv run python -m pipeline.run --all [...]   # writes eval/runs/<model-slug>.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from pipeline import db, extract, renal, structured
from pipeline.tree import load_tree, walk

ROOT = Path(__file__).resolve().parent.parent
TREES = ROOT / "trees"


def tree_path(drug: str) -> Path:
    return TREES / f"{drug.strip().lower()}.json"


def process(request_id: str, llm_config: extract.LLMConfig, conn: Any = None,
            client: Any = None, ctx_hook: Callable[[db.Context], db.Context] | None = None
            ) -> dict[str, Any]:
    """`ctx_hook` rewrites the loaded context (notes/justification) before anything reads it;
    used by evals/ metamorphic variants. Structured SQL still reads the DB."""
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    own = conn is None
    conn = conn or db.connect()
    try:
        ctx = db.load_context(conn, request_id)
        if ctx_hook is not None:
            ctx = ctx_hook(ctx)
        req = ctx.request
        tree = load_tree(tree_path(req["drug"]))
        specs = tree["variables"]
        timings["load_s"] = time.perf_counter() - t0
        t = time.perf_counter()
        derived = structured.derive(specs, ctx, conn)
        egfr = structured.egfr_latest(ctx, conn)
        timings["structured_s"] = time.perf_counter() - t
    finally:
        if own:
            conn.close()

    variables: dict[str, dict[str, Any]] = {}
    for name, (value, evidence) in derived.items():
        # structured_or_text with a NULL structured value falls through to text extraction
        if value is not None or specs[name]["source"] == "structured":
            variables[name] = {"value": value, "source": "structured", "evidence": evidence,
                               "quote_failed": False}

    to_extract = {n: s for n, s in specs.items() if s["source"] != "structured" and n not in variables}
    source_text = extract.build_source_text(req["justification"], ctx.notes)
    ex = extract.extract(to_extract, source_text, llm_config, client=client)
    for name, item in ex.variables.items():
        variables[name] = {"source": "text", **item}
    timings["llm_s"] = ex.latency_s

    result = walk(tree, {n: v["value"] for n, v in variables.items()})
    rc = renal.check(renal.load_renal(TREES / "renal_dosing.json"), req["drug"], egfr,
                     ctx.patient["dialysis_modality"], float(req["dose_mg"]), req["frequency"],
                     float(ctx.patient["weight_kg"]))
    timings["total_s"] = time.perf_counter() - t0
    return {
        "request_id": request_id, "tree_id": tree["tree_id"], "model": llm_config.model,
        "variables": {n: variables[n] for n in specs},
        **result.to_dict(), "egfr_latest": egfr,
        "renal_check": rc.to_dict(),
        "llm": {"attempts": ex.attempts, "parse_failed": ex.parse_failed, "usage": ex.usage,
                "n_vars": len(to_extract), "source_chars": len(source_text),
                "prompt_version": llm_config.prompt_version, "mode": llm_config.mode,
                "raw": ex.raw[:4000]},
        "timings": {k: round(v, 3) for k, v in timings.items()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--request-id")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--provider", choices=["mlx", "moonshot"], default="mlx")
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    a = ap.parse_args(argv)
    cfg = extract.LLMConfig.from_env(a.provider, a.model, a.base_url)

    with db.connect() as conn:
        if a.request_id:
            print(json.dumps(process(a.request_id, cfg, conn), indent=2, default=str))
            return 0
        out = ROOT / "eval" / "runs" / f"{cfg.slug}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        ids = db.request_ids(conn)
        with out.open("w") as f:
            for i, rid in enumerate(ids, 1):
                try:
                    rec = process(rid, cfg, conn)
                except Exception as e:  # keep the batch going; record the failure
                    rec = {"request_id": rid, "model": cfg.model, "error": f"{type(e).__name__}: {e}"}
                f.write(json.dumps(rec, default=str) + "\n")
                f.flush()
                print(f"[{i}/{len(ids)}] {rid} {rec.get('outcome', rec.get('error'))}",
                      file=sys.stderr)
        print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
