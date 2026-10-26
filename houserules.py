"""The house agreement, parsed into a policy the ledger can execute.

This is the idea the project rests on. Splitwise and every app like it make
you pick a split from a dropdown. Here the split comes from the document the
house actually signed: the roster and its percentage shares, the category
table, the shared grocery list, the approval thresholds, the things that are
never split.

It is also the safety design. Homework 6 taught that a model asked to both
read a document *and* decide what it means can be argued out of the right
answer by the document itself. So the model only reads. Everything below is
lookups and arithmetic over the signed agreement, and a doctored receipt has
nothing to argue with.

Parsed once from `data/house_agreement.md` and cached, so a change to the
agreement changes the behaviour of the app with no code edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from config import HOUSE_AGREEMENT
from models import Category, Expense, LineItem, Share, SplitRule, purchased_items

#: Categories the agreement says are never divided.
NEVER_SPLIT: set[str] = {"personal", "guest", "fine"}

#: Section 6 exempts rent and the lease's recurring utilities from approval:
#: the house agreed to them at signing.
PRE_AGREED: set[str] = {"rent", "utilities"}


@dataclass(frozen=True)
class Policy:
    shares: dict[str, float]              # tenant -> fractional share
    category_rule: dict[str, SplitRule]   # category -> how it splits
    shared_groceries: tuple[str, ...]
    approval_over: float
    unanimous_over: float
    rent_total: float
    settle_threshold: float
    tenants: tuple[str, ...] = field(default=())

    def rule_for(self, category: str) -> SplitRule:
        return self.category_rule.get(category, "evenly")

    def signers_required(self, shares: list, payer: str) -> list[str]:
        """Who has to say yes before this charge counts.

        Section 6: everyone the charge actually falls on. For an ordinary
        four-way bill that is the other three; for a takeaway where two
        people ate, it is the one other person who ate. Paying is the
        buyer's own yes, so the buyer is never on this list, and nobody is
        asked to sign off money they are not paying.
        """
        owing = []
        for sh in shares:
            tenant = sh["tenant"] if isinstance(sh, dict) else sh.tenant
            amount = sh["amount"] if isinstance(sh, dict) else sh.amount
            if tenant != payer and abs(amount) > 0.005 and tenant not in owing:
                owing.append(tenant)
        return sorted(owing)


def _money(text: str) -> float | None:
    m = re.search(r"\$([\d,]+(?:\.\d{2})?)", text)
    return float(m.group(1).replace(",", "")) if m else None


@lru_cache
def load_policy() -> Policy:
    """Read the signed agreement and turn it into executable policy."""
    text = HOUSE_AGREEMENT.read_text(encoding="utf-8")

    # --- section 1: the roster and its shares ---
    roster = text.split("## 1. Who lives here")[1].split("## 2.")[0]
    shares: dict[str, float] = {}
    for row in re.finditer(r"^\|\s*(\w+)\s*\|[^|]*\|\s*(\d+)%\s*\|", roster, re.M):
        shares[row.group(1)] = int(row.group(2)) / 100

    # --- section 3: what splits how ---
    cat_block = text.split("## 3. How expenses are split")[1].split("## 4.")[0]
    rules: dict[str, SplitRule] = {}
    for row in cat_block.splitlines():
        cells = [c.strip().strip("*") for c in row.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0].lower().startswith(("category", "---")):
            continue
        name = cells[0].lower()
        how = cells[1].lower()
        key = (
            "rent" if "rent" in name
            else "utilities" if "utilit" in name
            else "groceries" if "grocer" in name
            else "food" if "food" in name
            else "household_supplies" if "household" in name
            else "furniture" if "furniture" in name
            else "repairs" if "repair" in name
            else "social" if "social" in name
            else "personal" if "personal" in name
            else "guest" if "guest" in name
            else "fine" if "fine" in name
            else None
        )
        if key is None:
            continue
        rules[key] = (
            "by_share" if "share" in how
            else "by_item" if "item" in how
            else "not_split" if "not split" in how
            else "evenly"
        )

    # --- section 4: the shared grocery list ---
    gro = text.split("## 4. The shared grocery list")[1].split("## 5.")[0]
    body = gro.split("evenly:")[1].split("Anything not on this list")[0]
    staples = tuple(
        s.strip().lower() for s in re.split(r"[,\n]", body) if s.strip()
    )

    # --- thresholds ---
    approvals = text.split("## 6. Approvals")[1].split("## 7.")[0]
    approval_over = _money(approvals) or 75.0
    # Section 6 sets the bar for agreement. If it names a figure, below that
    # figure one housemate signing is enough; if it says three out of four is
    # never agreement, the bar is the whole house at any amount — which is
    # what a zero here means, since every expense is over zero.
    unan = re.search(r"[Tt]hree out of four is not agreement for anything over \$([\d,]+)",
                     approvals)
    if unan:
        unanimous_over = float(unan.group(1).replace(",", ""))
    elif re.search(r"[Tt]hree out of four is not agreement", approvals):
        unanimous_over = 0.0
    else:
        unanimous_over = 300.0
    rent = _money(text.split("## 2. The lease")[1].split("## 3.")[0]) or 0.0
    settle = _money(text.split("## 5. Settling up")[1].split("## 6.")[0]) or 150.0

    if not shares or not rules or not staples:
        raise RuntimeError("The house agreement did not parse; refusing to split anything.")

    return Policy(
        shares=shares,
        category_rule=rules,
        shared_groceries=staples,
        approval_over=approval_over,
        unanimous_over=unanimous_over,
        rent_total=rent,
        settle_threshold=settle,
        tenants=tuple(shares),
    )


# ---------------------------------------------------------------------------
# Categorising, from the agreement rather than from a guess
# ---------------------------------------------------------------------------

MERCHANT_HINTS: list[tuple[str, Category]] = [
    (r"elm city properties|landlord|rent", "rent"),
    (r"united illuminating|eversource|avangrid|southern connecticut gas|water|frontier|comcast|xfinity|optimum", "utilities"),
    (r"stop & ?shop|trader joe|whole foods|elm city market|ferraro|grocer|market|aldi|costco", "groceries"),
    (r"home depot|lowe|ace hardware|hardware|target|cvs|walgreens", "household_supplies"),
    (r"ikea|wayfair|furniture|mattress", "furniture"),
    (r"plumb|electric(?:ian)?|hvac|repair|handyman", "repairs"),
    (r"parking|violation|ticket|tow(?:ing)?|citation|fine|penalty", "fine"),
    # Section 4: a restaurant or takeaway is nobody's house staple. It is
    # personal unless the house agreed to it beforehand as a house dinner,
    # and "we agreed" is a thing tenants say, not a thing a receipt knows.
    (r"mcdonald|burger|pizza|chipotle|starbucks|dunkin|subway|taco|wendy|"
     r"popeyes|chick-?fil|shake shack|sweetgreen|panera|cafe|caf\u00e9|coffee|"
     r"restaurant|kitchen|grill|diner|deli|sushi|thai|ramen|doordash|uber eats|"
     r"grubhub|seamless|bar &|tavern|pub\b", "food"),
]


def categorise(merchant: str | None, line_items: list[LineItem]) -> tuple[Category, str]:
    """Pick the category, and say what in the document decided it."""
    blob = (merchant or "").lower()
    for pattern, cat in MERCHANT_HINTS:
        if re.search(pattern, blob):
            return cat, f"merchant '{merchant}' matches the {cat.replace('_',' ')} category"

    policy = load_policy()
    names = " ".join((li.description or "").lower() for li in line_items)
    if any(s in names for s in policy.shared_groceries if len(s) > 3):
        return "groceries", "the items are on the shared grocery list in section 4"
    return "unknown", "no merchant or item matched a category in section 3"


def is_shared_grocery(description: str) -> bool:
    """Is this line a house staple, or someone's own shopping?"""
    policy = load_policy()
    d = (description or "").lower()
    return any(s in d for s in policy.shared_groceries if len(s) > 3)


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


