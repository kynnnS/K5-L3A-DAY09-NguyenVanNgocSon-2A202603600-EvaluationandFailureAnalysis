"""Shape C — orchestrator plus a read-only investigator.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Roles:

- Orchestrator (this file): reads the claim view, plans, builds the handoff,
  checks the investigator's report, and is the only caller of
  `durability.issue_refund`.
- Investigator (`lab8.runtime.run_readonly_investigators`): receives only the
  sanitized handoff and runs the read tools named in it. It has no write
  registry; `make_handoff` rejects any tool outside READONLY_TOOLS.

Steps:

1. `get_claim_view`, then one MockLLM "plan" call that reads the ticket.
2. order_id missing -> `needs_info`. No handoff is built; there is nothing to investigate.
3. `make_handoff` with `facts={}` and three read tools. The handoff carries the
   claim id and the task, nothing from the parent context: no ticket, no
   principal id, no secret, no headers.
4. Investigator runs `lookup_order`, `lookup_refund_history`, `compute_compensation`
   and reports back (one MockLLM "investigate" call).
5. Orchestrator independently re-reads the claim-bound order (principal scoped),
   recalculates the canonical quote under current policy, and compares both
   against the investigator report. Any disagreement is traced and ignored.
   Two additional read calls are measured in the tool budget.
6. FULL quote to `durability.issue_refund`; the host owns approval, per-order
   cap, and idempotency.
7. One MockLLM "draft" call words the reply after the decision.

Trace: the sanitized handoff (never the parent context), an allowlisted summary
of the investigator's report, the host call, and the decision.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import make_handoff
from lab8.mock_llm import complete
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

SHAPE = "orchestrator"

# Read-only, in dependency order: the quote needs the order view from the same handoff.
INVESTIGATOR_TOOLS = ["lookup_order", "lookup_refund_history", "compute_compensation"]

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
    return run_with_tools(
        claim_id,
        seed=seed,
        store_path=store_path,
        crash_at=crash_at,
        investigator_tools=INVESTIGATOR_TOOLS,
    )


def run_with_tools(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None,
    investigator_tools: list[str],
) -> RunResult:
    """Same orchestrator, with the investigator's read tools as the one free variable.

    `run_claim` always uses INVESTIGATOR_TOOLS. The ablation in `src/ablation/` calls this
    with a shorter or longer list to measure what each extra read costs.
    """
    budget = Budget()
    trace = _GuardedTrace(make_run_id(SHAPE, claim_id, seed))
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(store_path)
        return _run(
            conn,
            claim_id,
            seed=seed,
            crash_at=crash_at,
            budget=budget,
            trace=trace,
            investigator_tools=list(investigator_tools),
        )
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
    investigator_tools: list[str],
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

    # Step 1: orchestrator's own context. The ticket stays here; it is not handed off or traced.
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    if not claim.get("found"):
        trace.add("claim", {"found": False})
        return done("failed", 0, "unknown_claim")
    order_id = claim.get("order_id")
    # "Missing" means what the host means: empty or null. Any other value is looked up, never guessed.
    has_order_id = bool(order_id)
    complete(budget, seed=seed, purpose="plan", prompt=str(claim.get("ticket") or ""))
    trace.add("plan", {"found": True, "has_order_id": has_order_id, "delegate": has_order_id})

    # Step 2: nothing to investigate without an order id on the claim.
    if not has_order_id:
        return done("needs_info", 0, "missing_order_id")

    # Step 3: typed handoff. facts={} — the orchestrator knows no order facts yet, and
    # must not forward what the ticket claims as if it were a fact.
    handoff = make_handoff(
        request_id=f"REQ-{claim_id}",
        claim_id=claim_id,
        task="verify_order_and_quote",
        facts={},
        allowed_tools=investigator_tools,
    )
    trace.add("handoff", safe_dump(handoff))

    # Step 4: investigator works from the handoff with read tools only.
    investigated = run_readonly_investigators(conn, budget, handoff, principal_id=claim["principal_id"])
    tools = investigated.get("tools") if isinstance(investigated, dict) else None
    tools = tools if isinstance(tools, dict) else {}
    report = _report(tools)
    complete(budget, seed=seed, purpose="investigate", prompt=json.dumps(report, ensure_ascii=False))
    trace.add("investigator", report)

    # Step 5: investigator output is evidence, NOT authority for money.
    # Independently re-read the claim-bound order under its principal scope and
    # recompute the canonical quote using current policy. Charge both read calls.
    # This protects against a *self-consistent but fabricated* investigator quote
    # (old code checked only amount == max(reported_parts)).
    budget.add_tool(tool_cost())
    trusted_order_view = lookup_order(conn, order_id, claim["principal_id"])
    if not trusted_order_view.get("visible"):
        return done("escalated", 0, trusted_order_view.get("reason") or "order_not_visible")
    order = trusted_order_view["order"]
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)

    reported_order_view = tools.get("lookup_order") or {}
    reported_order = reported_order_view.get("order") if isinstance(reported_order_view, dict) else None
    reported_quote = tools.get("compute_compensation")
    problem = _quote_problem(reported_quote)
    if reported_order != order:
        problem = "order_mismatch"
    elif problem is None and reported_quote != quote:
        problem = "quote_mismatch"
    if problem:
        trace.add("report_rejected", {"reason": problem, "recomputed": _quote_event(quote)})
    proposal = int(quote["amount_vnd"])
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


def _report(tools: dict[str, Any]) -> dict[str, Any]:
    """Trace typed diagnostics only: even allowlisted values may contain PII."""
    report: dict[str, Any] = {}
    order_view = tools.get("lookup_order")
    report["order"] = {"visible": isinstance(order_view, dict) and order_view.get("visible") is True}
    history = tools.get("lookup_refund_history")
    if isinstance(history, dict) and history.get("visible") is True:
        report["refund_history"] = {
            key: value if type(value) is int and value >= 0 else None
            for key in ("refund_count", "refunded_vnd")
            for value in (history.get(key),)
        }
    quote = tools.get("compute_compensation")
    if isinstance(quote, dict):
        amount = quote.get("amount_vnd")
        # Investigator text is untrusted; trace only primitive diagnostic facts.
        report["quote"] = {
            "amount_vnd": amount if isinstance(amount, int) and not isinstance(amount, bool) else None,
            "well_formed": _quote_problem(quote) is None,
        }
    return report


def _quote_problem(quote: Any) -> str | None:
    """Why the orchestrator will not pay from this quote, or None if it is well-formed."""
    if not isinstance(quote, dict):
        return "missing_quote"
    amount = quote.get("amount_vnd")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        return "bad_amount"
    parts = quote.get("parts") or {}
    if not isinstance(parts, dict) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in parts.values()
    ):
        return "bad_parts"
    expected = max(parts.values()) if parts else 0
    if amount != expected:
        # Stacking rule: one refund equal to the largest eligible part, never the sum.
        return "amount_not_max_of_parts"
    return None


def _quote_event(quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "amount_vnd": int(quote.get("amount_vnd") or 0),
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
