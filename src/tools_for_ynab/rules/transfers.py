"""Pair the two bank-imported halves of a transfer and merge them into one
YNAB transfer (e.g. Acme Round Up: checking outflow + savings inflow)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from tools_for_ynab.core.client import YnabClient, YnabError

from .rules import TransferRule


@dataclass
class TransferPair:
    rule: TransferRule
    source: dict[str, Any]  # outflow; this one becomes the transfer
    dest: dict[str, Any]  # inflow; replaced by the transfer's counterpart


def _days_apart(a: dict[str, Any], b: dict[str, Any]) -> int:
    return abs((date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days)


def _free(txn: dict[str, Any]) -> bool:
    return not txn.get("transfer_account_id") and not txn.get("subtransactions")


def find_pairs(txns: list[dict[str, Any]], rules: list[TransferRule]) -> list[TransferPair]:
    """Match each outflow to the inflow of the same amount with the closest date
    (within the rule's window). Each transaction is used at most once."""
    used: set[str] = set()
    pairs = []
    for rule in rules:
        sources = sorted(
            (t for t in txns if _free(t) and t["amount"] < 0 and rule.source.matches(t)),
            key=lambda t: t["date"],
        )
        dests = [t for t in txns if _free(t) and t["amount"] > 0 and rule.dest.matches(t)]
        for src in sources:
            candidates = [
                d
                for d in dests
                if d["id"] not in used
                and d["account_id"] != src["account_id"]
                and d["amount"] == -src["amount"]
                and _days_apart(src, d) <= rule.max_days_apart
            ]
            if src["id"] in used or not candidates:
                continue
            dest = min(candidates, key=lambda d: (_days_apart(src, d), d["date"]))
            used.update((src["id"], dest["id"]))
            pairs.append(TransferPair(rule, src, dest))
    return pairs


def apply_pair(
    client: YnabClient, pair: TransferPair, accounts: dict[str, dict[str, Any]]
) -> tuple[str, bool]:
    """Turn the outflow into a transfer, then drop the now-redundant inflow.
    Returns (outcome message, whether the imported inflow was deleted).

    The inflow is deleted only if YNAB created a new counterpart; if YNAB linked
    the two existing transactions, nothing is deleted. Anything unexpected raises
    before the delete, so a failure leaves at worst a duplicate, never a gap."""
    src, dest = pair.source, pair.dest
    transfer_payee = accounts[dest["account_id"]]["transfer_payee_id"]

    updated = client.update_transaction(src, {"payee_id": transfer_payee, "approved": True})
    link_id = updated.get("transfer_transaction_id")
    if (
        not link_id
        or updated.get("transfer_account_id") != dest["account_id"]
        or updated["amount"] != src["amount"]
    ):
        raise YnabError(
            f"{pair.rule.name}: update did not produce the expected transfer; "
            "left the imported inflow in place"
        )
    if link_id == dest["id"]:
        return "linked the two existing transactions", False

    # A new counterpart was created: make it mirror the bank-imported inflow.
    counterpart = client.get_transaction(link_id)
    patch: dict[str, Any] = {"approved": True}
    if counterpart["cleared"] != dest["cleared"] and dest["cleared"] != "reconciled":
        patch["cleared"] = dest["cleared"]
    if counterpart["date"] != dest["date"]:
        patch["date"] = dest["date"]
    client.update_transaction(counterpart, patch)

    client.delete_transaction(dest["id"])
    return "converted to a transfer and removed the duplicate imported inflow", True
