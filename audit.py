"""Append-only logs: what the agents did, what it cost, what was blocked.

JSON arrays rather than JSON Lines, because the dashboard reads them straight
and a house ledger is never high-throughput. A failed write must never take a
run down — losing the log is bad, losing the run is worse.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import AUDIT_TRAIL, BLOCKED_ACTIONS, OUTPUT, USAGE_LOG

_LOCK = threading.Lock()
LOGS = (AUDIT_TRAIL, USAGE_LOG, BLOCKED_ACTIONS)

#: Never written to a log, at any nesting depth. A house ledger holds other
#: people's money and addresses; the audit trail does not need either.
REDACT = {"api_key", "portkey_api_key", "openai_api_key", "authorization",
          "token", "secret", "password", "email", "phone", "address",
          "venmo", "card", "account_number"}

MAX_CHARS = 240


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_logs() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for p in LOGS:
        if not p.exists():
            p.write_text("[]\n", encoding="utf-8")


def scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {k: ("[redacted]" if k.lower() in REDACT else scrub(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [scrub(v, depth + 1) for v in list(value)[:10]]
        if len(value) > 10:
            items.append(f"... {len(value) - 10} more")
        return items
    if isinstance(value, str) and len(value) > MAX_CHARS:
        return value[:MAX_CHARS] + f"... [{len(value)} chars]"
    return value


def append(path: Path, entry: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except (FileNotFoundError, json.JSONDecodeError):
            existing = []
        existing.append(entry)
        with _LOCK:
            path.write_text(json.dumps(existing, indent=2, default=str) + "\n",
                            encoding="utf-8")
    except Exception:
        pass


def record(event: str, **fields: Any) -> None:
    append(AUDIT_TRAIL, {"time": now(), "event": event,
                         **{k: scrub(v) for k, v in fields.items() if v is not None}})


def record_blocked(tool: str, arguments: dict, run_id: str, document: str | None,
                   why: str) -> None:
    append(BLOCKED_ACTIONS, {
        "time": now(), "tool": tool, "arguments": scrub(arguments),
        "run_id": run_id, "document": document, "outcome": "blocked — nothing happened",
        "why": why,
    })


def record_usage(**fields: Any) -> None:
    append(USAGE_LOG, {"time": now(), **fields})


def read(path: Path, limit: int | None = None) -> list[dict]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return rows[-limit:] if limit else rows
