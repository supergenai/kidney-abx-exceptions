"""Postgres access. Nothing after requested_at is ever read."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

DEFAULT_URL = "postgresql://kidney_abx:kidney_abx@localhost:5433/kidney_abx"
NOTE_WINDOW_DAYS = 7  # trees/variables.md: requested_at - 7d <= written_at <= requested_at

Row = dict[str, Any]


def connect(url: str | None = None) -> psycopg.Connection:
    from dotenv import load_dotenv
    load_dotenv()
    return psycopg.connect(url or os.environ.get("DATABASE_URL", DEFAULT_URL),
                           row_factory=dict_row, connect_timeout=3, autocommit=True)


@dataclass
class Context:
    request: Row
    patient: Row
    notes: list[Row]  # current-episode window, newest first


def load_context(conn: psycopg.Connection, request_id: str) -> Context:
    req = conn.execute("SELECT * FROM exception_requests WHERE request_id = %s",
                       (request_id,)).fetchone()
    if req is None:
        raise KeyError(f"request {request_id} not found")
    pid, t = req["patient_id"], req["requested_at"]
    patient = conn.execute("SELECT * FROM patients WHERE patient_id = %s", (pid,)).fetchone()
    notes = conn.execute(
        "SELECT * FROM clinical_notes WHERE patient_id = %s"
        " AND written_at BETWEEN %s::timestamptz - make_interval(days => %s) AND %s"
        " ORDER BY written_at DESC", (pid, t, NOTE_WINDOW_DAYS, t)).fetchall()
    return Context(request=req, patient=patient, notes=notes)


EVAL_PREFIX = "EVAL-"  # behavioral eval fixtures (evals/fixtures_db.py); never part of the dataset


def request_ids(conn: psycopg.Connection) -> list[str]:
    return [r["request_id"] for r in conn.execute(
        "SELECT request_id FROM exception_requests WHERE request_id NOT LIKE %s ORDER BY request_id",
        (EVAL_PREFIX + "%",))]
