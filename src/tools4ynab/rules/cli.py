"""`tools4ynab rules`: the commands that apply the rules, and the ones that help you write,
check and reverse them."""

from __future__ import annotations

import functools
import os
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import click

from tools4ynab.core import notify
from tools4ynab.core.client import YnabError
from tools4ynab.core.commands import DefaultGroup
from tools4ynab.core.session import Session, open_session, where

from . import analysis, digest
from .audit import AuditLog, fetch_github_audit, latest_log, load_entries, plan_undo
from .planner import Context, apply_plan, build_plan, load_context
from .rules import RuleError, RuleSet, disable, load_rules, parse_disabled


class RulesSession(Session):
    """A session that also remembers this run's audit log, so a failure can report it."""

    audit: AuditLog | None = None


def _history(session: RulesSession, days: float) -> list[dict[str, Any]]:
    return session.client.transactions(date.today() - timedelta(days=round(days)))


def _months_to_days(months: float) -> float:
    return months * 30.44


def _load_context(session: RulesSession, rules_path: Path, *, disabled: bool = False) -> Context:
    ruleset: RuleSet = load_rules(rules_path)
    names = parse_disabled(session.env.get("YNAB_DISABLED_RULES")) if disabled else []
    if names:
        ruleset = disable(ruleset, names)
        click.echo(f"Disabled via YNAB_DISABLED_RULES: {', '.join(names)}")
    return load_context(session.client, ruleset)


# --- plumbing -----------------------------------------------------------------


def with_session(fn: Callable[..., int]) -> Callable[..., None]:
    """Run a command with a session: report errors without leaking secrets (or, in Actions,
    data), email the failure of an --apply run, and exit with the command's code."""

    @functools.wraps(fn)
    def command(**params: Any) -> None:
        click.get_current_context().exit(_execute(fn, params))

    return command


def _execute(fn: Callable[..., int], params: dict[str, Any]) -> int:
    session = open_session(RulesSession)
    if session is None:
        return 2

    try:
        code, message, log_message = fn(session, **params), None, None
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
    click.echo(f"error: {log_message}", err=True)
    if params.get("apply"):
        subject, text, body = digest.failure(
            message,
            session.audit.entries if session.audit else [],
            notify.run_url(session.env),
        )
        session.send_email(subject, text, body)
    return 1


