"""tools4ynab budget copy, end to end against the fake YNAB."""

from datetime import date

from cli_helpers import invoke, tools4ynab

from tools4ynab.budget.cli import budget
from tools4ynab.budget.plan import next_month

OCT, NOV = "2026-10-01", "2026-11-01"
FROM = ["--from", "2026-10"]


def main(args):
    return invoke(budget, args, "tools4ynab budget")


def seed(y):
    y.groups[0]["categories"].append({**y._cat("Retired"), "hidden": True})
    y.groups.append(
        {
            "name": "Internal Master Category",
            "deleted": False,
            "categories": [y._cat("Inflow")],
        }
    )
    for name, dollars in (
        ("Groceries", 600),
        ("Restaurants", 150),
        ("Comedy", 5),
        ("Retired", 40),
        ("Inflow", 1),
    ):
        y.assigned[(OCT, y.cat_id(name))] = dollars * 1000


def test_next_month_wraps_the_year():
    assert next_month(date(2026, 10, 31)) == date(2026, 11, 1)
    assert next_month(date(2026, 12, 15)) == date(2027, 1, 1)


def test_dry_run_lists_amounts_and_writes_nothing(ynab, capsys):
    seed(ynab)
    assert main(FROM) == 0
    out = capsys.readouterr().out
    assert "Dry run: copying assigned amounts from October 2026 to November 2026" in out
    assert "Everyday: Groceries" in out and "600.00" in out
    assert "Retired" not in out and "Inflow" not in out  # hidden and internal are skipped
    assert "Re-run with --apply" in out
    assert not any(method == "PATCH" for method, _ in ynab.calls)


def test_apply_copies_into_the_next_month(ynab, capsys):
    seed(ynab)
    assert main([*FROM, "--apply"]) == 0
    assert "Done: assigned 3 categories." in capsys.readouterr().out
    nov = {cat: v for (month, cat), v in ynab.assigned.items() if month == NOV}
    assert nov == {
        ynab.cat_id("Groceries"): 600_000,
        ynab.cat_id("Restaurants"): 150_000,
        ynab.cat_id("Comedy"): 5_000,
    }
    # Running it again has nothing left to do.
    assert main([*FROM, "--apply"]) == 0
    assert "0 to copy, 3 already match" in capsys.readouterr().out


def test_amounts_already_set_are_kept_unless_overwrite(ynab, capsys):
    seed(ynab)
    ynab.assigned[(NOV, ynab.cat_id("Groceries"))] = 700_000
    ynab.assigned[(NOV, ynab.cat_id("Sketches"))] = 9_000  # nothing in October: untouched
    assert main([*FROM, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "2 to copy, 1 kept" in out and "(October: 600.00)" in out
    assert ynab.assigned[(NOV, ynab.cat_id("Groceries"))] == 700_000

    assert main([*FROM, "--overwrite", "--apply"]) == 0
    assert "(replaces 700.00)" in capsys.readouterr().out
    assert ynab.assigned[(NOV, ynab.cat_id("Groceries"))] == 600_000
    assert ynab.assigned[(NOV, ynab.cat_id("Sketches"))] == 9_000


def test_explicit_target_month(ynab, capsys):
    seed(ynab)
    assert main([*FROM, "--to", "2027-01", "--apply"]) == 0
    assert ynab.assigned[("2027-01-01", ynab.cat_id("Groceries"))] == 600_000
    assert main(["--from", "2026-10", "--to", "2026-10"]) == 2


def test_quiet_mode_prints_counts_only(ynab, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    seed(ynab)
    assert main(FROM) == 0
    out = capsys.readouterr().out
    assert "3 to copy" in out
    assert "Groceries" not in out and "600" not in out


def test_failure_partway_says_how_far_it_got(ynab, capsys):
    seed(ynab)
    ynab.fail_on.add(f"PATCH /months/{NOV}/categories/{ynab.cat_id('Comedy')}")
    assert main([*FROM, "--apply"]) == 1
    err = capsys.readouterr().err
    assert "after 1 of 3; re-run to copy the rest" in err
    assert "test-key-123" not in err and "budget-abc" not in err


def test_reachable_from_the_top_level_command(ynab, capsys):
    seed(ynab)
    assert tools4ynab(["budget", "copy", *FROM]) == 0
    assert "Everyday: Groceries" in capsys.readouterr().out
