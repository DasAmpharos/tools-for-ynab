"""Command line entry point. `tools4ynab rules [run] [--apply]` applies the rules; the other
subcommands (backtest, unmatched, suggest, explain, duplicates, undo) help you write,
check and reverse them."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from tools_for_ynab.core import notify
from tools_for_ynab.core.client import YnabError
from tools_for_ynab.core.session import Session, open_session, where

from . import analysis, digest
from .audit import AuditLog, fetch_github_audit, latest_log, load_entries, plan_undo
from .planner import Context, apply_plan, build_plan, load_context
from .rules import RuleError, RuleSet, disable, load_rules, parse_disabled

COMMANDS = (
    "run",
    "backtest",
    "unmatched",
    "suggest",
    "explain",
    "duplicates",
    "undo",
    "test-email",
)


class RulesSession(Session):
    """A session that also remembers this run's audit log, so a failure can report it."""

    audit: AuditLog | None = None


def _history(session: RulesSession, days: float) -> list[dict[str, Any]]:
    return session.client.transactions(date.today() - timedelta(days=round(days)))


def _months_to_days(months: float) -> float:
    return months * 30.44


def _load_context(
    session: RulesSession, args: argparse.Namespace, *, disabled: bool = False
) -> Context:
    ruleset: RuleSet = load_rules(args.rules)
    names = parse_disabled(session.env.get("YNAB_DISABLED_RULES")) if disabled else []
    if names:
        ruleset = disable(ruleset, names)
        print(f"Disabled via YNAB_DISABLED_RULES: {', '.join(names)}")
    return load_context(session.client, ruleset)


# --- commands -----------------------------------------------------------------


def cmd_run(session: RulesSession, args: argparse.Namespace) -> int:
    ctx = _load_context(session, args, disabled=True)
    all_txns = _history(session, ctx.ruleset.settings.since_days)
    plan = build_plan(ctx, all_txns)

    mode = "Applying" if args.apply else "Dry run:"
    print(
        f"{mode} {len(plan.changes)} categorization(s), {len(plan.splits)} split(s), "
        f"{len(plan.pairs)} transfer pair(s) from {plan.scanned} transaction(s)"
    )
    # In GitHub Actions logs: counts and rule names only, never payees, amounts or categories.
    lines = plan.quiet_lines() if session.quiet else plan.lines()
    print("\n".join(lines))
    _write_step_summary(mode, plan.scanned, lines)

    duplicates = analysis.find_duplicates(all_txns)
    warnings = [
        f"Possible duplicate: {analysis._desc(a)} ({a['account_name']}) looks like "
        f"{analysis._desc(b)}, which the bank imported; check for double-counting"
        for a, b in duplicates
    ]
    if session.quiet and duplicates:
        print(f"{len(duplicates)} possible duplicate(s); run `tools4ynab rules duplicates` locally")
    elif warnings:
        print("\n".join(warnings))

    if not args.apply:
        if plan.total:
            print("Re-run with --apply to write these changes.")
        return 0

    session.audit = AuditLog(args.audit_dir, redact=session.quiet)
    outcomes = apply_plan(ctx, plan, session.audit)
    print("\n".join(outcomes))
    print("Done.")
    if session.audit.entries:
        subject, text, body = digest.digest(
            session.audit.entries,
            str(session.audit.path) if session.audit.path else None,
            notify.run_url(session.env),
            warnings,
        )
        session.send_email(subject, text, body)
    return 0


def cmd_backtest(session: RulesSession, args: argparse.Namespace) -> int:
    ctx = _load_context(session, args)
    report = analysis.backtest(ctx, _history(session, _months_to_days(args.months)))
    print("\n".join(report.lines))
    return 1 if report.problems else 0


def cmd_unmatched(session: RulesSession, args: argparse.Namespace) -> int:
    ctx = _load_context(session, args)
    txns = _history(session, _months_to_days(args.months))
    print("\n".join(analysis.unmatched(ctx, txns, args.limit)))
    return 0


def cmd_suggest(session: RulesSession, args: argparse.Namespace) -> int:
    ctx = _load_context(session, args)
    txns = _history(session, _months_to_days(args.months))
    sugs = analysis.suggest(ctx, txns, args.min_count, args.max_age_days)
    if not sugs:
        print("No new rules to suggest.")
        return 0
    text = analysis.render_suggestions(sugs, args.months)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(f"Wrote {len(sugs)} suggested rule(s) to {args.output}")
    else:
        print(text)
    return 0


def cmd_explain(session: RulesSession, args: argparse.Namespace) -> int:
    ctx = _load_context(session, args)
    print("\n".join(analysis.explain(ctx, _history(session, args.days), args.query)))
    return 0


def cmd_duplicates(session: RulesSession, args: argparse.Namespace) -> int:
    found = analysis.find_duplicates(_history(session, args.days))
    for a, b in found:
        print(f"{analysis._desc(a)} ({a['account_name']}, manual, uncleared)")
        print(f"  looks like {analysis._desc(b)} (bank import, cleared)")
    print(f"{len(found)} possible duplicate(s)")
    return 0


