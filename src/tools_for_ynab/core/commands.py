"""Click plumbing shared by the tools."""

from __future__ import annotations

from typing import Any

import click

CONTEXT_SETTINGS: dict[str, Any] = {"help_option_names": ["-h", "--help"]}


class DefaultGroup(click.Group):
    """A group that runs `default_command` when no subcommand is named, so
    `tools4ynab rules --apply` means `tools4ynab rules run --apply`."""

    def __init__(self, *args: Any, default_command: str, **kwargs: Any) -> None:
        kwargs.setdefault("context_settings", CONTEXT_SETTINGS)
        super().__init__(*args, **kwargs)
        self.default_command = default_command

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args or (args[0] not in self.commands and args[0] not in ctx.help_option_names):
            args = [self.default_command, *args]
        return super().parse_args(ctx, args)
