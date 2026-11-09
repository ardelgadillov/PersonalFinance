"""Database engine/session helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from finance.config import get_settings
from finance.models import Base

_engines: dict[str, Engine] = {}


def get_engine(db_url: str | None = None) -> Engine:
    if db_url is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db_url = settings.db_url
    if db_url not in _engines:
        engine = create_engine(db_url, future=True)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        _engines[db_url] = engine
    return _engines[db_url]


def _add_missing_columns(engine: Engine) -> None:
    """Tiny forward-only migration: add columns that exist in the models but not in the DB."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))


def init_db(engine: Engine | None = None) -> Engine:
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    from finance.categories import seed_categories
    with Session(engine) as s:
        if seed_categories(s):
            s.commit()
    return engine
