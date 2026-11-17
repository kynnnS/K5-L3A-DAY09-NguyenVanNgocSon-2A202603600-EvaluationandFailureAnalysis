"""Bonus B5 variant: the team's workflow, with the payout routed through a provider.

Same signature and the same decision path as lab8/shapes/workflow.py. The only
change is step 6: instead of calling the host directly, the amount goes through
`bonus.two_phase.settle` (reserve -> provider -> host confirm -> settlement audit).

Run it next to the baseline, from src/:

    python -m lab8.benchmark --shapes workflow,workflow_provider \
        --extra-shape workflow_provider=bonus.workflow_provider:run_claim \
        --seeds 11,22,33,44,55 --out reports/bonus_benchmark.json

Each provider request is billed as one tool call, so cost/success shows what the
extra safety costs. `crash_at` accepts the three host points and the three new
points in bonus.two_phase.NEW_CRASH_POINTS.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from bonus.provider import Provider
from bonus.two_phase import settle
from lab8.db import connect
from lab8.durability import InjectedCrash, committed_audit_count, get_refund
from lab8.mock_llm import complete
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

SHAPE = "workflow_provider"
_ORDER_TRACE_FIELDS = ("order_id", "status", "delay_days", "shipping_fee_vnd", "paid_vnd", "damage_verified")


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))
    conn: sqlite3.Connection | None = None
    provider: Provider | None = None
    status, proposal, reason = "failed", 0, "internal_error"
    try:
        conn = connect(store_path)
        provider = Provider(store_path, dedupe=True)
        status, proposal, reason = _decide(conn, provider, claim_id, crash_at=crash_at, budget=budget, trace=trace)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        _close(conn, provider)
        raise
    except Exception as exc:  # noqa: BLE001 — run_claim must not raise; fail closed without paying
        status, proposal, reason = "failed", 0, f"internal_error:{type(exc).__name__}"
        trace.add("error", {"reason": "internal_error", "type": type(exc).__name__})
    try:
        # The model only words the reply for a decision that is already made.
        complete(budget, seed=seed, purpose="draft", prompt=f"status={status};proposal_vnd={proposal}")
        trace.add("decision", {"status": status, "proposal_vnd": proposal, "reason": reason})
        effect, audits = _ledger(conn, claim_id)
    finally:
        _close(conn, provider)
    return RunResult(
        run_id=trace.run_id,
        claim_id=claim_id,
        shape=SHAPE,
        seed=seed,
        status=status,
        proposal_amount=int(proposal),
        refund_effects=[effect] if effect else [],
        committed_audit_count=audits,
        model_calls=budget.model_calls,
        tool_calls=budget.tool_calls,
        input_tokens=budget.input_tokens,
        output_tokens=budget.output_tokens,
        cost_estimate=round(budget.cost_estimate, 6),
        trace_path=trace.write(),
        error_reason=reason,
    )


def _decide(
    conn: sqlite3.Connection,
    provider: Provider,
    claim_id: str,
    *,
    crash_at: str | None,
    budget: Budget,
    trace: Trace,
) -> tuple[str, int, str | None]:
    """Returns (status, proposal_vnd, reason). Steps 1-5 are the baseline workflow's."""
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    if not claim.get("found"):
        trace.add("claim", {"found": False})
        return "failed", 0, "unknown_claim"
    order_id = claim.get("order_id")
    trace.add("claim", {"found": True, "has_order_id": bool(order_id)})
    if not order_id:
        return "needs_info", 0, "missing_order_id"

    budget.add_tool(tool_cost())
    order_view = lookup_order(conn, order_id, claim["principal_id"])
    if not order_view.get("visible"):
        trace.add("order", {"visible": False, "reason": order_view.get("reason")})
        return "escalated", 0, order_view.get("reason")
    order = order_view["order"]
    trace.add("order", {"visible": True, **{key: order[key] for key in _ORDER_TRACE_FIELDS if key in order}})

    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add("quote", {"amount_vnd": proposal, "reason": quote.get("reason"), "clause_ids": list(quote.get("clause_ids") or [])})
    if proposal <= 0:
        return "denied", 0, quote.get("reason")

    # Step 6: reserve -> provider -> host confirm -> settlement audit. InjectedCrash propagates.
    outcome = settle(conn, provider, claim_id, proposal, crash_at=crash_at)
    for _ in range(outcome["provider_calls"]):
        budget.add_tool(tool_cost())
    trace.add("provider_settle", _settle_event(outcome))
    if outcome["ok"]:
        return "resolved", proposal, None
    if outcome["conflict"]:
        return "failed", proposal, outcome["reason"]
    return "denied", proposal, outcome["reason"]


def _settle_event(outcome: dict[str, Any]) -> dict[str, Any]:
    effect = outcome.get("effect") or {}
    return {
        "ok": bool(outcome["ok"]),
        "reason": outcome["reason"],
        "conflict": bool(outcome["conflict"]),
        "phase": outcome["phase"],
        "provider_calls": int(outcome["provider_calls"]),
        "committed_vnd": effect.get("amount_vnd"),
    }


def _ledger(conn: sqlite3.Connection | None, claim_id: str) -> tuple[dict[str, Any] | None, int]:
    """Result fields are read from the ledger, never from what the flow believes it did."""
    if conn is None:
        return None, 0
    try:
        return get_refund(conn, claim_id), committed_audit_count(conn, claim_id)
    except sqlite3.Error:
        return None, 0


def _close(conn: sqlite3.Connection | None, provider: Provider | None) -> None:
    if provider is not None:
        provider.close()
    if conn is not None:
        conn.close()
