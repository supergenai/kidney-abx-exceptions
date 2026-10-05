from pathlib import Path

import pytest

from pipeline.renal import check, interval_hours, load_renal

TABLE = load_renal(Path(__file__).parent / "fixtures" / "renal_fixture.json")


def chk(egfr, dose, freq, dialysis="none", drug="fixturemycin", weight=None):
    return check(TABLE, drug, egfr, dialysis, dose, freq, weight)


@pytest.mark.parametrize("egfr,band", [(80, "egfr>=50"), (50, "egfr>=50"), (49.9, "egfr10-49"),
                                       (10, "egfr10-49"), (9.99, "egfr<10"), (0, "egfr<10")])
def test_band_boundaries(egfr, band):
    assert chk(egfr, 500, "q24h").band == band


def test_ok_and_less_frequent_is_ok():
    assert chk(60, 1000, "q8h").status == "OK"
    assert chk(30, 500, "q24h").status == "OK"  # longer interval than minimum


def test_adjust_dose_and_interval():
    r = chk(30, 1000, "q8h")
    assert r.status == "ADJUST" and "dose" in r.reason and "interval" in r.reason
    assert r.recommended == {"max_dose_mg": 500.0, "frequency": "q12h", "min_interval_hours": 12}


def test_dialysis_overrides_egfr():
    assert chk(80, 1000, "post-HD", dialysis="HD").status == "OK"
    assert chk(None, 1000, "q8h", dialysis="HD").status == "ADJUST"
    assert chk(None, 500, "q24h", dialysis="PD").band == "PD"


def test_unknown_cases():
    assert chk(None, 500, "q24h").status == "UNKNOWN"          # no eGFR, not on dialysis
    assert chk(60, 500, "whenever").status == "UNKNOWN"         # unparseable frequency
    assert chk(60, 500, "q8h", drug="nodrug").status == "UNKNOWN"
    assert chk(60, 500, "q24h", drug="weightomycin").status == "UNKNOWN"  # no weight


def test_weight_cap():
    # cap = min(1000, 8 mg/kg * 70 kg = 560)
    assert chk(60, 560, "q24h", drug="weightomycin", weight=70).status == "OK"
    r = chk(60, 600, "q24h", drug="weightomycin", weight=70)
    assert r.status == "ADJUST" and r.recommended["max_dose_mg"] == 560.0
    assert chk(60, 1000, "q24h", drug="weightomycin", weight=150).status == "OK"  # 1000 < 1200


@pytest.mark.parametrize("freq,hours", [("q8h", 8), ("Q12H", 12), ("q 24 h", 24), ("post-HD", 48),
                                        ("after dialysis", 48), ("every 8 hours", 8), ("BID", 12),
                                        ("q5d", 120), ("prn", None)])
def test_interval_parsing(freq, hours):
    assert interval_hours(freq, TABLE) == hours
