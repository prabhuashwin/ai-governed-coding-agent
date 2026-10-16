"""Shared HTTP plumbing for API-backed providers: retries with backoff, clear errors."""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import ProviderError

RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def post_json(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float,
              attempts: int = 4, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    last: str = ""
    for i in range(attempts):
        try:
            with httpx.Client(timeout=timeout, transport=transport) as client:
                r = client.post(url, headers=headers, json=body)
            if r.status_code in RETRY_STATUSES and i < attempts - 1:
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(min(2 ** i, 20))
                continue
            if r.status_code >= 400:
                raise ProviderError(f"HTTP {r.status_code} from {url}: {r.text[:1000]}")
            return r.json()
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = f"{type(e).__name__}: {e}"
            if i < attempts - 1:
                time.sleep(min(2 ** i, 20))
                continue
    raise ProviderError(f"request failed after {attempts} attempts: {last}")
