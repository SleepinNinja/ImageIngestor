# ── Build stage ──────────────────────────────────────────────────────────────
# python:3.12-slim is ~60 MB smaller than the full image and includes pip.
FROM python:3.12-slim AS base

# Prevent Python from buffering stdout/stderr (crucial for Docker log capture)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Pin Poetry version so the build is reproducible
    POETRY_VERSION=1.8.3 \
    # Never prompt for user input during poetry commands
    POETRY_NO_INTERACTION=1 \
    # Install packages into the system Python — no virtualenv needed inside Docker
    POETRY_VIRTUALENVS_CREATE=false \
    # Cache pip downloads between builds (mounted as a BuildKit cache)
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install Poetry globally
RUN pip install "poetry==$POETRY_VERSION"

WORKDIR /app

# ── Layer 1: dependencies (cached unless pyproject.toml / poetry.lock change) ─
# Copying these two files first means Docker reuses this layer on every build
# that doesn't change the dependency manifest — even if application code changed.
COPY pyproject.toml poetry.lock ./

RUN poetry install --only main --no-root

# ── Layer 2: application code ─────────────────────────────────────────────────
# Copied last so code changes don't bust the dependency layer above.
COPY . .

EXPOSE 8000

# Single uvicorn worker — job state is an in-process singleton
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
