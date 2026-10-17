"""Persistent user state: corrections, claims, conversations, private memory, audit log.

Nothing here modifies ingested source rows. Every mutation writes an audit entry in the same
transaction.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateStore:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str | None, action: str, object_type: str, object_id: str, detail: dict | None = None) -> None:
        self.c.execute(
            "INSERT INTO audit_log(at, actor_user_id, action, object_type, object_id, detail_json) VALUES (?,?,?,?,?,?)",
            (_now(), actor, action, object_type, object_id, json.dumps(detail or {}, default=str)),
        )

    def audit_trail(self, object_type: str | None = None, object_id: str | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT * FROM audit_log", []
        conds = []
        if object_type:
            conds.append("object_type=?"); args.append(object_type)
        if object_id:
            conds.append("object_id=?"); args.append(object_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.c.execute(q, args)]

    # ------------------------------------------------------------------ corrections
    def _next_id(self, prefix: str, table: str, col: str) -> str:
        n = self.c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1
        while True:
            cid = f"{prefix}-{n:04d}"
            if not self.c.execute(f"SELECT 1 FROM {table} WHERE {col}=?", (cid,)).fetchone():
                return cid
            n += 1

    def add_correction(self, *, kind: str, target_type: str, target_id: str, field: str, original_value: Any,
                       new_value: Any, scope_kind: str, scope_value: str, author_user_id: str, reason: str | None,
                       conflicts: list[dict] | None = None, supersedes_id: str | None = None) -> dict:
        cid = self._next_id("COR", "corrections", "correction_id")
        with self.c:
            if supersedes_id:
                self.c.execute("UPDATE corrections SET status='superseded' WHERE correction_id=?", (supersedes_id,))
            self.c.execute(
                "INSERT INTO corrections(correction_id,kind,target_type,target_id,field,original_value,new_value,scope_kind,"
                "scope_value,author_user_id,reason,created_at,status,supersedes_id,conflicts_with_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, kind, target_type, target_id, field, _j(original_value), _j(new_value), scope_kind, scope_value,
                 author_user_id, reason, _now(), "active", supersedes_id, json.dumps(conflicts or [])),
            )
            self.audit(author_user_id, "correction_created", "correction", cid,
                       {"kind": kind, "target": f"{target_type}:{target_id}", "field": field, "from": original_value,
                        "to": new_value, "scope": f"{scope_kind}:{scope_value}", "reason": reason,
                        "supersedes": supersedes_id, "conflicts": conflicts or []})
        return self.get_correction(cid)

    def get_correction(self, cid: str) -> dict | None:
        r = self.c.execute("SELECT * FROM corrections WHERE correction_id=?", (cid,)).fetchone()
        return _corr(r) if r else None

    def withdraw_correction(self, cid: str, actor: str, reason: str | None) -> dict | None:
        cur = self.get_correction(cid)
        if not cur or cur["status"] != "active":
            return cur
        with self.c:
            self.c.execute("UPDATE corrections SET status='withdrawn' WHERE correction_id=?", (cid,))
            self.audit(actor, "correction_withdrawn", "correction", cid, {"reason": reason})
        return self.get_correction(cid)

    def corrections(self, *, target_type: str | None = None, target_id: str | None = None, field: str | None = None,
                    kind: str | None = None, active_only: bool = True) -> list[dict]:
        q, args = "SELECT * FROM corrections WHERE 1=1", []
        for col, v in (("target_type", target_type), ("target_id", target_id), ("field", field), ("kind", kind)):
            if v is not None:
                q += f" AND {col}=?"; args.append(v)
        if active_only:
            q += " AND status='active'"
        q += " ORDER BY created_at, correction_id"
        return [_corr(r) for r in self.c.execute(q, args)]

    # ------------------------------------------------------------------ claims
    def add_claim(self, *, asset_id: str, author_user_id: str, text: str, claim_type: str, valid_from: str | None,
                  valid_to: str | None, visibility: str, verification: str = "unverified",
                  verification_detail: str | None = None) -> dict:
        cid = self._next_id("CLM", "claims", "claim_id")
        with self.c:
            self.c.execute(
                "INSERT INTO claims(claim_id,asset_id,author_user_id,text,claim_type,valid_from,valid_to,visibility,verification,"
                "verification_detail,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, asset_id, author_user_id, text, claim_type, valid_from, valid_to, visibility, verification,
                 verification_detail, _now()))
            self.audit(author_user_id, "claim_created", "claim", cid,
                       {"asset": asset_id, "type": claim_type, "visibility": visibility, "verification": verification,
                        "detail": verification_detail})
        return dict(self.c.execute("SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone())

    def claims(self, asset_id: str, viewer_user_id: str | None) -> list[dict]:
        """Shared claims plus the viewer's own private claims. Others' private claims never leave here."""
        rows = self.c.execute(
            "SELECT * FROM claims WHERE asset_id=? AND (visibility='shared' OR author_user_id=?) ORDER BY created_at",
            (asset_id, viewer_user_id or ""))
        return [dict(r) for r in rows]

    def set_claim_verification(self, claim_id: str, verification: str, detail: str | None, actor: str | None) -> None:
        with self.c:
            self.c.execute("UPDATE claims SET verification=?, verification_detail=? WHERE claim_id=?", (verification, detail, claim_id))
            self.audit(actor, "claim_verification_changed", "claim", claim_id, {"verification": verification, "detail": detail})

    # ------------------------------------------------------------------ conversations & memory
    def ensure_conversation(self, user_id: str, conversation_id: str | None, title: str | None = None) -> str:
        if conversation_id:
            r = self.c.execute("SELECT user_id FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if r:
                if r["user_id"] != user_id:
                    raise PermissionError("conversation belongs to another user")
                return conversation_id
        cid = conversation_id or f"CONV-{uuid.uuid4().hex[:10]}"
        with self.c:
            self.c.execute("INSERT INTO conversations VALUES (?,?,?,?)", (cid, user_id, title, _now()))
        return cid

    def add_message(self, conversation_id: str, user_id: str, role: str, content: str, response: dict | None = None) -> int:
        with self.c:
            cur = self.c.execute(
                "INSERT INTO messages(conversation_id,user_id,role,content,response_json,created_at) VALUES (?,?,?,?,?,?)",
                (conversation_id, user_id, role, content, json.dumps(response, default=str) if response else None, _now()))
        return cur.lastrowid

    def history(self, conversation_id: str, user_id: str, limit: int = 12) -> list[dict]:
        rows = self.c.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (conversation_id, user_id, limit))
        return [dict(r) for r in reversed(rows.fetchall())]

    def conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        """A user's own conversations, newest first (never anyone else's)."""
        rows = self.c.execute("SELECT conversation_id, title, started_at FROM conversations WHERE user_id=? ORDER BY started_at DESC, rowid DESC LIMIT ?", (user_id, limit))
        return [dict(r) for r in rows]

    def messages(self, conversation_id: str, user_id: str, limit: int = 100) -> list[dict]:
        """Messages of one of the user's conversations, oldest first, with the stored structured response of assistant turns."""
        rows = self.c.execute("SELECT role, content, response_json, created_at FROM messages WHERE conversation_id=? AND user_id=? ORDER BY id LIMIT ?", (conversation_id, user_id, limit))
        out = []
        for r in rows:
            d = {"role": r["role"], "content": r["content"], "created_at": r["created_at"], "response": None}
            if r["response_json"]:
                try:
                    d["response"] = json.loads(r["response_json"])
                except ValueError:
                    pass
            out.append(d)
        return out

    def remember(self, *, user_id: str, conversation_id: str | None, topic: str, question: str, entity_ids: list[str],
                 window: dict | None, tool_name: str) -> None:
        with self.c:
            self.c.execute(
                "INSERT INTO user_memory(user_id,conversation_id,topic,question,entity_ids_json,window_json,tool_name,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, conversation_id, topic, question, json.dumps(entity_ids), json.dumps(window or {}), tool_name, _now()))

    def recall(self, user_id: str, *, before: str | None = None, since: str | None = None, limit: int = 20) -> list[dict]:
        """Only ever reads rows for this user_id."""
        q, args = "SELECT * FROM user_memory WHERE user_id=?", [user_id]
        if before:
            q += " AND created_at < ?"; args.append(before)
        if since:
            q += " AND created_at >= ?"; args.append(since)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        out = []
        for r in self.c.execute(q, args):
            d = dict(r)
            d["entity_ids"] = json.loads(d.pop("entity_ids_json") or "[]")
            d["window"] = json.loads(d.pop("window_json") or "{}")
            out.append(d)
        return out


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v)


def _corr(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("original_value", "new_value", "conflicts_with_json"):
        if d.get(k) is not None:
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    d["conflicts"] = d.pop("conflicts_with_json", None) or []
    return d
