import smtplib

import pytest
import requests
from fake_ynab import BUDGET, FakeYnab

RULES = """\
settings: {since_days: 30, scope: unapproved}
rules:
  - name: Greenleaf groceries
    match: {payee_equals: Greenleaf, direction: outflow}
    set: {category: Groceries}
splits:
  - name: AppHub bundle
    match: {payee_equals: AppHub, amount: {equals: 27.98}, direction: outflow}
    parts:
      - {category: Comedy, amount: 4.99}
      - {category: Assistant, amount: 15.00}
      - {category: Sketches, amount: 7.99}
transfers:
  - name: Round Up
    from: {account: Acme Checking, payee_contains: round up}
    to: {account: Acme Savings, payee_contains: roundup}
"""


@pytest.fixture
def ynab(monkeypatch, tmp_path) -> FakeYnab:
    """A fake YNAB behind requests, a rules file, and a clean environment."""
    fake = FakeYnab()
    monkeypatch.setattr(
        requests.Session, "request", lambda self, method, url, **kw: fake.request(method, url, **kw)
    )
    monkeypatch.setattr(
        "tools_for_ynab.core.session.load_dotenv", lambda *a, **k: None
    )  # never read a real .env
    for var in (
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_HOST",
        "SMTP_PORT",
        "NOTIFY_EMAIL_TO",
        "NOTIFY_EMAIL_FROM",
        "YNAB_DISABLED_RULES",
        "YNAB_AUDIT_DIR",
        "GITHUB_RUN_ID",
        "GITHUB_ACTIONS",
        "YNAB_VERBOSE",
        "GITHUB_STEP_SUMMARY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("YNAB_API_KEY", "test-key-123")
    monkeypatch.setenv("YNAB_BUDGET_ID", BUDGET)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules.yaml").write_text(RULES)
    return fake


@pytest.fixture
def mailbox(monkeypatch) -> list:
    """Captures emails instead of sending; also turns notifications on."""
    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            self.password = password

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password-xyz")
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "me@example.com, partner@example.com")
    return sent
