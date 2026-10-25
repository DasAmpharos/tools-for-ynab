"""Turn raw YNAB transactions into spending lines and decide which months are usable."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date
from typing import Any

from .config import ReportConfig

INCOME_CATEGORY = "inflow: ready to assign"


@dataclass(frozen=True)
class Spend:
    """One category's share of one transaction. Positive = money spent, negative = a
    refund/credit against that category."""

    date: str
    month: str  # YYYY-MM
    category_id: str
    category: str
    group: str
    payee: str
    amount: float  # dollars
    txn_id: str


def spend_items(
    txns: list[dict[str, Any]], categories: list[dict[str, Any]], cfg: ReportConfig
) -> list[Spend]:
    """Spending lines from transactions. Splits count under their own categories,
    transfers and income don't count, and excluded groups/categories are dropped."""
    by_id = {c["id"]: c for c in categories}
    skip_groups = [g.lower() for g in cfg.exclude_groups]
    skip_cats = {c.lower() for c in cfg.exclude_categories}

    def usable(cat_id: str | None) -> dict[str, Any] | None:
        cat = by_id.get(cat_id)
        if cat is None or cat["name"].lower() == INCOME_CATEGORY:
            return None
        if cat["name"].lower() in skip_cats or any(
            g in cat["group_name"].lower() for g in skip_groups
        ):
            return None
        return cat

    out: list[Spend] = []
    for t in txns:
        if t.get("transfer_account_id"):
            continue
        parts = (
            [(s["category_id"], s["amount"]) for s in t["subtransactions"]]
            if t.get("subtransactions")
            else [(t.get("category_id"), t["amount"])]
        )
        for cat_id, milli in parts:
            cat = usable(cat_id)
            if cat is None:
                continue
            out.append(
                Spend(
                    t["date"],
                    t["date"][:7],
                    cat["id"],
                    cat["name"],
                    cat["group_name"],
                    html.unescape(t.get("payee_name") or "?"),
                    -milli / 1000,
                    t["id"],
                )
            )
    return out


def _next_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y + (mo == 12)}-{1 if mo == 12 else mo + 1:02d}"


def complete_months(txns: list[dict[str, Any]], as_of: date) -> list[str]:
    """Contiguous months safe to compare: after the (usually partial) first month of
    data and before the current month. Months with no spending are included as zeros."""
    if not txns:
        return []
    first = min(t["date"] for t in txns)
    start = first[:7]
    if int(first[8:10]) > 5:  # data began mid-month, so that month is incomplete
        start = _next_month(start)
    current = as_of.strftime("%Y-%m")
    months, m = [], start
    while m < current:
        months.append(m)
        m = _next_month(m)
    return months


def month_label(m: str) -> str:
    return date(int(m[:4]), int(m[5:7]), 1).strftime("%b %Y")
