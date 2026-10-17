"""SQLite storage with versioned schema.

Two files:
  source.db  - ingested challenge data. Rebuildable from the dataset, upserted incrementally.
  state.db   - user state (corrections, shared notes, memory, audit). Never rebuilt by ingestion.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Settings

SOURCE_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 5        # how long to wait for another process's lock before failing

SOURCE_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, root TEXT, summary_json TEXT);
CREATE TABLE IF NOT EXISTS ingest_files (
  path TEXT PRIMARY KEY, kind TEXT, sha256 TEXT, size INTEGER, record_count INTEGER,
  last_seen_run INTEGER, ingested_at TEXT);
CREATE TABLE IF NOT EXISTS ingest_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, severity TEXT, path TEXT, line_no INTEGER,
  code TEXT, message TEXT, raw TEXT);

CREATE TABLE IF NOT EXISTS teams (team_id TEXT PRIMARY KEY, name TEXT, lead_user_id TEXT);
CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, name TEXT, role TEXT, team_id TEXT);
CREATE TABLE IF NOT EXISTS areas (
  area_id TEXT PRIMARY KEY, name TEXT, team_id TEXT, center_lat REAL, center_lon REAL);

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY, name TEXT, asset_category TEXT, well_type TEXT, area_id TEXT, team_id TEXT,
  in_charge_user_id TEXT, lat REAL, lon REAL, installed_on TEXT, recorded_status TEXT,
  last_inspection_id TEXT, last_inspection_at TEXT, last_inspector_user_id TEXT,
  src_path TEXT, ingested_at TEXT);
CREATE TABLE IF NOT EXISTS equipment (
  equipment_id TEXT PRIMARY KEY, asset_id TEXT, kind TEXT, model TEXT, src_path TEXT);

CREATE TABLE IF NOT EXISTS inspections (
  inspection_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, inspected_at TEXT NOT NULL,
  inspector_user_id TEXT, method TEXT, ambient_temp_c REAL, wellhead_temp_c REAL,
  spill_detected INTEGER, spill_area_pct REAL, spill_est_volume_bbl REAL,
  evidence_images TEXT, notes TEXT, src_path TEXT, src_line INTEGER, ingested_at TEXT);
CREATE INDEX IF NOT EXISTS ix_ins_asset_time ON inspections(asset_id, inspected_at);
CREATE INDEX IF NOT EXISTS ix_ins_time ON inspections(inspected_at);

CREATE TABLE IF NOT EXISTS maintenance (
  maintenance_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
  maintenance_type TEXT, equipment_kind TEXT, issue TEXT, action_taken TEXT, technician_user_id TEXT,
  contractor TEXT, downtime_hours REAL, cost_labor REAL, cost_parts REAL, cost_total REAL,
  triggered_by_inspection_id TEXT, report_file TEXT, src_path TEXT, src_line INTEGER, ingested_at TEXT);
CREATE INDEX IF NOT EXISTS ix_mnt_asset_time ON maintenance(asset_id, started_at);

CREATE TABLE IF NOT EXISTS reg_events (
  event_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, date TEXT NOT NULL, agency TEXT, event_type TEXT,
  protocol_id TEXT, related_inspection_id TEXT, hours_to_report REAL, detail TEXT, fine_usd REAL,
  status TEXT, src_path TEXT, src_line INTEGER, ingested_at TEXT);
CREATE INDEX IF NOT EXISTS ix_reg_asset ON reg_events(asset_id, date);

CREATE TABLE IF NOT EXISTS notes (
  note_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, created_at TEXT NOT NULL, author_user_id TEXT,
  note_type TEXT, inspection_id TEXT, text TEXT, valid_from TEXT, valid_to TEXT,
  src_path TEXT, src_line INTEGER, ingested_at TEXT);
CREATE INDEX IF NOT EXISTS ix_notes_asset ON notes(asset_id, created_at);

CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, kind TEXT, title TEXT, path TEXT, asset_id TEXT, maintenance_id TEXT,
  well_type TEXT, sha256 TEXT, text TEXT, ingested_at TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks USING fts5(
  doc_id UNINDEXED, section UNINDEXED, text, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS rules (
  rule_key TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_value TEXT NOT NULL,
  value_num REAL, unit TEXT, source_doc TEXT NOT NULL, source_section TEXT, excerpt TEXT,
  status TEXT NOT NULL DEFAULT 'ok',            -- ok | rejected (failed validation; never applied)
  status_reason TEXT,
  PRIMARY KEY (rule_key, scope_kind, scope_value, source_doc));

CREATE TABLE IF NOT EXISTS report_facts (
  maintenance_id TEXT NOT NULL, fact_key TEXT NOT NULL, value_num REAL, value_text TEXT,
  protocol_id TEXT, excerpt TEXT, extractor TEXT, PRIMARY KEY (maintenance_id, fact_key, protocol_id));
"""

