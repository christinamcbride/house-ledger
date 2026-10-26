"""Who is using the ledger, and whether they are allowed to say yes.

A dropdown labelled "signed off by" is not an approval. Anybody looking at
the screen could pick any of the four names and post a charge against the
other three, which is exactly the thing section 6 exists to prevent. So the
app knows who is logged in, and an approval is recorded against that person.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt, from the
standard library, so there is nothing to install and nothing to go stale. The
stored form is `pbkdf2_sha256$iterations$salt$hash`, and the plaintext is
never written anywhere — not to the database, not to the audit trail.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import ledger
from models import TENANTS

ITERATIONS = 240_000
SESSION_DAYS = 14

#: Demo accounts. The teaching staff needs to get in, and this project has no
#: sign-up flow — four people who already share a lease do not need one. The
#: README prints these; a real house would set them at move-in.
DEMO_PASSWORD = os.environ.get("HOUSE_DEMO_PASSWORD", "statestreet")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, AttributeError):
        return False
    # Constant time, so a wrong password takes as long as a right one.
    return hmac.compare_digest(dk.hex(), want)


def ensure_accounts() -> None:
    """Make sure the four tenants can log in. Never changes an existing one."""
    with ledger.connect() as c:
        for name in TENANTS:
            row = c.execute("SELECT name FROM tenants WHERE name=?", (name,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO tenants (name, password_hash, created_at) VALUES (?,?,?)",
                    (name, hash_password(DEMO_PASSWORD), ledger.now()))


def login(name: str, password: str) -> str | None:
    """Return a session token, or None. Says nothing about which half failed."""
    with ledger.connect() as c:
        row = c.execute("SELECT name, password_hash FROM tenants WHERE name=?",
                        (name,)).fetchone()
    if row is None or not check_password(password, row["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
               ).isoformat(timespec="seconds")
    with ledger.connect() as c:
        c.execute("INSERT INTO sessions (token, tenant, created_at, expires_at)"
                  " VALUES (?,?,?,?)", (token, row["name"], ledger.now(), expires))
    return token


def whoami(token: str | None) -> str | None:
    """The tenant behind a session token, if it is still good."""
    if not token:
        return None
    with ledger.connect() as c:
        row = c.execute("SELECT tenant, expires_at FROM sessions WHERE token=?",
                        (token,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] < datetime.now(timezone.utc).isoformat(timespec="seconds"):
        logout(token)
        return None
    return row["tenant"]


def logout(token: str | None) -> None:
    if not token:
        return
    with ledger.connect() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def set_password(name: str, password: str) -> dict[str, Any]:
    with ledger.connect() as c:
        c.execute("UPDATE tenants SET password_hash=? WHERE name=?",
                  (hash_password(password), name))
        c.execute("DELETE FROM sessions WHERE tenant=?", (name,))
    return {"tenant": name, "changed": True, "sessions_cleared": True}
