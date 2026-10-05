"""Checks the real trees/ against the pipeline (skipped until the files exist), plus an optional
DB integration test (skipped when Postgres is unreachable)."""
import json
from pathlib import Path

import pytest

from pipeline.extract import LLMConfig
from pipeline.structured import load_rules
from pipeline.tree import load_tree

TREES = Path(__file__).resolve().parent.parent / "trees"
TREE_FILES = sorted(p for p in TREES.glob("*.json") if p.name != "renal_dosing.json")


@pytest.mark.skipif(not TREE_FILES, reason="trees/ not generated yet")
@pytest.mark.parametrize("path", TREE_FILES, ids=lambda p: p.stem)
def test_real_tree_valid_and_rules_cover_structured(path):
    tree = load_tree(path)
    assert path.stem == tree["drug"]  # tree selection is trees/<drug>.json
    if (TREES / "variables.md").exists():
        rules = load_rules(TREES / "variables.md")
        need = {n for n, s in tree["variables"].items() if s["source"] != "text"}
        assert need <= set(rules), need - set(rules)


def _db():
    try:
        from pipeline import db
        conn = db.connect()
        conn.execute("SELECT 1 FROM exception_requests LIMIT 1")
        return conn
    except Exception:
        return None


def test_integration_db_structured_vs_gold():
    """Structured variables from the DB must equal gold labels (no LLM: fake client returns nulls)."""
    conn = _db()
    gold_path = TREES.parent / "data" / "gold.jsonl"
    if conn is None or not gold_path.exists():
        pytest.skip("Postgres or data/gold.jsonl unavailable")
    from types import SimpleNamespace

    from pipeline.run import process

    class NullClient:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))], usage=None)))

    from pipeline.db import request_ids
    in_db = set(request_ids(conn))
    golds = [g for g in (json.loads(x) for x in gold_path.read_text().splitlines() if x.strip())
             if g["request_id"] in in_db]
    if not golds:
        conn.close()
        pytest.skip("gold.jsonl and DB out of sync (dataset being regenerated?)")
    cfg = LLMConfig(model="null", base_url="http://unused")
    mismatches = []
    for g in golds[:20]:
        out = process(g["request_id"], cfg, conn, client=NullClient())
        assert out["tree_id"] == g["tree_id"]
        for name, v in out["variables"].items():
            if v["source"] == "structured" and name in g["variables"]:
                gv = g["variables"][name]
                gv = gv.get("value") if isinstance(gv, dict) else gv
                if v["value"] != gv:
                    mismatches.append((g["request_id"], name, v["value"], gv))
    conn.close()
    assert not mismatches, mismatches
