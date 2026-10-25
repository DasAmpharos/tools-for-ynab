"""Read-only commands that compare the rules against your transaction history."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .engine import norm_name, plan_changes, plan_splits
from .planner import Context
from .rules import in_scope
from .transfers import find_pairs

NEVER_SUGGEST_PAYEES = ("transfer", "starting balance", "reconciliation", "manual balance")


def _plain(txn: dict[str, Any]) -> bool:
    return not txn.get("transfer_account_id") and not txn.get("subtransactions")


def _money(milli: int) -> str:
    return f"{milli / 1000:.2f}"


def _desc(t: dict[str, Any]) -> str:
    return f"{t['date']} {(t.get('payee_name') or '?')[:30]} {_money(t['amount'])}"


def _covered(ctx: Context, txn: dict[str, Any]) -> bool:
    rs = ctx.ruleset
    return any(r.match.matches(txn) for r in [*rs.rules, *rs.splits])


# --- backtest ---------------------------------------------------------------


@dataclass
class Report:
    lines: list[str]
    problems: int


def backtest(ctx: Context, txns: list[dict[str, Any]]) -> Report:
    """Replay the rules over history and flag any place a rule would have chosen
    differently from what you actually did."""
    rs = ctx.ruleset
    lines: list[str] = []
    problems = 0

    fired: Counter[str] = Counter()
    diffs = []
    for t in txns:
        if not _plain(t) or t.get("category_id") is None:
            continue
        rule = next((r for r in rs.rules if r.match.matches(t)), None)
        if rule is None or rule.actions.category is None:
            continue
        fired[rule.name] += 1
        if t["category_id"] != ctx.category_ids[rule.actions.category]:
            diffs.append((t, rule))
    total = sum(fired.values())
    lines.append(
        f"Rules: {total} historical transaction(s) matched, "
        f"{total - len(diffs)} agree with your categories, {len(diffs)} differ"
    )
    for t, rule in diffs:
        lines.append(
            f"  DIFF {_desc(t)}: [{rule.name}] wants {rule.actions.category}, "
            f"you chose {t['category_name']}"
        )
    problems += len(diffs)
    idle = [r.name for r in rs.rules if r.actions.category and r.name not in fired]
    if idle:
        lines.append(f"  No history to test: {', '.join(idle)}")

    for sp in rs.splits:
        agree = differ = unsplit = 0
        want = sorted((ctx.category_ids[p.category], round(p.amount * 1000)) for p in sp.parts)
        for t in txns:
            if t.get("transfer_account_id") or not sp.match.matches(t):
                continue
            if not t.get("subtransactions"):
                unsplit += 1
                continue
            sign = -1 if t["amount"] < 0 else 1
            got = sorted((s["category_id"], sign * s["amount"]) for s in t["subtransactions"])
            if got == want:
                agree += 1
            else:
                differ += 1
                lines.append(f"  DIFF {_desc(t)}: [{sp.name}] parts differ from your split")
        lines.append(
            f"Split [{sp.name}]: {agree} identical to your real splits, {differ} differ, "
            f"{unsplit} matching charge(s) were not split"
        )
        problems += differ

    for tr in rs.transfers:
        names = {a.lower() for a in tr.source.account + tr.dest.account}
        # Pretend nothing was merged yet, then see if we'd pair them as you did.
        relevant = [
            {**t, "transfer_account_id": None}
            for t in txns
            if (t.get("account_name") or "").lower() in names and not t.get("subtransactions")
        ]
        pairs = find_pairs(relevant, [tr])
        real = {t["id"]: t for t in txns}
        agree = sum(
            1 for p in pairs if real[p.source["id"]].get("transfer_transaction_id") == p.dest["id"]
        )
        lines.append(
            f"Transfer [{tr.name}]: would pair {len(pairs)}, {agree} exactly as you linked "
            f"them (the rest are same-amount pairs matched to a different neighbor)"
        )
    return Report(lines, problems)


# --- unmatched --------------------------------------------------------------


def unmatched(ctx: Context, txns: list[dict[str, Any]], limit: int = 25) -> list[str]:
    """Payees no rule covers, busiest first, so new rules go where they pay off."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in txns:
        if _plain(t) and not _covered(ctx, t):
            groups[t.get("payee_name") or "?"].append(t)
    rows = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    covered = sum(1 for t in txns if _plain(t) and _covered(ctx, t))
    lines = [
        f"{covered} of {len([t for t in txns if _plain(t)])} transactions are covered by a "
        f"rule; {len(rows)} payee(s) are not. Busiest:"
    ]
    for payee, L in rows[:limit]:
        cats = Counter(t["category_name"] or "(none)" for t in L)
        top, n = cats.most_common(1)[0]
        verdict = f"always {top}" if len(cats) == 1 else f"mixed, mostly {top} ({n}/{len(L)})"
        total = sum(-t["amount"] for t in L) / 1000
        lines.append(
            f"  {len(L):>3}x  {payee[:34]:<34} ${total:>9.2f}  {verdict}  "
            f"(last {max(t['date'] for t in L)})"
        )
    return lines


