"""Put the demo month on the ledger.

    python scripts/seed_ledger.py

The data itself lives in `demo.py`, because the Reset button in the app
restores the same month and the two must not drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth  # noqa: E402
import demo  # noqa: E402
import ledger  # noqa: E402


def main() -> None:
    for eid in demo.seed():
        e = [x for x in ledger.list_expenses() if x["id"] == eid][0]
        state = "waiting on a person" if e["needs_approval"] else "posted"
        print(f"  #{eid}  {(e['merchant'] or '?'):<42} ${e['total']:>8,.2f}  "
              f"{e['category']:<18} {e['split_rule']:<10} {state}")
    auth.ensure_accounts()

    print("\n  Balances")
    for tenant, amount in ledger.balances().items():
        print(f"    {tenant:<10} {'is owed' if amount > 0 else 'owes   '} ${abs(amount):>8,.2f}")
    print("\n  Settle up")
    for p in ledger.settle_up():
        print(f"    {p['from']} -> {p['to']}  ${p['amount']:,.2f}")


if __name__ == "__main__":
    main()
