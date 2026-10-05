"""Idempotent install of behavioral fixtures into Postgres under the reserved `EVAL-` prefix.

Never truncates; only rows whose patient_id starts with `EVAL-` are deleted (FKs cascade).
data/generate.py TRUNCATEs every table on regeneration, so re-run this afterwards (evals.run does it
automatically before the behavioral suite).

  uv run python -m evals.fixtures_db            # (re)install
  uv run python -m evals.fixtures_db --remove   # delete EVAL- rows only
  uv run python -m evals.fixtures_db --check    # install inside a rolled-back transaction and verify
                                                # the real SQL rules agree with behavioral.structured_values
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from typing import Any

import psycopg

from evals.behavioral import CASES, MICRO, REQUESTED_AT, structured_values
from pipeline.db import EVAL_PREFIX

CKD = [(45, "3a"), (30, "3b"), (15, "4"), (0, "5")]


def ids(case: dict[str, Any]) -> tuple[str, str]:
    return f"{EVAL_PREFIX}P{case['id']}", f"{EVAL_PREFIX}R{case['id']}"


def remove(conn: psycopg.Connection) -> int:
    return conn.execute("DELETE FROM patients WHERE patient_id LIKE %s", (EVAL_PREFIX + "%",)).rowcount


def _insert(conn: psycopg.Connection, case: dict[str, Any]) -> None:
    pid, rid = ids(case)
    at = REQUESTED_AT
    ckd = "5D" if case["dialysis"] != "none" else next(s for lo, s in CKD if case["egfr"] >= lo)
    conn.execute(
        "INSERT INTO patients (patient_id, mrn, age, sex, weight_kg, ckd_stage, dialysis_modality,"
        " dialysis_days, clinic) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (pid, f"SYN-FAKE-EVAL-{case['id']}", 64, "M", case["weight"], ckd, case["dialysis"],
         "MWF" if case["dialysis"] == "HD" else None, "Synthetic Eval Clinic"))
    if case["egfr"] is not None:
        conn.execute("INSERT INTO labs (patient_id, test_code, test_name, value, unit, collected_at)"
                     " VALUES (%s,'EGFR','eGFR',%s,'mL/min/1.73m2',%s)",
                     (pid, case["egfr"], at - timedelta(days=1)))
    for agent, reaction, severity in case["allergies"]:
        conn.execute("INSERT INTO allergies (patient_id, agent, reaction, severity, recorded_at)"
                     " VALUES (%s,%s,%s,%s,%s)", (pid, agent, reaction, severity, at - timedelta(days=365)))
    for key in case["micro"]:
        for specimen, organism, abx, interp in MICRO[key]:
            conn.execute("INSERT INTO microbiology (patient_id, specimen, collected_at, organism,"
                         " antibiotic, interpretation, mic) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (pid, specimen, at - timedelta(days=2), organism, abx, interp, None))
    for hours, note_type, body in case["notes"]:
        conn.execute("INSERT INTO clinical_notes (patient_id, note_type, author_role, written_at, body)"
                     " VALUES (%s,%s,'physician',%s,%s)", (pid, note_type, at - timedelta(hours=hours), body))
    conn.execute(
        "INSERT INTO exception_requests (request_id, patient_id, drug, dose_mg, frequency, route,"
        " indication, justification, requested_at, prescriber) VALUES (%s,%s,%s,%s,%s,'IV',%s,%s,%s,%s)",
        (rid, pid, case["drug"], case["dose"], case["freq"], f"eval case {case['case_type']}",
         case["justification"], at, "Dr. Eval Fixture (SYNTHETIC)"))


def install(conn: psycopg.Connection) -> int:
    """Delete then insert all EVAL- fixtures in one transaction."""
    with conn.transaction():
        remove(conn)
        for case in CASES:
            _insert(conn, case)
    return len(CASES)


def check(conn: psycopg.Connection) -> list[str]:
    """Install in a transaction that is always rolled back; compare SQL-derived structured vars and
    the loader's note window with the Python mirror. Returns mismatch descriptions."""
    from pipeline import db, structured
    from pipeline.run import tree_path
    from pipeline.tree import load_tree
    problems: list[str] = []
    with conn.transaction(force_rollback=True):
        remove(conn)
        for case in CASES:
            _insert(conn, case)
        for case in CASES:
            ctx = db.load_context(conn, ids(case)[1])
            specs = load_tree(tree_path(case["drug"]))["variables"]
            got = {n: v for n, (v, _) in structured.derive(specs, ctx, conn).items()}
            want = structured_values(case)
            problems += [f"{case['id']} {n}: sql={v!r} mirror={want[n]!r}"
                         for n, v in got.items() if v != want[n]]
            in_window = sum(1 for h, *_ in case["notes"] if 0 <= h <= 7 * 24)
            if len(ctx.notes) != in_window:
                problems.append(f"{case['id']}: loader returned {len(ctx.notes)} notes, want {in_window}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--remove", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    from pipeline.db import connect
    with connect() as conn:
        if a.remove:
            print(f"removed {remove(conn)} EVAL- patients")
        elif a.check:
            problems = check(conn)
            print("\n".join(problems) or f"OK: {len(CASES)} cases, SQL agrees with mirror (rolled back)")
            return 1 if problems else 0
        else:
            print(f"installed {install(conn)} EVAL- cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