def _even(amount: float, tenants: tuple[str, ...]) -> list[Share]:
    """Split evenly, with the rounding remainder handed to the first tenant.

    Four ways into an odd number of cents leaves a cent over. Dropping it
    quietly makes the ledger stop balancing, which is how a finance app loses
    people's trust.
    """
    cents = round(amount * 100)
    base, extra = divmod(cents, len(tenants))
    out = []
    for i, t in enumerate(tenants):
        c = base + (1 if i < extra else 0)
        out.append(Share(tenant=t, amount=round(c / 100, 2),
                         basis=f"even split, {len(tenants)} ways"))
    return out


def _by_share(amount: float, policy: Policy) -> list[Share]:
    cents = round(amount * 100)
    out, assigned = [], 0
    names = list(policy.shares)
    for i, t in enumerate(names):
        c = (cents - assigned) if i == len(names) - 1 else round(cents * policy.shares[t])
        assigned += c
        out.append(Share(tenant=t, amount=round(c / 100, 2),
                         basis=f"{policy.shares[t]:.0%} room share, section 1"))
    return out


#: What a tenant picks when an item belongs to everybody.
HOUSE = "the house"


def owners_of(value, tenants: tuple[str, ...]) -> list[str]:
    """Who an item belongs to, from whatever the dashboard sent.

    A tenant may be one name, several names, or the house. Several names is
    the ordinary case people forget to build for: two housemates split a
    bag of coffee, and the other two had none of it.
    """
    if value is None or value == HOUSE:
        return list(tenants)
    if isinstance(value, str):
        return [value] if value in tenants else list(tenants)
    named = [v for v in value if v in tenants]
    return named or list(tenants)


