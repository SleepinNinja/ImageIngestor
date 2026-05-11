"""Job control endpoints: start, status, stop.

All three endpoints share the same JobState singleton stored on app.state.
The worker runs as a fire-and-forget asyncio.Task — there is no Celery or
Redis involved.  See README for production scaling notes.
"""

import asyncio
import logging

import asyncpg  # type: ignore[import]
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.worker import JobState, run_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/job", tags=["job"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class JobStatusResponse(BaseModel):
    """Snapshot of the current worker state returned by GET /job/status."""

    running: bool
    stop_requested: bool
    processed: int
    failed: int
    total: int


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_job_state(request: Request) -> JobState:
    """Return the JobState singleton attached to app.state at startup."""
    return request.app.state.job_state


def _get_pool(request: Request) -> asyncpg.Pool:
    """Return the asyncpg pool attached to app.state at startup."""
    return request.app.state.pool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/start", response_model=JobStatusResponse, status_code=202)
async def start_job(request: Request) -> JobStatusResponse:
    """Start the background photo-fetch worker.

    Returns 409 if the worker is already running.
    The worker runs as a fire-and-forget asyncio Task on the event loop;
    this endpoint returns immediately while the worker runs in the background.
    """
    job_state = _get_job_state(request)
    pool = _get_pool(request)

    if job_state.running:
        raise HTTPException(status_code=409, detail="Worker is already running")

    # Cancel any finished/cancelled previous task before replacing it
    if job_state.task is not None and not job_state.task.done():
        job_state.task.cancel()

    # Schedule the worker coroutine as a background task on the running loop
    job_state.task = asyncio.create_task(run_worker(pool, job_state))

    logger.info("job started")
    return JobStatusResponse(
        running=job_state.running,
        stop_requested=job_state.stop_requested,
        processed=job_state.processed,
        failed=job_state.failed,
        total=job_state.total,
    )


@router.get("/status", response_model=JobStatusResponse)
async def get_job_status(request: Request) -> JobStatusResponse:
    """Return the current worker state.

    This is a simple in-memory read — no DB query is performed.
    Poll this endpoint from the frontend to track progress.
    """
    job_state = _get_job_state(request)
    return JobStatusResponse(
        running=job_state.running,
        stop_requested=job_state.stop_requested,
        processed=job_state.processed,
        failed=job_state.failed,
        total=job_state.total,
    )


@router.post("/stop", response_model=JobStatusResponse)
async def stop_job(request: Request) -> JobStatusResponse:
    """Request a graceful stop of the running worker.

    Sets the stop_requested flag; the worker will finish its current batch
    and then exit cleanly.  Returns 409 if no worker is running.

    Note: this does NOT cancel the asyncio task immediately — it is a
    cooperative signal.  The current batch will always complete before the
    worker stops, so the database is never left in a partially-written state.
    """
    job_state = _get_job_state(request)

    if not job_state.running:
        raise HTTPException(status_code=409, detail="No worker is currently running")

    # Cooperative stop: worker reads this flag after each batch finishes
    job_state.stop_requested = True

    logger.info("graceful stop requested")
    return JobStatusResponse(
        running=job_state.running,
        stop_requested=job_state.stop_requested,
        processed=job_state.processed,
        failed=job_state.failed,
        total=job_state.total,
    )
