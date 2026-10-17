"""Credential storage, secret redaction and the interactive setup utility.

All secret-like values here are SYNTHETIC. Nothing in this module contacts Google. Only `test_real_windows_store_*`
touches the real operating-system credential store, and only with a throwaway service name and synthetic value.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from copilot import credentials
from copilot.config import ROOT, Settings
from copilot.credentials import CredentialStoreError
from copilot.llm import LLMError, get_provider
from copilot.llm.gemma_api import KEY_HELP, GemmaApiProvider

from .conftest import MemoryStore
from .test_llm_provider import FakeClient, api_error, prov

REAL_BACKEND = credentials._backend            # captured at import, before the autouse fixture replaces it
SYNTH = "SYNTHETIC-" + "key-for-tests-" + "Zq81xV3mN5"                 # not key-shaped on purpose
SYNTH_AIZA = "AI" + "za" + "Sy" + "-synthetic-" + "0123456789abcdefghij"  # key-shaped, built at runtime
U = None


def load_script():
    spec = importlib.util.spec_from_file_location("setup_gemini_key_under_test", ROOT / "scripts" / "setup_gemini_key.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Driver:
    """Runs the setup script's main() with scripted hidden/plain prompts and captured output."""

    def __init__(self, mod, monkeypatch, capsys, secrets=(), answers=(), interactive=True):
        self.mod, self.capsys = mod, capsys
        self.secret_prompts, self.plain_prompts = [], []
        self._secrets, self._answers = list(secrets), list(answers)

        def fake_getpass(prompt=""):
            self.secret_prompts.append(prompt)
            v = self._secrets.pop(0)
            if isinstance(v, BaseException):
                raise v
            return v

        def fake_input(prompt=""):
            self.plain_prompts.append(prompt)
            return self._answers.pop(0)
        monkeypatch.setattr(mod, "_getpass", fake_getpass)
        monkeypatch.setattr(mod, "_input", fake_input)
        monkeypatch.setattr(mod, "_is_interactive", lambda: interactive)

    def run(self, *argv):
        code = self.mod.main(list(argv))
        out = self.capsys.readouterr()
        self.out = out.out + out.err
        return code


@pytest.fixture
def script():
    return load_script()


# ======================================================================================= backend policy
def fake_backend(module: str, name: str, backends=None):
    cls = type(name, (), {"__module__": module})
    inst = cls()
    if backends is not None:
        inst.backends = backends
    return inst


@pytest.mark.parametrize("module,name", [
    ("keyring.backends.fail", "Keyring"), ("keyring.backends.null", "Keyring"), ("keyrings.alt.file", "PlaintextKeyring"),
    ("keyrings.alt.file", "EncryptedKeyring"), ("some.plugin", "WhateverKeyring"),
])
def test_insecure_or_unknown_backends_are_refused(monkeypatch, tmp_path, module, name):
    import keyring
    monkeypatch.setattr(keyring, "get_keyring", lambda: fake_backend(module, name))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CredentialStoreError) as ei:
        REAL_BACKEND()
    assert "not OS-protected" in ei.value.message and "Nothing was saved" in ei.value.message
    monkeypatch.setattr(credentials, "_backend", REAL_BACKEND)
    for call in (lambda: credentials.set_api_key(SYNTH), lambda: credentials.get_api_key(), lambda: credentials.backend_name(), lambda: credentials.delete_api_key()):
        with pytest.raises(CredentialStoreError):
            call()
    assert list(tmp_path.iterdir()) == [] and SYNTH not in ei.value.message        # no plaintext file written anywhere


def test_secure_backends_and_chainer_unwrapping(monkeypatch):
    import keyring
    secure = fake_backend("keyring.backends.Windows", "WinVaultKeyring")
    plain = fake_backend("keyrings.alt.file", "PlaintextKeyring")
    monkeypatch.setattr(keyring, "get_keyring", lambda: secure)
    assert REAL_BACKEND()[1] == "keyring.backends.Windows.WinVaultKeyring"
    monkeypatch.setattr(keyring, "get_keyring", lambda: fake_backend("keyring.backends.chainer", "ChainerBackend", [secure, plain]))
    assert REAL_BACKEND()[1].endswith("WinVaultKeyring")
    monkeypatch.setattr(keyring, "get_keyring", lambda: fake_backend("keyring.backends.chainer", "ChainerBackend", [plain, secure]))
    with pytest.raises(CredentialStoreError):                                       # the effective (first) backend is insecure
        REAL_BACKEND()