# --- suggest ----------------------------------------------------------------


@dataclass
class Suggestion:
    name: str
    payee: str
    category: str
    amount: float | None  # dollars; None = any amount
    direction: str | None
    evidence: str


def category_label(ctx: Context, cat_id: str) -> str:
    """Plain category name when unambiguous, else "Group: Category"."""
    cat = next(c for c in ctx.categories if c["id"] == cat_id)
    if ctx.category_index.get(norm_name(cat["name"])) == cat_id:
        return cat["name"]
    return f"{cat['group_name']}: {cat['name']}"


def _direction(group: list[dict[str, Any]]) -> str | None:
    if all(t["amount"] < 0 for t in group):
        return "outflow"
    if all(t["amount"] > 0 for t in group):
        return "inflow"
    return None


def suggest(
    ctx: Context,
    txns: list[dict[str, Any]],
    min_count: int = 3,
    max_age_days: int = 120,
) -> list[Suggestion]:
    """Rules that would have been right every time in your history, for payees that
    nothing covers yet and that are still active. Deliberately conservative."""
    skip_cats = {
        c["id"]
        for c in ctx.categories
        if c.get("hidden") or c["name"] == "Uncategorized" or "completed" in c["group_name"].lower()
    }
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    # Payees you've ever split get no "any amount" rule: auto-approving them would
    # lock out the split you sometimes need.
    split_payees = {t.get("payee_name") for t in txns if t.get("subtransactions")}
    ignored = {p.lower() for p in ctx.ruleset.settings.suggest_ignore}
    by_payee: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in txns:
        payee = t.get("payee_name") or ""
        if (
            _plain(t)
            and t.get("category_id")
            and payee
            and not payee.lower().startswith(NEVER_SUGGEST_PAYEES)
            and payee.lower() not in ignored
            and not _covered(ctx, t)
        ):
            by_payee[payee].append(t)

    taken = {r.name.lower() for r in [*ctx.ruleset.rules, *ctx.ruleset.splits]}
    out: list[Suggestion] = []

    def add(payee: str, group: list[dict[str, Any]], amount: float | None) -> None:
        cat_id = group[0]["category_id"]
        label = category_label(ctx, cat_id)
        base = f"{payee} ${amount:.2f}" if amount is not None else payee
        name, i = base, 2
        while name.lower() in taken:
            name, i = f"{base} ({i})", i + 1
        taken.add(name.lower())
        last = max(t["date"] for t in group)
        out.append(
            Suggestion(
                name,
                payee,
                label,
                amount,
                _direction(group),
                f"{len(group)} transactions, always {ctx.category_names[cat_id]}, last {last}",
            )
        )

    for payee, L in sorted(by_payee.items()):
        if max(t["date"] for t in L) < cutoff:
            continue
        # Judge consistency on everything, then drop event categories (a closed trip's
        # spending is evidence the payee varies, not a rule worth making).
        cats = {t["category_id"] for t in L}
        if len(cats) == 1 and len(L) >= min_count:
            if cats & skip_cats:
                continue
            amounts = {abs(t["amount"]) for t in L}
            if len(amounts) == 1:
                add(payee, L, amounts.pop() / 1000)  # a fixed price is the precise rule
            elif payee not in split_payees:
                add(payee, L, None)
            continue
        by_amount: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for t in L:
            by_amount[t["amount"]].append(t)
        for amount, G in sorted(by_amount.items()):
            if (
                len(G) >= min_count
                and len({t["category_id"] for t in G}) == 1
                and max(t["date"] for t in G) >= cutoff
                and G[0]["category_id"] not in skip_cats
            ):
                add(payee, G, abs(amount) / 1000)
    return out


