"""OS-protected storage for the model API key (Windows Credential Manager on Windows).

Rules enforced here
* Only operating-system credential stores are accepted. If the active `keyring` backend is anything else
  (plaintext file, "null", "fail", an unknown plugin), every operation raises `CredentialStoreError`.
  Nothing is ever written to a project file as a fallback.
* Error messages are fixed, safe strings: they never include the secret and never include exception text
  coming from the backend.
* The key is returned to the caller on demand and is not cached in this module.
"""
from __future__ import annotations

import hmac

SERVICE = "asset-intelligence-copilot"
ACCOUNT = "gemini-api-key"

# keyring backends that delegate to an operating-system secret store.
SECURE_BACKENDS = frozenset({
    "keyring.backends.Windows.WinVaultKeyring",
    "keyring.backends.macOS.Keyring",
    "keyring.backends.SecretService.Keyring",
    "keyring.backends.kwallet.DBusKeyring",
    "keyring.backends.kwallet.DBusKeyringKWallet4",
})


class CredentialStoreError(Exception):
    """Secure credential storage is unavailable or failed. `message` is safe to display."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _qualname(obj) -> str:
    return f"{type(obj).__module__}.{type(obj).__name__}"


def _backend():
    """Return (backend, qualified_name) for a secure OS store, or raise CredentialStoreError."""
    try:
        import keyring
    except ImportError:
        raise CredentialStoreError("the 'keyring' package is not installed (pip install keyring); no secure credential store is available") from None
    try:
        kr = keyring.get_keyring()
        if hasattr(kr, "backends") and getattr(kr, "backends", None):      # ChainerBackend: use its highest-priority member
            kr = kr.backends[0]
    except Exception:
        raise CredentialStoreError("the operating system's credential store could not be initialised") from None
    name = _qualname(kr)
    if name not in SECURE_BACKENDS:
        raise CredentialStoreError(
            f"no secure operating-system credential store is available (keyring is using '{name}', which is not OS-protected). "
            "Nothing was saved. Use an environment variable for the process instead, or fix the credential store.")
    return kr, name


def backend_name() -> str:
    """Name of the secure backend in use (raises CredentialStoreError if none)."""
    return _backend()[1]


def get_api_key(service: str = SERVICE, account: str = ACCOUNT) -> str | None:
    kr, _ = _backend()
    try:
        v = kr.get_password(service, account)
    except Exception as e:
        raise CredentialStoreError(f"the credential store could not be read ({type(e).__name__})") from None
    v = (v or "").strip()
    return v or None


def set_api_key(value: str, service: str = SERVICE, account: str = ACCOUNT) -> None:
    if not value or not value.strip():
        raise CredentialStoreError("refusing to store an empty credential")
    kr, _ = _backend()
    try:
        kr.set_password(service, account, value.strip())
    except Exception as e:
        raise CredentialStoreError(f"the credential could not be saved ({type(e).__name__}); nothing was written elsewhere") from None


def delete_api_key(service: str = SERVICE, account: str = ACCOUNT) -> bool:
    """Remove the stored credential. Returns True if one existed."""
    kr, _ = _backend()
    try:
        existed = kr.get_password(service, account) is not None
        if existed:
            kr.delete_password(service, account)
        return existed
    except Exception as e:
        raise CredentialStoreError(f"the credential could not be removed ({type(e).__name__})") from None


def verify_round_trip(expected: str, service: str = SERVICE, account: str = ACCOUNT) -> bool:
    """True if the stored value equals `expected` (constant-time comparison; nothing is printed or returned)."""
    got = get_api_key(service, account)
    return got is not None and hmac.compare_digest(got.encode(), expected.strip().encode())
