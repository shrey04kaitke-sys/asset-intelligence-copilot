"""MOCKED tests for request pacing and rate-limit-aware retry.

A fake SDK client and fake clocks drive the real Pacer and the real provider retry loop. These check policy (honouring
Retry-After, bounded backoff with jitter, no retry for auth/validation errors, serialised requests). They are not evidence
about the real API's limits: those are observed in scripts/run_live_eval.py against the live service.
"""
from __future__ import annotations

import random
import threading
import time
from email.utils import formatdate

import httpx
import pytest
from google.genai import errors

from copilot.config import Settings
from copilot.llm import LLMError, Turn
from copilot.llm.gemma_api import GemmaApiProvider
from copilot.llm.pacing import Pacer, classify_429, retry_after_seconds

from .test_llm_provider import FakeClient, MidRng, api_error, resp

U = [Turn("user", "hi")]


def err429(message="You exceeded your current quota", *, header=None, retry_delay=None, quota_id=None):
    body = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED", "details": []}}
    if retry_delay:
        body["error"]["details"].append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    if quota_id:
        body["error"]["details"].append({"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id, "quotaMetric": "generativelanguage.googleapis.com/x"}]})
    r = httpx.Response(429, headers={"Retry-After": header} if header else {})
    return errors.ClientError(429, body, response=r)


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 6))
        self.t += s


def provider(script, *, clock=None, min_interval=0.0, **kw):
    clock = clock or FakeClock()
    pacer = Pacer(min_interval, clock=clock.now, sleep=clock.sleep)
    kw.setdefault("rng", MidRng())
    p = GemmaApiProvider(Settings(model="gemma-4-26b-a4b-it"), client=FakeClient(script), sleep=clock.sleep, pacer=pacer, **kw)
    p.clock = clock
    return p


OK = resp([{"text": "ok"}])


# ----------------------------------------------------------------------------- reading the API's own delay hints
def test_retry_after_header_forms():
    assert retry_after_seconds(err429(header="7")) == 7
    assert retry_after_seconds(err429(header="1.5")) == 1.5
    assert 18 < retry_after_seconds(err429(header=formatdate(time.time() + 20, usegmt=True))) <= 21          # HTTP-date form
    assert retry_after_seconds(err429()) is None


def test_retry_info_detail_and_message_forms():
    assert retry_after_seconds(err429(retry_delay="34s")) == 34
    assert retry_after_seconds(err429(retry_delay="34.5s")) == 34.5
    assert retry_after_seconds(err429(retry_delay="1m")) == 60
    assert retry_after_seconds(err429(retry_delay="500ms")) == 0.5
    assert retry_after_seconds(err429("Quota exceeded. Please retry in 12.5s.")) == 12.5
    assert retry_after_seconds(RuntimeError("no hints")) is None
    assert retry_after_seconds(err429(header="7", retry_delay="34s")) == 7                                    # header wins


