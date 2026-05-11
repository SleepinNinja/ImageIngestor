"""Background worker: claims rows with SKIP LOCKED and fetches photo metadata.

Architecture notes
------------------
* SELECT ... FOR UPDATE SKIP LOCKED atomically claims a batch of rows so a future
  second worker process cannot double-process them.  The lock is held for the
  entire duration of the API calls and released only when the UPDATE commits.
  This is intentional: it is the cheapest correct approach without a separate
  queue service.

* asyncio.Semaphore(MAX_CONCURRENCY) caps outbound HTTP fan-out so we never
  exceed the Google API per-second quota in a burst.

* The entire claim → fetch → update cycle runs inside one database transaction.
  The connection is held while API calls are in flight, which is acceptable for
  a single-worker setup with small BATCH_SIZE values.

* Exponential backoff on HTTP 429 / 5xx: 1 s → 2 s → 4 s (3 attempts max).
  After exhausting retries we store {"photos": []} so the row is never retried
  again and the worker does not loop forever on a broken place ID.

* Graceful shutdown: the stop_requested flag is checked between batches, not
  mid-batch, so we always finish writing the current batch before stopping.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import asyncpg  # type: ignore

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class JobState:
    """In-memory snapshot of the running worker's state.

    Intentionally simple — see README for production notes on persistence.
    """

    running: bool = False
    stop_requested: bool = False
    processed: int = 0
    failed: int = 0
    # Total unprocessed rows at the moment the job was started
    total: int = 0
    # Reference to the asyncio Task so callers can await it
    task: asyncio.Task | None = field(default=None, repr=False)


async def fetch_photos_for_place(
    session: aiohttp.ClientSession,
    place_id: str,
    api_key: str,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """Call the Google Places API (New) and return the photos array.

    Args:
        session: Shared aiohttp session for connection reuse.
        place_id: Google Place ID (e.g. "ChIJN1t_tDeuEmsRUsoyG83frY4").
        api_key: Google Places API key.
        max_retries: How many times to retry on transient errors before giving up.

    Returns:
        List of Photo objects as returned by the API, or [] on permanent failure.

    Notes:
        * Retries with exponential backoff (1 s, 2 s, 4 s) on HTTP 429 and 5xx.
        * A 404 or other 4xx (except 429) is treated as permanent — returns []
          immediately so the row is not retried.
        * The full Photo object (name, widthPx, heightPx, authorAttributions,
          flagContentUri, googleMapsUri) is returned, not just the name field.
    """
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    params = {"fields": "photos", "key": api_key}

    for attempt in range(max_retries):
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # "photos" key may be absent if the place has no photos
                    return data.get("photos", [])

                # Transient errors — back off and retry
                if resp.status in (429, 500, 502, 503, 504):
                    wait = 2**attempt  # 1 s, 2 s, 4 s
                    logger.warning(
                        "transient HTTP %s for place_id=%s attempt=%d/%d sleeping=%ds",
                        resp.status,
                        place_id,
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                # Permanent error (404, 400, …) — stop immediately
                logger.error(
                    "permanent HTTP %s for place_id=%s — storing empty photos",
                    resp.status,
                    place_id,
                )
                return []

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            wait = 2**attempt
            logger.warning(
                "network error for place_id=%s attempt=%d/%d sleeping=%ds: %s",
                place_id,
                attempt + 1,
                max_retries,
                wait,
                exc,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(wait)

    logger.error(
        "max retries exhausted for place_id=%s — storing empty photos", place_id
    )
    return []


async def process_batch(
    conn: asyncpg.Connection,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    rows: list[asyncpg.Record],
) -> tuple[int, int]:
    """Fetch photos for every row in the batch concurrently, then bulk-update.

    Args:
        conn: Database connection with an open transaction (rows are locked).
        session: Shared aiohttp session.
        semaphore: Limits concurrent API fan-out to MAX_CONCURRENCY.
        rows: Records with (id, place_id) from the SKIP LOCKED claim.

    Returns:
        Tuple of (processed_count, failed_count).
        "Failed" means the API returned no photos (error or genuinely empty place).

    Notes:
        * All API calls fan out concurrently; the semaphore caps parallelism.
        * Results are written in a single UPDATE … FROM unnest() statement —
          never row-by-row — to minimise round trips.
        * Even on failure we write {"photos": []} so the row exits the NULL
          state and is never claimed again.
    """

    async def _fetch_one(row: asyncpg.Record) -> tuple[int, list[dict]]:
        """Fetch one place under the shared semaphore."""
        async with semaphore:
            photos = await fetch_photos_for_place(
                session, row["place_id"], settings.GOOGLE_PLACES_API_KEY
            )
        return row["id"], photos

    # Fan out all API calls for this batch concurrently
    results: list[tuple[int, list[dict]]] = await asyncio.gather(
        *[_fetch_one(row) for row in rows]
    )

    ids = [r[0] for r in results]
    # Serialise full Photo objects; empty list encodes "no photos / error"
    photos_json_list = [json.dumps({"photos": r[1]}) for r in results]

    # Single UPDATE touching all rows in one statement — never row-by-row
    # unnest() expands the two arrays into a virtual table joined on id
    await conn.execute(
        """
        UPDATE places
        SET    photos = data.photos::jsonb
        FROM   unnest($1::bigint[], $2::text[]) AS data(id, photos)
        WHERE  places.id = data.id
        """,
        ids,
        photos_json_list,
    )

    failed = sum(1 for _, photos in results if not photos)
    return len(results), failed


async def run_worker(pool: asyncpg.Pool, job_state: JobState) -> None:
    """Main worker loop: claim → fetch → update until no rows remain or stop is requested.

    Args:
        pool: asyncpg connection pool shared with the FastAPI app.
        job_state: Shared mutable state updated in-place for status polling.

    Notes:
        * The stop_requested flag is checked between batches (after a full batch
          commits), never mid-batch — guaranteeing the DB is never left with
          partially-processed rows.
        * SKIP LOCKED ensures this is safe to run alongside a second worker
          process without a separate queue; each worker claims a disjoint set.
        * The connection used for claim + update is held open during API calls.
          For small BATCH_SIZE values this is an acceptable trade-off; see README
          for production scaling notes.
    """
    job_state.running = True
    job_state.stop_requested = False
    job_state.processed = 0
    job_state.failed = 0

    # Snapshot the queue depth at job start for progress reporting
    async with pool.acquire() as count_conn:
        job_state.total = await count_conn.fetchval(
            "SELECT COUNT(*) FROM places WHERE photos IS NULL"
        )

    logger.info("worker started: total_unprocessed=%d", job_state.total)

    # One shared semaphore caps outbound HTTP concurrency across the entire job
    semaphore = asyncio.Semaphore(settings.MAX_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        while not job_state.stop_requested:
            # Acquire one connection from the pool and hold it for the full batch
            # so the SKIP LOCKED row-level locks span both the SELECT and UPDATE.
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Claim up to BATCH_SIZE unprocessed rows atomically.
                    # SKIP LOCKED means rows already locked by another connection
                    # are simply skipped — no waiting, no deadlocks.
                    rows = await conn.fetch(
                        """
                        SELECT id, place_id
                        FROM   places
                        WHERE  photos IS NULL
                        FOR UPDATE SKIP LOCKED
                        LIMIT  $1
                        """,
                        settings.BATCH_SIZE,
                    )

                    if not rows:
                        # Queue is drained — nothing left to do
                        logger.info("worker: no more unprocessed rows — stopping")
                        job_state.running = False
                        return

                    processed, failed = await process_batch(
                        conn, session, semaphore, rows
                    )
                    # Transaction commits here, releasing the row-level locks

            job_state.processed += processed
            job_state.failed += failed

            logger.info(
                "batch complete: processed=%d failed=%d total_so_far=%d/%d",
                processed,
                failed,
                job_state.processed,
                job_state.total,
            )

    # Reached here only when stop_requested was set between batches
    logger.info(
        "worker stopped gracefully: processed=%d failed=%d",
        job_state.processed,
        job_state.failed,
    )
    job_state.running = False
