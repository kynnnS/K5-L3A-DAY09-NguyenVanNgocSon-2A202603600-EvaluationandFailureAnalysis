"""Two-phase payout next to the host ledger (bonus B5).

    reserve  ->  provider.refund(key)  ->  confirm (host issue_refund)  ->  settlement audit

lab8/durability.py is not modified. The host still owns approval, the per-order
cap, the ledger row and the `refund_committed` audit. This module adds what the
host does not have: a record of intent written before money leaves (the
reservation), and a job that closes reservations left open by a crash.

Rules:

- The reservation fixes the amount before the provider is called. The amount
  comes from the host's own `authorize`, so the provider is never asked for more
  than the host would write.
- The provider key is the ledger key, `refund:{claim_id}`.
- On resume, authorization is checked again before anything else. If the
  approval changed while the reservation was open, the old charge is voided and
  the flow starts over; the host then pays the new amount or denies.
- A ledger row with no reservation is left alone. It may be a refund paid before
  this flow existed (seeded `prior_refunds`); paying it again would be a double charge.

`settle` is safe to call again after any crash. `reconcile` calls it for every
open reservation, then voids duplicate charges it can see.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from bonus.provider import Provider
from lab8.durability import InjectedCrash, get_refund, idempotency_key, issue_refund
from lab8.policy import authorize, order_refunded_vnd

NEW_CRASH_POINTS = ("after_reserve_before_provider", "after_provider_before_confirm", "after_confirm_before_audit")
HOST_CRASH_POINTS = ("before_effect", "after_effect_before_checkpoint", "after_checkpoint")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bonus_reservations (
  claim_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  proposed_vnd INTEGER NOT NULL,
  amount_vnd INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('reserved', 'confirmed', 'released', 'stuck'))
);
CREATE TABLE IF NOT EXISTS bonus_settlement_audit (
  audit_key TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  details_json TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_reservation(conn: sqlite3.Connection, claim_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM bonus_reservations WHERE claim_id = ?", (claim_id,)).fetchone()
    return dict(row) if row else None


def settlement_audit_count(conn: sqlite3.Connection, claim_id: str, event_type: str = "provider_settled") -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM bonus_settlement_audit WHERE claim_id = ? AND event_type = ?",
        (claim_id, event_type),
    ).fetchone()
    return int(row["n"])


def preauthorize(conn: sqlite3.Connection, claim_id: str, proposed_vnd: int) -> int | None:
    """Amount the host would write right now, or None if it would deny. Read-only.

    Same checks, same order, same functions as the top of durability.issue_refund.
    """
    claim = conn.execute("SELECT principal_id, order_id FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if claim is None or not claim["order_id"]:
        return None
    order = conn.execute("SELECT principal_id, paid_vnd FROM orders WHERE order_id = ?", (claim["order_id"],)).fetchone()
    if order is None or order["principal_id"] != claim["principal_id"]:
        return None
    already = order_refunded_vnd(conn, claim["order_id"], exclude_claim_id=claim_id)
    decision = authorize(conn, claim_id, proposed_vnd, paid_vnd=int(order["paid_vnd"]), already_refunded_vnd=already)
    return decision.amount_vnd if decision.ok else None


def settle(
    conn: sqlite3.Connection,
    provider: Provider,
    claim_id: str,
    proposed_vnd: int,
    *,
    crash_at: str | None = None,
    lookup_before_send: bool = True,
) -> dict[str, Any]:
    """Pay one claim through the provider and the host ledger. Safe to re-run.

    Returns the host outcome ({ok, reason, effect, conflict}) plus `provider_calls`
    (requests sent to the provider in this call) and `phase` (where the flow ended).
    lookup_before_send=False is the naive retry used as a counterexample.
    """
    ensure_schema(conn)
    key = idempotency_key(claim_id)
    calls = 0
    reservation = get_reservation(conn, claim_id)
    ledger = get_refund(conn, claim_id)

    if reservation is not None and reservation["state"] == "stuck":
        return {"ok": False, "reason": "provider_ledger_mismatch", "effect": ledger, "conflict": True, "provider_calls": 0, "phase": "stuck"}

    if ledger is not None and (reservation is None or reservation["state"] != "reserved"):
        # Already paid (by this flow, or before it existed). The host answers replay or conflict.
        outcome = issue_refund(conn, claim_id, proposed_vnd)
        return {**outcome, "provider_calls": 0, "phase": "already_committed"}

    if ledger is None:
        amount = preauthorize(conn, claim_id, proposed_vnd)
        if reservation is not None and reservation["state"] == "reserved" and amount != reservation["amount_vnd"]:
            calls += _release(conn, provider, reservation, "authorization_changed")
            reservation = None
        if reservation is None or reservation["state"] != "reserved":
            if amount is None:
                # The host denies and writes its own policy_denied audit. No money was reserved.
                outcome = issue_refund(conn, claim_id, proposed_vnd)
                return {**outcome, "provider_calls": calls, "phase": "denied_before_reserve"}
            conn.execute(
                "INSERT OR REPLACE INTO bonus_reservations (claim_id, idempotency_key, proposed_vnd, amount_vnd, state) VALUES (?, ?, ?, ?, 'reserved')",
                (claim_id, key, int(proposed_vnd), amount),
            )
            conn.commit()
            reservation = get_reservation(conn, claim_id)
            if crash_at == "after_reserve_before_provider":
                raise InjectedCrash(crash_at)

        # Phase 2: money leaves. A resumed run asks the provider first instead of trusting local state.
        seen = []
        if lookup_before_send:
            calls += 1
            seen = provider.lookup(key)
        if not seen:
            calls += 1
            sent = provider.refund(key, claim_id, reservation["amount_vnd"])
            if not sent["ok"]:
                _mark(conn, reservation, "stuck", "provider_rejected", {"reason": sent["reason"]})
                return {"ok": False, "reason": "provider_rejected", "effect": None, "conflict": True, "provider_calls": calls, "phase": "stuck"}
        if crash_at == "after_provider_before_confirm":
            raise InjectedCrash(crash_at)

    # Phase 3: confirm. The host writes the ledger row and its audit in one transaction.
    host_crash = crash_at if crash_at in HOST_CRASH_POINTS else None
    outcome = issue_refund(conn, claim_id, proposed_vnd, crash_at=host_crash)
    effect = outcome.get("effect")
    if effect is None:
        # Charged, then the host refused (approval changed between the two phases). Give the money back.
        calls += _release(conn, provider, reservation, f"host_denied:{outcome['reason']}")
        return {**outcome, "provider_calls": calls, "phase": "released"}
    if int(effect["amount_vnd"]) != int(reservation["amount_vnd"]):
        # Ledger and provider disagree on the amount. Stop here; a person decides.
        _mark(conn, reservation, "stuck", "provider_ledger_mismatch", {"ledger_vnd": int(effect["amount_vnd"]), "reserved_vnd": int(reservation["amount_vnd"])})
        return {"ok": False, "reason": "provider_ledger_mismatch", "effect": effect, "conflict": True, "provider_calls": calls, "phase": "stuck"}
    if crash_at == "after_confirm_before_audit":
        raise InjectedCrash(crash_at)
    _mark(conn, reservation, "confirmed", "provider_settled", {"amount_vnd": int(effect["amount_vnd"]), "idempotency_key": key})
    return {**outcome, "provider_calls": calls, "phase": "confirmed"}


def reconcile(conn: sqlite3.Connection, provider: Provider) -> dict[str, Any]:
    """Close every reservation a crash left open, then void duplicate charges that are visible.

    Roll forward only: an open reservation is re-driven through `settle`, which
    re-checks authorization and asks the provider before sending. Safe to run twice.
    """
    ensure_schema(conn)
    report: dict[str, Any] = {"rolled_forward": [], "released": [], "stuck": [], "duplicates_voided": [], "provider_calls": 0}
    rows = conn.execute("SELECT claim_id, proposed_vnd, state FROM bonus_reservations ORDER BY claim_id").fetchall()
    for row in [dict(item) for item in rows]:
        claim_id = row["claim_id"]
        if row["state"] == "reserved":
            outcome = settle(conn, provider, claim_id, int(row["proposed_vnd"]))
            report["provider_calls"] += outcome["provider_calls"]
            bucket = {"confirmed": "rolled_forward", "stuck": "stuck"}.get(outcome["phase"], "released")
            report[bucket].append(claim_id)
        if (get_reservation(conn, claim_id) or {}).get("state") != "confirmed":
            continue
        report["provider_calls"] += 1
        for extra in provider.lookup(idempotency_key(claim_id))[1:]:
            provider.void(extra["charge_id"])
            report["provider_calls"] += 1
            report["duplicates_voided"].append({"claim_id": claim_id, "charge_id": extra["charge_id"]})
            _audit(conn, claim_id, f"duplicate_voided:{claim_id}:{extra['charge_id']}", "duplicate_voided", {"charge_id": extra["charge_id"]})
            conn.commit()  # release the write lock before the provider connection writes again
    return report


def _release(conn: sqlite3.Connection, provider: Provider, reservation: dict[str, Any], why: str) -> int:
    """Void every visible charge for this reservation and mark it released. Returns provider calls made."""
    charges = provider.lookup(reservation["idempotency_key"])
    for charge in charges:
        provider.void(charge["charge_id"])
    _mark(conn, reservation, "released", "reservation_released", {"why": why, "voided": len(charges)}, unique=False)
    return 1 + len(charges)


def _mark(
    conn: sqlite3.Connection,
    reservation: dict[str, Any],
    state: str,
    event_type: str,
    details: dict[str, Any],
    *,
    unique: bool = True,
) -> None:
    """State change and its audit row in one transaction."""
    claim_id = reservation["claim_id"]
    conn.execute("UPDATE bonus_reservations SET state = ? WHERE claim_id = ?", (state, claim_id))
    suffix = "" if unique else f":{settlement_audit_count(conn, claim_id, event_type) + 1}"
    _audit(conn, claim_id, f"{event_type}:{claim_id}{suffix}", event_type, details)
    conn.commit()


def _audit(conn: sqlite3.Connection, claim_id: str, audit_key: str, event_type: str, details: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO bonus_settlement_audit (audit_key, claim_id, event_type, details_json) VALUES (?, ?, ?, ?)",
        (audit_key, claim_id, event_type, json.dumps(details, ensure_ascii=False)),
    )
