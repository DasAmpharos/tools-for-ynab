from datetime import date

import pytest

from tools_for_ynab.reports.config import ReportConfig, ReportError, load_config
from tools_for_ynab.reports.data import Spend, complete_months, spend_items
from tools_for_ynab.reports.drift import analyze_drift, sparkline
from tools_for_ynab.reports.render import build_report, money
from tools_for_ynab.reports.subscriptions import find_subscriptions
from tools_for_ynab.rules.rules import Actions, Match, Rule

CATS = [
    {"id": "g", "name": "Groceries", "group_name": "Everyday"},
    {"id": "b", "name": "Books", "group_name": "Everyday"},
    {"id": "t", "name": "Trip", "group_name": "Completed"},
    {"id": "r", "name": "Inflow: Ready to Assign", "group_name": "Internal Master Category"},
]
AS_OF = date(2026, 10, 17)
MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]


def sp(day, amount, payee="Shop", cat="Books", group="Everyday"):
    return Spend(day, day[:7], cat, cat, group, payee, amount, f"t-{day}-{payee}-{amount}")


def txn(day, amount, cat="g", payee="Shop", **kw):
    return {
        "id": f"{day}{amount}",
        "date": day,
        "amount": round(amount * -1000),
        "category_id": cat,
        "payee_name": payee,
        "transfer_account_id": None,
        "subtransactions": [],
        **kw,
    }


# --- data -----------------------------------------------------------------------------


def test_spend_items_rules_of_what_counts():
    txns = [
        txn("2026-09-01", 10, "g", "A &amp; B"),  # HTML-escaped payee from YNAB
        txn("2026-09-02", 20, "g", transfer_account_id="x"),  # transfer
        txn("2026-09-03", -5, "g"),  # refund -> negative spend
        txn("2026-09-04", 50, "t"),  # excluded group
        txn("2026-09-05", -3000, "r"),  # income
        txn("2026-09-06", 99, None),  # uncategorized
        txn(
            "2026-09-07",
            30,
            None,
            subtransactions=[
                {"category_id": "g", "amount": -10000},
                {"category_id": "b", "amount": -20000},
            ],
        ),
    ]
    items = spend_items(txns, CATS, ReportConfig())
    assert sorted((i.category, i.amount) for i in items) == [
        ("Books", 20.0),
        ("Groceries", -5.0),
        ("Groceries", 10.0),
        ("Groceries", 10.0),
    ]
    assert any(i.payee == "A & B" for i in items)  # unescaped so payees merge


@pytest.mark.parametrize(
    "first,as_of,expected",
    [
        ("2026-04-27", date(2026, 10, 17), MONTHS),  # April began mid-month: partial
        ("2026-05-02", date(2026, 10, 17), MONTHS),  # May is whole
        ("2025-11-20", date(2026, 2, 3), ["2025-12", "2026-01"]),  # across a year
    ],
)
def test_complete_months(first, as_of, expected):
    got = complete_months([txn(first, 1)], as_of)
    assert got == (expected if first != "2026-05-02" else ["2026-05", *MONTHS[1:]])


# --- drift ----------------------------------------------------------------------------


def books_items():
    return [
        sp("2026-08-15", 41, "2nd & Charles"),
        sp("2026-09-05", 200, "Quill & Page"),
        sp("2026-09-06", 270, "Quill & Page"),
        sp("2026-09-07", 195, "Quill & Page"),
    ]


def test_one_month_blip_is_a_spike_with_its_driver():
    r = analyze_drift(books_items(), MONTHS, ReportConfig())
    [item] = r.items
    assert item.kind == "spike" and item.spike_month == "2026-09"
    assert round(item.recent_avg) == 235 and item.previous_avg == 0
    assert item.drivers[0].payee == "Quill & Page" and item.drivers[0].count == 3
    assert item.series == [0, 0, 0, 41, 665]


def test_lasting_rise_is_sustained():
    items = [sp(f"{m}-10", 1000 if m < "2026-07" else 1500, cat="Groceries") for m in MONTHS]
    [item] = analyze_drift(items, MONTHS, ReportConfig()).items
    assert item.kind == "sustained" and round(item.delta) == 500 and round(item.percent) == 50


def test_small_moves_are_not_reported():
    flat = [sp(f"{m}-10", 1000, cat="Groceries") for m in MONTHS]
    under_dollars = [sp(f"{m}-10", 10 if m < "2026-07" else 30, cat="Groceries") for m in MONTHS]
    under_percent = [
        sp(f"{m}-10", 1000 if m < "2026-07" else 1200, cat="Groceries") for m in MONTHS
    ]
    for items in (flat, under_dollars, under_percent):
        assert analyze_drift(items, MONTHS, ReportConfig()).items == []


