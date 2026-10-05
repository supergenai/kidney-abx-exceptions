from types import SimpleNamespace

import pytest

from pipeline.extract import (LLMConfig, build_source_text, coerce, extract, parse_json_object,
                              quote_ok, verify)

SRC = ("[Exception request justification]\nFailed 72h of  piperacillin-tazobactam.\n\n"
       "[progress]\nPatient   on norepinephrine.\nDenies penicillin allergy. "
       "No history of C. difficile.")
VARS = {
    "septic_shock": {"type": "bool"},
    "c_diff_history": {"type": "bool"},
    "pcn_allergy_severity": {"type": "enum", "values": ["none", "mild", "severe", "anaphylaxis"]},
    "lactate": {"type": "number"},
}

# ---- quote check ----

def test_quote_verbatim():
    assert quote_ok("Patient   on norepinephrine.", SRC)


def test_quote_whitespace_and_case_normalised():
    assert quote_ok("failed 72H of piperacillin-tazobactam", SRC)
    assert quote_ok("patient on\nnorepinephrine", SRC)


def test_quote_paraphrase_fails():
    assert not quote_ok("patient is on vasopressors", SRC)
    assert not quote_ok("", SRC) and not quote_ok(None, SRC)


def test_verify_nulls_paraphrase_and_flags():
    out = verify({"septic_shock": {"value": True, "evidence": "on pressors"}}, VARS, SRC)
    assert out["septic_shock"] == {"value": None, "evidence": "on pressors", "quote_failed": True}


def test_verify_value_without_evidence_is_nulled():
    out = verify({"septic_shock": True}, VARS, SRC)  # flat shorthand, no evidence
    assert out["septic_shock"]["value"] is None and out["septic_shock"]["quote_failed"]


def test_negation_false_value_passes():
    out = verify({"c_diff_history": {"value": "no", "evidence": "No history of C. difficile"}},
                 VARS, SRC)
    assert out["c_diff_history"] == {"value": False, "evidence": "No history of C. difficile",
                                     "quote_failed": False}


def test_negation_conflict_true_from_negated_span():
    # quote is a verbatim substring but the source negates it
    out = verify({"c_diff_history": {"value": True, "evidence": "history of C. difficile"}},
                 VARS, SRC)
    assert out["c_diff_history"]["value"] is None
    assert out["c_diff_history"]["negation_conflict"] is True
    assert out["c_diff_history"]["quote_failed"] is False


def test_negation_does_not_cross_sentence_boundary():
    src = "This patient: no pressors, MAP 82. Perinephric abscess drained by IR yesterday."
    spec = {"src_ctl": {"type": "bool"}}
    out = verify({"src_ctl": {"value": True, "evidence": "Perinephric abscess drained by IR"}},
                 spec, src)
    assert out["src_ctl"]["value"] is True and "negation_conflict" not in out["src_ctl"]
    # same-sentence negation still blocks a true value
    out = verify({"src_ctl": {"value": True, "evidence": "abscess drained"}}, spec,
                 "MAP 82. No abscess drained yet.")
    assert out["src_ctl"]["value"] is None and out["src_ctl"]["negation_conflict"] is True


@pytest.mark.parametrize("name,evidence,ok", [
    ("septic_shock", "MRSA bacteremia from AV graft; ID following.", False),
    ("septic_shock", "Day 4 cefepime with improvement, afebrile, no treatment failure.", False),
    ("septic_shock", "Not septic, no pressors, MAP 80s.", True),
    ("septic_shock", "On norepinephrine 0.1 mcg/kg/min", True),
    ("hemodynamic_instability", "T 37.1, HR 92, RR 16, SpO2 98% RA", False),
    ("hemodynamic_instability", "BP 86/54", True),
    ("source_control_achieved", "Repeat blood cx drawn 3/3: no growth to date", False),
    ("vanc_failure_or_intolerance", "Vanc off the table d/t severe DRESS", True),
    ("unlisted_var", "anything", True),
])
def test_on_topic(name, evidence, ok):
    from pipeline.extract import on_topic
    assert on_topic(name, evidence) is ok


def test_verify_relevance_nulls_off_topic_quote():
    src = "MRSA bacteremia from AV graft; ID following."
    raw = {"septic_shock": {"evidence": "MRSA bacteremia from AV graft", "value": True}}
    spec = {"septic_shock": {"type": "bool"}}
    assert verify(raw, spec, src)["septic_shock"]["value"] is True  # off by default
    out = verify(raw, spec, src, relevance=True)["septic_shock"]
    assert out["value"] is None and out["off_topic"] is True


def test_cross_check_nulls_shock_without_instability():
    from pipeline.extract import cross_check
    out = cross_check({"septic_shock": {"value": True, "evidence": "a"},
                       "hemodynamic_instability": {"value": False, "evidence": "b"}})
    assert out["septic_shock"]["value"] is None and out["hemodynamic_instability"]["value"] is None
    assert out["septic_shock"]["cross_conflict"] is True
    ok = cross_check({"septic_shock": {"value": True}, "hemodynamic_instability": {"value": True}})
    assert ok["septic_shock"]["value"] is True and "cross_conflict" not in ok["septic_shock"]


