"""Find steady monthly charges, and report price changes, new and stopped ones."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any

from .data import Spend

NEW_DAYS = 50  # labeled "new" this long (a monthly report mentions it about once)
YOUNG_DAYS = 75  # a subscription this young qualifies with only two charges
ACTIVE_DAYS = 45  # no charge for longer than this = stopped
STOPPED_REPORT_DAYS = 150  # stop mentioning a long-gone subscription after this
PRICE_CHANGE_DAYS = 120  # only mention price changes this recent


@dataclass
class Subscription:
    payee: str
    category: str
    amount: float  # latest price
    first_seen: str
    last_seen: str
    count: int
    status: str  # "active" | "new" | "stopped"
    price_change: tuple[float, float, str] | None = None  # old, new, date
    stale_rules: list[str] = field(default_factory=list)  # rules still expecting the old price


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def find_subscriptions(
    items: list[Spend], as_of: date, rules: list[Any] = ()
) -> list[Subscription]:
    """Charges that recur about monthly at a fixed price, allowing at most one price
    change. Variable bills (utilities) and everyday spending don't qualify."""
    today = as_of.isoformat()
    groups: dict[tuple[str, str], list[Spend]] = defaultdict(list)
    for s in items:
        if s.amount > 0:
            groups[(s.payee, s.category)].append(s)

    found: list[Subscription] = []
    for (payee, category), lines in groups.items():
        lines.sort(key=lambda s: s.date)
        if len(lines) < 2:
            continue
        gaps = [_days(a.date, b.date) for a, b in zip(lines, lines[1:], strict=False)]
        if not 25 <= median(gaps) <= 35 or sum(20 <= g <= 45 for g in gaps) / len(gaps) < 0.75:
            continue
        amounts = [round(s.amount, 2) for s in lines]
        changes = [i for i in range(1, len(amounts)) if amounts[i] != amounts[i - 1]]
        if len(changes) > 1:
            continue
        first, last = lines[0].date, lines[-1].date
        age = _days(first, today)
        if len(lines) < 3 and age > YOUNG_DAYS:
            continue  # two charges only count while the subscription is young

        since_last = _days(last, today)
        if since_last > ACTIVE_DAYS:
            if since_last > STOPPED_REPORT_DAYS:
                continue
            status = "stopped"
        else:
            status = "new" if age <= NEW_DAYS else "active"

        change = None
        if changes:
            i = changes[0]
            if _days(lines[i].date, today) <= PRICE_CHANGE_DAYS:
                change = (amounts[i - 1], amounts[i], lines[i].date)
        sub = Subscription(payee, category, amounts[-1], first, last, len(lines), status, change)
        if change:
            sub.stale_rules = _stale_rules(rules, payee, change[0], change[1])
        found.append(sub)

    found.sort(key=lambda s: (s.status == "stopped", -s.amount))
    return found


def _stale_rules(rules: list[Any], payee: str, old: float, new: float) -> list[str]:
    """Names of rules that matched this subscription at its old price but not its new one."""

    def txn(amount: float) -> dict[str, Any]:
        return {
            "payee_name": payee,
            "import_payee_name": payee,
            "memo": None,
            "account_name": None,
            "amount": -round(amount * 1000),
        }

    return [r.name for r in rules if r.match.matches(txn(old)) and not r.match.matches(txn(new))]


def monthly_total(subs: list[Subscription]) -> float:
    return sum(s.amount for s in subs if s.status != "stopped")
