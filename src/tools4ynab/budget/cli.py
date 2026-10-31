"""`tools4ynab budget`: set up a month's budget.

`copy` (the default) fills next month with this month's assigned amounts as a starting
point; a dry run unless --apply."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import click

from tools4ynab.core.client import YnabError
from tools4ynab.core.commands import DefaultGroup, execute
from tools4ynab.core.session import Session

from .plan import next_month, plan_copy


def run_copy(
    session: Session,
    source: datetime | None,
    target: datetime | None,
    overwrite: bool,
    apply: bool,
) -> int:
    src = (source.date() if source else date.today()).replace(day=1)
    dst = target.date() if target else next_month(src)
    if dst == src:
        click.echo("error: --from and --to are the same month", err=True)
        return 2

    client = session.client
    plan = plan_copy(
        src, dst, client.categories(), client.assigned(src), client.assigned(dst), overwrite
    )
    mode = "Copying" if apply else "Dry run: copying"
    click.echo(f"{mode} assigned amounts from {src:%B %Y} to {dst:%B %Y}: {plan.summary()}")
    if not session.quiet:  # Actions logs are readable by anyone with repo access
        click.echo("\n".join(plan.lines()))

    if not apply:
        if plan.copy:
            click.echo("Re-run with --apply to write these to YNAB.")
        return 0

    for done, item in enumerate(plan.copy):
        try:
            client.set_assigned(dst, item.category_id, item.amount)
        except YnabError as e:
            # Categories already copied now match, so a re-run picks up where this stopped
            # (useful after YNAB's limit of 200 requests an hour).
            raise YnabError(
                f"{e} (after {done} of {len(plan.copy)}; re-run to copy the rest)"
            ) from None
    click.echo(f"Done: assigned {len(plan.copy)} categor{'y' if len(plan.copy) == 1 else 'ies'}.")
    return 0


month_type = click.DateTime(["%Y-%m"])


@click.group(cls=DefaultGroup, default_command="copy")
def budget() -> None:
    """Set up a month's budget.

    With no command, runs `copy`."""


@budget.command("copy")
@click.option(
    "--from",
    "source",
    type=month_type,
    metavar="YYYY-MM",
    help="Month to copy. [default: this month]",
)
@click.option(
    "--to",
    "target",
    type=month_type,
    metavar="YYYY-MM",
    help="Month to fill in. [default: the month after --from]",
)
@click.option(
    "--overwrite", is_flag=True, help="Also replace amounts already assigned in the --to month."
)
@click.option("--apply", is_flag=True, help="Write the amounts to YNAB.")
def cmd_copy(**params: Any) -> None:
    """Copy one month's assigned amounts into the next, as a starting point.

    A dry run unless --apply. Only categories with an amount assigned are copied, and
    amounts you already set in the --to month are kept unless --overwrite."""
    click.get_current_context().exit(execute(run_copy, params))
