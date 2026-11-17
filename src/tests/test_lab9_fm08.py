"""FM-08 (reply vs ledger) and stats input validation, on hand-built inputs (no shape run).
Run: python -m pytest -q -m student -o addopts= -k lab9"""
import math

import pytest

pytestmark = pytest.mark.student


@pytest.fixture(scope="module")
def graders():
    from lab9 import graders

    return graders


@pytest.fixture(scope="module")
def stats():
    from lab9 import stats

    return stats


def ctx(trial, label=None):
    from pydantic_evals.evaluators import EvaluatorContext

    return EvaluatorContext(name="t", inputs={}, metadata=None, expected_output=label or {}, output=trial,
                            duration=0.0, _span_tree=None, attributes={}, metrics={})


def trial(ledger, *replies, other_events=({"kind": "quote", "payload": {"amount_vnd": 35000}},)):
    events = [*other_events, *({"kind": "reply", "payload": {"text": t}} for t in replies)]
    return {"snapshot": {"refund_vnd": ledger, "refund_count": int(ledger > 0)}, "events": events,
            "unexpected_rows": 0, "blob": "", "result": {}, "lab8_pass": True, "lab8_reason": None}


def judge(graders, t):
    return graders.ReplyMatchesLedger().evaluate(ctx(t)).value


# ---- amount formats --------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("Dạ, LogiX đã hoàn 35.000đ cho đơn O-107.", 35000),
    ("LogiX đã hoàn phí 120.000đ cho đơn O-515 của anh/chị ạ.", 120000),
    ("Đã hoàn 35,000 VND", 35000),
    ("Đã hoàn 35000 đồng", 35000),
    ("Đã hoàn 35.000 VNĐ", 35000),
    ("Đã hoàn 35.000 ₫", 35000),
    ("Refunded VND 35,000", 35000),
    ("Đã hoàn ₫35.000", 35000),
    ("Đã hoàn 35k", 35000),
    ("Đã hoàn 35 nghìn đồng", 35000),
    ("Đã hoàn 1,5 triệu", 1_500_000),
    ("Đã hoàn 1.500 nghìn", 1_500_000),
    ("Đã hoàn 1.250.000đ", 1_250_000),
    ("Đã hoàn 35.000,00đ", 35000),
    ("Đã hoàn 35 000 đ", 35000),
])
def test_amount_formats(graders, text, want):
    assert graders.classify_amounts(text) == [(want, "refund")]


@pytest.mark.parametrize("text", [
    "Dạ, đơn O-202 chưa đủ điều kiện hoàn theo chính sách hiện hành.",
    "Tiền về trong 3–5 ngày cho đơn O-515.",
    "Mã yêu cầu C-101, 5 đơn đang xử lý, mã O-35k.",
    "Dạ, anh/chị vui lòng cho LogiX xin mã đơn hàng để kiểm tra ạ.",
])
def test_text_without_money_has_no_amount(graders, text):
    assert graders.classify_amounts(text) == []


# ---- refunded amount vs requested / order value / cap -----------------------------------------
@pytest.mark.parametrize("text,roles", [
    ("Anh yêu cầu hoàn 50.000đ, LogiX đã hoàn 35.000đ.", [(50000, "other"), (35000, "refund")]),
    ("Theo yêu cầu của anh LogiX đã hoàn 35.000đ.", [(35000, "refund")]),
    ("LogiX đã hoàn 35.000đ (hạn mức tối đa 50.000đ).", [(35000, "refund"), (50000, "other")]),
    ("Đơn trị giá 200.000đ đã được hoàn 35.000đ.", [(200000, "other"), (35000, "refund")]),
    ("LogiX chưa hoàn 35.000đ.", [(35000, "negated")]),
    ("LogiX không thể hoàn 50.000đ theo chính sách.", [(50000, "negated")]),
])
def test_amount_roles(graders, text, roles):
    assert graders.classify_amounts(text) == roles


# ---- the grader: positives (pass) --------------------------------------------------------------
@pytest.mark.parametrize("ledger,replies", [
    (35000, ["Dạ, LogiX đã hoàn 35.000đ cho đơn O-107."]),
    (35000, ["LogiX đã hoàn phí 35,000 VND cho đơn O-107 của anh/chị ạ."]),           # honest paraphrase
    (35000, ["Anh yêu cầu hoàn 50.000đ, LogiX đã hoàn 35.000đ."]),                   # requested != refunded
    (35000, ["LogiX đã hoàn 35.000đ (hạn mức tối đa 50.000đ)."]),                     # cap is context
    (35000, ["Đơn trị giá 200.000đ đã được hoàn 35.000đ."]),                          # order value is context
    (0, ["Dạ, đơn O-202 chưa đủ điều kiện hoàn theo chính sách hiện hành."]),        # denial, no amount
    (0, ["LogiX không thể hoàn 50.000đ theo chính sách."]),                          # nothing moved, says so
    (35000, ["Dạ, yêu cầu đã được xử lý."]),                                          # reply states no amount
    (35000, ["Dạ, LogiX đã hoàn 35.000đ.", "Tiền về trong 3–5 ngày ạ."]),             # several reply events
])
def test_reply_consistent_with_ledger(graders, ledger, replies):
    assert judge(graders, trial(ledger, *replies)) is True


