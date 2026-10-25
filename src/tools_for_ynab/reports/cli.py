"""`tools4ynab reports`: spending trends and subscription tracking from your YNAB history.

Read-only. `monthly` (the default) combines both reports; `--email` sends it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import click

from tools_for_ynab.core import notify
from tools_for_ynab.core.client import YnabError
from tools_for_ynab.core.commands import DefaultGroup
from tools_for_ynab.core.session import Session, open_session, where
from tools_for_ynab.rules.rules import RuleError, load_rules

from .config import ReportError, load_config
from .data import complete_months, spend_items
from .drift import analyze_drift
from .render import assemble, summary, to_html, to_text
from .subscriptions import find_subscriptions

SECTIONS = {
    "monthly": ("drift", "subscriptions"),
    "drift": ("drift",),
    "subscriptions": ("subscriptions",),
}
RECIPIENT_VARS = ("REPORT_EMAIL_TO", "NOTIFY_EMAIL_TO")
HISTORY_MONTHS = 14


def _since(as_of: date) -> date:
    """First day of the month HISTORY_MONTHS back, so the earliest month fetched is whole."""
    y, m = as_of.year, as_of.month - HISTORY_MONTHS
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def run(
    session: Session,
    sections: tuple[str, ...],
    config: Path,
    rules_path: Path,
    as_of: datetime | None,
    email: bool,
    save_html: Path | None,
) -> int:
    cfg = load_config(config)
    as_of_day = as_of.date() if as_of else date.today()
    if email and notify.EmailConfig.from_env(session.env, RECIPIENT_VARS) is None:
        click.echo(
            "Email is not configured: set SMTP_USER, SMTP_PASSWORD and REPORT_EMAIL_TO "
            "(or NOTIFY_EMAIL_TO).",
            err=True,
        )
        return 1

    categories = session.client.categories()
    txns = session.client.transactions(_since(as_of_day))
    items = spend_items(txns, categories, cfg)
    months = complete_months(txns, as_of_day)

    rules = []
    if rules_path.exists():
        try:
            rules = load_rules(rules_path).rules
        except RuleError as e:
            click.echo(f"note: rules not checked against price changes ({e})", err=True)

    subs = find_subscriptions(items, as_of_day, rules)
    # Fixed recurring charges (mortgage, subscriptions) only move when a price changes or a
    # payment lands a day early; that is reported under recurring charges, not as a trend.
    recurring = {(s.payee, s.category) for s in subs}
    trend_items = [s for s in items if (s.payee, s.category) not in recurring]
    report = assemble(
        analyze_drift(trend_items, months, cfg),
        subs,
        months,
        as_of_day,
        cfg,
        notify.run_url(session.env),
        sections,
    )
    text, body = to_text(report), to_html(report)
    if session.quiet:
        # GitHub Actions logs are readable by anyone with repo access: counts only.
        click.echo(f"Report for {as_of_day:%Y-%m}: {summary(report)}.")
        if not email:
            click.echo(
                "Nothing sent. Re-run with the verbose option to print the report in the log."
            )
    else:
        click.echo(text)
    if save_html:
        save_html.write_text(body)
        click.echo(f"Wrote {save_html}", err=True)
    if email:
        session.send_email(report.subject, text, body, RECIPIENT_VARS)
    return 0


def _execute(fn: Callable[..., int], params: dict[str, Any]) -> int:
    session = open_session()
    if session is None:
        return 2
    try:
        code, message = fn(session, **params), None
    except (YnabError, ReportError) as e:
        code, message = 1, session.scrub(str(e))
    except Exception as e:  # noqa: BLE001 - never print a raw traceback that could hold a URL
        # Exception text can hold data (a KeyError names its key), so CI logs get type + place.
        detail = f"{type(e).__name__} at {where(e)}"
        code, message = 1, session.scrub(detail if session.quiet else f"{detail}: {e}")
    if session.quiet:
        session.print_trace()
    if message is not None:
        click.echo(f"error: {message}", err=True)
    return code


def report_command(name: str, help_: str) -> click.Command:
    """One report command; they differ only in which sections they include."""

    @click.command(name, help=help_)
    @click.option(
        "--config",
        type=click.Path(path_type=Path),
        default="reports.yaml",
        show_default=True,
        help="Optional settings.",
    )
    @click.option(
        "--rules",
        "rules_path",
        type=click.Path(path_type=Path),
        default="rules.yaml",
        show_default=True,
        help="Rules file, used to say which rule a price change broke (optional).",
    )
    @click.option(
        "--as-of",
        type=click.DateTime(["%Y-%m-%d"]),
        metavar="YYYY-MM-DD",
        help="Report as of this date instead of today.",
    )
    @click.option("--email", is_flag=True, help="Email the report.")
    @click.option(
        "--save-html",
        type=click.Path(path_type=Path),
        help="Also write the email version to this file.",
    )
    def command(**params: Any) -> None:
        click.get_current_context().exit(_execute(run, {**params, "sections": SECTIONS[name]}))

    return command


@click.group(cls=DefaultGroup, default_command="monthly")
def reports() -> None:
    """Spending trends and recurring charges from your YNAB history. Read-only.

    With no command, runs `monthly`."""


reports.add_command(report_command("monthly", "Trends and recurring charges together."))
reports.add_command(report_command("drift", "Just the spending trends."))
reports.add_command(report_command("subscriptions", "Just the recurring charges."))
