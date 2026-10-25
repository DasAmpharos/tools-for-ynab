"""usage: tools4ynab {rules,reports} ...

rules    auto-categorize transactions (see `tools4ynab rules -h`)
reports  spending trends and recurring charges (see `tools4ynab reports -h`)"""

from __future__ import annotations

import sys
from collections.abc import Callable

from tools_for_ynab.reports import cli as reports_cli
from tools_for_ynab.rules import cli as rules_cli

TOOLS: dict[str, Callable[[list[str]], int]] = {
    "rules": rules_cli.main,
    "reports": reports_cli.main,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in TOOLS:
        return TOOLS[argv[0]](argv[1:])
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