def test_drop_and_settling_after_a_spike():
    drop = [sp(f"{m}-10", 1000 if m < "2026-07" else 400, cat="Groceries") for m in MONTHS]
    assert analyze_drift(drop, MONTHS, ReportConfig()).items[0].kind == "down"
    settled = [sp("2026-05-10", 900, cat="Gifts")]  # one big month, then nothing
    [item] = analyze_drift(settled, MONTHS, ReportConfig()).items
    assert item.kind == "settled" and item.spike_month == "2026-05"


def test_not_enough_history_says_so():
    r = analyze_drift(books_items(), MONTHS[-3:], ReportConfig())
    assert r.items == [] and "complete months" in r.note


def test_group_rollup_without_drivers():
    items = [sp("2026-09-05", 300, "A", cat="Books"), sp("2026-08-05", 300, "B", cat="Movies")]
    r = analyze_drift(items, MONTHS, ReportConfig(), by="group", with_drivers=False)
    assert r.items[0].key == "Everyday" and r.items[0].drivers == []


def test_sparkline():
    assert sparkline([0, 0, 0]) == "▁▁▁" and sparkline([]) == ""
    s = sparkline([0, 5, 10])
    assert s[0] == "▁" and s[-1] == "█"


# --- subscriptions --------------------------------------------------------------------


def monthly(payee, cat, amounts, start="2026-05-05", **kw):
    y, m, d = int(start[:4]), int(start[5:7]), start[8:]
    out = []
    for i, a in enumerate(amounts):
        mm = m + i
        out.append(sp(f"{y + (mm - 1) // 12}-{(mm - 1) % 12 + 1:02d}-{d}", a, payee, cat, **kw))
    return out


def flixly_rule():
    return Rule(
        "Flixly",
        Match.parse({"payee_equals": "Flixly", "amount": {"equals": 8.99}}),
        Actions(category="Flixly"),
    )


def test_finds_monthly_fixed_charge_and_price_change_with_stale_rule():
    items = monthly("Flixly", "Flixly", [8.99, 8.99, 8.99, 8.99, 9.99])
    [s] = find_subscriptions(items, AS_OF, [flixly_rule()])
    assert (s.status, s.amount, s.count) == ("active", 9.99, 5)
    assert s.price_change == (8.99, 9.99, "2026-09-05")
    assert s.stale_rules == ["Flixly"]  # the rule still expects the old price


def test_a_rule_that_matches_any_price_is_not_stale():
    items = monthly("Flixly", "Flixly", [8.99, 8.99, 8.99, 9.99])
    rule = Rule("Any", Match.parse({"payee_equals": "Flixly"}), Actions(category="N"))
    assert find_subscriptions(items, AS_OF, [rule])[0].stale_rules == []


def test_stopped_new_and_ignored_patterns():
    stopped = monthly("Gym", "Gym", [30, 30, 30], start="2026-04-05")  # last charge 2026-06-05
    new = monthly("Streamer", "Streamer", [10, 10], start="2026-09-02")
    bills = monthly("Power", "Electric", [150, 162, 251, 294, 300])  # amounts keep changing
    weekly = [sp(f"2026-09-{d:02d}", 20, "Corner", "Food") for d in (1, 8, 15, 22, 29)]
    subs = {s.payee: s for s in find_subscriptions(stopped + new + bills + weekly, AS_OF)}
    assert set(subs) == {"Gym", "Streamer"}
    assert subs["Gym"].status == "stopped" and subs["Streamer"].status == "new"


def test_two_old_charges_are_not_enough():
    assert find_subscriptions(monthly("Old", "Old", [5, 5], start="2026-05-05"), AS_OF) == []


def test_split_bundle_lines_become_separate_subscriptions():
    items = []
    for m in ("2026-07", "2026-08", "2026-09"):
        items += [
            sp(f"{m}-16", 4.99, "AppHub", "Comedy"),
            sp(f"{m}-16", 15.0, "AppHub", "Assistant"),
        ]
    assert {(s.category, s.amount) for s in find_subscriptions(items, AS_OF)} == {
        ("Comedy", 4.99),
        ("Assistant", 15.0),
    }


# --- rendering and config -----------------------------------------------------------------


