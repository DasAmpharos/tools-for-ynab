"""Apply rules to transactions and build the YNAB update patches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tools4ynab.core.client import YnabClient, YnabError

from .rules import Rule, RuleError, SplitRule


@dataclass
class Change:
    txn: dict[str, Any]
    rule: Rule
    patch: dict[str, Any]  # fields to send to YNAB, including `id`
    summary: str


def norm_name(name: str) -> str:
    """Lowercase and drop emoji/punctuation so "Groceries" matches "🛒 Groceries"."""
    name = re.sub(r"[^\w\s:]|_", "", name).lower()
    return re.sub(r"\s*:\s*", ":", re.sub(r"\s+", " ", name)).strip()


def build_category_index(categories: list[dict[str, Any]]) -> dict[str, str]:
    """Map normalized "name" and "group: name" to category ids.
    Names shared by several groups are only reachable via the qualified form."""
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for cat in categories:
        index[norm_name(f"{cat['group_name']}: {cat['name']}")] = cat["id"]
        key = norm_name(cat["name"])
        if key in index:
            ambiguous.add(key)
        index[key] = cat["id"]
    for key in ambiguous:
        del index[key]
    return index


def resolve_categories(
    rules: list[Rule], index: dict[str, str], splits: list[SplitRule] = ()
) -> dict[str, str]:
    """Resolve every category named in the rules up front so typos fail fast."""
    named = [(r.name, r.actions.category) for r in rules if r.actions.category]
    named += [(sp.name, p.category) for sp in splits for p in sp.parts]
    resolved = {}
    for rule_name, name in named:
        cat_id = index.get(norm_name(name))
        if cat_id is None:
            raise RuleError(
                f'{rule_name}: category {name!r} not found (or ambiguous; use "Group: Category")'
            )
        resolved[name] = cat_id
    return resolved


@dataclass
class SplitChange:
    txn: dict[str, Any]
    rule: SplitRule
    patch: dict[str, Any]  # parent fields to send; includes `subtransactions`

    @property
    def summary(self) -> str:
        return "split: " + " + ".join(f"{p.category} {p.amount:.2f}" for p in self.rule.parts)


def plan_splits(
    txns: list[dict[str, Any]],
    splits: list[SplitRule],
    category_ids: dict[str, str],
) -> list[SplitChange]:
    """Turn matching bundled charges into splits. An approved, categorized
    transaction is final and left alone, like everywhere else."""
    changes = []
    for txn in txns:
        if not _eligible(txn):
            continue
        if txn.get("approved") and txn.get("category_id") is not None:
            continue
        rule = next((r for r in splits if r.match.matches(txn)), None)
        if rule is None:
            continue
        sign = -1 if txn["amount"] < 0 else 1
        patch = {
            "category_id": None,  # YNAB requires a null parent category on a split
            "approved": True,
            "subtransactions": [
                {
                    "amount": sign * round(p.amount * 1000),
                    "category_id": category_ids[p.category],
                }
                for p in rule.parts
            ],
        }
        changes.append(SplitChange(txn, rule, patch))
    return changes


def _eligible(txn: dict[str, Any]) -> bool:
    # Splits and transfers can't be meaningfully categorized by a rule.
    return not txn.get("subtransactions") and not txn.get("transfer_account_id")


def plan_changes(
    txns: list[dict[str, Any]],
    rules: list[Rule],
    category_ids: dict[str, str],
    category_names: dict[str, str],
) -> list[Change]:
    """First matching rule wins per transaction. Fields that already hold the
    target value are omitted, and transactions with nothing to change are skipped
    so repeated runs are no-ops."""
    changes = []
    for txn in txns:
        if not _eligible(txn):
            continue
        rule = next((r for r in rules if r.match.matches(txn)), None)
        if rule is None:
            continue

        a = rule.actions
        patch: dict[str, Any] = {}
        notes = []
        # An unapproved transaction's category is only YNAB's auto-categorization
        # guess (last category used for the payee), so rules may replace it.
        # Once approved, the category is final and rules never change it.
        if a.category is not None:
            cat_id = category_ids[a.category]
            current = txn.get("category_id")
            if current != cat_id and (current is None or not txn.get("approved")):
                patch["category_id"] = cat_id
                was = f" (was {category_names[current]})" if current else ""
                notes.append(f"category={category_names[cat_id]}{was}")
        if a.payee is not None and txn.get("payee_name") != a.payee:
            patch["payee_name"] = a.payee
            notes.append(f"payee={a.payee}")
        if a.memo is not None and (txn.get("memo") or "") != a.memo:
            patch["memo"] = a.memo
            notes.append(f"memo={a.memo}")
        if a.flag_color is not None:
            color = None if a.flag_color == "none" else a.flag_color
            if txn.get("flag_color") != color:
                patch["flag_color"] = color
                notes.append(f"flag={a.flag_color}")
        # A rule that sets a category approves by default; approval is what locks
        # the category against later runs. `approve: false` opts out.
        approve = a.approve if a.approve is not None else (True if a.category else None)
        if approve is not None and txn.get("approved") != approve:
            patch["approved"] = approve
            notes.append(f"approved={approve}")

        if patch:
            patch["id"] = txn["id"]
            changes.append(Change(txn, rule, patch, ", ".join(notes)))
    return changes


def apply_split(client: YnabClient, change: SplitChange) -> str:
    """PUT the split, then confirm YNAB actually created the subtransactions."""
    updated = client.update_transaction(change.txn, change.patch)
    got = sorted(s["amount"] for s in updated.get("subtransactions") or [])
    want = sorted(s["amount"] for s in change.patch["subtransactions"])
    if got != want:
        raise YnabError(
            f"{change.rule.name}: YNAB did not create the expected split "
            f"({len(got)} subtransaction(s) came back)"
        )
    return "split and approved"
