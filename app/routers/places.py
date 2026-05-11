"""Places endpoints: upload, list, detail.

Upload
------
Accepts a multipart Excel file (.xlsx / .xls).  Looks for a column named
"place_id" (case-insensitive); falls back to the first column.  Duplicate
place IDs are silently ignored via ON CONFLICT DO NOTHING.

Batch insert uses an unnest() CTE so one round-trip handles the entire file.

List
----
Cursor-based pagination: WHERE id > :cursor ORDER BY id LIMIT :limit.
OFFSET is never used — it degrades to O(n) scans on large tables.

Detail
------
Single-row fetch by primary key.
"""

import io
import json
import logging
from typing import Any, Literal

import asyncpg
import openpyxl
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/places", tags=["places"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


def _compute_status(photos: Any) -> str:
    """Derive a human-readable status from the raw JSONB value.

    * None          → "pending"   (never processed)
    * {"photos":[]} → "failed"    (processed but no photos, or API error)
    * {"photos":[…]}→ "done"      (photos fetched successfully)
    """
    if photos is None:
        return "pending"
    if isinstance(photos, dict) and photos.get("photos"):
        return "done"
    return "failed"


class PlaceResponse(BaseModel):
    """Single place record returned by list and detail endpoints."""

    id: int
    place_id: str
    photos: dict | None
    status: str  # "pending" | "done" | "failed"


class PlacesListResponse(BaseModel):
    """Paginated list result.  next_cursor is None when there are no more rows."""

    items: list[PlaceResponse]
    next_cursor: int | None


class UploadResponse(BaseModel):
    """Summary returned after a successful upload."""

    inserted: int
    skipped: int  # duplicate place IDs that already existed


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_pool(request: Request) -> asyncpg.Pool:
    """Return the asyncpg pool from app.state."""
    return request.app.state.pool


# ---------------------------------------------------------------------------
# Excel parsing helper
# ---------------------------------------------------------------------------


def _extract_place_ids(file_bytes: bytes) -> list[str]:
    """Parse an Excel workbook and return a deduplicated list of place IDs.

    Column detection order:
    1. Header row containing a cell named "place_id" (case-insensitive).
    2. First column as fallback when no matching header is found.

    Empty / whitespace-only cells are silently dropped.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # Detect header row: first row assumed to be headers
    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    try:
        col_idx = header.index("place_id")
        data_rows = rows[1:]  # skip header
    except ValueError:
        # No "place_id" header — use first column, treat all rows as data
        col_idx = 0
        # If first cell looks like a header string (non-ChIJ prefix), skip it
        data_rows = rows[1:] if header[0] and not header[0].startswith("chij") else rows

    seen: set[str] = set()
    place_ids: list[str] = []
    for row in data_rows:
        if col_idx >= len(row):
            continue
        raw = row[col_idx]
        if raw is None:
            continue
        pid = str(raw).strip()
        if pid and pid not in seen:
            seen.add(pid)
            place_ids.append(pid)

    wb.close()
    return place_ids


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/upload", response_model=UploadResponse, status_code=201)
async def upload_places(
    request: Request,
    file: UploadFile = File(..., description="Excel file (.xlsx) with a 'place_id' column"),
) -> UploadResponse:
    """Upload an Excel file and batch-insert unique place IDs.

    * Duplicate place IDs (already in DB) are skipped silently.
    * A single unnest() CTE inserts all rows in one round-trip.
    * Returns the count of newly inserted rows and skipped duplicates.
    """
    pool = _get_pool(request)

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx / .xls files are accepted",
        )

    content = await file.read()
    try:
        place_ids = _extract_place_ids(content)
    except Exception as exc:
        logger.exception("failed to parse Excel file: %s", exc)
        raise HTTPException(status_code=422, detail=f"Could not parse Excel file: {exc}") from exc

    if not place_ids:
        raise HTTPException(status_code=422, detail="No place IDs found in the uploaded file")

    # Batch insert via unnest — single statement regardless of file size.
    # ON CONFLICT DO NOTHING skips duplicates without raising an error.
    # The RETURNING clause lets us count how many rows were actually inserted.
    async with pool.acquire() as conn:
        inserted_rows = await conn.fetch(
            """
            WITH data(place_id) AS (
                SELECT unnest($1::text[])
            )
            INSERT INTO places (place_id)
            SELECT place_id FROM data
            ON CONFLICT (place_id) DO NOTHING
            RETURNING id
            """,
            place_ids,
        )

    inserted = len(inserted_rows)
    skipped = len(place_ids) - inserted

    logger.info(
        "upload complete: file=%s inserted=%d skipped=%d",
        file.filename,
        inserted,
        skipped,
    )
    return UploadResponse(inserted=inserted, skipped=skipped)


@router.get("", response_model=PlacesListResponse)
async def list_places(
    request: Request,
    cursor: int = Query(default=0, ge=0, description="Last seen ID; 0 fetches from the beginning"),
    limit: int = Query(default=20, ge=1, le=200, description="Page size"),
    status: Literal["all", "pending", "done", "failed"] = Query(
        default="all", description="Filter by processing status"
    ),
) -> PlacesListResponse:
    """List places with forward-only cursor pagination.

    Cursor is the last ID seen on the previous page.  Pass next_cursor from
    the response to fetch the next page.  Returns next_cursor=None when there
    are no more rows.

    Uses WHERE id > :cursor ORDER BY id — never OFFSET — so performance is
    O(log n) on the primary key index regardless of page depth.
    """
    pool = _get_pool(request)

    # Build status filter fragment — evaluated once, not per-row
    if status == "pending":
        status_clause = "AND photos IS NULL"
    elif status == "done":
        # Rows where photos is not null AND has a non-empty array
        status_clause = "AND photos IS NOT NULL AND jsonb_array_length(photos->'photos') > 0"
    elif status == "failed":
        # Rows where photos is not null but array is empty (error or no photos)
        status_clause = "AND photos IS NOT NULL AND jsonb_array_length(photos->'photos') = 0"
    else:
        status_clause = ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, place_id, photos
            FROM   places
            WHERE  id > $1
            {status_clause}
            ORDER  BY id
            LIMIT  $2
            """,
            cursor,
            limit,
        )

    items = [
        PlaceResponse(
            id=row["id"],
            place_id=row["place_id"],
            photos=json.loads(row["photos"]) if row["photos"] else None,
            status=_compute_status(json.loads(row["photos"]) if row["photos"] else None),
        )
        for row in rows
    ]

    # next_cursor is the last ID we returned; None signals end-of-results
    next_cursor = items[-1].id if len(items) == limit else None

    return PlacesListResponse(items=items, next_cursor=next_cursor)


@router.get("/{place_id_pk}", response_model=PlaceResponse)
async def get_place(request: Request, place_id_pk: int) -> PlaceResponse:
    """Return a single place by its integer primary key.

    Returns 404 if the ID does not exist.
    """
    pool = _get_pool(request)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, place_id, photos FROM places WHERE id = $1",
            place_id_pk,
        )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Place {place_id_pk} not found")

    photos_val = json.loads(row["photos"]) if row["photos"] else None
    return PlaceResponse(
        id=row["id"],
        place_id=row["place_id"],
        photos=photos_val,
        status=_compute_status(photos_val),
    )
