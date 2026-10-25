"""Rule definitions, YAML loading, and transaction matching."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCOPES = {"uncategorized", "unapproved", "all"}
FLAG_COLORS = {"red", "orange", "yellow", "green", "blue", "purple", "none"}
MATCH_KEYS = {
    "payee_contains",
    "payee_equals",
    "payee_regex",
    "memo_contains",
    "memo_regex",
    "account",
    "amount",
    "direction",
}
ACTION_KEYS = {"category", "payee", "memo", "flag_color", "approve"}


class RuleError(ValueError):
    pass


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise RuleError(f"expected a string or list of strings, got {value!r}")


@dataclass
class Match:
    payee_contains: list[str] = field(default_factory=list)
    payee_equals: list[str] = field(default_factory=list)
    payee_regex: list[re.Pattern[str]] = field(default_factory=list)
    memo_contains: list[str] = field(default_factory=list)
    memo_regex: list[re.Pattern[str]] = field(default_factory=list)
    account: list[str] = field(default_factory=list)
    # Absolute dollar bounds; sign is handled by `direction`.
    amount_min: float | None = None
    amount_max: float | None = None
    amount_equals: float | None = None
    direction: str | None = None  # "outflow" | "inflow"

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Match:
        unknown = set(raw) - MATCH_KEYS
        if unknown:
            raise RuleError(f"unknown match keys: {sorted(unknown)}")
        m = cls()
        try:
            for key in ("payee_contains", "memo_contains", "account", "payee_equals"):
                if key in raw:
                    setattr(m, key, [s.lower() for s in _as_list(raw[key])])
            for key in ("payee_regex", "memo_regex"):
                if key in raw:
                    setattr(m, key, [re.compile(p, re.I) for p in _as_list(raw[key])])
        except re.error as e:
            raise RuleError(f"invalid regex: {e}") from e
        if "amount" in raw:
            amt = raw["amount"]
            if not isinstance(amt, dict) or set(amt) - {"min", "max", "equals"}:
                raise RuleError("amount must be a mapping of min/max/equals")
            m.amount_min = amt.get("min")
            m.amount_max = amt.get("max")
            m.amount_equals = amt.get("equals")
        if "direction" in raw:
            if raw["direction"] not in ("outflow", "inflow"):
                raise RuleError("direction must be 'outflow' or 'inflow'")
            m.direction = raw["direction"]
        return m

    def is_empty(self) -> bool:
        return not any(
            (
                self.payee_contains,
                self.payee_equals,
                self.payee_regex,
                self.memo_contains,
                self.memo_regex,
                self.account,
                self.amount_min is not None,
                self.amount_max is not None,
                self.amount_equals is not None,
                self.direction,
            )
        )

    def matches(self, txn: dict[str, Any]) -> bool:
        """All specified criteria must hold; values within one criterion are OR'd."""
        # Payee criteria match the current payee or the name the bank originally
        # sent, so a payee renamed in YNAB still matches on its bank text.
        payees = [n for n in (txn.get("payee_name"), txn.get("import_payee_name")) if n]
        memo = (txn.get("memo") or "").lower()
        if self.payee_contains and not any(
            s in n.lower() for n in payees for s in self.payee_contains
        ):
            return False
        if self.payee_equals and not any(n.lower() in self.payee_equals for n in payees):
            return False
        if self.payee_regex and not any(p.search(n) for n in payees for p in self.payee_regex):
            return False
        if self.memo_contains and not any(s in memo for s in self.memo_contains):
            return False
        if self.memo_regex and not any(p.search(txn.get("memo") or "") for p in self.memo_regex):
            return False
        if self.account and (txn.get("account_name") or "").lower() not in self.account:
            return False

        milli = txn["amount"]  # negative = outflow
        if self.direction == "outflow" and milli >= 0:
            return False
        if self.direction == "inflow" and milli <= 0:
            return False
        dollars = abs(milli) / 1000
        if self.amount_min is not None and dollars < self.amount_min:
            return False
        if self.amount_max is not None and dollars > self.amount_max:
            return False
        if self.amount_equals is not None and round(dollars, 2) != round(self.amount_equals, 2):
            return False
        return True