def test_missing_keyring_package_is_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(CredentialStoreError, match="not installed"):
        REAL_BACKEND()


def test_backend_initialisation_failure_is_safe(monkeypatch):
    import keyring

    def boom():
        raise RuntimeError("synthetic internal detail " + SYNTH)
    monkeypatch.setattr(keyring, "get_keyring", boom)
    with pytest.raises(CredentialStoreError) as ei:
        REAL_BACKEND()
    assert SYNTH not in ei.value.message and "synthetic internal detail" not in ei.value.message


# ======================================================================================= store operations
def test_store_round_trip_and_cleaning(isolated_credentials):
    assert credentials.get_api_key() is None
    credentials.set_api_key("  " + SYNTH + "  ")
    assert credentials.get_api_key() == SYNTH and credentials.verify_round_trip(SYNTH) and not credentials.verify_round_trip(SYNTH + "x")
    assert credentials.delete_api_key() is True and credentials.get_api_key() is None and credentials.delete_api_key() is False
    assert credentials.verify_round_trip(SYNTH) is False
    for empty in ("", "   ", None):
        with pytest.raises(CredentialStoreError):
            credentials.set_api_key(empty)  # type: ignore[arg-type]


def test_backend_exceptions_never_leak_text_or_secret(monkeypatch):
    class Leaky:
        def get_password(self, *a):
            raise RuntimeError("backend says key=" + SYNTH)

        def set_password(self, *a):
            raise OSError("cannot write " + SYNTH)

        def delete_password(self, *a):
            raise RuntimeError(SYNTH)
    monkeypatch.setattr(credentials, "_backend", lambda: (Leaky(), "keyring.backends.Windows.WinVaultKeyring"))
    for call in (credentials.get_api_key, lambda: credentials.set_api_key(SYNTH), credentials.delete_api_key):
        with pytest.raises(CredentialStoreError) as ei:
            call()
        assert SYNTH not in ei.value.message and SYNTH not in repr(ei.value) and "backend says" not in ei.value.message
        assert ei.value.__cause__ is None                                           # the original exception is not chained either


# ======================================================================================= Settings integration
def test_missing_credentials_resolve_to_none(isolated_credentials):
    assert Settings().api_key() is None and Settings().api_key_source() is None


def test_environment_variable_has_precedence_and_skips_the_store(monkeypatch):
    def forbidden():
        raise AssertionError("credential store must not be touched when an environment variable is set")
    monkeypatch.setattr(credentials, "_backend", forbidden)
    monkeypatch.setenv("GEMINI_API_KEY", " '" + SYNTH + "' ")
    assert Settings().api_key() == SYNTH
    assert Settings().api_key_source() == "environment variable GEMINI_API_KEY" and SYNTH not in Settings().api_key_source()
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", SYNTH)
    assert Settings().api_key_source() == "environment variable GOOGLE_API_KEY"


def test_stored_credential_is_used_when_no_environment_variable(isolated_credentials):
    credentials.set_api_key(SYNTH)
    s = Settings()
    assert s.api_key() == SYNTH and s.api_key_source() == "Windows Credential Manager"
    assert SYNTH not in repr(s) and SYNTH not in str(vars(s))                       # settings object holds no copy


def test_credential_store_failure_propagates_as_safe_error(monkeypatch):
    def broken():
        raise CredentialStoreError("the operating system's credential store could not be initialised")
    monkeypatch.setattr(credentials, "_backend", broken)
    with pytest.raises(CredentialStoreError):
        Settings().api_key()


# ======================================================================================= provider integration
def test_provider_without_any_credential_is_unconfigured():
    with pytest.raises(LLMError) as ei:
        GemmaApiProvider(Settings())
    assert ei.value.kind == "unconfigured" and ei.value.message == KEY_HELP


