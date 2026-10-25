"""Email text for the rules tool: what a run changed, or why it failed."""

from __future__ import annotations

from typing import Any

from tools4ynab.core.notify import render_list


def _line(e: dict[str, Any]) -> str:
    return (
        f"{e['date']}  {(e.get('payee') or '?')[:28]:<28} "
        f"{e['amount'] / 1000:>10.2f}  [{e['rule']}] {e['summary']}"
    )


def digest(
    entries: list[dict[str, Any]],
    audit_file: str | None,
    url: str | None,
    warnings: list[str] = (),
) -> tuple[str, str, str]:
    """(subject, text, html) summarizing applied changes."""
    n = len(entries)
    subject = f"YNAB automation: {n} change{'s' if n != 1 else ''} applied"
    footer = [*warnings]
    if url:
        footer.append(f"Run: {url}")
        footer.append(
            "Undo this run's field edits (category, payee, memo, flag, approval) with: "
            f"tools4ynab rules undo --from-github {url.rsplit('/', 1)[1]}"
            "  (audit log kept ~90 days)"
        )
    elif audit_file:
        footer.append(
            f"Audit log: {audit_file}. Undo field edits with `tools4ynab rules undo --apply`."
        )
    text, body = render_list(subject, [_line(e) for e in entries], footer)
    return subject, text, body


def failure(error: str, entries: list[dict[str, Any]], url: str | None) -> tuple[str, str, str]:
    """(subject, text, html) for a failed run, listing anything applied before it."""
    subject = "YNAB automation FAILED"
    items = [f"error: {error}"]
    if entries:
        items += ["Applied before the failure:", *[_line(e) for e in entries]]
    footer = [f"Run: {url}"] if url else []
    text, body = render_list(subject, items, footer)
    return subject, text, body
