"""Sending email through SMTP (Gmail by default), shared by the rules and reports tools."""

from __future__ import annotations

import html
import smtplib
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass
class EmailConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: list[str]

    @classmethod
    def from_env(
        cls, env: Mapping[str, str], recipient_vars: tuple[str, ...] = ("NOTIFY_EMAIL_TO",)
    ) -> EmailConfig | None:
        """None unless SMTP credentials and at least one recipient are configured.
        Recipients come from the first non-empty variable in `recipient_vars`."""
        user, password = env.get("SMTP_USER"), env.get("SMTP_PASSWORD")
        raw = next((env[v] for v in recipient_vars if env.get(v)), "")
        recipients = [r.strip() for r in raw.split(",") if r.strip()]
        if not (user and password and recipients):
            return None
        host = env.get("SMTP_HOST") or "smtp.gmail.com"
        if host.endswith("gmail.com"):
            # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
            password = password.replace(" ", "")
        return cls(
            host=host,
            port=int(env.get("SMTP_PORT") or 587),
            user=user,
            password=password,
            sender=env.get("NOTIFY_EMAIL_FROM") or user,
            recipients=recipients,
        )


def send_email(cfg: EmailConfig, subject: str, text: str, body_html: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg.sender, ", ".join(cfg.recipients)
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    smtp_cls = smtplib.SMTP_SSL if cfg.port == 465 else smtplib.SMTP
    with smtp_cls(cfg.host, cfg.port, timeout=30) as smtp:
        if cfg.port != 465:
            smtp.starttls()
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)


def run_url(env: Mapping[str, str]) -> str | None:
    """Link to the GitHub Actions run, when running there."""
    if env.get("GITHUB_RUN_ID") and env.get("GITHUB_REPOSITORY"):
        server = env.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    return None


def render_list(title: str, items: list[str], footer: list[str]) -> tuple[str, str]:
    """(plain text, html) for a titled list of monospace lines with footer paragraphs."""
    text = "\n".join([title, "", *items, "", *footer])
    rows = "".join(f"<li><code>{html.escape(i)}</code></li>" for i in items)
    foot = "".join(f"<p>{html.escape(f)}</p>" for f in footer)
    return text, f"<h3>{html.escape(title)}</h3><ul>{rows}</ul>{foot}"
