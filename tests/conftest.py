"""Shared fixtures. The sample dataset is ingested once into a temp dir; the dataset itself is read-only."""
from __future__ import annotations

from pathlib import Path

import pytest

from copilot import credentials
from copilot.config import ROOT, Settings
from copilot.db import STATE_DDL, STATE_SCHEMA_VERSION, connect, open_source
from copilot.domain import Domain
from copilot.feedback import Feedback
from copilot.ingest import ingest
from copilot.state import StateStore

SAMPLE = ROOT / "dataset" / "sample-data"

# User IDs from fleet/users.json (names verified against the file in test_users_match_challenge_roster)
MICHAEL, JESSICA, TYLER, JAKE, LAURA, CHRIS, RYAN = "U-201", "U-202", "U-204", "U-102", "U-401", "U-402", "U-104"


@pytest.fixture(scope="session")
def source_conn(tmp_path_factory):
    if not SAMPLE.is_dir():
        pytest.skip("challenge sample dataset not present (clone it into ./dataset)")
    data_dir = tmp_path_factory.mktemp("copilot-data")
    settings = Settings(dataset_dir=SAMPLE, data_dir=data_dir, today_override="2026-10-16")
    conn = open_source(settings)
    summary = ingest(settings, conn)
    assert summary["records"]["inspection"]["rejected"] == 0
    return conn


@pytest.fixture
def state(tmp_path):
    return StateStore(connect(tmp_path / "state.db", STATE_DDL, STATE_SCHEMA_VERSION))


@pytest.fixture
def dom(source_conn, state):
    return Domain(source_conn, state)


@pytest.fixture
def fb(dom):
    return Feedback(dom)


class MemoryStore:
    """In-memory stand-in for the OS credential store, so no unit test ever reads or writes the real one."""

    def __init__(self):
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, value):
        self.data[(service, account)] = value

    def delete_password(self, service, account):
        del self.data[(service, account)]


@pytest.fixture(autouse=True)
def isolated_credentials(request, monkeypatch):
    """Every test starts with an empty in-memory credential store and no key in the environment.

    Tests marked `realstore` (synthetic values only) or `live` (real API, real key) are exempt.
    """
    if request.node.get_closest_marker("realstore") or request.node.get_closest_marker("live"):
        yield None
        return
    store = MemoryStore()
    monkeypatch.setattr(credentials, "_backend", lambda: (store, "keyring.backends.Windows.WinVaultKeyring"))
    for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    yield store
