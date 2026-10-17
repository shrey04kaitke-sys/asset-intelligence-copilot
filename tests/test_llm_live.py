"""LIVE tests against the real hosted Gemma 4 API (real key, real network, real inference).

Opt-in: set COPILOT_LIVE=1 (key from the OS credential store, set up with scripts/setup_gemini_key.py). When enabled, a missing key or any failed required step FAILS the test (it never
skips silently). Without COPILOT_LIVE=1 this module reports one explicit skip, which is not evidence of anything.
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.live


def test_live_gemma_provider_end_to_end():
    if os.environ.get("COPILOT_LIVE") != "1":
        pytest.skip("LIVE test not run: set COPILOT_LIVE=1 (needs a key from the credential store or GEMINI_API_KEY) to call the real hosted Gemma 4 API")
    from copilot.llm.live_check import run_live_check
    rep = run_live_check()
    summary = [(s.name, s.ok, s.error) for s in rep.steps]
    assert rep.passed(), f"live verification {rep.verdict}: {json.dumps(summary, default=str)[:1500]}"
