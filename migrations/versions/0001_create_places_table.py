"""create places table

Revision ID: 0001
Revises:
Create Date: 2026-05-12 00:00:00.000000

Schema
------
places
  id          BIGSERIAL PRIMARY KEY
  place_id    TEXT NOT NULL UNIQUE          — Google Place ID
  photos      JSONB                         — NULL = unprocessed, {...} = done/failed
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()

Indexes
-------
* Primary key on id (implicit).
* Unique index on place_id (implicit from UNIQUE constraint).
* Partial index ``idx_places_photos_null`` on (id) WHERE photos IS NULL.
  This index is used exclusively by the worker's claim query:
      SELECT … FROM places WHERE photos IS NULL FOR UPDATE SKIP LOCKED LIMIT n
  A partial index is much smaller than a full index on all rows and stays
  useful as the table grows — it shrinks as rows are processed.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the places table and supporting indexes."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS places (
            id         BIGSERIAL    PRIMARY KEY,
            place_id   TEXT         NOT NULL UNIQUE,
            photos     JSONB,
            created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
        """
    )

    # Partial index: only indexes unprocessed rows.
    # When all rows are processed this index becomes empty — zero overhead.
    # The worker query (WHERE photos IS NULL) hits this index directly via
    # an index scan instead of a seq scan on a potentially large table.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_places_photos_null
        ON places (id)
        WHERE photos IS NULL
        """
    )


def downgrade() -> None:
    """Drop the partial index and the places table."""
    op.execute("DROP INDEX IF EXISTS idx_places_photos_null")
    op.execute("DROP TABLE IF EXISTS places")
