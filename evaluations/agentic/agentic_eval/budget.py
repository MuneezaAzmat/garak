"""Transactional target-dispatch accounting with conservative crash recovery."""

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4


class BudgetExhausted(RuntimeError):
    """A run or component has no unreserved target requests remaining."""


class Budget:
    """Persist budget reservations and dispatches in a local SQLite ledger.

    Call reserve, then mark_dispatched immediately before the network call, then
    finish. A crash between recording dispatch and sending it is indeterminate;
    never automatically replay or refund it. Pending reservations hold capacity
    but are reported separately from recorded dispatches. Each method uses its
    own transaction so multiple local workers share the same hard cap.
    """

    def __init__(self, path: str | Path, cap: int, component_caps: dict):
        if type(cap) is not int or cap <= 0:
            raise ValueError("cap must be a positive integer")
        if not component_caps or any(
            not isinstance(key, str) or type(value) is not int or value <= 0
            for key, value in component_caps.items()
        ):
            raise ValueError("component caps must be positive named integers")
        if sum(component_caps.values()) != cap:
            raise ValueError("Component allocations must sum to the run cap")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cap = cap
        self.component_caps = dict(component_caps)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS requests ("
                "id TEXT PRIMARY KEY, component TEXT NOT NULL, purpose TEXT NOT NULL, "
                "state TEXT NOT NULL, outcome TEXT, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            config = json.dumps(
                {"cap": cap, "component_caps": component_caps}, sort_keys=True
            )
            previous = connection.execute(
                "SELECT value FROM metadata WHERE key='config'"
            ).fetchone()
            if previous is not None and previous[0] != config:
                raise ValueError("Existing ledger has a different budget configuration")
            connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('config', ?)", (config,)
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(
        self, component: str, purpose: str, request_id: str | None = None
    ) -> str:
        """Hold one slot atomically, without claiming a network dispatch occurred."""
        if component not in self.component_caps or not purpose:
            raise ValueError("A known component and request purpose are required")
        request_id = request_id or uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM requests WHERE id=?", (request_id,)
            ).fetchone():
                raise ValueError("Request ID already exists; do not replay it")
            counts = dict(
                connection.execute(
                    "SELECT component, count(*) FROM requests "
                    "WHERE state != 'cancelled' GROUP BY component"
                )
            )
            if (
                sum(counts.values()) >= self.cap
                or counts.get(component, 0) >= self.component_caps[component]
            ):
                raise BudgetExhausted(
                    f"No target-request budget remains for {component}"
                )
            connection.execute(
                "INSERT INTO requests (id, component, purpose, state) VALUES (?, ?, ?, 'reserved')",
                (request_id, component, purpose),
            )
        return request_id

    def _transition(self, request_id: str, old: str, new: str, outcome=None) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE requests SET state=?, outcome=? WHERE id=? AND state=?",
                (new, outcome, request_id, old),
            ).rowcount
            if changed != 1:
                raise ValueError(f"Request must be {old}: {request_id}")

    def mark_dispatched(self, request_id: str) -> None:
        """Durably record dispatch immediately before invoking the target adapter."""
        self._transition(request_id, "reserved", "dispatched")

    def finish(self, request_id: str, outcome: str = "completed") -> None:
        """Record completion, provider failure, or an ambiguous dispatched outcome."""
        if outcome not in {"completed", "error", "indeterminate"}:
            raise ValueError("Unknown dispatch outcome")
        self._transition(request_id, "dispatched", "finished", outcome)

    def cancel_before_dispatch(self, request_id: str) -> None:
        """Release a reservation only when the caller knows dispatch never began."""
        self._transition(request_id, "reserved", "cancelled")

    def snapshot(self) -> dict:
        """Return counters and pending IDs without refunding uncertain operations."""
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            records = [
                dict(row)
                for row in connection.execute("SELECT * FROM requests ORDER BY rowid")
            ]
        used = [record for record in records if record["state"] != "cancelled"]
        dispatched = [record for record in used if record["state"] != "reserved"]
        return {
            "cap": self.cap,
            "used": len(used),
            "remaining": self.cap - len(used),
            "dispatched": len(dispatched),
            "reserved": sum(record["state"] == "reserved" for record in used),
            "component_used": {
                key: sum(record["component"] == key for record in used)
                for key in self.component_caps
            },
            "unresolved_ids": [
                record["id"]
                for record in used
                if record["state"] in {"reserved", "dispatched"}
                or record["outcome"] == "indeterminate"
            ],
            "requests": records,
        }
