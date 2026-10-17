"""Thin HTTP interface over the orchestrator.

    python -m copilot.api            # serves on http://127.0.0.1:8000 (docs at /docs)

Honest scope
* The caller states who they are (`user_id`). This is a user *picker* as the challenge allows, NOT authentication; the
  server binds to localhost by default and must not be exposed to a network without real authentication in front of it.
* Requests are processed one at a time: the database connections are shared and the hosted model is rate limited anyway.
* The server starts, and `/health` reports status, even when no model key is configured; `/ask` then returns a clear 503.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import ROOT, Settings
from .credentials import CredentialStoreError
from .db import open_source, open_state
from .domain import Domain
from .feedback import Feedback, FeedbackError
from .views import Views, ViewError
from .llm.base import LLMError, LLMResponse, Turn
from .orchestrator import AskRequest, AskResponse, ConfirmResponse, Orchestrator, OrchestratorConfig
from .state import StateStore
from .tools import ToolRegistry, build_registry

log = logging.getLogger("copilot.api")

_STATUS = {"unknown_user": 404, "forbidden": 403, "rate_limited": 429, "llm_unavailable": 503, "llm_not_configured": 503, "llm_auth": 503, "llm_model_not_found": 503,
           "truncated": 502, "llm_empty_response": 502, "llm_error": 502, "blocked": 422, "internal_error": 500}


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    proposal_id: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    approve: bool = True


UserId = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = UserId
    field: str = Field(min_length=1, max_length=40)
    new_value: float | bool
    reason: str = Field(min_length=3, max_length=300)
    inspection_id: str | None = Field(None, max_length=40)
    asset_id: str | None = Field(None, max_length=40)
    date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    confirmed: bool = False


class ClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = UserId
    asset_id: str = Field(min_length=2, max_length=40)
    text: str = Field(min_length=3, max_length=500)
    claim_type: str = Field(min_length=3, max_length=30)
    valid_from: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_to: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    visibility: str = Field("shared", pattern=r"^(shared|private)$")


class RuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = UserId
    rule_key: str = Field(min_length=3, max_length=60)
    value: float
    scope: str = Field(pattern=r"^(well_type|asset)$")
    scope_value: str = Field(min_length=2, max_length=30)
    reason: str = Field(min_length=3, max_length=300)
    confirmed: bool = False


class WithdrawBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = UserId
    correction_id: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    reason: str | None = Field(None, max_length=300)
    confirmed: bool = False


def _feedback_status(msg: str) -> int:
    low = msg.lower()
    if low.startswith("unknown") or "not found" in low:
        return 404
    if "cannot change" in low or "only the author" in low or "needs supervisor" in low:
        return 403
    return 400


class LazyProvider:
    """Builds the real provider on first use, so the server can start (and report health) without a key."""
    name = "gemma_api"

    def __init__(self, settings: Settings):
        self.settings, self._p = settings, None
        self.model = settings.model

    def generate(self, turns: list[Turn], **kw) -> LLMResponse:
        if self._p is None:
            from .llm import get_provider
            self._p = get_provider(self.settings)              # raises LLMError('unconfigured', ...) with a safe message
        return self._p.generate(turns, **kw)


def create_app(orchestrator: Orchestrator | None = None, settings: Settings | None = None, allowed_origins: list[str] | None = None,
               web_dir: Path | None = None) -> FastAPI:
    settings = settings or Settings()
    if orchestrator is None:
        src, st = open_source(settings), StateStore(open_state(settings))
        dom = Domain(src, st)
        orchestrator = Orchestrator(dom, st, Feedback(dom), build_registry(), LazyProvider(settings), OrchestratorConfig())
    o = orchestrator
    lock = threading.Lock()                                     # one question at a time (shared SQLite connections, rate-limited model)
    app = FastAPI(title="Asset Intelligence Copilot", version="0.1.0",
                  description="Evidence-grounded decision support for oil-well asset data. Not an operational control system.")
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins or ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["GET", "POST"], allow_headers=["content-type"], allow_credentials=False)

    views = Views(o.domain, o.registry, o.feedback)
    web = Path(web_dir) if web_dir else ROOT / "web"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
            resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return resp

    @app.exception_handler(ViewError)
    async def _view_error(_: Request, exc: ViewError):
        return JSONResponse(status_code=exc.status, content={"ok": False, "error": {"code": "not_found" if exc.status == 404 else "invalid_request", "message": exc.message}})

    def _viewer(user_id: str | None) -> str | None:
        if user_id is not None and user_id not in o.domain.users():
            raise ViewError(404, "unknown user")
        return user_id

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"ok": False, "error": {"code": "internal_error", "message": "the request could not be completed; the failure was logged"}})

    @app.get("/health")
    def health() -> dict[str, Any]:
        wells = o.domain.src.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        try:
            key_source = settings.api_key_source()
        except CredentialStoreError as e:
            key_source = None
            store = e.message
        else:
            store = "ok"
        return {"status": "ok" if wells else "no_data", "data_loaded": wells > 0, "wells": wells, "data_date": o.domain.as_of.isoformat(), "model": o.provider.model,
                "model_key_available": key_source is not None, "credential_store": store, "tools": len(o.registry.names),
                "rule_conflicts": len(o.domain.rule_conflicts()), "rules_rejected": len(o.domain.rule_rejections()),
                "note": "decision support only; no live drone, sensor or control integration"}

    @app.get("/users")
    def users() -> list[dict[str, Any]]:
        return [{"user_id": u["user_id"], "name": u["name"], "role": u["role"], "team_id": u["team_id"]} for u in o.domain.users().values()]

    @app.get("/tools")
    def tools() -> list[dict[str, Any]]:
        return [{"name": n, "description": o.registry.get(n).description, "records_data": o.registry.get(n).mutating} for n in o.registry.names]

    @app.post("/ask", response_model=AskResponse)
    def ask(req: AskRequest, response: Response) -> AskResponse:
        with lock:
            r = o.ask(req)
        if not r.ok and r.error:
            response.status_code = _STATUS.get(r.error.code, 502)
            if r.error.retry_after_s:
                response.headers["Retry-After"] = str(int(r.error.retry_after_s) + 1)
        return r

    @app.post("/confirm", response_model=ConfirmResponse)
    def confirm(req: ConfirmRequest, response: Response) -> ConfirmResponse:
        with lock:
            r = o.confirm(req.user_id, req.proposal_id, req.approve)
        if not r.ok and r.error:
            response.status_code = 404 if r.error.code in ("not_found", "unknown_user") else 400
        return r

    @app.get("/conversations/{conversation_id}/messages")
    def messages(conversation_id: str, user_id: str = Query(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")) -> list[dict[str, Any]]:
        if user_id not in o.domain.users():
            raise HTTPException(404, "unknown user")
        with lock:
            row = o.state.c.execute("SELECT user_id FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "no such conversation")
            if row["user_id"] != user_id:
                raise HTTPException(403, "that conversation belongs to another user")
            return o.state.messages(conversation_id, user_id, limit=100)

    # ------------------------------------------------------------------ workspace read models (same deterministic code as the chat tools)
    UID = Query(None, min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")

    @app.get("/fleet")
    def fleet(area: str | None = Query(None, max_length=80), well_type: str | None = Query(None, max_length=20), period: str = Query("90d", max_length=6), user_id: str | None = UID):
        with lock:
            return views.fleet(area=area, well_type=well_type, period=period, user_id=_viewer(user_id))

    @app.get("/assets")
    def assets(area: str | None = Query(None, max_length=80), well_type: str | None = Query(None, max_length=20), status: str = Query("all", max_length=20), stale: bool = False,
               reason: str | None = Query(None, max_length=10), concern: str | None = Query(None, max_length=20), q: str | None = Query(None, max_length=40),
               sort: str = Query("id", max_length=12), desc: bool = False, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), user_id: str | None = UID):
        with lock:
            return views.assets(area=area, well_type=well_type, status=status, stale=stale, reason=reason, concern=concern, q=q, sort=sort, desc=desc, limit=limit, offset=offset, user_id=_viewer(user_id))

    @app.get("/assets/{asset_id}")
    def asset(asset_id: str, user_id: str | None = UID):
        with lock:
            return views.asset(asset_id, _viewer(user_id))

    @app.get("/evidence")
    def evidence(ref: str = Query(min_length=3, max_length=220), user_id: str | None = UID):
        with lock:
            return views.evidence(ref, _viewer(user_id))

    @app.get("/knowledge")
    def knowledge(user_id: str | None = UID):
        with lock:
            return views.knowledge(_viewer(user_id))

    @app.get("/conversations")
    def conversations(user_id: str = Query(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")):
        if user_id not in o.domain.users():
            raise HTTPException(404, "unknown user")
        with lock:
            return o.state.conversations(user_id)

    @app.get("/examples")
    def examples():
        with lock:
            return views.examples()

    @app.get("/audit")
    def audit(object_type: str | None = Query(None, max_length=30), object_id: str | None = Query(None, max_length=80), limit: int = Query(50, ge=1, le=200), user_id: str | None = UID):
        with lock:
            return views.audit(object_type=object_type, object_id=object_id, viewer=_viewer(user_id), limit=limit)

    @app.get("/corrections")
    def corrections(asset_id: str = Query(min_length=2, max_length=40)):
        with lock:
            return views.corrections(asset_id)

    # ------------------------------------------------------------------ direct feedback forms (no model involved; same service, gates and audit as the chat tools)
    def _need_confirmed(ok: bool, what: str) -> None:
        if not ok:
            raise HTTPException(400, f"{what} needs explicit confirmation (confirmed=true)")

    def _run_feedback(fn):
        try:
            with lock:
                return {"ok": True, "result": fn()}
        except FeedbackError as e:
            raise HTTPException(_feedback_status(str(e)), str(e)) from None

    @app.post("/feedback/correction")
    def post_correction(b: CorrectionBody):
        _need_confirmed(b.confirmed, "recording a correction")
        return _run_feedback(lambda: o.feedback.correct_inspection(user_id=b.user_id, field=b.field, new_value=b.new_value, reason=b.reason, asset_id=b.asset_id, on_date=b.date, inspection_id=b.inspection_id))

    @app.post("/feedback/claim")
    def post_claim(b: ClaimBody):
        return _run_feedback(lambda: o.feedback.add_claim(user_id=b.user_id, asset_id=b.asset_id, text=b.text, claim_type=b.claim_type, valid_from=b.valid_from, valid_to=b.valid_to, visibility=b.visibility))

    @app.post("/feedback/rule")
    def post_rule(b: RuleBody):
        _need_confirmed(b.confirmed, "changing a rule")
        return _run_feedback(lambda: o.feedback.set_rule_override(user_id=b.user_id, rule_key=b.rule_key, value=b.value, scope=b.scope, scope_value=b.scope_value, reason=b.reason))

    @app.post("/feedback/withdraw")
    def post_withdraw(b: WithdrawBody):
        _need_confirmed(b.confirmed, "withdrawing a correction")
        return _run_feedback(lambda: o.feedback.withdraw(b.user_id, b.correction_id, b.reason))

    # ------------------------------------------------------------------ the web UI (static files; no build step)
    if web.is_dir():
        app.mount("/static", StaticFiles(directory=str(web)), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(web / "index.html")

    return app


def main() -> None:                                              # pragma: no cover - thin launcher
    import uvicorn
    s = Settings()
    src = open_source(s)
    if src.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0:
        from .ingest import ingest
        print("loading dataset ...")
        print(ingest(s, src)["records"])
    host = os.environ.get("COPILOT_HOST", "127.0.0.1")
    uvicorn.run(create_app(settings=s), host=host, port=int(os.environ.get("COPILOT_PORT", "8000")))


if __name__ == "__main__":                                       # pragma: no cover
    main()
