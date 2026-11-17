"""Regression tests from docs/trace_case.md: a real trial, its first wrong step, and the control."""
import pytest

pytestmark = pytest.mark.student


@pytest.fixture(autouse=True)
def _restore_shapes():
    """investigate() registers lab9 variants into lab8's global SHAPES; undo it so Lab 8's
    contract tests (exactly four shapes) still pass in the same pytest process."""
    from lab8.shapes import SHAPES

    saved = dict(SHAPES)
    yield
    SHAPES.clear()
    SHAPES.update(saved)


def _ctx(trial, label):
    from pydantic_evals.evaluators import EvaluatorContext

    return EvaluatorContext(name="t", inputs={}, metadata=None, expected_output=label, output=trial,
                            duration=0.0, _span_tree=None, attributes={}, metrics={})


def _kinds(trial):
    return [e.get("kind") for e in trial["events"]]


def test_c002_ticket_override_is_first_wrong_step():
    from lab9.graders import DbFactKept, RefundAmountCorrect
    from lab9.trace_case import investigate

    bad = investigate("seeded_regression", "C-002", 11)
    kinds = _kinds(bad["trial"])
    # FM-03: the override comes right after the investigator read the DB (delay_days=2).
    assert kinds.index("model_override") == kinds.index("investigator") + 1
    assert bad["trial"]["events"][kinds.index("model_override")]["payload"] == {"delay_days": 5}
    assert DbFactKept().evaluate(_ctx(bad["trial"], bad["label"])).value is False
    assert RefundAmountCorrect().evaluate(_ctx(bad["trial"], bad["label"])).value is False
    assert bad["trial"]["snapshot"]["refund_vnd"] == 20000

    good = investigate("fixed", "C-002", 55)   # the seed where the reference swarm trusts the ticket
    assert "model_override" not in _kinds(good["trial"])
    assert DbFactKept().evaluate(_ctx(good["trial"], good["label"])).value is True
    assert good["trial"]["snapshot"]["refund_vnd"] == 0 and good["trial"]["lab8_pass"]


def test_c008_misreport_caught_only_by_reply_metric():
    """FM-08: the ledger is right (approval caps at 20k) but the reply says 35k. Lab 8's grader passes it."""
    from lab8.shapes import SHAPES
    from lab9.graders import ReplyMatchesLedger
    from lab9.trace_case import investigate
    from lab9.variant_agent import make_shape

    SHAPES.setdefault("test_misreport", make_shape("test_misreport", frozenset({"no_override", "misreport"})))
    bad = investigate("test_misreport", "C-008", 11)
    assert bad["trial"]["lab8_pass"] is True
    assert bad["trial"]["snapshot"]["refund_vnd"] == 20000
    assert ReplyMatchesLedger().evaluate(_ctx(bad["trial"], bad["label"])).value is False

    good = investigate("fixed", "C-008", 11)
    assert ReplyMatchesLedger().evaluate(_ctx(good["trial"], good["label"])).value is True
