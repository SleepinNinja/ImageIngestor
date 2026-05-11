"""Application configuration via pydantic-settings.

All settings can be overridden with environment variables or a .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central config loaded from environment / .env at startup."""

    # PostgreSQL DSN used by asyncpg at runtime
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/places"

    # Google Places API (New) key — required for the worker to fetch photos
    GOOGLE_PLACES_API_KEY: str = ""

    # Number of rows claimed per worker iteration
    BATCH_SIZE: int = 500

    # Maximum concurrent outbound HTTP requests to the Places API
    MAX_CONCURRENCY: int = 50

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Do not raise on extra env vars (docker-compose often injects extras)
        extra="ignore",
    )


settings = Settings()
