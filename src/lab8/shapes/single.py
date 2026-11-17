"""Shape B — one ReAct agent.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

The agent runs inside `lab8.runtime.run_react` with max_steps=8. Each step is one
MockLLM call followed by at most one READ tool:

    get_claim_view -> lookup_order -> compute_compensation -> lookup_refund_history -> final

The agent stops early when a later tool cannot change the answer (no order id,
order not visible, nothing owed). Worst case is 5 steps, under the cap of 8.

Code / model boundary:

- The model picks read tools and reads the ticket. It has no write tool:
  `run_react` rejects `issue_refund`, and this file never offers it.
- What the ticket *says* (for example "trễ 9 ngày") is recorded as a claim and
  compared with the order row. The order row always wins. The disagreement is
  traced as two integers, never as ticket text.
- After the loop, code (`_settle`) rebuilds the decision from the tool results it
  kept itself. The agent's `final` is only a recommendation. The quote must be the
  output of `compute_compensation` for exactly the order view `lookup_order`
  returned, otherwise code recomputes it. Then code calls
  `durability.issue_refund` with the FULL quote; the host owns approval,
  per-order cap, and idempotency.

Trace: one event per ReAct step, plus the host call and the decision. Only
allowlisted scalars. No ticket, no principal id, no raw rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, tool_cost
from lab8.trace import Trace

SHAPE = "single"
MAX_STEPS = 8

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


class _Agent:
    """The ReAct policy. `decide` is what `run_react` calls once per step.

    `notes` is code's own copy of every tool result. `run_react` only hands back
    the last result, and `_settle` must not depend on what the model chooses to
    repeat in its final answer.
    """

    def __init__(self, claim_id: str, seed: int, budget: Budget, trace: Trace) -> None:
        self.claim_id = claim_id
        self.seed = seed
        self.budget = budget
        self.trace = trace
        self.notes: dict[str, dict[str, Any]] = {}
        self.quoted_order: dict[str, Any] | None = None
        self.ticket_delay_claim: int | None = None
        self.steps = 0

    def decide(self, seen: dict[str, Any]) -> dict[str, Any]:
        self.steps += 1
        observed = seen.get("last_tool")
        if observed:
            self.notes[observed] = seen.get("last_result") or {}
        self._think(observed)
        action = self._next_action()
        event: dict[str, Any] = {
            "step": self.steps,
            "observed": observed,
            "observation": _summarize(observed, self.notes.get(observed)) if observed else None,
            "action": action.get("tool") or "final",
        }
        if "final" in action:
            event["recommend"] = action["final"].get("recommend")
        if observed == "lookup_order":
            conflict = self._ticket_conflict()
            if conflict:
                event["ticket_conflict"] = conflict
        self.trace.add("react_step", event)
        return action

    def _think(self, observed: str | None) -> None:
        """One model call per step. Right after the claim is read, the model reads the ticket."""
        claim = self.notes.get("get_claim_view") or {}
        if observed == "get_claim_view" and claim.get("found"):
            text = complete(self.budget, seed=self.seed, purpose="parse", prompt=str(claim.get("ticket") or ""))
            match = re.search(r"parsed_delay=(\d+)", text)
            self.ticket_delay_claim = int(match.group(1)) if match else None
            return
        if observed:
            prompt = json.dumps(_summarize(observed, self.notes.get(observed)), ensure_ascii=False)
        else:
            prompt = f"claim_id={self.claim_id}"
        complete(self.budget, seed=self.seed, purpose="react", prompt=prompt)

    def _ticket_conflict(self) -> dict[str, Any] | None:
        order = (self.notes.get("lookup_order") or {}).get("order") or {}
        if self.ticket_delay_claim is None or "delay_days" not in order:
            return None
        if int(order["delay_days"]) == self.ticket_delay_claim:
            return None
        return {
            "ticket_delay_days": self.ticket_delay_claim,
            "order_delay_days": int(order["delay_days"]),
            "used": "order_row",
        }

    def _next_action(self) -> dict[str, Any]:
        claim = self.notes.get("get_claim_view")
        if claim is None:
            return {"tool": "get_claim_view", "args": {"claim_id": self.claim_id}}
        if not claim.get("found"):
            return {"final": {"recommend": "fail"}}
        if not _has_order_id(claim):
            return {"final": {"recommend": "needs_info"}}

        order_view = self.notes.get("lookup_order")
        if order_view is None:
            return {
                "tool": "lookup_order",
                "args": {"order_id": claim["order_id"], "principal_id": claim["principal_id"]},
            }
        if not order_view.get("visible"):
            return {"final": {"recommend": "escalate"}}

        quote = self.notes.get("compute_compensation")
        if quote is None:
            # The order view goes in unchanged. Nothing from the ticket is merged into it.
            self.quoted_order = dict(order_view["order"])
            return {"tool": "compute_compensation", "args": {"order": dict(order_view["order"])}}
        if int(quote.get("amount_vnd") or 0) <= 0:
            return {"final": {"recommend": "deny"}}

        if "lookup_refund_history" not in self.notes:
            return {
                "tool": "lookup_refund_history",
                "args": {"order_id": claim["order_id"], "principal_id": claim["principal_id"]},
            }
        return {"final": {"recommend": "refund"}}


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
    budget: Budget,
    trace: Trace,
) -> RunResult:
    agent = _Agent(claim_id, seed, budget, trace)
    outcome = run_react(
        conn,
        budget,
        observation={"claim": {"claim_id": claim_id}},
        decide=agent.decide,
        max_steps=MAX_STEPS,
    )
    recommend = (outcome.get("final") or {}).get("recommend")

    def done(status: str, proposal: int = 0, reason: str | None = None) -> RunResult:
        trace.add(
            "decision",
            {"status": status, "proposal_vnd": proposal, "reason": reason, "model_recommend": recommend},
        )
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

    # From here on it is code, reading its own notes. The model's `final` is not an input.
    if outcome.get("stopped") != "final":
        return done("failed", 0, "step_cap")
    notes = agent.notes
    claim = notes.get("get_claim_view")
    if claim is None:
        return done("failed", 0, "incomplete_investigation")
    if not claim.get("found"):
        return done("failed", 0, "unknown_claim")
    if not _has_order_id(claim):
        return done("needs_info", 0, "missing_order_id")

    order_view = notes.get("lookup_order")
    if order_view is None:
        return done("failed", 0, "incomplete_investigation")
    if not order_view.get("visible"):
        return done("escalated", 0, order_view.get("reason"))
    order = order_view["order"]

    # The quote is only accepted if the tool was run on exactly this order view.
    quote = notes.get("compute_compensation")
    if quote is None or agent.quoted_order != order:
        budget.add_tool(tool_cost())
        quote = compute_compensation(conn, order)
        trace.add("quote_recomputed", _quote_event(quote))
    proposal = int(quote["amount_vnd"])
    if proposal <= 0:
        return done("denied", 0, quote.get("reason"))

    # FULL quote to the host. InjectedCrash propagates to run_claim.
    result = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    trace.add("host_refund", _outcome_event(result))
    if result["ok"]:
        return done("resolved", proposal, None)
    if result["conflict"]:
        return done("failed", proposal, result["reason"])
    return done("denied", proposal, result["reason"])


def _has_order_id(claim: dict[str, Any]) -> bool:
    # "Missing" means what the host means: empty or null. Any other value is looked up, never guessed.
    return bool(claim.get("order_id"))


def _summarize(tool: str | None, result: dict[str, Any] | None) -> dict[str, Any]:
    """Allowlisted view of a tool result. This is what the trace and later prompts get."""
    result = result or {}
    if tool == "get_claim_view":
        return {"found": bool(result.get("found")), "has_order_id": _has_order_id(result)}
    if tool == "lookup_order":
        if not result.get("visible"):
            return {"visible": False, "reason": result.get("reason")}
        order = result.get("order") or {}
        return {"visible": True, **{key: order[key] for key in _ORDER_TRACE_FIELDS if key in order}}
    if tool == "compute_compensation":
        return _quote_event(result)
    if tool == "lookup_refund_history":
        return {
            "visible": bool(result.get("visible")),
            "refund_count": result.get("refund_count"),
            "refunded_vnd": result.get("refunded_vnd"),
        }
    return {}


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