def render_suggestions(sugs: list[Suggestion], months: float) -> str:
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731 - YAML-safe quoting
    out = [
        f"# Suggested by `tools4ynab rules suggest` on {date.today()} from the last "
        f"{months:g} months of history.",
        "# Every rule below was right every time. Delete any you don't want, then merge.",
        "rules:",
    ]
    for s in sugs:
        out += [
            f"  # {s.evidence}",
            f"  - name: {q(s.name)}",
            "    match:",
            f"      payee_equals: {q(s.payee)}",
        ]
        if s.amount is not None:
            out.append(f"      amount: {{equals: {s.amount:.2f}}}")
        if s.direction:
            out.append(f"      direction: {s.direction}")
        out += ["    set:", f"      category: {q(s.category)}", ""]
    return "\n".join(out)


# --- explain ----------------------------------------------------------------


def explain(ctx: Context, all_txns: list[dict[str, Any]], query: str) -> list[str]:
    """For each transaction matching `query` (payee text or id prefix), say what the
    rules would do and why."""
    rs, q = ctx.ruleset, query.lower()
    hits = [
        t
        for t in all_txns
        if q in (t.get("payee_name") or "").lower()
        or q in (t.get("import_payee_name") or "").lower()
        or t["id"].startswith(query)
    ]
    pairs = find_pairs(all_txns, rs.transfers)
    paired = {t["id"]: p for p in pairs for t in (p.source, p.dest)}
    lines = [f"{len(hits)} transaction(s) match {query!r}"]
    for t in sorted(hits, key=lambda t: t["date"]):
        state = "approved" if t.get("approved") else "unapproved"
        lines.append(f"- {_desc(t)} | {t.get('account_name')} | {t.get('category_name')} | {state}")
        if t.get("transfer_account_id"):
            lines.append("    skipped: already a transfer")
        elif t.get("subtransactions"):
            lines.append("    skipped: already a split")
        elif t["id"] in paired:
            lines.append(f"    transfer rule [{paired[t['id']].rule.name}] would merge this pair")
        elif not in_scope(t, rs.settings.scope):
            lines.append(
                f"    out of scope (settings.scope={rs.settings.scope}): approved and categorized, "
                "so it is final"
            )
        else:
            _explain_rules(ctx, t, lines)
    return lines


def _explain_rules(ctx: Context, t: dict[str, Any], lines: list[str]) -> None:
    rs = ctx.ruleset
    if splits := plan_splits([t], rs.splits, ctx.category_ids):
        lines.append(f"    split rule [{splits[0].rule.name}]: {splits[0].summary}")
        return
    matching = [r for r in rs.rules if r.match.matches(t)]
    if not matching:
        lines.append("    no rule matches")
        return
    lines.append(f"    first match: [{matching[0].name}]")
    for r in matching[1:]:
        lines.append(f"    also matches (ignored, first match wins): [{r.name}]")
    if changes := plan_changes([t], rs.rules, ctx.category_ids, ctx.category_names):
        lines.append(f"    would change: {changes[0].summary}")
    elif t.get("approved") and t.get("category_id"):
        lines.append("    no change: the category is final because it is approved")
    else:
        lines.append("    no change: already up to date")


# --- duplicates -------------------------------------------------------------


def find_duplicates(txns: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Manually entered, still-uncleared transactions that have a cleared bank
    import with the same account, amount and nearby date: usually a manual entry
    YNAB failed to match, which double-counts the money."""
    imported = [t for t in txns if t.get("import_id") and t["cleared"] != "uncleared"]
    found = []
    for a in txns:
        if a.get("import_id") or a["cleared"] != "uncleared" or a["amount"] == 0:
            continue
        for b in imported:
            gap = abs((date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days)
            if (
                b["id"] != a["id"]
                and b["account_id"] == a["account_id"]
                and b["amount"] == a["amount"]
                and gap <= 3
            ):
                found.append((a, b))
                break
    return found
