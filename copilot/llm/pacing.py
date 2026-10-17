"""Request pacing and retry-delay extraction for the hosted model API.

* `Pacer` lets one request run at a time (no bursts of large tool-declaration prompts), enforces a minimum gap between
  requests, and is told about rate-limit delays so every waiting caller backs off together. After a rate limit the gap
  widens, and it relaxes again after successes.
* `retry_after_seconds` reads the delay the API itself asks for: the Retry-After header, the structured RetryInfo detail,
  or "retry in 34.5s" in the message.
* `classify_429` separates a per-minute limit (worth waiting for) from a daily/billing quota (not).
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from typing import Any


class Pacer:
    def __init__(self, min_interval_s: float = 1.5, *, max_interval_s: float = 30.0, clock=time.monotonic, sleep=time.sleep):
        self.base_interval = max(0.0, min_interval_s)
        self.interval = self.base_interval
        self.max_interval = max(self.base_interval, max_interval_s)
        self._clock, self._sleep = clock, sleep
        self._gate = threading.Lock()                 # one in-flight request at a time
        self._state = threading.Lock()
        self._next_ok = 0.0

    @contextmanager
    def slot(self):
        """Hold the single request slot, first waiting until the pacing window allows a request."""
        with self._gate:
            with self._state:
                wait = self._next_ok - self._clock()
            if wait > 0:
                self._sleep(wait)
            try:
                yield
            finally:
                with self._state:
                    self._next_ok = max(self._next_ok, self._clock() + self.interval)

    def penalize(self, seconds: float) -> None:
        """A rate limit told us to wait: no request (from any caller) starts before then."""
        with self._state:
            self._next_ok = max(self._next_ok, self._clock() + max(0.0, seconds))

    def widen(self) -> None:
        with self._state:
            self.interval = min(self.max_interval, max(self.interval * 1.5, self.base_interval + 1.0))

    def relax(self) -> None:
        with self._state:
            self.interval = max(self.base_interval, self.interval * 0.9)


_DELAY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.I)
_MSG_DELAY = re.compile(r"retry(?:ing)? in (\d+(?:\.\d+)?)\s*(ms|s|m)\b", re.I)


def _to_seconds(value: float, unit: str | None) -> float:
    unit = (unit or "s").lower()
    return value / 1000 if unit == "ms" else value * 60 if unit == "m" else value * 3600 if unit == "h" else value


def retry_after_seconds(err: Any) -> float | None:
    """The delay the API asks for, in seconds, or None if it did not say."""
    headers = getattr(getattr(err, "response", None), "headers", None)
    if headers is not None:
        try:
            v = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            v = None
        if v:
            m = _DELAY.match(str(v))
            if m:
                return _to_seconds(float(m.group(1)), m.group(2))
            try:
                dt = parsedate_to_datetime(str(v))
                return max(0.0, dt.timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    details = getattr(err, "details", None)
    items = (details.get("error", {}) or {}).get("details", []) if isinstance(details, dict) else []
    for d in items if isinstance(items, list) else []:
        if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo") and d.get("retryDelay") is not None:
            m = _DELAY.match(str(d["retryDelay"]))
            if m:
                return _to_seconds(float(m.group(1)), m.group(2))
    m = _MSG_DELAY.search(str(getattr(err, "message", "") or ""))
    return _to_seconds(float(m.group(1)), m.group(2)) if m else None


def classify_429(err: Any) -> str:
    """'daily' (waiting will not help), 'per_minute' (waiting will), or 'unknown'."""
    details = getattr(err, "details", None)
    text = str(getattr(err, "message", "") or "").lower()
    ids = []
    items = (details.get("error", {}) or {}).get("details", []) if isinstance(details, dict) else []
    for d in items if isinstance(items, list) else []:
        if isinstance(d, dict):
            for v in d.get("violations", []) or []:
                if isinstance(v, dict):
                    ids.append(f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}".lower())
    blob = " ".join(ids) + " " + text
    if any(w in blob for w in ("perday", "per day", "per_day", "requestsperday", "daily limit", "daily quota")):
        return "daily"
    if any(w in blob for w in ("perminute", "per minute", "per_minute", "rate limit", "too many requests")):
        return "per_minute"
    return "unknown"