def test_provider_reports_unavailable_store_safely_and_does_not_fall_back(monkeypatch):
    def broken():
        raise CredentialStoreError("no secure operating-system credential store is available (keyring is using 'x.Y', which is not OS-protected). Nothing was saved.")
    monkeypatch.setattr(credentials, "_backend", broken)
    with pytest.raises(LLMError) as ei:
        GemmaApiProvider(Settings())
    assert ei.value.kind == "unconfigured" and "Nothing was saved" in ei.value.message and "setup_gemini_key.py" in ei.value.message
    with pytest.raises(LLMError):
        get_provider(Settings(provider="gemma_api"))


def test_provider_hands_the_stored_key_to_the_sdk_only(isolated_credentials, monkeypatch):
    credentials.set_api_key(SYNTH)
    import google.genai as genai
    seen = {}
    monkeypatch.setattr(genai, "Client", lambda api_key, http_options=None: seen.setdefault("key_ok", api_key == SYNTH) and FakeClient([]))
    p = GemmaApiProvider(Settings())
    assert seen["key_ok"] is True
    assert SYNTH not in repr(p) and SYNTH not in repr(vars(p)) and p.model == "gemma-4-26b-a4b-it"


def test_error_messages_redact_a_stored_key(isolated_credentials, monkeypatch):
    credentials.set_api_key(SYNTH)                                                  # key exists only in the store, not the environment
    import google.genai as genai
    leak = api_error(400, f"invalid request, echo={SYNTH} again {SYNTH} and key={SYNTH_AIZA}", "INVALID_ARGUMENT")
    monkeypatch.setattr(genai, "Client", lambda api_key, http_options=None: FakeClient([leak]))
    p = GemmaApiProvider(Settings(), sleep=lambda s: None)
    from copilot.llm import Turn
    with pytest.raises(LLMError) as ei:
        p.generate([Turn("user", "hi")])
    for secret in (SYNTH, SYNTH_AIZA, "AIza"):
        assert secret not in ei.value.message and secret not in str(ei.value) and secret not in repr(ei.value)


def test_scrubbing_survives_a_failing_credential_store(isolated_credentials, monkeypatch):
    p = prov([api_error(400, "bad request", "INVALID_ARGUMENT")])
    monkeypatch.setattr(credentials, "_backend", lambda: (_ for _ in ()).throw(CredentialStoreError("store down")))
    from copilot.llm import Turn
    with pytest.raises(LLMError) as ei:                                             # error mapping must not itself crash
        p.generate([Turn("user", "hi")])
    assert ei.value.kind == "invalid_request"


def test_model_restriction_and_no_fallback_are_unchanged(isolated_credentials):
    credentials.set_api_key(SYNTH)
    for bad in ("gemini-2.5-flash", "claude-sonnet", ""):
        with pytest.raises(LLMError) as ei:
            GemmaApiProvider(Settings(model=bad))
        assert ei.value.kind == "invalid_request" and "refusing" in ei.value.message
    with pytest.raises(LLMError, match="not implemented"):
        get_provider(Settings(provider="ollama"))


# ======================================================================================= setup utility
def test_setup_stores_key_hidden_and_never_prints_it(script, monkeypatch, capsys, isolated_credentials):
    d = Driver(script, monkeypatch, capsys, secrets=[SYNTH_AIZA, SYNTH_AIZA])
    assert d.run() == 0
    assert credentials.get_api_key() == SYNTH_AIZA
    assert "Setup succeeded" in d.out and "No request was sent" in d.out
    assert SYNTH_AIZA not in d.out and "AIzaSy" not in d.out
    assert len(d.secret_prompts) == 2 and all("hidden" in p for p in d.secret_prompts)
    assert d.plain_prompts == []                                                    # the key is never asked for through a visible prompt


def test_setup_refuses_when_not_interactive(script, monkeypatch, capsys, isolated_credentials):
    d = Driver(script, monkeypatch, capsys, secrets=[], interactive=False)
    assert d.run() == 4 and "interactive terminal" in d.out and d.secret_prompts == [] and credentials.get_api_key() is None


def test_setup_stops_before_prompting_when_secure_storage_is_unavailable(script, monkeypatch, capsys, tmp_path):
    def broken():
        raise CredentialStoreError("no secure operating-system credential store is available (keyring is using 'a.B', which is not OS-protected). Nothing was saved.")
    monkeypatch.setattr(credentials, "_backend", broken)
    monkeypatch.chdir(tmp_path)
    d = Driver(script, monkeypatch, capsys, secrets=[SYNTH, SYNTH])
    assert d.run() == 3
    assert "unavailable" in d.out and "No key was requested and nothing was saved" in d.out
    assert d.secret_prompts == [] and list(tmp_path.iterdir()) == []                 # never asked, never wrote a fallback file


