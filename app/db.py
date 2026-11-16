"""SQLite persistence with integer revisions for methods and runs.

Each method and each run carries a monotonically increasing revision
(starting at 1 on creation, incremented on every update).  Updates require the
caller to state the revision they saw; a mismatch raises
:class:`RevisionConflict`, which the HTTP layer maps to 409.

Every committed run revision also persists an immutable peak-table snapshot in
``run_revisions`` (append-only, enforced by triggers).  Run creation and every
correction write the live peak rows and the snapshot in one transaction, so a
failed correction never leaves a broken chain or a half-updated peak table.
The snapshots back the per-analysis change chain
(:meth:`Database.get_peak_table_chain`).  Databases created before snapshots
existed are migrated on open: the *current* revision of every run is
back-filled from the live peak rows (flagged ``backfilled``); intermediate
revisions that were never recorded stay missing and are reported as
untraceable gaps rather than being reconstructed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Optional

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

-- Append-only peak-table snapshots, one row per committed run revision.
-- ``backfilled`` marks rows reconstructed from the live peak table during
-- migration of a pre-snapshot database (content is the true state at that
-- revision; only intermediate revisions remain unrecoverable).
CREATE TABLE IF NOT EXISTS run_revisions (
    run_id     TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    peaks      TEXT NOT NULL,
    backfilled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, revision),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- Snapshots are immutable history: never updated, never deleted.
CREATE TRIGGER IF NOT EXISTS run_revisions_no_update
BEFORE UPDATE ON run_revisions
BEGIN
    SELECT RAISE(ABORT, 'run_revisions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS run_revisions_no_delete
BEFORE DELETE ON run_revisions
BEGIN
    SELECT RAISE(ABORT, 'run_revisions is append-only');
END;
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


def _snapshot_peak_rows(peaks: list[dict]) -> list[dict]:
    """Serialise a peak list for snapshot storage (canonical m/z text)."""
    rows = []
    for peak in peaks:
        mz = peak["mz"]
        if not isinstance(mz, str):
            mz = canonical_decimal(mz)
        rows.append({"peak_id": peak["peak_id"], "mz": mz})
    return rows


def diff_peak_lists(before: list[dict], after: list[dict]) -> dict:
    """Peak-ID keyed diff between two peak-table snapshots.

    m/z values are canonical decimal strings, so text equality is exact.
    Output is sorted by peak_id for deterministic responses.
    """
    old = {p["peak_id"]: p["mz"] for p in before}
    new = {p["peak_id"]: p["mz"] for p in after}
    added = [
        {"peak_id": pid, "mz": new[pid]} for pid in sorted(new.keys() - old.keys())
    ]
    removed = [
        {"peak_id": pid, "mz": old[pid]} for pid in sorted(old.keys() - new.keys())
    ]
    mz_changed = [
        {"peak_id": pid, "before": old[pid], "after": new[pid]}
        for pid in sorted(old.keys() & new.keys())
        if old[pid] != new[pid]
    ]
    return {"added": added, "removed": removed, "mz_changed": mz_changed}


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
        self._backfill_missing_current_snapshots()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize writes and make them atomic.

        Every statement inside the block commits together; any exception
        rolls the connection back so a failed write never leaves partial
        changes behind for the next transaction to pick up.
        """
        with self._lock:
            try:
                yield
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # ------------------------------------------------- run revision snapshots
    def _backfill_missing_current_snapshots(self) -> None:
        """Migration for pre-snapshot databases.

        For every run whose *current* revision has no snapshot, record one
        from the live peak rows (flagged ``backfilled``).  This anchors the
        chain end without inventing any intermediate history.
        """
        with self._transaction():
            rows = self._conn.execute(
                "SELECT r.run_id, r.revision FROM runs r "
                "LEFT JOIN run_revisions rv "
                "  ON rv.run_id = r.run_id AND rv.revision = r.revision "
                "WHERE rv.run_id IS NULL"
            ).fetchall()
            now = _utcnow()
            for row in rows:
                peaks = self._live_peak_rows(row["run_id"])
                self._conn.execute(
                    "INSERT INTO run_revisions "
                    "(run_id, revision, peaks, backfilled, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (
                        row["run_id"],
                        row["revision"],
                        json.dumps(peaks),
                        now,
                    ),
                )

    def _live_peak_rows(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT peak_id, mz FROM peaks WHERE run_id = ? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
        return [{"peak_id": r["peak_id"], "mz": r["mz"]} for r in rows]

    def _insert_run_snapshot(
        self,
        run_id: str,
        revision: int,
        peaks: list[dict],
        now: str,
        *,
        backfilled: bool,
    ) -> None:
        self._conn.execute(
            "INSERT INTO run_revisions "
            "(run_id, revision, peaks, backfilled, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                revision,
                json.dumps(_snapshot_peak_rows(peaks)),
                1 if backfilled else 0,
                now,
            ),
        )

    def _ensure_current_snapshot(self, run_id: str, revision: int, now: str) -> None:
        """Heal a legacy run in-place: if its current revision has no
        snapshot yet, back-fill it from the live peak rows before the run is
        corrected, so the correction itself becomes traceable."""
        exists = self._conn.execute(
            "SELECT 1 FROM run_revisions WHERE run_id = ? AND revision = ?",
            (run_id, revision),
        ).fetchone()
        if exists is None:
            self._insert_run_snapshot(
                run_id, revision, self._live_peak_rows(run_id), now, backfilled=True
            )

    # ------------------------------------------------------------------ methods
    def create_method(
        self, method_id: str, allowed_charges: list[int], error: Decimal
    ) -> dict:
        now = _utcnow()
        with self._transaction():
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
        with self._transaction():
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
        return self.get_method(method_id)

    # -------------------------------------------------------------------- runs
    def create_run(self, run_id: str, method_id: str, peaks: list[dict]) -> dict:
        now = _utcnow()
        with self._transaction():
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
            # Revision 1 snapshot commits atomically with the run itself.
            self._insert_run_snapshot(run_id, 1, peaks, now, backfilled=False)
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
        """Correct a run's peak table and snapshot the new revision atomically.

        The revision check and bump are a single compare-and-swap
        (``WHERE revision = expected``), so of concurrent corrections based on
        the same revision exactly one commits; the loser gets
        :class:`RevisionConflict` and, thanks to the surrounding transaction,
        leaves neither a changed peak table nor a gap in the snapshot chain.
        """
        with self._transaction():
            row = self._conn.execute(
                "SELECT revision FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"run {run_id!r} does not exist")
            current = row["revision"]
            if current != expected_revision:
                raise RevisionConflict("run", run_id, expected_revision, current)
            now = _utcnow()
            # Legacy runs may lack a snapshot for the revision they are
            # sitting on; back-fill it from the live rows first so the
            # transition about to be committed is fully traceable.
            self._ensure_current_snapshot(run_id, current, now)
            bumped = self._conn.execute(
                "UPDATE runs SET revision = revision + 1, updated_at = ? "
                "WHERE run_id = ? AND revision = ?",
                (now, run_id, expected_revision),
            )
            if bumped.rowcount != 1:
                # Unreachable while all writes serialise on self._lock; kept
                # as a guard so a stolen race still fails cleanly.
                raise RevisionConflict("run", run_id, expected_revision, current)
            self._conn.execute("DELETE FROM peaks WHERE run_id = ?", (run_id,))
            self._insert_peaks(run_id, peaks)
            self._insert_run_snapshot(
                run_id, current + 1, peaks, now, backfilled=False
            )
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
        with self._transaction():
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

    # -------------------------------------------------------- change chains
    def get_peak_table_chain(self, analysis_id: str) -> dict:
        """Per-revision change chain of a run's peak table since an analysis.

        The chain starts from the peak table sealed into the analysis and
        walks every committed run revision up to the current one.  Each
        traceable step lists added / removed / m/z-changed peaks by peak ID
        with before/after values; revisions whose snapshots were never
        recorded (pre-snapshot legacy runs) are reported as untraceable gaps,
        never reconstructed.  The whole read happens under the write lock, so
        the chain and ``current_revision`` always describe one committed
        state even if a correction lands concurrently.
        """
        with self._lock:
            analysis_row = self._conn.execute(
                "SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if analysis_row is None:
                raise NotFound(f"analysis {analysis_id!r} does not exist")
            run_id = analysis_row["run_id"]
            sealed_revision = analysis_row["run_revision"]

            run_row = self._conn.execute(
                "SELECT revision FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise NotFound(f"run {run_id!r} does not exist")
            current_revision = run_row["revision"]

            # Anchor: the peak table sealed into the analysis result itself.
            sealed_peaks: Optional[list[dict]] = None
            try:
                result = json.loads(analysis_row["result"])
                sealed_peaks = result["basis"]["run"]["peaks"]
            except (KeyError, TypeError, json.JSONDecodeError):
                sealed_peaks = None

            revision_rows = self._conn.execute(
                "SELECT revision, peaks, backfilled, created_at "
                "FROM run_revisions "
                "WHERE run_id = ? AND revision BETWEEN ? AND ? "
                "ORDER BY revision",
                (run_id, sealed_revision, current_revision),
            ).fetchall()
            current_peaks = self._live_peak_rows(run_id)

        snapshots: dict[int, list[dict]] = {}
        committed_at: dict[int, str] = {}
        for row in revision_rows:
            snapshots[row["revision"]] = json.loads(row["peaks"])
            committed_at[row["revision"]] = row["created_at"]

        if sealed_peaks is None:
            # Very old analyses without an embedded basis fall back to the
            # stored snapshot of the sealed revision, if one survives.
            sealed_peaks = snapshots.get(sealed_revision)
        if sealed_peaks is not None:
            snapshots[sealed_revision] = sealed_peaks

        chain: list[dict] = []
        revision = sealed_revision
        while revision < current_revision:
            before = snapshots.get(revision)
            after = snapshots.get(revision + 1)
            if before is not None and after is not None:
                entry = {
                    "from_revision": revision,
                    "to_revision": revision + 1,
                    "traceable": True,
                    "committed_at": committed_at.get(revision + 1),
                }
                entry.update(diff_peak_lists(before, after))
                chain.append(entry)
                revision += 1
            else:
                # Merge every consecutive untraceable transition into one
                # explicit gap; the missing intermediate states are unknown
                # and must not be interpolated.
                gap_start = revision
                missing: list[int] = []
                while revision < current_revision and (
                    snapshots.get(revision) is None
                    or snapshots.get(revision + 1) is None
                ):
                    if snapshots.get(revision + 1) is None:
                        missing.append(revision + 1)
                    revision += 1
                chain.append(
                    {
                        "from_revision": gap_start,
                        "to_revision": revision,
                        "traceable": False,
                        "missing_revisions": missing,
                        "detail": (
                            "peak-table snapshots for revision(s) "
                            + ", ".join(str(rev) for rev in missing)
                            + " were never recorded (pre-snapshot history); "
                            "intermediate changes cannot be reconstructed"
                        ),
                    }
                )

        return {
            "analysis_id": analysis_id,
            "run_id": run_id,
            "sealed_run_revision": sealed_revision,
            "sealed_peaks": sealed_peaks,
            "current_revision": current_revision,
            "current_peaks": current_peaks,
            "fully_traceable": all(entry["traceable"] for entry in chain),
            "chain": chain,
        }
