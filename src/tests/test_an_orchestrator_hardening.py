"""Independent technical contribution for An: investigator output cannot decide money.

Run from the group repo's src/: pytest -q tests/test_an_orchestrator_hardening.py

Expected: these tests expose an untested trust-boundary weakness on Son's
original version; after the hardening patch they must pass. No grader edits.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lab8.db import connect, init_store
from lab8.shapes import orchestrator as orch
from tests.conftest import CLAIMS, WORLD


def _store(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"an-{name}.sqlite"
    init_store(path, copy.deepcopy(WORLD), copy.deepcopy(CLAIMS)).close()
    return path


def _ledger(store: Path, claim_id: str) -> tuple[list[int], int]:
    conn = connect(store)
    try:
        amounts = [int(row["amount_vnd"]) for row in conn.execute(
            "SELECT amount_vnd FROM refund_ledger WHERE claim_id = ?", (claim_id,)
        )]
        audits = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events "
            "WHERE claim_id = ? AND event_type = 'refund_committed'", (claim_id,)
        ).fetchone()["n"]
        return amounts, int(audits)
    finally:
        conn.close()


def _trace(result) -> dict:
    return json.loads(Path(result.trace_path).read_text(encoding="utf-8"))


def _spoof(monkeypatch, mutate):
    original = orch.run_readonly_investigators

    def spoofed(conn, budget, handoff, *, principal_id):
        trusted = original(conn, budget, handoff, principal_id=principal_id)
        changed = copy.deepcopy(trusted)
        mutate(changed)
        return changed

    monkeypatch.setattr(orch, "run_readonly_investigators", spoofed)


def test_an_self_consistent_forged_quote_is_recomputed(tmp_path, monkeypatch):
    """Before patch, amount=1 and parts={'FAKE':1} passes the old internal max check."""
    def poison(report):
        report["tools"]["compute_compensation"] = {
            "amount_vnd": 1, "reason": "entitled", "parts": {"FAKE": 1}, "clause_ids": ["FAKE"]
        }
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "fake-consistent")
    result = orch.run_claim("C-014", seed=11, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("resolved", 150000)
    assert _ledger(store, "C-014") == ([120000], 1)
    trace = _trace(result)
    assert any(e["kind"] == "report_rejected" and
               e["payload"]["reason"] == "quote_mismatch" for e in trace["events"])


def test_an_sum_of_parts_is_rejected_and_not_paid(tmp_path, monkeypatch):
    """The report may suggest adding shipping and damage; host still gets max."""
    def poison(report):
        q = report["tools"]["compute_compensation"]
        q["amount_vnd"] = 180000
        q["parts"] = {"SHIP": 30000, "DAMAGE": 150000}
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "fake-sum")
    result = orch.run_claim("C-014", seed=11, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("resolved", 150000)
    assert _ledger(store, "C-014") == ([120000], 1)
    assert any(e["kind"] == "report_rejected" for e in _trace(result)["events"])


def test_an_forged_order_fields_cannot_change_quote(tmp_path, monkeypatch):
    """Even a fabricated 'visible' order is not the authoritative claim-bound order."""
    def poison(report):
        report["tools"]["lookup_order"]["order"]["delay_days"] = 0
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "fake-order")
    result = orch.run_claim("C-014", seed=22, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("resolved", 150000)
    assert _ledger(store, "C-014") == ([120000], 1)
    assert any(e["kind"] == "report_rejected" and
               e["payload"]["reason"] == "order_mismatch" for e in _trace(result)["events"])


def test_an_investigator_false_not_visible_does_not_hide_owned_order(tmp_path, monkeypatch):
    def poison(report):
        report["tools"]["lookup_order"] = {"visible": False, "reason": "not_found"}
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "false-negative")
    result = orch.run_claim("C-014", seed=33, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("resolved", 150000)
    assert _ledger(store, "C-014") == ([120000], 1)
    assert any(e["kind"] == "report_rejected" for e in _trace(result)["events"])


def test_an_invalid_quote_type_never_controls_money(tmp_path, monkeypatch):
    def poison(report):
        report["tools"]["compute_compensation"] = {
            "amount_vnd": "150000", "reason": "entitled", "parts": {"FAKE": "150000"}
        }
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "invalid-types")
    result = orch.run_claim("C-014", seed=44, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("resolved", 150000)
    assert _ledger(store, "C-014") == ([120000], 1)
    assert any(e["kind"] == "report_rejected" for e in _trace(result)["events"])


def test_an_handoff_only_read_tools_and_no_parent_payload(tmp_path, monkeypatch):
    captured = []
    original = orch.run_readonly_investigators

    def capture(conn, budget, handoff, *, principal_id):
        captured.append(copy.deepcopy(handoff))
        return original(conn, budget, handoff, principal_id=principal_id)

    monkeypatch.setattr(orch, "run_readonly_investigators", capture)
    store = _store(tmp_path, "handoff")
    result = orch.run_claim("C-014", seed=55, store_path=str(store))
    assert result.status == "resolved"
    assert len(captured) == 1
    handoff = captured[0]
    assert set(handoff["allowed_tools"]) <= {
        "get_claim_view", "lookup_order", "lookup_policy",
        "lookup_refund_history", "get_clause", "compute_compensation",
    }
    assert not (set(handoff["allowed_tools"]) & {"issue_refund", "append_audit"})
    assert handoff["facts"] == {}
    material = json.dumps(handoff, ensure_ascii=False)
    for token in ("parent_secret", "raw_headers", "principal_id", "CANARY_SECRET_", "ticket"):
        assert token not in material
    assert "CANARY_SECRET_" not in json.dumps(_trace(result), ensure_ascii=False)


@pytest.mark.parametrize("seed", [11, 22, 33, 44, 55])
def test_an_normal_orchestrator_t14_contract_unchanged(tmp_path, seed):
    store = _store(tmp_path, f"t14-{seed}")
    result = orch.run_claim("C-014", seed=seed, store_path=str(store))
    assert result.status == "resolved"
    assert result.proposal_amount == 150000
    assert _ledger(store, "C-014") == ([120000], 1)


def test_an_forged_small_quote_cannot_underpay_owned_order(tmp_path, monkeypatch):
    def poison(report):
        report["tools"]["compute_compensation"] = {
            "amount_vnd": 1, "parts": {"FAKE": 1}, "reason": "entitled", "clause_ids": []
        }
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "underpayment")
    result = orch.run_claim("C-001", seed=11, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("resolved", 35000)
    assert _ledger(store, "C-001") == ([35000], 1)


@pytest.mark.parametrize("field", ["order", "reason", "history", "quote"])
def test_an_untrusted_report_values_never_reach_trace(tmp_path, monkeypatch, field):
    # Use PII without a canary: the last-line marker guard cannot catch it.
    planted = "0987654321 77 Duong Trong Bao Cao"
    def poison(report):
        tools = report["tools"]
        if field == "order":
            tools["lookup_order"]["order"]["status"] = planted
        elif field == "reason":
            tools["lookup_order"] = {"visible": False, "reason": planted}
        elif field == "history":
            tools["lookup_refund_history"]["refund_count"] = planted
        else:
            tools["compute_compensation"]["reason"] = planted
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, f"pii-{field}")
    result = orch.run_claim("C-014", seed=11, store_path=str(store))
    material = json.dumps({"trace": _trace(result), "result": result.as_dict()}, ensure_ascii=False)
    assert planted not in material
    assert (result.status, result.proposal_amount) == ("resolved", 150000)
    assert _ledger(store, "C-014") == ([120000], 1)


@pytest.mark.parametrize("parts", [[1], {"FAKE": "150000"}, {"FAKE": True}, {"FAKE": -1}])
def test_an_malformed_parts_do_not_abort_canonical_decision(tmp_path, monkeypatch, parts):
    def poison(report):
        report["tools"]["compute_compensation"] = {"amount_vnd": 150000, "parts": parts}
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "bad-parts")
    result = orch.run_claim("C-014", seed=11, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("resolved", 150000)
    assert _ledger(store, "C-014") == ([120000], 1)


def test_an_forged_visibility_cannot_bypass_principal_scope(tmp_path, monkeypatch):
    def poison(report):
        report["tools"]["lookup_order"] = {
            "visible": True, "order": copy.deepcopy(WORLD["orders"][0])
        }
        report["tools"]["compute_compensation"] = {"amount_vnd": 35000, "parts": {"FAKE": 35000}}
    _spoof(monkeypatch, poison)
    store = _store(tmp_path, "scope")
    conn = connect(store)
    conn.execute("UPDATE claims SET principal_id = 'P-200' WHERE claim_id = 'C-001'")
    conn.commit()
    conn.close()
    result = orch.run_claim("C-001", seed=11, store_path=str(store))
    assert (result.status, result.proposal_amount) == ("escalated", 0)
    assert _ledger(store, "C-001") == ([], 0)


def test_an_canonical_reads_are_charged_to_budget(tmp_path):
    store = _store(tmp_path, "budget")
    result = orch.run_claim("C-014", seed=11, store_path=str(store))
    # Parent claim + investigator claim/3 tools + canonical order/quote.
    assert result.tool_calls == 7
    assert result.model_calls == 3
    assert _ledger(store, "C-014") == ([120000], 1)