@pytest.mark.parametrize("first,second,code", [
    (SYNTH_AIZA, SYNTH_AIZA[:-1] + "X", 5),          # confirmation mismatch
    ("", "", 5),                                      # nothing entered
])
def test_setup_aborts_on_mismatch_or_empty(script, monkeypatch, capsys, isolated_credentials, first, second, code):
    d = Driver(script, monkeypatch, capsys, secrets=[first, second])
    assert d.run() == code and credentials.get_api_key() is None
    assert SYNTH_AIZA not in d.out


def test_setup_rejects_malformed_input_without_echoing_it(script, monkeypatch, capsys, isolated_credentials):
    bad = "has spaces and !!! symbols"
    d = Driver(script, monkeypatch, capsys, secrets=[bad, bad])
    assert d.run() == 1 and credentials.get_api_key() is None
    assert "does not look like an API key" in d.out and bad not in d.out and "symbols" not in d.out


def test_setup_unusual_prefix_needs_explicit_confirmation(script, monkeypatch, capsys, isolated_credentials):
    odd = "unusual-prefix-key-1234567890abcd"
    d = Driver(script, monkeypatch, capsys, secrets=[odd, odd], answers=["n"])
    assert d.run() == 5 and credentials.get_api_key() is None and odd not in d.out
    d = Driver(script, monkeypatch, capsys, secrets=[odd, odd], answers=["y"])
    assert d.run() == 0 and credentials.get_api_key() == odd and odd not in d.out


def test_setup_replace_flow(script, monkeypatch, capsys, isolated_credentials):
    credentials.set_api_key(SYNTH_AIZA)
    new = SYNTH_AIZA[:-4] + "ZZZZ"
    d = Driver(script, monkeypatch, capsys, secrets=[new, new], answers=["n"])
    assert d.run() == 5 and credentials.get_api_key() == SYNTH_AIZA and d.secret_prompts == []     # declined: kept, never prompted
    d = Driver(script, monkeypatch, capsys, secrets=[new, new], answers=["y"])
    assert d.run() == 0 and credentials.get_api_key() == new and new not in d.out and SYNTH_AIZA not in d.out


def test_setup_cancel_and_store_write_failure_are_safe(script, monkeypatch, capsys, isolated_credentials):
    d = Driver(script, monkeypatch, capsys, secrets=[KeyboardInterrupt()])
    assert d.run() == 5 and "Nothing was stored" in d.out and credentials.get_api_key() is None
    d = Driver(script, monkeypatch, capsys, secrets=[EOFError()])
    assert d.run() == 5

    class Failing(MemoryStore):
        def set_password(self, *a):
            raise OSError("disk says no: " + SYNTH_AIZA)
    monkeypatch.setattr(credentials, "_backend", lambda: (Failing(), "keyring.backends.Windows.WinVaultKeyring"))
    d = Driver(script, monkeypatch, capsys, secrets=[SYNTH_AIZA, SYNTH_AIZA])
    assert d.run() == 3 and "could not be saved" in d.out and SYNTH_AIZA not in d.out and "disk says no" not in d.out


def test_setup_notes_environment_variable_precedence_without_value(script, monkeypatch, capsys, isolated_credentials):
    monkeypatch.setenv("GEMINI_API_KEY", SYNTH)
    d = Driver(script, monkeypatch, capsys, secrets=[SYNTH_AIZA, SYNTH_AIZA])
    assert d.run() == 0 and "environment variable GEMINI_API_KEY is also set" in d.out and SYNTH not in d.out


