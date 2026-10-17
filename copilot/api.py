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

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .credentials import CredentialStoreError
from .db import open_source, open_state
from .domain import Domain
from .feedback import Feedback
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


def create_app(orchestrator: Orchestrator | None = None, settings: Settings | None = None, allowed_origins: list[str] | None = None) -> FastAPI:
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
            return o.state.history(conversation_id, user_id, limit=100)

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
