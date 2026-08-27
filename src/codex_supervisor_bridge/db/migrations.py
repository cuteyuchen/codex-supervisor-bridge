from __future__ import annotations

import sqlite3

from .schema import SCHEMA_SQL, SCHEMA_VERSION

MIGRATIONS: dict[int, str] = {
    1: SCHEMA_SQL,
}


def current_schema_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    current = current_schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than supported version {SCHEMA_VERSION}"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        sql = MIGRATIONS.get(version)
        if sql is None:
            raise RuntimeError(f"Missing database migration for version {version}")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )
    return SCHEMA_VERSION
