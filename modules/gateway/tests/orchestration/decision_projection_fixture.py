"""Apply the production projection migration to an owned test schema."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration(revision="107_repository_eval_display"):
    path = Path(__file__).parents[2] / f"alembic/versions/{revision}.py"
    spec = importlib.util.spec_from_file_location("decision_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_migration(connection):
    context = MigrationContext.configure(connection)
    with Operations.context(context), context.begin_transaction():
        migration("104_decision_display_projection").upgrade()
        migration().upgrade()
