"""HTTP transport: one JSON client, one downloader, one error vocabulary.

Every wire call the Engine makes goes through this module, so retry hints, the
provider's error envelope and error classification live in one place.

Standard library only: the skeleton carries no third-party runtime dependency,
so a new engine can be adopted without adding a package to the fleet.
"""

from __future__ import annotations

import json
import random
import uuid
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlencode

DEFAULT_BASE_URL = "https://hub.oxen.ai"
DEFAULT_TIMEOUT = 600.0
DOWNLOAD_TIMEOUT = 300.0
RETRY_AFTER_CAP = 300.0
USER_AGENT = "helmer-engine/0.1"

_MAX_ERROR_CHARS = 600

#: HTTP statuses and provider codes where every remaining input fails the same
#: way: the key is bad, the balance is short, a cap is reached, or the account
#: is not allowed to run models at all.
FATAL_STATUSES = frozenset({401, 402, 403})
FATAL_TOKENS = (
    "insufficient",
    "payment",
    "billing",
    "balance",
    "quota",
    "unauthorized",
    "forbidden",
    "not allowed",
)

#: Transient: waiting and trying again is the documented advice.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
RETRYABLE_TOKENS = ("rate limit", "ratelimit", "throttl", "too many", "high demand",
                    "temporarily unavailable", "timeout", "timed out")


class ProviderError(RuntimeError):
    """A failed provider call.

    Attributes:
        status: HTTP status, or 0 when the request never completed.
        payload: the decoded error envelope, when there was one.
        retry_after: the ``Retry-After`` header in seconds, when sent.
        code: the provider's own error code, when the body carries one.
    """

    def __init__(
        self,
        status: int,
        message: str,
        payload: dict | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}
        self.retry_after = retry_after

    @property
    def code(self):
        return self.payload.get("code")


def error_message(payload: dict) -> str:
    """Human message out of the provider's error envelope, best effort."""
    if not isinstance(payload, dict):
        return ""
    for key in ("msg", "message", "detail", "error_description"):
        if payload.get(key):
            return str(payload[key])
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "title", "code"):
            if error.get(key):
                return str(error[key])
    if isinstance(error, str) and error:
        return error
    return ""


def classify_error(exc: Exception) -> str:
    """``"fatal"``, ``"retryable"`` or ``"other"``.

    Fatal means the whole batch will fail the same way; retryable means waiting
    helps; other means this one input was wrong and the next may succeed.
    """
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status in FATAL_STATUSES:
        return "fatal"
    text = str(exc).lower()
    if any(token in text for token in FATAL_TOKENS):
        return "fatal"
    if status in RETRYABLE_STATUSES:
        return "retryable"
    if any(token in text for token in RETRYABLE_TOKENS):
        return "retryable"
    return "other"


def is_batch_fatal(exc: Exception) -> bool:
    return classify_error(exc) == "fatal"


def is_retryable(exc: Exception) -> bool:
    return classify_error(exc) == "retryable"


def backoff(base_delay: float, attempt: int, *, long_backoff: bool = False,
            exc: Exception | None = None) -> float:
    """Seconds to wait before retry ``attempt`` (0-based).

    A provider's ``Retry-After`` wins when sent; otherwise exponential, with
    jitter, stretched eight times for capacity outages that resolve slowly.
    """
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        return max(1.0, min(float(retry_after), RETRY_AFTER_CAP))
    return base_delay * (8 if long_backoff else 2) ** attempt + random.uniform(0, 1)


def request_with_retry(
    client: "Client",
    method: str,
    path: str,
    payload: dict | None,
    *,
    retries: int,
    base_delay: float,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    on_retry: "Callable[[int, int, float, bool], None] | None" = None,
) -> dict:
    """One request, retried while the failure is transient."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return client.request_json(method, path, payload, headers=headers, params=params)
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if not is_retryable(exc) or attempt >= retries:
                raise
            last_exc = exc
            long_backoff = getattr(exc, "status", None) in (502, 503, 504)
            sleep = backoff(base_delay, attempt, long_backoff=long_backoff, exc=exc)
            if on_retry:
                on_retry(attempt + 1, retries, sleep, long_backoff)
            time.sleep(sleep)
    raise last_exc  # pragma: no cover - the loop returns or raises


class Client:
    """Minimal JSON client. One instance per batch."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def request_json(self, method: str, path: str, payload: dict | None = None,
                     *, headers: dict[str, str] | None = None,
                     params: dict[str, str] | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers={**self._headers, **(headers or {})},
            method=method.upper(),
        )
        return self._send(request)

    def _send(self, request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise ProviderError(0, f"Connection to {self.base_url} failed: {exc.reason}") from None
        if not body.strip():
            return {}
        try:
            decoded = json.loads(body)
        except ValueError:
            raise ProviderError(200, f"Unreadable response body: {body[:_MAX_ERROR_CHARS]}") from None
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    def request_form(self, method: str, path: str, fields: dict,
                     *, files: dict | None = None) -> dict:
        """A multipart/form-data request. Video creation takes a form, not JSON."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        boundary = f"----engine{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in (fields or {}).items():
            if value is None:
                continue
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )
        for name, payload in (files or {}).items():
            filename, data, mime = payload
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8")
            )
            chunks.append(data)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        headers = {k: v for k, v in self._headers.items() if k.lower() != "content-type"}
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        return self._send(request)

    def request_bytes(self, method: str, path: str) -> bytes:
        """Fetch a raw file with the API key attached (video content lives here)."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(url, headers=self._headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise ProviderError(0, f"Connection to {self.base_url} failed: {exc.reason}") from None

    def download(self, url: str) -> bytes:
        """Fetch a result file.

        No API key is sent: result URLs are their own grants, and providers
        reject a download that attaches one.
        """
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ProviderError(exc.code, f"Download failed ({exc.code}) for {url}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(0, f"Download failed for {url}: {exc.reason}") from None

    def _http_error(self, exc: urllib.error.HTTPError) -> ProviderError:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            payload = json.loads(raw)
            payload = payload if isinstance(payload, dict) else {}
        except ValueError:
            payload = {}
        message = error_message(payload) or raw.strip()[:_MAX_ERROR_CHARS] or exc.reason
        retry_after = None
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                retry_after = float(header)
            except (TypeError, ValueError):
                retry_after = None
        return ProviderError(exc.code, str(message), payload, retry_after)
