"""End-to-end HTTP tests covering registration, revisions and 409 conflicts."""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="deisotope-test-")
    monkeypatch.setenv("DEISOTOPE_DB_PATH", os.path.join(tmpdir, "test.db"))
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client
    main.db.close()


def _method_payload(error="0.002", charges=(1, 2, 3)):
    return {
        "method_id": "M1",
        "allowed_charges": list(charges),
        "error": error,
    }


def _run_payload(run_id="R1", peaks=None):
    if peaks is None:
        peaks = [
            {"peak_id": "p1", "mz": "100.000000"},
            {"peak_id": "p2", "mz": "101.003355"},
        ]
    return {"run_id": run_id, "method_id": "M1", "peaks": peaks}


def _analysis_payload(method_rev=1, run_rev=1, run_id="R1", method_id="M1"):
    return {
        "run_id": run_id,
        "method_id": method_id,
        "expected_method_revision": method_rev,
        "expected_run_revision": run_rev,
    }


def test_docs_and_openapi_are_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/analyses" in schema["paths"]
    assert client.get("/").status_code in (200, 307)


def test_method_crud_and_revision_lifecycle(client):
    resp = client.post("/methods", json=_method_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["revision"] == 1
    assert body["allowed_charges"] == [1, 2, 3]
    assert body["error"] == "0.002"

    # duplicate creation conflicts
    assert client.post("/methods", json=_method_payload()).status_code == 409

    # validation: charges outside 1..3 rejected
    bad = client.post(
        "/methods",
        json={"method_id": "M2", "allowed_charges": [4], "error": "0"},
    )
    assert bad.status_code == 422

    # negative error rejected
    bad2 = client.post(
        "/methods",
        json={"method_id": "M3", "allowed_charges": [1], "error": "-0.1"},
    )
    assert bad2.status_code == 422

    # float mz/error rejected: decimals must travel as strings
    bad3 = client.post(
        "/methods",
        json={"method_id": "M4", "allowed_charges": [1], "error": 0.002},
    )
    assert bad3.status_code == 422

    # correct revision updates
    ok = client.put(
        "/methods/M1",
        json={
            "expected_revision": 1,
            "allowed_charges": [1],
            "error": "0.001",
        },
    )
    assert ok.status_code == 200
    assert ok.json()["revision"] == 2
    assert ok.json()["error"] == "0.001"

    # stale revision -> 409 with both revisions
    stale = client.put(
        "/methods/M1",
        json={
            "expected_revision": 1,
            "allowed_charges": [1],
            "error": "0.001",
        },
    )
    assert stale.status_code == 409
    detail = stale.json()
    assert detail["conflict"] == "revision"
    assert detail["seen_revision"] == 1
    assert detail["current_revision"] == 2

    assert client.get("/methods/missing").status_code == 404


def test_run_crud_peak_validation_and_revision_conflicts(client):
    client.post("/methods", json=_method_payload())
    resp = client.post("/runs", json=_run_payload())
    assert resp.status_code == 201
    assert resp.json()["revision"] == 1
    assert [p["peak_id"] for p in resp.json()["peaks"]] == ["p1", "p2"]

    # non-positive m/z rejected
    bad = client.post(
        "/runs",
        json=_run_payload(run_id="R2", peaks=[{"peak_id": "x", "mz": "0"}]),
    )
    assert bad.status_code == 422

    # duplicate peak ids rejected
    bad2 = client.post(
        "/runs",
        json=_run_payload(
            run_id="R3",
            peaks=[
                {"peak_id": "x", "mz": "1"},
                {"peak_id": "x", "mz": "2"},
            ],
        ),
    )
    assert bad2.status_code == 422

    # 13 peaks rejected
    too_many = [{"peak_id": f"p{i}", "mz": str(100 + i)} for i in range(13)]
    assert (
        client.post("/runs", json=_run_payload("R4", too_many)).status_code == 422
    )

    # run under unknown method rejected
    orphan = client.post(
        "/runs",
        json={
            "run_id": "R5",
            "method_id": "nope",
            "peaks": [{"peak_id": "p1", "mz": "100"}],
        },
    )
    assert orphan.status_code == 404

    # first update with rev 1 succeeds, second one at rev 1 conflicts
    updated = client.put(
        "/runs/R1",
        json={
            "expected_revision": 1,
            "peaks": [
                {"peak_id": "p1", "mz": "100.000000"},
                {"peak_id": "p2", "mz": "101.003355"},
            ],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2

    again = client.put(
        "/runs/R1",
        json={
            "expected_revision": 1,
            "peaks": [{"peak_id": "p1", "mz": "100.000000"}],
        },
    )
    assert again.status_code == 409
    assert again.json()["current_revision"] == 2


def test_analysis_happy_path_and_stored_result(client):
    client.post("/methods", json=_method_payload(error="0.002"))
    client.post("/runs", json=_run_payload())

    resp = client.post("/analyses", json=_analysis_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["method_revision"] == 1
    assert body["run_revision"] == 1
    assert body["method"]["error"] == "0.002"
    # m/z text is normalised numerically (trailing zeros dropped); the value
    # is preserved exactly for decimal comparison.
    from decimal import Decimal

    assert Decimal(body["run"]["peaks"][0]["mz"]) == Decimal("100.000000")
    assert body["run"]["peaks"][1]["mz"] == "101.003355"

    result = body["result"]
    assert result["group_count"] == 1
    assert result["partition"] == [{"peak_ids": ["p1", "p2"], "charge": 1}]
    assert result["basis"]["decimal_arithmetic"] is True
    possibilities = {p["peak_id"]: p for p in result["peak_possibilities"]}
    assert possibilities["p1"]["certain"] is True

    # stored analysis retrievable and still references the same revisions
    fetched = client.get(f"/analyses/{body['analysis_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["method_revision"] == 1
    assert fetched.json()["result"]["partition"] == result["partition"]


def test_analysis_rejects_stale_method_and_run_revisions(client):
    client.post("/methods", json=_method_payload())
    client.post("/runs", json=_run_payload())

    # revise the method -> analysis pinned to revision 1 must conflict
    client.put(
        "/methods/M1",
        json={
            "expected_revision": 1,
            "allowed_charges": [1],
            "error": "0.001",
        },
    )
    conflict = client.post("/analyses", json=_analysis_payload(method_rev=1))
    assert conflict.status_code == 409
    assert conflict.json()["resource"] == "method"
    assert conflict.json()["current_revision"] == 2

    # now match the method but revise the run instead
    client.put(
        "/runs/R1",
        json={
            "expected_revision": 1,
            "peaks": [{"peak_id": "p1", "mz": "100.000000"}],
        },
    )
    conflict2 = client.post(
        "/analyses", json=_analysis_payload(method_rev=2, run_rev=1)
    )
    assert conflict2.status_code == 409
    assert conflict2.json()["resource"] == "run"
    assert conflict2.json()["current_revision"] == 2

    # matching both revisions succeeds and uses the new single-peak state
    ok = client.post("/analyses", json=_analysis_payload(method_rev=2, run_rev=2))
    assert ok.status_code == 201
    assert ok.json()["result"]["group_count"] == 1
    assert ok.json()["result"]["partition"][0]["charge"] is None


def test_analysis_rejects_run_method_mismatch(client):
    client.post("/methods", json=_method_payload())
    client.post(
        "/methods",
        json={"method_id": "M2", "allowed_charges": [1], "error": "0.001"},
    )
    client.post("/runs", json=_run_payload())
    resp = client.post(
        "/analyses",
        json=_analysis_payload(method_id="M2"),
    )
    assert resp.status_code == 409


def test_analysis_of_ambiguous_run_reports_possibilities(client):
    client.post(
        "/methods",
        json={"method_id": "M1", "allowed_charges": [1, 2], "error": "0"},
    )
    client.post(
        "/runs",
        json=_run_payload(
            peaks=[
                {"peak_id": "A", "mz": "100.000000"},
                {"peak_id": "B", "mz": "101.003355"},
                {"peak_id": "C", "mz": "101.5050325"},
            ]
        ),
    )
    body = client.post("/analyses", json=_analysis_payload()).json()
    result = body["result"]
    assert result["group_count"] == 2
    by_peak = {p["peak_id"]: p for p in result["peak_possibilities"]}
    assert by_peak["A"]["certain"] is False
    assert by_peak["A"]["possible_charges"] == [None, 1]
    assert by_peak["B"]["certain"] is False
    assert by_peak["B"]["possible_charges"] == [1, 2]
