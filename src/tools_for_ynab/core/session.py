"""Credentials, the YNAB client, secret scrubbing and email for one CLI invocation."""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Mapping

from dotenv import find_dotenv, load_dotenv

from . import notify
from .client import YnabClient


def quiet_mode(env: Mapping[str, str]) -> bool:
    """True in GitHub Actions unless YNAB_VERBOSE is set: logs there are readable by anyone
    with access to the repo, so they carry counts and diagnostics but no spending data."""
    return env.get("GITHUB_ACTIONS") == "true" and env.get("YNAB_VERBOSE", "").lower() not in (
        "1",
        "true",
        "yes",
    )


def where(exc: BaseException) -> str:
    """Where an exception came from (file:line in function), without any of its data."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "/tools_for_ynab/" in f.filename.replace("\\", "/")]
    f = (ours or frames or [None])[-1]
    return f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} in {f.name}" if f else "unknown"


class Session:
    """Everything a command needs: environment, YNAB client, and what's required to scrub
    secrets from anything printed or emailed."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = dict(env)
        self.api_key = self.env["YNAB_API_KEY"]
        self.budget_id = self.env.get("YNAB_BUDGET_ID") or "last-used"
        self.client = YnabClient(self.api_key, self.budget_id)
        self.quiet = quiet_mode(self.env)
        self._secrets = [
            v
            for v in (self.api_key, self.budget_id, self.env.get("SMTP_PASSWORD"))
            if v and v != "last-used"
        ]

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def print_trace(self) -> None:
        """The API calls made so far: status and timing only, safe for public logs."""
        if self.client.trace:
            print("API calls:")
            print("\n".join(f"  {line}" for line in self.client.trace))

    def send_email(
        self,
        subject: str,
        text: str,
        body: str,
        recipient_vars: tuple[str, ...] = ("NOTIFY_EMAIL_TO",),
    ) -> None:
        """Email if configured. A mail failure never fails the run: whatever the run did
        is already done (and recorded)."""
        cfg = notify.EmailConfig.from_env(self.env, recipient_vars)
        if cfg is None:
            return
        try:
            notify.send_email(cfg, subject, self.scrub(text), self.scrub(body))
            print(f"Emailed to {len(cfg.recipients)} recipient(s).")
        except Exception as e:  # noqa: BLE001 - any SMTP/network failure
            print(f"warning: could not send email ({type(e).__name__})", file=sys.stderr)


def open_session[S: Session](session_cls: type[S] | None = None) -> S | None:
    """Load .env (a no-op in GitHub Actions, where secrets are real env vars) and start a
    session, or print why not."""
    # From the current folder (your config), not wherever this package is installed.
    load_dotenv(find_dotenv(usecwd=True))
    if not os.environ.get("YNAB_API_KEY"):
        print("YNAB_API_KEY is not set (put it in .env)", file=sys.stderr)
        return None
    return (session_cls or Session)(os.environ)
