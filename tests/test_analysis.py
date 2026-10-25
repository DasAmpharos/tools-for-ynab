from pathlib import Path

from tools_for_ynab.core.client import YnabClient
from tools_for_ynab.rules import analysis
from tools_for_ynab.rules.planner import load_context
from tools_for_ynab.rules.rules import load_rules


def ctx_and_txns(y, extra_categories=()):
    for name in extra_categories:
        y.groups[0]["categories"].append(y._cat(name))
    ctx = load_context(YnabClient("k", "budget-abc"), load_rules(Path("rules.yaml")))
    return ctx, YnabClient("k", "budget-abc").transactions(__import__("datetime").date(2000, 1, 1))


def history(y, payee, n, amount=-10, category="Groceries", **kw):
    for i in range(n):
        y.add(
            "chk",
            amount - (i if kw.pop("vary", False) else 0),
            payee,
            days_ago=5 + i,
            approved=True,
            category_id=y.cat_id(category),
            **kw,
        )


def names(sugs):
    return {s.payee for s in sugs}


def test_suggests_consistent_payees_only(ynab):
    history(ynab, "Steady Store", 4)  # always Groceries, same price -> fixed-amount rule
    history(ynab, "Rare Store", 2)  # too few
    for i in range(3):  # mixed categories
        ynab.add(
            "chk",
            -5,
            "Mixed",
            days_ago=5 + i,
            approved=True,
            category_id=ynab.cat_id(["Groceries", "Sketches", "Assistant"][i]),
        )
    ctx, txns = ctx_and_txns(ynab)
    sugs = analysis.suggest(ctx, txns)
    assert names(sugs) == {"Steady Store"}
    assert sugs[0].amount == 10.0 and sugs[0].direction == "outflow"


def test_varying_amounts_give_a_payee_only_rule(ynab):
    for i, amt in enumerate((-11, -23, -37)):
        ynab.add(
            "chk",
            amt,
            "Corner Deli",
            days_ago=3 + i,
            approved=True,
            category_id=ynab.cat_id("Restaurants"),
        )
    ctx, txns = ctx_and_txns(ynab)
    [s] = analysis.suggest(ctx, txns)
    assert s.amount is None and s.category == "Restaurants"


def test_payee_that_was_ever_split_gets_no_any_amount_rule(ynab):
    for i, amt in enumerate((-11, -23, -37)):
        ynab.add(
            "chk",
            amt,
            "Big Box",
            days_ago=3 + i,
            approved=True,
            category_id=ynab.cat_id("Groceries"),
        )
    ynab.add(
        "chk",
        -50,
        "Big Box",
        days_ago=9,
        approved=True,
        subtransactions=[{"id": "s1", "amount": -30000, "category_id": "x", "deleted": False}],
    )
    ctx, txns = ctx_and_txns(ynab)
    assert analysis.suggest(ctx, txns) == []


def test_event_category_spending_counts_against_a_payee(ynab):
    # Cafe: 3 normal + 3 on a (completed) trip. No "any amount" rule, but the exact
    # $4.00 charge was always Restaurants, so only that fixed-price rule is offered.
    ynab.groups.append({"name": "Completed", "deleted": False, "categories": [ynab._cat("Trip")]})
    for i in range(3):
        ynab.add(
            "chk", -4, "Cafe", days_ago=3 + i, approved=True, category_id=ynab.cat_id("Restaurants")
        )
        ynab.add(
            "chk", -6 - i, "Cafe", days_ago=20 + i, approved=True, category_id=ynab.cat_id("Trip")
        )
    ctx, txns = ctx_and_txns(ynab)
    [s] = analysis.suggest(ctx, txns)
    assert s.payee == "Cafe" and s.amount == 4.0


def test_stale_payees_and_covered_payees_are_skipped(ynab):
    for i in range(4):
        ynab.add(
            "chk",
            -9,
            "Old Gym",
            days_ago=200 + i,
            approved=True,
            category_id=ynab.cat_id("Sketches"),
        )
        ynab.add(
            "chk",
            -9,
            "Greenleaf",
            days_ago=5 + i,
            approved=True,
            category_id=ynab.cat_id("Groceries"),
        )
    ctx, txns = ctx_and_txns(ynab)
    assert analysis.suggest(ctx, txns) == []  # Gym idle >120d; Greenleaf already has a rule


def test_rendered_suggestions_parse_and_resolve(ynab):
    history(ynab, "Steady Store", 4)
    history(ynab, 'Café "Quote": Co', 3, amount=-8, category="Restaurants")
    ctx, txns = ctx_and_txns(ynab)
    text = analysis.render_suggestions(analysis.suggest(ctx, txns), 6)
    import yaml

    from tools_for_ynab.rules.engine import resolve_categories
    from tools_for_ynab.rules.rules import _parse_sections

    rules, _, _ = _parse_sections(yaml.safe_load(text))
    assert len(rules) == 2
    resolve_categories(rules, ctx.category_index)  # every category name resolves


def test_duplicates_ignore_legitimate_lookalikes(ynab):
    # manual+uncleared vs. an import with a different amount / account / far-off date
    ynab.add("chk", -5, "A", cleared="uncleared", import_id=None)
    ynab.add("chk", -6, "A")
    ynab.add("sav", -5, "A")
    ynab.add("chk", -5, "A", days_ago=30)
    _, txns = ctx_and_txns(ynab)
    assert analysis.find_duplicates(txns) == []


def test_suggest_ignore_setting_silences_a_payee(ynab):
    history(ynab, "Steady Store", 4)
    Path("rules.yaml").write_text(
        Path("rules.yaml")
        .read_text()
        .replace("scope: unapproved}", "scope: unapproved, suggest_ignore: [steady STORE]}")
    )
    ctx, txns = ctx_and_txns(ynab)
    assert analysis.suggest(ctx, txns) == []
