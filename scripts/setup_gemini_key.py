#!/usr/bin/env python
"""Store your Google AI Studio API key in the Windows Credential Manager (hidden prompt).

Run this yourself in a terminal on your own computer:

    python scripts/setup_gemini_key.py             # enter the key (hidden), stored securely
    python scripts/setup_gemini_key.py --status    # is a key stored? (never shows it)
    python scripts/setup_gemini_key.py --delete    # remove the stored key

Design rules
* The key is read with getpass (characters are not displayed) and is never accepted as a command-line argument.
* It is stored only in the operating system's credential store. If none is available the script stops and
  explains; it never falls back to a file.
* The key is never printed, logged, put in an exception message or written anywhere except the credential store.
* The script refuses to run unless stdin and stdout are a real terminal, so a key cannot be echoed or piped.
* No network request is made.
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from copilot import credentials  # noqa: E402  (after sys.path setup)

# Indirection so tests can drive the prompts without a console.
_getpass = getpass.getpass
_input = input


def _is_interactive() -> bool:
    return bool(sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty())


KEY_SHAPE = re.compile(r"^[A-Za-z0-9_.\-]{20,256}$")
EXIT_OK, EXIT_FAIL, EXIT_STORE_UNAVAILABLE, EXIT_NOT_INTERACTIVE, EXIT_CANCELLED = 0, 1, 3, 4, 5


def _env_names() -> list[str]:
    return [n for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY") if (os.environ.get(n) or "").strip()]


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _clean(raw: str) -> str:
    return (raw or "").strip().strip("\"'").strip()


def cmd_status() -> int:
    env = _env_names()
    _say("Gemini API key status (the key itself is never shown)")
    try:
        name = credentials.backend_name()
    except credentials.CredentialStoreError as e:
        _say(f"  secure credential store: UNAVAILABLE - {e.message}")
        _say(f"  environment variable set: {', '.join(env) if env else 'no'}")
        return EXIT_STORE_UNAVAILABLE
    try:
        stored = credentials.get_api_key() is not None
    except credentials.CredentialStoreError as e:
        _say(f"  secure credential store: {name}")
        _say(f"  could not read it: {e.message}")
        return EXIT_FAIL
    _say(f"  secure credential store: {name}")
    _say(f"  key stored in it: {'yes' if stored else 'no'}")
    _say(f"  environment variable set: {', '.join(env) if env else 'no'}" + (" (takes precedence over the stored key)" if env else ""))
    _say("  usable by the app: " + ("yes" if (stored or env) else "no - run: python scripts/setup_gemini_key.py"))
    return EXIT_OK


def cmd_delete() -> int:
    if not _is_interactive():
        _say("Refusing to run: this must be run in an interactive terminal.")
        return EXIT_NOT_INTERACTIVE
    try:
        credentials.backend_name()
        if credentials.get_api_key() is None:
            _say("No stored key found; nothing to delete.")
            return EXIT_OK
        if _input("Delete the stored Gemini API key from the credential store? [y/N] ").strip().lower() != "y":
            _say("Cancelled. Nothing was changed.")
            return EXIT_CANCELLED
        credentials.delete_api_key()
        _say("Deleted. The stored key was removed (an environment variable, if any, is untouched).")
        return EXIT_OK
    except credentials.CredentialStoreError as e:
        _say(f"Could not complete: {e.message}")
        return EXIT_STORE_UNAVAILABLE


def cmd_setup() -> int:
    _say("Gemini API key setup")
    _say("  - Your input is hidden and is stored only in the operating system's credential store.")
    _say("  - Do NOT paste the key anywhere else (chat, notes, commands).")
    if not _is_interactive():
        _say("\nRefusing to run: this must be run directly in an interactive terminal (not piped or captured), so the key can be entered hidden.")
        return EXIT_NOT_INTERACTIVE
    try:
        backend = credentials.backend_name()                 # check BEFORE asking for a secret
    except credentials.CredentialStoreError as e:
        _say(f"\nSecure credential storage is unavailable: {e.message}")
        _say("No key was requested and nothing was saved.")
        return EXIT_STORE_UNAVAILABLE
    _say(f"  - Secure store: {backend}\n")
    key1 = key2 = None
    try:
        if credentials.get_api_key() is not None:
            if _input("A key is already stored. Replace it? [y/N] ").strip().lower() != "y":
                _say("Cancelled. The existing key was kept.")
                return EXIT_CANCELLED
        key1 = _clean(_getpass("Enter your Google AI Studio API key (hidden): "))
        if not key1:
            _say("Nothing entered. Nothing was stored.")
            return EXIT_CANCELLED
        key2 = _clean(_getpass("Re-enter the key to confirm (hidden): "))
        if not hmac.compare_digest(key1.encode(), key2.encode()):
            _say("The two entries did not match. Nothing was stored.")
            return EXIT_CANCELLED
        if not KEY_SHAPE.match(key1):
            _say("That does not look like an API key (expected 20-256 letters, digits, '-', '_' or '.', no spaces). Nothing was stored.")
            return EXIT_FAIL
        if not key1.startswith("AIza"):
            if _input("This does not start with the usual 'AIza' prefix of AI Studio keys. Store it anyway? [y/N] ").strip().lower() != "y":
                _say("Cancelled. Nothing was stored.")
                return EXIT_CANCELLED
        credentials.set_api_key(key1)
        ok = credentials.verify_round_trip(key1)
        if not ok:
            _say("The key was written but could not be read back correctly. Run --status, then try again.")
            return EXIT_FAIL
        _say("\nSetup succeeded: the key is stored and can be read back from the credential store.")
        env = _env_names()
        if env:
            _say(f"Note: environment variable {', '.join(env)} is also set and takes precedence over the stored key.")
        _say("No request was sent to Google. Next: tell Claude setup is done so it can run the live Gemma check.")
        return EXIT_OK
    except credentials.CredentialStoreError as e:
        _say(f"\nCould not store the key: {e.message}")
        return EXIT_STORE_UNAVAILABLE
    except (KeyboardInterrupt, EOFError):
        _say("\nCancelled. Nothing was stored.")
        return EXIT_CANCELLED
    finally:
        key1 = key2 = None                                   # drop references (best effort; strings are immutable)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Store the Gemini API key in the OS credential store (hidden prompt).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="show whether a key is stored (never shows the key)")
    g.add_argument("--delete", action="store_true", help="remove the stored key")
    args = ap.parse_args(argv)
    try:
        if args.status:
            return cmd_status()
        if args.delete:
            return cmd_delete()
        return cmd_setup()
    except Exception as e:                                    # never print a traceback near a secret
        _say(f"Unexpected error ({type(e).__name__}). Nothing was stored or displayed.")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
