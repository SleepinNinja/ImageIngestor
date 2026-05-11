# Places Photo Worker

An async FastAPI service that accepts Excel uploads of Google Place IDs,
batch-inserts them into PostgreSQL, runs a background worker to fetch photo
metadata from the **Google Places API (New)**, and stores the full Photo
objects as JSONB.

---

## What it does

1. **Upload** — POST an `.xlsx` file containing a `place_id` column.
   Rows are inserted in a single `INSERT … SELECT unnest()` statement.
   Duplicates are silently skipped with `ON CONFLICT DO NOTHING`.

2. **Work** — Start the background worker via `POST /job/start`.
   It claims rows with `SELECT … FOR UPDATE SKIP LOCKED` (no Redis/Celery
   needed), fans out concurrent Google API calls up to `MAX_CONCURRENCY`,
   and writes results in a single batch `UPDATE … FROM unnest()`.

3. **Inspect** — Browse results in the web UI or via the REST API.
   Cursor-based pagination (`WHERE id > :cursor`) keeps list queries
   `O(log n)` regardless of table size.

4. **Control** — Stop the worker gracefully at any time.
   The current batch always finishes before the worker exits.

---

## Setup (2 commands)

```bash
cp .env.example .env          # set GOOGLE_PLACES_API_KEY inside .env
docker compose up --build     # starts db, runs migrations, starts api + pgadmin
```

| Service  | URL                        |
|----------|----------------------------|
| Frontend | http://localhost:8001      |
| Swagger  | http://localhost:8001/docs |
| pgAdmin  | http://localhost:5051      |

Sign into pgAdmin with `admin@admin.com` / `admin` (or whatever you set
in `.env`). The **Places DB** server is pre-registered — just click
Connect and enter the password `postgres`.

---

## API reference

| Method | Path               | Description                                  |
|--------|--------------------|----------------------------------------------|
| POST   | `/places/upload`   | Upload Excel file — batch-inserts place IDs  |
| GET    | `/places`          | List places — cursor pagination + status filter |
| GET    | `/places/{id}`     | Single place detail by integer primary key   |
| POST   | `/job/start`       | Start the background photo-fetch worker      |
| GET    | `/job/status`      | Snapshot of worker state (processed, failed) |
| POST   | `/job/stop`        | Request graceful stop (finishes current batch)|

### Query parameters — `GET /places`

| Param    | Default | Description                                     |
|----------|---------|-------------------------------------------------|
| `cursor` | `0`     | Last seen ID; `0` fetches from the beginning    |
| `limit`  | `20`    | Page size (max 200)                             |
| `status` | `all`   | `all` \| `pending` \| `done` \| `failed`       |

### Photo object schema (stored as JSONB)

```json
{
  "photos": [
    {
      "name": "places/ChIJ.../photos/AXCi...",
      "widthPx": 4032,
      "heightPx": 3024,
      "authorAttributions": [{ "displayName": "...", "uri": "...", "photoUri": "..." }],
      "flagContentUri": "https://...",
      "googleMapsUri": "https://..."
    }
  ]
}
```

`photos IS NULL` = not yet processed.
`{"photos": []}` = processed but no photos found (or API error).

---

## Environment variables

| Variable                 | Default                                        | Required | Description                                         |
|--------------------------|------------------------------------------------|----------|-----------------------------------------------------|
| `DATABASE_URL`           | `postgresql://postgres:postgres@db:5432/places`| yes      | asyncpg-compatible PostgreSQL DSN                   |
| `GOOGLE_PLACES_API_KEY`  | _(empty)_                                      | **yes**  | Google Places API (New) key                         |
| `BATCH_SIZE`             | `500`                                           | no       | Rows per worker batch                               |
| `MAX_CONCURRENCY`        | `10`                                            | no       | Max concurrent outbound API calls                   |
| `PGADMIN_DEFAULT_EMAIL`  | `admin@admin.com`                              | no       | pgAdmin login email                                 |
| `PGADMIN_DEFAULT_PASSWORD`| `admin`                                       | no       | pgAdmin login password                              |

---

## Assumptions

