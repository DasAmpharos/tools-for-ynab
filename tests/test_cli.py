"""The `tools4ynab` entry point hands its arguments to the rules or reports tool."""

from tools_for_ynab import cli


def test_dispatches_the_rest_of_the_arguments(monkeypatch):
    seen = {}
    monkeypatch.setitem(cli.TOOLS, "rules", lambda argv: seen.setdefault("rules", argv) and 7)
    assert cli.main(["rules", "run", "--apply"]) == 7
    assert seen["rules"] == ["run", "--apply"]


def test_rules_dry_run_end_to_end(ynab, capsys):
    assert cli.main(["rules"]) == 0
    assert "Dry run:" in capsys.readouterr().out


def test_unknown_or_missing_tool_prints_usage(capsys):
    assert cli.main([]) == 2
    assert cli.main(["budget"]) == 2
    assert "usage: tools4ynab" in capsys.readouterr().err
    assert cli.main(["--help"]) == 0
    assert "rules" in capsys.readouterr().out
