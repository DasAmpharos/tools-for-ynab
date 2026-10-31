"""Plan copying one month's assigned amounts into another month, as a starting point."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

# YNAB's group for "Inflow: Ready to Assign"; nothing in it is assigned by hand.
INTERNAL_GROUP = "Internal Master Category"


def next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _money(milli: int) -> str:
    return f"{milli / 1000:,.2f}"


@dataclass(frozen=True)
class Item:
    label: str  # "Group: Category"
    category_id: str
    amount: int  # milliunits assigned in the source month
    existing: int  # milliunits already assigned in the target month


@dataclass
class CopyPlan:
    source: date
    target: date
    copy: list[Item] = field(default_factory=list)
    kept: list[Item] = field(default_factory=list)  # target already has its own amount
    unchanged: int = 0  # target already matches the source

    def summary(self) -> str:
        parts = [f"{len(self.copy)} to copy"]
        if self.kept:
            parts.append(f"{len(self.kept)} kept (already assigned in {self.target:%B})")
        if self.unchanged:
            parts.append(f"{self.unchanged} already match")
        return ", ".join(parts)

    def lines(self) -> list[str]:
        width = max((len(i.label) for i in self.copy + self.kept), default=0)
        out = []
        for i in self.copy:
            note = f"  (replaces {_money(i.existing)})" if i.existing else ""
            out.append(f"  {i.label:<{width}} {_money(i.amount):>12}{note}")
        if self.kept:
            out.append(f"Kept, because {self.target:%B} already has an amount (--overwrite):")
            for i in self.kept:
                out.append(
                    f"  {i.label:<{width}} {_money(i.existing):>12}"
                    f"  ({self.source:%B}: {_money(i.amount)})"
                )
        return out


def plan_copy(
    source: date,
    target: date,
    categories: list[dict[str, Any]],
    source_assigned: dict[str, int],
    target_assigned: dict[str, int],
    overwrite: bool = False,
) -> CopyPlan:
    """What to assign in `target`: every visible category with an amount in `source`.

    Categories with nothing assigned in `source` are left alone (never zeroed), and ones
    that already have a different amount in `target` are kept unless `overwrite`."""
    plan = CopyPlan(source, target)
    for cat in categories:
        if cat["hidden"] or cat["group_name"] == INTERNAL_GROUP:
            continue
        amount = source_assigned.get(cat["id"], 0)
        existing = target_assigned.get(cat["id"], 0)
        if not amount:
            continue
        item = Item(f"{cat['group_name']}: {cat['name']}", cat["id"], amount, existing)
        if existing == amount:
            plan.unchanged += 1
        elif existing and not overwrite:
            plan.kept.append(item)
        else:
            plan.copy.append(item)
    return plan