@dataclass
class Actions:
    category: str | None = None
    payee: str | None = None
    memo: str | None = None
    flag_color: str | None = None
    approve: bool | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Actions:
        unknown = set(raw) - ACTION_KEYS
        if unknown:
            raise RuleError(f"unknown set keys: {sorted(unknown)}")
        a = cls(**raw)
        if a.flag_color is not None and a.flag_color not in FLAG_COLORS:
            raise RuleError(f"flag_color must be one of {sorted(FLAG_COLORS)}")
        if a.approve is not None and not isinstance(a.approve, bool):
            raise RuleError("approve must be true or false")
        if all(getattr(a, k) is None for k in ACTION_KEYS):
            raise RuleError("rule has no actions")
        return a


@dataclass
class Rule:
    name: str
    match: Match
    actions: Actions


@dataclass
class TransferRule:
    """Pairs an outflow in one account with the matching inflow in another and
    turns them into a single transfer."""

    name: str
    source: Match  # the outflow side (e.g. checking)
    dest: Match  # the inflow side (e.g. savings)
    max_days_apart: int = 4


@dataclass
class SplitPart:
    category: str
    amount: float  # dollars, positive; sign comes from the transaction


@dataclass
class SplitRule:
    """Splits one bundled charge (e.g. an app store billing several subscriptions at once)
    into fixed parts. Matches on an exact total, so a changed price stops matching."""

    name: str
    match: Match
    parts: list[SplitPart]


@dataclass
class Settings:
    since_days: int = 30
    scope: str = "unapproved"
    suggest_ignore: list[str] = field(default_factory=list)  # payees `suggest` skips


def in_scope(txn: dict[str, Any], scope: str) -> bool:
    if scope == "uncategorized":
        return txn.get("category_id") is None
    if scope == "unapproved":
        # Also includes approved-but-uncategorized, which still needs a category.
        return not txn.get("approved") or txn.get("category_id") is None
    return True


def _parse_transfer(raw: dict[str, Any], index: int) -> TransferRule:
    name = raw.get("name", f"transfer #{index}")
    try:
        unknown = set(raw) - {"name", "from", "to", "max_days_apart"}
        if unknown:
            raise RuleError(f"unknown keys: {sorted(unknown)}")
        sides = []
        for key in ("from", "to"):
            m = Match.parse(raw.get(key) or {})
            if not m.account or not (m.payee_contains or m.payee_regex or m.payee_equals):
                raise RuleError(f"'{key}' needs an account and a payee criterion")
            sides.append(m)
        days = raw.get("max_days_apart", 4)
        if not isinstance(days, int) or days < 0:
            raise RuleError("max_days_apart must be a non-negative integer")
        return TransferRule(name, sides[0], sides[1], days)
    except RuleError as e:
        raise RuleError(f"{name}: {e}") from e


def _parse_split(raw: dict[str, Any], index: int) -> SplitRule:
    name = raw.get("name", f"split #{index}")
    try:
        unknown = set(raw) - {"name", "match", "parts"}
        if unknown:
            raise RuleError(f"unknown keys: {sorted(unknown)}")
        match = Match.parse(raw.get("match") or {})
        if match.amount_equals is None or match.direction is None:
            raise RuleError("match needs amount.equals and direction (the exact total)")
        parts = []
        for p in raw.get("parts") or []:
            if not isinstance(p, dict) or set(p) != {"category", "amount"}:
                raise RuleError("each part needs exactly 'category' and 'amount'")
            if not isinstance(p["amount"], (int, float)) or p["amount"] <= 0:
                raise RuleError("part amounts must be positive numbers")
            parts.append(SplitPart(p["category"], p["amount"]))
        if len(parts) < 2:
            raise RuleError("a split needs at least 2 parts")
        if sum(round(p.amount * 1000) for p in parts) != round(match.amount_equals * 1000):
            raise RuleError("parts do not add up to the matched amount")
        return SplitRule(name, match, parts)
    except RuleError as e:
        raise RuleError(f"{name}: {e}") from e


