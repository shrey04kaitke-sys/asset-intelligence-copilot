"""Generic, incremental ingestion of a challenge-format dataset.

* Files are discovered by glob (no hardcoded well IDs).
* Each file is hashed; unchanged files are skipped, changed files are replaced atomically
  (rows from that file are removed and re-inserted), removed files delete their rows.
* Invalid records are rejected with an issue entry; they never abort the run.
* The source dataset is only ever read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import extract
from .config import Settings
from .db import open_source

log = logging.getLogger("copilot.ingest")


# ----------------------------------------------------------------------------- helpers

def parse_ts(v: Any) -> str | None:
    """Normalise an ISO-8601 timestamp to UTC 'YYYY-MM-DDTHH:MM:SSZ'; None if invalid."""
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00Z")
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def parse_date(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.strptime(v.strip()[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Run:
    def __init__(self, conn: sqlite3.Connection, root: Path):
        self.conn, self.root = conn, root
        self.started = now_iso()
        cur = conn.execute("INSERT INTO ingest_runs(started_at, root) VALUES (?,?)", (self.started, str(root)))
        self.id = cur.lastrowid
        self.counts: dict[str, dict[str, int]] = {}
        self.files = {"added": 0, "changed": 0, "unchanged": 0, "removed": 0}

    def count(self, kind: str, what: str, n: int = 1) -> None:
        self.counts.setdefault(kind, {"accepted": 0, "rejected": 0})
        self.counts[kind][what] = self.counts[kind].get(what, 0) + n

    def issue(self, severity: str, path: str, code: str, message: str, line_no: int | None = None, raw: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO ingest_issues(run_id,severity,path,line_no,code,message,raw) VALUES (?,?,?,?,?,?,?)",
            (self.id, severity, path, line_no, code, message, (raw or "")[:500]),
        )


def iter_jsonl(path: Path) -> Iterable[tuple[int, str, dict | None, str | None]]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                yield i, line, None, f"invalid JSON: {e.msg}"
                continue
            if not isinstance(obj, dict):
                yield i, line, None, "record is not a JSON object"
                continue
            yield i, line, obj, None


# ----------------------------------------------------------------------------- per-kind loaders

def _insert(run: Run, table: str, row: dict, rel: str, line_no: int | None, kind: str) -> bool:
    cols = ",".join(row)
    qs = ",".join("?" for _ in row)
    try:
        run.conn.execute(f"INSERT INTO {table}({cols}) VALUES ({qs})", list(row.values()))
        run.count(kind, "accepted")
        return True
    except sqlite3.IntegrityError:
        existing = run.conn.execute(f"SELECT src_path FROM {table} WHERE {_pk(table)}=?", (row[_pk(table)],)).fetchone()
        where = existing["src_path"] if existing else "?"
        run.issue("error", rel, "duplicate_id", f"{kind} id {row[_pk(table)]} already loaded from {where}; later duplicate ignored", line_no)
        run.count(kind, "rejected")
        return False


def _pk(table: str) -> str:
    return {"inspections": "inspection_id", "maintenance": "maintenance_id", "reg_events": "event_id", "notes": "note_id"}[table]


def load_inspections(run: Run, path: Path, rel: str) -> int:
    n = 0
    t = now_iso()
    for ln, raw, o, err in iter_jsonl(path):
        if err:
            run.issue("error", rel, "bad_json", err, ln, raw); run.count("inspection", "rejected"); continue
        iid, aid = o.get("inspection_id"), o.get("asset_id")
        ts = parse_ts(o.get("inspected_at"))
        if not iid or not aid or not ts:
            run.issue("error", rel, "missing_field", "inspection needs inspection_id, asset_id and a valid inspected_at", ln, raw)
            run.count("inspection", "rejected"); continue
        if not _asset_exists(run, aid):
            run.issue("error", rel, "unknown_asset", f"inspection {iid} references unknown asset {aid}", ln, raw)
            run.count("inspection", "rejected"); continue
        amb, wh = num(o.get("ambient_temp_c")), num(o.get("wellhead_temp_c"))
        if amb is None or wh is None:
            run.issue("warning", rel, "missing_temperature", f"{iid}: temperature missing/invalid; production status will be unknown", ln)
        elif not (-90 <= amb <= 70 and -90 <= wh <= 300):
            run.issue("warning", rel, "implausible_temperature", f"{iid}: ambient {amb} / wellhead {wh} outside plausible range", ln)
        sd = o.get("spill_detected")
        vol, pct = num(o.get("spill_est_volume_bbl")), num(o.get("spill_area_pct"))
        if sd and vol is None:
            run.issue("warning", rel, "spill_without_volume", f"{iid}: spill detected but no usable volume estimate", ln)
        if vol is not None and vol < 0:
            run.issue("error", rel, "negative_volume", f"{iid}: negative spill volume", ln, raw); run.count("inspection", "rejected"); continue
        row = dict(inspection_id=iid, asset_id=aid, inspected_at=ts, inspector_user_id=o.get("inspector_user_id"),
                   method=o.get("method"), ambient_temp_c=amb, wellhead_temp_c=wh, spill_detected=1 if sd else 0,
                   spill_area_pct=pct, spill_est_volume_bbl=vol, evidence_images=json.dumps(o.get("evidence_images") or []),
                   notes=o.get("notes") or "", src_path=rel, src_line=ln, ingested_at=t)
        n += _insert(run, "inspections", row, rel, ln, "inspection")
    return n


def load_maintenance(run: Run, path: Path, rel: str) -> int:
    n, t = 0, now_iso()
    for ln, raw, o, err in iter_jsonl(path):
        if err:
            run.issue("error", rel, "bad_json", err, ln, raw); run.count("maintenance", "rejected"); continue
        mid, aid = o.get("maintenance_id"), o.get("asset_id")
        st, en = parse_ts(o.get("started_at")), parse_ts(o.get("completed_at"))
        if not mid or not aid or not st:
            run.issue("error", rel, "missing_field", "maintenance needs maintenance_id, asset_id and valid started_at", ln, raw)
            run.count("maintenance", "rejected"); continue
        if not _asset_exists(run, aid):
            run.issue("error", rel, "unknown_asset", f"maintenance {mid} references unknown asset {aid}", ln, raw)
            run.count("maintenance", "rejected"); continue
        if o.get("completed_at") and not en:
            run.issue("warning", rel, "bad_completed_at", f"{mid}: completed_at invalid; stored as null", ln)
        if en and en < st:
            run.issue("warning", rel, "end_before_start", f"{mid}: completed_at precedes started_at", ln)
        cost = o.get("cost_usd") if isinstance(o.get("cost_usd"), dict) else {}
        row = dict(maintenance_id=mid, asset_id=aid, started_at=st, completed_at=en,
                   maintenance_type=o.get("maintenance_type"), equipment_kind=o.get("equipment_kind"),
                   issue=o.get("issue"), action_taken=o.get("action_taken"), technician_user_id=o.get("technician_user_id"),
                   contractor=o.get("contractor"), downtime_hours=num(o.get("downtime_hours")),
                   cost_labor=num(cost.get("labor")), cost_parts=num(cost.get("parts")), cost_total=num(cost.get("total")),
                   triggered_by_inspection_id=o.get("triggered_by_inspection_id"), report_file=o.get("report_file"),
                   src_path=rel, src_line=ln, ingested_at=t)
        n += _insert(run, "maintenance", row, rel, ln, "maintenance")
    return n


def load_reg_events(run: Run, path: Path, rel: str) -> int:
    n, t = 0, now_iso()
    for ln, raw, o, err in iter_jsonl(path):
        if err:
            run.issue("error", rel, "bad_json", err, ln, raw); run.count("regulatory_event", "rejected"); continue
        eid, aid, d = o.get("event_id"), o.get("asset_id"), parse_date(o.get("date"))
        if not eid or not aid or not d:
            run.issue("error", rel, "missing_field", "regulatory event needs event_id, asset_id and valid date", ln, raw)
            run.count("regulatory_event", "rejected"); continue
        if not _asset_exists(run, aid):
            run.issue("error", rel, "unknown_asset", f"event {eid} references unknown asset {aid}", ln, raw)
            run.count("regulatory_event", "rejected"); continue
        row = dict(event_id=eid, asset_id=aid, date=d, agency=o.get("agency"), event_type=o.get("event_type"),
                   protocol_id=o.get("protocol_id"), related_inspection_id=o.get("related_inspection_id"),
                   hours_to_report=num(o.get("hours_to_report")), detail=o.get("detail"), fine_usd=num(o.get("fine_usd")) or 0.0,
                   status=o.get("status"), src_path=rel, src_line=ln, ingested_at=t)
        n += _insert(run, "reg_events", row, rel, ln, "regulatory_event")
    return n


def load_notes(run: Run, path: Path, rel: str) -> int:
    n, t = 0, now_iso()
    for ln, raw, o, err in iter_jsonl(path):
        if err:
            run.issue("error", rel, "bad_json", err, ln, raw); run.count("note", "rejected"); continue
        nid, aid, ca = o.get("note_id"), o.get("asset_id"), parse_ts(o.get("created_at"))
        if not nid or not aid or not ca or not o.get("text"):
            run.issue("error", rel, "missing_field", "note needs note_id, asset_id, created_at and text", ln, raw)
            run.count("note", "rejected"); continue
        if not _asset_exists(run, aid):
            run.issue("error", rel, "unknown_asset", f"note {nid} references unknown asset {aid}", ln, raw)
            run.count("note", "rejected"); continue
        vf, vt = parse_date(o.get("valid_from")), parse_date(o.get("valid_to"))
        if o.get("valid_to") and not vt:
            run.issue("warning", rel, "bad_valid_to", f"{nid}: valid_to invalid; treated as open-ended", ln)
        if o.get("note_type") == "correction" and not o.get("inspection_id"):
            run.issue("warning", rel, "correction_without_target", f"{nid}: correction note has no inspection_id; cannot be applied", ln)
        row = dict(note_id=nid, asset_id=aid, created_at=ca, author_user_id=o.get("author_user_id"),
                   note_type=o.get("note_type"), inspection_id=o.get("inspection_id"), text=o["text"],
                   valid_from=vf, valid_to=vt, src_path=rel, src_line=ln, ingested_at=t)
        n += _insert(run, "notes", row, rel, ln, "note")
    return n


def _asset_exists(run: Run, aid: str) -> bool:
    return run.conn.execute("SELECT 1 FROM assets WHERE asset_id=?", (aid,)).fetchone() is not None


def load_metadata(run: Run, path: Path, rel: str) -> int:
    t = now_iso()
    try:
        o = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as e:
        run.issue("error", rel, "bad_json", f"metadata unreadable: {e}"); run.count("asset", "rejected"); return 0
    aid = o.get("asset_id") if isinstance(o, dict) else None
    if not aid or not o.get("well_type"):
        run.issue("error", rel, "missing_field", "metadata needs asset_id and well_type"); run.count("asset", "rejected"); return 0
    folder = path.parent.name
    if folder != aid:
        run.issue("warning", rel, "folder_mismatch", f"asset_id {aid} differs from folder {folder}")
    loc = o.get("location") or {}
    li = o.get("last_inspection") or {}
    lat, lon = num(loc.get("lat")), num(loc.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        run.issue("warning", rel, "bad_location", f"{aid}: missing or invalid coordinates; asset cannot be mapped")
        lat = lon = None
    existing = run.conn.execute("SELECT src_path FROM assets WHERE asset_id=?", (aid,)).fetchone()
    if existing and existing["src_path"] != rel:
        run.issue("error", rel, "duplicate_id", f"asset {aid} already loaded from {existing['src_path']}"); run.count("asset", "rejected"); return 0
    run.conn.execute("DELETE FROM assets WHERE asset_id=?", (aid,))
    run.conn.execute("DELETE FROM equipment WHERE asset_id=?", (aid,))
    run.conn.execute(
        "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, o.get("name"), o.get("asset_category"), o["well_type"], o.get("area_id"), o.get("team_id"),
         o.get("in_charge_user_id"), lat, lon, o.get("installed_on"), o.get("recorded_status"),
         li.get("inspection_id"), parse_ts(li.get("inspected_at")), li.get("inspector_user_id"), rel, t))
    for eq in o.get("equipment") or []:
        if isinstance(eq, dict) and eq.get("equipment_id"):
            run.conn.execute("INSERT OR REPLACE INTO equipment VALUES (?,?,?,?,?)",
                             (eq["equipment_id"], aid, eq.get("kind"), eq.get("model"), rel))
    run.count("asset", "accepted")
    return 1


def load_fleet_json(run: Run, path: Path, rel: str) -> int:
    try:
        o = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as e:
        run.issue("error", rel, "bad_json", f"unreadable: {e}"); return 0
    n = 0
    c = run.conn
    if path.name == "areas.json":
        for t in o.get("teams", []):
            c.execute("INSERT OR REPLACE INTO teams VALUES (?,?,?)", (t.get("team_id"), t.get("name"), t.get("lead_user_id"))); n += 1
        for a in o.get("areas", []):
            ctr = a.get("center") or {}
            c.execute("INSERT OR REPLACE INTO areas VALUES (?,?,?,?,?)",
                      (a.get("area_id"), a.get("name"), a.get("team_id"), num(ctr.get("lat")), num(ctr.get("lon")))); n += 1
        run.count("fleet", "accepted", n)
    elif path.name == "users.json":
        for u in o.get("users", []):
            if u.get("user_id"):
                c.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?)", (u["user_id"], u.get("name"), u.get("role"), u.get("team_id"))); n += 1
        run.count("fleet", "accepted", n)
    return n


# ----------------------------------------------------------------------------- documents

def _chunks(text: str, max_chars: int = 1400) -> list[tuple[str, str]]:
    out = []
    for heading, body in extract._sections(text):
        body = body.strip()
        if not body:
            continue
        paras, buf = body.split("\n\n"), ""
        for p in paras:
            if buf and len(buf) + len(p) > max_chars:
                out.append((heading, buf.strip())); buf = ""
            buf += p + "\n\n"
        if buf.strip():
            out.append((heading, buf.strip()))
    return out


def _store_doc(run: Run, doc_id: str, kind: str, text: str, rel: str, sha: str, **extra) -> None:
    c = run.conn
    c.execute("DELETE FROM doc_chunks WHERE doc_id=?", (doc_id,))
    c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
    c.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
              (doc_id, kind, extract.doc_title(text, Path(rel).stem), rel, extra.get("asset_id"),
               extra.get("maintenance_id"), extra.get("well_type"), sha, text, now_iso()))
    for section, chunk in _chunks(text):
        c.execute("INSERT INTO doc_chunks(doc_id, section, text) VALUES (?,?,?)", (doc_id, section, chunk))


def load_manual(run: Run, path: Path, rel: str, sha: str) -> int:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    types, rules = extract.parse_manual(text, rel)
    run.conn.execute("DELETE FROM rules WHERE source_doc=?", (rel,))
    if not types:
        run.issue("warning", rel, "manual_without_type", "no `well_type = \"...\"` declaration found; no rules extracted")
    _store_doc(run, rel, "manual", text, rel, sha, well_type=types[0] if types else None)
    for r in rules:
        run.conn.execute("INSERT OR REPLACE INTO rules(rule_key,scope_kind,scope_value,value_num,unit,source_doc,source_section,excerpt) VALUES (?,?,?,?,?,?,?,?)",
                         (r.rule_key, r.scope_kind, r.scope_value, r.value_num, r.unit, r.source_doc, r.source_section, r.excerpt))
    run.count("manual", "accepted")
    return len(rules)


def load_sop(run: Run, path: Path, rel: str, sha: str) -> int:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rules = extract.parse_sop(text, rel)
    run.conn.execute("DELETE FROM rules WHERE source_doc=?", (rel,))
    _store_doc(run, rel, "sop", text, rel, sha)
    for r in rules:
        run.conn.execute("INSERT OR REPLACE INTO rules(rule_key,scope_kind,scope_value,value_num,unit,source_doc,source_section,excerpt) VALUES (?,?,?,?,?,?,?,?)",
                         (r.rule_key, r.scope_kind, r.scope_value, r.value_num, r.unit, r.source_doc, r.source_section, r.excerpt))
    run.count("sop", "accepted")
    return len(rules)


def load_report(run: Run, path: Path, rel: str, sha: str) -> int:
    import re
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    m = re.search(r"Maintenance Report\s*[—-]\s*(MNT-[\w-]+)", text)
    mid = m.group(1) if m else None
    am = re.search(r"\*\*Asset:\*\*\s*(\S+)", text)
    aid = am.group(1) if am else (path.parent.parent.name)
    if not mid:
        run.issue("warning", rel, "report_without_id", "could not read maintenance id from report title")
    _store_doc(run, rel, "report", text, rel, sha, asset_id=aid, maintenance_id=mid)
    run.conn.execute("DELETE FROM report_facts WHERE maintenance_id=?", (mid,)) if mid else None
    if mid:
        for f in extract.parse_report(text):
            run.conn.execute("INSERT OR REPLACE INTO report_facts VALUES (?,?,?,?,?,?,?)",
                             (mid, f.fact_key, f.value_num, f.value_text, f.protocol_id, f.excerpt, f.extractor))
    run.count("report", "accepted")
    return 1


# ----------------------------------------------------------------------------- orchestration

TABLE_FOR = {"inspection": "inspections", "maintenance": "maintenance", "regulatory_event": "reg_events", "note": "notes"}


def classify(rel: str) -> str | None:
    p = Path(rel)
    parts = p.parts
    if rel == "fleet/areas.json" or rel == "fleet/users.json":
        return "fleet"
    if len(parts) >= 3 and parts[0] == "fleet" and parts[1] == "well-types" and p.suffix == ".md":
        return "manual"
    if len(parts) >= 3 and parts[0] == "fleet" and parts[1] == "sops" and p.suffix == ".md":
        return "sop"
    if len(parts) >= 3 and parts[0] == "assets":
        if p.name == "metadata.json" and len(parts) == 3:
            return "metadata"
        if p.name == "inspections.jsonl":
            return "inspections"
        if p.name == "maintenance.jsonl":
            return "maintenance"
        if p.name == "regulatory-events.jsonl":
            return "reg_events"
        if p.name == "context.jsonl":
            return "notes"
        if len(parts) >= 4 and parts[2] == "maintenance-reports" and p.suffix == ".md":
            return "report"
    return None


ORDER = ["fleet", "manual", "sop", "metadata", "inspections", "maintenance", "reg_events", "notes", "report"]
DELETE_FROM = {"inspections": "inspections", "maintenance": "maintenance", "reg_events": "reg_events", "notes": "notes"}


def _purge_file(conn: sqlite3.Connection, kind: str, rel: str) -> None:
    if kind in DELETE_FROM:
        conn.execute(f"DELETE FROM {DELETE_FROM[kind]} WHERE src_path=?", (rel,))
    elif kind == "metadata":
        ids = [r[0] for r in conn.execute("SELECT asset_id FROM assets WHERE src_path=?", (rel,))]
        for a in ids:
            conn.execute("DELETE FROM equipment WHERE asset_id=?", (a,))
            conn.execute("DELETE FROM assets WHERE asset_id=?", (a,))
    elif kind in ("manual", "sop", "report"):
        conn.execute("DELETE FROM doc_chunks WHERE doc_id=?", (rel,))
        conn.execute("DELETE FROM report_facts WHERE maintenance_id IN (SELECT maintenance_id FROM documents WHERE doc_id=?)", (rel,))
        conn.execute("DELETE FROM documents WHERE doc_id=?", (rel,))
        conn.execute("DELETE FROM rules WHERE source_doc=?", (rel,))


def discover(root: Path) -> dict[str, tuple[str, Path]]:
    found: dict[str, tuple[str, Path]] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        kind = classify(rel)
        if kind:
            found[rel] = (kind, p)
    return found


def ingest(settings: Settings | None = None, conn: sqlite3.Connection | None = None, force: bool = False) -> dict:
    settings = settings or Settings()
    root = Path(settings.dataset_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"dataset directory not found: {root}")
    conn = conn or open_source(settings)
    run = Run(conn, root)
    found = discover(root)
    known = {r["path"]: r for r in conn.execute("SELECT * FROM ingest_files")}

    # removed files
    for rel, row in known.items():
        if rel not in found:
            _purge_file(conn, row["kind"], rel)
            conn.execute("DELETE FROM ingest_files WHERE path=?", (rel,))
            run.files["removed"] += 1
            run.issue("info", rel, "file_removed", "file no longer present; its records were removed")

    todo: list[tuple[str, str, Path, str, bool]] = []
    for rel, (kind, p) in found.items():
        sha = sha256_file(p)
        prev = known.get(rel)
        if prev and prev["sha256"] == sha and not force:
            run.files["unchanged"] += 1
            conn.execute("UPDATE ingest_files SET last_seen_run=? WHERE path=?", (run.id, rel))
            continue
        todo.append((rel, kind, p, sha, prev is not None))
    todo.sort(key=lambda t: (ORDER.index(t[1]), t[0]))

    loaders = {
        "fleet": lambda p, rel, sha: load_fleet_json(run, p, rel),
        "manual": lambda p, rel, sha: load_manual(run, p, rel, sha),
        "sop": lambda p, rel, sha: load_sop(run, p, rel, sha),
        "metadata": lambda p, rel, sha: load_metadata(run, p, rel),
        "inspections": lambda p, rel, sha: load_inspections(run, p, rel),
        "maintenance": lambda p, rel, sha: load_maintenance(run, p, rel),
        "reg_events": lambda p, rel, sha: load_reg_events(run, p, rel),
        "notes": lambda p, rel, sha: load_notes(run, p, rel),
        "report": lambda p, rel, sha: load_report(run, p, rel, sha),
    }
    for rel, kind, p, sha, existed in todo:
        _purge_file(conn, kind, rel)
        run.files["changed" if existed else "added"] += 1
        try:
            n = loaders[kind](p, rel, sha)
        except Exception as e:  # a bad file must not abort the run
            log.exception("failed to ingest %s", rel)
            run.issue("error", rel, "loader_exception", f"{type(e).__name__}: {e}")
            n = 0
        conn.execute("INSERT OR REPLACE INTO ingest_files VALUES (?,?,?,?,?,?,?)",
                     (rel, kind, sha, p.stat().st_size, n, run.id, now_iso()))
    conn.commit()
    _post_checks(run, settings)
    summary = {
        "run_id": run.id, "dataset_dir": str(root), "files": run.files, "records": run.counts,
        "as_of": conn.execute("SELECT value FROM meta WHERE key='as_of'").fetchone()[0],
        "issues": {r["severity"]: r["n"] for r in conn.execute(
            "SELECT severity, COUNT(*) n FROM ingest_issues WHERE run_id=? GROUP BY severity", (run.id,))},
    }
    conn.execute("UPDATE ingest_runs SET finished_at=?, summary_json=? WHERE id=?", (now_iso(), json.dumps(summary), run.id))
    conn.commit()
    return summary


# Plausible ranges for extracted thresholds (lo, hi, unit). A value outside is almost certainly a parsing slip or a typo in a
# document: it is quarantined (status 'rejected', never applied) and reported, not guessed around.
RULE_RANGES: dict[str, tuple[float, float]] = {
    "production.min_delta_c": (0.5, 100), "temp_alarm_c": (30, 400), "spill.shutin_pad_pct": (0.1, 100), "rca.min_corrective_events": (1, 50), "rca.window_months": (1, 120),
    "downtime.report_after_days": (1, 365), "sr01.report_hours": (0.25, 720), "sr01.major_report_hours": (0.25, 720), "sr02.contain_hours": (0.25, 720),
    "sr03.tph_target_pct": (0.01, 100), "sr03.min_soil_samples": (1, 100), "sr03.verify_days": (1, 730), "rc01.max_inspection_gap_days": (1, 365),
    "rc03.report_after_days": (1, 365), "rc04.nov_response_days": (1, 365), "spill.moderate_hse_notice_hours": (0.25, 720),
    "spill.class.moderate_min_bbl": (0.01, 100000), "spill.class.reportable_min_bbl": (0.01, 100000), "spill.class.major_min_bbl": (0.01, 100000),
}


def _validate_rules(run: Run) -> None:
    c = run.conn
    c.execute("UPDATE rules SET status='ok', status_reason=NULL")
    for r in c.execute("SELECT * FROM rules WHERE rule_key!='inherits_from'").fetchall():
        rng = RULE_RANGES.get(r["rule_key"])
        if rng and r["value_num"] is not None and not (rng[0] <= r["value_num"] <= rng[1]):
            why = f"{r['value_num']:g} {r['unit'] or ''} is outside the plausible range {rng[0]:g}..{rng[1]:g}".replace("  ", " ")
            c.execute("UPDATE rules SET status='rejected', status_reason=? WHERE rule_key=? AND scope_kind=? AND scope_value=? AND source_doc=?", (why, r["rule_key"], r["scope_kind"], r["scope_value"], r["source_doc"]))
            run.issue("error", r["source_doc"], "rule_out_of_range", f"{r['rule_key']} ({r['scope_kind']} {r['scope_value']}) = {why}; the rule was not applied. Passage: {r['excerpt'][:120]}")
    cls = {r["rule_key"]: r["value_num"] for r in c.execute("SELECT rule_key, value_num FROM rules WHERE status='ok' AND rule_key LIKE 'spill.class.%'")}
    ordered = [cls.get(k) for k in ("spill.class.moderate_min_bbl", "spill.class.reportable_min_bbl", "spill.class.major_min_bbl")]
    present = [v for v in ordered if v is not None]
    if len(present) >= 2 and present != sorted(set(present)):
        c.execute("UPDATE rules SET status='rejected', status_reason='spill class thresholds are not strictly increasing' WHERE rule_key LIKE 'spill.class.%'")
        run.issue("error", "fleet/sops", "inconsistent_spill_classes", f"spill class thresholds {present} are not strictly increasing; spill classification is unavailable until the SOP is fixed")
    groups: dict[tuple, dict[str, float]] = {}
    for r in c.execute("SELECT rule_key, scope_kind, scope_value, source_doc, value_num FROM rules WHERE status='ok' AND rule_key!='inherits_from' ORDER BY source_doc"):
        groups.setdefault((r["rule_key"], r["scope_kind"], r["scope_value"]), {})[r["source_doc"]] = r["value_num"]
    for (key, kind, scope), docs in groups.items():
        if len(set(docs.values())) > 1:
            run.issue("error", ", ".join(docs), "conflicting_rule", f"documents disagree about {key} ({kind} {scope}): " + "; ".join(f"{d} says {v:g}" for d, v in docs.items())
                      + ". The rule is unavailable until the documents are reconciled.")


def _has_rule(c: sqlite3.Connection, key: str, well_type: str) -> bool:
    seen, wt = set(), well_type
    while wt and wt not in seen:
        seen.add(wt)
        if c.execute("SELECT 1 FROM rules WHERE rule_key=? AND scope_kind='well_type' AND scope_value=? AND status='ok'", (key, wt)).fetchone():
            return True
        p = c.execute("SELECT excerpt FROM rules WHERE rule_key='inherits_from' AND scope_value=?", (wt,)).fetchone()
        wt = p["excerpt"] if p else None
    return False


def _production_rule_coverage(run: Run) -> None:
    """Every well type needs a readable 'producing when the wellhead is N degC above ambient' rule, or its wells' status is unknown."""
    c = run.conn
    for r in c.execute("SELECT DISTINCT well_type FROM documents WHERE kind='manual' AND well_type IS NOT NULL").fetchall():
        wt = r[0]
        if not _has_rule(c, "production.min_delta_c", wt):
            n = c.execute("SELECT COUNT(*) FROM assets WHERE well_type=?", (wt,)).fetchone()[0]
            run.issue("warning", f"fleet/well-types ({wt})", "no_production_rule",
                      f"the manual for well type {wt} has no recognisable production-temperature rule (expected wording like 'at least N °C above ambient'); "
                      f"production status is unknown for {n} wells of this type until the manual is readable")


