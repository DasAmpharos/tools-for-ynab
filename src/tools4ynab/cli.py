"""`tools4ynab`: the entry point that groups the tools."""

from __future__ import annotations

import click

from tools4ynab.core.commands import CONTEXT_SETTINGS
from tools4ynab.reports.cli import reports
from tools4ynab.rules.cli import rules


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(package_name="tools4ynab")
def cli() -> None:
    """Command-line tools to automate YNAB budgets."""


cli.add_command(rules)
cli.add_command(reports)


if __name__ == "__main__":
    cli()
