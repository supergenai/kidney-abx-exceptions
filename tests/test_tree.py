import copy
import json
from pathlib import Path

import pytest

from pipeline.tree import NEED_INFO, TreeError, load_tree, validate_tree, walk

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def tree():
    return load_tree(FIX / "tree_basic.json")


def test_approve_short_path(tree):
    r = walk(tree, {"esbl_isolated": True})
    assert r.outcome == "APPROVE_ELIGIBLE"
    assert [s.as_tuple() for s in r.path] == [("n1", "esbl_isolated", True, "yes")]
    assert r.missing == []


def test_deny(tree):
    r = walk(tree, {"esbl_isolated": False, "septic_shock": False})
    assert r.outcome == "DENY_ELIGIBLE"
    assert [s.branch for s in r.path] == ["no", "no"]


def test_in_op_and_review(tree):
    base = {"esbl_isolated": False, "septic_shock": True}
    assert walk(tree, base | {"pcn_allergy_severity": "anaphylaxis"}).outcome == "APPROVE_ELIGIBLE"
    r = walk(tree, base | {"pcn_allergy_severity": "mild", "egfr": 10})
    assert r.outcome == "PHARMACIST_REVIEW"
    assert [s.node_id for s in r.path] == ["n1", "n2", "n3", "n4"]
    assert r.to_dict()["path"][-1] == ["n4", "egfr", 10, "yes"]


def test_need_info_on_null(tree):
    r = walk(tree, {"esbl_isolated": False, "septic_shock": None})
    assert r.outcome == NEED_INFO
    assert r.missing == ["septic_shock"]
    assert [s.as_tuple() for s in r.path] == [("n1", "esbl_isolated", False, "no"),
                                              ("n2", "septic_shock", None, None)]


def test_need_info_irrelevant_null_ignored(tree):
    # septic_shock unknown but not needed on this path
    assert walk(tree, {"esbl_isolated": True, "septic_shock": None}).outcome == "APPROVE_ELIGIBLE"


def test_false_is_not_missing(tree):
    assert walk(tree, {"esbl_isolated": False, "septic_shock": False}).outcome == "DENY_ELIGIBLE"


def _broken(mutate):
    t = json.loads((FIX / "tree_basic.json").read_text())
    mutate(t)
    with pytest.raises(TreeError) as e:
        validate_tree(t)
    return str(e.value)


def test_unknown_var():
    assert "unknown variable" in _broken(lambda t: t["nodes"]["n2"].update(var="nope"))


def test_dangling_id():
    assert "dangling" in _broken(lambda t: t["nodes"]["n2"].update(yes="n99"))


def test_cycle():
    assert "cycle" in _broken(lambda t: t["nodes"]["n4"].update(no="n2"))


def test_unreachable():
    msg = _broken(lambda t: t["nodes"].update(
        n5={"var": "egfr", "op": ">", "value": 1, "yes": "DENY_ELIGIBLE", "no": "DENY_ELIGIBLE"}))
    assert "unreachable" in msg and "n5" in msg


def test_bad_enum_value_and_op():
    msg = _broken(lambda t: t["nodes"]["n3"].update(value=["fatal"]))
    assert "not allowed" in msg
    assert "bad op" in _broken(lambda t: t["nodes"]["n4"].update(op="~="))


def test_walk_is_pure(tree):
    before = copy.deepcopy(tree)
    vals = {"esbl_isolated": False, "septic_shock": True, "pcn_allergy_severity": "none", "egfr": 40}
    assert walk(tree, vals).to_dict() == walk(tree, vals).to_dict()
    assert tree == before
