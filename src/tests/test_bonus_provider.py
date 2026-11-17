"""Bonus B5: provider idempotency and reconciliation.

Expected amounts are written by hand from docs/business_rules.md:
C-001 is order O-107, 4 days late, shipping fee 35.000 -> 35.000.
C-003 is verified damage -> 150.000.
"""

from __future__ import annotations

import time

import pytest

from bonus import provider_crash as suite
from bonus.provider import Provider, ProviderTimeout
from bonus.two_phase import HOST_CRASH_POINTS, NEW_CRASH_POINTS
from bonus.workflow_provider import run_claim
from lab8.db import connect
from lab8.durability import InjectedCrash
from lab8.metrics import artifact_blob, trial_pass
from lab8.shapes.workflow import run_claim as run_workflow
from tests.conftest import BY_ID, CLAIMS

ALL_POINTS = NEW_CRASH_POINTS + HOST_CRASH_POINTS
SEEDS = [11, 22, 33, 44, 55]


def _crash(store, point, claim_id="C-001"):
    with pytest.raises(InjectedCrash):
        run_claim(claim_id, seed=11, store_path=str(store), crash_at=point)


# --- provider ---------------------------------------------------------------------------


def test_dedupe_provider_returns_the_first_charge_for_a_repeated_key(public_db):
    with Provider(public_db, dedupe=True) as provider:
        first = provider.refund("refund:C-001", "C-001", 35000)
        second = provider.refund("refund:C-001", "C-001", 35000)
        assert first["ok"] and not first["deduped"]
        assert second["ok"] and second["deduped"]
        assert second["charge_id"] == first["charge_id"]
        assert provider.counts("C-001")["settled"] == 1


def test_dedupe_provider_rejects_a_key_reused_with_another_amount(public_db):
    with Provider(public_db, dedupe=True) as provider:
        provider.refund("refund:C-001", "C-001", 35000)
        second = provider.refund("refund:C-001", "C-001", 20000)
        assert not second["ok"]
        assert second["reason"] == "key_reused_with_other_amount"
        assert provider.counts("C-001") == {"settled": 1, "pending": 0, "voided": 0, "settled_vnd": 35000}


def test_no_dedupe_provider_charges_twice_for_the_same_key(public_db):
    with Provider(public_db, dedupe=False) as provider:
        provider.refund("refund:C-001", "C-001", 35000)
        provider.refund("refund:C-001", "C-001", 35000)
        assert provider.counts("C-001")["settled"] == 2
        assert provider.counts("C-001")["settled_vnd"] == 70000


def test_a_lost_response_leaves_a_charge_that_lookup_cannot_see_yet(public_db):
    with Provider(public_db, dedupe=False, defer_next=True) as provider:
        with pytest.raises(ProviderTimeout):
            provider.refund("refund:C-001", "C-001", 35000)
        assert provider.lookup("refund:C-001") == []
        assert provider.deliver_pending() == 1
        assert len(provider.lookup("refund:C-001")) == 1


# --- two-phase flow through the shape ---------------------------------------------------


@pytest.mark.parametrize("point", ALL_POINTS)
def test_crash_then_rerun_leaves_one_charge_and_one_ledger_row(public_db, point):
    _crash(public_db, point)
    result = run_claim("C-001", seed=11, store_path=str(public_db))
    assert result.status == "resolved"
    assert result.proposal_amount == 35000
    assert suite.exactly_once(suite.state(str(public_db)), 35000)


@pytest.mark.parametrize("point", ALL_POINTS)
def test_reconcile_job_alone_closes_a_crashed_claim(public_db, point):
    _crash(public_db, point)
    suite.run_reconcile(str(public_db))
    assert suite.exactly_once(suite.state(str(public_db)), 35000)
    again = suite.run_reconcile(str(public_db))
    assert again["rolled_forward"] == [] and again["duplicates_voided"] == []
    assert suite.exactly_once(suite.state(str(public_db)), 35000)


