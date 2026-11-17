"""Ledger + Evaluation: branches and evidence the earlier tests did not cover.

Ledger
- Another writer commits a refund on the same order between the host's first
  read and its write transaction (`order_total_changed` re-check in
  durability.issue_refund): the host retries once with the new total, and fails
  closed if the total moves again.
- Crash at every host point on the larger amounts: verified damage (C-003,
  150.000) and an order with a refund paid before the run (C-014, 150.000 -
  30.000 = 120.000). The seeded prior refund must stay untouched.

Evaluation
- The committed report files are what the commands in src/reports/README.md
  produce today (no hand edits, no drift after code changes).
- Ablation variants write their own trace and do not overwrite the orchestrator's.

Expected amounts are written by hand from docs/business_rules.md.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import lab8.durability as durability
from lab8.app import register_extra_shapes
from lab8.benchmark import run_benchmark
from lab8.config_loader import repo_path
from lab8.crash_test import run_suite as run_crash_suite
from lab8.db import connect, init_store
from lab8.durability import InjectedCrash, committed_audit_count, get_refund, issue_refund
from lab8.metrics import check_trace
from lab8.shapes import SHAPES
from tests.conftest import CLAIMS, WORLD

TEAM_SHAPES = ["workflow", "single", "orchestrator"]
CRASH_POINTS = ["before_effect", "after_effect_before_checkpoint", "after_checkpoint"]
SEEDS = [11, 22, 33, 44, 55]
REPORTS = repo_path("reports")


def _order_rows(store, order_id: str) -> list[tuple[str, int]]:
    conn = connect(store)
    try:
        rows = conn.execute(
            "SELECT claim_id, amount_vnd FROM refund_ledger WHERE order_id = ? ORDER BY claim_id", (order_id,)
        ).fetchall()
        return [(row["claim_id"], int(row["amount_vnd"])) for row in rows]
    finally:
        conn.close()


def _denied_reasons(store, claim_id: str) -> list[str]:
    conn = connect(store)
    try:
        rows = conn.execute(
            "SELECT details_json FROM audit_events WHERE claim_id = ? AND event_type = 'policy_denied'", (claim_id,)
        ).fetchall()
        return sorted(json.loads(row["details_json"])["reason"] for row in rows)
    finally:
        conn.close()


def _other_writer(store, writes: list[tuple[str, int]]):
    """Patch the host's first read of the order total so that another writer commits right after it.

    Each call to the host's read takes the next pending write, commits it on a
    separate connection, and returns the total as it was *before* that write,
    which is the race the re-check inside BEGIN IMMEDIATE exists for.
    """
    real = durability.order_refunded_vnd
    pending = list(writes)
    reads = {"n": 0}

    def racing(conn, order_id, *, exclude_claim_id=None):
        reads["n"] += 1
        before = real(conn, order_id, exclude_claim_id=exclude_claim_id)
        # Odd reads are the pre-check; even reads are the re-check inside the transaction.
        if pending and reads["n"] % 2 == 1:
            claim_id, amount = pending.pop(0)
            other = connect(store)
            other.execute(
                "INSERT INTO refund_ledger (claim_id, order_id, idempotency_key, amount_vnd, status) VALUES (?, ?, ?, ?, 'committed')",
                (claim_id, order_id, f"refund:{claim_id}", amount),
            )
            other.commit()
            other.close()
        return before

    return racing


# --- Ledger: a concurrent writer on the same order ---------------------------------------


def test_refund_committed_by_another_claim_mid_write_is_seen_on_retry(public_db, monkeypatch):
    # C-010 and C-001 both point at O-107 (fee 35.000). C-001 pays while C-010 is between read and write.
    monkeypatch.setattr(durability, "order_refunded_vnd", _other_writer(public_db, [("C-001", 35000)]))
    conn = connect(public_db)
    outcome = issue_refund(conn, "C-010", 35000)
    conn.close()
    assert (outcome["ok"], outcome["reason"]) == (False, "order_already_refunded")
    assert _order_rows(public_db, "O-107") == [("C-001", 35000)]
    assert _denied_reasons(public_db, "C-010") == ["order_already_refunded"]


def test_partial_refund_mid_write_makes_the_retry_pay_only_the_rest(public_db, monkeypatch):
    # Another claim takes 10.000 of O-107's 35.000 mid-write. The retry pays 25.000, not 35.000.
    monkeypatch.setattr(durability, "order_refunded_vnd", _other_writer(public_db, [("C-001", 10000)]))
    conn = connect(public_db)
    outcome = issue_refund(conn, "C-010", 35000)
    conn.close()
    assert (outcome["ok"], outcome["reason"]) == (True, "committed")
    assert _order_rows(public_db, "O-107") == [("C-001", 10000), ("C-010", 25000)]
    assert sum(amount for _, amount in _order_rows(public_db, "O-107")) <= 35000


def test_order_total_moving_twice_fails_closed_without_paying(public_db, monkeypatch):
    # The total changes on the first attempt and again on the retry: the host gives up, writes nothing.
    racing = _other_writer(public_db, [("C-001", 10000), ("C-011", 10000)])
    monkeypatch.setattr(durability, "order_refunded_vnd", racing)
    conn = connect(public_db)
    outcome = issue_refund(conn, "C-010", 35000)
    conn.close()
    assert (outcome["ok"], outcome["reason"]) == (False, "order_total_changed")
    assert _order_rows(public_db, "O-107") == [("C-001", 10000), ("C-011", 10000)]
    assert _denied_reasons(public_db, "C-010") == ["order_total_changed"]


# --- Ledger: crash on the larger amounts -------------------------------------------------


@pytest.mark.parametrize("shape", TEAM_SHAPES)
@pytest.mark.parametrize("point", CRASH_POINTS)
@pytest.mark.parametrize(
    ("claim_id", "order_id", "amount", "prior"),
    [
        ("C-003", "O-303", 150000, []),  # verified damage
        ("C-014", "O-515", 120000, [("C-014-PRIOR", 30000)]),  # damage minus a refund paid before the run
    ],
)
def test_crash_then_resume_on_large_and_capped_refunds(tmp_path, shape, point, claim_id, order_id, amount, prior):
    store = tmp_path / f"{shape}-{claim_id}-{point}.sqlite"
    init_store(store, WORLD, CLAIMS).close()
    prior = _order_rows(store, order_id)  # the seeded row, read from the store itself

    with pytest.raises(InjectedCrash):
        SHAPES[shape](claim_id, seed=11, store_path=str(store), crash_at=point)
    started = time.time()
    result = SHAPES[shape](claim_id, seed=11, store_path=str(store))

    assert result.status == "resolved"
    assert [int(effect["amount_vnd"]) for effect in result.refund_effects] == [amount]
    assert result.committed_audit_count == 1
    assert _order_rows(store, order_id) == sorted(prior + [(claim_id, amount)])
    assert check_trace(result.as_dict(), started_at=started) == (True, "ok")


def test_c014_prior_refund_is_the_one_seeded_by_the_world(public_db):
    """Guards the test above: O-515 starts with exactly one earlier refund of 30.000."""
    rows = _order_rows(public_db, "O-515")
    assert len(rows) == 1 and rows[0][1] == 30000 and rows[0][0] != "C-014"
    conn = connect(public_db)
    try:
        assert get_refund(conn, "C-014") is None and committed_audit_count(conn, "C-014") == 0
    finally:
        conn.close()


# --- Evaluation: committed reports match a fresh run -------------------------------------


def _committed(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def test_benchmark_report_matches_a_fresh_run():
    committed = _committed("benchmark.json")
    fresh = run_benchmark(world=WORLD, claims=CLAIMS, shapes=["workflow", "single", "orchestrator", "swarm"], seeds=SEEDS)
    assert fresh["controlled_variables"] == committed["controlled_variables"]
    assert fresh["by_shape"] == committed["by_shape"]
    assert fresh["trials"] == committed["trials"]


def test_crash_report_matches_a_fresh_run(tmp_path):
    committed = _committed("crash.json")
    fresh = run_crash_suite(tmp_path)
    assert fresh == committed
    assert fresh["crash_safety"] == "20/20"


def test_ablation_report_matches_a_fresh_run():
    committed = _committed("ablation_investigator_tools.json")
    names = register_extra_shapes(
        [
            "orchestrator_min=ablation.orchestrator_variants:run_claim_min",
            "orchestrator_wide=ablation.orchestrator_variants:run_claim_wide",
        ]
    )
    try:
        fresh = run_benchmark(world=WORLD, claims=CLAIMS, shapes=["orchestrator_min", "orchestrator", "orchestrator_wide"], seeds=SEEDS)
    finally:
        for name in names:  # extra shapes are per process; leave SHAPES as the other tests expect it
            SHAPES.pop(name, None)
    assert fresh["by_shape"] == committed["by_shape"]
    assert fresh["trials"] == committed["trials"]


# --- Evaluation: ablation traces do not collide ------------------------------------------


def test_each_ablation_variant_keeps_its_own_trace(tmp_path):
    from ablation.orchestrator_variants import run_claim_min, run_claim_wide

    results = {}
    for name, runner in (("min", run_claim_min), ("base", SHAPES["orchestrator"]), ("wide", run_claim_wide)):
        store = tmp_path / f"{name}.sqlite"
        init_store(store, WORLD, CLAIMS).close()
        started = time.time()
        results[name] = runner("C-014", seed=11, store_path=str(store))
        assert check_trace(results[name].as_dict(), started_at=started) == (True, "ok")

    run_ids = {name: result.run_id for name, result in results.items()}
    assert run_ids == {
        "min": "R-orchestrator_min-C-014-s11",
        "base": "R-orchestrator-C-014-s11",
        "wide": "R-orchestrator_wide-C-014-s11",
    }
    paths = {Path(result.trace_path) for result in results.values()}
    assert len(paths) == 3 and all(path.is_file() for path in paths)
    # The variants differ only in read tools, so the wide one makes the most tool calls.
    assert results["min"].tool_calls < results["base"].tool_calls < results["wide"].tool_calls
    for result in results.values():
        assert json.loads(Path(result.trace_path).read_text(encoding="utf-8"))["run_id"] == result.run_id


def test_crashed_ablation_run_leaves_its_trace_under_the_variant_name(public_db):
    from ablation.orchestrator_variants import run_claim_wide

    with pytest.raises(InjectedCrash):
        run_claim_wide("C-001", seed=22, store_path=str(public_db), crash_at="after_effect_before_checkpoint")
    trace = Path("traces") / "R-orchestrator_wide-C-001-s22.json"
    assert trace.is_file()
    assert json.loads(trace.read_text(encoding="utf-8"))["run_id"] == "R-orchestrator_wide-C-001-s22"
