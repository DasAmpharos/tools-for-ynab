"""`tools4ynab reports`: spending trends and subscription tracking from your YNAB history.

Read-only. `monthly` (the default) combines both reports; `--email` sends it."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from tools_for_ynab.core import notify
from tools_for_ynab.core.client import YnabError
from tools_for_ynab.core.session import Session, open_session, where
from tools_for_ynab.rules.rules import RuleError, load_rules

from .config import ReportError, load_config
from .data import complete_months, spend_items
from .drift import analyze_drift
from .render import assemble, summary, to_html, to_text
from .subscriptions import find_subscriptions

COMMANDS = ("monthly", "drift", "subscriptions")
SECTIONS = {
    "monthly": ("drift", "subscriptions"),
    "drift": ("drift",),
    "subscriptions": ("subscriptions",),
}
RECIPIENT_VARS = ("REPORT_EMAIL_TO", "NOTIFY_EMAIL_TO")
HISTORY_MONTHS = 14


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tools4ynab reports", description=__doc__)
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="monthly")
    parser.add_argument(
        "--config", type=Path, default=Path("reports.yaml"), help="optional settings"
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path("rules.yaml"),
        help="rules file, used to say which rule a price change broke (optional)",
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, help="report as of this date (YYYY-MM-DD)"
    )
    parser.add_argument("--email", action="store_true", help="email the report")
    parser.add_argument("--save-html", type=Path, help="also write the email version to this file")
    return parser


def _since(as_of: date) -> date:
    """First day of the month HISTORY_MONTHS back, so the earliest month fetched is whole."""
    y, m = as_of.year, as_of.month - HISTORY_MONTHS
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def run(session: Session, args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    as_of = args.as_of or date.today()
    if args.email and notify.EmailConfig.from_env(session.env, RECIPIENT_VARS) is None:
        print(
            "Email is not configured: set SMTP_USER, SMTP_PASSWORD and REPORT_EMAIL_TO "
            "(or NOTIFY_EMAIL_TO).",
            file=sys.stderr,
        )
        return 1

    categories = session.client.categories()
    txns = session.client.transactions(_since(as_of))
    items = spend_items(txns, categories, cfg)
    months = complete_months(txns, as_of)

    rules = []
    if args.rules.exists():
        try:
            rules = load_rules(args.rules).rules
        except RuleError as e:
            print(f"note: rules not checked against price changes ({e})", file=sys.stderr)

    subs = find_subscriptions(items, as_of, rules)
    # Fixed recurring charges (mortgage, subscriptions) only move when a price changes or a
    # payment lands a day early; that is reported under recurring charges, not as a trend.
    recurring = {(s.payee, s.category) for s in subs}
    trend_items = [s for s in items if (s.payee, s.category) not in recurring]
    report = assemble(
        analyze_drift(trend_items, months, cfg),
        subs,
        months,
        as_of,
        cfg,
        notify.run_url(session.env),
        SECTIONS[args.command],
    )
    text, body = to_text(report), to_html(report)
    if session.quiet:
        # GitHub Actions logs are readable by anyone with repo access: counts only.
        print(f"Report for {as_of:%Y-%m}: {summary(report)}.")
        if not args.email:
            print("Nothing sent. Re-run with the verbose option to print the report in the log.")
    else:
        print(text)
    if args.save_html:
        args.save_html.write_text(body)
        print(f"Wrote {args.save_html}", file=sys.stderr)
    if args.email:
        session.send_email(report.subject, text, body, RECIPIENT_VARS)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = open_session()
    if session is None:
        return 2
    try:
        code, message = run(session, args), None
    except (YnabError, ReportError) as e:
        code, message = 1, session.scrub(str(e))
    except Exception as e:  # noqa: BLE001 - never print a raw traceback that could hold a URL
        # Exception text can hold data (a KeyError names its key), so CI logs get type + place.
        detail = f"{type(e).__name__} at {where(e)}"
        code, message = 1, session.scrub(detail if session.quiet else f"{detail}: {e}")
    if session.quiet:
        session.print_trace()
    if message is not None:
        print(f"error: {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
