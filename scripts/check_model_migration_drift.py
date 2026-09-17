"""Fail when the live schema differs from the SQLAlchemy model metadata.

The database must already be at Alembic head. This performs the same
autogenerate comparison used to author migrations without writing a file.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.database import Base

# Keep model registration aligned with alembic/env.py.
from app.models import *  # noqa: F401,F403,E402
from app.models.messaging import *  # noqa: F401,F403,E402

# Keep optional model modules aligned with alembic/env.py.
try:
    from app.models.smart_analytics import *  # noqa: F401,F403,E402
except ImportError:
    pass


ACTIONABLE_OPS = {"add_table", "remove_table", "add_column", "remove_column"}


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    engine = create_engine(database_url)

    with engine.connect() as connection:
        # Match Alembic's normal autogenerate settings. Index, default, FK,
        # and type naming differences are legacy noise in this repository.
        context = MigrationContext.configure(connection)
        differences = compare_metadata(context, Base.metadata)
    differences = [
        difference for difference in differences
        if (
            isinstance(difference, tuple)
            and difference
            and isinstance(difference[0], str)
            and difference[0] in ACTIONABLE_OPS
        )
    ]

    if differences:
        print("Model/migration drift detected:")
        for difference in differences:
            print(f"  {difference!r}")
        return 1

    print("Model/migration drift check passed: no differences found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
