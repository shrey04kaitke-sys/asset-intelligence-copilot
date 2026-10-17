"""Database lifecycle: an old derived source.db is rebuilt in place (never deleted), state.db is never touched, and API startup
works while another connection holds the database open (the Windows WinError 32 case)."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.config import Settings
from copilot.db import SOURCE_SCHEMA_VERSION, STATE_DDL, STATE_SCHEMA_VERSION, connect, open_source, open_state


def old_source(settings: Settings, version: int = 1) -> None:
    """A source.db as an earlier release left it: old schema, a table of the old shape, an FTS table, data."""
    settings.source_db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(settings.source_db))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE rules (rule_key TEXT, value REAL)")
    c.execute("INSERT INTO rules VALUES ('old', 1)")
    c.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(body)")
    c.execute("INSERT INTO docs_fts VALUES ('stale text')")
    c.execute(f"PRAGMA user_version = {version}")
    c.commit()
    c.close()


@pytest.fixture
def s(tmp_path):
    return Settings(dataset_dir=tmp_path / "ds", data_dir=tmp_path / "data")


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_old_schema_is_rebuilt_in_place_without_deleting_the_file(s, monkeypatch):
    old_source(s)
    monkeypatch.setattr(type(s.source_db), "unlink", lambda *a, **k: pytest.fail("source.db must never be deleted"), raising=False)
    conn = open_source(s)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SOURCE_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0            # stale rows gone, new-shape table present
    assert "status" in [r[1] for r in conn.execute("PRAGMA table_info(rules)")]
    assert "assets" in tables(conn)
    conn.close()


def test_rebuild_works_while_another_connection_is_open_and_idle(s):
    """The Windows failure: a second handle on the file made unlink() raise PermissionError. An idle reader must not block the rebuild."""
    old_source(s)
    other = sqlite3.connect(str(s.source_db))
    other.execute("SELECT 1").fetchone()                                             # open, no transaction
    conn = open_source(s)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SOURCE_SCHEMA_VERSION
    assert s.source_db.exists()
    other.close()
    conn.close()


def test_rebuild_blocked_by_a_writer_fails_clearly_and_keeps_the_database(s, monkeypatch):
    import copilot.db as dbm
    monkeypatch.setattr(dbm, "BUSY_TIMEOUT_S", 0.1)
    old_source(s)
    holder = sqlite3.connect(str(s.source_db), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")                                                # another process is writing
    try:
        with pytest.raises(RuntimeError, match="in use by another process"):
            open_source(s)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    c = sqlite3.connect(str(s.source_db))
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1                       # untouched
    assert c.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 1
    c.close()


def test_current_schema_is_left_alone(s):
    c = open_source(s)
    c.execute("INSERT INTO ingest_runs(started_at) VALUES ('x')") if "ingest_runs" in tables(c) else None
    n = len(tables(c))
    c.close()
    again = open_source(s)
    assert len(tables(again)) == n and again.execute("PRAGMA user_version").fetchone()[0] == SOURCE_SCHEMA_VERSION
    again.close()


def test_unreadable_or_future_database_is_not_destroyed(s):
    s.source_db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(s.source_db))
    c.execute(f"PRAGMA user_version = {SOURCE_SCHEMA_VERSION + 5}")
    c.execute("CREATE TABLE keepme (x)")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        open_source(s)
    c = sqlite3.connect(str(s.source_db))
    assert "keepme" in tables(c)
    c.close()


def test_state_db_is_untouched_by_source_rebuild(s):
    st = connect(s.state_db, STATE_DDL, STATE_SCHEMA_VERSION)
    st.execute("INSERT INTO user_memory(user_id, topic, question, created_at) VALUES ('U-1','t','keep me','2026-01-01')")
    st.commit()
    st.close()
    before = s.state_db.read_bytes()
    old_source(s)
    open_source(s).close()
    assert s.state_db.read_bytes() == before
    st = open_state(s)
    assert st.execute("SELECT question FROM user_memory").fetchone()[0] == "keep me"
    st.close()


def test_open_source_closes_its_connection_on_failure(s, monkeypatch):
    old_source(s, version=SOURCE_SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError):
        open_source(s)
    s.source_db.rename(s.source_db.with_name("moved.db"))                            # would fail on Windows if a handle leaked


def test_api_startup_with_old_database_and_a_second_open_handle(s):
    old_source(s)
    other = sqlite3.connect(str(s.source_db))
    other.execute("SELECT 1").fetchone()
    app = create_app(settings=s)                                                     # opens source.db and state.db itself
    r = TestClient(app).get("/health")
    assert r.status_code == 200 and r.json()["tools"] >= 1
    other.close()