def split_by_items(
    *,
    merchant: str | None,
    total: float,
    paid_by: str,
    line_items: list[LineItem],
    assignments: dict[str, object],
    category: Category,
    why: str,
    purchase_date: str | None = None,
    source_document: str | None = None,
) -> Expense:
    """Split a receipt line by line, because one bill often has four owners.

    This is the case a shared flat actually runs into: one person taps their
    card at the counter and three people eat. Splitting the total four ways
    is wrong, and leaving it all on the payer is wrong too.

    Each line is divided evenly between whoever had it. Items nobody claims
    belong to the house. Tax and fees are never on a line, so they are
    apportioned in the same proportion as the items they were charged on —
    the alternative is shares that do not add up to what was paid.
    """
    policy = load_policy()
    items = purchased_items(line_items)
    if not items:
        raise ValueError("There are no priced items on this receipt to assign.")

    owed: dict[str, float] = {t: 0.0 for t in policy.tenants}
    # Listing all seven items against all four names makes a sentence nobody
    # reads. What a tenant wants to know is what is theirs alone and what
    # they split with somebody — the rest is just "the usual shared stuff".
    named: dict[str, list[str]] = {t: [] for t in policy.tenants}
    whole_house = {t: 0 for t in policy.tenants}
    shared_total = 0.0

    for li in items:
        who = owners_of(assignments.get(li.description or ""), policy.tenants)
        each = li.amount / len(who)
        if len(who) > 1:
            shared_total += li.amount
        everyone = len(who) == len(policy.tenants)
        for t in who:
            owed[t] += each
            if everyone:
                whole_house[t] += 1
            else:
                named[t].append((li.description or "item") if len(who) == 1
                                else f"{li.description or 'item'} (split {len(who)} ways)")

    def describe(t: str) -> str:
        bits = list(named[t])
        if whole_house[t]:
            bits.append(f"a quarter of {whole_house[t]} shared item"
                        + ("s" if whole_house[t] > 1 else ""))
        return ", ".join(bits)

    basis = {t: describe(t) for t in policy.tenants}

    items_total = round(sum(li.amount for li in items), 2)
    residual = round(total - items_total, 2)      # tax, fees, a bag charge
    if items_total > 0 and abs(residual) > 0.005:
        for t in owed:
            owed[t] += residual * (owed[t] / items_total)

    shares_map = {t: round(owed[t], 2) for t in policy.tenants}

    # The cents have to land on the stated total, not near it.
    drift = round(total - sum(shares_map.values()), 2)
    if abs(drift) >= 0.01:
        shares_map[paid_by] = round(shares_map[paid_by] + drift, 2)

    shares = [
        Share(tenant=t, amount=shares_map[t], basis=basis[t] or "nothing on this receipt")
        for t in policy.tenants if abs(shares_map[t]) > 0.005
    ]
    # The sentence only needs the people who had something of their own.
    standout = [t for t in policy.tenants if named[t]]
    return Expense(
        merchant=merchant, purchase_date=purchase_date, total=round(total, 2),
        paid_by=paid_by, category=category, split_rule="by_item",
        shares=shares, personal_items=[],
        needs_approval=round(shared_total, 2) > policy.approval_over,
        source_document=source_document,
        reasoning=(
            f"{why}. Split line by line"
            + (": " + "; ".join(f"{t} has {', '.join(named[t])}" for t in standout)
               if standout else ", evenly — nobody claimed anything individually")
            + (f"; ${residual:.2f} of tax and fees apportioned across the items"
               if abs(residual) > 0.005 else "")
            + "."
        ),
    )


