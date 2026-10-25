"""In GitHub Actions logs carry counts and diagnostics, never spending data."""

import json
from pathlib import Path

import pytest
from cli_helpers import reports_main, rules_main
from test_reports_cli import seed as seed_reports

from tools4ynab.core.client import YnabClient
from tools4ynab.core.session import quiet_mode, where

RULES = """\
settings: {since_days: 30, scope: unapproved}
rules:
  - name: Rule A
    match: {payee_equals: Zxcv Mart, direction: outflow}
    set: {category: Groceries}
splits:
  - name: Split B
    match: {payee_equals: Plugh Co, amount: {equals: 27.98}, direction: outflow}
    parts:
      - {category: Comedy, amount: 4.99}
      - {category: Assistant, amount: 15.00}
      - {category: Sketches, amount: 7.99}
transfers:
  - name: Transfer C
    from: {account: Acme Checking, payee_contains: sweep out}
    to: {account: Acme Savings, payee_contains: sweep in}
"""
SECRET_BITS = (
    "Zxcv",
    "Plugh",
    "Groceries",
    "Restaurants",
    "42.00",
    "27.98",
    "Acme Checking",
    "sweep",
)


@pytest.fixture
def world(ynab, monkeypatch):
    Path("rules.yaml").write_text(RULES)
    ynab.add("chk", -42.00, "Zxcv Mart", category_id=ynab.cat_id("Restaurants"))
    ynab.add("chk", -27.98, "Plugh Co")
    ynab.add("chk", -0.64, "sweep out to savings")
    ynab.add("sav", 0.64, "sweep in from checking")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    return ynab


def output(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_quiet_mode_detection():
    assert quiet_mode({"GITHUB_ACTIONS": "true"})
    assert not quiet_mode({})  # local runs are verbose
    assert not quiet_mode({"GITHUB_ACTIONS": "true", "YNAB_VERBOSE": "1"})
    assert quiet_mode({"GITHUB_ACTIONS": "true", "YNAB_VERBOSE": ""})


def test_rules_run_logs_counts_and_rule_names_only(world, capsys):
    assert rules_main(["run", "--apply"]) == 0
    out = output(capsys)
    for bit in SECRET_BITS:
        assert bit not in out
    assert "Applying 1 categorization(s), 1 split(s), 1 transfer pair(s)" in out
    assert "[Rule A] x1: approved, category_id" in out and "[Split B] x1: split" in out
    assert "[Transfer C] x1: transfer pair" in out


def test_quiet_run_still_shows_the_api_calls_for_debugging(world, capsys):
    rules_main(["run"])
    out = output(capsys)
    assert "API calls:" in out and "GET /budgets/<budget>/categories -> 200" in out
    assert "budget-abc" not in out and "test-key-123" not in out


def test_verbose_switch_restores_the_details(world, monkeypatch, capsys):
    monkeypatch.setenv("YNAB_VERBOSE", "1")
    rules_main(["run"])
    out = output(capsys)
    assert "Zxcv Mart" in out and "API calls:" not in out


def test_step_summary_has_no_spending_data(world, monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rules_main(["run"])
    text = summary.read_text()
    assert "Rule A" in text and not any(bit in text for bit in SECRET_BITS)


def test_audit_file_is_redacted_but_undo_still_works(world, capsys):
    k = next(t for t in world.txns.values() if t["payee_name"] == "Zxcv Mart")
    rules_main(["run", "--apply"])
    text = next(Path("audit").glob("*.jsonl")).read_text()
    for bit in ("Zxcv", "Plugh", "sweep", "Acme", "42000", "27980"):
        assert bit not in text
    entry = json.loads(text.splitlines()[0])
    assert entry["payee"] is None and entry["amount"] is None
    assert entry["summary"].startswith("fields:")

    capsys.readouterr()
    assert rules_main(["undo", "--apply"]) == 0
    assert k["category_id"] == world.cat_id("Restaurants") and not k["approved"]


def test_the_email_still_has_the_details(world, mailbox, capsys):
    rules_main(["run", "--apply"])
    body = mailbox[0].get_body(("plain",)).get_content()
    assert "Zxcv Mart" in body and "42.00" in body


def test_duplicates_are_counted_not_described(world, capsys):
    world.add("chk", -7.77, "Cafe Luna", cleared="uncleared", import_id=None)
    world.add("chk", -7.77, "Cafe Luna")
    rules_main(["run"])
    out = output(capsys)
    assert "1 possible duplicate(s)" in out and "Cafe" not in out


def test_unexpected_errors_report_the_place_not_the_data(world, monkeypatch, capsys):
    def boom(*a, **k):
        raise KeyError("Zxcv Mart")

    monkeypatch.setattr("tools4ynab.rules.cli.apply_plan", boom)
    assert rules_main(["run", "--apply"]) == 1
    err = output(capsys)
    assert "KeyError at cli.py:" in err and "Zxcv" not in err
    monkeypatch.setenv("YNAB_VERBOSE", "1")  # verbose: the full message is fine locally
    rules_main(["run", "--apply"])
    assert "Zxcv Mart" in output(capsys)


def test_api_failures_are_visible_in_the_trace(world, capsys):
    world.fail_on.add("PATCH /transactions")
    assert rules_main(["run", "--apply"]) == 1
    out = output(capsys)
    assert "PATCH /budgets/<budget>/transactions -> 500" in out and "error:" in out
    assert "budget-abc" not in out and "test-key-123" not in out


def test_reports_log_only_counts(ynab, monkeypatch, capsys):
    seed_reports(ynab)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert reports_main(["--as-of", "2026-10-17"]) == 0
    out = output(capsys)
    for bit in ("Books", "Quill", "Shopwell", "Flixly", "$", "Groceries"):
        assert bit not in out
    assert "Report for 2026-10: 1 trending up, 0 down, 1 one-time" in out
    assert "Nothing sent" in out and "API calls:" in out


def test_reports_email_is_still_complete_while_the_log_is_not(ynab, mailbox, monkeypatch, capsys):
    seed_reports(ynab)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert reports_main(["--email", "--as-of", "2026-10-17"]) == 0
    assert "Quill & Page" in mailbox[0].get_body(("plain",)).get_content()
    assert "Quill" not in output(capsys)
    monkeypatch.setenv("YNAB_VERBOSE", "1")
    reports_main(["--as-of", "2026-10-17"])
    assert "Quill & Page" in output(capsys)


def test_reports_unexpected_errors_report_the_place_not_the_data(ynab, monkeypatch, capsys):
    seed_reports(ynab)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    def boom(*a, **k):
        raise ValueError("Shopwell")

    monkeypatch.setattr("tools4ynab.reports.cli.analyze_drift", boom)
    assert reports_main(["--as-of", "2026-10-17"]) == 1
    err = output(capsys)
    assert "ValueError at cli.py:" in err and "Shopwell" not in err


def test_where_names_the_code_location_only():
    try:
        raise RuntimeError("secret")
    except RuntimeError as e:
        located = where(e)
    assert located.startswith("test_quiet.py:") and "secret" not in located


def test_client_trace_redacts_the_budget_id(ynab):
    client = YnabClient("test-key-123", "budget-abc")
    client.categories()
    assert client.trace[0].startswith("GET /budgets/<budget>/categories -> 200")
