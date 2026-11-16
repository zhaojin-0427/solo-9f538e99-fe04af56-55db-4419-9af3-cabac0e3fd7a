"""Tests for the per-analysis run peak-table change chain.

Covers: immutable revision snapshots written atomically with run creation and
every correction, per-revision diffs that never collapse into a net
difference, optimistic-concurrency losers leaving no trace, consistent reads
under interleaved queries/corrections, and untraceable-gap reporting for
legacy (pre-snapshot) runs.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import tempfile
import threading
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="deisotope-history-")
    monkeypatch.setenv("DEISOTOPE_DB_PATH", os.path.join(tmpdir, "test.db"))
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client
    main.db.close()


def _method(client, method_id="M1", error="0.002"):
    resp = client.post(
        "/methods",
        json={"method_id": method_id, "allowed_charges": [1, 2, 3], "error": error},
    )
    assert resp.status_code == 201


def _run(client, run_id="R1", peaks=None, method_id="M1"):
    if peaks is None:
        peaks = [
            {"peak_id": "p1", "mz": "100.000000"},
            {"peak_id": "p2", "mz": "101.003355"},
        ]
    resp = client.post(
        "/runs", json={"run_id": run_id, "method_id": method_id, "peaks": peaks}
    )
    assert resp.status_code == 201
    return resp.json()


def _analyse(client, run_id="R1", method_id="M1", method_rev=1, run_rev=1):
    resp = client.post(
        "/analyses",
        json={
            "run_id": run_id,
            "method_id": method_id,
            "expected_method_revision": method_rev,
            "expected_run_revision": run_rev,
        },
    )
    assert resp.status_code == 201
    return resp.json()["analysis_id"]


def _update_run(client, run_id, expected_revision, peaks):
    return client.put(
        f"/runs/{run_id}",
        json={"expected_revision": expected_revision, "peaks": peaks},
    )


def _chain(client, analysis_id):
    resp = client.get(f"/analyses/{analysis_id}/peak-table-changes")
    assert resp.status_code == 200
    return resp.json()


def test_chain_unknown_analysis_is_404(client):
    assert client.get("/analyses/nope/peak-table-changes").status_code == 404


def test_chain_empty_when_run_unchanged_since_analysis(client):
    _method(client)
    _run(client)
    analysis_id = _analyse(client)

    body = _chain(client, analysis_id)
    assert body["analysis_id"] == analysis_id
    assert body["run_id"] == "R1"
    assert body["sealed_run_revision"] == 1
    assert body["current_revision"] == 1
    assert body["chain"] == []
    assert body["fully_traceable"] is True
    # sealed and current tables agree and carry canonical m/z text
    assert body["sealed_peaks"] == [
        {"peak_id": "p1", "mz": "100"},
        {"peak_id": "p2", "mz": "101.003355"},
    ]
    assert body["current_peaks"] == body["sealed_peaks"]


def test_chain_lists_add_remove_and_mz_change_per_revision(client):
    _method(client)
    _run(client)
    analysis_id = _analyse(client)

    # revision 2: change p1 m/z, drop p2, add p3
    resp = _update_run(
        client,
        "R1",
        1,
        [
            {"peak_id": "p1", "mz": "100.000400"},
            {"peak_id": "p3", "mz": "150.5"},
        ],
    )
    assert resp.status_code == 200

    body = _chain(client, analysis_id)
    assert body["current_revision"] == 2
    assert body["fully_traceable"] is True
    assert len(body["chain"]) == 1
    entry = body["chain"][0]
    assert entry["from_revision"] == 1
    assert entry["to_revision"] == 2
    assert entry["traceable"] is True
    assert entry["committed_at"]
    assert entry["added"] == [{"peak_id": "p3", "mz": "150.5"}]
    assert entry["removed"] == [{"peak_id": "p2", "mz": "101.003355"}]
    assert entry["mz_changed"] == [
        {"peak_id": "p1", "before": "100", "after": "100.0004"}
    ]


def test_chain_never_collapses_multiple_changes_into_net_difference(client):
    _method(client)
    _run(client)
    analysis_id = _analyse(client)

    # rev 2: p1 100 -> 101 ; rev 3: p1 101 -> 100 (back to the start value)
    assert _update_run(
        client, "R1", 1, [
            {"peak_id": "p1", "mz": "101"},
            {"peak_id": "p2", "mz": "101.003355"},
        ]
    ).status_code == 200
    assert _update_run(
        client, "R1", 2, [
            {"peak_id": "p1", "mz": "100"},
            {"peak_id": "p2", "mz": "101.003355"},
        ]
    ).status_code == 200

    body = _chain(client, analysis_id)
    assert body["current_revision"] == 3
    # net difference sealed-vs-current is zero, yet both corrections appear
    assert [ (e["from_revision"], e["to_revision"]) for e in body["chain"] ] == [
        (1, 2),
        (2, 3),
    ]
    first, second = body["chain"]
    assert first["mz_changed"] == [
        {"peak_id": "p1", "before": "100", "after": "101"}
    ]
    assert second["mz_changed"] == [
        {"peak_id": "p1", "before": "101", "after": "100"}
    ]
    assert first["added"] == first["removed"] == []
    assert second["added"] == second["removed"] == []


def test_chain_and_current_revision_come_from_one_committed_state(client):
    _method(client)
    _run(client)
    analysis_id = _analyse(client)
    for rev in (1, 2, 3):
        assert _update_run(
            client,
            "R1",
            rev,
            [{"peak_id": "p1", "mz": f"10{rev}.1"}],
        ).status_code == 200

    body = _chain(client, analysis_id)
    run = client.get("/runs/R1").json()
    # the chain end, the advertised current revision and the live run agree
    assert body["current_revision"] == run["revision"] == 4
    assert body["chain"][-1]["to_revision"] == 4
    assert body["current_peaks"] == [
        {"peak_id": p["peak_id"], "mz": p["mz"]} for p in run["peaks"]
    ]
    # contiguous, gap-free coverage from the sealed revision
    assert body["chain"][0]["from_revision"] == 1
    for left, right in zip(body["chain"], body["chain"][1:]):
        assert left["to_revision"] == right["from_revision"]


def test_concurrent_corrections_only_one_commits_and_loser_leaves_no_trace(
    client, monkeypatch
):
    _method(client)
    _run(client)
    analysis_id = _analyse(client)

    import app.main as main

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []

    def correct(tag: str, mz: str) -> None:
        barrier.wait()
        try:
            result = main.db.update_run(
                "R1",
                [{"peak_id": "p1", "mz": Decimal(mz)}],
                expected_revision=1,
            )
            outcomes.append((tag, result["revision"]))
        except main.RevisionConflict as exc:
            outcomes.append((tag, exc))

    threads = [
        threading.Thread(target=correct, args=("a", "100.1")),
        threading.Thread(target=correct, args=("b", "100.2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # exactly one winner; the loser saw a revision conflict
    winners = [rev for _, rev in outcomes if isinstance(rev, int)]
    losers = [exc for _, exc in outcomes if isinstance(exc, main.RevisionConflict)]
    assert len(winners) == 1 and winners[0] == 2
    assert len(losers) == 1 and losers[0].seen == 1 and losers[0].current == 2

    # no broken chain, current table is exactly the winner's
    body = _chain(client, analysis_id)
    assert body["current_revision"] == 2
    assert len(body["chain"]) == 1
    entry = body["chain"][0]
    assert entry["traceable"] is True
    assert len(entry["mz_changed"]) == 1
    assert entry["mz_changed"][0]["before"] == "100"
    assert entry["mz_changed"][0]["after"] in ("100.1", "100.2")
    assert body["current_peaks"] == [
        {"peak_id": "p1", "mz": entry["mz_changed"][0]["after"]}
    ]


def test_interleaved_queries_and_corrections_stay_consistent(client):
    """Chain responses observed while corrections land must each describe one
    committed state: contiguous from the sealed revision and ending exactly at
    the advertised current revision."""
    _method(client)
    _run(client)
    analysis_id = _analyse(client)

    errors: list[object] = []

    def writer(tag: int) -> None:
        for i in range(15):
            revision = client.get("/runs/R1").json()["revision"]
            resp = _update_run(
                client, "R1", revision, [{"peak_id": "p1", "mz": f"9{tag}{i}.5"}]
            )
            if resp.status_code not in (200, 409):
                errors.append(("writer", resp.status_code))

    def reader() -> None:
        for _ in range(40):
            body = _chain(client, analysis_id)
            chain = body["chain"]
            if not chain:
                consistent = body["current_revision"] == body["sealed_run_revision"]
            else:
                consistent = (
                    chain[0]["from_revision"] == body["sealed_run_revision"]
                    and chain[-1]["to_revision"] == body["current_revision"]
                    and all(
                        left["to_revision"] == right["from_revision"]
                        for left, right in zip(chain, chain[1:])
                    )
                    and all(entry["traceable"] for entry in chain)
                )
            if not consistent:
                errors.append(("inconsistent-chain", body))

    threads = [
        threading.Thread(target=writer, args=(n,)) for n in range(3)
    ] + [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # final chain covers every committed revision exactly once
    final = _chain(client, analysis_id)
    final_revision = client.get("/runs/R1").json()["revision"]
    assert final["current_revision"] == final_revision
    assert [e["to_revision"] for e in final["chain"]] == list(
        range(2, final_revision + 1)
    )


def test_failed_correction_rolls_back_peak_table_and_snapshot(client):
    _method(client)
    _run(client)
    analysis_id = _analyse(client)
    assert _update_run(
        client, "R1", 1, [{"peak_id": "p1", "mz": "100.9"}]
    ).status_code == 200

    import app.main as main

    before_run = client.get("/runs/R1").json()
    before_chain = _chain(client, analysis_id)

    # duplicate peak_id blows up mid-transaction (after the revision bump and
    # the peak-table delete): everything must roll back
    with pytest.raises(main.Conflict):
        main.db.update_run(
            "R1",
            [
                {"peak_id": "dup", "mz": Decimal("1")},
                {"peak_id": "dup", "mz": Decimal("2")},
            ],
            expected_revision=2,
        )

    assert client.get("/runs/R1").json() == before_run
    assert _chain(client, analysis_id) == before_chain
    # and the run is still correctable afterwards (no poisoned state)
    assert _update_run(
        client, "R1", 2, [{"peak_id": "p1", "mz": "100.8"}]
    ).status_code == 200


def test_run_creation_persists_revision_one_snapshot(client):
    _method(client)
    _run(client)

    import app.main as main

    with main.db._lock:
        row = main.db._conn.execute(
            "SELECT peaks, backfilled FROM run_revisions WHERE run_id='R1' AND revision=1"
        ).fetchone()
    assert row is not None
    assert row["backfilled"] == 0
    assert json.loads(row["peaks"]) == [
        {"peak_id": "p1", "mz": "100"},
        {"peak_id": "p2", "mz": "101.003355"},
    ]


def test_run_revision_snapshots_are_immutable(client):
    _method(client)
    _run(client)

    import app.main as main

    with pytest.raises(sqlite3.IntegrityError):
        with main.db._transaction():
            main.db._conn.execute(
                "UPDATE run_revisions SET peaks='[]' WHERE run_id='R1'"
            )
    with pytest.raises(sqlite3.IntegrityError):
        with main.db._transaction():
            main.db._conn.execute(
                "DELETE FROM run_revisions WHERE run_id='R1'"
            )
    # snapshot content untouched
    with main.db._lock:
        peaks = main.db._conn.execute(
            "SELECT peaks FROM run_revisions WHERE run_id='R1' AND revision=1"
        ).fetchone()["peaks"]
    assert json.loads(peaks)[0]["peak_id"] == "p1"


# --------------------------------------------------------------------- legacy


def _build_legacy_db(path: str) -> None:
    """Create a pre-snapshot database: schema and rows as the old code left
    them (no run_revisions table, run already corrected twice, analysis that
    sealed revision 1)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE methods (
            method_id TEXT PRIMARY KEY, allowed_charges TEXT NOT NULL,
            error TEXT NOT NULL, revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, method_id TEXT NOT NULL,
            revision INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE peaks (
            run_id TEXT NOT NULL, peak_id TEXT NOT NULL, mz TEXT NOT NULL,
            ordinal INTEGER NOT NULL, PRIMARY KEY (run_id, peak_id)
        );
        CREATE TABLE analyses (
            analysis_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
            method_id TEXT NOT NULL, method_revision INTEGER NOT NULL,
            run_revision INTEGER NOT NULL, result TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO methods VALUES ('M1', '[1, 2, 3]', '0.002', 1, ?, ?)", (now, now)
    )
    # run sits at revision 3; only the *current* peak table survives
    conn.execute("INSERT INTO runs VALUES ('R1', 'M1', 3, ?, ?)", (now, now))
    conn.executemany(
        "INSERT INTO peaks VALUES ('R1', ?, ?, ?)",
        [("p1", "100.0004", 0), ("p9", "150.5", 1)],
    )
    sealed_result = {
        "group_count": 1,
        "basis": {
            "run": {
                "run_id": "R1",
                "revision": 1,
                "peaks": [
                    {"peak_id": "p1", "mz": "100"},
                    {"peak_id": "p2", "mz": "101.003355"},
                ],
            },
            "method": {
                "method_id": "M1",
                "revision": 1,
                "allowed_charges": [1, 2, 3],
                "error": "0.002",
            },
        },
    }
    conn.execute(
        "INSERT INTO analyses VALUES ('A1', 'R1', 'M1', 1, 1, ?, ?)",
        (json.dumps(sealed_result), now),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def legacy_client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="deisotope-legacy-")
    db_path = os.path.join(tmpdir, "legacy.db")
    _build_legacy_db(db_path)
    monkeypatch.setenv("DEISOTOPE_DB_PATH", db_path)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client
    main.db.close()


def test_legacy_run_marks_untraceable_gap_without_fabricating(legacy_client):
    body = _chain(legacy_client, "A1")
    assert body["sealed_run_revision"] == 1
    assert body["current_revision"] == 3
    assert body["fully_traceable"] is False
    # one explicit gap, no invented per-revision changes
    assert len(body["chain"]) == 1
    gap = body["chain"][0]
    assert gap["traceable"] is False
    assert gap["from_revision"] == 1
    assert gap["to_revision"] == 3
    assert gap["missing_revisions"] == [2]
    assert "added" not in gap and "removed" not in gap and "mz_changed" not in gap
    # sealed table still comes from the analysis itself
    assert body["sealed_peaks"] == [
        {"peak_id": "p1", "mz": "100"},
        {"peak_id": "p2", "mz": "101.003355"},
    ]
    # current table is the live one
    assert body["current_peaks"] == [
        {"peak_id": "p1", "mz": "100.0004"},
        {"peak_id": "p9", "mz": "150.5"},
    ]


def test_legacy_run_correction_becomes_traceable_after_healing(legacy_client):
    # correct the legacy run once: the new transition must be traceable even
    # though the older history stays a marked gap
    resp = _update_run(
        legacy_client,
        "R1",
        3,
        [
            {"peak_id": "p1", "mz": "100.0004"},
            {"peak_id": "p9", "mz": "150.5001"},
        ],
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 4

    body = _chain(legacy_client, "A1")
    assert body["current_revision"] == 4
    assert body["fully_traceable"] is False
    assert len(body["chain"]) == 2

    gap, healed = body["chain"]
    assert gap["traceable"] is False
    assert (gap["from_revision"], gap["to_revision"]) == (1, 3)
    assert healed["traceable"] is True
    assert (healed["from_revision"], healed["to_revision"]) == (3, 4)
    assert healed["mz_changed"] == [
        {"peak_id": "p9", "before": "150.5", "after": "150.5001"}
    ]
    assert healed["added"] == healed["removed"] == []


def test_legacy_analysis_and_existing_endpoints_keep_working(legacy_client):
    # old analysis retrievable through the unchanged endpoint
    stored = legacy_client.get("/analyses/A1")
    assert stored.status_code == 200
    assert stored.json()["run_revision"] == 1

    # existing run/method endpoints see the legacy rows
    assert legacy_client.get("/runs/R1").json()["revision"] == 3
    assert legacy_client.get("/methods/M1").json()["error"] == "0.002"

    # new analyses can still be submitted against the legacy run
    resp = legacy_client.post(
        "/analyses",
        json={
            "run_id": "R1",
            "method_id": "M1",
            "expected_method_revision": 1,
            "expected_run_revision": 3,
        },
    )
    assert resp.status_code == 201
    new_id = resp.json()["analysis_id"]
    # and the new analysis starts a fully traceable chain from its own state
    body = _chain(legacy_client, new_id)
    assert body["sealed_run_revision"] == 3
    assert body["current_revision"] == 3
    assert body["chain"] == []
    assert body["fully_traceable"] is True


def test_migration_backfills_only_current_revision(legacy_client):
    import app.main as main

    with main.db._lock:
        rows = main.db._conn.execute(
            "SELECT revision, backfilled FROM run_revisions WHERE run_id='R1' "
            "ORDER BY revision"
        ).fetchall()
    # exactly one backfilled row, for the current revision 3; the missing
    # intermediate revisions 1-2 are NOT invented
    assert [(r["revision"], r["backfilled"]) for r in rows] == [(3, 1)]
