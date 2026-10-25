"""`tools4ynab`: the entry point that groups the tools."""

from __future__ import annotations

import click

from tools_for_ynab.core.commands import CONTEXT_SETTINGS
from tools_for_ynab.reports.cli import reports
from tools_for_ynab.rules.cli import rules


@click.group(context_settings=CONTEXT_SETTINGS)
def cli() -> None:
    """Command-line tools to automate YNAB budgets."""


cli.add_command(rules)
cli.add_command(reports)


if __name__ == "__main__":
    cli()
