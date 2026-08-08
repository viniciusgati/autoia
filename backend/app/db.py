"""Engine e sessões do SQLAlchemy."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def utcnow() -> datetime:
    """Datetime UTC 'naive' (consistente com o que o SQLite/Pydantic esperam)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str):
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return engine
    return create_engine(database_url)


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


# Colunas aditivas para bancos criados em versões anteriores (v1.0 -> v1.1).
ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "robots": [("role", "VARCHAR(30) DEFAULT 'implement' NOT NULL")],
    "tasks": [
        ("acceptance_criteria", "TEXT"),
        ("pm_decisions", "INTEGER DEFAULT 0 NOT NULL"),
        ("feedback", "TEXT"),
    ],
    "task_steps": [
        ("verdict", "VARCHAR(30)"),
        ("post_merge", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("diff_stat", "TEXT"),
    ],
    "pipeline_steps": [("post_merge", "BOOLEAN DEFAULT 0 NOT NULL")],
}


def migrate_schema(engine) -> None:
    """Adiciona colunas novas em tabelas já existentes (somente adições)."""
    with engine.begin() as conn:
        for table, columns in ADDITIVE_COLUMNS.items():
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for name, ddl in columns:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
