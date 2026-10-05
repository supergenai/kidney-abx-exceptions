"""Phoenix-style task: `task(input) -> output`, wrapping pipeline.run.process."""
from __future__ import annotations

from typing import Any, Callable

from evals.perturb import hook
from pipeline.extract import LLMConfig
from pipeline.run import process


def make_task(llm_config: LLMConfig, conn: Any = None, client: Any = None
              ) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """input = {"request_id": ..., "perturbation": {"kind": ...} (optional)}. Task errors become
    {"error": ...} so one bad example never aborts a run (evaluators treat it as a failure)."""
    def task(input: dict[str, Any]) -> dict[str, Any]:
        try:
            return process(input["request_id"], llm_config, conn, client,
                           ctx_hook=hook(input.get("perturbation")))
        except Exception as e:  # noqa: BLE001 - recorded, scored as error
            return {"request_id": input.get("request_id"), "error": f"{type(e).__name__}: {e}"}
    return task
