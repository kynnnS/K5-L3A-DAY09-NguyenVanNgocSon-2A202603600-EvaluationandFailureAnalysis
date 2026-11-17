"""Replay with a different amount than the one already committed.

C-001 pays 35.000 (order O-107, 4 days late, shipping fee 35.000). The approval is
then lowered to 20.000 and the claim is run again. The committed amount must stay:
the shape reports `failed` / `amount_conflict`, the ledger keeps one 35.000 row and
one `refund_committed` audit, and the host records one `amount_conflict` audit.
"""

from __future__ import annotations

import pytest

from lab8.db import connect
from lab8.shapes import SHAPES

TEAM_SHAPES = ["workflow", "single", "orchestrator"]


@pytest.mark.parametrize("shape", TEAM_SHAPES)
def test_replay_with_a_lower_approval_keeps_the_committed_amount(public_db, shape):
    store = str(public_db)
    first = SHAPES[shape]("C-001", seed=11, store_path=store)
    assert first.status == "resolved"

    conn = connect(store)
    conn.execute("UPDATE approvals SET approved_vnd = 20000 WHERE approval_id = 'A-001'")
    conn.commit()
    conn.close()

    second = SHAPES[shape]("C-001", seed=11, store_path=store)
    assert second.status == "failed"
    assert second.error_reason == "amount_conflict"
    assert second.proposal_amount == 35000
    assert [int(effect["amount_vnd"]) for effect in second.refund_effects] == [35000]
    assert second.committed_audit_count == 1

    conn = connect(store)
    rows = conn.execute("SELECT amount_vnd FROM refund_ledger WHERE claim_id = 'C-001'").fetchall()
    conflicts = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE claim_id = 'C-001' AND event_type = 'amount_conflict'"
    ).fetchone()["n"]
    conn.close()
    assert [int(row["amount_vnd"]) for row in rows] == [35000]
    assert conflicts == 1
