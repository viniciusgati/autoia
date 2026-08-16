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
    "repositories": [
        ("max_attempts", "INTEGER"),
        ("max_pm_decisions", "INTEGER"),
        ("run_timeout", "INTEGER"),
        ("task_budget", "FLOAT"),
        ("cost_per_interaction", "FLOAT"),
        ("risky_patterns_extra", "TEXT"),
        ("db_rule", "TEXT"),
        ("allow_auto_tasks", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("allow_external_tasks", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("default_pipeline_id", "INTEGER REFERENCES pipelines(id)"),
        ("auto_summary", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("sandbox", "VARCHAR(10)"),
        ("task_targets", "JSON"),
        ("external_context", "TEXT"),
    ],
    "robots": [
        ("role", "VARCHAR(30) DEFAULT 'implement' NOT NULL"),
        ("repository_id", "INTEGER REFERENCES repositories(id)"),
        ("archived", "BOOLEAN DEFAULT 0 NOT NULL"),
    ],
    "pipelines": [("repository_id", "INTEGER REFERENCES repositories(id)")],
    "tasks": [
        ("acceptance_criteria", "TEXT"),
        ("pm_decisions", "INTEGER DEFAULT 0 NOT NULL"),
        ("feedback", "TEXT"),
        ("parent_task_id", "INTEGER REFERENCES tasks(id)"),
        ("executor", "VARCHAR(20) DEFAULT 'kimi' NOT NULL"),
        ("details", "TEXT"),
        ("resume_instruction", "TEXT"),
        ("block_reason_type", "VARCHAR(50)"),
        ("block_reason", "TEXT"),
        ("block_question", "TEXT"),
        ("block_options", "JSON"),
        ("responsible_id", "INTEGER REFERENCES users(id)"),
        ("project_id", "INTEGER REFERENCES projects(id)"),
        ("epic_id", "INTEGER REFERENCES epics(id)"),
    ],
    "task_steps": [
        ("verdict", "VARCHAR(30)"),
        ("post_merge", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("diff_stat", "TEXT"),
        ("pause_before", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("goal", "TEXT"),
        ("responsible_id", "INTEGER REFERENCES users(id)"),
        ("finished_by_id", "INTEGER REFERENCES users(id)"),
        ("session_id", "VARCHAR(200)"),
        ("archived", "BOOLEAN DEFAULT 0 NOT NULL"),
    ],
    "pipeline_steps": [
        ("post_merge", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("pause_before", "BOOLEAN DEFAULT 0 NOT NULL"),
    ],
    "step_artifacts": [],
    "task_proposals": [
        ("pipeline_id", "INTEGER REFERENCES pipelines(id)"),
    ],
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
