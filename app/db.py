"""SQLite persistence with integer revisions for methods and runs.

Each method and each run carries a monotonically increasing revision
(starting at 1 on creation, incremented on every update).  Updates require the
caller to state the revision they saw; a mismatch raises
:class:`RevisionConflict`, which the HTTP layer maps to 409.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS methods (
    method_id       TEXT PRIMARY KEY,
    allowed_charges TEXT NOT NULL,
    error           TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    method_id  TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (method_id) REFERENCES methods(method_id)
);

CREATE TABLE IF NOT EXISTS peaks (
    run_id   TEXT NOT NULL,
    peak_id  TEXT NOT NULL,
    mz       TEXT NOT NULL,
    ordinal  INTEGER NOT NULL,
    PRIMARY KEY (run_id, peak_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    method_id       TEXT NOT NULL,
    method_revision INTEGER NOT NULL,
    run_revision    INTEGER NOT NULL,
    result          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


class NotFound(Exception):
    """Raised when a referenced method/run/analysis does not exist."""


class RevisionConflict(Exception):
    """Raised when an update/analysis is based on a stale revision."""

    def __init__(self, resource: str, resource_id: str, seen: int, current: int):
        self.resource = resource
        self.resource_id = resource_id
        self.seen = seen
        self.current = current
        super().__init__(
            f"{resource} {resource_id!r}: expected revision {seen}, current is {current}"
        )


class Conflict(Exception):
    """Generic 409 conflict (e.g. duplicate identifier)."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_decimal(raw: Any, *, field: str, positive: bool = False) -> Decimal:
    """Parse a JSON decimal string exactly, rejecting floats and non-finite values."""
    if isinstance(raw, bool):
        raise ValueError(f"{field} must be a decimal string")
    if not isinstance(raw, (str, int, Decimal)):
        raise ValueError(f"{field} must be a decimal string")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} is not a valid decimal") from None
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def canonical_decimal(value: Decimal) -> str:
    """Stable textual form used both for storage and exact comparison."""
    return format(value.normalize(), "f")


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ methods
    def create_method(
        self, method_id: str, allowed_charges: list[int], error: Decimal
    ) -> dict:
        now = _utcnow()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO methods "
                    "(method_id, allowed_charges, error, revision, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (
                        method_id,
                        json.dumps(sorted(set(allowed_charges))),
                        canonical_decimal(error),
                        now,
                        now,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                raise Conflict(f"method {method_id!r} already exists") from None
        return self.get_method(method_id)

    def get_method(self, method_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM methods WHERE method_id = ?", (method_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"method {method_id!r} does not exist")
        return {
            "method_id": row["method_id"],
            "allowed_charges": json.loads(row["allowed_charges"]),
            "error": row["error"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_method(
        self,
        method_id: str,
        allowed_charges: list[int],
        error: Decimal,
        expected_revision: int,
    ) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM methods WHERE method_id = ?", (method_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"method {method_id!r} does not exist")
            if row["revision"] != expected_revision:
                raise RevisionConflict(
                    "method", method_id, expected_revision, row["revision"]
                )
            now = _utcnow()
            self._conn.execute(
                "UPDATE methods SET allowed_charges = ?, error = ?, "
                "revision = revision + 1, updated_at = ? WHERE method_id = ?",
                (
                    json.dumps(sorted(set(allowed_charges))),
                    canonical_decimal(error),
                    now,
                    method_id,
                ),
            )
            self._conn.commit()
        return self.get_method(method_id)

    # -------------------------------------------------------------------- runs
    def create_run(self, run_id: str, method_id: str, peaks: list[dict]) -> dict:
        now = _utcnow()
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM methods WHERE method_id = ?", (method_id,)
            ).fetchone() is None:
                raise NotFound(f"method {method_id!r} does not exist")
            try:
                self._conn.execute(
                    "INSERT INTO runs (run_id, method_id, revision, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (run_id, method_id, now, now),
                )
            except sqlite3.IntegrityError:
                raise Conflict(f"run {run_id!r} already exists") from None
            self._insert_peaks(run_id, peaks)
            self._conn.commit()
        return self.get_run(run_id)

    def _insert_peaks(self, run_id: str, peaks: list[dict]) -> None:
        rows = [
            (run_id, peak["peak_id"], canonical_decimal(peak["mz"]), ordinal)
            for ordinal, peak in enumerate(peaks)
        ]
        try:
            self._conn.executemany(
                "INSERT INTO peaks (run_id, peak_id, mz, ordinal) VALUES (?, ?, ?, ?)",
                rows,
            )
        except sqlite3.IntegrityError:
            raise Conflict("duplicate peak_id within run") from None

    def get_run(self, run_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"run {run_id!r} does not exist")
            peak_rows = self._conn.execute(
                "SELECT peak_id, mz, ordinal FROM peaks WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        return {
            "run_id": row["run_id"],
            "method_id": row["method_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "peaks": [
                {"peak_id": r["peak_id"], "mz": r["mz"], "ordinal": r["ordinal"]}
                for r in peak_rows
            ],
        }

    def update_run(self, run_id: str, peaks: list[dict], expected_revision: int) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"run {run_id!r} does not exist")
            if row["revision"] != expected_revision:
                raise RevisionConflict("run", run_id, expected_revision, row["revision"])
            self._conn.execute("DELETE FROM peaks WHERE run_id = ?", (run_id,))
            self._insert_peaks(run_id, peaks)
            now = _utcnow()
            self._conn.execute(
                "UPDATE runs SET revision = revision + 1, updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            self._conn.commit()
        return self.get_run(run_id)

    # ---------------------------------------------------------------- analyses
    def save_analysis(
        self,
        run_id: str,
        method_id: str,
        method_revision: int,
        run_revision: int,
        result: dict,
    ) -> str:
        analysis_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO analyses "
                "(analysis_id, run_id, method_id, method_revision, run_revision, "
                "result, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_id,
                    run_id,
                    method_id,
                    method_revision,
                    run_revision,
                    json.dumps(result),
                    _utcnow(),
                ),
            )
            self._conn.commit()
        return analysis_id

    def get_analysis(self, analysis_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "analysis_id": row["analysis_id"],
            "run_id": row["run_id"],
            "method_id": row["method_id"],
            "method_revision": row["method_revision"],
            "run_revision": row["run_revision"],
            "result": json.loads(row["result"]),
            "created_at": row["created_at"],
        }
