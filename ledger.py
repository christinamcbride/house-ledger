"""The house ledger: SQLite, and the arithmetic of who owes whom.

Everything that changes money goes through here, and nothing here is decided
by a model. `houserules.py` works out the split from the signed agreement;
this module stores it, adds it up, and works out the smallest set of payments
that settles the month.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from config import LEDGER_DB
from houserules import load_policy
from models import Expense, Share

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id              INTEGER PRIMARY KEY,
    merchant        TEXT,
    purchase_date   TEXT,
    total           REAL NOT NULL,
    paid_by         TEXT NOT NULL,
    category        TEXT NOT NULL,
    split_rule      TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    personal_items  TEXT,            -- JSON
    line_items      TEXT,            -- JSON, what the paper listed
    assignments     TEXT,            -- JSON, description -> tenant
    source_document TEXT,
    needs_approval  INTEGER NOT NULL DEFAULT 0,
    approved_by     TEXT,            -- a tenant's name; never an agent
    approved_at     TEXT,
    status          TEXT NOT NULL,   -- pending | posted | rejected | disputed
    flags           TEXT,            -- JSON, from verify.py
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shares (
    id          INTEGER PRIMARY KEY,
    expense_id  INTEGER NOT NULL REFERENCES expenses(id),
    tenant      TEXT NOT NULL,
    amount      REAL NOT NULL,
    basis       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disputes (
    id          INTEGER PRIMARY KEY,
    expense_id  INTEGER NOT NULL REFERENCES expenses(id),
    raised_by   TEXT NOT NULL,
    claim       TEXT NOT NULL,
    ruling      TEXT,
    clause      TEXT,
    explanation TEXT,
    decided_by  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY,
    expense_id  INTEGER NOT NULL REFERENCES expenses(id),
    tenant      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (expense_id, tenant)
);

CREATE TABLE IF NOT EXISTS tenants (
    name          TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,     -- pbkdf2_sha256$iters$salt$hash
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    tenant     TEXT NOT NULL REFERENCES tenants(name),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    tenant     TEXT NOT NULL,
    role       TEXT NOT NULL,   -- 'tenant' | 'house'
    text       TEXT NOT NULL,
    data       TEXT,            -- JSON the dashboard renders as cards
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY,
    recipient   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    about       TEXT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL   -- always 'on_board'; nothing is ever sent
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    LEDGER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        # Columns added after the first release. A house that has been
        # running since before them should not have to start over.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(expenses)")}
        for col in ("line_items", "assignments"):
            if col not in have:
                conn.execute(f"ALTER TABLE expenses ADD COLUMN {col} TEXT")
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_expense(expense: Expense, flags: list[dict] | None = None,
                   line_items: list | None = None) -> int:
    """Store a split expense. Nothing is owed until it is posted."""
    status = "pending" if expense.needs_approval else "posted"
    with connect() as c:
        cur = c.execute(
            "INSERT INTO expenses (merchant, purchase_date, total, paid_by, category,"
            " split_rule, reasoning, personal_items, line_items, source_document,"
            " needs_approval, status, flags, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (expense.merchant, expense.purchase_date, expense.total, expense.paid_by,
             expense.category, expense.split_rule, expense.reasoning,
             json.dumps([li.model_dump() for li in expense.personal_items]),
             json.dumps([li.model_dump() for li in (line_items or [])]),
             expense.source_document, int(expense.needs_approval), status,
             json.dumps(flags or []), now()),
        )
        eid = int(cur.lastrowid)
        for s in expense.shares:
            c.execute("INSERT INTO shares (expense_id, tenant, amount, basis)"
                      " VALUES (?,?,?,?)", (eid, s.tenant, s.amount, s.basis))
    return eid


def approve_expense(expense_id: int, tenant: str) -> dict[str, Any]:
    """Record one tenant's signature, and post the charge once it has enough.

    Who has to sign is a question for the agreement, not for this module:
    section 6 asks everyone the charge falls on. Buying the thing counts as
    the payer's own yes, so the payer cannot sign their own charge off.
    """
    tenant = tenant.strip()
    if not tenant:
        raise ValueError("An approval has to name the tenant making it.")
    policy = load_policy()
    with connect() as c:
        row = c.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if row is None:
            raise ValueError(f"No expense {expense_id}.")
        if row["status"] == "posted":
            raise ValueError(f"Expense {expense_id} is already posted.")
        if row["status"] == "rejected":
            raise ValueError(f"Expense {expense_id} was rejected; it cannot be signed off.")
        if tenant == row["paid_by"]:
            raise ValueError(
                f"{tenant} paid for this, and paying is not approving. Section 6 "
                "needs somebody else in the house to agree to it.")

        shares = [dict(r) for r in c.execute(
            "SELECT tenant, amount FROM shares WHERE expense_id=?", (expense_id,))]
        required = policy.signers_required(shares, row["paid_by"])
        if tenant not in required:
            raise ValueError(
                f"{tenant} is not paying anything towards this, so {tenant} does not "
                "sign it off. Section 6: the people a charge falls on are the ones "
                "who agree to it.")

        c.execute("INSERT OR IGNORE INTO approvals (expense_id, tenant, created_at)"
                  " VALUES (?,?,?)", (expense_id, tenant, now()))
        signed = [r["tenant"] for r in c.execute(
            "SELECT tenant FROM approvals WHERE expense_id=? ORDER BY id", (expense_id,))]
        waiting = [t for t in required if t not in signed]

        if not waiting:
            c.execute("UPDATE expenses SET status='posted', approved_by=?, approved_at=?"
                      " WHERE id=?", (", ".join(signed), now(), expense_id))
            return {"expense_id": expense_id, "status": "posted", "signatures": signed,
                    "required": required, "approved_by": ", ".join(signed)}

    return {"expense_id": expense_id, "status": "pending", "signatures": signed,
            "required": required, "waiting_on": waiting,
            "message": f"{len(signed)} of {len(required)} signatures. Still waiting on "
                       + " and ".join(waiting) + "."}


def signatures(expense_id: int) -> list[str]:
    with connect() as c:
        return [r["tenant"] for r in c.execute(
            "SELECT tenant FROM approvals WHERE expense_id=? ORDER BY id", (expense_id,))]


def reject_expense(expense_id: int, tenant: str, reason: str = "") -> dict[str, Any]:
    with connect() as c:
        c.execute("UPDATE expenses SET status='rejected', approved_by=?, approved_at=?"
                  " WHERE id=?", (tenant.strip(), now(), expense_id))
    return {"expense_id": expense_id, "status": "rejected", "by": tenant, "reason": reason}


def balances() -> dict[str, float]:
    """Net position per tenant: positive means the house owes them."""
    policy = load_policy()
    net = {t: 0.0 for t in policy.tenants}
    with connect() as c:
        for row in c.execute(
            "SELECT e.paid_by, e.total, e.id FROM expenses e WHERE e.status='posted'"
        ):
            net[row["paid_by"]] = round(net.get(row["paid_by"], 0) + row["total"], 2)
        for row in c.execute(
            "SELECT s.tenant, s.amount FROM shares s JOIN expenses e ON e.id=s.expense_id"
            " WHERE e.status='posted'"
        ):
            net[row["tenant"]] = round(net.get(row["tenant"], 0) - row["amount"], 2)
    return {k: round(v, 2) for k, v in net.items()}


def settle_up() -> list[dict[str, Any]]:
    """The fewest payments that clear the board.

    Greedy largest-debtor to largest-creditor. For four people this is the
    minimum number of transfers, and it keeps the settle-up message short
    enough that people actually act on it.
    """
    net = balances()
    owe = sorted(((t, -v) for t, v in net.items() if v < -0.005), key=lambda x: -x[1])
    due = sorted(((t, v) for t, v in net.items() if v > 0.005), key=lambda x: -x[1])
    payments, i, j = [], 0, 0
    while i < len(owe) and j < len(due):
        (d, amt_d), (cr, amt_c) = owe[i], due[j]
        amt = round(min(amt_d, amt_c), 2)
        if amt > 0.005:
            payments.append({"from": d, "to": cr, "amount": amt})
        owe[i] = (d, round(amt_d - amt, 2))
        due[j] = (cr, round(amt_c - amt, 2))
        if owe[i][1] <= 0.005:
            i += 1
        if due[j][1] <= 0.005:
            j += 1
    return payments


def list_expenses(status: str | None = None) -> list[dict[str, Any]]:
    policy = load_policy()
    with connect() as c:
        sql = "SELECT * FROM expenses"
        rows = (c.execute(sql + " WHERE status=? ORDER BY id DESC", (status,))
                if status else c.execute(sql + " ORDER BY id DESC")).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            d["personal_items"] = json.loads(d.get("personal_items") or "[]")
            d["line_items"] = json.loads(d.get("line_items") or "[]")
            d["assignments"] = json.loads(d.get("assignments") or "{}")
            d["shares"] = [dict(s) for s in c.execute(
                "SELECT tenant, amount, basis FROM shares WHERE expense_id=?", (r["id"],))]
            d["signatures"] = [a["tenant"] for a in c.execute(
                "SELECT tenant FROM approvals WHERE expense_id=? ORDER BY id", (r["id"],))]
            d["signers_required"] = policy.signers_required(d["shares"], d["paid_by"])
            out.append(d)
    return out


def resplit(expense_id: int, expense: Expense, assignments: dict[str, str]) -> dict[str, Any]:
    """Replace an expense's shares after a tenant says who had what.

    The charge itself does not move — same merchant, same total, same paper.
    Only the division changes, and the new one is written whole rather than
    patched, so a second pass cannot leave half the old split behind.
    """
    with connect() as c:
        row = c.execute("SELECT status FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if row is None:
            raise ValueError(f"No expense {expense_id}.")
        c.execute("DELETE FROM shares WHERE expense_id=?", (expense_id,))
        for sh in expense.shares:
            c.execute("INSERT INTO shares (expense_id, tenant, amount, basis)"
                      " VALUES (?,?,?,?)",
                      (expense_id, sh.tenant, sh.amount, sh.basis))
        status = row["status"]
        if status in ("pending", "posted"):
            status = "pending" if expense.needs_approval else "posted"
        c.execute("UPDATE expenses SET reasoning=?, split_rule=?, category=?,"
                  " assignments=?, needs_approval=?, status=? WHERE id=?",
                  (expense.reasoning, expense.split_rule, expense.category,
                   json.dumps(assignments), int(expense.needs_approval), status,
                   expense_id))
    return {"expense_id": expense_id, "status": status,
            "shares": [sh.model_dump() for sh in expense.shares]}


def record_dispute(expense_id: int, raised_by: str, claim: str) -> int:
    with connect() as c:
        c.execute("UPDATE expenses SET status='disputed' WHERE id=? AND status!='rejected'",
                  (expense_id,))
        cur = c.execute(
            "INSERT INTO disputes (expense_id, raised_by, claim, created_at)"
            " VALUES (?,?,?,?)", (expense_id, raised_by, claim, now()))
    return int(cur.lastrowid)


def rule_on_dispute(dispute_id: int, ruling: str, clause: str, explanation: str,
                    decided_by: str = "house counsel") -> dict[str, Any]:
    with connect() as c:
        c.execute("UPDATE disputes SET ruling=?, clause=?, explanation=?, decided_by=?"
                  " WHERE id=?", (ruling, clause, explanation, decided_by, dispute_id))
        row = c.execute("SELECT expense_id FROM disputes WHERE id=?", (dispute_id,)).fetchone()
        if row and ruling == "rejected":
            c.execute("UPDATE expenses SET status='posted' WHERE id=?", (row["expense_id"],))
    return {"dispute_id": dispute_id, "ruling": ruling, "clause": clause}


def list_disputes() -> list[dict[str, Any]]:
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT d.*, e.merchant, e.total FROM disputes d"
            " JOIN expenses e ON e.id=d.expense_id ORDER BY d.id DESC")]


def save_draft(recipient: str, subject: str, body: str, about: str = "") -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO drafts (recipient, subject, body, about, created_at, status)"
            " VALUES (?,?,?,?,?,'on_board')", (recipient, subject, body, about, now()))
    return int(cur.lastrowid)


def list_drafts() -> list[dict[str, Any]]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM drafts ORDER BY id DESC")]


def save_message(tenant: str, role: str, text: str, data: dict | None = None) -> int:
    """One turn of the Ask the house conversation, kept per tenant."""
    with connect() as c:
        cur = c.execute(
            "INSERT INTO messages (tenant, role, text, data, created_at) VALUES (?,?,?,?,?)",
            (tenant, role, text, json.dumps(data or {}), now()))
    return int(cur.lastrowid)


def list_messages(tenant: str, limit: int = 40) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE tenant=? ORDER BY id DESC LIMIT ?",
            (tenant, limit)).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["data"] = json.loads(d.get("data") or "{}")
        out.append(d)
    return out


def clear_messages(tenant: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM messages WHERE tenant=?", (tenant,))


def reset() -> None:
    """Clear the ledger back to an empty house."""
    if LEDGER_DB.exists():
        LEDGER_DB.unlink()
    with connect():
        pass