def test_status_command_reveals_no_secret(script, monkeypatch, capsys, isolated_credentials):
    d = Driver(script, monkeypatch, capsys)
    assert d.run("--status") == 0 and "key stored in it: no" in d.out and "usable by the app: no" in d.out
    credentials.set_api_key(SYNTH_AIZA)
    monkeypatch.setenv("GEMINI_API_KEY", SYNTH)
    assert d.run("--status") == 0 and "key stored in it: yes" in d.out and "GEMINI_API_KEY" in d.out and "takes precedence" in d.out
    assert SYNTH not in d.out and SYNTH_AIZA not in d.out and "AIzaSy" not in d.out
    monkeypatch.setattr(credentials, "_backend", lambda: (_ for _ in ()).throw(CredentialStoreError("store unavailable (synthetic)")))
    assert d.run("--status") == 3 and "UNAVAILABLE" in d.out


def test_delete_command(script, monkeypatch, capsys, isolated_credentials):
    credentials.set_api_key(SYNTH_AIZA)
    d = Driver(script, monkeypatch, capsys, answers=["n"])
    assert d.run("--delete") == 5 and credentials.get_api_key() == SYNTH_AIZA
    d = Driver(script, monkeypatch, capsys, answers=["y"])
    assert d.run("--delete") == 0 and credentials.get_api_key() is None and "Deleted" in d.out
    d = Driver(script, monkeypatch, capsys, interactive=False)
    assert d.run("--delete") == 4


def test_key_cannot_be_passed_as_an_argument(script, monkeypatch, capsys, isolated_credentials):
    for argv in (["--key", SYNTH], [SYNTH], ["--api-key=" + SYNTH]):
        with pytest.raises(SystemExit) as ei:
            script.main(argv)
        assert ei.value.code == 2
    err = capsys.readouterr().err
    assert credentials.get_api_key() is None
    assert "unrecognized arguments" in err or "error" in err.lower()            # argparse itself echoes bad args; that is why no secret flag exists


def test_setup_script_source_never_outputs_the_key_or_spawns_processes():
    src = (ROOT / "scripts" / "setup_gemini_key.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    secret_names = {"key1", "key2"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fn in ("print", "_say", "write", "info", "debug", "warning", "error", "exception"):
                names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
                assert not names & secret_names, f"line {node.lineno}: secret variable passed to {fn}"
            assert fn not in ("system", "popen", "Popen", "run", "call", "check_output", "exec", "eval"), f"line {node.lineno}: {fn}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not {m.split(".")[0] for m in mods} & {"subprocess", "requests", "httpx", "urllib", "socket", "logging"}, mods
    assert "getpass" in src and "input(\"Enter" not in src and "sys.argv" not in src.replace("main(argv", "")


# ======================================================================================= real Windows store (synthetic only)
@pytest.mark.realstore
@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager test")
def test_real_windows_store_round_trip_with_synthetic_value():
    service = "asset-intelligence-copilot-selftest-" + uuid.uuid4().hex[:12]
    try:
        _, name = credentials._backend()
        assert name == "keyring.backends.Windows.WinVaultKeyring"
        assert credentials.get_api_key(service) is None
        credentials.set_api_key(SYNTH, service)
        assert credentials.get_api_key(service) == SYNTH and credentials.verify_round_trip(SYNTH, service)
        assert credentials.delete_api_key(service) is True
        assert credentials.get_api_key(service) is None
    finally:
        try:
            credentials.delete_api_key(service)
        except CredentialStoreError:
            pass


# ======================================================================================= repository hygiene
SECRET_SHAPES = [re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), re.compile(r"sk-or-v1-[0-9a-f]{20,}"), re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
                 re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{24,}[\"']")]


def test_no_key_shaped_strings_in_tracked_source():
    skip_dirs = {"dataset", "data", ".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or set(p.relative_to(ROOT).parts) & skip_dirs or p.suffix in {".pyc", ".db", ".png", ".jpg"}:
            continue
        if p.name == ".env":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in SECRET_SHAPES:
            if rx.search(text):
                hits.append(f"{p.relative_to(ROOT)} ({rx.pattern[:20]})")
    assert hits == []


def test_gitignore_covers_credential_files():
    lines = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for must in (".env", ".env.*", "!.env.example", "data/", "dataset/", "*.key", "*.pem", "secrets/", "credentials*.json"):
        assert must in lines, must
    for path in (".env", "data/state.db", "secrets/x", "my.key", ".env.local", "credentials.json"):
        r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, capture_output=True)
        assert r.returncode == 0, f"{path} is not git-ignored"
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT, capture_output=True).returncode == 1   # the template is not