# ---- the grader: negatives (FM-08) -------------------------------------------------------------
@pytest.mark.parametrize("ledger,replies", [
    (20000, ["Dạ, LogiX đã hoàn 35.000đ cho đơn O-808."]),                  # proposal told, cap committed
    (0, ["Dạ, LogiX đã hoàn 35.000đ cho đơn O-202."]),                      # host denied, reply says refunded
    (35000, ["Dạ, LogiX đã hoàn 30k cho đơn O-107."]),                      # rounding, other format
    (35000, ["Anh yêu cầu hoàn 35.000đ, LogiX đã hoàn 50.000đ."]),          # refunded amount wrong
    (35000, ["LogiX chưa hoàn 35.000đ."]),                                  # told NOT refunded, but it was
    (35000, ["LogiX không thể hoàn 50.000đ."]),                             # told nothing moved, 35k moved
    (35000, ["Dạ, LogiX đã hoàn 35.000đ.", "Bổ sung: LogiX đã hoàn 70.000đ."]),  # one wrong reply is enough
])
def test_reply_misreports_ledger(graders, ledger, replies):
    assert judge(graders, trial(ledger, *replies)) is False


# ---- missing evidence vs nothing told ----------------------------------------------------------
def test_no_reply_event_is_nothing_told(graders):
    # The Lab 8 swarm emits no reply events: there is nothing to misreport.
    res = graders.ReplyMatchesLedger().evaluate(ctx(trial(35000)))
    assert res.value is True and "no reply" in res.reason


@pytest.mark.parametrize("broken", [
    {"snapshot": {"refund_vnd": 35000}, "events": []},          # trace empty / unreadable
    {"snapshot": {"refund_vnd": 35000}},                        # no trace at all
    {"events": [{"kind": "reply", "payload": {"text": "Đã hoàn 35.000đ"}}]},   # no ledger snapshot
    {"snapshot": None, "events": [{"kind": "reply", "payload": {"text": "x"}}]},
    {"snapshot": {"refund_vnd": "n/a"}, "events": [{"kind": "reply", "payload": {"text": "x"}}]},
])
def test_missing_evidence_is_not_a_pass(graders, broken):
    res = graders.ReplyMatchesLedger().evaluate(ctx(broken))
    assert res.value is False and res.reason


# ---- stats input validation --------------------------------------------------------------------
@pytest.mark.parametrize("human,judge_labels", [
    (["FAIL", "PASS"], ["FAIL"]),            # length mismatch
    (["FAIL", ""], ["FAIL", "PASS"]),        # blank label
    (["FAIL", "N/A"], ["FAIL", "PASS"]),     # unknown label
    (["FAIL", None], ["FAIL", "PASS"]),
    ("FAIL", "PASS"),                        # strings are not label sequences
])
def test_confusion_rejects_bad_labels(stats, human, judge_labels):
    with pytest.raises(ValueError):
        stats.confusion(human, judge_labels)


def test_confusion_normalises_case_and_space(stats):
    assert stats.confusion([" fail", "Pass "], ["FAIL", "fail"]) == {"tp": 1, "fp": 1, "fn": 0, "tn": 0}


@pytest.mark.parametrize("args", [(1.2, 0.8, 0.9), (-0.1, 0.8, 0.9), (0.3, float("nan"), 0.9),
                                  (0.3, 0.8, True), (0.3, "0.8", 0.9), (0.3, 0.5, 0.5)])
def test_rogan_gladen_rejects_bad_inputs(stats, args):
    with pytest.raises(ValueError):
        stats.rogan_gladen(*args)


@pytest.mark.parametrize("a,b,kw", [
    ([1, 0], [1], {}), ([], [], {}), ([1, float("nan")], [1, 0], {}), ([1, None], [1, 0], {}),
    ([1, 0], [0, 1], {"confidence": 1.0}), ([1, 0], [0, 1], {"confidence": 0}),
    ([1, 0], [0, 1], {"n_resamples": 0}), ([1, 0], [0, 1], {"rng": "x"}), ("10", "01", {}),
])
def test_paired_bootstrap_rejects_bad_inputs(stats, a, b, kw):
    with pytest.raises(ValueError):
        stats.paired_bootstrap(a, b, **kw)


def test_paired_bootstrap_confidence_widens_the_ci(stats):
    a, b = [1, 1, 0.8, 1, 0.6], [1, 0.8, 0.8, 0.6, 0.6]
    _, lo90, hi90 = stats.paired_bootstrap(a, b, confidence=0.90)
    _, lo99, hi99 = stats.paired_bootstrap(a, b, confidence=0.99)
    assert lo99 <= lo90 and hi99 >= hi90 and math.isfinite(lo99)
