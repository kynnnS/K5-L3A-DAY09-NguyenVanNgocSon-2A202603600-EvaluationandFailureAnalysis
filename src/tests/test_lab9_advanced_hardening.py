"""M5-M7 hardening (team-written): integrity on BOTH reports, ratchet, bootstrap confidence, and
end-to-end gate decisions on variants built from lab9.variant_agent switches (FM-08, honest refactor).
Run with -k advanced. No claim id, seed or expected metric value is hard-coded in the gate or graders."""
import copy
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.student

ROWS = [{"item_id": f"E-{i:03d}", "seed": s, "pass_rate": i != 3 or s != 2,
         "slices": ["money_movement"] if i >= 2 else [], "cost": 1.0}
        for i in range(4) for s in (1, 2)]
SPEC = {"eval_set_version": "v2", "seeds": [1, 2],
        "bootstrap": {"n_resamples": 1000, "rng": 2027, "confidence": 0.95},
        "metrics": [{"id": "pass_rate", "kind": "capability", "blocking": True,
                     "threshold": {"min": 0.5, "no_significant_drop": True}},
                    {"id": "cost_per_success", "kind": "system", "threshold": {"max_increase": 0.1}}],
        "slices": [{"name": "money_movement", "metric": "pass_rate", "min": 0.5,
                    "no_significant_drop": True, "blocking": True}]}


def _report(rows, items=4, seeds=(1, 2), metrics=None):
    from lab9.eval import aggregate

    return {"manifest": {"eval_set_sha": "x", "items": items, "seeds": list(seeds)}, "rows": rows,
            "metrics": metrics if metrics is not None else aggregate(rows, ["pass_rate"]),
            "evaluator_failures": []}


@pytest.fixture
def base():
    return _report(copy.deepcopy(ROWS))


def _problems(pr, base, spec=SPEC, base_spec=SPEC):
    from lab9 import gate

    return gate.integrity_check(pr, base, spec, base_spec)


def test_baseline_against_itself_merges(base):
    from lab9 import gate

    assert _problems(base, base) == []
    blocked, _ = gate.check(base, base, SPEC, SPEC)
    assert blocked == []


# ---- integrity: the baseline is checked too ----------------------------------------------------
@pytest.mark.parametrize("tamper", [
    lambda r: r["metrics"].__setitem__("pass_rate", 1.0),                      # hand-edited headline
    lambda r: r["rows"].append(dict(r["rows"][0])),                            # duplicated row
    lambda r: r.__setitem__("evaluator_failures", ["E-000@1:TrialPass:boom"]),  # crashed grader
    lambda r: r["rows"][0].__setitem__("pass_rate", None),                     # missing graded value
])
def test_doctored_baseline_is_untrusted(base, tamper):
    bad = copy.deepcopy(base)
    tamper(bad)
    assert any(p.startswith("baseline") for p in _problems(base, bad))


# ---- integrity: one problem each ---------------------------------------------------------------
def _with(base, fn):
    rep = copy.deepcopy(base)
    fn(rep)
    return rep


