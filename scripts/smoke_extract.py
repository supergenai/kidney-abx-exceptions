"""One extraction call against tests/fixtures/smoke_note.json.
Usage: uv run python scripts/smoke_extract.py [--model ID] [--base-url URL] [--provider mlx|moonshot]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.extract import LLMConfig, build_source_text, extract

ap = argparse.ArgumentParser()
ap.add_argument("--provider", default="mlx")
ap.add_argument("--model")
ap.add_argument("--base-url")
a = ap.parse_args()
fx = json.loads((Path(__file__).parent.parent / "tests/fixtures/smoke_note.json").read_text())
cfg = LLMConfig.from_env(a.provider, a.model, a.base_url)
src = build_source_text(fx["justification"], fx["notes"])
ex = extract(fx["variables"], src, cfg)
tok_s = ex.usage.get("completion_tokens", 0) / ex.latency_s if ex.latency_s else 0
correct = sum(ex.variables[v]["value"] == e for v, e in fx["expected"].items())
print(json.dumps({"model": cfg.model, "latency_s": round(ex.latency_s, 2), "usage": ex.usage,
                  "completion_tok_per_s": round(tok_s, 1), "attempts": ex.attempts,
                  "parse_failed": ex.parse_failed, "correct": f"{correct}/{len(fx['expected'])}",
                  "variables": ex.variables, "raw": ex.raw}, indent=2))