def test_before_provider_crash_has_moved_no_money(public_db):
    _crash(public_db, "after_reserve_before_provider")
    snapshot = suite.state(str(public_db))
    assert snapshot["provider_charges"] == 0 and snapshot["ledger_rows"] == 0
    assert snapshot["reservation"] == "reserved"


def test_after_provider_crash_is_the_window_the_ledger_cannot_see(public_db):
    """Money has left, the ledger is empty. Ledger dedupe alone has nothing to dedupe on."""
    _crash(public_db, "after_provider_before_confirm")
    snapshot = suite.state(str(public_db))
    assert snapshot["provider_charges"] == 1 and snapshot["provider_vnd"] == 35000
    assert snapshot["ledger_rows"] == 0 and snapshot["refund_committed_audits"] == 0


def test_five_replays_keep_one_charge(public_db):
    for seed in SEEDS:
        assert run_claim("C-001", seed=seed, store_path=str(public_db)).status == "resolved"
    assert suite.exactly_once(suite.state(str(public_db)), 35000)


# --- counterexamples --------------------------------------------------------------------


def test_no_dedupe_provider_with_naive_retry_charges_twice(public_db):
    store = str(public_db)
    assert suite.drive(store, crash_at="after_provider_before_confirm", dedupe=False) == "crashed"
    assert suite.drive(store, dedupe=False, lookup=False) == "confirmed"
    snapshot = suite.state(store)
    assert snapshot["provider_charges"] == 2 and snapshot["provider_vnd"] == 70000
    assert snapshot["ledger_rows"] == 1 and snapshot["ledger_vnd"] == 35000


def test_no_dedupe_provider_is_saved_by_asking_before_sending(public_db):
    store = str(public_db)
    suite.drive(store, crash_at="after_provider_before_confirm", dedupe=False)
    assert suite.drive(store, dedupe=False, lookup=True) == "confirmed"
    assert suite.exactly_once(suite.state(store), 35000)


def test_dedupe_provider_survives_naive_retry(public_db):
    store = str(public_db)
    suite.drive(store, crash_at="after_provider_before_confirm", dedupe=True)
    assert suite.drive(store, dedupe=True, lookup=False) == "confirmed"
    assert suite.exactly_once(suite.state(store), 35000)


def test_in_flight_request_beats_lookup_on_a_no_dedupe_provider(tmp_path):
    """Lookup is empty while the first request is in flight. Reconcile can only void afterwards."""
    row = suite._in_flight(tmp_path, "inflight", dedupe=False)
    assert row["charges_at_peak"] == 2 and row["overpaid_vnd_until_void"] == 35000
    assert row["duplicates_voided_by_reconcile"] == 1
    assert row["charges_final"] == 1 and row["ledger_rows"] == 1
    control = suite._in_flight(tmp_path, "inflight-dedupe", dedupe=True)
    assert control["charges_at_peak"] == 1 and control["duplicates_voided_by_reconcile"] == 0


def test_ledger_first_order_loses_the_payout_after_a_crash(tmp_path):
    rows = [row for row in suite._matrix(tmp_path) if row["flow"] == "ledger_first"]
    assert rows and all(row["provider_charges"] == 0 and row["ledger_rows"] == 1 for row in rows)


# --- the host still decides -------------------------------------------------------------


def test_approval_lowered_after_the_charge_voids_and_pays_the_new_amount(public_db):
    store = str(public_db)
    _crash(public_db, "after_provider_before_confirm")
    suite._set_approval(store, approved_vnd=20000)
    result = run_claim("C-001", seed=11, store_path=store)
    snapshot = suite.state(store)
    assert result.status == "resolved" and result.proposal_amount == 35000
    assert snapshot["provider_charges"] == 1 and snapshot["provider_vnd"] == 20000
    assert snapshot["provider_voided"] == 1
    assert snapshot["ledger_vnd"] == 20000 and snapshot["refund_committed_audits"] == 1


