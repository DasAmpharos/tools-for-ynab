"""Run the click commands the way the `tools4ynab` script does, but return the exit code
instead of exiting, so tests can assert on it and read the output with capsys."""

import click

from tools_for_ynab.cli import cli
from tools_for_ynab.reports.cli import reports
from tools_for_ynab.rules.cli import rules


def invoke(command: click.Command, args: list[str], prog_name: str) -> int:
    try:
        command.main(args, prog_name=prog_name)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else int(e.code is not None)
    return 0


def tools4ynab(args: list[str]) -> int:
    return invoke(cli, args, "tools4ynab")


def rules_main(args: list[str]) -> int:
    return invoke(rules, args, "tools4ynab rules")


def reports_main(args: list[str]) -> int:
    return invoke(reports, args, "tools4ynab reports")
