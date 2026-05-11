# Places Photo Worker

## Project Overview
An async FastAPI service that:
1. Accepts Excel uploads of Google Place IDs and batch-inserts them into PostgreSQL
2. Runs a background job that calls the Google Places API (New) to fetch photo metadata
3. Stores full Photo objects as JSONB in the `photos` column
4. Exposes job control via REST API (start, status, stop)
5. Serves a minimal HTML/CSS/JS frontend

## Tech Stack
- Python 3.12
- FastAPI + uvicorn
- asyncpg (runtime DB queries)
- aiohttp (Google API calls)
- Alembic + SQLAlchemy + psycopg2-binary (migrations only)
- pydantic-settings (config)
- openpyxl (Excel parsing)
- python-json-logger (structured JSON logs)
- Poetry (dependency management)
- Docker + Docker Compose (PostgreSQL + API + pgAdmin)

## Key Architecture Decisions
- Use SELECT ... FOR UPDATE SKIP LOCKED for row claiming — no Redis/Celery needed
- asyncio.Semaphore controls concurrent API calls (MAX_CONCURRENCY env var)
- Batch UPDATE after each batch — never row-by-row
- Exponential backoff retry on HTTP 429 and 5xx (max 3 retries)
- Store {"photos": []} on error so rows are never retried
- Graceful shutdown — finish current batch before stopping
- Alembic runs automatically at container startup
- Job state is in-memory (intentional — see README for production notes)
- Cursor-based pagination (WHERE id > cursor) not OFFSET

## Google Places API
- Endpoint: GET https://places.googleapis.com/v1/places/{place_id}?fields=photos&key={api_key}
- Response has a "photos" array of Photo objects
- Each Photo object: { name, widthPx, heightPx, authorAttributions, flagContentUri, googleMapsUri }
- Store the FULL Photo object per photo, not just the name field

## Project Structure
- app/main.py — FastAPI entry point, lifespan hooks
- app/config.py — pydantic-settings config
- app/database.py — asyncpg pool
- app/worker.py — JobState class + fetch logic + run_worker loop
- app/routers/job.py — POST /job/start, GET /job/status, POST /job/stop
- app/routers/places.py — POST /places/upload, GET /places, GET /places/{id}
- migrations/ — Alembic migration files
- static/index.html — complete frontend (no framework)
- docker-compose.yml — db + api + pgadmin
- Dockerfile — python:3.12-slim, poetry, layer-cached

## Services
- api: http://localhost:8000 (frontend + API + swagger at /docs)
- pgadmin: http://localhost:5050

## Constraints
- No Celery, Redis, Kafka — SKIP LOCKED is the queue
- No React/Vue — plain HTML/CSS/JS served by FastAPI
- No OFFSET pagination — cursor-based only
- Single uvicorn worker (job state is singleton)
- photos IS NULL = unprocessed, {"photos": [...]} = done, {"photos": []} = failed/no photos