def render(items, subs=()):
    cfg = ReportConfig()
    return build_report(
        analyze_drift(items, MONTHS, cfg),
        analyze_drift(items, MONTHS, cfg, by="group", with_drivers=False),
        list(subs),
        MONTHS,
        AS_OF,
        cfg,
        "https://example/run/1",
    )


def test_report_text_and_html():
    subject, text, html = render(
        books_items()
        + [sp(f"{m}-10", 1000 if m < "2026-07" else 1500, "Shopwell", "Groceries") for m in MONTHS]
    )
    assert subject == "YNAB report for Sep 2026: 1 trending up, 0 down"
    for heading in ("AT A GLANCE", "SPENDING CREEPING UP", "ONE-TIME PURCHASES"):
        assert heading in text
    assert "Mostly Quill & Page ($665)" in text and "Run: https://example/run/1" in text
    assert "Monthly report: September 2026" in html and "Spending creeping up" in html
    assert "Large in a single month" in html  # each section says what it is for


def test_glance_explains_the_headline_numbers():
    _, text, _ = render(
        books_items()
        + [sp(f"{m}-10", 1000 if m < "2026-07" else 1500, "Shopwell", "Groceries") for m in MONTHS]
    )
    glance = text.split("AT A GLANCE")[1].split("\n\n")[0]
    assert "1 category is trending up, about $500/month more than before" in glance
    assert "1 category had a large one-month spike" in glance


def test_sections_are_capped_with_a_more_line():
    items = [
        sp(f"{m}-10", 100 if m < "2026-07" else 400, "Shop", f"Cat{i}")
        for i in range(9)
        for m in MONTHS
    ]
    cfg = ReportConfig(max_items=4)
    _, text, html = build_report(
        analyze_drift(items, MONTHS, cfg),
        analyze_drift(items, MONTHS, cfg, by="group"),
        [],
        MONTHS,
        AS_OF,
        cfg,
    )
    assert text.count("/mo now, was") == 4 and "+ 5 smaller changes not shown" in text
    assert "+ 5 more smaller changes not shown" in html


def test_down_rows_say_what_was_cut_not_what_was_spent():
    items = [sp(f"{m}-10", 300, "Old Habit", "Cat") for m in ("2026-05", "2026-06")]
    items += [sp(f"{m}-10", 20, "Other", "Cat") for m in MONTHS[2:]]
    _, text, _ = render(items)
    assert "Less at Old Habit (was $300/mo)" in text


def test_cancelled_charges_are_one_quiet_callout_and_late_ones_are_loud():
    from tools_for_ynab.reports.render import assemble

    old = monthly("Gym", "Gym", [30, 30, 30], start="2026-03-05") + monthly(
        "Pool", "Pool", [40, 40, 40], start="2026-03-06"
    )
    late = monthly("Loan", "Loan", [500, 500, 500], start="2026-05-01")  # last 2026-07-01
    cfg = ReportConfig()
    subs = find_subscriptions(old + late, date(2026, 8, 25))
    r = assemble(
        analyze_drift([], MONTHS, cfg),
        subs,
        MONTHS,
        date(2026, 8, 25),
        cfg,
        None,
        ("subscriptions",),
    )
    tones = {c.title.split(":")[0]: c.tone for c in r.callouts}
    assert tones["Loan"] == "warn" and tones["Looks cancelled"] == "info"
    cancelled = next(c for c in r.callouts if c.title == "Looks cancelled")
    assert "Gym" in cancelled.detail and "Pool" in cancelled.detail


def test_label_adds_the_merchant_only_when_it_adds_information():
    from tools_for_ynab.reports.render import label

    assert label("Flixly", "📺 Flixly") == "📺 Flixly"
    assert label("HomeLoan Co", "🏠 Mortgage") == "🏠 Mortgage (HomeLoan Co)"


def test_html_escapes_payees():
    _, _, html = render([sp("2026-09-05", 500, "<script>alert(1)</script>", "Books")])
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_money_formats_negatives():
    assert money(-155.4) == "-$155" and money(8.99, cents=True) == "$8.99" and money(-0.2) == "$0"


def test_config_validation(tmp_path):
    assert load_config(tmp_path / "missing.yaml") == ReportConfig()
    bad = tmp_path / "r.yaml"
    bad.write_text("exclude_group: [x]\n")
    with pytest.raises(ReportError, match="unknown"):
        load_config(bad)
    bad.write_text("window_months: 0\n")
    with pytest.raises(ReportError):
        load_config(bad)
