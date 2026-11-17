"""Shape A — deterministic workflow.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Fixed steps, in this order, every time:

1. `get_claim_view`           claim not found -> `failed`
2. order_id missing           -> `needs_info` (never read an order id out of the ticket)
3. `lookup_order`             not visible -> `escalated`
4. `compute_compensation`     quote from the order row only
5. quote == 0                 -> `denied`, host is not called
6. `durability.issue_refund`  FULL quote; host owns approval, per-order cap, idempotency
7. one MockLLM call drafts the reply. It runs after the decision and cannot change it.

Code / model boundary: the ticket text is never an input to a decision here.
The model sees only the final status and amount, to word the reply.

Trace: one event per step. Only allowlisted scalars are written. The ticket,
the principal id, and anything outside `lookup_order`'s allowlist never reach
the trace or the result.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

SHAPE = "workflow"

# Order fields that are safe to log and are enough to explain the quote.
_ORDER_TRACE_FIELDS = ("order_id", "status", "delay_days", "shipping_fee_vnd", "paid_vnd", "damage_verified")
# Tripwire only. The real control is that nothing outside the allowlist is ever added.
_LEAK_MARKERS = ("CANARY_", "parent_secret", "raw_headers", "Bearer ")


class _GuardedTrace(Trace):
    """lab8.trace.Trace plus a last-line check before the file is written."""

    def write(self) -> str:
        blob = self.dump()
        if any(marker in blob for marker in _LEAK_MARKERS):
            self.events = [{"kind": "redacted", "payload": {"reason": "leak_marker_blocked", "dropped_events": len(self.events)}}]
        return super().write()


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    budget = Budget()
    trace = _GuardedTrace(make_run_id(SHAPE, claim_id, seed))
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(store_path)
        return _run(conn, claim_id, seed=seed, crash_at=crash_at, budget=budget, trace=trace)
    except InjectedCrash:
        # Not swallowed: flush the trace so the crashed run is still auditable, then re-raise.
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    except Exception as exc:  # noqa: BLE001 — run_claim must not raise; fail closed without paying
        trace.add("error", {"reason": "internal_error", "type": type(exc).__name__})
        return _fail_closed(conn, claim_id, seed, budget, trace, f"internal_error:{type(exc).__name__}")
    finally:
        if conn is not None:
            conn.close()


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
    budget: Budget,
    trace: Trace,
) -> RunResult:
    def done(status: str, proposal: int = 0, reason: str | None = None) -> RunResult:
        # Step 7: the model only words the reply for a decision that is already made.
        complete(budget, seed=seed, purpose="draft", prompt=f"status={status};proposal_vnd={proposal}")
        trace.add("decision", {"status": status, "proposal_vnd": proposal, "reason": reason})
        return finish(
            conn,
            shape=SHAPE,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    # Step 1: claim through the allowlisted view (no parent_secret, no headers).
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    if not claim.get("found"):
        trace.add("claim", {"found": False})
        return done("failed", 0, "unknown_claim")
    order_id = claim.get("order_id")
    # "Missing" means what the host means: empty or null. Any other value is looked up, never guessed.
    has_order_id = bool(order_id)
    trace.add("claim", {"found": True, "has_order_id": has_order_id})

    # Step 2: no order id on the claim. Do not guess one from the ticket.
    if not has_order_id:
        return done("needs_info", 0, "missing_order_id")

    # Step 3: order scoped to the claim's principal.
    budget.add_tool(tool_cost())
    order_view = lookup_order(conn, order_id, claim["principal_id"])
    if not order_view.get("visible"):
        trace.add("order", {"visible": False, "reason": order_view.get("reason")})
        return done("escalated", 0, order_view.get("reason"))
    order = order_view["order"]
    trace.add("order", {"visible": True, **_pick(order, _ORDER_TRACE_FIELDS)})

    # Step 4: money is computed by code from the order row and current policy.
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add("quote", _quote_event(quote))

    # Step 5: nothing owed. The host is not called, so no policy_denied audit.
    if proposal <= 0:
        return done("denied", 0, quote.get("reason"))

    # Step 6: FULL quote to the host. InjectedCrash propagates to run_claim.
    outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    trace.add("host_refund", _outcome_event(outcome))
    if outcome["ok"]:
        return done("resolved", proposal, None)
    if outcome["conflict"]:
        return done("failed", proposal, outcome["reason"])
    return done("denied", proposal, outcome["reason"])


def _pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


def _quote_event(quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "amount_vnd": int(quote["amount_vnd"]),
        "reason": quote.get("reason"),
        "clause_ids": list(quote.get("clause_ids") or []),
        "parts": dict(quote.get("parts") or {}),
    }


def _outcome_event(outcome: dict[str, Any]) -> dict[str, Any]:
    effect = outcome.get("effect") or {}
    return {
        "ok": bool(outcome["ok"]),
        "reason": outcome["reason"],
        "conflict": bool(outcome["conflict"]),
        "committed_vnd": effect.get("amount_vnd"),
    }


def _fail_closed(
    conn: sqlite3.Connection | None,
    claim_id: str,
    seed: int,
    budget: Budget,
    trace: Trace,
    reason: str,
) -> RunResult:
    """Unexpected error: report `failed`, still with a contract-v1 trace and the real ledger state."""
    if conn is not None:
        try:
            return finish(
                conn,
                shape=SHAPE,
                claim_id=claim_id,
                seed=seed,
                status="failed",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason=reason,
            )
        except Exception:  # noqa: BLE001 — store unreadable; fall through to a ledger-free result
            pass
    return RunResult(
        run_id=trace.run_id,
        claim_id=claim_id,
        shape=SHAPE,
        seed=seed,
        status="failed",
        proposal_amount=0,
        refund_effects=[],
        committed_audit_count=0,
        model_calls=budget.model_calls,
        tool_calls=budget.tool_calls,
        input_tokens=budget.input_tokens,
        output_tokens=budget.output_tokens,
        cost_estimate=round(budget.cost_estimate, 6),
        trace_path=trace.write(),
        error_reason=reason,
    )
