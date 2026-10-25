"""Spending drift: where the recent average moved away from the previous one, and why."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .config import ReportConfig
from .data import Spend

SPARK = "▁▂▃▄▅▆▇█"
SPIKE_SHARE = 0.7  # one month holding this much of the recent total makes it a spike
SERIES_MONTHS = 6


def sparkline(values: list[float]) -> str:
    top = max(values, default=0)
    if top <= 0:
        return SPARK[0] * len(values)
    return "".join(
        SPARK[min(len(SPARK) - 1, int(max(v, 0) / top * (len(SPARK) - 0.001)))] for v in values
    )


@dataclass
class Driver:
    payee: str
    recent_total: float  # spent with this payee across the recent window
    previous_avg: float  # per month, in the previous window
    count: int  # transactions in the recent window


@dataclass
class DriftItem:
    key: str
    group: str
    recent_avg: float
    previous_avg: float
    # sustained = a lasting rise; spike = a rise from one month; down = a lasting drop;
    # settled = a drop because the previous period had a one-month spike
    kind: str
    spike_month: str | None
    series: list[float]  # last few complete months, oldest first
    drivers: list[Driver] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.recent_avg - self.previous_avg

    @property
    def percent(self) -> float | None:
        return self.delta / self.previous_avg * 100 if self.previous_avg > 0 else None


@dataclass
class DriftResult:
    recent: list[str]
    previous: list[str]
    items: list[DriftItem]  # biggest move first
    note: str | None = None  # why there's nothing to compare, if so

    def of_kind(self, kind: str) -> list[DriftItem]:
        return [i for i in self.items if i.kind == kind]


def analyze_drift(
    items: list[Spend],
    months: list[str],
    cfg: ReportConfig,
    by: str = "category",
    with_drivers: bool = True,
) -> DriftResult:
    """Compare the average of the last `window_months` complete months with the average
    of the months before. Only moves of at least `min_change_dollars` a month (and
    `min_change_percent` of the old level) are reported."""
    w = cfg.window_months
    recent, previous = months[-w:], months[-2 * w : -w]
    if len(previous) < 2:
        return DriftResult(
            recent,
            previous,
            [],
            f"Not enough history to compare yet: need {w + 2} complete months, have {len(months)}.",
        )

    def key_of(s: Spend) -> str:
        return s.category if by == "category" else s.group

    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    groups: dict[str, str] = {}
    for s in items:
        if s.month in months:
            totals[key_of(s)][s.month] += s.amount
            groups.setdefault(key_of(s), s.group)

    found: list[DriftItem] = []
    for key, by_month in totals.items():
        rec_total = sum(by_month.get(m, 0.0) for m in recent)
        recent_avg = rec_total / len(recent)
        previous_avg = sum(by_month.get(m, 0.0) for m in previous) / len(previous)
        delta = recent_avg - previous_avg
        if abs(delta) < cfg.min_change_dollars:
            continue
        if previous_avg > 0 and abs(delta) / previous_avg * 100 < cfg.min_change_percent:
            continue

        kind, spike_month = _classify(by_month, recent, previous, delta)
        item = DriftItem(
            key,
            groups[key],
            recent_avg,
            previous_avg,
            kind,
            spike_month,
            [by_month.get(m, 0.0) for m in months[-SERIES_MONTHS:]],
        )
        if with_drivers:
            item.drivers = _drivers(items, key, key_of, recent, previous, delta, cfg.top_drivers)
        found.append(item)

    found.sort(key=lambda i: -abs(i.delta))
    return DriftResult(recent, previous, found)


def _classify(by_month, recent, previous, delta) -> tuple[str, str | None]:
    """Is a move a lasting trend, or one month's blip (in the recent or previous window)?"""

    def concentrated(window: list[str]) -> str | None:
        total = sum(by_month.get(m, 0.0) for m in window)
        top = max(window, key=lambda m: by_month.get(m, 0.0))
        if len(window) >= 2 and total > 0 and by_month.get(top, 0.0) / total >= SPIKE_SHARE:
            return top
        return None

    if delta > 0:
        month = concentrated(recent)
        return ("spike", month) if month else ("sustained", None)
    month = concentrated(previous)
    return ("settled", month) if month else ("down", None)


def _drivers(items, key, key_of, recent, previous, delta, limit) -> list[Driver]:
    """The payees that account for most of the change in one category."""
    rec: dict[str, float] = defaultdict(float)
    prev: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for s in items:
        if key_of(s) != key:
            continue
        if s.month in recent:
            rec[s.payee] += s.amount
            count[s.payee] += 1
        elif s.month in previous:
            prev[s.payee] += s.amount
    moves = {p: rec[p] / len(recent) - prev[p] / len(previous) for p in {*rec, *prev}}
    ranked = sorted(moves, key=lambda p: -moves[p] if delta > 0 else moves[p])
    return [
        Driver(p, rec[p], prev[p] / len(previous), count[p])
        for p in ranked[:limit]
        if (moves[p] > 0) == (delta > 0) and abs(moves[p]) >= 1
    ]
