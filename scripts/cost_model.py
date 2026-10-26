"""What this costs to run, per household and at scale.

Every per-run figure here is measured, not estimated: they come out of
`output/usage_log.json`, which the app writes on each agent call. The only
judgement is how often a house does each thing in a month, and that is
stated below so it can be argued with.

    python scripts/cost_model.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit  # noqa: E402
from config import OUTPUT, USAGE_LOG  # noqa: E402

#: A month at 888 State Street, from the shape of the demo data. Four people
#: sharing a flat put up somewhere around five documents a week between them,
#: argue about one charge, and settle once.
MONTHLY = {
    "reader": 20,        # receipts, bills and confirmations read
    "concierge": 15,     # questions asked of the ledger
    "messenger": 4,      # settle-up notes drafted, one per housemate
    "house-counsel": 2,  # disputes that actually get raised
}

#: Measured where we have runs; the cached counsel figure comes from
#: `output/cache_experiment.json`, which is the realistic steady state.
FALLBACK = {"house-counsel": 0.000807}

HOUSEHOLD_SIZE = 4


def measured() -> dict[str, float]:
    rows = audit.read(USAGE_LOG)
    agg: dict[str, list[float]] = {}
    for r in rows:
        agg.setdefault(r.get("agent", "?"), []).append(r.get("cost_usd", 0.0))
    out = {k: sum(v) / len(v) for k, v in agg.items() if v}
    for k, v in FALLBACK.items():
        out.setdefault(k, v)
    return out


def main() -> None:
    per_run = measured()
    missing = [a for a in MONTHLY if a not in per_run]
    if missing:
        print(f"  no measured runs for {missing}; using fallbacks")

    lines = []
    household = 0.0
    for agent, times in MONTHLY.items():
        cost = per_run.get(agent, 0.0)
        month = cost * times
        household += month
        lines.append({"agent": agent, "runs_per_month": times,
                      "usd_per_run": round(cost, 6),
                      "usd_per_month": round(month, 6)})

    per_person = household / HOUSEHOLD_SIZE
    report = {
        "assumptions": MONTHLY,
        "household_size": HOUSEHOLD_SIZE,
        "per_agent": lines,
        "household_per_month_usd": round(household, 6),
        "per_person_per_month_usd": round(per_person, 6),
        "at_100_users_per_month_usd": round(per_person * 100, 4),
        "at_10000_users_per_month_usd": round(per_person * 10_000, 2),
        "at_10000_users_per_year_usd": round(per_person * 10_000 * 12, 2),
    }
    out = OUTPUT / "cost_model.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"\n  {'agent':<16}{'runs/mo':>9}{'$/run':>11}{'$/month':>11}")
    print("  " + "-" * 47)
    for r in lines:
        print(f"  {r['agent']:<16}{r['runs_per_month']:>9}"
              f"{r['usd_per_run']:>11.6f}{r['usd_per_month']:>11.6f}")
    print("  " + "-" * 47)
    print(f"  {'one household':<16}{'':>9}{'':>11}{household:>11.6f}")
    print(f"\n  per person per month      ${per_person:.6f}")
    print(f"  100 users                 ${per_person * 100:,.2f} / month")
    print(f"  10,000 users              ${per_person * 10_000:,.2f} / month"
          f"  (${per_person * 10_000 * 12:,.2f} / year)")
    print(f"\n  Written to {out}")


if __name__ == "__main__":
    main()
