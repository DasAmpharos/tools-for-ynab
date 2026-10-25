import pytest

from tools4ynab.rules.rules import (
    Actions,
    Match,
    Rule,
    RuleError,
    RuleSet,
    Settings,
    TransferRule,
    disable,
    parse_disabled,
)


def ruleset():
    m = Match.parse({"payee_contains": "x"})
    return RuleSet(
        Settings(),
        [Rule("Groceries", m, Actions(category="a")), Rule("Coffee", m, Actions(category="b"))],
        [],
        [TransferRule("Acme Round Up", m, m)],
    )


def test_parse_disabled():
    assert parse_disabled(None) == [] and parse_disabled("") == []
    assert parse_disabled(" a , b\nc ,") == ["a", "b", "c"]


def test_disables_any_kind_case_insensitively():
    rs = disable(ruleset(), ["acme round up", "Coffee"])
    assert [r.name for r in rs.rules] == ["Groceries"]
    assert rs.transfers == []


def test_unknown_name_is_an_error():
    with pytest.raises(RuleError, match="unknown"):
        disable(ruleset(), ["Acme Rnoud Up"])
