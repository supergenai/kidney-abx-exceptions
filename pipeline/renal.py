"""Renal dose check against trees/renal_dosing.json. Deterministic; illustrative tables only.

Band: dialysis_modality HD/PD -> that band; else first eGFR band with egfr_min <= eGFR < egfr_max.
OK iff dose_mg <= cap (max_dose_mg, and max_mg_per_kg * weight when given) and
interval_hours(frequency) >= min_interval_hours. Otherwise ADJUST with the band's regimen.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_NAMED = {"qd": 24, "daily": 24, "once daily": 24, "bid": 12, "tid": 8, "qid": 6}


@dataclass
class RenalCheck:
    status: str  # OK | ADJUST | UNKNOWN
    reason: str
    band: str | None = None
    recommended: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_renal(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def interval_hours(freq: str, table: dict[str, Any]) -> float | None:
    """Table key first (case/space-insensitive), then generic 'qNh' / 'every N hours' / BID."""
    f = re.sub(r"\s+", "", freq.strip().lower())
    known = {re.sub(r"\s+", "", k.lower()): v for k, v in table.get("frequency_hours", {}).items()}
    if f in known:
        return float(known[f])
    if re.fullmatch(r"(post|after)-?(hd|dialysis)", f) and "post-hd" in known:
        return float(known["post-hd"])
    if m := re.fullmatch(r"q(\d+(?:\.\d+)?)h(?:rs?|ours?)?", f) or re.fullmatch(
            r"every(\d+(?:\.\d+)?)h(?:rs?|ours?)?", f):
        return float(m.group(1))
    if m := re.fullmatch(r"q(\d+)d", f):
        return 24.0 * int(m.group(1))
    return float(_NAMED[f]) if f in _NAMED else None


def select_band(spec: dict[str, Any], egfr: float | None, dialysis: str | None
                ) -> dict[str, Any] | None:
    bands = spec.get("bands", [])
    if dialysis in ("HD", "PD"):
        return next((b for b in bands if b["band"] == dialysis), None)
    if egfr is None:
        return None
    return next((b for b in bands if "egfr_min" in b and b["egfr_min"] <= egfr
                 and (b.get("egfr_max") is None or egfr < b["egfr_max"])), None)


def check(table: dict[str, Any], drug: str, egfr: float | None, dialysis: str | None,
          dose_mg: float, frequency: str, weight_kg: float | None = None) -> RenalCheck:
    spec = table.get("drugs", {}).get(drug)
    if spec is None:
        return RenalCheck("UNKNOWN", f"no renal table for {drug}")
    band = select_band(spec, egfr, dialysis)
    if band is None:
        why = "no eGFR at or before request" if egfr is None and dialysis not in ("HD", "PD") \
            else f"no band for eGFR={egfr} dialysis={dialysis}"
        return RenalCheck("UNKNOWN", why)
    cap = float(band["max_dose_mg"])
    if "max_mg_per_kg" in band:
        if weight_kg is None:
            return RenalCheck("UNKNOWN", "weight-based cap but no weight", band["band"])
        cap = min(cap, band["max_mg_per_kg"] * float(weight_kg))
    rec = {"max_dose_mg": round(cap, 1), "frequency": band["frequency"],
           "min_interval_hours": band["min_interval_hours"]}
    hours = interval_hours(frequency, table)
    if hours is None:
        return RenalCheck("UNKNOWN", f"unrecognised frequency {frequency!r}", band["band"], rec)
    problems = []
    if dose_mg > cap + 1e-9:
        problems.append(f"dose {dose_mg:g} mg > cap {cap:g} mg")
    if hours < band["min_interval_hours"]:
        problems.append(f"interval {hours:g}h < {band['min_interval_hours']}h")
    if problems:
        return RenalCheck("ADJUST", "; ".join(problems), band["band"], rec)
    return RenalCheck("OK", "within band limits", band["band"], rec)
