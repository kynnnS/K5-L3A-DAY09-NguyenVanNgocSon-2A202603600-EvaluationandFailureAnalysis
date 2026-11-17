"""Shape A (workflow) always walks the same fixed steps.

Locks down the code / model boundary of `lab8.shapes.workflow`: tools are called
by code in a fixed order, and the model is called exactly once, after the
decision, only to word the reply.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab8.shapes.workflow import run_claim
from tests.conftest import BY_ID


def _events(result) -> list[dict]:
    return json.loads(Path(result.trace_path).read_text(encoding="utf-8"))["events"]


def test_resolved_claim_walks_every_fixed_step_in_order(public_db):
    result = run_claim("C-001", seed=11, store_path=str(public_db))
    kinds = [event["kind"] for event in _events(result)]
    assert kinds == ["claim", "order", "quote", "host_refund", "decision"]
    assert result.model_calls == 1
    assert result.tool_calls == 3


@pytest.mark.parametrize("claim_id", sorted(BY_ID))
def test_one_model_call_and_at_most_three_tool_calls(public_db, claim_id):
    result = run_claim(claim_id, seed=11, store_path=str(public_db))
    kinds = [event["kind"] for event in _events(result)]
    assert result.model_calls == 1
    assert result.tool_calls <= 3
    # The decision is always the last event: the model never runs before it.
    assert kinds[0] == "claim"
    assert kinds[-1] == "decision"
    assert kinds.count("decision") == 1