STATE_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Append-only audit log of every state change.
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_user_id TEXT, action TEXT NOT NULL,
  object_type TEXT, object_id TEXT, detail_json TEXT);

-- Corrections never alter source rows; they are overlays resolved at read time.
CREATE TABLE IF NOT EXISTS corrections (
  correction_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,            -- raw_value | analysis_value | detection | rule_override
  target_type TEXT NOT NULL,     -- inspection | asset | well_type
  target_id TEXT NOT NULL,
  field TEXT NOT NULL,
  original_value TEXT, new_value TEXT,
  scope_kind TEXT NOT NULL,      -- inspection | asset | well_type
  scope_value TEXT NOT NULL,
  author_user_id TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',   -- active | superseded | withdrawn
  supersedes_id TEXT, conflicts_with_json TEXT);
CREATE INDEX IF NOT EXISTS ix_corr_target ON corrections(target_type, target_id, field, status);
CREATE INDEX IF NOT EXISTS ix_corr_scope ON corrections(scope_kind, scope_value, field, status);

-- Shared and private statements people make about assets (plans, claims, observations).
CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, author_user_id TEXT NOT NULL, text TEXT NOT NULL,
  claim_type TEXT NOT NULL,      -- planned_shutdown | status_claim | observation | other
  valid_from TEXT, valid_to TEXT, visibility TEXT NOT NULL DEFAULT 'shared',  -- shared | private
  verification TEXT NOT NULL DEFAULT 'unverified',  -- unverified | supported | contradicted | confirmed
  verification_detail TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_claims_asset ON claims(asset_id, created_at);

CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT, started_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, response_json TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_id, created_at);

-- Private, per-user record of the entities (e.g. wells) and the question behind each result.
CREATE TABLE IF NOT EXISTS user_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, conversation_id TEXT,
  topic TEXT, question TEXT, entity_ids_json TEXT, window_json TEXT, tool_name TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_mem_user ON user_memory(user_id, created_at);
"""


def _apply(conn: sqlite3.Connection, ddl: str, version: int) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > version:
        raise RuntimeError(f"database schema v{current} is newer than this code (v{version})")
    if current < version:
        conn.executescript(ddl)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


def connect(path: Path, ddl: str, version: int, apply: bool = True) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if apply:
        _apply(conn, ddl, version)
    return conn


def _reset_derived(conn: sqlite3.Connection) -> None:
    """Drop every table, view and trigger so the DDL can be re-applied. Used only for the derived source database."""
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for kind in ("view", "trigger"):
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,)).fetchall():
                conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
        virtual = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'").fetchall()]
        for name in virtual:                                     # dropping a virtual table also removes its shadow tables
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def open_source(settings: Settings) -> sqlite3.Connection:
    """The source database is derived from the dataset, so an older schema is rebuilt (never half-migrated).

    The rebuild happens inside the database, never by deleting the file: on Windows a file that another process (a running server
    or evaluation) has open cannot be deleted, and deleting under a live reader is unsafe anyway. If another process holds a lock
    the rebuild waits briefly, then fails with a clear message and leaves the database as it was. User state lives in state.db and is untouched."""
    path = Path(settings.source_db)
    conn = connect(path, "", 0, apply=False)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if 0 < version < SOURCE_SCHEMA_VERSION:
            try:
                _reset_derived(conn)
            except sqlite3.OperationalError as e:
                raise RuntimeError(f"{path.name} has an older schema (v{version}) and must be rebuilt, but it is in use by another process "
                                   f"({e}). Stop the other server or evaluation, or set COPILOT_DATA_DIR to a fresh folder.") from None
        _apply(conn, SOURCE_DDL, SOURCE_SCHEMA_VERSION)
    except BaseException:
        conn.close()
        raise
    return conn


def open_state(settings: Settings) -> sqlite3.Connection:
    return connect(settings.state_db, STATE_DDL, STATE_SCHEMA_VERSION)