def cmd_undo(session: RulesSession, args: argparse.Namespace) -> int:
    if args.from_github:
        run_id = None if args.from_github == "latest" else args.from_github
        with tempfile.TemporaryDirectory() as tmp:
            paths = fetch_github_audit(run_id, Path(tmp))
            entries = [e for p in paths for e in load_entries(p)]
        source = f"GitHub run {run_id or '(latest)'}"
    else:
        path = args.file or latest_log(args.audit_dir)
        if path is None:
            print(f"No audit logs found in {args.audit_dir}/", file=sys.stderr)
            return 1
        entries, source = load_entries(path), str(path)
    patches, notes = plan_undo(session.client, entries)
    print(f"{source}: {len(entries)} change(s) logged, {len(patches)} can be restored")
    print("\n".join(notes))
    if not args.apply:
        if patches:
            print("Re-run with --apply to restore them.")
        return 0
    if patches:
        session.client.update_transactions(patches)
    print(f"Restored {len(patches)} transaction(s).")
    return 0


def cmd_test_email(session: RulesSession, args: argparse.Namespace) -> int:
    """Send a sample email so you can check SMTP credentials without waiting for a run."""
    cfg = notify.EmailConfig.from_env(session.env)
    if cfg is None:
        missing = [
            v for v in ("SMTP_USER", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO") if not session.env.get(v)
        ]
        print(f"Email is not configured; missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    subject, text, body = digest.digest(
        [
            {
                "date": date.today().isoformat(),
                "payee": "Example Store",
                "amount": -12340,
                "rule": "Example rule",
                "summary": "category=Groceries, approved=True",
            }
        ],
        None,
        notify.run_url(session.env),
        ["This is a test email from tools4ynab rules; nothing was changed in YNAB."],
    )
    try:
        notify.send_email(cfg, "[TEST] " + subject, text, body)
    except Exception as e:  # noqa: BLE001 - show why (bad password, blocked, ...)
        print(f"error: {session.scrub(f'{type(e).__name__}: {e}')}", file=sys.stderr)
        return 1
    print(f"Sent a test email to {len(cfg.recipients)} recipient(s) via {cfg.host}:{cfg.port}.")
    return 0


# --- plumbing -----------------------------------------------------------------


def _write_step_summary(mode: str, scanned: int, lines: list[str]) -> None:
    """Surface results on the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    body = "\n".join(f"- `{line}`" for line in lines) or "_No matching transactions._"
    with open(path, "a") as f:
        f.write(f"### {mode} {len(lines)} change(s) ({scanned} scanned)\n\n{body}\n")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--rules", type=Path, default=Path("rules.yaml"), help="path to rules file")
    common.add_argument(
        "--audit-dir",
        type=Path,
        default=Path(os.environ.get("YNAB_AUDIT_DIR") or "audit"),
        help="where applied changes are logged (default: audit/)",
    )

    parser = argparse.ArgumentParser(prog="tools4ynab rules", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str, fn: Callable[..., int]) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("run", "apply the rules (dry run unless --apply); the default command", cmd_run)
    p.add_argument("--apply", action="store_true", help="write changes to YNAB")

    p = add("backtest", "replay the rules over history and flag disagreements", cmd_backtest)
    p.add_argument("--months", type=float, default=6)

    p = add("unmatched", "list the payees no rule covers yet", cmd_unmatched)
    p.add_argument("--months", type=float, default=6)
    p.add_argument("--limit", type=int, default=25)

    p = add("suggest", "propose rules that were right every time in your history", cmd_suggest)
    p.add_argument("--months", type=float, default=6)
    p.add_argument("--min-count", type=int, default=3)
    p.add_argument("--max-age-days", type=int, default=120, help="skip payees idle longer")
    p.add_argument("--output", type=Path, help="write YAML here instead of printing")

    p = add("explain", "show what the rules would do for matching transactions", cmd_explain)
    p.add_argument("query", help="payee text or transaction id prefix")
    p.add_argument("--days", type=int, default=60)

    p = add("duplicates", "find manual entries that duplicate a bank import", cmd_duplicates)
    p.add_argument("--days", type=int, default=90)

    p = add(
        "undo", "restore field edits from an audit log (default: the newest local one)", cmd_undo
    )
    p.add_argument("file", type=Path, nargs="?", help="audit log file")
    p.add_argument(
        "--from-github",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="download the audit log of a GitHub Actions run (default: the latest) via gh",
    )
    p.add_argument("--apply", action="store_true", help="write the restores to YNAB")
    add("test-email", "send a sample email to check the SMTP settings", cmd_test_email)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `run` is the default: `tools4ynab rules --apply` means `run --apply`.
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)

    session = open_session(RulesSession)
    if session is None:
        return 2

    try:
        code, message, log_message = args.fn(session, args), None, None
    except (RuleError, YnabError) as e:
        code, message = 1, session.scrub(str(e))
        log_message = message
    except Exception as e:  # noqa: BLE001 - report and email anything unexpected
        code, message = 1, session.scrub(f"{type(e).__name__} at {where(e)}: {e}")
        # The exception text can hold data (e.g. a KeyError names its key); keep it out of CI logs.
        log_message = f"{type(e).__name__} at {where(e)}" if session.quiet else message
    if session.quiet:
        session.print_trace()
    if message is None:
        return code
    print(f"error: {log_message}", file=sys.stderr)
    if getattr(args, "apply", False):
        subject, text, body = digest.failure(
            message,
            session.audit.entries if session.audit else [],
            notify.run_url(session.env),
        )
        session.send_email(subject, text, body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
