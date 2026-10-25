"""Whole-program tests: main() against a fake YNAB, covering --apply paths that
unit tests can't (HTTP shape, audit log, email, undo)."""

import json
from pathlib import Path

from cli_helpers import rules_main as main
from fake_ynab import FakeYnab


def seed(y: FakeYnab):
    greenleaf = y.add(
        "chk", -42.00, "Greenleaf", category_id=y.cat_id("Restaurants")
    )  # YNAB's guess
    apphub = y.add("chk", -27.98, "AppHub")
    out = y.add("chk", -0.64, "ROUND UP TRANSFER TO ACCT 1")
    inn = y.add("sav", 0.64, "ROUNDUP CREDIT FROM ACCT 2")
    return greenleaf, apphub, out, inn


def test_dry_run_changes_nothing(ynab, capsys):
    seed(ynab)
    before = json.dumps(ynab.txns, sort_keys=True)
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Dry run: 1 categorization(s), 1 split(s), 1 transfer pair(s)" in out
    assert "was Restaurants" in out  # YNAB's guess is shown as replaced
    assert json.dumps(ynab.txns, sort_keys=True) == before
    assert not Path("audit").exists()


def test_apply_everything(ynab, capsys):
    greenleaf, apphub, out, inn = seed(ynab)
    assert main(["--apply"]) == 0  # legacy form still means `run --apply`

    assert greenleaf["category_id"] == ynab.cat_id("Groceries") and greenleaf["approved"]

    assert apphub["category_id"] is None and apphub["approved"]
    assert sorted(s["amount"] for s in apphub["subtransactions"]) == [-15000, -7990, -4990]

    assert out["transfer_account_id"] == "sav" and out["approved"]
    assert inn["deleted"]  # the imported duplicate was removed
    counterpart = ynab.txns[out["transfer_transaction_id"]]
    assert counterpart["cleared"] == "cleared" and counterpart["date"] == inn["date"]

    entries = [
        json.loads(line)
        for f in Path("audit").glob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    assert sorted(e["kind"] for e in entries) == ["split", "transfer", "update"]
    removed = next(e for e in entries if e["kind"] == "transfer")["after"]["removed"]
    assert removed["id"] == inn["id"]  # what was deleted is on record


def test_second_run_is_a_noop(ynab, capsys):
    seed(ynab)
    main(["run", "--apply"])
    capsys.readouterr()
    main(["run", "--apply"])
    assert "0 categorization(s), 0 split(s), 0 transfer pair(s)" in capsys.readouterr().out
    assert len(list(Path("audit").glob("*.jsonl"))) == 1  # no new log when nothing changed


def test_transfer_that_links_existing_pair_deletes_nothing(ynab):
    ynab.transfer_mode = "link"
    _, _, out, inn = seed(ynab)
    assert main(["run", "--apply"]) == 0
    assert not inn["deleted"] and out["transfer_transaction_id"] == inn["id"]


def test_approved_category_is_never_touched(ynab):
    k = ynab.add("chk", -10, "Greenleaf", approved=True, category_id=ynab.cat_id("Restaurants"))
    main(["run", "--apply"])
    assert k["category_id"] == ynab.cat_id("Restaurants")


def test_disabled_rules_are_skipped_but_typos_fail(ynab, monkeypatch, capsys):
    greenleaf, *_ = seed(ynab)
    monkeypatch.setenv("YNAB_DISABLED_RULES", "Round Up,AppHub bundle")
    assert main(["run", "--apply"]) == 0
    assert greenleaf["approved"]  # categorization still ran
    assert ynab.txns["txn-3"]["transfer_account_id"] is None  # transfer skipped
    assert ynab.txns["txn-2"]["subtransactions"] == []  # split skipped
    monkeypatch.setenv("YNAB_DISABLED_RULES", "Rnoud Up")
    assert main(["run", "--apply"]) == 1
    assert "unknown rule" in capsys.readouterr().err


def test_email_digest_goes_to_every_recipient_without_secrets(ynab, mailbox):
    seed(ynab)
    assert main(["run", "--apply"]) == 0
    assert len(mailbox) == 1
    msg = mailbox[0]
    assert msg["To"] == "me@example.com, partner@example.com"
    assert msg["Subject"] == "YNAB automation: 3 changes applied"
    body = msg.get_body(("plain",)).get_content()
    assert "Greenleaf" in body and "AppHub" in body
    for secret in ("test-key-123", "app-password-xyz", "budget-abc"):
        assert secret not in msg.as_string()


def test_no_email_for_dry_runs_or_when_nothing_changed(ynab, mailbox):
    seed(ynab)
    main(["run"])
    assert mailbox == []
    main(["run", "--apply"])
    mailbox.clear()
    main(["run", "--apply"])
    assert mailbox == []


def test_failure_emails_and_scrubs_secrets(ynab, mailbox, capsys):
    seed(ynab)
    ynab.fail_on.add("PATCH /transactions")
    assert main(["run", "--apply"]) == 1
    err = capsys.readouterr().err
    # The fake API echoes the key and budget id in its error; neither may escape.
    assert "error:" in err and "boom" in err
    assert "test-key-123" not in err and "budget-abc" not in err and "<budget>" in err
    assert len(mailbox) == 1 and mailbox[0]["Subject"] == "YNAB automation FAILED"
    for secret in ("test-key-123", "budget-abc", "app-password-xyz"):
        assert secret not in mailbox[0].as_string()


def test_failure_after_partial_progress_is_audited_and_reported(ynab, mailbox):
    seed(ynab)
    ynab.fail_on.add("PUT /transactions/txn-2")  # the split fails; categorization already done
    assert main(["run", "--apply"]) == 1
    logged = [
        json.loads(line)
        for f in Path("audit").glob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    assert [e["kind"] for e in logged] == ["update"]
    assert "Applied before the failure" in mailbox[0].get_body(("plain",)).get_content()
    assert ynab.txns["txn-2"]["subtransactions"] == []  # nothing half-done


def test_mail_failure_never_fails_the_run(ynab, mailbox, monkeypatch, capsys):
    import smtplib

    def boom(*a, **k):
        raise OSError("smtp down")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    seed(ynab)
    assert main(["run", "--apply"]) == 0
    assert "could not send email (OSError)" in capsys.readouterr().err


def test_undo_restores_field_edits_but_not_later_changes(ynab, capsys):
    k1 = ynab.add("chk", -42, "Greenleaf", category_id=ynab.cat_id("Restaurants"))
    k2 = ynab.add("chk", -20, "Greenleaf")
    main(["run", "--apply"])
    # You re-categorize one of them after the run.
    k2["category_id"] = ynab.cat_id("Sketches")
    capsys.readouterr()

    assert main(["undo"]) == 0  # dry run
    assert k1["category_id"] == ynab.cat_id("Groceries")
    assert main(["undo", "--apply"]) == 0
    out = capsys.readouterr().out
    assert k1["category_id"] == ynab.cat_id("Restaurants") and not k1["approved"]
    assert k2["category_id"] == ynab.cat_id("Sketches")  # your edit survived
    assert "changed again since" in out


def test_undo_explains_what_it_cannot_reverse(ynab, capsys):
    seed(ynab)
    main(["run", "--apply"])
    capsys.readouterr()
    main(["undo"])
    out = capsys.readouterr().out
    assert "split can't be undone" in out and "transfer can't be undone" in out


def test_rules_d_is_loaded_after_the_main_file(ynab):
    Path("rules.d").mkdir()
    Path("rules.d/extra.yaml").write_text(
        "rules:\n  - name: Extra\n    match: {payee_equals: Mercado}\n"
        "    set: {category: Groceries}\n"
    )
    t = ynab.add("chk", -5, "Mercado")
    main(["run", "--apply"])
    assert t["category_id"] == ynab.cat_id("Groceries")


def test_suggest_writes_a_file_the_loader_accepts(ynab, capsys):
    for i in range(4):
        ynab.add(
            "chk",
            -18.99,
            "Tunely",
            days_ago=10 + i,
            approved=True,
            category_id=ynab.cat_id("Comedy"),
        )
    assert main(["suggest", "--output", "rules.d/suggested.yaml"]) == 0
    assert "Wrote 1 suggested rule(s)" in capsys.readouterr().out
    t = ynab.add("chk", -18.99, "Tunely")
    assert main(["run", "--apply"]) == 0  # includes the generated file
    assert t["category_id"] == ynab.cat_id("Comedy")
    assert main(["suggest", "--output", "rules.d/again.yaml"]) == 0
    assert not Path("rules.d/again.yaml").exists()  # already covered: nothing new


def test_backtest_flags_disagreements_with_a_failing_exit_code(ynab, capsys):
    ynab.add("chk", -42, "Greenleaf", approved=True, category_id=ynab.cat_id("Groceries"))
    assert main(["backtest"]) == 0
    ynab.add("chk", -43, "Greenleaf", approved=True, category_id=ynab.cat_id("Restaurants"))
    assert main(["backtest"]) == 1
    assert "DIFF" in capsys.readouterr().out


def test_explain_and_unmatched_and_duplicates(ynab, capsys):
    seed(ynab)
    ynab.add("chk", -9, "Mystery Shop", approved=True, category_id=ynab.cat_id("Restaurants"))
    ynab.add("chk", -7.77, "Cafe", cleared="uncleared", import_id=None)
    ynab.add("chk", -7.77, "Cafe")
    assert main(["explain", "greenleaf"]) == 0
    assert "would change: category=Groceries (was Restaurants)" in capsys.readouterr().out
    assert main(["unmatched"]) == 0
    assert "Mystery Shop" in capsys.readouterr().out
    assert main(["duplicates"]) == 0
    assert "1 possible duplicate" in capsys.readouterr().out


def test_missing_credentials(ynab, monkeypatch, capsys):
    monkeypatch.delenv("YNAB_API_KEY")
    assert main([]) == 2
    assert "YNAB_API_KEY is not set" in capsys.readouterr().err


def fake_gh(monkeypatch, audit_text, *, run_id="4242", artifacts=True):
    """Stand in for the gh CLI: `gh api ...artifacts` -> a run id, `gh run download` ->
    writes the audit artifact into -D."""
    import subprocess
    from types import SimpleNamespace

    calls = []

    def run(cmd, capture_output=True, text=True):
        calls.append(cmd)
        if cmd[1] == "api":
            return SimpleNamespace(returncode=0, stdout=run_id if artifacts else "", stderr="")
        dest = Path(cmd[cmd.index("-D") + 1])
        (dest / "20261018T011700000000Z.jsonl").write_text(audit_text)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_undo_from_github_downloads_the_run_artifact(ynab, monkeypatch, capsys):
    import shutil

    k = ynab.add("chk", -42, "Greenleaf", category_id=ynab.cat_id("Restaurants"))
    main(["run", "--apply"])
    text = next(Path("audit").glob("*.jsonl")).read_text()
    shutil.rmtree("audit")  # the CI runner's disk is gone; only the artifact remains
    calls = fake_gh(monkeypatch, text)
    capsys.readouterr()

    assert main(["undo", "--from-github"]) == 0  # dry run, latest run
    assert "GitHub run (latest)" in capsys.readouterr().out
    assert k["category_id"] == ynab.cat_id("Groceries")
    assert main(["undo", "--from-github", "777", "--apply"]) == 0  # a specific run
    assert k["category_id"] == ynab.cat_id("Restaurants") and not k["approved"]
    assert ["gh", "run", "download", "777", "-n", "audit"] == calls[-1][:6]


def test_undo_from_github_failures_are_reported(ynab, monkeypatch, capsys):
    fake_gh(monkeypatch, "", artifacts=False)
    assert main(["undo", "--from-github"]) == 1
    assert "no unexpired audit artifact" in capsys.readouterr().err

    import subprocess

    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    assert main(["undo", "--from-github"]) == 1
    assert "gh CLI is required" in capsys.readouterr().err


def test_email_tells_you_how_to_undo_the_run(ynab, mailbox, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/ynab")
    seed(ynab)
    main(["run", "--apply"])
    body = mailbox[0].get_body(("plain",)).get_content()
    assert "https://github.com/me/ynab/actions/runs/999" in body
    assert "tools4ynab rules undo --from-github 999" in body


def test_test_email_sends_to_everyone_and_reports_problems(ynab, mailbox, capsys):
    assert main(["test-email"]) == 0
    assert mailbox[0]["To"] == "me@example.com, partner@example.com"
    assert mailbox[0]["Subject"].startswith("[TEST]")
    assert "Sent a test email to 2 recipient(s) via smtp.gmail.com:587" in capsys.readouterr().out


def test_test_email_names_what_is_missing_without_values(ynab, monkeypatch, capsys):
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    assert main(["test-email"]) == 1
    err = capsys.readouterr().err
    assert "missing: SMTP_PASSWORD, NOTIFY_EMAIL_TO" in err and "bot@example.com" not in err


def test_test_email_surfaces_a_login_failure_without_the_password(
    ynab, mailbox, monkeypatch, capsys
):
    import smtplib

    class Rejects(smtplib.SMTP):
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(
                535, f"Username and Password not accepted ({password})".encode()
            )

    monkeypatch.setattr(smtplib, "SMTP", Rejects)
    assert main(["test-email"]) == 1
    err = capsys.readouterr().err
    assert "SMTPAuthenticationError" in err and "app-password-xyz" not in err


def test_gmail_app_password_spaces_are_ignored(monkeypatch):
    from tools4ynab.core.notify import EmailConfig

    cfg = EmailConfig.from_env(
        {
            "SMTP_USER": "a@gmail.com",
            "SMTP_PASSWORD": "abcd efgh ijkl mnop",
            "NOTIFY_EMAIL_TO": "a@gmail.com",
        }
    )
    assert cfg.password == "abcdefghijklmnop" and cfg.port == 587