@dataclass
class RuleSet:
    settings: Settings
    rules: list[Rule]
    splits: list[SplitRule]
    transfers: list[TransferRule]


def parse_disabled(value: str | None) -> list[str]:
    """Split a comma/newline separated list of rule names."""
    return [n.strip() for n in (value or "").replace("\n", ",").split(",") if n.strip()]


def disable(ruleset: RuleSet, names: list[str]) -> RuleSet:
    """Drop the named rules/splits/transfers. An unknown name is an error, so a
    typo can never leave a rule you meant to disable running."""
    wanted = {n.lower() for n in names}
    everything = [*ruleset.rules, *ruleset.splits, *ruleset.transfers]
    unknown = wanted - {r.name.lower() for r in everything}
    if unknown:
        raise RuleError(f"cannot disable unknown rule(s): {sorted(unknown)}")

    def keep(items: list[Any]) -> list[Any]:
        return [i for i in items if i.name.lower() not in wanted]

    return RuleSet(
        ruleset.settings,
        keep(ruleset.rules),
        keep(ruleset.splits),
        keep(ruleset.transfers),
    )


INCLUDE_DIR = "rules.d"


def _parse_sections(
    raw: dict[str, Any],
) -> tuple[list[Rule], list[SplitRule], list[TransferRule]]:
    rules = []
    for i, r in enumerate(raw.get("rules") or [], 1):
        name = r.get("name", f"rule #{i}")
        try:
            match = Match.parse(r.get("match") or {})
            if match.is_empty():
                raise RuleError("rule has no match criteria (would match everything)")
            rules.append(Rule(name, match, Actions.parse(r.get("set") or {})))
        except (RuleError, TypeError) as e:
            raise RuleError(f"{name}: {e}") from e
    splits = [_parse_split(sp, i) for i, sp in enumerate(raw.get("splits") or [], 1)]
    transfers = [_parse_transfer(t, i) for i, t in enumerate(raw.get("transfers") or [], 1)]
    return rules, splits, transfers


def _read_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise RuleError(f"{path.name}: expected a mapping at the top level")
    return raw


def load_rules(path: Path) -> RuleSet:
    """Load `path` plus any `rules.d/*.yaml` next to it. Included files hold only
    rules/splits/transfers and are consulted after the main file (first match
    wins), so hand-written rules always take priority over generated ones."""
    raw = _read_yaml(path)
    unknown = set(raw) - {"settings", "rules", "splits", "transfers"}
    if unknown:
        raise RuleError(f"unknown top-level keys: {sorted(unknown)}")

    settings = Settings(**(raw.get("settings") or {}))
    if settings.scope not in SCOPES:
        raise RuleError(f"settings.scope must be one of {sorted(SCOPES)}")
    if not isinstance(settings.suggest_ignore, list) or not all(
        isinstance(p, str) for p in settings.suggest_ignore
    ):
        raise RuleError("settings.suggest_ignore must be a list of payee names")

    rules, splits, transfers = _parse_sections(raw)
    include_dir = path.parent / INCLUDE_DIR
    for extra in sorted(include_dir.glob("*.y*ml")) if include_dir.is_dir() else []:
        doc = _read_yaml(extra)
        unknown = set(doc) - {"rules", "splits", "transfers"}
        if unknown:
            raise RuleError(f"{extra.name}: unsupported keys {sorted(unknown)}")
        try:
            r, sp, t = _parse_sections(doc)
        except RuleError as e:
            raise RuleError(f"{extra.name}: {e}") from e
        rules, splits, transfers = rules + r, splits + sp, transfers + t

    seen: set[str] = set()
    for item in [*rules, *splits, *transfers]:
        if item.name.lower() in seen:
            raise RuleError(f"duplicate rule name {item.name!r} (names must be unique)")
        seen.add(item.name.lower())
    return RuleSet(settings, rules, splits, transfers)
