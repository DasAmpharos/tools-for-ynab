"""Append-only record of every change a run applies, plus undo for field edits.

One file per run (`<run id>.jsonl`). In GitHub Actions the file is uploaded as a
workflow artifact (kept for the artifact retention period) and `undo --from-github`
downloads it again."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools4ynab.core.client import YnabClient, YnabError

# Fields a categorization rule can change; the only ones `undo` knows how to restore.
UNDOABLE = {"category_id", "payee_name", "memo", "flag_color", "approved"}


class AuditLog:
    def __init__(
        self, directory: Path | None, run_id: str | None = None, redact: bool = False
    ) -> None:
        self.directory = directory
        # When set, the file on disk omits payee, amount, account and the description (what
        # a CI artifact would otherwise expose). It keeps what `undo` needs; the full entry
        # is still held in memory for the email.
        self.redact = redact
        # Microseconds keep ids unique even for two runs in the same second.
        self.run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.entries: list[dict[str, Any]] = []

    @property
    def path(self) -> Path | None:
        return self.directory / f"{self.run_id}.jsonl" if self.directory else None

    def record(
        self,
        kind: str,
        txn: dict[str, Any],
        rule: str,
        before: dict[str, Any],
        after: dict[str, Any],
        summary: str,
    ) -> None:
        """Remember (and, if a directory is set, persist) one applied change."""
        entry = {
            "run": self.run_id,
            "kind": kind,  # update | split | transfer
            "rule": rule,
            "txn_id": txn["id"],
            "date": txn["date"],
            "payee": txn.get("payee_name"),
            "amount": txn["amount"],
            "account": txn.get("account_name"),
            "summary": summary,
            "before": before,
            "after": after,
        }
        self.entries.append(entry)
        if self.path:
            on_disk = _redacted(entry) if self.redact else entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(on_disk, ensure_ascii=False) + "\n")


def _redacted(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry minus anything that names a merchant, account or amount. Field edits keep
    their before/after values (undo needs them); a transfer keeps only the id, date and
    cleared state of the transaction it removed."""
    after = dict(entry["after"])
    before = entry["before"]
    if entry["kind"] == "transfer":
        before = {}
        removed = after.get("removed")
        after = {
            "removed": {k: removed[k] for k in ("id", "date", "cleared") if k in removed}
            if removed
            else None
        }
    return {
        **entry,
        "payee": None,
        "amount": None,
        "account": None,
        "summary": "fields: " + ", ".join(sorted(after)),
        "before": before,
        "after": after,
    }


def load_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def latest_log(directory: Path) -> Path | None:
    logs = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    return logs[-1] if logs else None


def fetch_github_audit(run_id: str | None, dest: Path) -> list[Path]:
    """Download the `audit` artifact of a Actions run (the latest unexpired one if
    `run_id` is None) with the gh CLI, and return the log files it contained."""

    def gh(*args: str) -> str:
        try:
            done = subprocess.run(["gh", *args], capture_output=True, text=True)
        except FileNotFoundError:
            raise YnabError("the gh CLI is required for --from-github") from None
        if done.returncode:
            raise YnabError(f"gh {args[0]} failed: {done.stderr.strip()[:200]}")
        return done.stdout.strip()

    if run_id is None:
        run_id = gh(
            "api",
            "repos/{owner}/{repo}/actions/artifacts?name=audit&per_page=20",
            "--jq",
            "[.artifacts[] | select(.expired | not)][0].workflow_run.id // empty",
        )
        if not run_id:
            raise YnabError("no unexpired audit artifact found (they are kept ~90 days)")
    gh("run", "download", str(run_id), "-n", "audit", "-D", str(dest))
    files = sorted(dest.rglob("*.jsonl"))
    if not files:
        raise YnabError(f"run {run_id} has no audit log")
    return files


def plan_undo(
    client: YnabClient, entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Patches that restore each field edit to its old value, plus notes for what
    can't be undone automatically. A field is only restored if it still holds the
    value the run set, so edits you made afterwards are never clobbered."""
    patches: list[dict[str, Any]] = []
    notes: list[str] = []
    for e in entries:
        who = e.get("payee") or e["txn_id"][:8]  # redacted logs identify by transaction id
        amount = f" {e['amount'] / 1000:.2f}" if e.get("amount") is not None else ""
        label = f"{e['date']} {who}{amount} [{e['rule']}]"
        if e["kind"] != "update":
            notes.append(
                f"{label}: {e['kind']} can't be undone automatically "
                "(YNAB's API can't unsplit or recreate a deleted transaction); "
                "fix it in YNAB by hand"
            )
            continue
        current = client.get_transaction(e["txn_id"])
        patch: dict[str, Any] = {"id": e["txn_id"]}
        for field, new in e["after"].items():
            old = e["before"][field]
            if field not in UNDOABLE or (field == "payee_name" and old is None):
                continue
            if (current.get(field) or None) != (new or None):
                notes.append(f"{label}: {field} was changed again since; left alone")
                continue
            patch[field] = old
        if len(patch) > 1:
            patches.append(patch)
    return patches, notes
