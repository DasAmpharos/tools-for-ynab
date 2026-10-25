import pytest

from tools4ynab.core.client import YnabError
from tools4ynab.rules.engine import apply_split, plan_splits
from tools4ynab.rules.rules import RuleError, _parse_split

IDS = {"Comedy": "c-d", "Assistant": "c-c", "Sketches": "c-s"}
BUNDLE = {
    "name": "bundle",
    "match": {"payee_equals": "AppHub", "amount": {"equals": 27.98}, "direction": "outflow"},
    "parts": [
        {"category": "Comedy", "amount": 4.99},
        {"category": "Assistant", "amount": 15.00},
        {"category": "Sketches", "amount": 7.99},
    ],
}


def txn(**kw):
    base = {
        "id": "t1",
        "date": "2026-10-16",
        "amount": -27_980,
        "payee_name": "AppHub",
        "category_id": None,
        "approved": False,
        "transfer_account_id": None,
        "subtransactions": [],
        "account_id": "a",
        "memo": None,
    }
    return {**base, **kw}


def rule(raw=BUNDLE):
    return _parse_split(raw, 1)


def test_builds_negative_subtransactions_that_sum_to_the_total():
    [c] = plan_splits([txn()], [rule()], IDS)
    subs = c.patch["subtransactions"]
    assert [s["amount"] for s in subs] == [-4990, -15000, -7990]
    assert sum(s["amount"] for s in subs) == -27_980
    assert c.patch["category_id"] is None and c.patch["approved"] is True
    assert "Assistant 15.00" in c.summary


def test_inflow_split_has_positive_parts():
    raw = {**BUNDLE, "match": {**BUNDLE["match"], "direction": "inflow"}}
    [c] = plan_splits([txn(amount=27_980)], [rule(raw)], IDS)
    assert all(s["amount"] > 0 for s in c.patch["subtransactions"])


@pytest.mark.parametrize(
    "t",
    [
        txn(amount=-28_980),  # different total
        txn(subtransactions=[{"id": "s"}]),  # already split
        txn(transfer_account_id="x"),
        txn(approved=True, category_id="c-d"),  # approved + categorized is final
    ],
)
def test_not_split(t):
    assert plan_splits([t], [rule()], IDS) == []


def test_unapproved_with_ynab_guess_category_is_split():
    assert len(plan_splits([txn(category_id="c-d")], [rule()], IDS)) == 1


def test_parts_must_add_up():
    bad = {**BUNDLE, "parts": BUNDLE["parts"][:2]}
    with pytest.raises(RuleError, match="add up"):
        rule(bad)


@pytest.mark.parametrize(
    "raw",
    [
        {**BUNDLE, "match": {"payee_equals": "AppHub"}},  # no exact total
        {**BUNDLE, "parts": BUNDLE["parts"][:1]},
        {**BUNDLE, "parts": [{"category": "x"}, {"category": "y"}]},
        {**BUNDLE, "bogus": 1},
    ],
)
def test_invalid_split_rules(raw):
    with pytest.raises(RuleError):
        rule(raw)


class FakeClient:
    def __init__(self, returned):
        self.returned, self.sent = returned, None

    def update_transaction(self, t, patch):
        self.sent = patch
        return {**t, "subtransactions": self.returned}


def test_apply_verifies_what_ynab_created():
    [c] = plan_splits([txn()], [rule()], IDS)
    ok = FakeClient(c.patch["subtransactions"])
    assert apply_split(ok, c)
    with pytest.raises(YnabError):
        apply_split(FakeClient([]), c)  # YNAB ignored the subtransactions