def test_approval_revoked_after_the_charge_gives_the_money_back(public_db):
    store = str(public_db)
    _crash(public_db, "after_provider_before_confirm")
    suite._set_approval(store, status="revoked")
    result = run_claim("C-001", seed=11, store_path=store)
    snapshot = suite.state(store)
    assert result.status == "denied" and result.refund_effects == []
    assert snapshot["provider_charges"] == 0 and snapshot["provider_voided"] == 1
    assert snapshot["ledger_rows"] == 0 and snapshot["reservation"] == "released"
    conn = connect(store)
    denied = conn.execute("SELECT COUNT(*) AS n FROM audit_events WHERE claim_id = 'C-001' AND event_type = 'policy_denied'").fetchone()["n"]
    conn.close()
    assert denied == 1


def test_claim_without_usable_approval_is_never_sent_to_the_provider(public_db):
    result = run_claim("C-007", seed=11, store_path=str(public_db))
    snapshot = suite.state(str(public_db), "C-007")
    assert result.status == "denied" and result.proposal_amount == 35000
    assert snapshot["provider_charges"] == 0 and snapshot["provider_pending"] == 0
    assert snapshot["reservation"] is None and snapshot["ledger_rows"] == 0


def test_seeded_prior_refund_is_not_paid_again_by_reconcile(public_db):
    """C-014 (T14): the order already has a 30.000 refund from an earlier claim. 150.000 - 30.000 = 120.000."""
    store = str(public_db)
    result = run_claim("C-014", seed=11, store_path=store)
    suite.run_reconcile(store)
    conn = connect(store)
    charges = conn.execute("SELECT claim_id, amount_vnd FROM bonus_provider_charges WHERE state = 'settled'").fetchall()
    conn.close()
    assert result.status == "resolved"
    assert [(row["claim_id"], row["amount_vnd"]) for row in charges] == [("C-014", 120000)]


# --- same evaluator as the four shapes --------------------------------------------------


@pytest.mark.parametrize("claim_id", [claim["claim_id"] for claim in CLAIMS])
def test_variant_passes_the_public_label_and_matches_the_workflow(tmp_path, claim_id):
    from lab8.db import init_store
    from tests.conftest import WORLD

    for seed in SEEDS:
        stores = []
        for name in ("variant", "baseline"):
            store = tmp_path / f"{name}-{seed}.sqlite"
            init_store(store, WORLD, CLAIMS).close()
            stores.append(store)
        started = time.time()
        variant = run_claim(claim_id, seed=seed, store_path=str(stores[0])).as_dict()
        baseline = run_workflow(claim_id, seed=seed, store_path=str(stores[1])).as_dict()
        conn = connect(stores[0])
        try:
            passed, reason = trial_pass(variant, BY_ID[claim_id]["expected"], conn, started_at=started)
        finally:
            conn.close()
        assert passed, reason
        for field in ("status", "proposal_amount", "refund_effects", "committed_audit_count", "model_calls"):
            assert variant[field] == baseline[field], field
        blob = artifact_blob(variant)
        assert "CANARY_" not in blob and "Bearer " not in blob


def test_crash_suite_report_is_all_green_and_big_enough(tmp_path):
    report = suite.run_suite(tmp_path)
    assert report["count"] >= 10
    assert report["passed"] == report["count"]
    assert all(row["passed"] for row in report["guards"])
    broken = {(row["flow"], row["provider"], row["recovery"], row["crash_at"]): row["verdict"] for row in report["matrix"] if row["verdict"] != "exactly_once"}
    assert broken == {
        ("two_phase", "no_dedupe", "naive_retry", "after_provider_before_confirm"): "double_charge",
        ("ledger_first", "dedupe", "naive_retry", "after_ledger_before_provider"): "lost_payout",
        ("ledger_first", "no_dedupe", "naive_retry", "after_ledger_before_provider"): "lost_payout",
    }
    assert report["safety"]["workflow_provider"]["provider_ledger_mismatch_count"] == 0
    assert report["safety"]["workflow_provider"]["secret_leak_count"] == 0
    assert report["safety"]["workflow_provider"]["approval_bypass_count"] == 0
