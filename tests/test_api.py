"""MOCKED tests for the HTTP layer: a scripted provider stands in for Gemma; everything else is the real application.

They check status-code mapping, validation, headers, privacy and that no internals leak. They say nothing about model quality.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from copilot.api import LazyProvider, create_app
from copilot.config import Settings
from copilot.llm.base import LLMError
from copilot.orchestrator import Orchestrator, OrchestratorConfig
from copilot.tools import build_registry

from .conftest import JESSICA, MICHAEL
from .test_orchestrator import DOWNTIME, STATUS, ScriptedProvider, mk

REF = "inspection:INS-0001-0203"


@pytest.fixture(scope="session")
def registry():
    return build_registry()


def client(dom, fb, state, registry, script, **cfg):
    prov = ScriptedProvider(script)
    app = create_app(Orchestrator(dom, state, fb, registry, prov, OrchestratorConfig(**cfg)))
    return TestClient(app, raise_server_exceptions=False), prov


def post(c, question="Is WELL-0001 producing oil right now?", user=MICHAEL, **kw):
    return c.post("/ask", json={"user_id": user, "question": question, **kw})


def test_health_users_tools(dom, fb, state, registry):
    c, _ = client(dom, fb, state, registry, [])
    h = c.get("/health").json()
    assert h["status"] == "ok" and h["wells"] == 200 and h["data_date"] == "2026-10-16" and h["tools"] == 22 and h["model"] == "scripted-gemma"
    assert "decision support only" in h["note"] and "AIza" not in json.dumps(h)
    users = c.get("/users").json()
    assert len(users) == 15 and {u["user_id"] for u in users} >= {MICHAEL, JESSICA} and set(users[0]) == {"user_id", "name", "role", "team_id"}
    tools = {t["name"]: t for t in c.get("/tools").json()}
    assert len(tools) == 22 and tools["record_correction"]["records_data"] is True and tools["get_asset_status"]["records_data"] is False


def test_ask_success_returns_the_structured_contract(dom, fb, state, registry):
    c, _ = client(dom, fb, state, registry, [mk(calls=[STATUS]), mk(text=f"WELL-0001 is not producing oil [{REF}].\n[visual: card]")])
    r = post(c)
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] and b["answer"] == f"WELL-0001 is not producing oil [{REF}]." and b["citations"][0]["ref"] == REF and b["visual"]["type"] == "card"
    assert set(b) >= {"conversation_id", "uncertainty", "conflicts", "assumptions", "warnings", "needs_confirmation", "pending_actions", "data", "trace", "usage", "error"}
    assert b["conflicts"] and b["error"] is None


@pytest.mark.parametrize("body", [{"user_id": MICHAEL, "question": ""}, {"user_id": MICHAEL}, {"question": "hi"}, {"user_id": "bad id", "question": "hi"},
                                  {"user_id": MICHAEL, "question": "x" * 2001}, {"user_id": MICHAEL, "question": "hi", "extra": 1}, {"user_id": MICHAEL, "question": "hi", "visual": "hologram"}])
def test_invalid_requests_are_rejected_before_any_work(dom, fb, state, registry, body):
    c, prov = client(dom, fb, state, registry, [mk(text="never")])
    r = c.post("/ask", json=body)
    assert r.status_code == 422 and prov.calls == []


def test_non_json_and_wrong_methods(dom, fb, state, registry):
    c, _ = client(dom, fb, state, registry, [])
    assert c.post("/ask", content="not json", headers={"content-type": "application/json"}).status_code == 422
    assert c.get("/ask").status_code == 405 and c.get("/nope").status_code == 404


@pytest.mark.parametrize("err,status,code", [
    (LLMError("rate_limit", "slow down"), 429, "rate_limited"), (LLMError("server", "500"), 503, "llm_unavailable"), (LLMError("unconfigured", "no key"), 503, "llm_not_configured"),
    (LLMError("auth", "rejected"), 503, "llm_auth"), (LLMError("truncated", "cut off"), 502, "truncated"), (LLMError("blocked", "safety"), 422, "blocked"),
])
def test_provider_failures_map_to_http_status(dom, fb, state, registry, err, status, code):
    c, _ = client(dom, fb, state, registry, [err])
    r = post(c)
    assert r.status_code == status and r.json()["ok"] is False and r.json()["error"]["code"] == code and r.json()["answer"] is None


def test_rate_limit_carries_retry_after_header(dom, fb, state, registry):
    err = LLMError("rate_limit", "limit", retry_after_s=63.2)
    c, _ = client(dom, fb, state, registry, [err])
    r = post(c)
    assert r.status_code == 429 and r.headers["retry-after"] == "64" and r.json()["error"]["retry_after_s"] == 63.2


def test_unknown_user_and_foreign_conversation(dom, fb, state, registry):
    c, prov = client(dom, fb, state, registry, [mk(text="a")])
    assert post(c, user="U-999").status_code == 404 and prov.calls == []
    conv = post(c).json()["conversation_id"]
    c2, _ = client(dom, fb, state, registry, [mk(text="never")])
    r = post(c2, user=JESSICA, conversation_id=conv)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


def test_conversation_messages_are_owner_only(dom, fb, state, registry):
    c, _ = client(dom, fb, state, registry, [mk(text="hello back")])
    conv = post(c, "hi there friend").json()["conversation_id"]
    ok = c.get(f"/conversations/{conv}/messages", params={"user_id": MICHAEL})
    assert ok.status_code == 200 and [m["role"] for m in ok.json()] == ["user", "assistant"]
    assert c.get(f"/conversations/{conv}/messages", params={"user_id": JESSICA}).status_code == 403
    assert c.get("/conversations/CONV-nope/messages", params={"user_id": MICHAEL}).status_code == 404
    assert c.get(f"/conversations/{conv}/messages", params={"user_id": "U-999"}).status_code == 404
    assert c.get(f"/conversations/{conv}/messages").status_code == 422


def test_confirmation_endpoint(dom, fb, state, registry):
    rule = ("record_rule_override", {"rule_key": "rca.min_corrective_events", "value": 2, "scope": "well_type", "scope_value": "T350", "reason": "team policy", "quote": "after 2 repairs"})
    c, _ = client(dom, fb, state, registry, [mk(calls=[rule]), mk(text="pending your confirmation")], require_confirmation_for=frozenset({"record_rule_override"}))
    pid = post(c, "Our team wants a pump jack analysis after 2 repairs for all T350 wells.").json()["pending_actions"][0]["proposal_id"]
    assert dom.rule("rca.min_corrective_events", "T350").value == 3.0
    bad = c.post("/confirm", json={"user_id": JESSICA, "proposal_id": pid})
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "not_found" and dom.rule("rca.min_corrective_events", "T350").value == 3.0
    good = c.post("/confirm", json={"user_id": MICHAEL, "proposal_id": pid})
    assert good.status_code == 200 and good.json()["status"] == "executed" and dom.rule("rca.min_corrective_events", "T350").value == 2.0
    assert c.post("/confirm", json={"user_id": MICHAEL, "proposal_id": pid}).status_code == 404
    assert c.post("/confirm", json={"user_id": MICHAEL, "proposal_id": "../etc"}).status_code == 422


def test_unexpected_server_errors_return_generic_json(dom, fb, state, registry):
    c, _ = client(dom, fb, state, registry, [RuntimeError(r"C:\secret\db sqlite AIzaSyLEAK")])
    r = post(c)
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert not any(x in r.text for x in ("secret", "sqlite", "AIza", "Traceback", "RuntimeError"))


def test_cors_allows_only_local_frontends(dom, fb, state, registry):
    c, _ = client(dom, fb, state, registry, [])
    ok = c.get("/health", headers={"Origin": "http://localhost:5173"})
    bad = c.get("/health", headers={"Origin": "https://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173" and "access-control-allow-origin" not in bad.headers


def test_server_starts_and_reports_health_without_a_model_key(dom, fb, state, registry, monkeypatch):
    """The real LazyProvider with no key: health works, /ask fails clearly with 503 and a safe message (nothing is called)."""
    for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    o = Orchestrator(dom, state, fb, registry, LazyProvider(Settings()), OrchestratorConfig())
    c = TestClient(create_app(o), raise_server_exceptions=False)
    h = c.get("/health").json()
    assert h["status"] == "ok" and h["model_key_available"] is False
    r = post(c)
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_not_configured" and "setup_gemini_key.py" in r.json()["error"]["message"]


def test_openapi_documents_the_contract(dom, fb, state, registry):
    c, _ = client(dom, fb, state, registry, [])
    spec = c.get("/openapi.json").json()
    assert {"/ask", "/confirm", "/health", "/users", "/tools", "/conversations/{conversation_id}/messages"} <= set(spec["paths"])
    assert "AskRequest" in spec["components"]["schemas"] and "AskResponse" in spec["components"]["schemas"]
