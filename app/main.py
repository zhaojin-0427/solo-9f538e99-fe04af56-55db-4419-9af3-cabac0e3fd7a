"""FastAPI application: method/run registration, revisions and de-isotoping analysis."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .db import Conflict, Database, NotFound, RevisionConflict, parse_decimal
from .grouping import Peak, solve_deisotoping
from .schemas import (
    AnalysisRequest,
    MethodCreate,
    MethodUpdate,
    RunCreate,
    RunUpdate,
)

def _default_db_path() -> str:
    configured = os.environ.get("DEISOTOPE_DB_PATH")
    if configured:
        return configured
    # Container layout (see Dockerfile): /data exists and is a volume.
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/deisotope.db"
    # Fallback for local runs without Docker / write access to /data.
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "deisotope.db")


DB_PATH = _default_db_path()

app = FastAPI(
    title="LC-MS De-isotoping API",
    description=(
        "Register methods (allowed charges 1-3, non-negative mass error) and "
        "runs (1-12 peaks), revise both under optimistic revisions, and submit "
        "analyses that compute the minimum-group isotope partition. Every "
        "committed run revision keeps an immutable peak-table snapshot; "
        "GET /analyses/{analysis_id}/peak-table-changes replays the per-revision "
        "change chain since an analysis for QC recompute decisions."
    ),
    version="1.1.0",
)
db = Database(DB_PATH)


@app.exception_handler(NotFound)
async def not_found_handler(_: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(RevisionConflict)
async def revision_conflict_handler(_: Request, exc: RevisionConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "conflict": "revision",
            "resource": exc.resource,
            "resource_id": exc.resource_id,
            "seen_revision": exc.seen,
            "current_revision": exc.current,
        },
    )


@app.exception_handler(Conflict)
async def conflict_handler(_: Request, exc: Conflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.post("/methods", status_code=201, tags=["methods"])
async def create_method(payload: MethodCreate) -> dict:
    return db.create_method(
        payload.method_id, payload.allowed_charges, payload.error
    )


@app.get("/methods/{method_id}", tags=["methods"])
async def get_method(method_id: str) -> dict:
    return db.get_method(method_id)


@app.put("/methods/{method_id}", tags=["methods"])
async def update_method(method_id: str, payload: MethodUpdate) -> dict:
    return db.update_method(
        method_id,
        payload.allowed_charges,
        payload.error,
        payload.expected_revision,
    )


@app.post("/runs", status_code=201, tags=["runs"])
async def create_run(payload: RunCreate) -> dict:
    peaks = [{"peak_id": p.peak_id, "mz": p.mz} for p in payload.peaks]
    return db.create_run(payload.run_id, payload.method_id, peaks)


@app.get("/runs/{run_id}", tags=["runs"])
async def get_run(run_id: str) -> dict:
    return db.get_run(run_id)


@app.put("/runs/{run_id}", tags=["runs"])
async def update_run(run_id: str, payload: RunUpdate) -> dict:
    peaks = [{"peak_id": p.peak_id, "mz": p.mz} for p in payload.peaks]
    return db.update_run(run_id, peaks, payload.expected_revision)


def _run_snapshot_payload(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "revision": run["revision"],
        "peaks": [{"peak_id": p["peak_id"], "mz": p["mz"]} for p in run["peaks"]],
    }


def _method_snapshot_payload(method: dict) -> dict:
    return {
        "method_id": method["method_id"],
        "revision": method["revision"],
        "allowed_charges": method["allowed_charges"],
        "error": method["error"],
    }


@app.post("/analyses", status_code=201, tags=["analyses"])
async def create_analysis(payload: AnalysisRequest) -> dict:
    """Run de-isotoping against the exact method/run revisions supplied.

    Stale revisions produce 409 without computing anything.  The stored and
    returned result always carries both revisions plus the full snapshots it
    was derived from, so every result is traceable to one committed state.
    """
    method = db.get_method(payload.method_id)
    if method["revision"] != payload.expected_method_revision:
        raise RevisionConflict(
            "method",
            payload.method_id,
            payload.expected_method_revision,
            method["revision"],
        )

    run = db.get_run(payload.run_id)
    if run["revision"] != payload.expected_run_revision:
        raise RevisionConflict(
            "run", payload.run_id, payload.expected_run_revision, run["revision"]
        )

    if run["method_id"] != payload.method_id:
        raise Conflict(
            f"run {payload.run_id!r} is registered under method "
            f"{run['method_id']!r}, not {payload.method_id!r}"
        )

    error = parse_decimal(method["error"], field="error")
    peaks = [
        Peak(peak_id=p["peak_id"], mz=parse_decimal(p["mz"], field="mz", positive=True))
        for p in run["peaks"]
    ]
    result: dict[str, Any] = solve_deisotoping(
        peaks, method["allowed_charges"], error
    )

    method_snapshot = _method_snapshot_payload(method)
    run_snapshot = _run_snapshot_payload(run)
    result["basis"] = {
        "method": method_snapshot,
        "run": run_snapshot,
        "decimal_arithmetic": True,
        "neutron_mass_spacing": "1.003355 / charge",
    }

    analysis_id = db.save_analysis(
        run_id=payload.run_id,
        method_id=payload.method_id,
        method_revision=method["revision"],
        run_revision=run["revision"],
        result=result,
    )

    return {
        "analysis_id": analysis_id,
        "run_id": payload.run_id,
        "method_id": payload.method_id,
        "method_revision": method["revision"],
        "run_revision": run["revision"],
        "method": method_snapshot,
        "run": run_snapshot,
        "result": result,
    }


@app.get("/analyses/{analysis_id}", tags=["analyses"])
async def get_analysis(analysis_id: str) -> dict:
    stored = db.get_analysis(analysis_id)
    if stored is None:
        raise NotFound(f"analysis {analysis_id!r} does not exist")
    return stored


@app.get("/analyses/{analysis_id}/peak-table-changes", tags=["analyses"])
async def get_peak_table_changes(analysis_id: str) -> dict:
    """Change chain of the analysed run's peak table since the analysis.

    Starts from the peak table sealed into the analysis and lists, for every
    committed run revision since, the peaks added, removed and re-m/z'd (by
    peak ID, with before/after values) — each revision separately, never
    collapsed into a net difference.  ``current_revision``/``current_peaks``
    are read in the same committed state as the chain itself.  Revision
    ranges whose snapshots were never recorded (runs that pre-date snapshot
    persistence) are returned as explicit untraceable gaps.
    """
    return db.get_peak_table_chain(analysis_id)
