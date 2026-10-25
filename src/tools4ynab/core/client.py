"""Thin wrapper over the YNAB REST API (https://api.ynab.com/v1)."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import requests

BASE_URL = "https://api.ynab.com/v1"
UPDATE_BATCH_SIZE = 100


class YnabError(RuntimeError):
    pass


class YnabClient:
    def __init__(self, api_key: str, budget_id: str = "last-used") -> None:
        self.budget_id = budget_id
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"
        # One line per API call (no payloads), for debugging without logging any data.
        self.trace: list[str] = []

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        # Error messages must never include the budget id (it is in the URL) or key.
        label = f"{method} {path.replace(self.budget_id, '<budget>')}"
        started = time.monotonic()
        try:
            resp = self._session.request(method, f"{BASE_URL}{path}", timeout=30, **kwargs)
        except requests.RequestException as e:
            self.trace.append(f"{label} -> failed ({type(e).__name__})")
            raise YnabError(f"{label} failed: {type(e).__name__}") from None
        self.trace.append(f"{label} -> {resp.status_code} ({time.monotonic() - started:.1f}s)")
        if not resp.ok:
            try:
                detail = resp.json()["error"]["detail"]
            except ValueError, KeyError:
                detail = "unparseable error body"
            raise YnabError(f"{label} -> {resp.status_code}: {detail}")
        return resp.json()["data"]

    def categories(self) -> list[dict[str, Any]]:
        """Flat list of non-deleted categories, each with a `group_name`."""
        data = self._request("GET", f"/budgets/{self.budget_id}/categories")
        return [
            {**cat, "group_name": group["name"]}
            for group in data["category_groups"]
            if not group["deleted"]
            for cat in group["categories"]
            if not cat["deleted"]
        ]

    def accounts(self) -> dict[str, dict[str, Any]]:
        data = self._request("GET", f"/budgets/{self.budget_id}/accounts")
        return {a["id"]: a for a in data["accounts"] if not a["deleted"]}

    def get_transaction(self, txn_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/budgets/{self.budget_id}/transactions/{txn_id}")
        return data["transaction"]

    def update_transaction(self, txn: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """PUT one transaction. YNAB's PUT may require the identifying fields, so
        they are always carried over from the existing transaction."""
        body = {
            "account_id": txn["account_id"],
            "date": txn["date"],
            "amount": txn["amount"],
            **patch,
        }
        data = self._request(
            "PUT",
            f"/budgets/{self.budget_id}/transactions/{txn['id']}",
            json={"transaction": body},
        )
        return data["transaction"]

    def delete_transaction(self, txn_id: str) -> None:
        self._request("DELETE", f"/budgets/{self.budget_id}/transactions/{txn_id}")

    def transactions(self, since: date) -> list[dict[str, Any]]:
        """All non-deleted transactions on/after `since`."""
        data = self._request(
            "GET",
            f"/budgets/{self.budget_id}/transactions",
            params={"since_date": since.isoformat()},
        )
        return [t for t in data["transactions"] if not t["deleted"]]

    def update_transactions(self, patches: list[dict[str, Any]]) -> None:
        for i in range(0, len(patches), UPDATE_BATCH_SIZE):
            self._request(
                "PATCH",
                f"/budgets/{self.budget_id}/transactions",
                json={"transactions": patches[i : i + UPDATE_BATCH_SIZE]},
            )
