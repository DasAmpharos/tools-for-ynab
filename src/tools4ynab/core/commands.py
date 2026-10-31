"""Click plumbing shared by the tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from .client import YnabError
from .session import open_session, where

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


def execute(
    fn: Callable[..., int],
    params: dict[str, Any],
    expected: tuple[type[Exception], ...] = (YnabError,),
) -> int:
    """Run `fn(session, **params)` and return its exit code, reporting errors without
    leaking secrets or (in GitHub Actions) data."""
    session = open_session()
    if session is None:
        return 2
    try:
        code, message = fn(session, **params), None
    except expected as e:
        code, message = 1, session.scrub(str(e))
    except Exception as e:  # noqa: BLE001 - never print a raw traceback that could hold a URL
        # Exception text can hold data (a KeyError names its key), so CI logs get type + place.
        detail = f"{type(e).__name__} at {where(e)}"
        code, message = 1, session.scrub(detail if session.quiet else f"{detail}: {e}")
    if session.quiet:
        session.print_trace()
    if message is not None:
        click.echo(f"error: {message}", err=True)
    return code
