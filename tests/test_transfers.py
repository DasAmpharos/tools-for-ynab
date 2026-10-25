import pytest

from tools4ynab.core.client import YnabError
from tools4ynab.rules.rules import Match, TransferRule
from tools4ynab.rules.transfers import apply_pair, find_pairs

RULE = TransferRule(
    "ktc",
    Match.parse({"account": "Acme Checking", "payee_contains": "round up"}),
    Match.parse({"account": "Acme Savings", "payee_contains": "roundup"}),
    max_days_apart=3,
)
ACCOUNTS = {"sav": {"transfer_payee_id": "xfer-sav"}, "chk": {"transfer_payee_id": "x"}}


def txn(id, acct, amount, date, payee, **kw):
    return {
        "id": id,
        "account_id": "chk" if acct == "Acme Checking" else "sav",
        "account_name": acct,
        "amount": amount,
        "date": date,
        "payee_name": payee,
        "import_payee_name": payee,
        "cleared": "cleared",
        "transfer_account_id": None,
        "subtransactions": [],
        **kw,
    }


def out(id, amount, date="2026-10-14"):
    return txn(id, "Acme Checking", -amount, date, "ROUND UP TRANSFER TO ACCT 1")


def inn(id, amount, date="2026-10-14"):
    return txn(id, "Acme Savings", amount, date, "ROUNDUP CREDIT FROM ACCT 2")


def test_pairs_matching_amount_and_closest_date():
    txns = [
        out("o1", 640, "2026-10-15"),
        inn("i-far", 640, "2026-10-12"),
        inn("i-near", 640, "2026-10-14"),
    ]
    [p] = find_pairs(txns, [RULE])
    assert (p.source["id"], p.dest["id"]) == ("o1", "i-near")


def test_each_transaction_used_once():
    txns = [out("o1", 10, "2026-10-01"), out("o2", 10, "2026-10-02"), inn("i1", 10, "2026-10-02")]
    pairs = find_pairs(txns, [RULE])
    assert len(pairs) == 1 and pairs[0].source["id"] == "o1"


@pytest.mark.parametrize(
    "dest",
    [
        inn("i", 999),  # amount differs
        inn("i", 10, "2026-10-30"),  # outside the window
        txn("i", "Acme Savings", 10, "2026-10-14", "Interest Earned"),  # wrong payee
    ],
)
def test_no_pair(dest):
    assert find_pairs([out("o", 10), dest], [RULE]) == []


def test_skips_existing_transfers():
    txns = [{**out("o", 10), "transfer_account_id": "sav"}, inn("i", 10)]
    assert find_pairs(txns, [RULE]) == []


class FakeClient:
    def __init__(self, result):
        self.result, self.calls = result, []

    def update_transaction(self, t, patch):
        self.calls.append(("update", t["id"], patch))
        return self.result if t["id"] == "o" else {**t, **patch}

    def get_transaction(self, id):
        self.calls.append(("get", id))
        return {**inn(id, 10), "cleared": "uncleared", "date": "2026-10-15"}

    def delete_transaction(self, id):
        self.calls.append(("delete", id))


def pair():
    [p] = find_pairs([out("o", 10), inn("i", 10)], [RULE])
    return p


def test_apply_creates_counterpart_then_deletes_import():
    c = FakeClient({**out("o", 10), "transfer_account_id": "sav", "transfer_transaction_id": "new"})
    apply_pair(c, pair(), ACCOUNTS)
    assert c.calls[0] == ("update", "o", {"payee_id": "xfer-sav", "approved": True})
    assert c.calls[1] == ("get", "new")
    assert c.calls[2][2] == {"approved": True, "cleared": "cleared", "date": "2026-10-14"}
    assert c.calls[-1] == ("delete", "i")


def test_apply_does_not_delete_when_ynab_linked_existing():
    c = FakeClient({**out("o", 10), "transfer_account_id": "sav", "transfer_transaction_id": "i"})
    apply_pair(c, pair(), ACCOUNTS)
    assert not any(call[0] == "delete" for call in c.calls)


def test_apply_never_deletes_if_no_transfer_resulted():
    c = FakeClient(out("o", 10))  # not a transfer
    with pytest.raises(YnabError):
        apply_pair(c, pair(), ACCOUNTS)
    assert not any(call[0] == "delete" for call in c.calls)
