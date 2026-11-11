"""The Engine: one interface over OpenAI's two shapes.

Images answer in one call; video is a job you poll and then fetch. Both end as a
list of assets, and everything downstream — naming, saving, error results — is
the same.

What it refuses to do: invent a media key, send an undeclared parameter, treat
an empty answer as a success, or keep spending after an error that will fail
every remaining file.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import outputs, tasks, transport
from .catalog import Catalog, EndpointSpec, load_catalog
from .datatypes import EngineError, InputFile, OutputFile, ProgressEvent
from .payloads import build_input, compose_prompt, is_unset
from .payloads import _to_bool

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TASK_TIMEOUT = 3600.0

IMAGES_PATH = "api/ai/images/generate"
VIDEO_SUBMIT_PATH = "api/ai/queue"
VIDEO_STATUS_PATH = "api/ai/queue/{id}"
VIDEO_CONTENT_PATH = ""


class Engine:
    PLATFORM: str = "oxen"
    PROVIDER_NAME: str = "Oxen.ai"
    PROVIDER_HOMEPAGE: str = "https://oxen.ai"
    API_KEY_ENV_VAR: str = "OXEN_API_KEY"
    API_KEY_PATTERN: str = r".+"
    BASE_URL: str = transport.DEFAULT_BASE_URL

    def __init__(
        self,
        profile: dict,
        output_dir: str | Path,
        api_key: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ):
        self._profile = profile
        self._output_dir = Path(output_dir)
        self._api_key = api_key
        self._on_progress = on_progress
        self._prefix = profile.get("prompt_prefix", "")
        self._suffix = profile.get("prompt_suffix", "")
        self._base_url = str(profile.get("base_url") or profile.get("api_base_url") or self.BASE_URL)
        self._poll_interval = _non_negative_float(profile.get("poll_interval"), DEFAULT_POLL_INTERVAL)
        self._task_timeout = _non_negative_float(profile.get("timeout_seconds"), DEFAULT_TASK_TIMEOUT)
        self._concurrency = profile.get("concurrency", 1)
        self._catalog: Catalog = load_catalog()
        self._client_instance: transport.Client | None = None
        self._client_lock = threading.Lock()
        self._consecutive_retryable = 0
        self._counter_lock = threading.Lock()

    # ── entry point ───────────────────────────────────────────────────────

    def run(self, inputs: list[InputFile]) -> list[OutputFile]:
        self._validate_preflight()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        if self._concurrency > 1 and len(inputs) > 1:
            from . import runner

            return runner.run_batch(self, inputs)
        return self._run_serial(inputs)

    def _run_serial(self, inputs: list[InputFile]) -> list[OutputFile]:
        results: list[OutputFile] = []
        stop = threading.Event()
        for index, item in enumerate(inputs):
            results.extend(self._run_one(index, len(inputs), item, stop))
            if stop.is_set():
                break
        return results

    # ── one input file ────────────────────────────────────────────────────

    def _run_one(
        self, index: int, total: int, item: InputFile, stop: threading.Event | None = None
    ) -> list[OutputFile]:
        spec = self._catalog.get(self._profile["endpoint"])
        params = dict(self._profile.get("parameters", {}))
        media_type = self._media_type(spec)

        stem = item.path.stem
        current = index + 1
        prefix = f"[{current}/{total}]"
        rel_dir = str(item.metadata.get("relative_dir", "") or "")
        dest_dir = self._output_dir / rel_dir if rel_dir else self._output_dir
        ext = outputs.default_extension(self._profile, media_type)
        expected = dest_dir / f"{outputs.output_name(stem, index, ext)}"

        delay = float(self._profile.get("delay_between_requests", 0.0))
        if index > 0 and delay > 0:
            time.sleep(delay)

        finish_id = str(item.metadata.get("draft_task_id") or "")
        draft = _to_bool(params.pop("draft", False)) is True and not finish_id
        prompt = compose_prompt(self._profile, item)
        if spec.prompt_required and not prompt and not finish_id:
            self._emit(f"{prefix} Empty prompt after applying prefix/suffix", level="error")
            return [self._error(item, expected, media_type, "Empty prompt after applying prefix/suffix")]

        self._emit(f"{prefix} 📡 Calling {self.PROVIDER_NAME} ({spec.model})...")
        self._emit(f"{prefix} 📝 Markdown: {item.path.name}")

        api_payload: dict | None = None
        try:
            if finish_id:
                api_payload, warnings = self._finish_body(params, finish_id, item.metadata)
            else:
                api_payload, warnings = build_input(self._profile, spec, params, prompt, item)
            if draft:
                # A draft is a fixed 480p preview; the final is where 1080p and
                # 4k belong, and it runs from the draft's id.
                api_payload.update(draft=True, resolution="480p")
            for warning in warnings:
                self._emit(f"{prefix} ⚠️  {warning}", level="warning")

            assets, record = self._dispatch(api_payload, prefix, spec)
            self._emit(f"{prefix} 📦 Payload: {api_payload}")
            saved = self._save_results(assets, media_type, stem, index, prefix, current, total, rel_dir)
            if not saved:
                return [
                    self._error(
                        item,
                        expected,
                        media_type,
                        f"No output returned from {self.PROVIDER_NAME}",
                        api_payload=api_payload,
                    )
                ]
            self._reset_breaker()
            metadata = {"api_payload": api_payload}
            if draft:
                video = (record.get("videos") or [{}])[0]
                expires = datetime.fromisoformat(str(video["draft_expires_at"]).replace("Z", "+00:00"))
                metadata["draft_task"] = {
                    "id": str(video["draft_task_id"]),
                    "created_at": int(record.get("enqueued_at") or time.time()),
                    "expires_at": int(expires.timestamp()),
                }
            if finish_id:
                metadata["finished_from"] = finish_id
            return [
                OutputFile(
                    source_path=item.path,
                    path=saved_path,
                    status="ok",
                    media_type=media_type,
                    metadata=dict(metadata),
                )
                for saved_path in saved
            ]

        except EngineError as exc:
            self._emit(f"{prefix} Error: {exc}", level="error", current=current,
                       total=total, api_payload=api_payload)
            return [self._error(item, expected, media_type, str(exc), api_payload=api_payload)]

        except Exception as exc:  # noqa: BLE001 - classified, then reported per file
            self._emit(f"{prefix} Error: {exc}", level="error", current=current,
                       total=total, api_payload=api_payload)
            output = self._error(item, expected, media_type, str(exc), api_payload=api_payload)
            if transport.is_batch_fatal(exc):
                self._emit(
                    f"{prefix} 🛑 Fatal provider error — aborting batch. "
                    f"Check the key, balance or spend cap. ({exc})",
                    level="error",
                )
                if stop is not None:
                    stop.set()
                return [output]
            if transport.is_retryable(exc):
                count = self._note_retryable()
                circuit_max = int(self._profile.get("circuit_breaker_max", 3))
                if count >= circuit_max:
                    self._emit(
                        f"{prefix} 🛑 Circuit breaker tripped after {count} "
                        f"consecutive temporary failures — aborting batch.",
                        level="error",
                    )
                    if stop is not None:
                        stop.set()
                    return [output]
            else:
                self._reset_breaker()
            return [output]

    def _finish_body(self, params: dict, finish_id: str, item_meta: dict) -> tuple[dict, list[str]]:
        """Render the final of a draft.

        Oxen reuses the draft's prompt, media and settings, so the body carries
        the draft id, the resolution and the draft's duration (Oxen holds back the
        price of the longest clip without it; it does not change the render). A final is 1080p or 4k; anything
        else is rendered at 1080p and said out loud rather than silently.
        """
        warnings: list[str] = []
        asked = str(params.get("resolution") or "").lower()
        if asked == "4k":
            resolution = "4k"
        else:
            resolution = "1080p"
            if asked not in ("", "1080p"):
                warnings.append(
                    f"a draft finishes at 1080p or 4k only — rendering at 1080p "
                    f"instead of {asked}"
                )
        body: dict = {"draft_task_id": finish_id, "resolution": resolution}
        duration = item_meta.get("draft_duration")
        if is_unset(duration):
            duration = params.get("duration")
        if not is_unset(duration):
            body["duration"] = int(duration)
        watermark = params.get("watermark")
        if not is_unset(watermark):
            body["watermark"] = _to_bool(watermark)
        return body, warnings

    # ── provider calls ────────────────────────────────────────────────────

    def _dispatch(self, payload: dict, prefix: str, spec: EndpointSpec) -> tuple[list[dict], dict]:
        """Return the assets, whichever shape the endpoint uses."""
        request = self._make_request(prefix)
        path = spec.api_path or (VIDEO_SUBMIT_PATH if spec.kind == "video" else IMAGES_PATH)

        if spec.mode == "sync":
            body = {"model": spec.model, **payload}
            record = tasks.call_sync(request, path, body, emit=self._emit, prefix=prefix)
            return tasks.assets_from(record), record

        # Oxen's queue takes the same flat JSON as its synchronous endpoints:
        # the model, the prompt and every setting side by side.
        body = {"model": spec.model, **payload}
        job = tasks.submit_async(request, path, body, emit=self._emit, prefix=prefix)
        record = tasks.await_async(
            request,
            job,
            status_path=VIDEO_STATUS_PATH,
            poll_interval=self._poll_interval,
            timeout=self._task_timeout,
            emit=self._emit,
            prefix=prefix,
        )
        assets = tasks.assets_from(record)
        if not assets and spec.content_path:
            job_id = str(record.get("id") or job.get("id") or "")
            assets = [{"bytes": self._client().request_bytes("GET", spec.content_path.format(id=job_id))}]
        return assets, record

    def _make_request(self, prefix: str) -> Callable[..., dict]:
        """A retrying JSON request bound to this batch's retry policy."""

        def note(attempt: int, total: int, sleep: float, long_backoff: bool) -> None:
            label = "model unavailable" if long_backoff else "rate limit"
            self._emit(f"{prefix} ⏳ {label}, retry {attempt}/{total} in {sleep:.1f}s")

        def request(method: str, path: str, payload: dict | None = None, *,
                    headers: dict[str, str] | None = None,
                    files: dict | None = None) -> dict:
            return transport.request_with_retry(
                self._client(), method, path, payload, headers=headers,
                retries=int(self._profile.get("retry_on_429", 4)),
                base_delay=float(self._profile.get("retry_base_delay", 2.0)),
                on_retry=note,
            )

        return request

    def _make_form_request(self, prefix: str) -> Callable[..., dict]:
        """Video creation takes a form, so it gets its own small wrapper."""

        def request(method: str, path: str, payload: dict | None = None, *,
                    headers: dict[str, str] | None = None, files: dict | None = None) -> dict:
            return self._client().request_form(method, path, payload or {}, files=files or {})

        return request

    # ── outputs ───────────────────────────────────────────────────────────

    def _save_results(self, assets: list[dict], media_type: str, stem: str, index: int,
                      prefix: str, current: int, total: int, rel_dir: str) -> list[Path]:
        dest_dir = self._output_dir / rel_dir if rel_dir else self._output_dir

        def on_saved(dest: Path) -> None:
            self._emit(f"{prefix} 💾 Saved: {dest.name}", current=current,
                       total=total, saved_path=dest)

        return outputs.save_assets(
            self._client(), dest_dir, assets, media_type, stem, index,
            on_download=lambda: self._emit(f"{prefix} ⬇️  Downloading..."),
            on_saved=on_saved,
            decode=base64.b64decode,
        )

    def _error(self, item: InputFile, expected: Path, media_type: str, message: str,
               api_payload: dict | None = None) -> OutputFile:
        metadata = {"api_payload": api_payload} if api_payload else {}
        return OutputFile(source_path=item.path, status="error", error_msg=message,
                          media_type=media_type, expected_path=expected, metadata=metadata)

    # ── preflight ─────────────────────────────────────────────────────────

    def _validate_preflight(self) -> None:
        if not self._profile.get("endpoint"):
            raise EngineError("Missing or empty 'endpoint' in profile")
        if (isinstance(self._concurrency, bool) or not isinstance(self._concurrency, int)
                or self._concurrency < 1):
            raise EngineError(
                f"Invalid 'concurrency' in profile: {self._concurrency!r} — "
                "must be a whole number >= 1"
            )
        if not self._resolve_api_key():
            raise EngineError(f"{self.API_KEY_ENV_VAR} not set in environment or .env file")

    def _resolve_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        return os.environ.get(self.API_KEY_ENV_VAR, "")

    def _client(self) -> transport.Client:
        with self._client_lock:
            if self._client_instance is None:
                timeout = _positive_float(self._profile.get("http_timeout"), transport.DEFAULT_TIMEOUT)
                self._client_instance = transport.Client(
                    api_key=self._resolve_api_key(), base_url=self._base_url, timeout=timeout
                )
            return self._client_instance

    # ── small state ───────────────────────────────────────────────────────

    def _media_type(self, spec: EndpointSpec | None = None) -> str:
        value = self._profile.get("media_type")
        if value:
            return str(value)
        if spec is not None:
            return {"image": "image", "video": "video", "text": "text", "audio": "audio"}.get(
                spec.kind, "image")
        return "image"

    def _note_retryable(self) -> int:
        with self._counter_lock:
            self._consecutive_retryable += 1
            return self._consecutive_retryable

    def _reset_breaker(self) -> None:
        with self._counter_lock:
            self._consecutive_retryable = 0

    def _emit(self, message: str, level: str = "info", current: int = 0, total: int = 0,
              saved_path: Path | None = None, api_payload: dict | None = None) -> None:
        if self._on_progress:
            self._on_progress(ProgressEvent(message=message, level=level, current=current,
                                            total=total, saved_path=saved_path,
                                            api_payload=api_payload))


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