def test_stale_check_nulls_true_from_older_note():
    from pipeline.extract import build_source_text, stale_check
    notes = [{"written_at": "2026-09-13", "note_type": "progress", "author_role": "MD",
              "body": "On norepinephrine, MAP 58, septic shock."},
             {"written_at": "2026-09-15", "note_type": "progress", "author_role": "MD",
              "body": "Pressors off since 06:00, MAP 82 off vasopressors, shock resolved."}]
    src = build_source_text("Requesting vancomycin.", notes)
    out = stale_check({"septic_shock": {"value": True,
                                        "evidence": "On norepinephrine, MAP 58, septic shock."}}, src)
    assert out["septic_shock"]["value"] is None and out["septic_shock"]["stale_conflict"]
    # the same quote in the NEWEST note stands
    notes[0]["written_at"] = "2026-09-16"
    src = build_source_text("Requesting vancomycin.", notes)
    out = stale_check({"septic_shock": {"value": True,
                                        "evidence": "On norepinephrine, MAP 58, septic shock."}}, src)
    assert out["septic_shock"]["value"] is True


def test_prompt_v3_adds_clarifications():
    from pipeline.extract import build_prompt
    spec = {"hemodynamic_instability": {"type": "bool"}}
    assert "not in shock" in build_prompt(spec, SRC, "v3")[1]["content"]
    assert "not in shock" not in build_prompt(spec, SRC, "v2")[1]["content"]


def test_null_value_stays_null_and_unflagged():
    out = verify({"lactate": {"value": None, "evidence": None}}, VARS, SRC)
    assert out["lactate"] == {"value": None, "evidence": None, "quote_failed": False}
    assert verify({}, VARS, SRC)["septic_shock"]["value"] is None  # missing key -> null

# ---- JSON parsing ----

@pytest.mark.parametrize("text", [
    '{"a": {"value": true, "evidence": "x"}}',
    '```json\n{"a": {"value": true, "evidence": "x"}}\n```',
    'Sure! Here is the JSON:\n{"a": {"value": true, "evidence": "x"}}\nLet me know.',
    '<think>maybe {"a": 1}</think>{"a": {"value": true, "evidence": "x"}}',
    'prefix {not json} then {"a": {"value": true, "evidence": "x"}} trailing }',
])
def test_parse_robust(text):
    assert parse_json_object(text) == {"a": {"value": True, "evidence": "x"}}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", '{"a": '])
def test_parse_failure(text):
    assert parse_json_object(text) is None

# ---- coercion ----

@pytest.mark.parametrize("raw,expected", [
    (True, True), ("yes", True), ("Yes", True), ("TRUE", True), (1, True),
    ("no", False), ("false", False), (0, False), ("unknown", None), ("maybe", None), (None, None),
])
def test_coerce_bool(raw, expected):
    assert coerce(raw, {"type": "bool"}) is expected


def test_coerce_enum_and_number():
    spec = VARS["pcn_allergy_severity"]
    assert coerce("Anaphylaxis", spec) == "anaphylaxis"
    assert coerce(" SEVERE ", spec) == "severe"
    assert coerce("hives", spec) is None
    assert coerce("4.2 mmol/L", {"type": "number"}) == 4.2
    assert coerce(True, {"type": "number"}) is None
    assert coerce("12", {"type": "int"}) == 12

# ---- source text ----

def test_source_text_order_and_cap():
    notes = [{"written_at": "2026-01-01", "body": "OLD"}, {"written_at": "2026-02-01", "body": "NEW"}]
    s = build_source_text("JUST", notes)
    assert s.index("JUST") < s.index("NEW") < s.index("OLD")
    assert len(build_source_text("x" * 10_000, notes)) == 6000

# ---- extract() with a fake client: retry then all-null ----

class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        assert kw["temperature"] == 0
        self.calls += 1
        msg = SimpleNamespace(content=self.replies.pop(0))
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


CFG = LLMConfig(model="fake", base_url="http://x", api_key="secret-key")


def test_extract_retry_then_success():
    c = FakeClient(["garbage", '{"septic_shock": {"value": "yes", "evidence": "on norepinephrine"}}'])
    ex = extract(VARS, SRC, CFG, client=c)
    assert c.calls == 2 and ex.attempts == 2 and not ex.parse_failed
    assert ex.variables["septic_shock"]["value"] is True
    assert ex.usage == {"prompt_tokens": 20, "completion_tokens": 10}


def test_extract_all_null_after_two_failures():
    ex = extract(VARS, SRC, CFG, client=FakeClient(["nope", "still nope"]))
    assert ex.parse_failed and all(v["value"] is None for v in ex.variables.values())