def test_classify_429():
    assert classify_429(err429("Quota exceeded for metric", quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier")) == "daily"
    assert classify_429(err429("Quota exceeded for metric", quota_id="GenerateContentInputTokensPerModelPerMinute-FreeTier")) == "per_minute"
    assert classify_429(err429("You exceeded your current quota, please check your plan and billing details.")) == "unknown"       # "billing" alone proves nothing
    assert classify_429(err429("Too many requests")) == "per_minute"


# ----------------------------------------------------------------------------- honouring and bounding waits
def test_retry_after_is_respected_with_small_positive_jitter():
    p = provider([err429(header="7"), OK], rng=random.Random(1))
    assert p.generate(U).text == "ok"
    assert len(p.clock.sleeps) >= 1
    waited = p.clock.sleeps[0]
    assert 7.0 <= waited <= 7.7                                                                              # never shorter than asked, at most +10%


def test_retry_info_delay_is_respected():
    p = provider([err429(retry_delay="34s"), OK], rng=MidRng())
    r = p.generate(U)
    assert r.attempts == 2 and 34.0 <= p.clock.sleeps[0] <= 37.4


def test_wait_longer_than_allowed_is_not_attempted_and_tells_the_caller_when_to_return():
    p = provider([err429(retry_delay="120s"), OK], max_retry_wait_s=75)
    with pytest.raises(LLMError) as ei:
        p.generate(U)
    e = ei.value
    assert e.kind == "rate_limit" and e.retry_after_s is not None and e.retry_after_s >= 120 and "not retried" in e.message
    assert p.clock.sleeps == [] and len(p._client.models.calls) == 1


def test_total_wait_budget_is_bounded():
    p = provider([err429(retry_delay="6s"), err429(retry_delay="6s"), err429(retry_delay="6s"), OK], max_total_wait_s=10, max_retries=5)
    with pytest.raises(LLMError) as ei:
        p.generate(U)
    assert ei.value.kind == "rate_limit" and len(p._client.models.calls) == 2                                  # waited once (~6s); the second wait would exceed the budget
    assert sum(p.clock.sleeps) <= 10


def test_daily_quota_is_never_retried_but_per_minute_limit_is():
    daily = provider([err429("Quota exceeded", quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier"), OK])
    with pytest.raises(LLMError) as ei:
        daily.generate(U)
    assert ei.value.kind == "quota" and not ei.value.retryable and daily.clock.sleeps == [] and len(daily._client.models.calls) == 1
    minute = provider([err429("Quota exceeded", quota_id="GenerateContentInputTokensPerModelPerMinute-FreeTier", retry_delay="10s"), OK])
    assert minute.generate(U).text == "ok" and len(minute._client.models.calls) == 2


def test_never_retries_indefinitely():
    p = provider([api_error(503, "overloaded", "UNAVAILABLE")] * 20, max_retries=3)
    with pytest.raises(LLMError) as ei:
        p.generate(U)
    assert len(p._client.models.calls) == 4 and ei.value.attempts == 4 and ei.value.kind == "server"


@pytest.mark.parametrize("err,kind", [
    (api_error(401, "API key not valid", "UNAUTHENTICATED"), "auth"), (api_error(403, "denied", "PERMISSION_DENIED"), "auth"),
    (api_error(400, "Invalid JSON payload", "INVALID_ARGUMENT"), "invalid_request"), (api_error(404, "not found", "NOT_FOUND"), "model_not_found"),
])
def test_non_retryable_errors_fail_immediately_without_waiting(err, kind):
    p = provider([err, OK])
    with pytest.raises(LLMError) as ei:
        p.generate(U)
    assert ei.value.kind == kind and p.clock.sleeps == [] and len(p._client.models.calls) == 1


def test_exponential_backoff_with_jitter_is_bounded():
    p = provider([api_error(503, "x", "UNAVAILABLE")] * 7, max_retries=6, rng=random.Random(42), max_retry_wait_s=100, max_total_wait_s=1000)
    with pytest.raises(LLMError):
        p.generate(U)
    bases = [1.5, 3.0, 6.0, 12.0, 24.0, 30.0]                                                                  # doubles, then capped at 30
    delays = [s for s in p.clock.sleeps]
    assert len(delays) == 6
    for d, b in zip(delays, bases):
        assert b * 0.75 <= d <= b * 1.25, (d, b)
    assert len({round(d / b, 3) for d, b in zip(delays, bases)}) > 1                                           # jitter really varies


def test_server_error_then_success_uses_backoff_and_reports_attempts():
    p = provider([api_error(500, "boom", "INTERNAL"), api_error(503, "busy", "UNAVAILABLE"), OK])
    r = p.generate(U)
    assert r.attempts == 3 and p.clock.sleeps == [1.5, 3.0]


# ----------------------------------------------------------------------------- pacer
def test_pacer_enforces_minimum_gap():
    c = FakeClock()
    pc = Pacer(2.0, clock=c.now, sleep=c.sleep)
    with pc.slot():
        pass
    with pc.slot():                                   # immediately after: must wait the full gap
        pass
    assert c.sleeps == [2.0]
    c.t += 10
    with pc.slot():                                   # long idle: no wait
        pass
    assert c.sleeps == [2.0]


def test_pacer_serialises_concurrent_requests():
    pc = Pacer(0.0)
    active, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal active, peak
        with pc.slot():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
    ts = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak == 1                                  # never two requests carrying tool declarations at once


def test_penalty_is_shared_by_every_caller():
    c = FakeClock()
    pc = Pacer(0.0, clock=c.now, sleep=c.sleep)
    pc.penalize(12)
    with pc.slot():
        pass
    assert c.sleeps == [12.0]


def test_interval_widens_after_rate_limits_and_relaxes_after_success():
    c = FakeClock()
    pc = Pacer(1.5, max_interval_s=10, clock=c.now, sleep=c.sleep)
    for _ in range(10):
        pc.widen()
    assert pc.interval == 10                           # capped
    for _ in range(60):
        pc.relax()
    assert pc.interval == 1.5                          # back to the configured gap


def test_provider_widens_pacing_after_a_rate_limit_and_waits_between_requests():
    p = provider([err429(retry_delay="2s"), OK, OK], min_interval=1.5)
    p.generate(U)
    assert p.pacer.interval <= 1.5 * 1.5 + 1.0 and p.pacer.interval >= 1.5 * 0.9                              # widened once, relaxed once after the success
    before = len(p.clock.sleeps)
    p.generate(U)
    assert len(p.clock.sleeps) > before                                                                        # the next request was paced, not fired immediately


def test_rate_limit_error_carries_retry_hint_through_the_orchestrator(dom, fb, state):
    from copilot.orchestrator import AskRequest, Orchestrator
    from copilot.tools import build_registry
    from .conftest import MICHAEL
    p = provider([err429(retry_delay="200s")], max_retry_wait_s=75)
    r = Orchestrator(dom, state, fb, build_registry(), p).ask(AskRequest(user_id=MICHAEL, question="Is WELL-0001 producing oil right now?"))
    assert not r.ok and r.error.code == "rate_limited" and r.error.retry_after_s >= 200
    assert "Traceback" not in r.error.message and "RESOURCE_EXHAUSTED" not in r.error.message.replace("HTTP 429 RESOURCE_EXHAUSTED", "")


# ----------------------------------------------------------------------------- configuration
def test_pacing_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("COPILOT_LLM_MIN_INTERVAL_S", "4.5")
    monkeypatch.setenv("COPILOT_LLM_MAX_RETRIES", "5")
    monkeypatch.setenv("COPILOT_LLM_MAX_RETRY_WAIT_S", "20")
    s = Settings()
    assert (s.llm_min_interval_s, s.llm_max_retries, s.llm_max_retry_wait_s) == (4.5, 5, 20.0)
    p = GemmaApiProvider(Settings(), client=FakeClient([]))
    assert p.max_retries == 5 and p.max_retry_wait_s == 20 and p.pacer.base_interval == 4.5
    monkeypatch.setenv("COPILOT_LLM_MIN_INTERVAL_S", "banana")
    assert Settings().llm_min_interval_s == 1.5                                                                # bad values fall back to defaults
