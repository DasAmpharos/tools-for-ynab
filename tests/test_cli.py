"""The `tools4ynab` group and the default-subcommand behavior of its tools."""

from cli_helpers import tools4ynab


def test_rules_with_no_command_is_a_dry_run(ynab, capsys):
    assert tools4ynab(["rules"]) == 0
    assert "Dry run:" in capsys.readouterr().out


def test_options_without_a_command_go_to_the_default(ynab, capsys):
    assert tools4ynab(["rules", "--apply"]) == 0
    assert "Applying" in capsys.readouterr().out


def test_help_lists_the_tools_and_their_commands(capsys):
    assert tools4ynab(["-h"]) == 0
    out = capsys.readouterr().out
    assert "rules" in out and "reports" in out
    assert tools4ynab(["rules", "-h"]) == 0  # help for the group, not for `run`
    assert "backtest" in capsys.readouterr().out
    assert tools4ynab(["reports", "--help"]) == 0
    assert "subscriptions" in capsys.readouterr().out


def test_unknown_tool_or_option_is_a_usage_error(capsys):
    assert tools4ynab(["budget"]) == 2
    assert "No such command 'budget'" in capsys.readouterr().err
    assert tools4ynab(["rules", "run", "--bogus"]) == 2
    assert tools4ynab(["reports", "--as-of", "October"]) == 2