def test_extract_per_var_mode_one_call_per_variable():
    two = {k: VARS[k] for k in ("septic_shock", "c_diff_history")}
    c = FakeClient(['{"septic_shock": {"evidence": "on norepinephrine", "value": true}}',
                    '{"c_diff_history": {"evidence": "No history of C. difficile", "value": false}}'])
    cfg = LLMConfig(model="fake", base_url="http://x", prompt_version="v2", mode="per_var")
    ex = extract(two, SRC, cfg, client=c)
    assert c.calls == 2 and ex.usage == {"prompt_tokens": 20, "completion_tokens": 10}
    assert ex.variables["septic_shock"]["value"] is True
    assert ex.variables["c_diff_history"]["value"] is False


def test_prompt_v2_evidence_first_and_guide():
    from pipeline.extract import SYSTEM_PROMPT_V2, build_prompt
    msgs = build_prompt({"septic_shock": {"type": "bool", "description": "short"}}, SRC, "v2")
    assert msgs[0]["content"] == SYSTEM_PROMPT_V2  # stable prefix for the server prompt cache
    assert SYSTEM_PROMPT_V2.index('"evidence"') < SYSTEM_PROMPT_V2.index('"value"')
    assert "vasopressors" in msgs[1]["content"] and "short" not in msgs[1]["content"]
    assert msgs[1]["content"].index("VARIABLES") < msgs[1]["content"].index(SRC[:20])


def test_config_repr_hides_key():
    assert "secret-key" not in repr(CFG)

# ---- value<->quote consistency ----

ALLERGY_SRC = ("PCN allergy: ANAPHYLAXIS to amoxicillin 2019. Sulfa: itchy rash. "
               "Cefazolin: angioedema. Vanc: moderate reaction. No anaphylaxis to ceftriaxone. "
               "NKDA per prior record.")
SEV = {"type": "enum", "values": ["none", "mild", "moderate", "severe", "anaphylaxis"]}


def _one(var_spec, value, evidence, src=ALLERGY_SRC):
    return verify({"v": {"value": value, "evidence": evidence}}, {"v": var_spec}, src)["v"]


def test_enum_value_contradicted_by_quote_is_nulled():
    out = _one(SEV, "moderate", "PCN allergy: ANAPHYLAXIS to amoxicillin 2019")
    assert out["value"] is None and out["consistency_failed"] is True
    assert out["quote_failed"] is False


def test_enum_exact_match_passes():
    out = _one(SEV, "anaphylaxis", "PCN allergy: ANAPHYLAXIS to amoxicillin 2019")
    assert out == {"value": "anaphylaxis", "evidence": "PCN allergy: ANAPHYLAXIS to amoxicillin 2019",
                   "quote_failed": False}


@pytest.mark.parametrize("value,evidence", [
    ("mild", "Sulfa: itchy rash"), ("severe", "Cefazolin: angioedema"),
    ("none", "NKDA per prior record"), ("moderate", "Vanc: moderate reaction"),
    ("moderate", "Sulfa: itchy rash"),  # variables.md: "amoxicillin rash" -> moderate
])
def test_enum_synonym_match_passes(value, evidence):
    out = _one(SEV, value, evidence)
    assert out["value"] == value and "consistency_failed" not in out


@pytest.mark.parametrize("value,evidence", [
    ("severe", "Sulfa: itchy rash"), ("mild", "Cefazolin: angioedema"),
    ("anaphylaxis", "NKDA per prior record"),
])
def test_enum_synonym_mismatch_fails(value, evidence):
    assert _one(SEV, value, evidence)["consistency_failed"] is True


def test_enum_negated_mention_and_unclear_quote_do_not_fail():
    # "No anaphylaxis" does not name anaphylaxis; nothing else named -> unsure -> keep
    assert _one(SEV, "mild", "No anaphylaxis to ceftriaxone")["value"] == "mild"
    assert _one(SEV, "none", "No anaphylaxis to ceftriaxone")["value"] == "none"
    assert _one(SEV, "moderate", "amoxicillin 2019")["value"] == "moderate"


def test_bool_false_with_affirmative_quote_fails():
    out = verify({"septic_shock": {"value": False, "evidence": "Patient   on norepinephrine."}},
                 VARS, SRC)["septic_shock"]
    assert out["value"] is None and out["consistency_failed"] is True


@pytest.mark.parametrize("evidence", ["Denies penicillin allergy", "history of C. difficile"])
def test_bool_false_with_negated_quote_passes(evidence):
    # explicit cue in quote, or quote negated by the surrounding source text
    out = verify({"c_diff_history": {"value": False, "evidence": evidence}}, VARS, SRC)
    assert out["c_diff_history"]["value"] is False and "consistency_failed" not in out["c_diff_history"]


def test_bool_true_affirmative_passes_and_negated_conflicts():
    ok = verify({"septic_shock": {"value": True, "evidence": "on norepinephrine"}}, VARS, SRC)
    assert ok["septic_shock"]["value"] is True and "consistency_failed" not in ok["septic_shock"]
    bad = verify({"c_diff_history": {"value": True, "evidence": "No history of C. difficile"}},
                 VARS, SRC)
    assert bad["c_diff_history"]["value"] is None and bad["c_diff_history"]["negation_conflict"]
