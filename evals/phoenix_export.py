"""Upload an eval dataset to Arize Phoenix (optional; arize-phoenix is NOT a project dependency).

  uv run --with arize-phoenix-client python -m evals.phoenix_export behavioral [--name kidney-abx-behavioral]

Phoenix example fields map 1:1: input -> inputs, expected -> outputs, metadata -> metadata.
Then `phoenix.client` experiments can reuse evals.task.make_task(...) as the task and the functions in
evals.evaluators as `evaluators=[...]` (their parameter names already match Phoenix binding).
"""
from __future__ import annotations

import argparse
from typing import Any

from evals import datasets


def to_phoenix(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {"inputs": [r["input"] for r in rows], "outputs": [r["expected"] for r in rows],
            "metadata": [{**r["metadata"], "example_id": r["id"]} for r in rows]}


def upload(suite: str, name: str | None = None) -> Any:
    cols, name = to_phoenix(datasets.load(suite)), name or f"kidney-abx-{suite}"
    try:  # current client package (arize-phoenix-client)
        from phoenix.client import Client
        return Client().datasets.create_dataset(name=name, inputs=cols["inputs"],
                                                outputs=cols["outputs"], metadata=cols["metadata"])
    except ImportError:
        pass
    try:  # legacy arize-phoenix
        import phoenix as px
        return px.Client().upload_dataset(dataset_name=name, inputs=cols["inputs"],
                                          outputs=cols["outputs"], metadata=cols["metadata"])
    except ImportError as e:
        raise SystemExit("phoenix not installed: `uv run --with arize-phoenix-client ...`") from e


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("suite", choices=datasets.SUITES)
    ap.add_argument("--name")
    a = ap.parse_args()
    print(upload(a.suite, a.name))