def _post_checks(run: Run, settings: Settings) -> None:
    c = run.conn
    # "today": explicit override, else the day after the newest inspection (documented assumption)
    if settings.today_override:
        as_of, basis = settings.today_override, "COPILOT_TODAY override"
    else:
        r = c.execute("SELECT MAX(inspected_at) FROM inspections").fetchone()[0]
        if r:
            d = datetime.strptime(r[:10], "%Y-%m-%d").date() + timedelta(days=1)
            as_of, basis = d.isoformat(), "inferred: day after newest inspection"
        else:
            as_of, basis = date.today().isoformat(), "system clock (no inspections loaded)"
    c.execute("INSERT OR REPLACE INTO meta VALUES ('as_of', ?)", (as_of,))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('as_of_basis', ?)", (basis,))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest_at', ?)", (now_iso(),))

    # referential integrity (warnings; records stay but the link is flagged)
    for table, col, target, tcol in [
        ("maintenance", "triggered_by_inspection_id", "inspections", "inspection_id"),
        ("reg_events", "related_inspection_id", "inspections", "inspection_id"),
        ("notes", "inspection_id", "inspections", "inspection_id"),
    ]:
        for r in c.execute(f"SELECT {table}.{_pk(table)} AS id, {table}.{col} AS ref, src_path FROM {table} "
                           f"WHERE {col} IS NOT NULL AND {col} NOT IN (SELECT {tcol} FROM {target})"):
            run.issue("warning", r["src_path"], "dangling_reference", f"{table} {r['id']} references missing {col} {r['ref']}")
    for r in c.execute("SELECT maintenance_id, report_file, asset_id, src_path FROM maintenance WHERE report_file IS NOT NULL"):
        rel = f"assets/{r['asset_id']}/{r['report_file']}"
        if not c.execute("SELECT 1 FROM documents WHERE path=?", (rel,)).fetchone():
            run.issue("warning", r["src_path"], "missing_report", f"{r['maintenance_id']} points to missing report {r['report_file']}")
    # stale metadata pointer
    for r in c.execute("""SELECT a.asset_id, a.last_inspection_id lid, a.src_path,
                                 (SELECT inspection_id FROM inspections i WHERE i.asset_id=a.asset_id ORDER BY inspected_at DESC LIMIT 1) real_id
                          FROM assets a"""):
        if r["real_id"] and r["lid"] != r["real_id"]:
            run.issue("warning", r["src_path"], "stale_last_inspection", f"{r['asset_id']}: metadata last_inspection {r['lid']} differs from newest inspection {r['real_id']}")
    _validate_rules(run)
    _production_rule_coverage(run)
    # rule coverage per well type
    types = [r[0] for r in c.execute("SELECT DISTINCT well_type FROM assets")]
    for wt in types:
        if not c.execute("SELECT 1 FROM documents WHERE kind='manual' AND well_type=?", (wt,)).fetchone():
            run.issue("warning", "fleet/well-types", "no_manual", f"well type {wt} has no manual; production status cannot be derived for these wells")
    c.commit()
