"""Simulated payment provider (bonus B5).

The provider is a second system of record. It lives in its own table and uses its
own SQLite connection, so a charge is never in the same transaction as the lab's
refund ledger. That gap is the thing this bonus measures.

Two modes:

- dedupe=True   a second request with the same idempotency key returns the first
                charge. Reusing a key with another amount is rejected.
- dedupe=False  every request creates a charge. This is the counterexample.

Faults are explicit and deterministic, never random:

- defer_next=True  the next request is accepted but the response is lost
                   (ProviderTimeout). The charge stays `pending`: `lookup` does not
                   see it until `deliver_pending()` runs. This models a request
                   that is still in flight when the caller asks "did it land?".

No real payment API, no network, no sleep.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS bonus_provider_charges (
  charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  amount_vnd INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending', 'settled', 'voided'))
);
"""
# No UNIQUE on idempotency_key on purpose: the no-dedupe mode must be able to
# write a duplicate. Dedupe mode enforces the key inside a write transaction.


class ProviderTimeout(Exception):
    """The request was sent and the response was lost. The caller cannot tell if it landed."""


class Provider:
    def __init__(self, store_path: str | Path, *, dedupe: bool = True, defer_next: bool = False) -> None:
        self.dedupe = dedupe
        self.defer_next = defer_next
        self.conn = sqlite3.connect(str(store_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Provider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def refund(self, key: str, claim_id: str, amount_vnd: int) -> dict[str, Any]:
        """Pay `amount_vnd` out. Returns {ok, charge_id, deduped} or {ok: False, reason}."""
        amount = int(amount_vnd)
        if amount <= 0:
            return {"ok": False, "reason": "non_positive_amount", "charge_id": None, "deduped": False}
        deferred, self.defer_next = self.defer_next, False
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.dedupe:
                seen = self.conn.execute(
                    """
                    SELECT charge_id, amount_vnd FROM bonus_provider_charges
                    WHERE idempotency_key = ? AND state != 'voided' ORDER BY charge_id LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                if seen is not None:
                    self.conn.rollback()
                    if int(seen["amount_vnd"]) != amount:
                        return {"ok": False, "reason": "key_reused_with_other_amount", "charge_id": seen["charge_id"], "deduped": True}
                    return {"ok": True, "reason": "deduped", "charge_id": seen["charge_id"], "deduped": True}
            cursor = self.conn.execute(
                "INSERT INTO bonus_provider_charges (idempotency_key, claim_id, amount_vnd, state) VALUES (?, ?, ?, ?)",
                (key, claim_id, amount, "pending" if deferred else "settled"),
            )
            charge_id = cursor.lastrowid
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if deferred:
            raise ProviderTimeout(key)
        return {"ok": True, "reason": "charged", "charge_id": charge_id, "deduped": False}

    def lookup(self, key: str) -> list[dict[str, Any]]:
        """Settled charges for a key. A request still in flight (`pending`) is not visible."""
        rows = self.conn.execute(
            """
            SELECT charge_id, idempotency_key, claim_id, amount_vnd, state FROM bonus_provider_charges
            WHERE idempotency_key = ? AND state = 'settled' ORDER BY charge_id
            """,
            (key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def deliver_pending(self) -> int:
        """In-flight requests land. A deduping provider drops the ones whose key is already settled."""
        landed = 0
        pending = self.conn.execute(
            "SELECT charge_id, idempotency_key FROM bonus_provider_charges WHERE state = 'pending' ORDER BY charge_id"
        ).fetchall()
        for row in pending:
            duplicate = self.dedupe and self.lookup(row["idempotency_key"])
            self.conn.execute(
                "UPDATE bonus_provider_charges SET state = ? WHERE charge_id = ?",
                ("voided" if duplicate else "settled", row["charge_id"]),
            )
            landed += 0 if duplicate else 1
        self.conn.commit()
        return landed

    def void(self, charge_id: int) -> None:
        """Reverse one charge. A real provider may refuse this or take days; see the report limits."""
        self.conn.execute("UPDATE bonus_provider_charges SET state = 'voided' WHERE charge_id = ?", (charge_id,))
        self.conn.commit()

    def counts(self, claim_id: str) -> dict[str, int]:
        """Charges per state for a claim, plus the money that actually left."""
        out = {"settled": 0, "pending": 0, "voided": 0, "settled_vnd": 0}
        for row in self.conn.execute(
            "SELECT state, COUNT(*) AS n, COALESCE(SUM(amount_vnd), 0) AS total FROM bonus_provider_charges WHERE claim_id = ? GROUP BY state",
            (claim_id,),
        ):
            out[row["state"]] = int(row["n"])
            if row["state"] == "settled":
                out["settled_vnd"] = int(row["total"])
        return out
