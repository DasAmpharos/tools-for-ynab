"""Build a run's plan from the rules and YNAB data, and apply it.

Order of precedence for a transaction: transfer pair, then split, then
categorization rule."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools_for_ynab.core.client import YnabClient

from .audit import AuditLog
from .engine import (
    Change,
    SplitChange,
    apply_split,
    build_category_index,
    plan_changes,
    plan_splits,
    resolve_categories,
)
from .rules import RuleSet, in_scope
from .transfers import TransferPair, apply_pair, find_pairs


@dataclass
class Context:
    client: YnabClient
    ruleset: RuleSet
    categories: list[dict[str, Any]]
    category_index: dict[str, str]  # normalized name -> id
    category_ids: dict[str, str]  # category names used by rules -> id
    category_names: dict[str, str]  # id -> display name


def load_context(client: YnabClient, ruleset: RuleSet) -> Context:
    """Fetch categories and resolve every category the rules name (typos fail here)."""
    categories = client.categories()
    index = build_category_index(categories)
    return Context(
        client,
        ruleset,
        categories,
        index,
        resolve_categories(ruleset.rules, index, ruleset.splits),
        {c["id"]: c["name"] for c in categories},
    )


def txn_line(txn: dict[str, Any], rule: str, summary: str) -> str:
    return (
        f"{txn['date']}  {(txn.get('payee_name') or '?')[:30]:<30} "
        f"{txn['amount'] / 1000:>10.2f}  [{rule}] {summary}"
    )


@dataclass
class Plan:
    scanned: int
    changes: list[Change]
    splits: list[SplitChange]
    pairs: list[TransferPair]

    @property
    def total(self) -> int:
        return len(self.changes) + len(self.splits) + len(self.pairs)

    def quiet_lines(self) -> list[str]:
        return _quiet_lines(self)

    def lines(self) -> list[str]:
        return (
            [txn_line(c.txn, c.rule.name, c.summary) for c in self.changes]
            + [txn_line(s.txn, s.rule.name, s.summary) for s in self.splits]
            + [
                f"{p.source['date']}  {p.source['account_name']} -> "
                f"{p.dest['account_name']:<12} {abs(p.source['amount']) / 1000:>10.2f}  "
                f"[{p.rule.name}] merge into one transfer"
                for p in self.pairs
            ]
        )


def _quiet_lines(plan: Plan) -> list[str]:
    """What a plan will do, by rule and field: no payees, amounts or categories."""
    fields: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for c in plan.changes:
        counts[c.rule.name] = counts.get(c.rule.name, 0) + 1
        fields.setdefault(c.rule.name, set()).update(k for k in c.patch if k != "id")
    for sp in plan.splits:
        counts[sp.rule.name] = counts.get(sp.rule.name, 0) + 1
        fields.setdefault(sp.rule.name, set()).add("split")
    for p in plan.pairs:
        counts[p.rule.name] = counts.get(p.rule.name, 0) + 1
        fields.setdefault(p.rule.name, set()).add("transfer pair")
    return [f"[{name}] x{n}: {', '.join(sorted(fields[name]))}" for name, n in counts.items()]


def build_plan(ctx: Context, all_txns: list[dict[str, Any]]) -> Plan:
    rs = ctx.ruleset
    pairs = find_pairs(all_txns, rs.transfers)
    paired = {t["id"] for p in pairs for t in (p.source, p.dest)}
    txns = [t for t in all_txns if t["id"] not in paired and in_scope(t, rs.settings.scope)]
    splits = plan_splits(txns, rs.splits, ctx.category_ids)
    split_ids = {s.txn["id"] for s in splits}
    changes = plan_changes(
        [t for t in txns if t["id"] not in split_ids],
        rs.rules,
        ctx.category_ids,
        ctx.category_names,
    )
    return Plan(len(txns), changes, splits, pairs)


def apply_plan(ctx: Context, plan: Plan, audit: AuditLog) -> list[str]:
    """Write the plan to YNAB, recording each applied change as it succeeds so a
    failure partway leaves an accurate audit trail. Returns outcome messages."""
    client, outcomes = ctx.client, []

    if plan.changes:
        client.update_transactions([c.patch for c in plan.changes])
        for c in plan.changes:
            fields = {k: v for k, v in c.patch.items() if k != "id"}
            audit.record(
                "update",
                c.txn,
                c.rule.name,
                before={k: c.txn.get(k) for k in fields},
                after=fields,
                summary=c.summary,
            )

    for s in plan.splits:
        outcomes.append(f"{s.txn['date']} {s.rule.name}: {apply_split(client, s)}")
        audit.record(
            "split",
            s.txn,
            s.rule.name,
            before={
                "category_id": s.txn.get("category_id"),
                "approved": s.txn.get("approved"),
            },
            after={"subtransactions": s.patch["subtransactions"]},
            summary=s.summary,
        )

    if plan.pairs:
        accounts = client.accounts()
        for p in plan.pairs:
            message, deleted = apply_pair(client, p, accounts)
            outcomes.append(f"{p.source['date']} {p.rule.name}: {message}")
            removed = (
                {
                    k: p.dest.get(k)
                    for k in (
                        "id",
                        "account_name",
                        "date",
                        "amount",
                        "payee_name",
                        "import_payee_name",
                        "cleared",
                    )
                }
                if deleted
                else None
            )
            audit.record(
                "transfer",
                p.source,
                p.rule.name,
                before={"payee_name": p.source.get("payee_name")},
                after={"transfer_to": p.dest["account_name"], "removed": removed},
                summary=f"merged into one transfer to {p.dest['account_name']}",
            )
    return outcomes
