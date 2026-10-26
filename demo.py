"""One ordinary month at 888 State Street, for anyone opening this cold.

No agent runs here and nothing is invented: every expense goes through
`houserules.split_expense`, the same function the app calls once a document
has been read. A grader opening the dashboard sees a working house rather
than an empty table, and the history they see was produced by the agreement
rather than typed in by hand.
"""

from __future__ import annotations

import houserules
import ledger
from models import LineItem

OCTOBER = [
    dict(merchant="Elm City Properties — October rent", purchase_date="2026-10-01",
         total=3600.00, paid_by="Christina", category_override="rent"),
    dict(merchant="United Illuminating", purchase_date="2026-10-08",
         total=142.80, paid_by="Sophia", category_override="utilities"),
    dict(merchant="Frontier Internet", purchase_date="2026-10-08",
         total=79.99, paid_by="Aletta", category_override="utilities"),
    dict(merchant="Stop & Shop", purchase_date="2026-10-12", total=46.25,
         paid_by="Angelika", line_items=[
             LineItem(description="Milk, 1 gal", amount=4.29),
             LineItem(description="Eggs, dozen", amount=5.49),
             LineItem(description="Bread", amount=3.99),
             LineItem(description="Bananas", amount=2.14),
             LineItem(description="Pasta x3", amount=5.37),
             LineItem(description="Coffee, ground", amount=11.99),
             LineItem(description="Paper towels, 6pk", amount=9.99),
         ]),
    dict(merchant="Target — Amity", purchase_date="2026-10-15", total=38.40,
         paid_by="Christina", category_override="household_supplies", line_items=[
             LineItem(description="Dish soap", amount=4.49),
             LineItem(description="Sponges, 6pk", amount=3.99),
             LineItem(description="Bin bags, 40ct", amount=12.99),
             LineItem(description="Laundry detergent", amount=14.49),
         ]),
    dict(merchant="IKEA New Haven", purchase_date="2026-10-19", total=240.00,
         paid_by="Sophia", category_override="furniture", line_items=[
             LineItem(description="KALLAX shelving unit", amount=129.00),
             LineItem(description="Floor lamp", amount=49.00),
             LineItem(description="Rug, 5x7", amount=62.00),
         ]),
]




def seed(reset: bool = True) -> list[int]:
    """Put October on the ledger. Returns the ids created."""
    if reset:
        ledger.reset()
    ids = []
    for row in OCTOBER:
        expense = houserules.split_expense(source_document="seeded", **row)
        ids.append(ledger.record_expense(expense, line_items=row.get("line_items")))
    return ids
