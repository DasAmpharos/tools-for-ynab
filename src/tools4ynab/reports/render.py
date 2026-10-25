"""Build the monthly report once as a plain structure, then render it as email HTML and as
plain text. HTML is the primary view (cards, mini bar charts); text is for the terminal."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date

from .config import ReportConfig
from .data import month_label
from .drift import DriftItem, DriftResult
from .subscriptions import Subscription, monthly_total

LATE_DAYS = 75  # a monthly charge missing for longer than this "looks cancelled"
RECURRING_SHOWN = 10


# --- formatting helpers ---------------------------------------------------------------


def money(x: float, cents: bool = False) -> str:
    sign = "-" if round(x, 2 if cents else 0) < 0 else ""
    return f"{sign}${abs(x):,.2f}" if cents else f"{sign}${abs(x):,.0f}"


def signed(x: float) -> str:
    return f"{'+' if round(x) >= 0 else '-'}{money(abs(x))}"


def clip(text: str, width: int = 34) -> str:
    """Some payees are whole bank memo lines (Zelle transfers); keep them readable."""
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def short_month(m: str) -> str:
    return month_label(m).split()[0]


def month_span(months: list[str]) -> str:
    a, b = months[0], months[-1]
    if a == b:
        return month_label(a)
    return (
        f"{short_month(a)}–{month_label(b)}"
        if a[:4] == b[:4]
        else f"{month_label(a)}–{month_label(b)}"
    )


def pretty_date(iso: str, as_of: date) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%b %-d") + ("" if d.year == as_of.year else f", {d.year}")


def _alnum(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def label(payee: str, category: str) -> str:
    """The budget category, plus the merchant when it adds information."""
    p, c = _alnum(payee), _alnum(category)
    return category if p in c or c in p else f"{category} ({clip(payee, 24)})"


# --- the report structure -------------------------------------------------------------


@dataclass
class Trend:
    name: str
    now: float  # average per month, recent window
    before: float
    series: list[float]  # last few complete months, oldest first
    recent_months: int  # how many of the series' trailing bars are the recent window
    note: str  # the payees behind it, one line
    total: float = 0.0  # spent across the recent window (used for one-time purchases)
    month: str = ""  # the month a one-time purchase landed in

    @property
    def delta(self) -> float:
        return self.now - self.before


@dataclass
class Callout:
    title: str
    detail: str
    tone: str  # "warn" | "info" | "good"


@dataclass
class Report:
    subject: str
    title: str
    period: str
    glance: list[str]
    callouts: list[Callout] = field(default_factory=list)
    up: list[Trend] = field(default_factory=list)
    up_more: int = 0
    down: list[Trend] = field(default_factory=list)
    down_more: int = 0
    bumps: list[Trend] = field(default_factory=list)
    bumps_more: int = 0
    recurring: list[tuple[str, float]] = field(default_factory=list)
    recurring_total: float = 0.0
    recurring_count: int = 0
    recurring_more: int = 0
    recurring_more_total: float = 0.0
    footer: list[str] = field(default_factory=list)


def summary(r: Report) -> str:
    """Counts only: what the report holds, safe to put in a CI log."""
    return (
        f"{len(r.up) + r.up_more} trending up, {len(r.down) + r.down_more} down, "
        f"{len(r.bumps) + r.bumps_more} one-time, {len(r.callouts)} to look at, "
        f"{r.recurring_count} recurring charges"
    )


def _trend(item: DriftItem, result: DriftResult) -> Trend:
    if item.delta < 0:
        note = "Less at " + ", ".join(
            f"{clip(d.payee, 28)} (was {money(d.previous_avg)}/mo)" for d in item.drivers
        )
    else:
        note = "Mostly " + ", ".join(
            f"{clip(d.payee, 28)} ({money(d.recent_total)})" for d in item.drivers
        )
    if not item.drivers:
        note = ""
    return Trend(
        item.key,
        item.recent_avg,
        item.previous_avg,
        item.series,
        len(result.recent),
        note,
        item.recent_avg * len(result.recent),
        short_month(item.spike_month) if item.spike_month else "",
    )


def _callouts(subs: list[Subscription], as_of: date) -> list[Callout]:
    out: list[Callout] = []
    cancelled: list[str] = []
    for s in subs:
        name = label(s.payee, s.category)
        if s.price_change:
            old, new, when = s.price_change
            stale = [
                f"Your rule “{r}” still expects {money(old, cents=True)}; update it."
                for r in s.stale_rules
            ]
            out.append(
                Callout(
                    f"{name} went {'up' if new > old else 'down'}: "
                    f"{money(old, cents=True)} → {money(new, cents=True)}",
                    " ".join([f"Changed on {pretty_date(when, as_of)}.", *stale]),
                    "warn" if stale else "info" if new > old else "good",
                )
            )
        elif s.status == "stopped":
            days = (as_of - date.fromisoformat(s.last_seen)).days
            if days > LATE_DAYS:
                cancelled.append(f"{name}, last {pretty_date(s.last_seen, as_of)}")
                continue
            out.append(
                Callout(
                    f"{name}: no charge since {pretty_date(s.last_seen, as_of)}",
                    "It normally bills monthly, so a payment may be late or missing.",
                    "warn",
                )
            )
        elif s.status == "new":
            out.append(
                Callout(
                    f"New: {name}, {money(s.amount, cents=True)}/month",
                    f"First charged {pretty_date(s.first_seen, as_of)}.",
                    "info",
                )
            )
    order = {"warn": 0, "info": 1, "good": 2}
    out.sort(key=lambda c: order[c.tone])
    if cancelled:
        out.append(
            Callout(
                "Looks cancelled",
                "No charge in over two months: " + "; ".join(cancelled) + ".",
                "info",
            )
        )
    return out


def build_report(
    categories: DriftResult,
    groups: DriftResult,  # unused: group roll-ups added noise, kept for the CLI's signature
    subs: list[Subscription],
    months: list[str],
    as_of: date,
    cfg: ReportConfig,
    url: str | None = None,
    sections: tuple[str, ...] = ("drift", "subscriptions"),
) -> tuple[str, str, str]:
    """(subject, plain text, html)."""
    r = assemble(categories, subs, months, as_of, cfg, url, sections)
    return r.subject, to_text(r), to_html(r)


def assemble(
    categories: DriftResult,
    subs: list[Subscription],
    months: list[str],
    as_of: date,
    cfg: ReportConfig,
    url: str | None,
    sections: tuple[str, ...],
) -> Report:
    cap = cfg.max_items
    through = month_label(months[-1]) if months else as_of.strftime("%b %Y")
    last = date.fromisoformat((months[-1] if months else as_of.strftime("%Y-%m")) + "-01")
    long_month = last.strftime("%B %Y")
    r = Report(f"YNAB report for {through}", f"Monthly report: {long_month}", "", [])
    show_trends, show_recurring = "drift" in sections, "subscriptions" in sections

    if show_trends:
        if categories.note:
            r.period = categories.note
        else:
            r.period = (
                f"Compares your average monthly spending for {month_span(categories.recent)} (the last "
                f"{len(categories.recent)} complete months) with {month_span(categories.previous)} "
                f"(the {len(categories.previous)} before). Only changes of {money(cfg.min_change_dollars)}+ "
                f"a month and {cfg.min_change_percent:.0f}%+ are shown."
            )
            for kind, attr in (("sustained", "up"), ("down", "down"), ("spike", "bumps")):
                items = categories.of_kind(kind)
                shown = [_trend(i, categories) for i in items[:cap]]
                setattr(r, attr, shown)
                setattr(r, f"{attr}_more", max(0, len(items) - cap))
            r.glance += _trend_glance(categories, r)
        if r.up or r.down:
            r.subject += f": {len(categories.of_kind('sustained'))} trending up, "
            r.subject += f"{len(categories.of_kind('down'))} down"

    if show_recurring and subs:
        live = [s for s in subs if s.status != "stopped"]
        r.callouts = _callouts(subs, as_of)
        rows = [(label(s.payee, s.category), s.amount) for s in live]
        r.recurring_total, r.recurring_count = monthly_total(subs), len(live)
        r.recurring = rows[:RECURRING_SHOWN]
        r.recurring_more = max(0, len(rows) - RECURRING_SHOWN)
        r.recurring_more_total = sum(a for _, a in rows[RECURRING_SHOWN:])
        r.glance.append(
            f"Recurring charges total {money(r.recurring_total)}/month across {r.recurring_count}."
            + (f" {len(r.callouts)} need a look below." if r.callouts else "")
        )
    if not r.glance and show_trends and not categories.note:
        r.glance.append("Nothing moved enough to mention.")

    r.footer = [
        "Transfers, income and excluded groups (like closed trips) are left out. Split purchases count "
        "under each category. Fixed recurring charges appear under recurring charges, not as trends."
    ]
    if url:
        r.footer.append(f"Run: {url}")
    return r


def _trend_glance(categories: DriftResult, r: Report) -> list[str]:
    out = []
    up, down = categories.of_kind("sustained"), categories.of_kind("down")
    if up:
        biggest = ", ".join(f"{i.key} ({signed(i.delta)})" for i in up[:2])
        out.append(
            f"{len(up)} categor{'y is' if len(up) == 1 else 'ies are'} trending up, about "
            f"{money(sum(i.delta for i in up))}/month more than before. Biggest: {biggest}."
        )
    if down:
        out.append(
            f"{len(down)} {'is' if len(down) == 1 else 'are'} down, about "
            f"{money(-sum(i.delta for i in down))}/month less."
        )
    if r.bumps:
        top = max(r.bumps, key=lambda t: t.total)
        n = len(categories.of_kind("spike"))
        out.append(
            f"{n} categor{'y' if n == 1 else 'ies'} had a large one-month spike (biggest: "
            f"{top.name}, {money(top.total)} in {top.month}). Those are listed separately, not as trends."
        )
    return out


# --- plain text -----------------------------------------------------------------------


def to_text(r: Report) -> str:
    def trends(heading: str, blurb: str, rows: list[Trend], more: int) -> list[str]:
        if not rows:
            return []
        out = ["", heading, f"  {blurb}"]
        for t in rows:
            out.append(f"  {t.name}")
            out.append(f"      {money(t.now)}/mo now, was {money(t.before)}   ({signed(t.delta)})")
            if t.note:
                out.append(f"      {t.note}")
        return out + (
            [f"  + {more} smaller change{'s' if more != 1 else ''} not shown"] if more else []
        )

    out = [r.subject.upper(), "", r.period] if r.period else [r.subject.upper()]
    if r.glance:
        out += ["", "AT A GLANCE", *[f"  • {g}" for g in r.glance]]
    if r.callouts:
        out += ["", "WORTH A LOOK"]
        for c in r.callouts:
            out += [f"  ! {c.title}", f"      {c.detail}"]
    out += trends(
        "SPENDING CREEPING UP", "Averaging more per month recently than before.", r.up, r.up_more
    )
    out += trends("SPENDING THAT'S COME DOWN", "Averaging less per month.", r.down, r.down_more)
    if r.bumps:
        out += ["", "ONE-TIME PURCHASES", "  Large in a single month, so probably not a new habit."]
        for t in r.bumps:
            out.append(f"  {t.name}: {money(t.total)} in {t.month}")
            if t.note:
                out.append(f"      {t.note}")
        if r.bumps_more:
            out.append(f"  + {r.bumps_more} more")
    if r.recurring:
        out += [
            "",
            f"RECURRING CHARGES: {money(r.recurring_total)}/MONTH",
            "  Charges that repeat every month at a fixed price.",
        ]
        out += [f"  {money(a, cents=True):>10}  {name}" for name, a in r.recurring]
        if r.recurring_more:
            out.append(
                f"  + {r.recurring_more} smaller charges ({money(r.recurring_more_total)}/month)"
            )
    return "\n".join([*out, "", *r.footer])


# --- email HTML -----------------------------------------------------------------------

INK, MUTED, FAINT, LINE = "#111827", "#6b7280", "#9ca3af", "#eceff3"
UP, DOWN, OLD_BAR = "#c2410c", "#15803d", "#cbd5e1"
CALLOUT = {  # background, border
    "warn": ("#fffbeb", "#f59e0b"),
    "info": ("#f1f5f9", "#94a3b8"),
    "good": ("#f0fdf4", "#22c55e"),
}
e = html.escape


def _bars(series: list[float], recent: int, colour: str) -> str:
    top = max(series, default=0) or 1
    cells = []
    for i, v in enumerate(series):
        height = max(2, round(max(v, 0) / top * 28))
        fill = colour if i >= len(series) - recent else OLD_BAR
        cells.append(
            f'<td valign="bottom" style="padding:0 2px 0 0"><div style="width:9px;height:{height}px;'
            f'background:{fill};border-radius:2px 2px 0 0;font-size:0;line-height:0">&nbsp;</div></td>'
        )
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" style="height:30px"><tr>'
        + "".join(cells)
        + "</tr></table>"
    )


def _section(title: str, blurb: str, body: str) -> str:
    return (
        f'<div style="margin:28px 0 0"><div style="font-size:17px;font-weight:700;color:{INK}">{e(title)}</div>'
        f'<div style="font-size:13px;color:{MUTED};margin:2px 0 10px">{e(blurb)}</div>{body}</div>'
    )


def _trend_table(rows: list[Trend], more: int, colour: str) -> str:
    head = "".join(
        f'<td style="padding:0 0 6px;{s}font-size:11px;letter-spacing:.04em;text-transform:uppercase;'
        f'color:{FAINT}">{t}</td>'
        for t, s in (
            ("Category", ""),
            ("6 months", "padding-left:8px;white-space:nowrap;"),
            ("Now", "text-align:right;white-space:nowrap;"),
            ("Change", "text-align:right;padding-left:12px;white-space:nowrap;"),
        )
    )
    body = [f"<tr>{head}</tr>"]
    for t in rows:
        note = (
            f'<div style="font-size:12.5px;color:{MUTED};margin-top:2px">{e(t.note)}</div>'
            if t.note
            else ""
        )
        body.append(
            f'<tr><td style="padding:11px 8px 11px 0;border-top:1px solid {LINE};vertical-align:top">'
            f'<div style="font-size:15px;font-weight:600;color:{INK}">{e(t.name)}</div>{note}</td>'
            f'<td style="padding:11px 8px;border-top:1px solid {LINE};vertical-align:middle">'
            f"{_bars(t.series, t.recent_months, colour)}</td>"
            f'<td style="padding:11px 0;border-top:1px solid {LINE};text-align:right;vertical-align:top;'
            f'white-space:nowrap"><div style="font-size:15px;font-weight:600;color:{INK}">{money(t.now)}'
            f'<span style="font-weight:400;color:{MUTED};font-size:12px">/mo</span></div>'
            f'<div style="font-size:12px;color:{MUTED}">was {money(t.before)}</div></td>'
            f'<td style="padding:11px 0 11px 12px;border-top:1px solid {LINE};text-align:right;'
            f'vertical-align:top;white-space:nowrap;font-size:15px;font-weight:700;color:{colour}">'
            f"{signed(t.delta)}</td></tr>"
        )
    table = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        + "".join(body)
        + "</table>"
    )
    return table + (_more(more, "smaller change") if more else "")


def _more(n: int, what: str) -> str:
    return f'<div style="font-size:12px;color:{FAINT};margin-top:6px">+ {n} more {what}{"s" if n != 1 else ""} not shown</div>'


def to_html(r: Report) -> str:
    parts = [f'<div style="font-size:24px;font-weight:800;color:{INK}">{e(r.title)}</div>']
    if r.period:
        parts.append(
            f'<div style="font-size:13px;color:{MUTED};margin-top:6px">{e(r.period)}</div>'
        )
    if r.glance:
        items = "".join(f'<li style="margin:5px 0">{e(g)}</li>' for g in r.glance)
        parts.append(
            f'<div style="background:#f8fafc;border:1px solid {LINE};border-radius:10px;padding:12px 16px;'
            f'margin-top:18px"><div style="font-size:12px;letter-spacing:.05em;text-transform:uppercase;'
            f'color:{FAINT};font-weight:700">At a glance</div><ul style="margin:6px 0 0;padding-left:18px;'
            f'font-size:14.5px;color:{INK};line-height:1.45">{items}</ul></div>'
        )
    if r.callouts:
        boxes = "".join(
            f'<div style="background:{CALLOUT[c.tone][0]};border-left:4px solid {CALLOUT[c.tone][1]};'
            f'border-radius:6px;padding:9px 12px;margin:8px 0"><div style="font-size:14.5px;font-weight:600;'
            f'color:{INK}">{e(c.title)}</div><div style="font-size:13px;color:{MUTED};margin-top:2px">'
            f"{e(c.detail)}</div></div>"
            for c in r.callouts
        )
        parts.append(
            _section(
                "Worth a look", "Changes to your recurring charges that need attention.", boxes
            )
        )
    if r.up:
        parts.append(
            _section(
                "Spending creeping up",
                "Averaging more per month recently than before. Darker bars are the recent months.",
                _trend_table(r.up, r.up_more, UP),
            )
        )
    if r.down:
        parts.append(
            _section(
                "Spending that's come down",
                "Averaging less per month.",
                _trend_table(r.down, r.down_more, DOWN),
            )
        )
    if r.bumps:
        rows = "".join(
            f'<tr><td style="padding:9px 8px 9px 0;border-top:1px solid {LINE};vertical-align:top">'
            f'<div style="font-size:14.5px;font-weight:600;color:{INK}">{e(t.name)}</div>'
            + (f'<div style="font-size:12.5px;color:{MUTED}">{e(t.note)}</div>' if t.note else "")
            + f'</td><td style="padding:9px 0;border-top:1px solid {LINE};text-align:right;vertical-align:top;'
            f'white-space:nowrap;font-size:14.5px;font-weight:600;color:{INK}">{money(t.total)}'
            f'<span style="font-weight:400;color:{MUTED};font-size:12px"> in {e(t.month)}</span></td></tr>'
            for t in r.bumps
        )
        parts.append(
            _section(
                "One-time purchases",
                "Large in a single month, so probably not a new habit.",
                f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows}</table>'
                + (_more(r.bumps_more, "purchase") if r.bumps_more else ""),
            )
        )
    if r.recurring:
        rows = "".join(
            f'<tr><td style="padding:6px 0;border-top:1px solid {LINE};font-size:14px;color:{INK}">{e(n)}</td>'
            f'<td style="padding:6px 0;border-top:1px solid {LINE};text-align:right;font-size:14px;'
            f'color:{INK};white-space:nowrap">{money(a, cents=True)}</td></tr>'
            for n, a in r.recurring
        )
        tail = (
            f'<div style="font-size:12px;color:{FAINT};margin-top:6px">+ {r.recurring_more} smaller charges '
            f"({money(r.recurring_more_total)}/month)</div>"
            if r.recurring_more
            else ""
        )
        parts.append(
            _section(
                f"Recurring charges · {money(r.recurring_total)}/month",
                "Charges that repeat every month at a fixed price.",
                f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows}</table>{tail}',
            )
        )
    foot = "".join(f'<p style="margin:6px 0">{e(f)}</p>' for f in r.footer)
    parts.append(
        f'<div style="font-size:12px;color:{FAINT};margin-top:30px;line-height:1.5">{foot}</div>'
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light"></head>'
        '<body style="margin:0;padding:16px;background:#f3f4f6">'
        '<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:14px;padding:26px 26px 22px;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        f'color:{INK}">' + "".join(parts) + "</div></body></html>"
    )