@pytest.mark.parametrize("name,fn,needle", [
    ("duplicate seeds", lambda r: r["manifest"].__setitem__("seeds", [1, 1, 2]), "duplicate seeds"),
    ("stray seed", lambda r: r["rows"][-1].__setitem__("seed", 3), "seed(s) [3]"),
    ("duplicated row", lambda r: r["rows"].__setitem__(-1, dict(r["rows"][-2])), "duplicated"),
    ("missing row", lambda r: r["rows"].pop(), "rows, manifest says"),
    ("evaluator failure", lambda r: r.__setitem__("evaluator_failures", ["x"]), "evaluator failure"),
    ("no failures field", lambda r: r.pop("evaluator_failures"), "evaluator_failures"),
    ("string boolean", lambda r: r["rows"][0].__setitem__("pass_rate", "True"), "non-boolean"),
    ("int boolean", lambda r: r["rows"][0].__setitem__("pass_rate", 1), "non-boolean"),
    ("missing spec metric", lambda r: r["metrics"].pop("pass_rate"), "lacks spec metric"),
    ("inf metric", lambda r: r["metrics"].__setitem__("pass_5", float("inf")), "inf"),
    ("bool metric", lambda r: r["metrics"].__setitem__("pass_rate", True), "not a number"),
    ("nan cost", lambda r: r["rows"][0].__setitem__("cost", float("nan")), "invalid cost"),
    ("negative cost", lambda r: r["rows"][0].__setitem__("cost", -5.0), "invalid cost"),
    ("partial cost", lambda r: r["rows"][0].pop("cost"), "no cost while others do"),
    ("edited cost", lambda r: r["metrics"].__setitem__("cost_per_success", 0.5), "rows give"),
    ("no manifest", lambda r: r.pop("manifest"), "manifest missing"),
    ("bad slice tags", lambda r: r["rows"][0].__setitem__("slices", "money_movement"), "slice tags"),
])
def test_integrity_problem(base, name, fn, needle):
    problems = _problems(_with(base, fn), base)
    assert any(needle in p for p in problems), (name, problems)


def test_unit_fixture_without_costs_is_still_clean():
    # Like the public fixtures in test_lab9_advanced.py: no cost column, no cost metric in the spec.
    rows = [{k: v for k, v in r.items() if k != "cost"} for r in copy.deepcopy(ROWS)]
    spec = dict(copy.deepcopy(SPEC), metrics=SPEC["metrics"][:1])
    rep = _report(rows, metrics={"pass_rate": 0.875})
    assert _problems(rep, rep, spec, spec) == []
    rep["metrics"]["cost_per_success"] = 1.0           # a cost metric the rows cannot back
    assert any("carry no cost" in p for p in _problems(rep, rep, spec, spec))


# ---- ratchet -----------------------------------------------------------------------------------
def _spec(fn):
    s = copy.deepcopy(SPEC)
    fn(s)
    return s


@pytest.mark.parametrize("name,fn", [
    ("metric removed", lambda s: s["metrics"].pop(0)),
    ("slice removed", lambda s: s.__setitem__("slices", [])),
    ("metric unblocked", lambda s: s["metrics"][0].__setitem__("blocking", False)),
    ("slice unblocked", lambda s: s["slices"][0].pop("blocking")),
    ("no_significant_drop dropped", lambda s: s["metrics"][0]["threshold"].pop("no_significant_drop")),
    ("slice no_significant_drop dropped", lambda s: s["slices"][0].pop("no_significant_drop")),
    ("min lowered", lambda s: s["metrics"][0]["threshold"].__setitem__("min", 0.4)),
    ("slice min lowered", lambda s: s["slices"][0].__setitem__("min", 0.1)),
    ("max_increase raised", lambda s: s["metrics"][1]["threshold"].__setitem__("max_increase", 0.5)),
    ("threshold made non-numeric", lambda s: s["metrics"][0]["threshold"].__setitem__("min", "0.5")),
    ("confidence lowered", lambda s: s["bootstrap"].__setitem__("confidence", 0.8)),
    ("resamples lowered", lambda s: s["bootstrap"].__setitem__("n_resamples", 10)),
    ("bootstrap re-rolled", lambda s: s["bootstrap"].__setitem__("rng", 1)),
    ("seeds changed", lambda s: s.__setitem__("seeds", [1, 3])),
    ("duplicate metric shadows", lambda s: s["metrics"].append(
        {"id": "pass_rate", "blocking": False, "threshold": {"min": 0.0}})),
])
def test_ratchet_blocks_loosening(base, name, fn):
    assert any(p.startswith("RATCHET") for p in _problems(base, base, _spec(fn), SPEC)), name


def test_ratchet_allows_stricter(base):
    stricter = _spec(lambda s: (s["metrics"][0]["threshold"].__setitem__("min", 0.6),
                                s["bootstrap"].__setitem__("confidence", 0.99)))
    assert _problems(base, base, stricter, SPEC) == []


