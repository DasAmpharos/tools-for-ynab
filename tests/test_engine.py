import pytest

from tools_for_ynab.rules.engine import build_category_index, plan_changes, resolve_categories
from tools_for_ynab.rules.rules import Actions, Match, Rule, RuleError

CATEGORIES = [
    {"id": "c-groc", "name": "Groceries", "group_name": "Food"},
    {"id": "c-dine", "name": "Dining Out", "group_name": "Food"},
    {"id": "c-misc-a", "name": "Misc", "group_name": "Home"},
    {"id": "c-misc-b", "name": "Misc", "group_name": "Fun"},
    {"id": "c-emoji", "name": "🍽️ Restaurants", "group_name": "🏠 Core"},
]
NAMES = {c["id"]: c["name"] for c in CATEGORIES}


def txn(**kw):
    base = {
        "id": "t1",
        "date": "2026-10-01",
        "amount": -42_000,
        "payee_name": "GREENLEAF #123",
        "memo": None,
        "account_name": "Checking",
        "category_id": None,
        "approved": False,
        "flag_color": None,
        "transfer_account_id": None,
        "subtransactions": [],
    }
    return {**base, **kw}


def rule(match, **actions):
    return Rule("r", Match.parse(match), Actions.parse(actions))


def plan(txns, rules):
    ids = resolve_categories(rules, build_category_index(CATEGORIES))
    return plan_changes(txns, rules, ids, NAMES)


def test_payee_contains_is_case_insensitive_and_sets_category():
    [c] = plan([txn()], [rule({"payee_contains": "greenleaf"}, category="Groceries")])
    assert c.patch == {"id": "t1", "category_id": "c-groc", "approved": True}


def test_all_criteria_must_match():
    r = rule({"payee_contains": "greenleaf", "amount": {"max": 10}}, category="Groceries")
    assert plan([txn()], [r]) == []  # $42 > $10


def test_first_matching_rule_wins():
    rules = [
        rule({"payee_contains": "greenleaf"}, category="Groceries"),
        rule({"payee_contains": "greenleaf"}, category="Dining Out"),
    ]
    [c] = plan([txn()], rules)
    assert c.patch["category_id"] == "c-groc"


def test_direction_and_amount_use_absolute_dollars():
    r = rule({"direction": "inflow", "amount": {"equals": 42}}, category="Groceries")
    assert plan([txn()], [r]) == []
    assert len(plan([txn(amount=42_000)], [r])) == 1


def test_no_change_when_already_set_and_approved():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries")
    assert plan([txn(category_id="c-groc", approved=True)], [r]) == []


def test_matching_category_already_correct_still_gets_approved():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries")
    [c] = plan([txn(category_id="c-groc")], [r])
    assert c.patch == {"id": "t1", "approved": True}


def test_approve_false_opts_out():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries", approve=False)
    [c] = plan([txn()], [r])
    assert c.patch == {"id": "t1", "category_id": "c-groc"}


def test_skips_splits_and_transfers():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries")
    assert plan([txn(subtransactions=[{"id": "s"}])], [r]) == []
    assert plan([txn(transfer_account_id="acct")], [r]) == []


def test_other_actions():
    r = rule(
        {"payee_regex": r"^greenleaf"},
        payee="Greenleaf",
        memo="auto",
        flag_color="blue",
        approve=True,
    )
    [c] = plan([txn(flag_color="red")], [r])
    assert c.patch == {
        "id": "t1",
        "payee_name": "Greenleaf",
        "memo": "auto",
        "flag_color": "blue",
        "approved": True,
    }


def test_flag_none_clears():
    [c] = plan([txn(flag_color="red")], [rule({"payee_contains": "g"}, flag_color="none")])
    assert c.patch["flag_color"] is None


def test_ambiguous_category_needs_group_qualifier():
    with pytest.raises(RuleError):
        plan([], [rule({"payee_contains": "x"}, category="Misc")])
    [c] = plan([txn()], [rule({"payee_contains": "greenleaf"}, category="Fun: Misc")])
    assert c.patch["category_id"] == "c-misc-b"


def test_unknown_category_fails_fast():
    with pytest.raises(RuleError):
        plan([], [rule({"payee_contains": "x"}, category="Nope")])


def test_invalid_rules_rejected():
    with pytest.raises(RuleError):
        Match.parse({"payee_contians": "x"})
    with pytest.raises(RuleError):
        Actions.parse({})
    with pytest.raises(RuleError):
        Match.parse({"payee_regex": "("})


def test_category_match_ignores_emoji_and_case():
    [c] = plan([txn()], [rule({"payee_contains": "greenleaf"}, category="restaurants")])
    assert c.patch["category_id"] == "c-emoji"
    [c] = plan([txn()], [rule({"payee_contains": "greenleaf"}, category="Core: Restaurants")])
    assert c.patch["category_id"] == "c-emoji"


def test_unapproved_category_is_treated_as_a_ynab_guess_and_replaced():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries")
    [c] = plan([txn(category_id="c-dine", approved=False)], [r])
    assert c.patch == {"id": "t1", "category_id": "c-groc", "approved": True}
    assert "was Dining Out" in c.summary


def test_approved_category_is_never_changed():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries")
    assert plan([txn(category_id="c-dine", approved=True)], [r]) == []


def test_approved_but_uncategorized_still_gets_a_category():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries")
    [c] = plan([txn(approved=True)], [r])
    assert c.patch == {"id": "t1", "category_id": "c-groc"}


def test_other_actions_still_apply_to_approved_txn_but_category_is_kept():
    r = rule({"payee_contains": "greenleaf"}, category="Groceries", memo="x")
    [c] = plan([txn(category_id="c-dine", approved=True)], [r])
    assert c.patch == {"id": "t1", "memo": "x"}
