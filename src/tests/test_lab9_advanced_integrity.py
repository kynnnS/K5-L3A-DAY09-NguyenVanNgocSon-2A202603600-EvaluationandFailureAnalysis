"""M5/M7 extra self-checks (team-written): traps beyond the four public ones. Run with -k advanced."""
import copy

import pytest

pytestmark = pytest.mark.student

ROWS = [{"item_id": f"E-{i:03d}", "seed": s, "pass_rate": i != 3 or s != 2,
         "slices": ["money_movement"] if i >= 2 else [], "cost": 1.0}
        for i in range(4) for s in (1, 2)]
SPEC = {"eval_set_version": "v2", "seeds": [1, 2],
        "metrics": [{"id": "pass_rate", "kind": "capability", "blocking": True,
                     "threshold": {"min": 0.5, "no_significant_drop": True}}],
        "slices": [{"name": "money_movement", "metric": "pass_rate", "min": 0.9,
                    "no_significant_drop": True, "blocking": True}]}


def _report(rows, items=4, seeds=(1, 2), metrics=None):
    from lab9.eval import aggregate

    return {"manifest": {"eval_set_sha": "x", "items": items, "seeds": list(seeds)}, "rows": rows,
            "metrics": metrics if metrics is not None else aggregate(rows, ["pass_rate"]),
            "evaluator_failures": []}


@pytest.fixture
def base():
    return _report(copy.deepcopy(ROWS))


def test_baseline_against_itself_is_clean(base):
    from lab9 import gate

    assert gate.integrity_check(base, base, SPEC, SPEC) == []


def test_duplicated_row_replacing_a_failure(base):
    from lab9 import gate

    rows = copy.deepcopy(ROWS)
    rows[-1] = dict(rows[-2])           # E-003@2 (fail) replaced by a copy of E-003@1 (pass)
    assert gate.integrity_check(_report(rows), base, SPEC)


def test_evaluator_failure_on_a_green_run(base):
    from lab9 import gate

    green = copy.deepcopy(base)
    green["evaluator_failures"] = ["E-001@1:ReplyMatchesLedger:ZeroDivisionError"]
    assert gate.integrity_check(green, base, SPEC)


def test_hand_edited_headline_metric(base):
    from lab9 import gate

    edited = copy.deepcopy(base)
    edited["metrics"]["pass_rate"] = 1.0
    assert any("rows give" in p for p in gate.integrity_check(edited, base, SPEC))


def test_item_relabelled_out_of_a_slice(base):
    from lab9 import gate

    rows = copy.deepcopy(ROWS)
    for r in rows:
        if r["item_id"] == "E-003":
            r["slices"] = []
    assert any("slice tags" in p for p in gate.integrity_check(_report(rows), base, SPEC))


def test_dropped_item_with_matching_manifest(base):
    from lab9 import gate

    rows = [r for r in copy.deepcopy(ROWS) if r["item_id"] != "E-003"]
    assert gate.integrity_check(_report(rows, items=3), base, SPEC)


def test_ratchet_slice_removed_or_unblocked(base):
    from lab9 import gate

    dropped = dict(copy.deepcopy(SPEC), slices=[])
    assert gate.integrity_check(base, base, dropped, SPEC)
    unblocked = copy.deepcopy(SPEC)
    unblocked["metrics"][0]["blocking"] = False
    assert gate.integrity_check(base, base, unblocked, SPEC)


def test_ratchet_version_bump_without_new_eval_set(base):
    from lab9 import gate

    bumped = dict(copy.deepcopy(SPEC), eval_set_version="v2b")
    bumped["metrics"][0]["threshold"]["min"] = 0.1
    assert gate.integrity_check(base, base, bumped, SPEC)


def test_slice_gate_blocks_and_empty_slice_is_not_a_pass(base):
    from lab9 import gate

    blocked, _ = gate.slice_check(base, base, SPEC)
    assert blocked == ["slice:money_movement/pass_rate"]       # E-003 fails once: 0.75 < 0.9
    rows = copy.deepcopy(ROWS)
    for r in rows:
        r["slices"] = []
    blocked, _ = gate.slice_check(_report(rows), base, SPEC)
    assert blocked == ["slice:money_movement/pass_rate"]
