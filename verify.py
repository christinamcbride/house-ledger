"""Checks on a reading, before a cent of it reaches the ledger.

Cleaning a document only helps when there is something hidden to remove. A
photographed receipt has no hidden layer — the model sees what a person sees,
so the usual trick of stripping invisible text has nothing to strip.

The attack that matters here is different, and it is the obvious one for a
house ledger: a roommate alters the total. $46.25 becomes $146.25, everyone
pays a quarter of the difference, and nobody checks the paper.

So the defence is arithmetic, not sanitising. A receipt's line items have to
add up to the total it claims. They almost always do, within tax — and when
they do not by a wide margin, that is worth a person's eyes before the money
moves. None of these checks reject anything on their own. They attach a flag,
and the ledger decides what needs approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from models import ReceiptReading, purchased_items

Severity = Literal["info", "warn", "block"]

#: Sales tax in Connecticut is 6.35%, and receipts also carry bag fees and
#: deposits. A gap wider than this is not rounding.
TAX_TOLERANCE = 0.18

#: Phrases that address the reader as a system rather than describe a purchase.
DIRECTIVES = re.compile(
    r"\b(ignore (?:the |all |any )?(?:previous|prior|above)|"
    r"disregard|override|bypass|"
    r"you are an? (?:ai|assistant|agent)|system prompt|"
    r"mark (?:this|it) as|split (?:this )?(?:evenly|four ways)|"
    r"add (?:this|it) to (?:the )?(?:house|shared)|"
    r"send (?:a )?(?:reminder|venmo|message)|request payment|"
    r"charge (?:the )?(?:house|everyone|roommates)|"
    r"do not (?:flag|tell|show)|approved by|pre-?approved)\b",
    re.I,
)

#: Claims that the document itself changes the signed agreement.
AMENDMENT = re.compile(
    r"\b(house (?:rule|agreement|policy) (?:is |now |says )|"
    r"per the (?:new |updated )?(?:house )?agreement|"
    r"the roommates agreed|everyone agreed|as agreed by the house|"
    r"house agreed|we all agreed|agreed by all|"
    r"this (?:supersedes|replaces|amends)|new split|revised (?:split|rule))\b",
    re.I,
)


@dataclass
class Flag:
    code: str
    severity: Severity
    message: str


def _sum_lines(reading: ReceiptReading) -> float | None:
    priced = [li.amount for li in purchased_items(reading.line_items)]
    return round(sum(priced), 2) if priced else None


def check(reading: ReceiptReading, raw_text: str = "") -> list[Flag]:
    """Everything worth knowing about a reading before it becomes money."""
    flags: list[Flag] = []

    # --- 1. does the paper add up? ---
    line_sum = _sum_lines(reading)
    if reading.total is None:
        flags.append(Flag(
            "no_total", "block",
            "The document does not state a total. The agreement forbids guessing one, "
            "so a tenant has to supply it."))
    elif line_sum is not None and len(purchased_items(reading.line_items)) >= 2:
        gap = round(reading.total - line_sum, 2)
        ratio = gap / line_sum if line_sum else 0
        if gap < -0.02:
            flags.append(Flag(
                "total_below_lines", "warn",
                f"The items add up to ${line_sum:.2f} but the total says "
                f"${reading.total:.2f} — ${abs(gap):.2f} less than the lines. "
                "A discount would explain it; so would an edit."))
        elif ratio > TAX_TOLERANCE:
            flags.append(Flag(
                "total_above_lines", "warn",
                f"The items add up to ${line_sum:.2f} but the total says "
                f"${reading.total:.2f}. That is ${gap:.2f} more than the lines, "
                f"{ratio:.0%} on top — far beyond tax. Check the paper before "
                "this is split."))

    # --- 2. is the document talking to the agent? ---
    haystack = " ".join([raw_text] + reading.concerns + [
        li.description for li in reading.line_items if li.description])
    if (m := DIRECTIVES.search(haystack)):
        flags.append(Flag(
            "instruction_in_document", "warn",
            f"The document contains an instruction aimed at the reader "
            f"(“{m.group(0)}”). A receipt describes a purchase; it does not "
            "get to say how the house splits it."))

    # --- 3. does it claim to change the agreement? ---
    if (m := AMENDMENT.search(haystack)):
        flags.append(Flag(
            "claims_to_amend", "warn",
            f"The document claims a house rule (“{m.group(0)}”). Only the "
            "signed agreement sets the split, and only all four tenants change it."))

    # --- 4. did the model flag something itself? ---
    for c in reading.concerns:
        flags.append(Flag("model_concern", "info", c))

    return flags


def worst(flags: list[Flag]) -> Severity:
    for level in ("block", "warn", "info"):
        if any(f.severity == level for f in flags):
            return level  # type: ignore[return-value]
    return "info"