def split_expense(
    *,
    merchant: str | None,
    total: float | None,
    paid_by: str,
    line_items: list[LineItem] | None = None,
    purchase_date: str | None = None,
    source_document: str | None = None,
    category_override: Category | None = None,
    assignments: dict[str, str] | None = None,
) -> Expense:
    """Turn a reading into money owed, strictly by the agreement."""
    policy = load_policy()
    line_items = line_items or []

    if total is None:
        raise ValueError(
            "The document does not state a total. The agreement forbids guessing "
            "one, so this cannot be split until a person supplies it."
        )
    if paid_by not in policy.tenants:
        raise ValueError(f"{paid_by!r} is not a tenant in section 1 of the agreement.")

    if category_override:
        category, why = category_override, "category set by a tenant"
    else:
        category, why = categorise(merchant, line_items)

    # A tenant saying who had what beats every default in the agreement,
    # because it is the only thing here that is first-hand knowledge.
    if assignments:
        return split_by_items(
            merchant=merchant, total=total, paid_by=paid_by, line_items=line_items,
            assignments=assignments, category=category,
            why="items assigned by a tenant", purchase_date=purchase_date,
            source_document=source_document)

    rule = policy.rule_for(category)
    personal: list[LineItem] = []

    # A takeaway with nobody named is the buyer's until somebody says
    # otherwise. Splitting it four ways would charge the house for a meal it
    # did not eat, so it waits for a person to say who had what.
    if rule == "by_item":
        return Expense(
            merchant=merchant, purchase_date=purchase_date, total=round(total, 2),
            paid_by=paid_by, category=category, split_rule="by_item",
            shares=[Share(tenant=paid_by, amount=round(total, 2),
                          basis="nobody has said who had what yet")],
            personal_items=[], needs_approval=True, source_document=source_document,
            reasoning=(f"{why}. Section 3: food is split by item. Until somebody "
                       f"says who had what, the whole bill sits with {paid_by}."),
        )

    # A mixed grocery run is the common real case: house staples plus someone's
    # own shampoo. The agreement says such a receipt is never split in full.
    if category == "groceries" and line_items:
        priced = purchased_items(line_items)
        if priced:
            personal = [li for li in priced if not is_shared_grocery(li.description)]
            shared_items = round(sum(li.amount for li in priced if li not in personal), 2)
            personal_items_total = round(sum(li.amount for li in personal), 2)

            # Tax, bag fees and deposits are on the total but never on a line.
            # Dropping the difference would make the shares stop summing to
            # what was actually paid, which is how a ledger loses trust.
            residual = round(total - shared_items - personal_items_total, 2)
            if abs(residual) > 0.005 and (shared_items + personal_items_total) > 0:
                prop = shared_items / (shared_items + personal_items_total)
                shared_total = round(shared_items + residual * prop, 2)
                excluded_extra = round(residual - (shared_total - shared_items), 2)
            else:
                shared_total = shared_items
                excluded_extra = 0.0

            if personal and shared_total > 0:
                shares = _even(shared_total, policy.tenants)
                excluded = round(personal_items_total + excluded_extra, 2)
                for s in shares:
                    if s.tenant == paid_by:
                        s.amount = round(s.amount + excluded, 2)
                        s.basis += f"; plus ${excluded:.2f} of personal items"
                return Expense(
                    merchant=merchant, purchase_date=purchase_date, total=total,
                    paid_by=paid_by, category=category, split_rule="evenly",
                    shares=shares, personal_items=personal,
                    needs_approval=shared_total > policy.approval_over,
                    source_document=source_document,
                    reasoning=(
                        f"{why}. ${shared_total:.2f} of shared staples split evenly; "
                        f"${excluded:.2f} of personal items stays with {paid_by} "
                        "(section 3: a mixed receipt is never split in full)"
                        + (f"; ${residual:.2f} of tax and fees apportioned between them."
                           if abs(residual) > 0.005 else ".")
                    ),
                )

    if rule == "not_split":
        shares = [Share(tenant=paid_by, amount=round(total, 2),
                        basis=f"{category.replace('_',' ')} is never split")]
        reason = f"{why}. Section 3 and 9: this category is never split."
    elif rule == "by_share":
        shares = _by_share(total, policy)
        reason = f"{why}. Split by room share, section 1."
    else:
        shares = _even(total, policy.tenants)
        reason = f"{why}. Split evenly, section 3."

    if category == "unknown":
        reason += (" Category could not be determined, so the even default applies "
                   "and a tenant has to confirm it before it counts.")

    return Expense(
        merchant=merchant, purchase_date=purchase_date, total=round(total, 2),
        paid_by=paid_by, category=category, split_rule=rule, shares=shares,
        needs_approval=(
            # An unrecognised receipt is never posted quietly, whatever the
            # amount. Splitting something four ways because nothing matched
            # is a guess, and a guess should stop at a person.
            category == "unknown"
            or rule != "not_split"
            and category not in PRE_AGREED
            and total > policy.approval_over
        ),
        source_document=source_document, reasoning=reason,
    )
