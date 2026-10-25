"""tools4ynab reports end to end against the fake YNAB (same fixtures as the rules tool)."""

from pathlib import Path

from tools_for_ynab.reports.cli import main

AS_OF = ["--as-of", "2026-10-17"]


def seed(y):
    y.groups[0]["categories"].append(y._cat("Books"))
    y.groups.append({"name": "Completed", "deleted": False, "categories": [y._cat("Trip")]})
    cat = y.cat_id
    # Groceries: two purchases a month (so not a fixed recurring charge), rising in July.
    for m in ("05", "06", "07", "08", "09"):
        each = 500 if m in ("05", "06") else 750
        for day in ("02", "17"):
            y.add(
                "chk",
                -each,
                "Shopwell",
                category_id=cat("Groceries"),
                approved=True,
                date=f"2026-{m}-{day}",
            )
    for m, price in zip(
        ("05", "06", "07", "08", "09"), (8.99, 8.99, 8.99, 8.99, 9.99), strict=True
    ):
        y.add(
            "chk",
            -price,
            "Flixly",
            category_id=cat("Restaurants"),
            approved=True,
            date=f"2026-{m}-05",
        )
    for d, amt in (("05", 200), ("06", 270), ("07", 195)):
        y.add(
            "chk",
            -amt,
            "Quill &amp; Page",
            category_id=cat("Books"),
            approved=True,
            date=f"2026-09-{d}",
        )
    y.add("chk", -900, "Cruise Line", category_id=cat("Trip"), approved=True, date="2026-08-10")
    y.add(
        "chk", -50, "Sub", category_id=cat("Groceries"), approved=True, date="2026-10-03"
    )  # partial month


def test_monthly_report_prints_trends_and_recurring_charges(ynab, capsys):
    seed(ynab)
    assert main(AS_OF) == 0
    out = capsys.readouterr().out
    assert "YNAB REPORT FOR SEP 2026" in out
    assert "SPENDING CREEPING UP" in out and "Groceries" in out
    assert "ONE-TIME PURCHASES" in out and "Books" in out and "Quill & Page" in out
    assert "Cruise" not in out  # closed-trip group is excluded
    assert "went up: $8.99 → $9.99" in out and "Flixly" in out
    trends = out.split("SPENDING CREEPING UP")[1].split("ONE-TIME PURCHASES")[0]
    assert "Flixly" not in trends  # recurring charges aren't trends
    assert "Sub" not in out  # the partial current month is ignored


def test_sections_can_be_requested_separately(ynab, capsys):
    seed(ynab)
    assert main(["drift", *AS_OF]) == 0
    out = capsys.readouterr().out
    assert "SPENDING CREEPING UP" in out and "RECURRING" not in out
    assert main(["subscriptions", *AS_OF]) == 0
    out = capsys.readouterr().out
    assert "RECURRING CHARGES" in out and "SPENDING CREEPING UP" not in out


def test_price_change_names_the_rule_it_broke(ynab, capsys):
    seed(ynab)
    Path("rules.yaml").write_text(
        "rules:\n  - name: Flixly price\n"
        "    match: {payee_equals: Flixly, amount: {equals: 8.99}}\n"
        "    set: {category: Restaurants}\n"
    )
    main(["subscriptions", *AS_OF])
    assert "“Flixly price” still expects $8.99" in capsys.readouterr().out


def test_email_goes_to_report_recipients_before_notify_recipients(
    ynab, mailbox, monkeypatch, capsys
):
    seed(ynab)
    assert main(["--email", *AS_OF]) == 0
    assert mailbox[0]["To"] == "me@example.com, partner@example.com"  # NOTIFY_EMAIL_TO fallback
    assert mailbox[0]["Subject"] == "YNAB report for Sep 2026: 1 trending up, 0 down"
    html = mailbox[0].get_body(("html",)).get_content()
    assert "Monthly report: September 2026" in html and html.startswith("<!doctype html>")
    monkeypatch.setenv("REPORT_EMAIL_TO", "partner@example.com")
    main(["--email", *AS_OF])
    assert mailbox[1]["To"] == "partner@example.com"
    for secret in ("test-key-123", "budget-abc", "app-password-xyz"):
        assert secret not in mailbox[0].as_string() and secret not in capsys.readouterr().out


def test_email_without_configuration_fails_loudly(ynab, capsys):
    seed(ynab)
    assert main(["--email", *AS_OF]) == 1
    assert "Email is not configured" in capsys.readouterr().err


def test_reports_yaml_controls_exclusions(ynab, capsys):
    seed(ynab)
    Path("reports.yaml").write_text("exclude_groups: [completed]\nexclude_categories: [Books]\n")
    main(["drift", *AS_OF])
    assert "Books" not in capsys.readouterr().out
    Path("reports.yaml").write_text("bogus: 1\n")
    assert main(["drift", *AS_OF]) == 1
    assert "unknown keys" in capsys.readouterr().err


def test_not_enough_history(ynab, capsys):
    ynab.add("chk", -10, "Shopwell", category_id=ynab.cat_id("Groceries"), date="2026-09-03")
    assert main(["drift", *AS_OF]) == 0
    assert "Not enough history" in capsys.readouterr().out


def test_missing_credentials(ynab, monkeypatch, capsys):
    monkeypatch.delenv("YNAB_API_KEY")
    assert main([]) == 2
    assert "YNAB_API_KEY is not set" in capsys.readouterr().err


def test_api_errors_are_reported_without_secrets(ynab, capsys):
    ynab.fail_on.add("GET /transactions")
    assert main(AS_OF) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "test-key-123" not in err and "budget-abc" not in err


def test_fixed_bill_with_payment_date_jitter_is_not_a_trend(ynab, capsys):
    # Paid every ~30 days, so June holds two payments and the recent window looks "lower":
    # exactly the false alarm the recurring-charge exclusion exists to prevent.
    ynab.groups[0]["categories"].append(ynab._cat("Mortgage"))
    for day in ("05-02", "06-01", "06-30", "07-30", "08-29", "09-28"):
        ynab.add(
            "chk",
            -2000,
            "Bank",
            category_id=ynab.cat_id("Mortgage"),
            approved=True,
            date=f"2026-{day}",
        )
    main(["monthly", *AS_OF])
    out = capsys.readouterr().out
    assert "Mortgage" not in out.split("RECURRING CHARGES")[0]  # not reported as a trend
    assert "Mortgage (Bank)" in out.split("RECURRING CHARGES")[1]  # shown there instead