* **Excel format** — the file must have a column named `place_id`
  (case-insensitive) in the first (header) row. If no such header exists the
  first column is used as a fallback. One place ID per row; empty cells are
  skipped.

* **Single worker process** — `uvicorn` runs with one worker. Job state is
  an in-process Python object (`app.state.job_state`). Restarting the
  container resets progress counters (processed/failed/total) but does **not**
  re-process rows that already have a `photos` value.

* **Google Places API (New)** — the worker calls
  `GET https://places.googleapis.com/v1/places/{place_id}?fields=photos&key=…`.
  This is the _new_ Places API, not the legacy `maps.googleapis.com` endpoint.
  Ensure "Places API (New)" is enabled in your Google Cloud project.

* **No retry after max retries** — on API failure the worker stores
  `{"photos": []}`, permanently marking the row as processed to avoid
  infinite retry loops. Re-run the worker against a filtered view or manually
  reset specific rows with `UPDATE places SET photos = NULL WHERE id = …`.

* **Idempotent migrations** — Alembic runs `upgrade head` on every container
  start. All DDL statements use `IF NOT EXISTS` so re-runs are safe.

---

## Performance tuning

### `BATCH_SIZE`
Controls how many rows are claimed per loop iteration. Larger values reduce
DB round-trips but hold row-level locks for longer during API calls. For the
default `MAX_CONCURRENCY=5`, a `BATCH_SIZE` of 10–20 is a good starting
point. Increase if your API quota allows higher throughput.

### `MAX_CONCURRENCY`
Controls the asyncio `Semaphore` that caps concurrent outbound HTTP requests
per batch. Set this below your Google API per-second quota. If you hit HTTP
429s frequently, reduce this value.

### Partial index
`CREATE INDEX idx_places_photos_null ON places (id) WHERE photos IS NULL`
keeps the worker's claim query O(log n) by only indexing unprocessed rows.
As rows are processed the index shrinks automatically. No manual maintenance
is needed.

### Connection pool
The asyncpg pool is initialised with `min_size=2, max_size=10`. For high
upload throughput you can increase `max_size`. The worker holds at most one
connection at a time.

---

## Scaling for production

The current design uses intentional simplifications (single process, in-memory
job state) that work well for moderate workloads. Here are eight strategies
to scale when needed:

1. **Persist job state in PostgreSQL or Redis.**
   Replace the `JobState` dataclass with a DB-backed model so progress
   survives restarts and is visible to multiple API replicas.

2. **Run multiple worker processes with SKIP LOCKED.**
   `SELECT … FOR UPDATE SKIP LOCKED` is already in place. Spin up additional
   worker containers or processes — each claims a disjoint batch without
   coordination. Remove the single-process uvicorn constraint (`--workers 4`)
   and store job state externally (see #1).

3. **Use a proper task queue (ARQ / Celery / Dramatiq).**
   For very high volumes, a Redis-backed queue gives better observability,
   dead-letter queues, and scheduled retries than the SKIP LOCKED approach.
   SKIP LOCKED remains valuable as the _internal_ queue inside each worker.

4. **Tune the asyncpg connection pool.**
   Increase `max_size` proportionally to the number of concurrent workers
   and expected query concurrency. Set `max_inactive_connection_lifetime` to
   avoid stale connections behind a load balancer.

5. **Add a read replica for list queries.**
   The `GET /places` endpoint is read-only. Route it to a PostgreSQL read
   replica to avoid contention with the worker's write transactions.

6. **Google API quota management.**
   Implement per-key rate limiting, API key rotation, or a shared token
   bucket (e.g. in Redis) to stay within quota across multiple workers.
   Consider caching responses for place IDs that are re-ingested frequently.

7. **Batch Google API requests with the Places "Nearby Search" multi-place endpoint.**
   Instead of one HTTP request per place ID, investigate whether the Google
   batch endpoint can reduce outbound request count and latency.

8. **Observability and alerting.**
   Emit Prometheus metrics (processed/s, failed/s, queue depth) from the
   worker. Use Grafana to alert on high failure rates or a stalled queue.
   The structured JSON logs produced by `python-json-logger` integrate
   directly with Datadog, ELK, and Google Cloud Logging.