def test_version_bump_needs_new_data(tmp_path, base):
    a, b, c = tmp_path / "a.jsonl", tmp_path / "copy.jsonl", tmp_path / "new.jsonl"
    a.write_text('{"item_id": "E-1"}\n', encoding="utf-8")
    b.write_text('{"item_id": "E-1"}\n', encoding="utf-8")      # renamed copy, same content
    c.write_text('{"item_id": "E-2"}\n', encoding="utf-8")
    old = dict(copy.deepcopy(SPEC), eval_set=str(a))
    renamed = dict(copy.deepcopy(SPEC), eval_set=str(b), eval_set_version="v3")
    renamed["metrics"][0]["threshold"]["min"] = 0.0
    assert any("content did not" in p for p in _problems(base, base, renamed, old))
    real = dict(copy.deepcopy(SPEC), eval_set=str(c), eval_set_version="v3")
    assert _problems(base, base, real, old) == []
    silent = dict(copy.deepcopy(SPEC), eval_set=str(c))           # data changed, same version
    assert any("without an eval_set_version bump" in p for p in _problems(base, base, silent, old))


# ---- bootstrap confidence reaches both gates ---------------------------------------------------
def test_spec_confidence_reaches_global_and_slice_gate(base, monkeypatch):
    from lab9 import gate, stats

    seen = []
    real = stats.paired_bootstrap
    monkeypatch.setattr(stats, "paired_bootstrap", lambda *a, **kw: (seen.append(kw["confidence"]), real(*a, **kw))[1])
    spec = _spec(lambda s: s["bootstrap"].__setitem__("confidence", 0.9))
    gate.check(base, base, spec)
    assert len(seen) == 2 and set(seen) == {0.9}      # pass_rate (global) + money_movement (slice)


# ---- end to end: variants from lab9.variant_agent switches through the v2 spec -----------------
@pytest.fixture(scope="module")
def v2():
    from lab9.eval import load_spec

    if not Path("reports/baseline_v2.json").is_file():
        pytest.skip("baseline_v2.json not generated yet")
    return load_spec(Path("config/metric_spec_v2.yaml")), json.loads(Path("reports/baseline_v2.json").read_text("utf-8"))


@pytest.fixture
def shapes():
    """Register test shapes for one test only: lab8's contract test asserts SHAPES holds exactly
    the four Lab 8 shapes, so nothing registered here may outlive the test."""
    from lab8.shapes import SHAPES

    before = dict(SHAPES)
    yield SHAPES
    SHAPES.clear()
    SHAPES.update(before)


def _gate(v2, shapes, name, bugs):
    from lab9 import gate
    from lab9.eval import run_eval
    from lab9.variant_agent import make_shape

    shapes[name] = make_shape(name, frozenset(bugs))
    spec, base = v2
    rep = run_eval(name, spec)
    return rep, gate.check(rep, base, spec, spec)[0]


@pytest.mark.parametrize("bugs", [{"no_override", "misreport"},
                                  {"no_override", "misreport", "paraphrase"},
                                  {"no_override", "misreport", "rare"}])
def test_fm08_misreport_is_blocked_by_reply_matches_ledger(v2, shapes, bugs):
    rep, blocked = _gate(v2, shapes, "t_fm08_" + "_".join(sorted(bugs)), bugs)
    assert "reply_matches_ledger" in blocked
    assert rep["metrics"]["reply_matches_ledger"] < 1.0


@pytest.mark.parametrize("bugs", [{"no_override", "paraphrase"},
                                  {"no_override", "extra_lookup"},
                                  {"no_override", "paraphrase", "extra_lookup", "intermittent"}])
def test_honest_refactor_is_not_a_false_alarm(v2, shapes, bugs):
    rep, blocked = _gate(v2, shapes, "t_honest_" + "_".join(sorted(bugs)), bugs)
    assert blocked == [], blocked
    assert rep["metrics"]["reply_matches_ledger"] == 1.0