def _write_step_summary(mode: str, scanned: int, lines: list[str]) -> None:
    """Surface results on the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    body = "\n".join(f"- `{line}`" for line in lines) or "_No matching transactions._"
    with open(path, "a") as f:
        f.write(f"### {mode} {len(lines)} change(s) ({scanned} scanned)\n\n{body}\n")


rules_option = click.option(
    "--rules",
    "rules_path",
    type=click.Path(path_type=Path),
    default="rules.yaml",
    show_default=True,
    help="Rules file.",
)
audit_dir_option = click.option(
    "--audit-dir",
    type=click.Path(path_type=Path),
    default="audit",
    envvar="YNAB_AUDIT_DIR",
    show_default=True,
    help="Where applied changes are logged.",
)
months_option = click.option(
    "--months", type=float, default=6, show_default=True, help="How much history to use."
)


# --- commands -----------------------------------------------------------------


@click.group(cls=DefaultGroup, default_command="run")
def rules() -> None:
    """Auto-categorize transactions with the rules in rules.yaml.

    With no command, runs `run`."""


@rules.command("run")
@rules_option
@audit_dir_option
@click.option("--apply", is_flag=True, help="Write the changes to YNAB.")
@with_session
def cmd_run(session: RulesSession, rules_path: Path, audit_dir: Path, apply: bool) -> int:
    """Apply the rules (a dry run unless --apply)."""
    ctx = _load_context(session, rules_path, disabled=True)
    all_txns = _history(session, ctx.ruleset.settings.since_days)
    plan = build_plan(ctx, all_txns)

    mode = "Applying" if apply else "Dry run:"
    click.echo(
        f"{mode} {len(plan.changes)} categorization(s), {len(plan.splits)} split(s), "
        f"{len(plan.pairs)} transfer pair(s) from {plan.scanned} transaction(s)"
    )
    # In GitHub Actions logs: counts and rule names only, never payees, amounts or categories.
    lines = plan.quiet_lines() if session.quiet else plan.lines()
    click.echo("\n".join(lines))
    _write_step_summary(mode, plan.scanned, lines)

    duplicates = analysis.find_duplicates(all_txns)
    warnings = [
        f"Possible duplicate: {analysis._desc(a)} ({a['account_name']}) looks like "
        f"{analysis._desc(b)}, which the bank imported; check for double-counting"
        for a, b in duplicates
    ]
    if session.quiet and duplicates:
        click.echo(
            f"{len(duplicates)} possible duplicate(s); run `tools4ynab rules duplicates` locally"
        )
    elif warnings:
        click.echo("\n".join(warnings))

    if not apply:
        if plan.total:
            click.echo("Re-run with --apply to write these changes.")
        return 0

    session.audit = AuditLog(audit_dir, redact=session.quiet)
    outcomes = apply_plan(ctx, plan, session.audit)
    click.echo("\n".join(outcomes))
    click.echo("Done.")
    if session.audit.entries:
        subject, text, body = digest.digest(
            session.audit.entries,
            str(session.audit.path) if session.audit.path else None,
            notify.run_url(session.env),
            warnings,
        )
        session.send_email(subject, text, body)
    return 0


@rules.command("backtest")
@rules_option
@months_option
@with_session
def cmd_backtest(session: RulesSession, rules_path: Path, months: float) -> int:
    """Replay the rules over history and flag disagreements.

    Exits with code 1 if any are found."""
    ctx = _load_context(session, rules_path)
    report = analysis.backtest(ctx, _history(session, _months_to_days(months)))
    click.echo("\n".join(report.lines))
    return 1 if report.problems else 0


@rules.command("unmatched")
@rules_option
@months_option
@click.option("--limit", type=int, default=25, show_default=True, help="How many payees to list.")
@with_session
def cmd_unmatched(session: RulesSession, rules_path: Path, months: float, limit: int) -> int:
    """List the payees no rule covers yet, busiest first."""
    ctx = _load_context(session, rules_path)
    txns = _history(session, _months_to_days(months))
    click.echo("\n".join(analysis.unmatched(ctx, txns, limit)))
    return 0


@rules.command("suggest")
@rules_option
@months_option
@click.option(
    "--min-count", type=int, default=3, show_default=True, help="Fewest matching transactions."
)
@click.option(
    "--max-age-days", type=int, default=120, show_default=True, help="Skip payees idle longer."
)
@click.option(
    "--output", type=click.Path(path_type=Path), help="Write YAML here instead of printing."
)
@with_session
def cmd_suggest(
    session: RulesSession,
    rules_path: Path,
    months: float,
    min_count: int,
    max_age_days: int,
    output: Path | None,
) -> int:
    """Propose rules that were right every time in your history."""
    ctx = _load_context(session, rules_path)
    txns = _history(session, _months_to_days(months))
    sugs = analysis.suggest(ctx, txns, min_count, max_age_days)
    if not sugs:
        click.echo("No new rules to suggest.")
        return 0
    text = analysis.render_suggestions(sugs, months)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
        click.echo(f"Wrote {len(sugs)} suggested rule(s) to {output}")
    else:
        click.echo(text)
    return 0


@rules.command("explain")
@rules_option
@click.argument("query")
@click.option("--days", type=int, default=60, show_default=True, help="How far back to look.")
@with_session
def cmd_explain(session: RulesSession, rules_path: Path, query: str, days: int) -> int:
    """Show what the rules would do for transactions matching QUERY.

    QUERY is payee text or the start of a transaction id."""
    ctx = _load_context(session, rules_path)
    click.echo("\n".join(analysis.explain(ctx, _history(session, days), query)))
    return 0


@rules.command("duplicates")
@click.option("--days", type=int, default=90, show_default=True, help="How far back to look.")
@with_session
def cmd_duplicates(session: RulesSession, days: int) -> int:
    """Find manual entries that duplicate a bank import."""
    found = analysis.find_duplicates(_history(session, days))
    for a, b in found:
        click.echo(f"{analysis._desc(a)} ({a['account_name']}, manual, uncleared)")
        click.echo(f"  looks like {analysis._desc(b)} (bank import, cleared)")
    click.echo(f"{len(found)} possible duplicate(s)")
    return 0


@rules.command("undo")
@click.argument("file", type=click.Path(path_type=Path), required=False)
@audit_dir_option
@click.option(
    "--from-github",
    is_flag=False,
    flag_value="latest",
    default=None,
    metavar="RUN_ID",
    help="Download the audit log of a GitHub Actions run (default: the latest) via gh.",
)
@click.option("--apply", is_flag=True, help="Write the restores to YNAB.")
@with_session
def cmd_undo(
    session: RulesSession,
    file: Path | None,
    audit_dir: Path,
    from_github: str | None,
    apply: bool,
) -> int:
    """Restore field edits from an audit log.

    FILE defaults to the newest log in --audit-dir."""
    if from_github:
        run_id = None if from_github == "latest" else from_github
        with tempfile.TemporaryDirectory() as tmp:
            paths = fetch_github_audit(run_id, Path(tmp))
            entries = [e for p in paths for e in load_entries(p)]
        source = f"GitHub run {run_id or '(latest)'}"
    else:
        path = file or latest_log(audit_dir)
        if path is None:
            click.echo(f"No audit logs found in {audit_dir}/", err=True)
            return 1
        entries, source = load_entries(path), str(path)
    patches, notes = plan_undo(session.client, entries)
    click.echo(f"{source}: {len(entries)} change(s) logged, {len(patches)} can be restored")
    click.echo("\n".join(notes))
    if not apply:
        if patches:
            click.echo("Re-run with --apply to restore them.")
        return 0
    if patches:
        session.client.update_transactions(patches)
    click.echo(f"Restored {len(patches)} transaction(s).")
    return 0


@rules.command("test-email")
@with_session
def cmd_test_email(session: RulesSession) -> int:
    """Send a sample email to check the SMTP settings.

    Nothing changes in YNAB."""
    cfg = notify.EmailConfig.from_env(session.env)
    if cfg is None:
        missing = [
            v for v in ("SMTP_USER", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO") if not session.env.get(v)
        ]
        click.echo(f"Email is not configured; missing: {', '.join(missing)}", err=True)
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
        click.echo(f"error: {session.scrub(f'{type(e).__name__}: {e}')}", err=True)
        return 1
    click.echo(
        f"Sent a test email to {len(cfg.recipients)} recipient(s) via {cfg.host}:{cfg.port}."
    )
    return 0
